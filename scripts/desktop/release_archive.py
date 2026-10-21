"""Create a release ZIP and validate its extracted symlinks before checksumming."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path

from normalize_bundle_symlinks import normalize_bundle_symlinks

ROOT = Path(__file__).resolve().parents[2]


def main():
    version = json.loads((ROOT / "desktop/package.json").read_text())["version"]
    app = ROOT / "dist/macos/Food Record-darwin-arm64/Food Record.app"
    normalize_bundle_symlinks(app, check_only=True)
    output = ROOT / f"dist/Food-Record-{version}-macOS-arm64.zip"
    subprocess.run(
        ["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(output)], check=True
    )
    with zipfile.ZipFile(output) as archive:
        if archive.testzip():
            raise ValueError("Release ZIP integrity check failed")
        for name in archive.namelist():
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts:
                raise ValueError("Unsafe archive path")
            if set(parts) & {".env", "records.db", "workspace.json", "docs", "work", "node_modules"}:
                raise ValueError(f"Private or development files in installer: {name}")
    with tempfile.TemporaryDirectory(prefix="food-record-archive-") as temporary:
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(output), temporary], check=True)
        rewritten, checked = normalize_bundle_symlinks(Path(temporary) / "Food Record.app", check_only=True)
        assert rewritten == 0
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{checksum}  {output.name}\n")
    print(f"Validated {checked} extracted links; {output.name}: {checksum}")


if __name__ == "__main__":
    main()
