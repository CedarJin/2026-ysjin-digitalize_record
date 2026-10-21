"""Private desktop backend. Capability is supplied over stdin, never in URLs or argv."""

from __future__ import annotations

import fcntl
import hmac
import json
import os
import secrets
import socket
import sys
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import db
from .backup import create_backup, restore_backup
from .desktop_archive import inspect_archive
from .desktop_attachments import previous_attachment_draft, stage_attachment
from .desktop_export import export_archive
from .desktop_import import commit_plan, preview_plan
from .desktop_inputs import reserve_id, stage_proposals
from .desktop_queue import DEFAULTS, DesktopQueue
from .ingest import InputError, current_snapshot, digest
from .review import DEFAULT_POLICY, ReviewError
from .review_web import STATIC, create_app
from .schemas import Metadata
from .workspace import Workspace


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default=DEFAULTS["model"], min_length=1, max_length=100)
    concurrency: int = Field(default=3, ge=1, le=8)
    max_output_tokens: int = Field(default=32000, ge=1000, le=64000)
    max_output_ceiling: int = Field(default=64000, ge=1000, le=128000)
    timeout: float = Field(default=1200, ge=30, le=3600)
    image_max_edge: int = Field(default=1200, ge=1000, le=4096)
    jpeg_quality: int = Field(default=70, ge=60, le=95)
    base_url: str = "https://api.openai.com/v1"
    dpi: int = Field(default=150, ge=72, le=300)
    max_pages: int = Field(default=20, ge=1, le=100)

    def checked(self):
        if self.base_url not in {
            "https://api.openai.com/v1",
            "https://us.api.openai.com/v1",
            "https://eu.api.openai.com/v1",
        }:
            raise InputError("Choose an official OpenAI endpoint.")
        if self.max_output_ceiling < self.max_output_tokens:
            raise InputError("Output ceiling must be at least the initial output limit.")
        return self.model_dump()


def identity(workspace):
    with workspace.factory.begin() as s:
        user = s.scalar(
            select(db.ReviewUser).where(
                db.ReviewUser.project_id == workspace.project_id,
                db.ReviewUser.email == "desktop@local.invalid",
            )
        )
        if not user:
            user = db.ReviewUser(
                project_id=workspace.project_id,
                email="desktop@local.invalid",
                name="Local study owner",
                role="owner",
            )
            s.add(user)
            s.flush()
        if not s.scalar(select(db.ReviewPolicy).where(db.ReviewPolicy.project_id == workspace.project_id)):
            s.add(
                db.ReviewPolicy(project_id=workspace.project_id, settings=DEFAULT_POLICY, created_by=user.id)
            )
        return dict(
            id=user.id,
            name=user.name,
            role="owner",
            email=user.email,
            project_id=workspace.project_id,
            csrf=secrets.token_urlsafe(32),
            desktop=True,
        )


