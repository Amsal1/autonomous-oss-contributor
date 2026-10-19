"""A stand-in for ``age`` in the hermetic backup/restore tests (no real cryptography).

``fake_age.py -e -r R [-r R ...] -o OUT IN`` writes OUT = header line + recipients line + IN's bytes
XOR 0x5A (so the plaintext is not visible in the file). ``fake_age.py -d -i - -o OUT IN`` reads the
identity from stdin: ``FAKE-IDENTITY:<recipient>`` must name one of the recipients, else exit 1 like
age's "no identity matched any of the recipients". The system test uses the real age.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HEADER = b"fake-age-encryption/v1\n"


def _xor(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)


def main(argv: list[str]) -> int:
    mode, recipients, identity_stdin, out, inp = "", [], False, "", ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in {"-e", "-d"}:
            mode = arg
        elif arg == "-r":
            recipients.append(argv[i + 1])
            i += 1
        elif arg == "-i":
            identity_stdin = argv[i + 1] == "-"
            i += 1
        elif arg == "-o":
            out = argv[i + 1]
            i += 1
        else:
            inp = arg
        i += 1
    data = Path(inp).read_bytes()
    if mode == "-e":
        if not recipients:
            print("age: error: missing recipients", file=sys.stderr)
            return 1
        Path(out).write_bytes(HEADER + json.dumps(recipients).encode() + b"\n" + _xor(data))
        return 0
    if not identity_stdin:
        print("age: error: missing identity", file=sys.stderr)
        return 1
    identity = sys.stdin.buffer.read().decode().strip()
    if not data.startswith(HEADER):
        print("age: error: failed to read header", file=sys.stderr)
        return 1
    head, _, body = data[len(HEADER) :].partition(b"\n")
    allowed = json.loads(head)
    if not identity.startswith("FAKE-IDENTITY:") or identity.split(":", 1)[1] not in allowed:
        print("age: error: no identity matched any of the recipients", file=sys.stderr)
        return 1
    Path(out).write_bytes(_xor(body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
