"""Image metadata stripping without decoding pixels (DESIGN §15.6, §14.12.5; ADR-0020: no Pillow).

A segment walker over the container formats Telegram delivers: JPEG (drops APP1 EXIF/XMP, APP13
IPTC, COM and every APPn but APP0 JFIF, APP2 ICC and APP14 Adobe), PNG (drops ``eXIf``, ``tEXt``,
``iTXt``, ``zTXt``, ``tIME`` and unknown ancillary chunks), WebP (drops ``EXIF`` and ``XMP `` and clears
their VP8X flags). Every byte of the image data (JPEG from SOS to EOI, PNG ``IDAT``, WebP ``VP8``/
``VP8L``/``ALPH``/``ANMF``) is copied unchanged. HEIC/HEIF is refused ("send it as a photo", Telegram
then converts it to JPEG). Anything malformed raises ``ImageRejected`` and nothing else.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAX_BYTES = 20 * 1024 * 1024
MAX_SEGMENTS = 4096
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}


class ImageRejected(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Stripped:
    data: bytes
    mime: str
    removed: list[str] = field(default_factory=list)


def detect(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in HEIC_BRANDS:
        return "image/heic"
    return "application/octet-stream"


def strip(data: bytes) -> Stripped:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ImageRejected("empty")
    if len(data) > MAX_BYTES:
        raise ImageRejected("too_large")
    data = bytes(data)
    mime = detect(data)
    try:
        if mime == "image/jpeg":
            return _jpeg(data)
        if mime == "image/png":
            return _png(data)
        if mime == "image/webp":
            return _webp(data)
    except ImageRejected:
        raise
    except (IndexError, ValueError, struct.error) as exc:
        raise ImageRejected("malformed") from exc
    if mime == "image/heic":
        raise ImageRejected("heic")
    raise ImageRejected("unsupported")


# --------------------------------------------------------------------------------------- JPEG
_JPEG_KEEP_APP = {0xE0, 0xEE}  # APP0 JFIF/JFXX, APP14 Adobe (colour transform)
_STANDALONE = {0x01, *range(0xD0, 0xD8)}  # TEM, RSTn


def _jpeg(data: bytes) -> Stripped:
    out = bytearray(b"\xff\xd8")
    removed: list[str] = []
    i, n, segments = 2, len(data), 0
    while True:
        segments += 1
        if segments > MAX_SEGMENTS:
            raise ImageRejected("malformed")
        if i + 2 > n or data[i] != 0xFF:
            raise ImageRejected("malformed")
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            raise ImageRejected("malformed")
        marker = data[i]
        i += 1
        if marker == 0xD9:  # EOI without SOS
            raise ImageRejected("malformed")
        if marker in _STANDALONE:
            out += bytes([0xFF, marker])
            continue
        if i + 2 > n:
            raise ImageRejected("malformed")
        length = struct.unpack(">H", data[i : i + 2])[0]
        if length < 2 or i + length > n:
            raise ImageRejected("malformed")
        payload = data[i + 2 : i + length]
        if marker == 0xDA:  # SOS: everything to EOI is image data, copied verbatim
            end = data.rfind(b"\xff\xd9")
            if end < i + length:
                raise ImageRejected("malformed")
            out += b"\xff\xda" + data[i : end + 2]
            return Stripped(bytes(out), "image/jpeg", removed)
        drop = False
        if 0xE0 <= marker <= 0xEF:
            if marker == 0xE2:
                drop = not payload.startswith(b"ICC_PROFILE\x00")  # keep the colour profile only
            else:
                drop = marker not in _JPEG_KEEP_APP
            if marker == 0xE1:
                kind = "xmp" if payload.startswith(b"http://ns.adobe.com/") else "exif"
                removed.append(f"jpeg:{kind}")
            elif drop:
                removed.append(f"jpeg:app{marker - 0xE0}")
        elif marker == 0xFE:
            drop = True
            removed.append("jpeg:comment")
        if not drop:
            out += bytes([0xFF, marker]) + data[i : i + length]
        i += length


# ---------------------------------------------------------------------------------------- PNG
_PNG_DROP = {b"eXIf", b"tEXt", b"iTXt", b"zTXt", b"tIME"}
_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB", b"bKGD",
             b"hIST", b"pHYs", b"sPLT", b"cICP", b"mDCv", b"cLLi", b"acTL", b"fcTL", b"fdAT"}  # fmt: skip


def _png(data: bytes) -> Stripped:
    out = bytearray(data[:8])
    removed: list[str] = []
    i, n, chunks, ended = 8, len(data), 0, False
    while i < n:
        chunks += 1
        if chunks > MAX_SEGMENTS or i + 12 > n:
            raise ImageRejected("malformed")
        length = struct.unpack(">I", data[i : i + 4])[0]
        ctype = data[i + 4 : i + 8]
        end = i + 12 + length
        if end > n or not ctype.isalpha():
            raise ImageRejected("malformed")
        if chunks == 1 and ctype != b"IHDR":
            raise ImageRejected("malformed")
        if ctype in _PNG_KEEP:
            out += data[i:end]
        elif ctype in _PNG_DROP or (ctype[0:1].islower()):
            removed.append(f"png:{ctype.decode('ascii')}")
        else:
            raise ImageRejected("unknown_critical_chunk")
        i = end
        if ctype == b"IEND":
            ended = True
            break
    if not ended:
        raise ImageRejected("malformed")
    if i < n:
        removed.append("png:trailing_data")
    return Stripped(bytes(out), "image/png", removed)


# --------------------------------------------------------------------------------------- WebP
_WEBP_DROP = {b"EXIF", b"XMP "}
_WEBP_KEEP = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}
_VP8X_EXIF, _VP8X_XMP = 0x08, 0x04


def _webp(data: bytes) -> Stripped:
    riff_size = struct.unpack("<I", data[4:8])[0]
    if riff_size + 8 > len(data) or riff_size < 4:
        raise ImageRejected("malformed")
    body = data[12 : 8 + riff_size]
    out = bytearray()
    removed: list[str] = []
    i, n, chunks = 0, len(body), 0
    while i < n:
        chunks += 1
        if chunks > MAX_SEGMENTS or i + 8 > n:
            raise ImageRejected("malformed")
        ctype = body[i : i + 4]
        length = struct.unpack("<I", body[i + 4 : i + 8])[0]
        end = i + 8 + length + (length & 1)
        if i + 8 + length > n:
            raise ImageRejected("malformed")
        chunk = body[i : min(end, n)]
        if ctype in _WEBP_DROP:
            removed.append(f"webp:{ctype.decode('ascii').strip().lower()}")
        elif ctype in _WEBP_KEEP:
            if ctype == b"VP8X":
                if length < 10:
                    raise ImageRejected("malformed")
                flags = chunk[8] & ~(_VP8X_EXIF | _VP8X_XMP)
                chunk = chunk[:8] + bytes([flags]) + chunk[9:]
            out += chunk
        else:
            removed.append(f"webp:{ctype.decode('latin-1').strip()}")
        i = end
    if not out:
        raise ImageRejected("malformed")
    head = b"RIFF" + struct.pack("<I", len(out) + 4) + b"WEBP"
    return Stripped(head + bytes(out), "image/webp", removed)
