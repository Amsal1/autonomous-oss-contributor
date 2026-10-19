"""Hand-built image containers for the metadata tests (structure only; the pixel data is opaque)."""

from __future__ import annotations

import struct
import zlib

GPS = b"GPS 52.5200N 13.4050E"


def seg(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def jpeg(scan: bytes = b"\x12\x34\xff\x00\x56" * 20) -> bytes:
    return (b"\xff\xd8" + seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
            + seg(0xE1, b"Exif\x00\x00" + GPS) + seg(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x>" + GPS + b"</x>")
            + seg(0xE2, b"ICC_PROFILE\x00\x01\x01icc") + seg(0xED, b"Photoshop 3.0\x00" + GPS)
            + seg(0xFE, b"comment " + GPS) + seg(0xDB, b"\x00" + bytes(64))
            + seg(0xC0, b"\x08\x00\x10\x00\x10\x01\x01\x11\x00") + seg(0xC4, b"\x00" + bytes(16) + b"\x00")
            + seg(0xDA, b"\x01\x01\x00\x00\x3f\x00") + scan + b"\xff\xd9")  # fmt: skip


def chunk(ctype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload))


def png(idat: bytes = zlib.compress(b"\x00\xff\x00\x00" * 16)) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"Comment\x00" + GPS) + chunk(b"eXIf", b"MM\x00*" + GPS)
            + chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + GPS)
            + chunk(b"zTXt", b"k\x00\x00" + zlib.compress(GPS))
            + chunk(b"tIME", b"\x07\xea\x0a\x13\x01\x02\x03") + chunk(b"pHYs", b"\x00\x00\x0b\x13\x00\x00\x0b\x13\x01")
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))  # fmt: skip


def wchunk(ctype: bytes, payload: bytes) -> bytes:
    pad = b"\x00" if len(payload) & 1 else b""
    return ctype + struct.pack("<I", len(payload)) + payload + pad


def webp(vp8: bytes = b"\x9d\x01\x2a" + bytes(17)) -> bytes:
    body = (wchunk(b"VP8X", bytes([0x0C, 0, 0, 0]) + b"\x03\x00\x00\x03\x00\x00") + wchunk(b"VP8 ", vp8)
            + wchunk(b"EXIF", b"MM\x00*" + GPS) + wchunk(b"XMP ", b"<x>" + GPS + b"</x>"))  # fmt: skip
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


def heic() -> bytes:
    return struct.pack(">I", 24) + b"ftypheic" + b"\x00\x00\x00\x00mif1heic" + bytes(32)
