"""Image metadata removal without Pillow (DESIGN §15.6, §14.12.5, ADR-0020; CR2-7d)."""

from __future__ import annotations

import pytest

from ossbot.gatekeeper.chat import imgmeta
from tests.unit.gatekeeper.chat import images


def test_jpeg_metadata_removed_pixels_identical() -> None:
    src = images.jpeg()
    out = imgmeta.strip(src)
    assert out.mime == "image/jpeg"
    assert images.GPS not in out.data
    assert set(out.removed) >= {"jpeg:exif", "jpeg:xmp", "jpeg:app13", "jpeg:comment"}
    assert src[src.index(b"\xff\xda") :] == out.data[out.data.index(b"\xff\xda") :]  # SOS..EOI byte-identical
    assert b"ICC_PROFILE" in out.data and b"JFIF" in out.data  # colour profile and JFIF header kept
    assert imgmeta.strip(out.data).data == out.data  # idempotent


def test_png_metadata_removed_pixels_identical() -> None:
    src = images.png()
    out = imgmeta.strip(src)
    assert images.GPS not in out.data
    assert {"png:tEXt", "png:eXIf", "png:iTXt", "png:zTXt", "png:tIME"} <= set(out.removed)
    idat = images.chunk(b"IDAT", src[src.index(b"IDAT") + 4 : src.index(b"IEND") - 8])
    assert idat in out.data and b"pHYs" in out.data


def test_webp_metadata_removed_flags_cleared() -> None:
    src = images.webp()
    out = imgmeta.strip(src)
    assert images.GPS not in out.data
    assert {"webp:exif", "webp:xmp"} <= set(out.removed)
    assert out.data[12:16] == b"VP8X" and out.data[20] & 0x0C == 0
    assert images.wchunk(b"VP8 ", b"\x9d\x01\x2a" + bytes(17)) in out.data
    assert int.from_bytes(out.data[4:8], "little") == len(out.data) - 8


@pytest.mark.parametrize(("data", "reason"), [
    (images.heic(), "heic"), (b"GIF89a" + bytes(20), "unsupported"), (b"", "empty"),
    (b"\xff\xd8\xff\xe0\x00", "malformed"), (images.png()[:40], "malformed"),
    (b"\x89PNG\r\n\x1a\n" + images.chunk(b"ABCD", b"x"), "malformed"),
])  # fmt: skip
def test_refusals(data: bytes, reason: str) -> None:
    with pytest.raises(imgmeta.ImageRejected) as err:
        imgmeta.strip(data)
    assert err.value.reason == reason


def test_size_cap() -> None:
    with pytest.raises(imgmeta.ImageRejected, match="too_large"):
        imgmeta.strip(b"\xff\xd8\xff" + bytes(imgmeta.MAX_BYTES))
