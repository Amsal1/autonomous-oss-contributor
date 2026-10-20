"""README demo pages from the capture transcript (dev-only; run by ``scripts/dev/make-demo.ps1``).

build.py render    --transcript T --out DIR   pages for the stills (light, dark) and the GIF frames, each
                                              checked with ``assert_faithful``; writes DIR/frames.json
build.py check-dom --transcript T --pages DIR re-check every page against the DOM Chrome dumped
                                              (``<page>.dom.html``)
build.py badges    --out DIR                  the README badges
build.py digest    --transcript T             the render digest (stale-media check)
build.py manifest  --transcript T --pages DIR --measure M --media DIR --out FILE   the media manifest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from demokit import badges, checks, pages, scenes
from demokit.chatstate import snapshot
from demokit.transcript import load

FRAMES = "frames.json"


def cmd_render(args: argparse.Namespace) -> int:
    built = pages.build(load(args.transcript))
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.html"):
        old.unlink()
    for page in built:
        (out / page.name).write_bytes(page.html.encode("utf-8"))
    table = [{"id": s.id, "title": s.title, "alt": s.alt, "still": s.still} for s in scenes.SCENES]
    doc = {"render_digest": pages.digest(built), "gif_s": scenes.total_s(), "pages": [p.entry for p in built],
           "scenes": table}  # fmt: skip
    data = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    (out / FRAMES).write_bytes(data.encode("utf-8"))
    print(f"render: {len(built)} pages, every page passed assert_faithful (digest {doc['render_digest'][:12]})")
    return 0


def cmd_check_dom(args: argparse.Namespace) -> int:
    transcript = load(args.transcript)
    marks = transcript.marks()
    doc = json.loads((args.pages / FRAMES).read_text(encoding="utf-8"))
    checked = 0
    for entry in doc["pages"]:
        dom = args.pages / (entry["page"].removesuffix(".html") + ".dom.html")
        if not dom.is_file():
            print(f"check-dom: missing Chrome DOM dump {dom.name}", file=sys.stderr)
            return 1
        frame = snapshot(transcript, marks[entry["mark"]])
        try:
            checks.assert_faithful(dom.read_text(encoding="utf-8"), frame)
        except checks.Unfaithful as exc:
            print(f"check-dom: {dom.name}: {exc}", file=sys.stderr)
            return 1
        checked += 1
    print(f"check-dom: {checked} Chrome DOM dumps match the captured payloads")
    return 0


def cmd_badges(args: argparse.Namespace) -> int:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    for name, text in badges.all_badges().items():
        target = out / name
        data = text.encode("utf-8")
        if target.is_file() and target.read_bytes() == data:
            print(f"badge {name}: unchanged")
            continue
        target.write_bytes(data)
        print(f"badge {name}: written")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    """docs/media/demo.manifest.json: tool versions, digests, per-scene payload hashes and every output's
    sha256, size and measurements (from make-demo.ps1). Rewritten only when its content changes."""
    frames = json.loads((args.pages / FRAMES).read_text(encoding="utf-8"))
    measured = json.loads(args.measure.read_text(encoding="utf-8-sig"))
    media: Path = args.media
    outputs = {}
    for rel, info in sorted(measured["outputs"].items()):
        data = (media / rel).read_bytes()
        outputs[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), **info}
    stills = {
        p["scene"]: p["payload_sha256"] for p in frames["pages"] if p["kind"] == "still" and p["theme"] == "light"
    }
    doc = {
        "generator": "scripts/dev/make-demo.ps1 (tests/e2e/test_demo_capture.py -> scripts/dev/demo/build.py)",
        "tools": {"chrome": measured["chrome_version"], "ffmpeg": measured["ffmpeg_version"]},
        "transcript_sha256": hashlib.sha256(args.transcript.read_bytes()).hexdigest(),
        "render_digest": frames["render_digest"],
        "gif_inputs": measured["gif_inputs"],
        "gif_s": frames["gif_s"],
        "scenes": [{"id": s["id"], "still": s["still"], "payload_sha256": stills[s["id"]]} for s in frames["scenes"]],
        "outputs": outputs,
    }
    data = (json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    target: Path = args.out
    if target.is_file() and target.read_bytes() == data:
        print("manifest: unchanged")
        return 0
    target.write_bytes(data)
    print("manifest: written")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    print(pages.digest(pages.build(load(args.transcript))))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render")
    p.add_argument("--transcript", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("check-dom")
    p.add_argument("--transcript", type=Path, required=True)
    p.add_argument("--pages", type=Path, required=True)
    p = sub.add_parser("badges")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("digest")
    p.add_argument("--transcript", type=Path, required=True)
    p = sub.add_parser("manifest")
    p.add_argument("--transcript", type=Path, required=True)
    p.add_argument("--pages", type=Path, required=True)
    p.add_argument("--measure", type=Path, required=True)
    p.add_argument("--media", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    handlers = {"render": cmd_render, "check-dom": cmd_check_dom, "badges": cmd_badges, "digest": cmd_digest,
                "manifest": cmd_manifest}  # fmt: skip
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
