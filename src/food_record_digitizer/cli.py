"""English-only P0/P1 command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select

from .db import Batch, Candidate, ExtractionRun, Record
from .extraction import extract_record, recover_run
from .ingest import (
    InputError,
    current_duplicate_content,
    current_snapshot,
    ingest_batch,
    ingest_one,
    inspect_inputs,
    read_metadata,
    split_id,
)
from .provider import OpenAIProvider, ProviderError, execution_policy, load_env
from .schemas import RecordContent
from .workspace import Workspace


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def parser():
    root = argparse.ArgumentParser(
        description="Digitize handwritten food records into traceable review drafts."
    )
    root.add_argument("--version", action="version", version="food-record-digitizer 0.4.3")
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a persistent project workspace")
    init.add_argument("--project", required=True)
    init.add_argument("--name", default="Food record study")
    audit = sub.add_parser("inspect", help="Read-only source and metadata inventory")
    audit.add_argument("--input", type=Path, required=True)
    audit.add_argument("--metadata", type=Path, required=True)
    audit.add_argument("--output", type=Path)
    audit.add_argument("--pages", action="store_true", help="Inspect page counts and text-layer presence")
    for name in ["ingest", "ingest-one", "attach"]:
        cmd = sub.add_parser(name)
        if name == "ingest":
            cmd.add_argument("--input", type=Path, required=True)
            cmd.add_argument("--metadata", type=Path, required=True)
            cmd.add_argument("--metadata-mode", choices=["delta", "snapshot"], default="delta")
        elif name == "ingest-one":
            cmd.add_argument("--pdf", type=Path, required=True)
            cmd.add_argument("--metadata", type=Path, required=True)
            cmd.add_argument("--attachment", type=Path, action="append", default=[])
        else:
            cmd.add_argument("--record-id", required=True)
            cmd.add_argument("--attachment", type=Path, action="append", required=True)
    extraction = sub.add_parser("extract", help="Generate AI drafts; this command makes billable API calls")
    scope = extraction.add_mutually_exclusive_group(required=True)
    scope.add_argument("--record-id", action="append")
    scope.add_argument("--batch-id")
    scope.add_argument("--pending-only", action="store_true")
    extraction.add_argument("--config", type=Path, help="TOML extraction settings")
    extraction.add_argument("--model")
    extraction.add_argument("--limit", type=int, default=20)
    extraction.add_argument("--max-pages", type=int, default=20)
    extraction.add_argument("--dpi", type=int, default=150)
    extraction.add_argument("--max-output-tokens", type=int, default=32000)
    extraction.add_argument(
        "--max-output-ceiling",
        type=int,
        default=64000,
        help="Fixed final budget for a confirmed truncation retry; at most 64000",
    )
    extraction.add_argument(
        "--request-timeout",
        type=int,
        default=1200,
        help="API timeout per attempt in seconds (1–1800)",
    )
    extraction.add_argument(
        "--image-max-edge",
        type=int,
        default=1200,
        help="Maximum transport image edge in pixels; 0 disables compression",
    )
    extraction.add_argument("--jpeg-quality", type=int, default=70, help="Transport JPEG quality, 60–95")
    extraction.add_argument("--force", action="store_true", help="Explicitly re-extract unchanged inputs")
    extraction.add_argument("--dry-run", action="store_true", help="List scope without contacting a provider")
    recovery = sub.add_parser("recover-run")
    recovery.add_argument("--run-id", required=True)
    recovery.add_argument("--worker-stopped", action="store_true", required=True)
    report = sub.add_parser("report")
    report.add_argument("--output", type=Path)
    export = sub.add_parser("export-draft", help="Export AI candidates; never research-approved data")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--record-id")
    review = sub.add_parser("review", help="Run the authenticated English review workspace (no AI calls)")
    review.add_argument("--host", default="127.0.0.1")
    review.add_argument("--port", type=int, default=8765)
    review.add_argument("--public-url", help="HTTPS origin for a reverse-proxy team deployment")
    owner = sub.add_parser("review-init-owner", help="Create a one-time invitation for the first owner")
    owner.add_argument("--email", required=True)
    owner.add_argument("--name", required=True)
    owner.add_argument("--url", default="http://127.0.0.1:8765")
    backup = sub.add_parser("backup", help="Create a verified offline workspace backup")
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--server-stopped", action="store_true", required=True)
    restore = sub.add_parser("restore", help="Restore a backup into a new directory")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    offline_review = sub.add_parser("offline-review", help="Open a package for local offline review")
    offline_review.add_argument("--input", type=Path)
    offline_review.add_argument("--no-browser", action="store_true")
    offline_export = sub.add_parser(
        "offline-export", help="Export complete daily assignments for offline review"
    )
    offline_export.add_argument("--owner-email", required=True)
    offline_export.add_argument("--reviewer-email", required=True)
    offline_export.add_argument("--reviewer-name", required=True)
    offline_export.add_argument("--day-id", action="append", required=True)
    offline_export.add_argument(
        "--units-per-package", type=int, help="Days per package; defaults to the last export setting or 10"
    )
    offline_export.add_argument("--dry-run", action="store_true")
    offline_return = sub.add_parser(
        "offline-import-result", help="Validate and import a returned offline result"
    )
    offline_return.add_argument("--owner-email", required=True)
    offline_return.add_argument("--input", type=Path, required=True)
    schema = sub.add_parser("schema")
    schema.add_argument("--output", type=Path)
    for name, cmd in sub.choices.items():
        if name not in {"inspect", "schema"}:
            cmd.add_argument("--workspace", type=Path, default=Path("work"))
        cmd.add_argument("--env-file", type=Path, default=Path(".env"))
    return root


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exports are snapshots; do not silently overwrite a previously used file.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def pdf_paths(directory: Path):
    if not directory.is_dir():
        raise InputError("Input directory does not exist")
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")


def inventory(paths, rows, pages=False):
    result = inspect_inputs(paths, rows)
    import hashlib
    from collections import defaultdict

    hashes = defaultdict(list)
    for path in paths:
        hashes[hashlib.sha256(path.read_bytes()).hexdigest()].append(path.stem)
    result["duplicate_content"] = [ids for ids in hashes.values() if len(ids) > 1]
    from collections import Counter

    from .validation import flag_codes

    result["flags"] = dict(Counter(r.flag for r in rows if r.flag))
    result["flag_rules"] = {flag: flag_codes(flag) for flag in result["flags"]}
    if pages:
        from pypdf import PdfReader

        details = []
        for path in paths:
            try:
                reader = PdfReader(path)
                page_details = [
                    {
                        "number": i + 1,
                        "has_text_layer": bool(page.extract_text().strip()),
                        "width_pt": float(page.mediabox.width),
                        "height_pt": float(page.mediabox.height),
                        "rotation": page.rotation,
                    }
                    for i, page in enumerate(reader.pages)
                ]
                details.append(
                    {"source_record_id": path.stem, "page_count": len(page_details), "pages": page_details}
                )
            except Exception:
                details.append({"source_record_id": path.stem, "error": "unreadable_pdf"})
        result["documents"] = details
        result["total_pages"] = sum(d.get("page_count", 0) for d in details)
    return result


def report_data(workspace):
    with workspace.factory() as session:
        records = list(
            session.scalars(
                select(Record).where(Record.project_id == workspace.project_id).order_by(Record.record_id)
            )
        )
        runs = list(
            session.scalars(
                select(ExtractionRun)
                .join(Record, ExtractionRun.record_pk == Record.id)
                .where(Record.project_id == workspace.project_id)
                .order_by(ExtractionRun.created_at)
            )
        )
        return {
            "project_id": workspace.project_id,
            "duplicate_content": current_duplicate_content(session, workspace.project_id),
            "records": [
                {
                    "record_id": r.record_id,
                    "input_version": r.input_version,
                    "state": r.state,
                    "active_run_id": r.active_run_id,
                }
                for r in records
            ],
            "runs": [
                {
                    "run_id": r.id,
                    "record_pk": r.record_pk,
                    "input_version": r.input_version,
                    "status": r.status,
                    "error_code": r.error_code,
                    "error_detail": r.configuration.get("failure_diagnostic", {}),
                    "usage": r.usage,
                    "elapsed_seconds": r.elapsed_seconds,
                    "created_at": r.created_at,
                }
                for r in runs
            ],
        }


def execute(args):
    load_env(args.env_file)
    if args.command == "extract" and args.config:
        import tomllib

        settings = tomllib.loads(args.config.read_text()).get("extraction", {})
        allowed = {
            "model",
            "limit",
            "max_pages",
            "dpi",
            "max_output_tokens",
            "max_output_ceiling",
            "request_timeout",
            "image_max_edge",
            "jpeg_quality",
        }
        if set(settings) - allowed:
            raise InputError("Unknown extraction config key")
        for key, value in settings.items():
            # Explicit CLI flags override the config file.
            flag = "--" + key.replace("_", "-")
            if not any(a == flag or a.startswith(flag + "=") for a in sys.argv[1:]):
                setattr(args, key, value)
    if args.command == "schema":
        result = RecordContent.model_json_schema()
        if args.output:
            write_json(args.output, result)
        else:
            emit(result)
        return 0
    if args.command == "inspect":
        result = inventory(pdf_paths(args.input), read_metadata(args.metadata), args.pages)
        if args.output:
            write_json(args.output, result)
            emit({k: v for k, v in result.items() if k not in {"groups", "documents", "flags", "flag_rules"}})
        else:
            emit(result)
        return 0
    if args.command == "init":
        workspace = Workspace.initialize(args.workspace, args.project, args.name)
        emit({"project_id": workspace.project_id, "workspace": str(workspace.path)})
        return 0
    if args.command == "restore":
        from .backup import restore_backup

        emit(restore_backup(args.input, args.output))
        return 0
    if args.command == "offline-review":
        from .offline_web import launch

        launch(args.input, args.workspace, not args.no_browser)
        return
    workspace = Workspace(args.workspace)
    if args.command in {"offline-export", "offline-import-result"}:
        from .db import ReviewUser
        from .offline import OfflineManager
        from .review import ReviewError

        manager = OfflineManager(workspace)
        with workspace.factory() as session:
            owner = session.scalar(
                select(ReviewUser).where(
                    ReviewUser.project_id == workspace.project_id,
                    ReviewUser.email == args.owner_email.strip().lower(),
                    ReviewUser.role == "owner",
                    ReviewUser.active == 1,
                )
            )
            if not owner:
                raise ReviewError("An active project owner is required.")
            actor_id = owner.id
        if args.command == "offline-export":
            if args.dry_run:
                with workspace.factory() as session:
                    reviewer = session.scalar(
                        select(ReviewUser).where(
                            ReviewUser.project_id == workspace.project_id,
                            ReviewUser.email == args.reviewer_email.strip().lower(),
                            ReviewUser.active == 1,
                        )
                    )
                    if not reviewer:
                        raise ReviewError("Preview requires an existing reviewer; no account was created.")
                    reviewer_id = reviewer.id
            else:
                reviewer_id = manager.reviewer(actor_id, args.reviewer_name, args.reviewer_email)
            emit(manager.export(actor_id, reviewer_id, args.day_id, args.units_per_package, args.dry_run))
        else:
            emit(manager.import_result(actor_id, args.input.read_bytes()))
        return
    if args.command == "backup":
        from .backup import create_backup

        emit(create_backup(workspace, args.output))
        return 0
    if args.command == "review-init-owner":
        from .review import ReviewService

        result = ReviewService(workspace).bootstrap_invite(args.email, args.name)
        emit(
            {
                "email": result["email"],
                "invitation_url": args.url.rstrip("/") + "/#invite=" + result["token"],
                "expires_in_hours": 24,
            }
        )
        return 0
    if args.command == "review":
        from urllib.parse import urlparse

        import uvicorn

        from .review_web import create_app

        if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.public_url:
            raise InputError(
                "External binding requires --public-url https://your-review-host and an HTTPS proxy."
            )
        public = urlparse(args.public_url) if args.public_url else None
        if public and (public.scheme != "https" or not public.hostname or public.path not in {"", "/"}):
            raise InputError("Public URL must be an HTTPS origin.")
        allowed = ["127.0.0.1", "localhost", "[::1]"] + ([public.hostname] if public else [])
        app = create_app(workspace, secure_cookies=bool(public), allowed_hosts=allowed)
        print("Review workspace: " + (args.public_url or f"http://{args.host}:{args.port}"), flush=True)
        uvicorn.run(app, host=args.host, port=args.port, access_log=False, proxy_headers=False)
        return 0
    if args.command == "ingest":
        result = ingest_batch(
            workspace.factory,
            workspace.store,
            workspace.project_id,
            pdf_paths(args.input),
            read_metadata(args.metadata),
        )
        emit(
            {
                "batch_id": result["batch_id"],
                "changed": result["changed"],
                "unchanged": result["unchanged"],
                "missing_source": result["missing_source"],
                "missing_metadata": result["missing_metadata"],
                "duplicate_content": result["duplicate_content"],
            }
        )
    elif args.command == "ingest-one":
        rows = read_metadata(args.metadata)
        if len(rows) != 1:
            raise InputError("Single-record input requires exactly one metadata row")
        emit(
            ingest_one(
                workspace.factory, workspace.store, workspace.project_id, args.pdf, rows[0], args.attachment
            )
        )
    elif args.command == "attach":
        if split_id(args.record_id)[1] is not None:
            raise InputError("Target must be a main record ID")
        if any(split_id(p.stem)[0] != args.record_id or split_id(p.stem)[1] is None for p in args.attachment):
            raise InputError("Attachment IDs must belong to the target record")
        emit(ingest_batch(workspace.factory, workspace.store, workspace.project_id, args.attachment, []))
    elif args.command == "recover-run":
        recover_run(workspace, args.run_id)
        emit({"run_id": args.run_id, "status": "abandoned"})
    elif args.command == "report":
        result = report_data(workspace)
        if args.output:
            write_json(args.output, result)
        else:
            emit(result)
    elif args.command == "export-draft":
        with workspace.factory() as session:
            records = workspace.records()
            if args.record_id:
                records = [r for r in records if r.record_id == args.record_id]
                if not records:
                    raise InputError("Record not found")
            output = []
            for record in records:
                candidate = session.scalar(
                    select(Candidate)
                    .where(Candidate.record_pk == record.id, Candidate.input_version == record.input_version)
                    .order_by(Candidate.created_at.desc())
                )
                if candidate:
                    output.append(
                        {
                            "status": "pending_review",
                            "input_version": record.input_version,
                            "candidate_id": candidate.id,
                            "provenance": candidate.provenance,
                            "content": candidate.content,
                            "sources": current_snapshot(session, record),
                        }
                    )
            write_json(
                args.output,
                {
                    "draft": True,
                    "research_approved": False,
                    "records": output,
                    "processing_report": report_data(workspace),
                },
            )
            emit({"output": str(args.output), "draft_records": len(output), "research_approved": False})
    elif args.command == "extract":
        if (
            args.limit < 1
            or args.max_pages < 1
            or not 72 <= args.dpi <= 300
            or args.max_output_tokens < 1
            or not 1 <= args.request_timeout <= 1800
        ):
            raise InputError("Invalid extraction limits")
        records = workspace.records()
        if args.record_id:
            wanted = set(args.record_id)
            if wanted - {r.record_id for r in records}:
                raise InputError("One or more requested records were not found")
            records = [r for r in records if r.record_id in wanted]
        elif args.batch_id:
            with workspace.factory() as session:
                batch = session.get(Batch, args.batch_id)
                if batch is None or batch.project_id != workspace.project_id:
                    raise InputError("Batch not found in this project")
                wanted = set(batch.manifest["groups"])
            records = [r for r in records if r.record_id in wanted]
        else:
            records = [r for r in records if r.state in {"pending_extraction", "failed"}]
        records = records[: args.limit]
        policy = execution_policy(args.max_output_tokens, args.max_output_ceiling, args.request_timeout)
        if args.dry_run:
            emit(
                {
                    "records": [{"record_id": r.record_id, "state": r.state} for r in records],
                    "api_calls": 0,
                    "execution_policy": policy,
                }
            )
            return 0
        provider = OpenAIProvider(
            args.model or os.environ.get("OPENAI_MODEL", "gpt-6.1-sol"),
            args.max_output_tokens,
            timeout=args.request_timeout,
            image_max_edge=args.image_max_edge,
            jpeg_quality=args.jpeg_quality,
            max_output_ceiling=args.max_output_ceiling,
        )
        failed = False
        try:
            for record in records:
                result = extract_record(
                    workspace, record.record_id, provider, args.dpi, args.max_pages, args.force
                )
                emit(result)
                failed |= result["status"] in {"failed", "stale", "busy"}
                # Authentication/configuration problems affect every record: stop the batch.
                if result.get("error") in {"api_http_401", "api_http_403", "api_http_404", "api_http_429"}:
                    break
        finally:
            provider.close()
        return 1 if failed else 0
    return 0


def main():
    try:
        return execute(parser().parse_args())
    except (InputError, FileNotFoundError, FileExistsError, ProviderError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            # Avoid printing validation input values (which can contain research data).
            print(
                "Error: Input does not match the schema. Check field types and required values.",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: Operation failed ({type(exc).__name__}). No credentials were logged.",
                file=sys.stderr,
            )
        return 2


if __name__ == "__main__":
    sys.exit(main())
