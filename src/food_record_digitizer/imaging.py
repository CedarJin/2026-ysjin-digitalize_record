"""Render immutable PDF sources with verified, atomically published image caches."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from .ingest import InputError

RENDER_VERSION = "2"


def _image_valid(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                return False
            image.verify()
        with Image.open(path) as image:
            image.load()
            return image.width > 0 and image.height > 0
    except (OSError, ValueError, SyntaxError):
        return False


def _cached(output: Path, receipt: Path, source_hash: str, page: int, dpi: int) -> bool:
    try:
        metadata = json.loads(receipt.read_text())
        expected = {
            "source_sha256": source_hash,
            "page_number": page,
            "dpi": dpi,
            "render_version": RENDER_VERSION,
        }
        if not isinstance(metadata, dict) or any(
            metadata.get(key) != value for key, value in expected.items()
        ):
            return False
        if hashlib.sha256(output.read_bytes()).hexdigest() != metadata.get("image_sha256"):
            return False
        return _image_valid(output)
    except (OSError, ValueError):
        return False


def render_pages(store, snapshot: dict, cache: Path, dpi: int = 150) -> list[dict]:
    binary = shutil.which("pdftoppm")
    if not binary:
        raise InputError("Poppler is required: install pdftoppm before extraction")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 72 <= dpi <= 300:
        raise InputError("Rendering DPI must be an integer from 72 to 300")
    pages = []
    for source_id, doc in sorted(snapshot["documents"].items(), key=lambda v: (v[1]["role"] != "main", v[0])):
        if doc["error"]:
            raise InputError("Cannot render an unreadable source PDF")
        source = store.verified_path(doc["sha256"])
        directory = cache / doc["sha256"] / f"dpi-{dpi}"
        directory.mkdir(parents=True, exist_ok=True)
        for page in range(1, doc["page_count"] + 1):
            output = directory / f"{page}.png"
            receipt = directory / f"{page}.json"
            if not _cached(output, receipt, doc["sha256"], page, dpi):
                # Never let an interrupted renderer leave a plausible completed cache entry.
                with tempfile.TemporaryDirectory(prefix="render-", dir=directory) as temp:
                    prefix = Path(temp) / "page"
                    generated = prefix.with_suffix(".png")
                    try:
                        subprocess.run(
                            [
                                binary,
                                "-f",
                                str(page),
                                "-l",
                                str(page),
                                "-singlefile",
                                "-r",
                                str(dpi),
                                "-png",
                                str(source),
                                str(prefix),
                            ],
                            check=True,
                            timeout=90,
                            capture_output=True,
                        )
                        if not _image_valid(generated):
                            raise InputError("PDF renderer did not produce a complete PNG")
                        metadata = {
                            "source_sha256": doc["sha256"],
                            "page_number": page,
                            "dpi": dpi,
                            "render_version": RENDER_VERSION,
                            "image_sha256": hashlib.sha256(generated.read_bytes()).hexdigest(),
                        }
                        staged_receipt = Path(temp) / "receipt.json"
                        staged_receipt.write_text(json.dumps(metadata))
                        os.replace(generated, output)
                        os.replace(staged_receipt, receipt)
                    except (subprocess.SubprocessError, OSError) as exc:
                        raise InputError("PDF rendering failed") from exc
            pages.append(
                {"source_record_id": source_id, "page_number": page, "role": doc["role"], "path": output}
            )
    return pages
