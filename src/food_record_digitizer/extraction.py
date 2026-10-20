"""Durable, bounded extraction. Only explicit commands can make model calls."""

from __future__ import annotations

import json
import time

from sqlalchemy import select, update

from .db import Audit, Candidate, ExtractionRun, Record, now, uid
from .imaging import RENDER_VERSION, render_pages
from .ingest import InputError, current_snapshot, digest
from .provider import PROMPT, PROMPT_VERSION, ProviderError, strict_schema
from .schemas import RecordContent
from .validation import validate_content


def save_response(workspace, run_id, raw):
    directory = workspace.path / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    response_path = directory / "response.json"
    with response_path.open("x", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=2)
    response_path.chmod(0o600)


def extract_record(workspace, record_id: str, provider, dpi=150, max_pages=20, force=False, on_claim=None):
    config = {
        "model": provider.model,
        "prompt_version": PROMPT_VERSION,
        "prompt_hash": digest(PROMPT),
        "schema_hash": digest(RecordContent.model_json_schema()),
        "dpi": dpi,
        "render_version": RENDER_VERSION,
        "max_pages": max_pages,
        "max_output_tokens": provider.max_output_tokens,
        "endpoint": provider.base_url,
    }
    if hasattr(provider, "image_max_edge"):
        config["image_preprocessing"] = {
            "version": "1",
            "max_edge": provider.image_max_edge,
            "jpeg_quality": provider.jpeg_quality,
        }
    if hasattr(provider, "execution_policy"):
        config["execution_policy"] = provider.execution_policy
    run_id = uid()
    with workspace.factory.begin() as session:
        record = session.scalar(
            select(Record)
            .where(Record.project_id == workspace.project_id, Record.record_id == record_id)
            .with_for_update()
        )
        if record is None:
            raise InputError("Record not found")
        snapshot = current_snapshot(session, record)
        if record.state in {"missing_source", "blocked", "exclusion_pending"}:
            return {"record_id": record_id, "status": "skipped", "reason": record.state}
        key = digest({"input": record.fingerprint, "config": config})
        old = session.scalar(
            select(ExtractionRun)
            .where(
                ExtractionRun.record_pk == record.id,
                ExtractionRun.input_version == record.input_version,
                ExtractionRun.cache_key == key,
                ExtractionRun.status == "succeeded",
            )
            .order_by(ExtractionRun.created_at.desc())
        )
        if old and not force:
            return {"record_id": record_id, "status": "cached", "candidate_id": old.candidate_id}
        if record.active_run_id:
            return {"record_id": record_id, "status": "busy", "run_id": record.active_run_id}
        record_pk, input_version = record.id, record.input_version
        claimed = session.execute(
            update(Record)
            .where(
                Record.id == record.id, Record.active_run_id.is_(None), Record.input_version == input_version
            )
            .values(active_run_id=run_id)
        )
        if claimed.rowcount != 1:
            raise InputError("Record was changed or claimed by another worker")
        session.add(
            ExtractionRun(
                id=run_id,
                record_pk=record_pk,
                input_version=input_version,
                cache_key=key,
                configuration=config,
            )
        )

    def persist_attempt(attempt, response):
        if response is not None:
            directory = workspace.path / "runs" / run_id / "attempts"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{attempt['number']}.json"
            with path.open("x", encoding="utf-8") as handle:
                json.dump(response, handle, ensure_ascii=False)
            path.chmod(0o600)
        with workspace.factory.begin() as session:
            run = session.get(ExtractionRun, run_id)
            run.configuration = {
                **run.configuration,
                "attempts": list(provider.attempts),
                "image_stats": getattr(provider, "last_image_stats", []),
            }

    if hasattr(provider, "attempt_callback"):
        provider.attempt_callback = persist_attempt
        provider.attempts = []
        provider.last_image_stats = []
    started = time.monotonic()
    raw, usage, error = None, {}, None
    diagnostic = {}
    try:
        if on_claim is not None:
            on_claim(run_id)
        import platform
        from importlib.metadata import version

        directory = workspace.path / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / "manifest.json"
        with manifest.open("x", encoding="utf-8") as handle:
            json.dump(
                {
                    "configuration": config,
                    "input_version": input_version,
                    "input_snapshot": snapshot,
                    "prompt": PROMPT,
                    "response_schema": strict_schema(),
                    "request_constants": {"store": False, "image_detail": "high", "strict": True},
                    "runtime": {
                        "python": platform.python_version(),
                        **{
                            name: version(name)
                            for name in ["food-record-digitizer", "pydantic", "pillow", "httpx"]
                        },
                    },
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        manifest.chmod(0o600)
        if sum(d["page_count"] for d in snapshot["documents"].values()) > max_pages:
            raise ProviderError("record_page_limit_exceeded")
        pages = render_pages(workspace.store, snapshot, workspace.path / "rendered", dpi)
        result = provider.extract(record_id, pages)
        raw, usage = result["raw"], result["usage"]
        image_stats = result.get("image_stats", [])
        save_response(workspace, run_id, raw)
        content = RecordContent.model_validate(result["content"])
        if content.record_id != record_id:
            raise ProviderError("record_id_mismatch")
        # Human-only fields can never be set by the model.
        for page in content.pages:
            page.reviewed = False
        for issue in content.issues:
            issue.resolution = None
        content.issues.extend(validate_content(content, snapshot))
        with workspace.factory.begin() as session:
            record = session.scalar(select(Record).where(Record.id == record_pk).with_for_update())
            run = session.get(ExtractionRun, run_id)
            run.configuration = {**run.configuration, "image_stats": image_stats}
            if record.input_version != input_version or record.active_run_id != run_id:
                run.status = "stale"
                error = "input_changed_during_extraction"
            else:
                candidate = Candidate(
                    record_pk=record_pk,
                    input_version=input_version,
                    content=content.model_dump(mode="json"),
                    provenance={
                        **config,
                        "image_stats": image_stats,
                        "attempts": getattr(provider, "attempts", []),
                        "run_id": run_id,
                        "response_id": result.get("response_id"),
                        "response_model": raw.get("model"),
                        "input_fingerprint": record.fingerprint,
                    },
                )
                session.add(candidate)
                session.flush()
                run.candidate_id = candidate.id
                run.status = "succeeded"
                record.state = "pending_review"
                record.approved_id = None
                from .db import ReviewTask

                for task in session.scalars(select(ReviewTask).where(ReviewTask.record_pk == record.id)):
                    if task.status not in {"cancelled", "returned", "stale"}:
                        task.status = "stale"
    except Exception as exc:
        if isinstance(exc, ProviderError):
            diagnostic = exc.diagnostic
        error = (
            exc.code
            if isinstance(exc, ProviderError)
            else (
                "schema_validation_failed"
                if exc.__class__.__name__ == "ValidationError"
                else "render_failed"
                if isinstance(exc, InputError)
                else "extraction_failed"
            )
        )
        if isinstance(exc, ProviderError) and exc.response:
            raw = exc.response
            usage = raw.get("usage", {})
        if raw is not None and not (workspace.path / "runs" / run_id / "response.json").exists():
            try:
                save_response(workspace, run_id, raw)
            except OSError:
                error = "response_persistence_failed"
    finally:
        with workspace.factory.begin() as session:
            record = session.scalar(select(Record).where(Record.id == record_pk).with_for_update())
            run = session.get(ExtractionRun, run_id)
            if run.status == "running":
                run.status = "failed"
            attempts = getattr(provider, "attempts", [])
            if attempts:
                usage = attempts[-1].get("aggregate_usage", usage)
                run.configuration = {**run.configuration, "attempts": attempts}
            run.error_code, run.usage = error, usage
            if hasattr(provider, "last_image_stats"):
                run.configuration = {**run.configuration, "image_stats": provider.last_image_stats}
            if diagnostic:
                run.configuration = {**run.configuration, "failure_diagnostic": diagnostic}
            run.elapsed_seconds = f"{time.monotonic() - started:.3f}"
            run.finished_at = now()
            if record.active_run_id == run_id:
                record.active_run_id = None
                if (
                    run.status == "failed"
                    and record.input_version == input_version
                    and record.state != "pending_review"
                ):
                    record.state = "failed"
            session.add(
                Audit(
                    project_id=workspace.project_id,
                    actor_id="extractor",
                    action="extraction_finished",
                    target_id=record_pk,
                    detail={"run_id": run_id, "status": run.status, "error": error},
                )
            )
            result_summary = {
                "record_id": record_id,
                "run_id": run_id,
                "status": run.status,
                "candidate_id": run.candidate_id,
                "error": error,
                "error_detail": diagnostic,
                "usage": usage,
            }
    if hasattr(provider, "attempt_callback"):
        provider.attempt_callback = None
    return result_summary


def recover_run(workspace, run_id: str):
    """Explicit operator recovery after verifying the worker has stopped."""
    with workspace.factory.begin() as session:
        run = session.get(ExtractionRun, run_id)
        if run is None:
            raise InputError("Run not found")
        record = session.scalar(select(Record).where(Record.id == run.record_pk).with_for_update())
        if record.project_id != workspace.project_id or run.status != "running":
            raise InputError("Run is not a running task in this project")
        run.status, run.error_code, run.finished_at = "abandoned", "operator_recovery", now()
        if record.active_run_id == run.id:
            record.active_run_id = None
        session.add(
            Audit(
                project_id=workspace.project_id,
                actor_id="local-operator",
                action="run_recovered",
                target_id=record.id,
                detail={"run_id": run_id},
            )
        )
