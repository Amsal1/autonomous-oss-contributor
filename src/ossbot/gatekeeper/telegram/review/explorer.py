"""S2 explorer and S3 file view (DESIGN §14.11.2-§14.11.3; mockup 02). Gatekeeper data only (U4); no
approval or confirm token on any of these views (U3). Every token is a multi-use ``nav``-class kit token
with ``{cid, rev, action_id}`` (delta 1) bound to its view message.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.review import patchview
from ossbot.gatekeeper.telegram.review.patchview import FilePatch
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui.limits import middle_cut

SECTIONS = ("Summary", "Why this issue", "Signals", "Risk", "Files", "Diff", "Tests", "Self-review", "PR text")
FILES_PER_PAGE = 6
PICKER_PER_PAGE = 8
HUNKS_PER_PAGE = 8
RISKY = (".github/", "Dockerfile", "package.json", "requirements", "pyproject.toml", "go.mod", "Cargo.toml",
         "lock", "vendor/", "generated", ".min.")  # fmt: skip
HINT = "Long-press lines → Reply with quote to comment on them."


@dataclass(frozen=True)
class Subject:
    cid: str
    rev: int
    action_id: str | None
    kind: str
    upstream: str
    contrib: sqlite3.Row
    files: list[FilePatch]

    def arg(self, **extra: Any) -> dict[str, Any]:
        return {"cid": self.cid, "rev": self.rev, "action_id": self.action_id, **extra}


def subject(conn: sqlite3.Connection, derived_dir: Any, cid: str, rev: int, action_id: str | None) -> Subject:
    contrib = db.one(conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    if contrib is None:
        raise KeyError(cid)
    kind = "open_pr"
    if action_id is not None:
        row = db.one(conn, "SELECT kind FROM outbound_actions WHERE id = ?", (action_id,))
        kind = str(row["kind"]) if row else kind
    return Subject(cid, rev, action_id, kind, str(contrib["upstream"]), contrib, patchview.load(derived_dir))


def _json(value: Any) -> dict[str, Any]:
    try:
        doc = json.loads(value) if value else {}
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _crumb(s: Subject, section: int) -> k.Line:
    return (k.Const("🧭 "), k.Bold(k.Const(f"{s.cid} r{s.rev}")), k.Const(f" · {section}/9 {SECTIONS[section - 1]}"))


SectionOut = tuple[list[k.Line], list[k.Element], list[list[k.ButtonSpec]]]


def _section_lines(s: Subject, section: int, page: int) -> SectionOut:
    c = s.contrib
    lines: list[k.Line] = []
    elements: list[k.Element] = []
    buttons: list[list[k.ButtonSpec]] = []
    added = sum(f.added for f in s.files)
    deleted = sum(f.deleted for f in s.files)
    if section == 1:
        lines.append((k.Bold(k.Text(s.upstream, 120)),))
        if c["issue_number"]:
            lines.append((k.Const(f"Issue #{c['issue_number']} "), k.Text(c["issue_title"] or "", 200)))
        lines.append(k.line(f"Change ✅ {len(s.files)} files +{added} −{deleted} · kind {s.kind}"))
        report = _json(c["size_report_json"])
        if report.get("measured_class"):
            lines.append(k.line(f"Size ✅ {report['measured_class']}"))
    elif section == 2:
        lines.append((k.Const("Issue: "), k.Text(c["issue_title"] or "(none)", 300)))
        vet = _json(c["vetting_json"])
        if vet:
            elements.append(k.claim("Vetter", str(vet.get("summary") or vet.get("reason") or json.dumps(vet))[:600],
                                    max_len=600))  # fmt: skip
        plan = _json(c["plan_json"])
        if plan.get("summary"):
            elements.append(k.claim("Planner", str(plan["summary"]), max_len=600))
    elif section == 3:
        facts = db_facts(s)
        lines += [(k.Text(f, 200),) for f in facts] or [k.line("No repo facts recorded.")]
    elif section == 4:
        tags = risk_tags(s.files)
        lines += [(k.Text(t, 200),) for t in tags] or [k.line("✅ No deterministic risk tags.")]
        if c["risk"]:
            elements.append(k.claim("Planner risk", f"{c['risk']} ({c['confidence'] or 0:.2f})"))
    elif section == 5:
        items, page, pages = k.list_view([(k.Const(f"{i + 1}. "), k.Code(k.Text(middle_cut(f.path, 60))),
                                           k.Const(f" +{f.added} −{f.deleted}" + (" binary" if f.binary else "")))
                                          for i, f in enumerate(s.files)], page, FILES_PER_PAGE)  # fmt: skip
        lines += items or [k.line("No files.")]
        start = (page - 1) * FILES_PER_PAGE
        opens = [k.button(f"Open {start + i + 1}", "file_pick", s.arg(index=start + i))
                 for i in range(len(items))]  # fmt: skip
        buttons += [opens[i : i + 3] for i in range(0, len(opens), 3)]
        if pages > 1:
            buttons.append(k.pager("nav", s.arg(view="explorer", section=5), page, pages))
    elif section == 6:
        for i, f in enumerate(s.files[:20]):
            heads = " · ".join(h.header.split("@@")[1].strip() for h in f.hunks[:4] if "@@" in h.header)
            lines.append((k.Const(f"{i + 1}. "), k.Code(k.Text(middle_cut(f.path, 50))), k.Const(" "),
                          k.Text(heads, 160)))  # fmt: skip
        buttons.append([k.button("📄 .patch", "nav", s.arg(view="doc_patch")),
                        k.button("🌐 HTML", "nav", s.arg(view="doc_html"))])  # fmt: skip
    elif section == 7:
        cmds = [x for x in _json(c["validation_json"]).get("commands", []) if isinstance(x, dict)]
        for cmd in cmds[:12]:
            mark = "✅" if cmd.get("exit_code") == 0 else "❌"
            lines.append((k.Const(f"{mark} "), k.Text(str(cmd.get("name", "?")), 60),
                          k.Const(f" · exit {cmd.get('exit_code')}")))  # fmt: skip
            if cmd.get("exit_code") != 0 and cmd.get("tail"):
                tail = "\n".join(str(cmd["tail"]).splitlines()[-40:])
                elements.append(k.Quote(((k.Text(tail, 1500),),), expandable=True))
        if not cmds:
            lines.append(k.line("No local commands (relies on upstream CI)."))
        lines.append(k.line("Upstream CI runs after submit."))
    elif section == 8:
        review = _json(c["review_json"])
        models = _json(c["models_json"])
        if models.get("reviewer") and models.get("coder"):
            same = models["reviewer"] == models["coder"]
            lines.append(k.line(f"Reviewer model {'= coder ⚠️' if same else '≠ coder ✅'}"))
        elements.append(k.claim("Verdict", str(review.get("verdict", "?"))))
        for finding in (review.get("blocking") or [])[:10]:
            issue = finding.get("issue") if isinstance(finding, dict) else finding
            elements.append(k.claim("Blocking", str(issue), max_len=200))
        for nit in (review.get("nits") or [])[:10]:
            elements.append(k.claim("Nit", str(nit), max_len=200))
    elif section == 9:
        title = c["pr_title_override"] if c["pr_title_override"] is not None else c["pr_title"]
        body = c["pr_body_override"] if c["pr_body_override"] is not None else c["pr_body"]
        lines.append((k.Const("Title "), k.Code(k.Verbatim(title or "", "pr_title"))))
        flags = []
        if c["pr_title_override"] is not None:
            flags.append("title edited by you")
        if c["pr_body_override"] is not None:
            flags.append("body edited by you")
        lines.append(k.line(" · ".join(flags) or "not edited"))
        lines.append(k.line("Disclosure ✅" if "AI" in (body or "") else "Disclosure ⚠️ not found"))
        lines.append(k.line("The body parts are the messages under the card."))
        if title:
            buttons.append([k.copy_button("📋 Copy title", title), k.button("📄 pr_body.md", "nav",
                                                                           s.arg(view="doc_body"))])  # fmt: skip
    return lines, elements, buttons


def db_facts(s: Subject) -> list[str]:
    return [f"Repo {s.upstream}"]


def risk_tags(files: list[FilePatch]) -> list[str]:
    tags: list[str] = []
    for f in files:
        marks = [m for m in RISKY if m.lower() in f.path.lower()]
        if f.binary:
            marks.append("binary")
        if f.deleted > 50:
            marks.append(f"deletes {f.deleted} lines")
        if f.added + f.deleted > 400:
            marks.append("> 400 changed lines")
        if marks:
            tags.append(f"⚠️ {middle_cut(f.path, 60)}: {', '.join(marks)}")
    return tags[:15]


def explorer_view(s: Subject, section: int = 1, page: int = 1, *, picker: bool = False) -> k.View:
    section = min(max(1, section), 9)
    lines, elements, extra = _section_lines(s, section, page)
    blocks = [k.Block((k.Lines((_crumb(s, section), *lines)),), name="section")]
    if elements:
        blocks.append(k.Block(tuple(elements), required=False, priority=1, name="details"))
    if picker:
        rows = [[k.button(f"{i}", "nav", s.arg(view="explorer", section=i)) for i in range(j, j + 3)]
                for j in (1, 4, 7)]  # fmt: skip
        rows.append([k.button("↩️ Back", "nav", s.arg(view="explorer", section=section))])
    else:
        rows = [k.pager("nav", s.arg(view="explorer"), section, 9)]
        rows[0][1] = k.button(f"{section}/9 ▾", "nav", s.arg(view="explorer_pick", section=section))
        rows += extra[:2]
        rows.append([k.button("🧾 Files", "nav", s.arg(view="explorer", section=5)),
                     k.button("🧪 Tests", "nav", s.arg(view="explorer", section=7)),
                     k.button("🤖 Review", "nav", s.arg(view="explorer", section=8))])  # fmt: skip
        if s.action_id is not None:
            rows.append([k.button("💬 Comment", "comment_hunk", s.arg(path=None)),
                         k.button("⬇️ Show card", "show_card", s.arg())])  # fmt: skip
    view = k.View(kind="explorer", blocks=blocks, rows=rows[:6], cid=s.cid, rev=s.rev, action_id=s.action_id,
                  page=section, state={"section": section, "page": page})  # fmt: skip  # rich views: M3 (item 50)
    view.max_rows = 6
    return view


# ----------------------------------------------------------------------------- S3 file view
def file_view(s: Subject, index: int, page: int = 1, *, seq: int = 0, picker_page: int | None = None,
              hunk_picker: int | None = None) -> k.View:  # fmt: skip
    if not s.files:
        return k.View(kind="file", blocks=[k.Block((k.Lines((k.line("🧾 No files in this revision."),)),))],
                      cid=s.cid, rev=s.rev, action_id=s.action_id)  # fmt: skip
    index = min(max(0, index), len(s.files) - 1)
    fp = s.files[index]
    pages = patchview.pages(fp)
    page = min(max(1, page), len(pages))
    shown = pages[page - 1]
    head = (k.Const("🧾 "), k.Code(k.Text(middle_cut(fp.path, 80))),
            k.Const(f" · file {index + 1}/{len(s.files)} · +{fp.added} −{fp.deleted}"
                    + (f" · page {page}/{len(pages)}" if len(pages) > 1 else "")))  # fmt: skip
    blocks = [
        k.Block((k.Lines((head,)),), name="head"),
        k.Block((k.PreBlock(shown.text, language="diff"),), name="diff"),
        k.Block((k.FooterEl(k.line(f"r{s.rev} · derived by gatekeeper · {HINT}")),), required=False, name="hint"),
    ]
    rows: k.Rows
    if picker_page is not None:
        names, picker_page, n_pages = k.list_view([(k.Const(str(i)),) for i in range(len(s.files))], picker_page,
                                                  PICKER_PER_PAGE)  # fmt: skip
        start = (picker_page - 1) * PICKER_PER_PAGE
        btns = [k.button(f"{start + i + 1} {middle_cut(s.files[start + i].path.rsplit('/', 1)[-1], 18)}",
                         "file_pick", s.arg(index=start + i)) for i in range(len(names))]  # fmt: skip
        rows = [btns[i : i + 2] for i in range(0, len(btns), 2)]
        if n_pages > 1:
            rows.append(k.pager("nav", s.arg(view="file_picker", index=index), picker_page, n_pages))
        rows.append([k.button("↩️ Back", "file_pick", s.arg(index=index))])
    elif hunk_picker is not None:
        heads, hunk_picker, n_pages = k.list_view([(k.Const(h.header),) for h in fp.hunks], hunk_picker,
                                                  HUNKS_PER_PAGE)  # fmt: skip
        start = (hunk_picker - 1) * HUNKS_PER_PAGE
        btns = [k.button(f"H{start + i + 1}", "comment_line_pick", s.arg(path=fp.path, hunk=start + i))
                for i in range(len(heads))]  # fmt: skip
        rows = [btns[i : i + 3] for i in range(0, len(btns), 3)]
        rows.append([k.button("💬 Whole file", "comment_line_pick", s.arg(path=fp.path)),
                     k.button("↩️ Back", "file_pick", s.arg(index=index))])  # fmt: skip
    else:
        nav = [k.button("◀ Prev file", "file_pick", s.arg(index=index - 1)) if index > 0
               else k.disabled_button("◀ Prev file"),
               k.button(f"📂 {index + 1}/{len(s.files)}", "nav", s.arg(view="file_picker", index=index)),
               k.button("Next file ▶", "file_pick", s.arg(index=index + 1)) if index + 1 < len(s.files)
               else k.disabled_button("Next file ▶")]  # fmt: skip
        rows = [nav]
        if len(pages) > 1:
            rows.append(k.pager("nav", s.arg(view="file", index=index), page, len(pages)))
        tools = [k.button("📄 .patch", "nav", s.arg(view="doc_patch")), k.button("🌐 HTML", "nav",
                                                                                s.arg(view="doc_html"))]  # fmt: skip
        if s.action_id is not None:
            tools.insert(0, k.button("💬 Comment", "nav", s.arg(view="hunk_picker", index=index)))
        rows.append(tools)
    state = {"seq": seq, "index": index, "page": page, "lines": [list(x) for x in shown.lines]}
    view = k.View(kind="file", blocks=blocks, rows=rows, cid=s.cid, rev=s.rev, action_id=s.action_id,
                  path=fp.path, page=page, state=state, rich=False)  # fmt: skip
    return view
