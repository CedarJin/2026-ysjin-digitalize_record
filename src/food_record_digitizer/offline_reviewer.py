"""Persistent local-only review workspace for a single issued package."""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from .day_review import DayReviewService
from .offline import retain
from .offline_archive import canonical, pack, sha256, unpack
from .review import ReviewError
from .schemas import RecordContent
from .validation import APPROVAL_BLOCKING_CODES


class OfflineReviewer:
    def __init__(self, root: Path, data: bytes):
        manifest, blobs = unpack(data, "task")
        self.validate_manifest(manifest, blobs)
        self.manifest = manifest
        self.archive_hash = sha256(data)
        # Untrusted IDs never become paths. Same exact package always resumes the same workspace.
        self.path = Path(root) / self.archive_hash
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        retain(self.path / "original", data)
        self.units = {u["id"]: u for u in manifest["units"]}
        self.tasks = {u["task_id"]: u for u in manifest["units"]}
        for name, blob in blobs.items():
            target = self.path / name
            target.parent.mkdir(exist_ok=True, mode=0o700)
            if target.exists() and sha256(target.read_bytes()) != sha256(blob):
                raise ReviewError("Local source checksum mismatch.")
            if not target.exists():
                with target.open("xb") as out:
                    out.write(blob)
                target.chmod(0o600)
        with self.connection() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS drafts (task TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS edits (task TEXT NOT NULL, revision INTEGER NOT NULL, content TEXT NOT NULL, PRIMARY KEY(task,revision))"
            )
            for task_id, unit in self.tasks.items():
                c.execute(
                    "INSERT OR IGNORE INTO drafts VALUES (?,1,?,?)",
                    (task_id, "assigned", canonical(unit["content"]).decode()),
                )
        (self.path / "review.sqlite").chmod(0o600)

    @staticmethod
    def validate_manifest(m, blobs):
        try:
            for key in ("package_id", "project_id"):
                if not isinstance(m[key], str) or not m[key]:
                    raise ValueError(key)
            if not isinstance(m["reviewer"], dict) or any(
                not isinstance(m["reviewer"][k], str) or not m["reviewer"][k] for k in ("id", "name", "email")
            ):
                raise ValueError("reviewer")
            if not isinstance(m["units"], list) or not m["units"]:
                raise ValueError("units")
            if len({u["id"] for u in m["units"]}) != len(m["units"]) or len(
                {u["task_id"] for u in m["units"]}
            ) != len(m["units"]):
                raise ValueError("duplicate units")
            used = set()
            for u in m["units"]:
                if any(
                    not isinstance(u[k], str) or not u[k]
                    for k in ("id", "task_id", "subject_id", "date", "fingerprint")
                ):
                    raise ValueError("identity")
                if type(u["input_version"]) is not int or u["input_version"] < 1:
                    raise ValueError("version")
                content = RecordContent.model_validate(u["content"])
                if content.record_id != u["id"] or sha256(canonical(u["content"])) != u["baseline_sha256"]:
                    raise ValueError("baseline")
                if not u["snapshot"]["documents"] or u["snapshot"].get("pending_placements"):
                    raise ValueError("sources")
                for doc in u["snapshot"]["documents"].values():
                    if (
                        type(doc["page_count"]) is not int
                        or not 1 <= doc["page_count"] <= 1000
                        or doc.get("error")
                    ):
                        raise ValueError("pages")
                    name = "blobs/" + doc["sha256"]
                    if name not in blobs:
                        raise ValueError("missing source")
                    used.add(name)
            for source_hash, pages in m.get("previews", {}).items():
                if "blobs/" + source_hash not in used or not isinstance(pages, dict):
                    raise ValueError("preview source")
                for page_number, image_hash in pages.items():
                    if not page_number.isdigit() or int(page_number) < 1:
                        raise ValueError("preview page")
                    name = "blobs/" + image_hash
                    if name not in blobs or not blobs[name].startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("preview image")
                    used.add(name)
            if used != set(blobs):
                raise ValueError("unreferenced source")
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewError("Invalid task package manifest or source references.") from exc

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path / "review.sqlite", timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def checked(self, unit, content, final=False):
        record = SimpleNamespace(record_id=unit["id"], subject_id=unit["subject_id"], date=unit["date"])
        # Pure validation: no project database or provider is accessed.
        return DayReviewService.checked(
            DayReviewService.__new__(DayReviewService),
            content,
            record,
            unit["snapshot"],
            unit["content"],
            final=final,
        )

    def detail(self, unit_id):
        if unit_id not in self.units:
            raise ReviewError("Day not found in this package.", 404)
        u = self.units[unit_id]
        with self.connection() as c:
            row = c.execute("SELECT * FROM drafts WHERE task=?", (u["task_id"],)).fetchone()
        content = self.checked(u, json.loads(row["content"]))
        task = {
            "id": u["task_id"],
            "revision": row["revision"],
            "status": row["status"],
            "assignee_id": self.manifest["reviewer"]["id"],
        }
        return {
            "record_id": u["id"],
            "subject_id": u["subject_id"],
            "date": u["date"],
            "source_record_ids": sorted({m["record_id"] for m in u["snapshot"]["members"]}),
            "input_version": u["input_version"],
            "snapshot": copy.deepcopy(u["snapshot"]),
            "content": content,
            "task": task,
            "tasks": [task],
            "required_reviews": 1,
            "submissions": [],
            "decision": None,
            "help": [],
            "approved_id": None,
            "blind_review": True,
            "display_mode": "offline_review",
            "approval_blocking_codes": sorted(APPROVAL_BLOCKING_CODES),
        }

    def queue(self):
        result = []
        for unit in self.units.values():
            d = self.detail(unit["id"])
            docs = unit["snapshot"]["documents"]
            result.append(
                {
                    **{
                        k: d[k]
                        for k in (
                            "record_id",
                            "subject_id",
                            "date",
                            "input_version",
                            "tasks",
                            "source_record_ids",
                        )
                    },
                    "state": d["task"]["status"],
                    "has_candidate": True,
                    "submitted": int(d["task"]["status"] == "submitted"),
                    "required_reviews": 1,
                    "pages": sum(x["page_count"] for x in docs.values()),
                    "attachments": sum(x["role"] == "attachment" for x in docs.values()),
                    "issues": len(d["content"]["issues"]),
                }
            )
        return result

    def save(self, task_id, revision, content, submit=False):
        if task_id not in self.tasks:
            raise ReviewError("Task not found in this package.", 404)
        if type(revision) is not int:
            raise ReviewError("A valid saved revision is required.")
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM drafts WHERE task=?", (task_id,)).fetchone()
            if row["revision"] != revision or row["status"] == "submitted":
                raise ReviewError("A newer or completed review exists. Reload before continuing.", 409)
            validated = self.checked(
                self.tasks[task_id], json.loads(row["content"]) if submit else content, final=submit
            )
            new_revision = revision + 1
            c.execute(
                "UPDATE drafts SET revision=?,status=?,content=? WHERE task=?",
                (
                    new_revision,
                    "submitted" if submit else "in_progress",
                    canonical(validated).decode(),
                    task_id,
                ),
            )
            c.execute(
                "INSERT INTO edits VALUES (?,?,?)", (task_id, new_revision, canonical(validated).decode())
            )
            return {"revision": new_revision, "content": validated}

    def reopen(self, task_id, revision):
        """Resume a completed local review without replacing its saved history."""
        if task_id not in self.tasks:
            raise ReviewError("Task not found in this package.", 404)
        if type(revision) is not int:
            raise ReviewError("A valid saved revision is required.")
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM drafts WHERE task=?", (task_id,)).fetchone()
            if row["revision"] != revision or row["status"] != "submitted":
                raise ReviewError("The review has changed. Reload before continuing.", 409)
            new_revision = revision + 1
            c.execute(
                "UPDATE drafts SET revision=?,status='in_progress' WHERE task=?",
                (new_revision, task_id),
            )
            c.execute(
                "INSERT INTO edits VALUES (?,?,?)", (task_id, new_revision, row["content"])
            )
            return {"revision": new_revision, "content": json.loads(row["content"])}

    def export_result(self):
        units = []
        with self.connection() as c:
            c.execute("BEGIN")
            for task_id, u in self.tasks.items():
                row = c.execute("SELECT * FROM drafts WHERE task=?", (task_id,)).fetchone()
                if row["status"] != "submitted":
                    raise ReviewError("Complete every day in this package before exporting results.")
                content = self.checked(u, json.loads(row["content"]), final=True)
                units.append(
                    {
                        "task_id": task_id,
                        "baseline_sha256": u["baseline_sha256"],
                        "content": content,
                        "edits": [
                            dict(r)
                            for r in c.execute(
                                "SELECT revision,content FROM edits WHERE task=? ORDER BY revision",
                                (task_id,),
                            )
                        ],
                    }
                )
        result = {
            "format_version": 1,
            "kind": "result",
            "package_id": self.manifest["package_id"],
            "project_id": self.manifest["project_id"],
            "reviewer_id": self.manifest["reviewer"]["id"],
            "task_archive_sha256": self.archive_hash,
            "units": units,
        }
        data = pack(result, [])
        retain(self.path / "results", data)
        return data
