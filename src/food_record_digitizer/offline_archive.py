"""Versioned, bounded offline exchange archives; no archive content is executed."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import unicodedata
import zipfile
from pathlib import PurePosixPath

from .review import ReviewError

FORMAT_VERSION = 1
MAX_FILES = 10000
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def batch_units(units, units_per_package=10):
    """Count complete participant/date units, never food rows or source pages."""
    if type(units_per_package) is not int or units_per_package < 1:
        raise ReviewError("Days per package must be a positive integer.")
    ordered = sorted(units, key=lambda u: (u["subject_id"], u["date"], u["id"]))
    if len({u["id"] for u in ordered}) != len(ordered):
        raise ReviewError("Select each day only once.")
    return [ordered[i : i + units_per_package] for i in range(0, len(ordered), units_per_package)]


def safe_name(name):
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or ":" in name
        or path.is_absolute()
        or any(x in {"", ".", ".."} for x in name.split("/"))
    ):
        raise ReviewError("Unsafe archive path.")
    return name


def pack(manifest, blobs):
    """Blobs are content-addressed, deduplicated files; manifest is generated locally."""
    if (
        type(manifest.get("format_version")) is not int
        or manifest.get("format_version") != FORMAT_VERSION
        or manifest.get("kind") not in {"task", "result"}
    ):
        raise ReviewError("Unsupported offline package format.")
    files = {}
    for data in blobs:
        files["blobs/" + sha256(data)] = data
    payload = {
        **manifest,
        "files": {name: {"sha256": sha256(data), "bytes": len(data)} for name, data in files.items()},
    }
    files["manifest.json"] = canonical(payload)
    if (
        len(files) > MAX_FILES
        or sum(map(len, files.values())) > MAX_TOTAL_BYTES
        or len(files["manifest.json"]) > MAX_MANIFEST_BYTES
        or any(len(v) > MAX_FILE_BYTES for v in files.values())
    ):
        raise ReviewError("Offline package exceeds the supported size limit.")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, data)
    return out.getvalue()


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON key")
        obj[key] = value
    return obj


def unpack(data, expected_kind):
    """Read bounded bytes into memory without extracting paths to the filesystem."""
    if len(data) > MAX_TOTAL_BYTES:
        raise ReviewError("Offline package exceeds the supported size limit.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
                raise ReviewError("Offline package exceeds the supported size limit.")
            archive_names = {info.filename for info in infos}
            if "Offline Reviewer/start_reviewer.py" in archive_names:
                raise ReviewError(
                    "This ZIP contains the reviewer application, not a review task. "
                    "Open a review-package-<ID>.zip exported by your coordinator "
                    "from the project page using Export review packages."
                )
            names = set()
            for info in infos:
                name = safe_name(info.filename)
                mode = info.external_attr >> 16
                if (
                    name in names
                    or info.is_dir()
                    or info.flag_bits & 1
                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                    or info.file_size > MAX_FILE_BYTES
                ):
                    raise ReviewError("Unsupported or duplicated archive entry.")
                if name != "manifest.json" and not re.fullmatch(r"blobs/[0-9a-f]{64}", name):
                    raise ReviewError(
                        "Unexpected archive entry. Choose the original package ZIP exported "
                        "by the project or reviewer application; do not unzip and repackage it."
                    )
                names.add(name)
            if (
                "manifest.json" not in names
                or archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES
            ):
                raise ReviewError("Missing or oversized package manifest.")
            manifest = json.loads(
                archive.read("manifest.json"),
                object_pairs_hook=unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")),
            )
            if (
                not isinstance(manifest, dict)
                or type(manifest.get("format_version")) is not int
                or manifest.get("format_version") != FORMAT_VERSION
                or manifest.get("kind") != expected_kind
            ):
                raise ReviewError("Unsupported offline package format.")
            declared = manifest.get("files")
            if not isinstance(declared, dict) or set(declared) != names - {"manifest.json"}:
                raise ReviewError("Package file manifest does not match its contents.")
            blobs = {}
            for name, descriptor in declared.items():
                blob = archive.read(name)
                if (
                    not isinstance(descriptor, dict)
                    or descriptor.get("bytes") != len(blob)
                    or descriptor.get("sha256") != sha256(blob)
                    or name != "blobs/" + sha256(blob)
                ):
                    raise ReviewError("Package source checksum mismatch.")
                blobs[name] = blob
            return manifest, blobs
    except (zipfile.BadZipFile, ValueError, KeyError, TypeError, RuntimeError, NotImplementedError) as exc:
        raise ReviewError("Invalid offline package.") from exc


def task_filename(manifest):
    """Portable public filename; archive identity remains in the manifest."""
    name = unicodedata.normalize("NFKC", manifest["reviewer"]["name"])
    name = "".join(c if c.isalnum() or c in " -_" else "-" for c in name)
    name = re.sub(r"[-\s]+", "-", name).strip("-_")[:64] or "Reviewer"
    return f"review-package-{name}-{manifest['package_id'][:8]}.zip"
