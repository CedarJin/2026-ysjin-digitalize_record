"""Authenticated, same-origin browser interface for the review service."""

from __future__ import annotations

import csv
import io
import json
import secrets
import threading
import time
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .day_review import DayReviewService
from .db import Audit, Batch, ReviewRelease, now
from .imaging import render_pages
from .ingest import InputError, ingest_batch, inspect_inputs, read_metadata
from .review import ReviewError, future
from .schemas import RecordContent

STATIC = Path(__file__).parent / "static"


def release_zip(release):
    payload = release.payload
    entries = {
        "records.json": json.dumps(payload["records"], ensure_ascii=False, indent=2).encode(),
        "processing_report.json": json.dumps(payload["processing_report"], indent=2).encode(),
    }

    def table(name, rows, fields):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            clean = {
                k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                for k, v in row.items()
                if k in fields
            }
            # Avoid spreadsheet formula execution when researchers open CSVs.
            clean = {
                k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v)
                for k, v in clean.items()
            }
            writer.writerow(clean)
        entries[name] = stream.getvalue().encode()

    def intake_fields(record, item, kind):
        if kind not in {"items", "events"}:
            return {}
        content = record["content"]
        event = (
            next((e for e in content["events"] if e["event_id"] == item.get("event_id")), {})
            if kind == "items"
            else item
        )
        day = next((d for d in content["days"] if d["day_id"] == event.get("day_id")), {})
        value = day.get("date", {})
        return {
            "review_group_date": record.get("date"),
            "date": value.get("normalized"),
            "intake_date": value.get("normalized"),
            "date_missing_reason": value.get("missing_reason"),
            "date_needs_confirmation": day.get("date_needs_confirmation", False),
        }

    for key, filename, idfield in [
        ("days", "record_days.csv", "day_id"),
        ("events", "eating_events.csv", "event_id"),
        ("items", "food_items.csv", "item_id"),
        ("issues", "issues.csv", "code"),
    ]:
        rows = [
            {
                "record_id": record["record_id"],
                **(
                    {k: record[k] for k in ("subject_id", "date", "source_record_ids")}
                    if record.get("review_unit_type")
                    else {}
                ),
                **item,
                **intake_fields(record, item, key),
            }
            for record in payload["records"]
            for item in record["content"][key]
        ]
        fields = (
            ["record_id", idfield] + sorted(set().union(*(set(x) for x in rows)) - {"record_id", idfield})
            if rows
            else ["record_id", idfield]
        )
        table(filename, rows, fields)
    table(
        "processing_report.csv",
        payload["processing_report"],
        ["record_id", "input_version", "state", "included"],
    )
    if payload.get("review_unit_type"):
        entries["scope.json"] = json.dumps({"review_unit_type": payload["review_unit_type"]}).encode()
        for name in ("source_processing_report", "unresolved_source_days"):
            entries[name + ".json"] = json.dumps(payload.get(name, []), indent=2).encode()
    entries["actors.json"] = json.dumps(payload.get("actors", []), indent=2).encode()
    entries["review_history.json"] = json.dumps(
        payload.get("review_history", []), ensure_ascii=False, indent=2
    ).encode()
    import hashlib

    manifest = {
        "release_id": release.id,
        "created_at": release.created_at,
        "created_by": release.actor_id,
        "research_approved": True,
        "payload_sha256": release.checksum,
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in entries.items()},
    }
    entries["export_manifest.json"] = json.dumps(manifest, indent=2).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    return stream.getvalue()