def protected(app, capability):
    @app.middleware("http")
    async def capability_guard(request, call_next):
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if (
            host.split(":")[0] not in {"127.0.0.1", "testserver"}
            or not hmac.compare_digest(request.headers.get("x-desktop-capability", ""), capability)
            or (origin and origin != "http://" + host)
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            return JSONResponse({"error": "Open this study from the desktop app."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    return app


def study_app(workspace, capability):
    who = identity(workspace)
    app = create_app(workspace, desktop_identity=who)

    @app.exception_handler(OSError)
    async def file_operation_error(_request, _error):
        return JSONResponse(
            {
                "error": "File operation failed. Check free space and Study folder permissions, then retry. Existing saved data remains available."
            },
            status_code=503,
        )

    queue = DesktopQueue(workspace)
    app.state.queue = queue
    # Keep the mature review UI at /review; the new study dashboard owns /.
    for route in app.router.routes:
        if getattr(route, "path", None) == "/":
            route.path = "/review"
            import re

            route.path_regex = re.compile("^/review$")
    settings_path = workspace.path / "desktop-settings.json"
    if not settings_path.exists():
        with workspace.factory() as session:
            latest = session.scalar(
                select(db.ExtractionRun)
                .join(db.Record)
                .where(db.Record.project_id == workspace.project_id, db.ExtractionRun.status == "succeeded")
                .order_by(db.ExtractionRun.created_at.desc())
            )
            if latest:
                config = latest.configuration
                values = {
                    **DEFAULTS,
                    **{
                        k: config[k]
                        for k in ("model", "dpi", "max_pages", "max_output_tokens")
                        if k in config
                    },
                }
                values["base_url"] = config.get("endpoint", DEFAULTS["base_url"])
                settings_path.write_text(json.dumps(Settings.model_validate(values).checked(), indent=2))

    def settings():
        return Settings.model_validate(
            json.loads(settings_path.read_text()) if settings_path.exists() else DEFAULTS
        ).checked()

    @app.get("/")
    def dashboard():
        return FileResponse(STATIC / "desktop.html")

    @app.get("/desktop/assets/{name}")
    def asset(name: str):
        if name not in {"desktop.js", "desktop.css"}:
            raise InputError("Unknown asset")
        return FileResponse(STATIC / name)

    @app.post("/desktop/study-name")
    async def rename_study(request: Request):
        body = await request.json()
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 200:
            raise InputError("Enter a study name of 1–200 characters.")
        with workspace.factory.begin() as session:
            session.get(db.Project, workspace.project_id).name = name
        return {"name": name}

    @app.get("/desktop/state")
    def state():
        with workspace.factory() as s:
            name = s.get(db.Project, workspace.project_id).name
        pending_import, pending_import_error = None, None
        pointer = workspace.path / "desktop-current-import.json"
        if pointer.exists():
            try:
                token = json.loads(pointer.read_text())["token"]
                if (
                    not isinstance(token, str)
                    or len(token) != 40
                    or any(c not in "0123456789abcdef" for c in token)
                ):
                    raise ValueError("Invalid import token")
                if not (workspace.path / "desktop-imports" / token / "draft.json").is_file():
                    raise ValueError("Missing import draft")
                pending_import = token
            except (OSError, ValueError, KeyError, TypeError):
                pending_import_error = "The unfinished import could not be opened. Existing Study records are safe. Restore a verified backup to recover that import, or start a new import from the original files."
        return dict(
            name=name,
            pending_import=pending_import,
            pending_import_error=pending_import_error,
            path=str(workspace.path),
            records=queue.records(),
            queue=queue.status(),
            settings=settings(),
            has_key=bool(queue.key),
            csrf=who["csrf"],
        )

    @app.post("/desktop/settings")
    def save_settings(body: Settings):
        value = body.checked()
        temp = settings_path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, indent=2))
        temp.replace(settings_path)
        return value

    @app.post("/desktop/process/preview")
    async def preview(request: Request):
        return queue.preview((await request.json())["ids"])

    @app.post("/desktop/process/start")
    async def process(request: Request):
        body = await request.json()
        if body.get("confirmed") is not True:
            raise InputError("Confirm the API processing scope and fees first.")
        return queue.enqueue(body["ids"], settings(), body["versions"], body.get("retry_unknown", False))

    @app.post("/desktop/process/pause")
    def pause():
        queue.paused = True
        return queue.status()

    @app.post("/desktop/manual/{record_id}")
    def manual(record_id: str):
        from .schemas import PageCoverage, RecordContent

        with queue.lock, workspace.factory.begin() as s:
            record = s.scalar(
                select(db.Record).where(
                    db.Record.project_id == workspace.project_id, db.Record.record_id == record_id
                )
            )
            if not record or record.active_run_id or queue.status()["active"] or queue.status()["queued"]:
                raise InputError("Finish processing before creating a manual draft.")
            if record.state in {"missing_source", "blocked", "excluded", "exclusion_pending"}:
                raise InputError("A readable main PDF and participant metadata are required.")
            if s.scalar(
                select(db.Candidate).where(
                    db.Candidate.record_pk == record.id, db.Candidate.input_version == record.input_version
                )
            ):
                return {"reused": True}
            snapshot = current_snapshot(s, record)
            content = RecordContent(
                record_id=record_id,
                pages=[
                    PageCoverage(
                        source_record_id=sid,
                        page_number=n,
                        classification="food_record" if doc["role"] == "main" else "clarification",
                    )
                    for sid, doc in snapshot["documents"].items()
                    for n in range(1, doc["page_count"] + 1)
                ],
            )
            provenance = {"method": "manual_entry", "created_by": who["id"]}
            prior = previous_attachment_draft(s, record, snapshot)
            if prior:
                content, previous_provenance = prior
                provenance.update(previous_provenance)
            s.add(
                db.Candidate(
                    record_pk=record.id,
                    input_version=record.input_version,
                    content=content.model_dump(mode="json"),
                    provenance=provenance,
                )
            )
            record.state = "pending_review"
        return {"created": True}

    @app.post("/api/tasks/{task_id}/reopen")
    async def reopen(task_id: str, request: Request):
        import copy

        body = await request.json()
        service = app.state.review
        with workspace.factory.begin() as s:
            old = s.get(db.DayReviewTask, task_id)
            if not old or old.assignee_id != who["id"] or old.status != "submitted":
                raise ReviewError("Only your submitted review can be reopened.", 409)
            record = s.get(db.DayReviewUnit, old.record_pk)
            if record.project_id != workspace.project_id or not service.fresh(s, old, record):
                raise ReviewError("The sources changed. Assign a new review instead.", 409)
            if old.revision != body["revision"]:
                raise ReviewError("Review changed. Refresh before reopening.", 409)
            data = copy.deepcopy(old.draft)
            if record.approved_id:
                data = copy.deepcopy(s.get(db.DayReviewApproval, record.approved_id).payload["content"])
            old.status = "returned"
            task = db.DayReviewTask(
                record_pk=record.id,
                input_version=record.input_version,
                candidate_id=old.candidate_id,
                policy_id=old.policy_id,
                assignee_id=who["id"],
                draft=data,
            )
            s.add(task)
            s.flush()
            record.approved_id = None
            record.state = "in_review"
            service.audit(
                s,
                service.actor(s, who["id"]),
                "desktop_review_reopened",
                task.id,
                {"previous_task_id": old.id, "old_releases_unchanged": True},
            )
            return {"task_id": task.id}

    @app.post("/desktop/process/resume")
    def resume():
        if not queue.key:
            raise InputError("Add your API key in Settings before resuming queued requests.")
        queue.paused = False
        return queue.status()

    @app.post("/desktop/import/upload")
    async def upload(request: Request):
        form = await request.form(max_files=1001, max_fields=10)
        uploads = form.getlist("files")
        if not 1 <= len(uploads) <= 1000:
            raise InputError("Select 1–1000 PDF files.")
        token = secrets.token_hex(20)
        directory = workspace.path / "desktop-imports" / token
        directory.mkdir(parents=True)
        descriptors = []
        for index, item in enumerate(uploads):
            if not item.filename.lower().endswith(".pdf"):
                raise InputError("Only PDF sources are supported.")
            payload = await item.read(32 * 1024 * 1024 + 1)
            if len(payload) > 32 * 1024 * 1024 or not payload.startswith(b"%PDF-"):
                raise InputError("Each PDF must be valid and at most 32 MB.")
            name = f"file-{index}.pdf"
            (directory / name).write_bytes(payload)
            descriptors.append(dict(file=name, filename=item.filename))
        metadata = form.get("metadata")
        metadata_path = None
        if metadata and getattr(metadata, "filename", None):
            suffix = Path(metadata.filename).suffix.lower()
            if suffix not in {".csv", ".xlsx", ".json"}:
                raise InputError("Choose CSV, XLSX or JSON metadata.")
            path = directory / ("metadata" + suffix)
            payload = await metadata.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise InputError("Metadata must be at most 4 MB.")
            path.write_bytes(payload)
            metadata_path = path
        return stage_proposals(
            workspace, directory, descriptors, metadata_path, metadata.filename if metadata_path else None
        )

    @app.post("/desktop/attachments/{record_id}")
    async def add_attachment(record_id: str, request: Request):
        form = await request.form(max_files=1, max_fields=4)
        upload = form.get("file")
        if upload and getattr(upload, "filename", None):
            payload = await upload.read(32 * 1024 * 1024 + 1)
            filename = Path(upload.filename).name
        else:
            payload = str(form.get("text", "")).encode("utf-8")
            filename = "Attachment note.txt"
        with queue.lock:
            if queue.status()["active"] or queue.status()["queued"]:
                raise InputError("Finish queued processing before adding attachments.")
            record_metadata(record_id)
            return stage_attachment(workspace, record_id, payload, filename, form.get("origin"))

    @app.get("/desktop/metadata/{record_id}")
    def record_metadata(record_id: str):
        with workspace.factory() as session:
            record = session.scalar(
                select(db.Record).where(
                    db.Record.project_id == workspace.project_id, db.Record.record_id == record_id
                )
            )
            if not record:
                raise InputError("Record not found.")
            return {
                "version": record.input_version,
                "metadata": {
                    **Metadata(record_id=record_id).model_dump(mode="json"),
                    **current_snapshot(session, record)["metadata"].get(record_id, {}),
                },
            }

    @app.post("/desktop/metadata/{record_id}/{action}")
    async def change_metadata(record_id: str, action: str, request: Request):
        body = await request.json()
        with queue.lock:
            if queue.status()["active"] or queue.status()["queued"]:
                raise InputError("Finish queued processing before changing metadata.")
            current = record_metadata(record_id)
            if body.get("version") != current["version"]:
                raise InputError("This record changed. Reload its metadata before continuing.")
            if action == "preview":
                metadata = Metadata.model_validate(
                    {**current["metadata"], **body["metadata"], "record_id": record_id}
                ).model_dump(mode="json")
                token = secrets.token_hex(20)
                directory = workspace.path / "desktop-imports" / token
                directory.mkdir(parents=True)
                (directory / "upload.json").write_text(
                    json.dumps(
                        {"files": [], "metadata_preview": metadata, "base_version": current["version"]}
                    )
                )
                return {
                    **preview_plan(workspace, directory, [], [metadata]),
                    "token": token,
                    "version": current["version"],
                }
            if action != "confirm":
                raise InputError("Unknown metadata action.")
            token = body.get("token", "")
            if (
                not isinstance(token, str)
                or len(token) != 40
                or any(c not in "0123456789abcdef" for c in token)
            ):
                raise InputError("Check the metadata changes again before saving.")
            directory = workspace.path / "desktop-imports" / token
            manifest = json.loads((directory / "upload.json").read_text())
            metadata = manifest.get("metadata_preview", {})
            if metadata.get("record_id") != record_id or manifest.get("base_version") != current["version"]:
                raise InputError("The metadata preview is no longer current. Check it again.")
            return commit_plan(workspace, directory, [], [metadata])

    @app.post("/desktop/import/{action}")
    async def import_action(action: str, request: Request):
        body = await request.json()
        token = body["token"]
        if len(token) != 40 or any(c not in "0123456789abcdef" for c in token):
            raise InputError("Invalid import preview.")
        directory = workspace.path / "desktop-imports" / token
        if action == "save-draft":
            manifest = json.loads((directory / "upload.json").read_text())
            if {e["file"] for e in body["entries"]} != set(manifest["files"]) or len(body["entries"]) != len(
                manifest["files"]
            ):
                raise InputError("The import draft no longer matches its uploaded files.")
            if len(json.dumps(body).encode()) > 4 * 1024 * 1024:
                raise InputError("The import draft is too large to save.")
            target = directory / "draft.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(body))
            os.replace(temporary, target)
            pointer = workspace.path / "desktop-current-import.json"
            pointer_temporary = pointer.with_suffix(".tmp")
            pointer_temporary.write_text(json.dumps({"token": token}))
            os.replace(pointer_temporary, pointer)
            return {"saved": True}
        if action == "reserve-id":
            return {
                "source_id": reserve_id(
                    workspace, body.get("parent"), [e["source_id"] for e in body.get("entries", [])]
                )
            }
        if action == "preview":
            report = preview_plan(workspace, directory, body["entries"], body["metadata"])
            manifest_path = directory / "upload.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["checked"] = digest({"entries": body["entries"], "metadata": body["metadata"]})
            manifest["checked_versions"] = report["versions"]
            manifest["checked_revisions"] = report["revised_records"]
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(manifest))
            os.replace(temporary, manifest_path)
            return report
        if action != "confirm":
            raise InputError("Unknown import action.")
        with queue.lock:
            if queue.status()["active"] or queue.status()["queued"]:
                raise InputError("Finish queued processing before changing study sources.")
            manifest = json.loads((directory / "upload.json").read_text())
            with workspace.factory() as session:
                versions = {
                    r.record_id: r.input_version
                    for r in session.scalars(
                        select(db.Record).where(db.Record.project_id == workspace.project_id)
                    )
                }
            if (
                manifest.get("checked") != digest({"entries": body["entries"], "metadata": body["metadata"]})
                or manifest.get("checked_versions") != versions
            ):
                raise InputError(
                    "The files, metadata or study changed. Check the import again before confirming."
                )
            if manifest.get("checked_revisions") and body.get("allow_update") is not True:
                raise InputError("Confirm updating the listed existing records before importing.")
            result = commit_plan(workspace, directory, body["entries"], body["metadata"])
            pointer = workspace.path / "desktop-current-import.json"
            if pointer.exists() and json.loads(pointer.read_text()).get("token") == token:
                pointer.unlink()
            return result

    @app.get("/desktop/import-draft/{token}")
    def load_import_draft(token: str):
        if len(token) != 40 or any(c not in "0123456789abcdef" for c in token):
            raise InputError("Invalid saved import.")
        path = workspace.path / "desktop-imports" / token / "draft.json"
        if not path.is_file():
            raise InputError("No saved import was found. Add the source files again.")
        return json.loads(path.read_text())

    @app.get("/desktop/template")
    def template():
        return Response(
            "participant,date\n",
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="metadata-template.csv"'},
        )

    @app.post("/desktop/export/drafts")
    async def drafts(request: Request):
        ids = set((await request.json())["ids"])
        records = []
        with workspace.factory() as s:
            for record in s.scalars(select(db.Record).where(db.Record.project_id == workspace.project_id)):
                if record.record_id not in ids:
                    continue
                candidate = s.scalar(
                    select(db.Candidate)
                    .where(
                        db.Candidate.record_pk == record.id,
                        db.Candidate.input_version == record.input_version,
                    )
                    .order_by(db.Candidate.created_at.desc())
                )
                if candidate:
                    snapshot = current_snapshot(s, record)
                    meta = snapshot["metadata"].get(record.record_id, {})
                    records.append(
                        dict(
                            record_id=record.record_id,
                            subject_id=meta.get("subject_id"),
                            date=meta.get("date"),
                            status="manual_draft"
                            if candidate.provenance.get("method") == "manual_entry"
                            else "ai_draft",
                            input_version=record.input_version,
                            candidate_id=candidate.id,
                            content=candidate.content,
                            provenance=candidate.provenance,
                            snapshot=snapshot,
                        )
                    )
        if not records:
            raise InputError("No current AI drafts in this selection.")
        return Response(export_archive(records), media_type="application/zip")

    @app.get("/desktop/export/release/{release_id}")
    def approved(release_id: str):
        with workspace.factory() as s:
            release = s.get(db.ReviewRelease, release_id)
            if not release or release.project_id != workspace.project_id:
                raise InputError("Release not found.")
            return Response(
                export_archive(release.payload["records"], True, release.id),
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="approved-{release.id}.zip"'},
            )

    @app.post("/desktop/backup")
    def backup():
        with queue.lock:
            if queue.status()["active"]:
                raise InputError("Pause the queue and wait for current requests before backing up.")
            target = workspace.path / "backups" / ("study-" + secrets.token_hex(8) + ".zip")
            create_backup(workspace, target)
            return FileResponse(target, filename=target.name)

    return protected(app, capability)


