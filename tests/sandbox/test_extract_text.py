"""PDF text in the real ``sandbox-full`` image (DESIGN §14.12.5, §14.19 item 9; PLAN item 37; root).

``extract_text`` runs in profile ``extract`` through sandboxd's JobService and the Docker filter:
``--network none``, no key, no repo; ``pdftotext`` (poppler-utils, pinned) reads the PDF and the text
comes back through the key-scanned artifacts.
"""

from __future__ import annotations

import json
import secrets

from tests.sandbox.conftest import Sandbox

CID = "sys-chat"


def tiny_pdf(text: str) -> bytes:
    """A one-page PDF with ``text`` in Helvetica and a correct xref table."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _run(sandbox: Sandbox, pdf: bytes) -> tuple[int, str | None, dict[str, bytes]]:
    inputs = sandbox.root / "inputs" / secrets.token_hex(4)
    inputs.mkdir(parents=True)
    (inputs / "doc.pdf").write_bytes(pdf)
    plan = sandbox.plan(CID, "extract", entrypoint="extract_text", inputs={"doc.pdf": inputs / "doc.pdf"},
                        timeout=120)  # fmt: skip
    out = sandbox.run(plan)
    return out.exit_code, out.error_class, out.files


def test_pdftotext_extracts_the_text_offline(sandbox: Sandbox) -> None:
    code, error, files = _run(sandbox, tiny_pdf("Protein 31 g per 100 g"))
    assert code == 0 and error is None, files.get("result.json")
    assert b"Protein 31 g per 100 g" in files["text.txt"]
    assert json.loads(files["result.json"])["ok"] is True


def test_not_a_pdf_is_refused(sandbox: Sandbox) -> None:
    code, _, files = _run(sandbox, b"PK\x03\x04 a zip, not a pdf")
    assert code == 2 and json.loads(files["result.json"])["error_class"] == "not_pdf"