def create_app(
    workspace,
    secure_cookies=False,
    allowed_hosts=None,
    *,
    service_factory=DayReviewService,
    desktop_identity=None,
):
    service = service_factory(workspace)
    app = FastAPI(title="Food record review", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.review = service
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["127.0.0.1", "localhost", "testserver"]
    )
    attempts = defaultdict(deque)
    rate_lock = threading.Lock()

    @app.exception_handler(ReviewError)
    async def review_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {
                "error": "The record does not satisfy the data schema. Check missing reasons, evidence and references.",
                "fields": [
                    {"path": ".".join(map(str, e["loc"])), "message": e["msg"]}
                    for e in exc.errors(include_input=False)
                ],
            },
            status_code=422,
        )

    @app.exception_handler(KeyError)
    @app.exception_handler(TypeError)
    @app.exception_handler(ValueError)
    async def invalid_request(request, exc):
        return JSONResponse({"error": "Invalid request fields or values."}, status_code=400)

    @app.exception_handler(InputError)
    async def input_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(StaleDataError)
    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return JSONResponse(
            {"error": "Another operation changed this resource. Refresh and try again."}, status_code=409
        )

    @app.middleware("http")
    async def security(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": "Cross-origin writes are not allowed."}, status_code=403)
            length = request.headers.get("content-length")
            try:
                limit = (
                    1024 * 1024 * 1024
                    if desktop_identity and request.url.path == "/desktop/import/upload"
                    else 64 * 1024 * 1024
                )
                if length is None or not 0 <= int(length) <= limit:
                    return JSONResponse({"error": "Request exceeds the 64 MB upload limit."}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "Invalid request size."}, status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        )
        return response

    def user(request, owner=False):
        # Only the capability-protected desktop wrapper supplies this identity.
        who = desktop_identity or service.authenticate(request.cookies.get("review_session"))
        if owner and who["role"] != "owner":
            raise ReviewError("Owner access required.", 403)
        if request.method not in {"GET", "HEAD"} and request.headers.get("x-csrf-token") != who["csrf"]:
            raise ReviewError("Session verification failed. Refresh before saving.", 403)
        return who

    def throttle(request, key):
        host = request.client.host if request.client else "unknown"
        with rate_lock:
            bucket = attempts[(host, key)]
            current = time.monotonic()
            while bucket and bucket[0] < current - 60:
                bucket.popleft()
            if len(bucket) >= 10:
                raise ReviewError("Too many attempts. Wait one minute before trying again.", 429)
            bucket.append(current)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/static/{name}")
    def asset(name: str):
        if name not in {"app.js", "review_flow.js", "style.css"}:
            raise ReviewError("Not found.", 404)
        return FileResponse(STATIC / name)

    @app.post("/api/login")
    async def login(request: Request):
        throttle(request, "login")
        body = await request.json()
        token, csrf = service.login(str(body.get("email", "")), str(body.get("password", "")))
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(
            "review_session",
            token,
            httponly=True,
            secure=secure_cookies,
            samesite="strict",
            max_age=43200,
            path="/",
        )
        return response

    @app.post("/api/accept-invite")
    async def accept(request: Request):
        throttle(request, "invite")
        body = await request.json()
        service.accept_invite(str(body.get("token", "")), str(body.get("password", "")))
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        return user(request)

    @app.post("/api/logout")
    def logout(request: Request):
        user(request)
        service.logout(request.cookies.get("review_session"))
        response = JSONResponse({"ok": True})
        response.delete_cookie("review_session")
        return response

    @app.get("/api/records")
    def queue(request: Request, batch_id: str | None = None):
        who = user(request)
        rows = service.queue(who["id"])
        if batch_id:
            with service.factory() as s:
                batch = s.get(Batch, batch_id)
                if not batch or batch.project_id != service.project_id:
                    raise ReviewError("Batch not found.", 404)
                rows = [
                    r
                    for r in rows
                    if set(r.get("source_record_ids", [r["record_id"]])) & set(batch.manifest["groups"])
                ]
        return rows

    @app.get("/api/records/{record_id}")
    def detail(request: Request, record_id: str):
        return service.detail(user(request)["id"], record_id)

    @app.get("/api/records/{record_id}/source/{source_id}/{page}")
    def image(request: Request, record_id: str, source_id: str, page: int):
        who = user(request)
        with service.factory() as s:
            actor = service.actor(s, who["id"])
            record = service.record(s, record_id)
            service.access(s, actor, record)
            snapshot = service.snapshot(s, record)
            document = snapshot["documents"].get(source_id)
            if not document or not 1 <= page <= document["page_count"]:
                raise ReviewError("Source page not found.", 404)
        pages = render_pages(
            workspace.store, {"documents": {source_id: document}}, workspace.path / "rendered", 150
        )
        return FileResponse(
            next(p["path"] for p in pages if p["page_number"] == page), media_type="image/png"
        )

    @app.get("/api/records/{record_id}/history")
    def history(request: Request, record_id: str):
        who = user(request)
        with service.factory() as s:
            actor = service.actor(s, who["id"])
            record = service.record(s, record_id)
            service.access(s, actor, record)
            tasks = list(
                s.scalars(select(service.ReviewTask).where(service.ReviewTask.record_pk == record.id))
            )
            # Never expose another reviewer's autosaved draft through history.
            ids = [t.id for t in tasks if t.assignee_id == actor.id]
            edits = list(
                s.scalars(
                    select(service.ReviewEdit)
                    .where(service.ReviewEdit.task_id.in_(ids))
                    .order_by(service.ReviewEdit.created_at.desc())
                )
            )
            audits = (
                list(
                    s.scalars(
                        select(Audit)
                        .where(
                            Audit.project_id == service.project_id,
                            Audit.target_id.in_([record.id, *[t.id for t in tasks]]),
                        )
                        .order_by(Audit.created_at.desc())
                    )
                )
                if actor.role == "owner"
                else []
            )
            return {
                "edits": [
                    {
                        "id": e.id,
                        "task_id": e.task_id,
                        "revision": e.revision,
                        "at": e.created_at,
                        "reason": e.reason,
                        "content": e.content,
                    }
                    for e in edits
                ],
                "events": [
                    {"at": a.created_at, "actor_id": a.actor_id, "action": a.action, "detail": a.detail}
                    for a in audits
                ],
            }

    @app.get("/api/grouping")
    def grouping(request: Request):
        return service.placements(user(request, True)["id"])

    @app.post("/api/grouping/{placement_id}")
    async def regroup(request: Request, placement_id: str):
        who = user(request, True)
        body = await request.json()
        return service.resolve_placement(
            who["id"], placement_id, body["revision"], body["subject_id"], body["date"], body["reason"]
        )

    @app.post("/api/grouping/{placement_id}/additional-day")
    async def additional_day(request: Request, placement_id: str):
        who = user(request, True)
        body = await request.json()
        return service.add_source_day(
            who["id"], placement_id, body["revision"], body["subject_id"], body["date"], body["reason"]
        )

    @app.get("/api/grouping/{placement_id}/source/{source_id}/{page}")
    def grouping_image(request: Request, placement_id: str, source_id: str, page: int):
        snapshot = service.placement_snapshot(user(request, True)["id"], placement_id)
        document = snapshot["documents"].get(source_id)
        if not document or not 1 <= page <= document["page_count"]:
            raise ReviewError("Source page not found.", 404)
        pages = render_pages(
            workspace.store, {"documents": {source_id: document}}, workspace.path / "rendered", 150
        )
        return FileResponse(
            next(p["path"] for p in pages if p["page_number"] == page), media_type="image/png"
        )

    @app.get("/api/record-review-archive")
    def archive(request: Request):
        return service.archive(user(request)["id"])

    @app.post("/api/record-review-archive/{task_id}/hide")
    def hide_archive(request: Request, task_id: str):
        return service.hide_archive(user(request)["id"], task_id)

    @app.get("/api/offline/settings")
    def offline_settings(request: Request):
        from .db import OfflineSettings

        user(request, True)
        with service.factory() as s:
            settings = s.get(OfflineSettings, service.project_id)
            return {"units_per_package": settings.units_per_package if settings else 10}

    @app.get("/api/offline/receipts/{receipt_id}/download")
    def offline_receipt_download(request: Request, receipt_id: str):
        from .db import OfflinePackage, OfflineReceipt
        from .offline_archive import sha256

        who = user(request, True)
        with service.factory.begin() as s:
            receipt = s.get(OfflineReceipt, receipt_id)
            package = s.get(OfflinePackage, receipt.package_id) if receipt else None
            if not package or package.project_id != service.project_id:
                raise ReviewError("Return receipt not found.", 404)
            if package.reviewer_id != who["id"]:
                actor = service.actor(s, who["id"], True)
                for unit in package.manifest["units"]:
                    service.audit(
                        s,
                        actor,
                        "review_answers_viewed",
                        unit["id"],
                        {"input_version": unit["input_version"], "kind": "offline_return"},
                    )
                    for member in unit["snapshot"]["members"]:
                        service.audit(
                            s,
                            actor,
                            "review_answers_viewed",
                            member["record_pk"],
                            {"input_version": member["input_version"], "kind": "offline_return"},
                        )
            path = workspace.path / "offline" / "results" / (receipt.result_sha256 + ".zip")
            if not path.exists() or sha256(path.read_bytes()) != receipt.result_sha256:
                raise ReviewError("Stored result is missing or damaged.")
        return FileResponse(
            path, media_type="application/zip", filename="returned-result-" + receipt_id + ".zip"
        )

    @app.get("/api/offline/receipts/{receipt_id}")
    def offline_receipt_detail(request: Request, receipt_id: str):
        from .db import OfflinePackage, OfflineReceipt
        from .offline_archive import sha256, unpack

        who = user(request, True)
        with service.factory.begin() as s:
            receipt = s.get(OfflineReceipt, receipt_id)
            package = s.get(OfflinePackage, receipt.package_id) if receipt else None
            if not package or package.project_id != service.project_id:
                raise ReviewError("Return receipt not found.", 404)
            if package.reviewer_id != who["id"]:
                actor = service.actor(s, who["id"], True)
                for unit in package.manifest["units"]:
                    service.audit(
                        s,
                        actor,
                        "review_answers_viewed",
                        unit["id"],
                        {"input_version": unit["input_version"], "kind": "offline_return"},
                    )
                    for member in unit["snapshot"]["members"]:
                        service.audit(
                            s,
                            actor,
                            "review_answers_viewed",
                            member["record_pk"],
                            {"input_version": member["input_version"], "kind": "offline_return"},
                        )
            data = (workspace.path / "offline" / "results" / (receipt.result_sha256 + ".zip")).read_bytes()
            if sha256(data) != receipt.result_sha256:
                raise ReviewError("Stored result checksum mismatch.")
            returned, _ = unpack(data, "result")
            labels = {
                u["task_id"]: {k: u[k] for k in ("subject_id", "date", "content")}
                for u in package.manifest["units"]
            }
            return {"returned": returned, "baseline": labels, "outcome": receipt.outcome}

    @app.post("/api/offline/reviewers")
    async def offline_reviewer(request: Request):
        from .offline import OfflineManager

        who = user(request, True)
        body = await request.json()
        if not isinstance(body.get("name"), str) or not isinstance(body.get("email"), str):
            raise ReviewError("Enter a reviewer name and email.")
        return {"id": OfflineManager(workspace).reviewer(who["id"], body["name"], body["email"])}

    @app.post("/api/offline/export")
    async def offline_export(request: Request):
        from .offline import OfflineManager

        who = user(request, True)
        body = await request.json()
        ids = body.get("unit_ids")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise ReviewError("Select valid daily units.")
        if not isinstance(body.get("reviewer_id"), str) or type(body.get("preview", False)) is not bool:
            raise ReviewError("Select a reviewer and a valid export mode.")
        rows = OfflineManager(workspace).export(
            who["id"], body["reviewer_id"], ids, body.get("units_per_package"), body.get("preview", False)
        )
        return [{k: v for k, v in row.items() if k != "path"} for row in rows]

    @app.get("/api/offline/packages")
    def offline_packages(request: Request):
        from .db import OfflinePackage, OfflineReceipt, ReviewUser

        user(request, True)
        with service.factory() as s:
            rows = []
            for package in s.scalars(
                select(OfflinePackage)
                .where(OfflinePackage.project_id == service.project_id)
                .order_by(OfflinePackage.created_at.desc())
            ):
                reviewer = s.get(ReviewUser, package.reviewer_id)
                receipts = list(
                    s.scalars(
                        select(OfflineReceipt)
                        .where(OfflineReceipt.package_id == package.id)
                        .order_by(OfflineReceipt.created_at)
                    )
                )
                rows.append(
                    {
                        "id": package.id,
                        "reviewer": reviewer.name,
                        "email": reviewer.email,
                        "created_at": package.created_at,
                        "days": len(package.manifest["units"]),
                        "receipts": [
                            {"id": r.id, "created_at": r.created_at, "outcome": r.outcome} for r in receipts
                        ],
                    }
                )
            return rows

    @app.get("/api/offline/packages/{package_id}/download")
    def offline_download(request: Request, package_id: str):
        from .db import OfflinePackage
        from .offline_archive import sha256, task_filename

        user(request, True)
        with service.factory() as s:
            package = s.get(OfflinePackage, package_id)
            if not package or package.project_id != service.project_id:
                raise ReviewError("Package not found.", 404)
            path = workspace.path / "offline" / "packages" / (package.archive_sha256 + ".zip")
            if not path.exists() or sha256(path.read_bytes()) != package.archive_sha256:
                raise ReviewError("Stored package is missing or damaged.")
            filename = task_filename(package.manifest)
        return FileResponse(path, media_type="application/zip", filename=filename)

    @app.post("/api/offline/import-result")
    async def offline_return(request: Request):
        from .offline import OfflineManager

        who = user(request, True)
        form = await request.form()
        upload = form.get("file")
        if not upload or not hasattr(upload, "read"):
            raise ReviewError("Choose a result package.")
        data = await upload.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            raise ReviewError("Result exceeds the 64 MB browser limit. Use the local import command.")
        return OfflineManager(workspace).import_result(who["id"], data)

    @app.get("/api/members")
    def members(request: Request):
        return service.members(user(request, True)["id"])

    @app.post("/api/members")
    async def invite(request: Request):
        who = user(request, True)
        body = await request.json()
        return service.invite(who["id"], body["email"], body["name"], body.get("role", "reviewer"))

    @app.post("/api/members/{member_id}/status")
    async def member_status(request: Request, member_id: str):
        who = user(request, True)
        body = await request.json()
        service.set_member_active(who["id"], member_id, bool(body["active"]))
        return {"ok": True}

    @app.post("/api/members/{member_id}/invite")
    def renew_invite(request: Request, member_id: str):
        return service.renew_invite(user(request, True)["id"], member_id)

    @app.post("/api/records/{record_id}/restore")
    async def restore(request: Request, record_id: str):
        who = user(request, True)
        body = await request.json()
        service.restore(who["id"], record_id, body["reason"])
        return {"ok": True}

    @app.get("/api/policy")
    def policy(request: Request):
        user(request, True)
        with service.factory() as s:
            p = service.policy(s)
            return {"id": p.id, "settings": p.settings}

    @app.post("/api/policy")
    async def change_policy(request: Request):
        who = user(request, True)
        return {"id": service.set_policy(who["id"], await request.json())}

    @app.post("/api/assign")
    async def assign(request: Request):
        who = user(request, True)
        body = await request.json()
        results = []
        for record_id in body["record_ids"]:
            try:
                results.append(
                    {
                        "record_id": record_id,
                        "task_id": service.assign(
                            who["id"], record_id, body["assignee_id"], body.get("due_at")
                        ),
                    }
                )
            except ReviewError as exc:
                results.append({"record_id": record_id, "error": str(exc)})
        return results

    @app.put("/api/tasks/{task_id}")
    async def save(request: Request, task_id: str):
        who = user(request)
        body = await request.json()
        return service.save(
            who["id"], task_id, body["revision"], body["content"], body.get("reason", "Draft saved")
        )

    @app.post("/api/tasks/{task_id}/submit")
    async def submit(request: Request, task_id: str):
        who = user(request)
        body = await request.json()
        return {"id": service.submit(who["id"], task_id, body["revision"])}

    @app.post("/api/tasks/{task_id}/return")
    async def return_task(request: Request, task_id: str):
        who = user(request, True)
        body = await request.json()
        service.cancel(who["id"], task_id, body["reason"])
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/help")
    async def help_request(request: Request, task_id: str):
        who = user(request)
        body = await request.json()
        return {"id": service.request_help(who["id"], task_id, body["question"])}

    @app.post("/api/help/{help_id}/assign")
    async def help_assign(request: Request, help_id: str):
        who = user(request, True)
        body = await request.json()
        service.assign_help(who["id"], help_id, body["helper_id"])
        return {"ok": True}

    @app.post("/api/help/{help_id}/reply")
    async def help_reply(request: Request, help_id: str):
        who = user(request)
        body = await request.json()
        service.help_reply(who["id"], help_id, body["message"], body.get("close", False))
        return {"ok": True}

    @app.post("/api/records/{record_id}/adjudicate")
    async def adjudicate(request: Request, record_id: str):
        who = user(request, True)
        body = await request.json()
        return {"id": service.adjudicate(who["id"], record_id, body["content"], body["reason"])}

    @app.post("/api/approve")
    async def approve(request: Request):
        who = user(request, True)
        body = await request.json()
        results = []
        for rid in body["record_ids"]:
            try:
                results.append({"record_id": rid, "approval_id": service.approve(who["id"], rid)})
            except ReviewError as exc:
                results.append({"record_id": rid, "error": str(exc)})
        return results

    @app.post("/api/records/{record_id}/exclude")
    async def exclude(request: Request, record_id: str):
        who = user(request, True)
        body = await request.json()
        service.exclude(who["id"], record_id, body["reason"])
        return {"ok": True}

    @app.get("/api/releases")
    def releases(request: Request):
        user(request, True)
        with service.factory() as s:
            return [
                {
                    "id": r.id,
                    "created_at": r.created_at,
                    "records": len(r.payload["records"]),
                    "checksum": r.checksum,
                }
                for r in s.scalars(
                    select(ReviewRelease)
                    .where(ReviewRelease.project_id == service.project_id)
                    .order_by(ReviewRelease.created_at.desc())
                )
            ]

    @app.post("/api/releases")
    async def release(request: Request):
        who = user(request, True)
        body = await request.json()
        return {"id": service.release(who["id"], body["record_ids"])}

    @app.get("/api/releases/{release_id}/download")
    def download(request: Request, release_id: str):
        user(request, True)
        with service.factory() as s:
            release = s.get(ReviewRelease, release_id)
            if not release or release.project_id != service.project_id:
                raise ReviewError("Release not found.", 404)
            return Response(
                release_zip(release),
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="release-{release.id}.zip"'},
            )

    @app.post("/api/import/preview")
    async def preview(request: Request):
        who = user(request, True)
        form = await request.form(max_files=201, max_fields=10, max_part_size=10 * 1024 * 1024)
        uploads = form.getlist("files")
        if not uploads or len(uploads) > 200:
            raise ReviewError("Choose between 1 and 200 PDFs.")
        token = secrets.token_hex(20)
        directory = workspace.path / "review_uploads" / token
        directory.mkdir(parents=True)
        paths = []
        for upload in uploads:
            filename = upload.filename or ""
            if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
                raise ReviewError("Upload named PDF files only.")
            data = await upload.read(10 * 1024 * 1024 + 1)
            if len(data) > 10 * 1024 * 1024 or not data.startswith(b"%PDF-"):
                raise ReviewError("Each PDF must be valid and at most 10 MB.")
            path = directory / filename
            if path.exists():
                raise ReviewError("Duplicate upload filename.")
            with path.open("xb") as handle:
                handle.write(data)
            paths.append(path)
        metadata = form.get("metadata_file")
        if metadata and getattr(metadata, "filename", None):
            suffix = Path(metadata.filename).suffix.lower()
            if suffix not in {".xlsx", ".csv", ".json"}:
                raise ReviewError("Metadata must be JSON, CSV or XLSX.")
            path = directory / ("metadata" + suffix)
            data = await metadata.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ReviewError("Metadata exceeds 2 MB.")
            path.write_bytes(data)
        else:
            path = directory / "metadata.json"
            path.write_text(str(form.get("metadata_json", "[]")))
        rows = read_metadata(path)
        report = inspect_inputs(paths, rows)
        manifest = {
            "owner": who["id"],
            "expires": future(1),
            "files": [p.name for p in paths],
            "metadata": path.name,
            "report": report,
        }
        (directory / "manifest.json").write_text(json.dumps(manifest))
        return {"token": token, "report": report}

    @app.post("/api/import/confirm")
    async def confirm(request: Request):
        who = user(request, True)
        body = await request.json()
        token = body.get("token", "")
        if len(token) != 40 or any(c not in "0123456789abcdef" for c in token):
            raise ReviewError("Invalid upload preview.")
        directory = workspace.path / "review_uploads" / token
        if not (directory / "manifest.json").exists():
            raise ReviewError("Upload preview expired.", 404)
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["owner"] != who["id"] or manifest["expires"] <= now():
            raise ReviewError("Upload preview expired.", 403)
        report = ingest_batch(
            workspace.factory,
            workspace.store,
            workspace.project_id,
            [directory / name for name in manifest["files"]],
            read_metadata(directory / manifest["metadata"]),
        )
        with service.factory.begin() as s:
            service.audit(
                s,
                service.actor(s, who["id"]),
                "browser_imported",
                report["batch_id"],
                {"changed": report["changed"]},
            )
        return report

    @app.get("/api/schema")
    def schema(request: Request):
        user(request)
        return RecordContent.model_json_schema()

    return app