def main():
    config = json.loads(sys.stdin.readline())
    os.environ["FOOD_RECORD_RENDERER"] = "pdfium"
    # No automatic dotenv loading in the desktop process.
    capability = config["capability"]
    lock = None
    mode = config.get("mode", "home")
    if mode == "study":
        path = Path(config["path"]).resolve()
        if config.get("restore_from"):
            restore_backup(Path(config["restore_from"]), path)
        if config.get("create"):
            Workspace.initialize(path, "study-" + secrets.token_hex(6), config["name"])
        raw = json.loads((path / "workspace.json").read_text())
        if raw.get("storage_version") != 1 or raw.get("database") != "sqlite":
            raise InputError("This app supports version 1 local SQLite studies only.")
        lock = (path / ".desktop.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InputError("This study is already open in another desktop instance.") from None
        # Back up existing storage before opening it through additive schema initialization.
        if not (path / "desktop-version.json").exists():
            from types import SimpleNamespace

            create_backup(
                SimpleNamespace(path=path, config=raw),
                path / "backups" / ("before-desktop-" + secrets.token_hex(8) + ".zip"),
            )
        marker = path / "desktop-version.json"
        if marker.exists() and json.loads(marker.read_text()).get("version") != 1:
            raise InputError("This study requires a newer desktop app.")
        workspace = Workspace(path)
        marker.write_text(json.dumps({"version": 1}))
        app = study_app(workspace, capability)
        app.state.queue.key = config.get("key")
    elif mode == "review":
        from .offline_reviewer import OfflineReviewer
        from .offline_web import create_offline_app

        reviewer = OfflineReviewer(Path(config["review_root"]), Path(config["path"]).read_bytes())
        lock = (reviewer.path / ".desktop.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app = protected(create_offline_app(reviewer, launch_token=capability), capability)
    else:
        app = protected(FastAPI(docs_url=None, redoc_url=None, openapi_url=None), capability)

        @app.get("/")
        def home():
            return FileResponse(STATIC / "desktop.html")

        @app.get("/desktop/assets/{name}")
        def asset(name: str):
            if name not in {"desktop.js", "desktop.css"}:
                return Response(status_code=404)
            return FileResponse(STATIC / name)

        @app.get("/desktop/state")
        def state():
            return {"home": True}

    @app.post("/desktop/inspect-archive")
    def archive_type(body: ArchiveRequest):
        try:
            return inspect_archive(body.path)
        except InputError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))

    def commands():
        orderly_stop = False
        for line in sys.stdin:
            command = json.loads(line)
            if command["action"] == "key" and hasattr(app.state, "queue"):
                app.state.queue.key = command.get("key")
                if not command.get("key"):
                    app.state.queue.paused = True
            if command["action"] == "stop":
                orderly_stop = True
                break
        if not orderly_stop and hasattr(app.state, "queue") and app.state.queue.status()["active"]:
            # Parent crashed/was force-quit. Do not leave an invisible service running.
            # Durable "processing" jobs become outcome-unknown on the next launch.
            os._exit(0)
        if hasattr(app.state, "queue"):
            app.state.queue.close()
        server.should_exit = True

    threading.Thread(target=commands, daemon=True).start()
    print(json.dumps({"port": sock.getsockname()[1], "mode": mode}), flush=True)
    try:
        server.run(sockets=[sock])
    finally:
        if hasattr(app.state, "queue"):
            app.state.queue.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    main()
