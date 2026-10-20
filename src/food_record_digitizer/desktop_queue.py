"""Persisted local queue. Opening a study never dispatches an API request."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from .db import Candidate, ExtractionRun, Record, now
from .extraction import extract_record, recover_run
from .ingest import InputError, current_snapshot
from .provider import OpenAIProvider

DEFAULTS = dict(
    model="gpt-6.1-sol",
    concurrency=3,
    max_output_tokens=32000,
    max_output_ceiling=64000,
    timeout=1200,
    image_max_edge=1200,
    jpeg_quality=70,
    base_url="https://api.openai.com/v1",
    dpi=150,
    max_pages=20,
)
UNKNOWN_ERRORS = {
    "request_timeout_outcome_unknown",
    "connection_failed",
    "invalid_api_response",
    "request_failed",
    "request_timeout",
    "network_error",
    "extraction_failed",
    "response_persistence_failed",
}


class DesktopQueue:
    def __init__(self, workspace, provider_factory=None):
        self.workspace = workspace
        self.path = workspace.path / "desktop-jobs.sqlite"
        self.lock = threading.RLock()
        self.key = None
        self.paused = True
        self.closed = False
        self.provider_factory = provider_factory or self.provider
        with self.connection() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS jobs (record_id TEXT PRIMARY KEY, version INTEGER, state TEXT, config TEXT, started TEXT, finished TEXT, seconds REAL, error TEXT)"
            )
            if "run_id" not in {row[1] for row in c.execute("PRAGMA table_info(jobs)")}:
                c.execute("ALTER TABLE jobs ADD COLUMN run_id TEXT")
            c.execute(
                "UPDATE jobs SET state='needs_attention', error='Interrupted; remote outcome unknown. Explicit retry may incur another charge.' WHERE state='processing'"
            )
        self.path.chmod(0o600)
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.active = set()
        self.thread = threading.Thread(target=self.dispatch, daemon=True)
        self.thread.start()

    def connection(self):
        return sqlite3.connect(self.path, timeout=30)

    def provider(self, config):
        return OpenAIProvider(
            api_key=self.key,
            **{k: v for k, v in config.items() if k not in {"concurrency", "dpi", "max_pages"}},
        )

    def records(self):
        with self.connection() as c:
            c.row_factory = sqlite3.Row
            jobs = {r["record_id"]: dict(r) for r in c.execute("SELECT * FROM jobs")}
        rows = []
        with self.workspace.factory() as s:
            for r in s.scalars(
                select(Record)
                .where(Record.project_id == self.workspace.project_id)
                .order_by(Record.record_id)
            ):
                snap = current_snapshot(s, r)
                candidate = s.scalar(
                    select(Candidate)
                    .where(Candidate.record_pk == r.id, Candidate.input_version == r.input_version)
                    .order_by(Candidate.created_at.desc())
                )
                run = s.scalar(
                    select(ExtractionRun)
                    .where(ExtractionRun.record_pk == r.id, ExtractionRun.input_version == r.input_version)
                    .order_by(ExtractionRun.created_at.desc())
                )
                job = jobs.get(r.record_id, {})
                if job.get("version") != r.input_version:
                    job = {}
                state = (
                    "processing"
                    if r.record_id in self.active
                    else "draft_ready"
                    if candidate
                    else "needs_attention"
                    if r.active_run_id
                    else job.get("state", r.state)
                )
                rows.append(
                    dict(
                        record_id=r.record_id,
                        version=r.input_version,
                        state=state,
                        metadata=snap["metadata"].get(r.record_id, {}),
                        pages=sum(d["page_count"] for d in snap["documents"].values()),
                        attachments=sum(d["role"] == "attachment" for d in snap["documents"].values()),
                        has_candidate=bool(candidate),
                        candidate_id=candidate.id if candidate else None,
                        run_id=job.get("run_id") or (run.id if run else None),
                        seconds=job.get("seconds") or (run.elapsed_seconds if run else None),
                        error=job.get("error") or (run.error_code if run else None),
                    )
                )
        return rows

    def preview(self, ids):
        ids = set(ids)
        rows = [r for r in self.records() if r["record_id"] in ids]
        if len(rows) != len(ids):
            raise InputError("Some records no longer exist. Refresh the list.")
        reusable = [r for r in rows if r["has_candidate"]]
        eligible = [
            r
            for r in rows
            if not r["has_candidate"]
            and r["state"]
            not in {"missing_source", "blocked", "exclusion_pending", "excluded", "queued", "processing"}
        ]
        return dict(
            records=rows,
            reuse=len(reusable),
            requests=len(eligible),
            pages=sum(r["pages"] for r in eligible),
            attachments=sum(r["attachments"] for r in eligible),
            unknown=[r["record_id"] for r in eligible if r["state"] == "needs_attention"],
            versions={r["record_id"]: r["version"] for r in rows},
        )

    def enqueue(self, ids, config, versions, retry_unknown=False):
        with self.lock:
            preview = self.preview(ids)
            if versions != preview["versions"]:
                raise InputError("Sources changed. Preview the processing scope again.")
            if preview["unknown"] and not retry_unknown:
                raise InputError("A previous request may have been charged. Confirm retry explicitly.")
            if preview["requests"] and not self.key and self.provider_factory == self.provider:
                raise InputError("Add your API key in Settings first.")
            for row in preview["records"]:
                if (
                    row["record_id"] in self.active
                    or row["has_candidate"]
                    or row["state"]
                    in {
                        "missing_source",
                        "blocked",
                        "exclusion_pending",
                        "excluded",
                        "queued",
                        "processing",
                    }
                ):
                    continue
                if row["state"] == "needs_attention":
                    with self.workspace.factory() as s:
                        record = s.scalar(
                            select(Record).where(
                                Record.project_id == self.workspace.project_id,
                                Record.record_id == row["record_id"],
                            )
                        )
                        active = record.active_run_id
                    if active:
                        recover_run(self.workspace, active)
                with self.connection() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO jobs (record_id,version,state,config) VALUES (?,?, 'queued', ?)",
                        (row["record_id"], row["version"], json.dumps(config)),
                    )
            self.paused = False
            return preview

    def dispatch(self):
        while not self.closed:
            with self.lock:
                if not self.paused:
                    with self.connection() as c:
                        job = c.execute(
                            "SELECT record_id,version,config FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1"
                        ).fetchone()
                        if job and len(self.active) < json.loads(job[2])["concurrency"]:
                            c.execute(
                                "UPDATE jobs SET state='processing', started=? WHERE record_id=? AND state='queued'",
                                (now(), job[0]),
                            )
                            self.active.add(job[0])
                            self.executor.submit(self.work, *job)
            time.sleep(0.1)

    def work(self, rid, version, encoded):
        started = time.monotonic()
        state, error = "failed", None
        provider = None
        try:
            row = next(r for r in self.records() if r["record_id"] == rid)
            if row["version"] != version:
                raise InputError("Sources changed before processing. Select this record again.")
            if row["has_candidate"]:
                state = "succeeded"
            else:
                config = json.loads(encoded)
                provider = self.provider_factory(config)

                def remember_run(run_id):
                    # Commit before the provider can send a request, including on crash.
                    with self.connection() as c:
                        c.execute(
                            "UPDATE jobs SET run_id=? WHERE record_id=? AND version=?",
                            (run_id, rid, version),
                        )

                result = extract_record(
                    self.workspace,
                    rid,
                    provider,
                    dpi=config["dpi"],
                    max_pages=config["max_pages"],
                    on_claim=remember_run,
                )
                error = result.get("error")
                state = "succeeded" if result["status"] in {"succeeded", "cached"} else "failed"
                if (
                    error in UNKNOWN_ERRORS
                    or (error or "").startswith("api_http_5")
                    or result["status"] == "busy"
                ):
                    state = "needs_attention"
        except InputError as exc:
            error = str(exc)
        except Exception:
            state, error = "needs_attention", "Processing interrupted; remote outcome may be unknown."
        finally:
            if provider:
                try:
                    provider.close()
                except Exception:
                    # Transport cleanup must not strand a completed job or lose its result.
                    pass
            with self.lock, self.connection() as c:
                c.execute(
                    "UPDATE jobs SET state=?,finished=?,seconds=?,error=? WHERE record_id=?",
                    (state, now(), time.monotonic() - started, error, rid),
                )
                self.active.discard(rid)

    def status(self):
        with self.lock, self.connection() as c:
            return dict(
                paused=self.paused,
                active=len(self.active),
                queued=c.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0],
            )

    def close(self):
        self.paused = True
        self.closed = True
        self.thread.join(timeout=2)
        self.executor.shutdown(wait=True)
