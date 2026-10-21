import subprocess

import pytest
from PIL import Image
from test_ingest import pdf

from food_record_digitizer.imaging import render_pages
from food_record_digitizer.ingest import BlobStore, InputError


@pytest.fixture
def source(tmp_path):
    store = BlobStore(tmp_path / "sources")
    doc = {**store.put(pdf(tmp_path / "R001.pdf")), "role": "main"}
    return store, {"documents": {"R001": doc}}, tmp_path / "rendered"


def test_truncated_render_cache_is_regenerated(source):
    store, snapshot, cache = source
    first = render_pages(store, snapshot, cache)
    output = first[0]["path"]
    output.write_bytes(b"incomplete png")
    assert render_pages(store, snapshot, cache)[0]["path"] == output
    with Image.open(output) as image:
        image.load()
        assert image.size == (1275, 1650)


def test_valid_but_replaced_render_cache_is_regenerated(source):
    store, snapshot, cache = source
    output = render_pages(store, snapshot, cache)[0]["path"]
    Image.new("RGB", (1275, 1650), "red").save(output)
    render_pages(store, snapshot, cache)
    with Image.open(output) as image:
        assert image.convert("RGB").getpixel((100, 100)) == (255, 255, 255)


def test_renderer_failure_never_publishes_partial_cache(source, monkeypatch):
    store, snapshot, cache = source

    def interrupt(command, **kwargs):
        from pathlib import Path

        Path(command[-1] + ".png").write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, 90)

    with monkeypatch.context() as patch:
        patch.setattr("food_record_digitizer.imaging.subprocess.run", interrupt)
        with pytest.raises(InputError):
            render_pages(store, snapshot, cache)
    assert not list(cache.rglob("*.png"))
    output = render_pages(store, snapshot, cache)[0]["path"]
    with Image.open(output) as image:
        image.load()


def test_transport_compression_preserves_source_and_dimensions(tmp_path):
    import hashlib
    import io

    from PIL import Image

    from food_record_digitizer.provider import prepare_image

    path = tmp_path / "page.png"
    source = Image.effect_noise((2400, 3200), 30).convert("RGB")
    source.save(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    data, mime, stats = prepare_image(path)
    assert mime == "image/jpeg"
    assert stats["sent_size"] == [900, 1200]
    assert stats["sent_bytes"] < stats["original_bytes"]
    with Image.open(io.BytesIO(data)) as result:
        assert result.format == "JPEG"
        assert result.size == (900, 1200)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    original, mime, stats = prepare_image(path, 0)
    assert original == path.read_bytes()
    assert mime == "image/png"


def test_transport_compression_does_not_upscale_or_enlarge_small_png(tmp_path):
    from PIL import Image

    from food_record_digitizer.provider import prepare_image

    path = tmp_path / "page.png"
    Image.new("RGB", (100, 200), "white").save(path)
    data, mime, stats = prepare_image(path)
    assert stats["sent_size"] == [100, 200]
    assert len(data) <= path.stat().st_size


def test_attachment_origin_survives_render_cache(source):
    store, snapshot, cache = source
    snapshot["documents"]["R001"]["attachment_original"] = {"origin": "researcher_note"}
    for _ in range(2):
        assert render_pages(store, snapshot, cache)[0]["attachment_origin"] == "researcher_note"
