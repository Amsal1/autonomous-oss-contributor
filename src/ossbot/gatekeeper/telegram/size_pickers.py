"""``/size`` and ``/filters`` (DESIGN §14.14.4; mockup 11; PLAN item 46): ``tune``-class pickers, one message
each, edited in place, reading and writing only through ``gatekeeper/discovery_prefs.py``.

Autonomous discovery only: 👤 ``/work`` ignores both. ``xxl`` is never offered and the hard ceiling is shown
read-only (U13): no button or typed value can raise it. Each change applies at once and arms
``[↩️ Undo]`` for 60 s (``DiscoveryPrefs.undo``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.config import DiscoveryFilters, resolve_target
from ossbot.common.schemas import PrefsUpdate
from ossbot.common.size import CLASS_BOUNDS, SelectableSizeClass
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.discovery_prefs import UNDO_SECONDS, PrefsRefused
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply, command
from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.proposal_cards import KIND_NAMES
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.limits import middle_cut

CLASSES: tuple[SelectableSizeClass, ...] = tuple(CLASS_BOUNDS)  # xs..xl: xxl is never selectable
REPOS_PER_PAGE = 8
LABELS = ("good first issue", "help wanted", "bug", "documentation")
TIERS = (("T0", "T0"), ("T1", "T0–T1"), ("T2", "T0–T2"))
AGES = ((30, "Age 30 d"), (90, "Age 90 d"), (180, "Age 180 d"), (365, "Age 365 d"), (36500, "Age any"))
ACTIVE = ((36500, "Active any"), (30, "Active 30 d"), (90, "Active 90 d"))
LANGS = ("Python", "TypeScript", "JavaScript", "Go", "Rust", "Java")
OPEN_PRS = ((1, "1 open PR"), (2, "2 open PRs"), (3, "3 open PRs"))
PAGES = ("Kinds", "Labels", "Age", "PRs")
XXL = "xxl can't be a preference; user-directed work has no size limit below the ceiling"


class SizeArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: Literal["main", "preferred", "repos", "repo"] = "main"
    cls: Literal["xs", "s", "m", "l", "xl", "inherit"] | None = None
    repo: str | None = Field(default=None, max_length=140)
    page: int = Field(default=1, ge=1, le=100)
    undo: str | None = Field(default=None, max_length=40)


class FilterArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(default=1, ge=1, le=4)
    key: str | None = Field(default=None, max_length=40)
    value: str | None = Field(default=None, max_length=40)
    undo: str | None = Field(default=None, max_length=40)


def enabled_targets(ctx: AppContext) -> list[str]:
    out: list[str] = []
    for category in ctx.config.targets.categories:
        for entry in category.repos:
            target = resolve_target(ctx.config.targets, entry.repo)
            if target is not None and target.enabled:
                out.append(target.repo)
    return list(dict.fromkeys(out))


def _bounds(cls: str) -> str:
    lines, files = CLASS_BOUNDS[cls]  # type: ignore[index]
    return f"≤ {lines:,} code lines · ≤ {files} files"


def _undo_row(purpose: str, base: dict[str, Any], undo: str | None) -> list[k.ButtonSpec]:
    return [k.button("↩️ Undo", purpose, {**base, "undo": undo}, single_use=True)] if undo else []


# ------------------------------------------------------------------------------------ /size
def size_view(ctx: AppContext, view: str = "main", *, repo: str | None = None, page: int = 1,
              undo: str | None = None) -> k.View:  # fmt: skip
    prefs = ctx.discovery_prefs
    eff = prefs.effective()
    lines: list[k.Line] = [(k.Const("📏 "), k.Bold(k.Const("Size preference")),
                            k.Const(" · autonomous discovery only")),
                           (k.Const("Bot proposes up to "), k.Bold(k.Const(eff.autonomous_max_class)),
                            k.Const(f" · {_bounds(eff.autonomous_max_class)}")),
                           k.line(f"Preferred: {', '.join(eff.preferred_classes) or '—'} · test lines never count"),
                           k.line("👤 Your /work requests ignore this.")]  # fmt: skip
    overrides = [(str(r["scope"]), str(r["autonomous_max_class"])) for r in prefs.rows()
                 if r["scope"] != "*" and r["autonomous_max_class"]]  # fmt: skip
    if overrides:
        lines.append((k.Const("Per repo: "), k.Text(" · ".join(f"{s} ≤ {c}" for s, c in overrides[:4]), 200)))
    c = ctx.config.policy.diff
    lines.append(k.line(f"Ceiling (config only): {c.max_files} files · {c.max_changed_lines:,} code ·"
                        f" {c.max_test_lines:,} test"))  # fmt: skip
    lines.append(k.line(f"Overrun: {ctx.config.policy.size.on_overrun} on the PR card (policy.size.on_overrun)"))
    rows: k.Rows
    if view == "preferred":
        on = set(eff.preferred_classes)
        btns = [k.button(f"{'✅' if cl in on else '⬜'} {cl}", "size_set", {"view": "preferred", "cls": cl})
                for cl in CLASSES]  # fmt: skip
        rows = [btns[:3], btns[3:], [k.button("↩️ Back", "size_set", {"view": "main"})]]
    elif view == "repos":
        targets = enabled_targets(ctx)
        with_override = [r for r, _ in overrides]
        ordered = [r for r in with_override if r in targets] + [r for r in targets if r not in with_override]
        repos, page, pages = k.list_view([(k.Const(r),) for r in ordered], page, REPOS_PER_PAGE)
        start = (page - 1) * REPOS_PER_PAGE
        btns = [k.button(middle_cut(ordered[start + i], 24), "size_set", {"view": "repo", "repo": ordered[start + i]})
                for i in range(len(repos))]  # fmt: skip
        rows = [btns[i : i + 2] for i in range(0, len(btns), 2)][:4]
        if pages > 1:
            rows.append(k.pager("size_set", {"view": "repos"}, page, pages))
        rows.append([k.button("↩️ Back", "size_set", {"view": "main"})])
    elif view == "repo" and repo is not None:
        inherit = prefs.effective().autonomous_max_class
        cur = prefs.effective(repo).autonomous_max_class
        lines.append((k.Const("Repo "), k.Bold(k.Text(repo, 140)), k.Const(f" · now ≤ {cur}")))
        btns = [k.button(f"Inherit ({inherit})", "size_set", {"view": "repo", "repo": repo, "cls": "inherit"})]
        btns += [k.button(("✅ " if cl == cur else "") + cl, "size_set", {"view": "repo", "repo": repo, "cls": cl})
                 for cl in CLASSES]  # fmt: skip
        rows = [btns[:3], btns[3:], [k.button("↩️ Back", "size_set", {"view": "repos"})]]
    else:
        btns = [k.button(("✅ " if cl == eff.autonomous_max_class else "") + f"{cl} ≤{CLASS_BOUNDS[cl][0]}",
                         "size_set", {"view": "main", "cls": cl}) for cl in CLASSES]  # fmt: skip
        rows = [btns[:3], btns[3:], [k.button("⭐ Preferred…", "size_set", {"view": "preferred"}),
                                     k.button("🏷 Per repo…", "size_set", {"view": "repos"})]]  # fmt: skip
    undo_row = _undo_row("size_set", {"view": view, **({"repo": repo} if repo else {})}, undo)
    if undo_row:
        rows.append(undo_row)
    return k.View(kind="picker", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows[:6],
                  state={"picker": "size", "view": view})  # fmt: skip


def _change(ctx: AppContext, req: PrefsUpdate, actor: str) -> tuple[str | None, str]:
    try:
        out = ctx.discovery_prefs.update(req, actor)
    except PrefsRefused as exc:
        return None, f"Refused: {exc.code}"
    return str(out["undo_id"]), ""


@command("size", help="Autonomous size preference (xs…xl; /work ignores it)", safety="tune")
async def size_cmd(ctx: CommandContext, args: list[str]) -> Reply:
    if args:
        cls = args[0].lower()
        if cls == "xxl":
            return Reply(esc(XXL))
        if cls not in CLASSES:
            return Reply(esc("Usage: /size [xs|s|m|l|xl]"))
        undo, error = _change(ctx.app, PrefsUpdate(autonomous_max_class=cls), ctx.actor)
        if undo is None:
            return Reply(esc(error))
        return Reply("", view=size_view(ctx.app, undo=undo))
    return Reply("", view=size_view(ctx.app))


@action("size_set", arg=SizeArg)
async def size_set(tap: Tap) -> str:
    a: SizeArg = tap.arg
    ctx = tap.app
    undo: str | None = None
    toast = ""
    if a.undo:
        try:
            ctx.discovery_prefs.undo(a.undo, tap.actor)
        except PrefsRefused as exc:
            return f"Too late to undo ({exc.code})." if exc.code == "too_late" else f"Refused: {exc.code}"
        toast = "↩️ Restored."
    elif a.cls is not None and a.view == "main":
        undo, error = _change(ctx, PrefsUpdate(autonomous_max_class=a.cls), tap.actor)
        if undo is None:
            return error
        toast = f"Auto size ≤ {a.cls}. Proposals already sent keep their size."
    elif a.cls is not None and a.view == "preferred":
        on = list(ctx.discovery_prefs.effective().preferred_classes)
        new = [c for c in CLASSES if (c in on) != (c == a.cls)]
        if not new:
            return "Keep at least one preferred class"
        undo, error = _change(ctx, PrefsUpdate(preferred_classes=new), tap.actor)
        if undo is None:
            return error
    elif a.cls is not None and a.view == "repo" and a.repo:
        if resolve_target(ctx.config.targets, a.repo) is None:
            return "Not a target repo."
        undo, error = _change(ctx, PrefsUpdate(scope=a.repo, autonomous_max_class=a.cls), tap.actor)
        if undo is None:
            return error
        toast = f"{a.repo}: {'inherits' if a.cls == 'inherit' else '≤ ' + a.cls}."
    ui_send.edit(ctx, size_view(ctx, a.view, repo=a.repo, page=a.page, undo=undo), message_id=tap.message_id)
    return toast


# --------------------------------------------------------------------------------- /filters
def _all_kinds() -> list[str]:
    defaults = DiscoveryFilters()
    return list(dict.fromkeys([*defaults.allowed_kinds, *defaults.excluded_kinds]))


def filters_view(ctx: AppContext, page: int = 1, *, undo: str | None = None) -> k.View:
    f = ctx.discovery_prefs.effective().filters
    page = (page - 1) % 4 + 1
    lines: list[k.Line] = [(k.Const("🧰 "), k.Bold(k.Const("Discovery filters")),
                            k.Const(f" · autonomous only · {page}/4 {PAGES[page - 1]}"))]  # fmt: skip
    rows: k.Rows = []
    if page == 1:
        lines.append(k.line("✅ = the bot may propose this kind. 👤 /work ignores filters."))
        allowed = set(f.allowed_kinds)
        btns = [k.button(f"{'✅' if kind in allowed else '⬜'} {KIND_NAMES.get(kind, kind)[:16]}", "filters_set",
                         {"page": 1, "key": "kind", "value": kind}) for kind in _all_kinds()]  # fmt: skip
        rows = [btns[i : i + 2] for i in range(0, len(btns), 2)][:5]
    elif page == 2:
        lines.append(k.line("Labels: an issue needs one of the ✅ labels. Tier: the highest validation tier."))
        req = set(f.require_labels_any)
        btns = [k.button(f"{'✅' if lab in req else '⬜'} {lab}", "filters_set",
                         {"page": 2, "key": "label", "value": lab}) for lab in LABELS]  # fmt: skip
        rows = [btns[:2], btns[2:], [k.button(f"{'✅' if not req else '⬜'} Any label (no label required)",
                                              "filters_set", {"page": 2, "key": "label", "value": "*"})],
                [k.button(("✅ " if f.validation_tier_max == t else "") + label, "filters_set",
                          {"page": 2, "key": "tier", "value": t}) for t, label in TIERS]]  # fmt: skip
    elif page == 3:
        lines.append(k.line("Age = max issue age · Active = maintainer activity within"))
        ages = [k.button(("✅ " if f.max_issue_age_days == d else "") + label, "filters_set",
                         {"page": 3, "key": "age", "value": str(d)}) for d, label in AGES]  # fmt: skip
        act = [k.button(("✅ " if f.min_maintainer_activity_days == d else "") + label, "filters_set",
                        {"page": 3, "key": "active", "value": str(d)}) for d, label in ACTIVE]  # fmt: skip
        rows = [ages[:3], ages[3:], act[:2], act[2:]]
    else:
        on = set(f.languages)
        lines.append(k.line("Languages: none ✅ = any language."))
        btns = [k.button(f"{'✅' if lang in on else '⬜'} {middle_cut(lang, 22)}", "filters_set",
                         {"page": 4, "key": "lang", "value": lang}) for lang in LANGS]  # fmt: skip
        rows = [btns[i : i + 2] for i in range(0, len(btns), 2)]
        extra = sorted(on - set(LANGS))
        if extra:
            lines.append(k.line(f"+{len(extra)} more in config"))
        cap = ctx.config.policy.limits.open_prs_per_repo
        lines.append(k.line(f"PRs: skip a repo with this many of our PRs open (hard limit {cap}, policy.yaml)."))
        rows.append([k.button(("✅ " if f.max_open_prs_by_me_per_repo == n else "") + label, "filters_set",
                              {"page": 4, "key": "prs", "value": str(n)}) for n, label in OPEN_PRS])  # fmt: skip
    prev_name, next_name = PAGES[(page - 2) % 4], PAGES[page % 4]
    pager = [k.button(f"◀ {prev_name}", "filters_set", {"page": (page - 2) % 4 + 1})]
    pager += _undo_row("filters_set", {"page": page}, undo) or []
    pager.append(k.button(f"{next_name} ▶", "filters_set", {"page": page % 4 + 1}))
    rows.append(pager)
    return k.View(kind="picker", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows[:6],
                  state={"picker": "filters", "page": page})  # fmt: skip


def _toggle(items: list[str], value: str) -> list[str]:
    return [x for x in items if x != value] if value in items else [*items, value]


def filter_change(ctx: AppContext, key: str, value: str) -> dict[str, Any] | None:
    f = ctx.discovery_prefs.effective().filters
    if key == "kind" and value in _all_kinds():
        allowed = _toggle(list(f.allowed_kinds), value)
        return {"allowed_kinds": allowed, "excluded_kinds": [x for x in _all_kinds() if x not in allowed]}
    if key == "label" and (value in LABELS or value == "*"):
        return {"require_labels_any": [] if value == "*" else _toggle(list(f.require_labels_any), value)}
    if key == "tier" and value in {t for t, _ in TIERS}:
        return {"validation_tier_max": value}
    if key == "age" and value in {str(d) for d, _ in AGES}:
        return {"max_issue_age_days": int(value)}
    if key == "active" and value in {str(d) for d, _ in ACTIVE}:
        return {"min_maintainer_activity_days": int(value)}
    if key == "prs" and value in {str(n) for n, _ in OPEN_PRS}:
        return {"max_open_prs_by_me_per_repo": int(value)}
    if key == "lang" and value in LANGS:
        return {"languages": _toggle(list(f.languages), value)}
    return None


@command("filters", help="Autonomous discovery filters (4 pages; /work ignores them)", safety="tune")
async def filters_cmd(ctx: CommandContext, args: list[str]) -> Reply:
    return Reply("", view=filters_view(ctx.app))


@action("filters_set", arg=FilterArg)
async def filters_set(tap: Tap) -> str:
    a: FilterArg = tap.arg
    ctx = tap.app
    undo: str | None = None
    toast = ""
    if a.undo:
        try:
            ctx.discovery_prefs.undo(a.undo, tap.actor)
        except PrefsRefused as exc:
            return f"Too late to undo ({exc.code})." if exc.code == "too_late" else f"Refused: {exc.code}"
        toast = "↩️ Restored."
    elif a.key is not None and a.value is not None:
        change = filter_change(ctx, a.key, a.value)
        if change is None:
            return "Unknown option."
        undo, error = _change(ctx, PrefsUpdate(filters=change), tap.actor)
        if undo is None:
            return error
        toast = f"Saved. Undo within {UNDO_SECONDS} s."
    ui_send.edit(ctx, filters_view(ctx, a.page, undo=undo), message_id=tap.message_id)
    return toast
