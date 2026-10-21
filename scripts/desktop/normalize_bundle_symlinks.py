"""Make PyInstaller's bundled symlinks portable and reject dangling links."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def normalize_bundle_symlinks(bundle: Path) -> tuple[int, int]:
    """Rewrite in-bundle absolute symlinks as relative links and validate all links.

    Absolute symlinks make a packaged app depend on the build machine's directory
    layout. Relative links are portable as long as they resolve within the bundle.
    Links to files outside the bundle and dangling links are rejected.
    """
    root = bundle.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Bundle path is not a directory: {bundle}")

    rewritten = 0
    checked = 0
    for link in root.rglob("*"):
        if not link.is_symlink():
            continue

        raw_target = os.readlink(link)
        try:
            target = link.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Broken or cyclic symlink: {link} -> {raw_target}") from exc

        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Symlink escapes the app bundle: {link} -> {raw_target}") from exc

        if Path(raw_target).is_absolute():
            relative_target = os.path.relpath(target, link.parent)
            link.unlink()
            link.symlink_to(relative_target)
            rewritten += 1
        checked += 1

    return rewritten, checked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="PyInstaller onedir output directory")
    args = parser.parse_args()
    rewritten, checked = normalize_bundle_symlinks(args.bundle)
    print(f"Validated {checked} bundled symlinks; rewrote {rewritten} absolute links.")


if __name__ == "__main__":
    main()
