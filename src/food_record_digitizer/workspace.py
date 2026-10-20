from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import select

from .db import Project, Record, database
from .ingest import BlobStore, InputError


class Workspace:
    def __init__(self, path: Path):
        self.path = path.resolve()
        config = self.path / "workspace.json"
        if not config.exists():
            raise InputError("Workspace not found. Run 'food-record init' first.")
        self.config = json.loads(config.read_text())
        if self.config.get("storage_version") != 1:
            raise InputError("Unsupported workspace version; migrate before opening")
        self.project_id = self.config["project_id"]
        url = os.environ.get("FOOD_RECORD_DATABASE_URL")
        if self.config["database"] == "postgresql" and not url:
            raise InputError("Set FOOD_RECORD_DATABASE_URL for this PostgreSQL workspace")
        if self.config["database"] == "sqlite":
            url = f"sqlite:///{self.path / 'records.db'}"
        self.factory = database(url)
        self.store = BlobStore(self.path / "sources")

    @classmethod
    def initialize(cls, path: Path, project_id: str, name: str):
        from .schemas import Metadata

        Metadata(record_id=project_id)
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        config = path / "workspace.json"
        if config.exists():
            raise InputError("Workspace already exists; it will not be overwritten")
        url = os.environ.get("FOOD_RECORD_DATABASE_URL")
        if url and not url.startswith("postgresql+psycopg://"):
            raise InputError("FOOD_RECORD_DATABASE_URL must use postgresql+psycopg://")
        config.write_text(
            json.dumps(
                {
                    "storage_version": 1,
                    "project_id": project_id,
                    "database": "postgresql" if url else "sqlite",
                },
                indent=2,
            )
        )
        try:
            workspace = cls(path)
            with workspace.factory.begin() as session:
                if session.get(Project, project_id) is None:
                    session.add(Project(id=project_id, name=name))
            return workspace
        except Exception:
            config.unlink()
            raise

    def records(self):
        with self.factory() as session:
            return list(
                session.scalars(
                    select(Record).where(Record.project_id == self.project_id).order_by(Record.record_id)
                )
            )
