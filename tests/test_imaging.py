from __future__ import annotations

from PIL import Image

from cair_agent.imaging import sampler
from cair_agent.imaging.encode import encode_jpeg_data_url
from cair_agent.imaging.ops import contact_sheet, crop_zoom, resize_max_side, side_by_side


def test_uniform_covers_first_and_last():
    idx = sampler.uniform_indices(679, 8)
    assert idx[0] == 0 and idx[-1] == 678 and len(idx) == 8
    assert idx == sorted(idx)
    assert sampler.uniform_indices(3, 8) == [0, 1, 2]
    assert sampler.uniform_indices(100, 1) == [50]


def test_neighbors_clipped():
    assert sampler.neighbor_indices(10, 0, 2) == [0, 1, 2]
    assert sampler.neighbor_indices(10, 9, 2) == [7, 8, 9]
    assert sampler.neighbor_indices(10, 5, 1) == [4, 5, 6]


def test_select_small_series_returns_all():
    assert sampler.select("anchors", 4, anchor=2) == [0, 1, 2, 3]
    assert sampler.select("uniform", 6, n=2) == list(range(6))


def test_select_strategies():
    assert sampler.select("anchors", 679, anchor=339) == [339]
    assert sampler.select("anchors", 679, anchor=None) == [339]
    assert len(sampler.select("uniform", 679, n=8)) == 8
    assert sampler.select("neighbors", 679, center=339, k=2) == [337, 338, 339, 340, 341]


def test_resize_never_upscales():
    im = Image.new("RGB", (512, 512))
    assert resize_max_side(im, 1024).size == (512, 512)
    big = Image.new("RGB", (2304, 2800))
    out = resize_max_side(big, 1024)
    assert max(out.size) == 1024


def test_crop_zoom_bounds():
    im = Image.new("L", (100, 200))
    out = crop_zoom(im, (0.5, 0.5, 1.5, 1.5), scale=2)  # roi beyond image is clipped
    assert out.size == (100, 200)
    out = crop_zoom(im, (0.9, 0.9, 0.9, 0.9), scale=2)  # degenerate roi -> at least 1px
    assert out.size[0] >= 1 and out.size[1] >= 1


def test_contact_sheet_and_strip():
    ims = [Image.new("L", (64, 64), c * 30) for c in range(8)]
    labels = [f"s{i}" for i in range(8)]
    sheet = contact_sheet(ims, labels, cols=4, tile=64)
    assert sheet.size == (256, 128)
    strip = side_by_side(ims[:2], labels[:2], height=32)
    assert strip.size[1] == 32


def test_encode_data_url():
    url = encode_jpeg_data_url(Image.new("RGB", (10, 10)))
    assert url.startswith("data:image/jpeg;base64,")
