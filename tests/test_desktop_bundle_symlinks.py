import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "desktop" / "normalize_bundle_symlinks.py"
SPEC = importlib.util.spec_from_file_location("normalize_bundle_symlinks", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
normalize_bundle_symlinks = MODULE.normalize_bundle_symlinks


def test_rewrites_absolute_links_to_relative_links(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    library_dir = bundle / "_internal" / "vendor"
    library_dir.mkdir(parents=True)
    library = library_dir / "libsample.dylib"
    library.write_bytes(b"library")
    link = bundle / "_internal" / "libsample.dylib"
    link.symlink_to(library)

    assert normalize_bundle_symlinks(bundle) == (1, 1)
    assert not Path(link.readlink()).is_absolute()
    assert link.resolve() == library


def test_preserves_valid_relative_links(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    library_dir = bundle / "_internal" / "vendor"
    library_dir.mkdir(parents=True)
    library = library_dir / "libsample.dylib"
    library.write_bytes(b"library")
    link = bundle / "_internal" / "libsample.dylib"
    link.symlink_to("vendor/libsample.dylib")

    assert normalize_bundle_symlinks(bundle) == (0, 1)
    assert link.readlink() == Path("vendor/libsample.dylib")


def test_rejects_dangling_links(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "missing.dylib").symlink_to("not-present.dylib")

    with pytest.raises(ValueError, match="Broken or cyclic symlink"):
        normalize_bundle_symlinks(bundle)


def test_rejects_links_outside_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside.dylib"
    outside.write_bytes(b"library")
    (bundle / "external.dylib").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes the app bundle"):
        normalize_bundle_symlinks(bundle)


def test_rejects_cyclic_links(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "a").symlink_to("b")
    (bundle / "b").symlink_to("a")
    with pytest.raises(ValueError, match="Broken or cyclic symlink"):
        normalize_bundle_symlinks(bundle)


def test_check_only_rejects_absolute_without_changing_link(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    library = bundle / "library"
    library.write_bytes(b"library")
    link = bundle / "link"
    link.symlink_to(library)
    with pytest.raises(ValueError, match="Absolute symlink"):
        normalize_bundle_symlinks(bundle, check_only=True)
    assert link.readlink() == library


def test_normalized_bundle_remains_valid_after_move(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    library = bundle / "library"
    library.write_bytes(b"library")
    (bundle / "link").symlink_to(library)
    normalize_bundle_symlinks(bundle)
    moved = tmp_path / "relocated"
    bundle.rename(moved)
    assert normalize_bundle_symlinks(moved, check_only=True) == (0, 1)
    assert (moved / "link").read_bytes() == b"library"
