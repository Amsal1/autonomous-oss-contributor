"""Config commands (DESIGN §14.14.1-2, §11.5, §15.5, §16, §7.9; PLAN item 47): ``/egress``, ``/hermes tier``,
``/model`` (+ the new-model card of §16.3), ``/reload`` and ``/repos``.

Every change is a picker confirm: the picker tap edits its message into the confirm panel, and only the
single-use, message-bound, 5-minute confirm token acts. Effects that reach root helpers or files beyond
SQLite (the egress re-render, the tier switch and its surface probe, the ``models.yaml`` writer, the
reload) are ``io`` confirms, spawned as ``command`` tasks (§14.18.5). Nothing here touches either gate,
the hard ceiling or dry-run.
"""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.clock import to_iso
from ossbot.common.config import ConfigError, resolve_target
from ossbot.common.control_flags import EGRESS_MODES, HERMES_TIERS, HermesTier
from ossbot.common.schemas import PrefsUpdate
from ossbot.gatekeeper import db
from ossbot.gatekeeper.chat.hermes_surface import allowed_toolsets
from ossbot.gatekeeper.config_snapshot import ReloadConflict
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.discovery_prefs import PrefsRefused
from ossbot.gatekeeper.models_admin import ModelSetError
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.common import confirm_button, confirm_panel, picker_rows
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Confirm, Reply, command, confirm_handler
from ossbot.gatekeeper.telegram.escape import b, esc
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import redact as ui_redact
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.limits import middle_cut

MODEL_ROLES = ("chat", "vision", "planner", "coder", "reviewer", "summarizer")  # the /model role picker
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODELS_PER_PAGE = 8
REPOS_PER_PAGE = 8
DIFF_MAX = 2500


# ----------------------------------------------------------------------------------- /egress
class ModeArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["allowlist", "research", "open"]


def _egress_now(app: AppContext) -> str:
    return app.control.egress_mode(app.config.egress.mode_default)


def egress_picker(app: AppContext) -> k.View:
    cur = _egress_now(app)
    opts = [
        k.button(m + (" ✅" if m == cur else ""), "egress_pick", {"mode": m}, single_use=True) for m in EGRESS_MODES
    ]
    return k.plain("picker", (k.Const("🌐 "), k.Bold(k.Const("Egress mode")), k.Const(f" · now {cur}")),
                   k.line("Applies to research, Hermes, agent LLM and catalog traffic; fetch and validate never"
                          " change."), rows=picker_rows(opts))  # fmt: skip


def egress_changes(app: AppContext, cur: str, new: str) -> list[str]:
    """One line per grantee whose categories differ between the two modes (§11.5)."""
    modes: dict[Any, Any] = dict(app.config.egress.modes)
    out = []
    for grantee in sorted(set(modes.get(cur, {})) | set(modes.get(new, {}))):
        before, after = set(modes.get(cur, {}).get(grantee, [])), set(modes.get(new, {}).get(grantee, []))
        added, removed = sorted(after - before), sorted(before - after)
        if added or removed:
            out.append(f"{grantee}: " + " ".join([*(f"+{c}" for c in added), *(f"−{c}" for c in removed)]))
    return out


def egress_confirm_view(app: AppContext, mode: str) -> k.View:
    cur = _egress_now(app)
    widens = EGRESS_MODES.index(mode) > EGRESS_MODES.index(cur)
    lines = [k.line(f"🌐 Egress mode {cur} → {mode}. Root re-renders the proxy; fetch and validate are unchanged.")]
    lines += [k.line(f"• {c}") for c in egress_changes(app, cur, mode)[:8]] or [k.line("No category changes.")]
    return confirm_panel(lines, confirm_button(f"✅ Switch to {mode}", "egress_confirm", mode,
                                               style="danger" if widens else "primary"))  # fmt: skip


@command("egress", help="Egress mode allowlist / research / open (asks for confirmation)", safety="config")
async def egress(ctx: CommandContext, args: list[str]) -> Reply:
    if args and args[0] in EGRESS_MODES and args[0] != _egress_now(ctx.app):
        return Reply("", view=egress_confirm_view(ctx.app, args[0]))
    return Reply("", view=egress_picker(ctx.app))


@action("egress_pick", arg=ModeArg)
async def egress_pick(tap: Tap) -> str:
    if tap.arg.mode == _egress_now(tap.app):
        ui_send.edit(tap.app, egress_picker(tap.app), message_id=tap.message_id)
        return f"Already {tap.arg.mode}."
    ui_send.edit(tap.app, egress_confirm_view(tap.app, tap.arg.mode), message_id=tap.message_id)
    return ""


@confirm_handler("egress_confirm", effect="io")
async def egress_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    if arg not in EGRESS_MODES:
        return Reply("This button is no longer valid.")
    previous = ctx.app.control.set_egress_mode(arg, ctx.actor, ctx.app.config.egress.mode_default)
    return Reply(f"🌐 {b('Egress mode')} {esc(previous)} → {b(arg)}. Root applies it to the proxy now.")


# ----------------------------------------------------------------------------------- /hermes
class TierArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: Literal["locked", "research", "self_improve"]


def _tier_now(app: AppContext) -> str:
    return app.control.hermes_tier(app.config.policy.hermes.capability_tier_default)


def tier_picker(app: AppContext) -> k.View:
    cur = _tier_now(app)
    opts = [k.button(t + (" ✅" if t == cur else ""), "hermes_tier_pick", {"tier": t}, single_use=True)
            for t in HERMES_TIERS]  # fmt: skip
    return k.plain("picker", (k.Const("🧠 "), k.Bold(k.Const("Hermes tier")), k.Const(f" · now {cur}")),
                   k.line("Toolsets: " + (", ".join(allowed_toolsets(cast(HermesTier, cur))) or "none")),
                   rows=picker_rows(opts))  # fmt: skip


def tier_confirm_view(app: AppContext, tier: str) -> k.View:
    cur = _tier_now(app)
    before, after = set(allowed_toolsets(cast(HermesTier, cur))), set(allowed_toolsets(cast(HermesTier, tier)))
    widens = HERMES_TIERS.index(tier) > HERMES_TIERS.index(cur)
    diff = " ".join([*(f"+{t}" for t in sorted(after - before)), *(f"−{t}" for t in sorted(before - after))])
    lines = [k.line(f"🧠 Hermes tier {cur} → {tier}. Open skill cards are invalidated; the surface probe re-runs."),
             k.line(f"Toolsets: {diff or 'no change'}")]  # fmt: skip
    return confirm_panel(lines, confirm_button(f"✅ Switch to {tier}", "hermes_tier_confirm", tier,
                                               style="danger" if widens else "primary"))  # fmt: skip


@command("hermes", help="Hermes capability tier: /hermes tier (asks for confirmation)", safety="config")
async def hermes(ctx: CommandContext, args: list[str]) -> Reply:
    if len(args) >= 2 and args[0] == "tier" and args[1] in HERMES_TIERS and args[1] != _tier_now(ctx.app):
        return Reply("", view=tier_confirm_view(ctx.app, args[1]))
    return Reply("", view=tier_picker(ctx.app))


@action("hermes_tier_pick", arg=TierArg)
async def hermes_tier_pick(tap: Tap) -> str:
    if tap.arg.tier == _tier_now(tap.app):
        ui_send.edit(tap.app, tier_picker(tap.app), message_id=tap.message_id)
        return f"Already {tap.arg.tier}."
    ui_send.edit(tap.app, tier_confirm_view(tap.app, tap.arg.tier), message_id=tap.message_id)
    return ""


@confirm_handler("hermes_tier_confirm", effect="io")
async def hermes_tier_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    if arg not in HERMES_TIERS:
        return Reply("This button is no longer valid.")
    app = ctx.app
    previous, invalidated = app.control.set_hermes_tier(arg, ctx.actor,
                                                        app.config.policy.hermes.capability_tier_default)  # fmt: skip
    probe = "not run (assistant off)"
    if app.chat is not None:  # the surface probe re-runs for the new tier (§15.5)
        app.chat.gate.invalidate()
        result = await app.chat.gate.check()
        probe = "ok" if result.open else result.reason
    return Reply(f"🧠 {b('Hermes tier')} {esc(previous)} → {b(arg)} · {len(invalidated)} skill card(s) invalidated"
                 f" · surface probe {esc(probe)}")  # fmt: skip


# ------------------------------------------------------------------------------------ /model
class ModelArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: Literal["roles", "role", "model", "effort"] = "roles"
    role: str | None = Field(default=None, max_length=20)
    model: str | None = Field(default=None, max_length=128)
    effort: str | None = Field(default=None, max_length=10)
    page: int = Field(default=1, ge=1, le=100)


def current_model(app: AppContext, role: str) -> tuple[str, str | None]:
    cfg = app.models.read()
    node = cfg.default if role == "default" else getattr(cfg.roles, role)
    return str(node.model), node.effort


def role_picker(app: AppContext, model: str | None = None) -> k.View:
    roles = MODEL_ROLES if model is None else ("default", *MODEL_ROLES)
    step = "role" if model is None else "model"
    opts = [k.button(r, "model_pick", {"step": step, "role": r, **({"model": model} if model else {})})
            for r in roles]  # fmt: skip
    head = "Which role?" if model is None else f"Use {model} for which role?"
    return k.plain("picker", (k.Const("🧠 "), k.Bold(k.Const("Model")), k.Const(" · "), k.Text(head, 120)),
                   rows=picker_rows(opts))  # fmt: skip


def catalog_view(app: AppContext, role: str, page: int) -> k.View:
    cur, _ = current_model(app, role)
    ids = [str(e["model_id"]) for e in app.models.catalog()]
    items, page, pages = k.list_view([(k.Text(i),) for i in ids], page, MODELS_PER_PAGE)
    start = (page - 1) * MODELS_PER_PAGE
    btns = [k.button(("✅ " if ids[start + n] == cur else "") + middle_cut(ids[start + n], 22), "model_pick",
                     {"step": "model", "role": role, "model": ids[start + n]}) for n in range(len(items))]  # fmt: skip
    rows = [btns[i : i + 2] for i in range(0, len(btns), 2)][:4]
    if pages > 1:
        rows.append(k.pager("model_pick", {"step": "role", "role": role}, page, pages))
    rows.append([k.button("↩️ Back", "model_pick", {"step": "roles"})])
    lines: list[k.Line] = [(k.Const("🧠 "), k.Bold(k.Const(f"Model for {role}")), k.Const(" · now "), k.Text(cur, 80))]
    if not ids:
        lines.append(k.line("The Kiro catalog is empty; the runner refreshes it (ossctl models refresh)."))
    return k.View(kind="picker", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows[:6])


def effort_view(role: str, model: str) -> k.View:
    opts = [k.button(e, "model_pick", {"step": "effort", "role": role, "model": model, "effort": e}) for e in EFFORTS]
    opts.append(k.button("Keep effort", "model_pick", {"step": "effort", "role": role, "model": model}))
    return k.plain("picker", (k.Const("🧠 "), k.Text(f"{role} → {model}", 120), k.Const(" · effort?")),
                   rows=picker_rows(opts))  # fmt: skip


def model_confirm_view(app: AppContext, role: str, model: str, effort: str | None) -> k.View:
    cur, cur_effort = current_model(app, role)
    show = f"{model} ({effort or cur_effort or 'default'})"
    lines = [(k.Const(f"🧠 Model: {role} "), k.Text(f"{cur} ({cur_effort or 'default'})", 100), k.Const(" → "),
              k.Text(show, 100)),
             k.line("Writes config/models.yaml (audited); stages and chat turns use it from their next"
                    " start.")]  # fmt: skip
    arg = json.dumps({"role": role, "model": model, "effort": effort}, sort_keys=True)
    return confirm_panel(lines, confirm_button("✅ Switch model", "model_confirm", arg))


@command("model", help="Switch the model of a role (asks for confirmation)", safety="config")
async def model(ctx: CommandContext, args: list[str]) -> Reply:
    if args and args[0] in MODEL_ROLES:
        return Reply("", view=catalog_view(ctx.app, args[0], 1))
    return Reply("", view=role_picker(ctx.app))


@action("model_pick", arg=ModelArg)
async def model_pick(tap: Tap) -> str:
    a: ModelArg = tap.arg
    roles = ("default", *MODEL_ROLES)
    if a.role is not None and a.role not in roles:
        return "This button is no longer valid."
    if a.step == "roles" or a.role is None:
        view = role_picker(tap.app, a.model)
    elif a.step == "role":
        view = catalog_view(tap.app, a.role, a.page)
    elif a.step == "model" and a.model:
        view = effort_view(a.role, a.model)
    elif a.step == "effort" and a.model:
        if a.effort is not None and a.effort not in EFFORTS:
            return "This button is no longer valid."
        view = model_confirm_view(tap.app, a.role, a.model, a.effort)
    else:
        return "This button is no longer valid."
    ui_send.edit(tap.app, view, message_id=tap.message_id)
    return ""


@confirm_handler("model_confirm", effect="io")
async def model_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    try:
        req = json.loads(arg or "")
        role, model_id, effort = str(req["role"]), str(req["model"]), req.get("effort")
    except (ValueError, KeyError, TypeError):
        return Reply("This button is no longer valid.")
    try:
        diff = ctx.app.models.set_model(role, model_id, effort, False, ctx.actor)
    except ModelSetError as exc:
        return Reply(f"Refused: {esc(str(exc)[:200])}")
    return Reply(f"🧠 {b('Model')} {esc(role)} → {esc(model_id)}" + ("" if diff else " (already set)"))


# §16.3 new-model card: [Set as default] [Set for role…] [Ignore] (config, confirm)
class NewModelArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=128)
    op: Literal["default", "role", "ignore"]


def new_model_card(app: AppContext, model_id: str, name: str | None) -> bool:
    """Called by ``ModelsAdmin`` for each new catalog id; True = the card replaces the plain FYI."""
    head: list[k.Span] = [k.Const("🧠 "), k.Bold(k.Const("New Kiro model: ")), k.Text(model_id, 128)]
    if name and name != model_id:
        head += [k.Const(" ("), k.Text(name, 100), k.Const(")")]
    rows = [[k.button("Set as default", "model_new", {"model": model_id, "op": "default"}),
             k.button("Set for role…", "model_new", {"model": model_id, "op": "role"})],
            [k.button("Ignore", "model_new", {"model": model_id, "op": "ignore"})]]  # fmt: skip
    ui_send.deliver(app, k.plain("fyi", tuple(head), rows=rows), thread="feed", category="stages")
    return True


@action("model_new", arg=NewModelArg)
async def model_new(tap: Tap) -> str:
    a: NewModelArg = tap.arg
    if a.op == "ignore":
        ui_send.edit(
            tap.app, k.plain("fyi", (k.Const("🧠 Ignored: "), k.Text(a.model, 128))), message_id=tap.message_id
        )
        return "Ignored."
    view = model_confirm_view(tap.app, "default", a.model, None) if a.op == "default" else role_picker(tap.app, a.model)
    ui_send.edit(tap.app, view, message_id=tap.message_id)
    return ""


# ----------------------------------------------------------------------------------- /reload
@command("reload", help="Reload the config files: see the changes, then confirm", safety="go", effect="io")
async def reload(ctx: CommandContext, args: list[str]) -> Reply:
    try:
        preview = ctx.app.snapshots.prepare_reload()
    except ConfigError as exc:
        return Reply(f"❌ {b('Config invalid')}; nothing changed.\n<pre>{esc(str(exc)[:1500])}</pre>")
    if not preview.changed_files:
        return Reply("Nothing to reload: the files match the running snapshot.")
    diff = ui_redact.redact(preview.diff, surface="reload_diff")[0]
    cut = diff if len(diff) <= DIFF_MAX else diff[:DIFF_MAX] + "\n…"
    return Reply(
        f"⚙️ {b('Reload')} {esc(', '.join(preview.changed_files))} (snapshot {esc(preview.hash[:12])})\n"
        f"<pre>{esc(cut)}</pre>\nEvery open approval, proposal and skill card is invalidated and re-issued.",
        Confirm("reload_confirm", "✅ Reload", preview.hash, style="danger"),
    )


@confirm_handler("reload_confirm", effect="io")
async def reload_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    try:
        snap, invalidated = ctx.app.snapshots.apply_reload(arg or "", ctx.actor)
    except (ConfigError, ReloadConflict) as exc:
        return Reply(f"Not reloaded: {esc(str(exc)[:300])}")
    ctx.app.reissue_cards(invalidated, ctx.actor)
    return Reply(f"⚙️ {b('Reloaded')} · snapshot {esc(snap.hash[:12])} · {len(invalidated)} card(s) re-issued")


# ------------------------------------------------------------------------------------ /repos
class RepoArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str | None = Field(default=None, max_length=140)
    page: int = Field(default=1, ge=1, le=100)
    filter: str | None = Field(default=None, max_length=40)
    op: Literal["view", "enable", "disable", "allow_ai", "revoke_ai", "unnever"] = "view"


def all_targets(app: AppContext) -> list[str]:
    out: list[str] = []
    for category in app.config.targets.categories:
        for entry in category.repos:
            target = resolve_target(app.config.targets, entry.repo)
            if target is not None:
                out.append(target.repo)
    return list(dict.fromkeys(out))


def _override(app: AppContext, repo: str) -> Any:
    return db.one(app.conn, "SELECT * FROM repo_overrides WHERE lower(upstream) = lower(?)", (repo,))


def _enabled(app: AppContext, repo: str) -> bool:
    row = _override(app, repo)
    if row is not None and row["enabled"] is not None:
        return bool(row["enabled"])
    target = resolve_target(app.config.targets, repo)
    return bool(target is not None and target.enabled)


def _badges(app: AppContext, repo: str) -> str:
    row = _override(app, repo)
    out = "✅" if _enabled(app, repo) else "⏸"
    if row is not None and row["ai_policy_override"] == "allowed":
        out += " 🤖"
    if app.discovery_prefs.effective(repo).never_repo:
        out += " 🚫"
    return out


def repos_view(app: AppContext, page: int = 1, flt: str | None = None) -> k.View:
    repos = [r for r in all_targets(app) if not flt or flt.lower() in r.lower()]
    items, page, pages = k.list_view([(k.Text(r),) for r in repos], page, REPOS_PER_PAGE)
    start = (page - 1) * REPOS_PER_PAGE
    btns = [k.button(f"{_badges(app, repos[start + n])} {middle_cut(repos[start + n], 18)}", "repos_set",
                     {"repo": repos[start + n]}) for n in range(len(items))]  # fmt: skip
    rows = [btns[i : i + 2] for i in range(0, len(btns), 2)][:4]
    if pages > 1:
        rows.append(k.pager("repos_set", {"filter": flt} if flt else {}, page, pages))
    lines = [(k.Const("🏷 "), k.Bold(k.Const("Target repos")), k.Const(f" · {len(repos)}")),
             k.line("✅ enabled · ⏸ disabled · 🤖 AI allowed by you · 🚫 never proposed")]  # fmt: skip
    return k.View(kind="picker", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows[:6])


def repo_card(app: AppContext, repo: str) -> k.View:
    row = _override(app, repo)
    prefs = app.discovery_prefs.effective(repo)
    ai = bool(row is not None and row["ai_policy_override"] == "allowed")
    lines = [(k.Const("🏷 "), k.Bold(k.Text(repo, 140)), k.Const(f" · {_badges(app, repo)}")),
             k.line(f"{'Enabled' if _enabled(app, repo) else 'Disabled'} · AI policy override:"
                    f" {'allowed' if ai else 'none'} · size ≤ {prefs.autonomous_max_class}")]  # fmt: skip
    first = [k.button("⏸ Disable" if _enabled(app, repo) else "✅ Enable", "repos_set",
                      {"repo": repo, "op": "disable" if _enabled(app, repo) else "enable"}),
             k.button("🤖 Revoke AI" if ai else "🤖 Allow AI", "repos_set",
                      {"repo": repo, "op": "revoke_ai" if ai else "allow_ai"})]  # fmt: skip
    rows: k.Rows = [first, [k.button(f"📏 Size: {prefs.autonomous_max_class} ▾", "size_set",
                                     {"view": "repo", "repo": repo})]]  # fmt: skip
    if prefs.never_repo:
        lines.append(k.line("🚫 Never proposed (you chose Never on a proposal)."))
        rows.append([k.button("↩️ Propose again", "repos_set", {"repo": repo, "op": "unnever"})])
    rows.append([k.button("↩️ Back", "repos_set", {})])
    return k.View(kind="picker", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows)


WIDENS = frozenset({"enable", "allow_ai"})
OP_TEXT = {"enable": "Enable {repo}: the bot may propose and work here again.",
           "disable": "Disable {repo}: no new proposals or work here; open cards stay valid.",
           "allow_ai": "Allow AI for {repo}: you confirm its AI policy allows AI-assisted contributions.",
           "revoke_ai": "Revoke your AI-policy override for {repo}."}  # fmt: skip


@command("repos", help="Target repos: enable, allow AI, per-repo size", safety="read")
async def repos(ctx: CommandContext, args: list[str]) -> Reply:
    app = ctx.app
    if len(args) >= 3 and args[1] == "size":
        if resolve_target(app.config.targets, args[0]) is None:
            return Reply("Not a target repo.")
        try:
            req = PrefsUpdate.model_validate({"scope": args[0], "autonomous_max_class": args[2].lower()})
            app.discovery_prefs.update(req, ctx.actor)
        except (PrefsRefused, ValueError) as exc:
            return Reply(
                f"Refused: {esc(getattr(exc, 'code', 'bad class'))}. Usage: /repos owner/name size xs|s|m|l|xl|inherit"
            )
        return Reply("", view=repo_card(app, args[0]))
    return Reply("", view=repos_view(app, 1, args[0] if args else None))


@action("repos_set", arg=RepoArg)
async def repos_set(tap: Tap) -> str:
    a: RepoArg = tap.arg
    app = tap.app
    if a.repo is None:
        ui_send.edit(app, repos_view(app, a.page, a.filter), message_id=tap.message_id)
        return ""
    if resolve_target(app.config.targets, a.repo) is None:
        return "Not a target repo."
    if a.op == "view":
        view = repo_card(app, a.repo)
    elif a.op == "unnever":  # tune: immediate, the Undo promised by "Never this repo" (§14.11.15)
        try:
            app.discovery_prefs.update(PrefsUpdate(scope=a.repo, filters={"never": False}), tap.actor)
        except PrefsRefused as exc:
            return f"Refused: {exc.code}"
        view = repo_card(app, a.repo)
    else:
        arg = json.dumps({"repo": a.repo, "op": a.op}, sort_keys=True)
        view = confirm_panel([k.line(OP_TEXT[a.op].format(repo=a.repo))],
                             confirm_button("✅ Confirm", "repo_confirm", arg,
                                            style="danger" if a.op in WIDENS else "primary"))  # fmt: skip
    ui_send.edit(app, view, message_id=tap.message_id)
    return ""


@confirm_handler("repo_confirm")
async def repo_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    app = ctx.app
    try:
        req = json.loads(arg or "")
        repo, op = str(req["repo"]), str(req["op"])
    except (ValueError, KeyError, TypeError):
        return Reply("This button is no longer valid.")
    if op not in OP_TEXT or resolve_target(app.config.targets, repo) is None:
        return Reply("This button is no longer valid.")
    column, value = ("enabled", int(op == "enable")) if op in {"enable", "disable"} else (
        "ai_policy_override", "allowed" if op == "allow_ai" else None)  # fmt: skip
    now = to_iso(app.clock.now())
    with db.transaction(app.conn):
        app.conn.execute(
            f"INSERT INTO repo_overrides (upstream, {column}, updated_by, updated_at) VALUES (?, ?, ?, ?)"  # noqa: S608
            f" ON CONFLICT(upstream) DO UPDATE SET {column} = excluded.{column}, updated_by = excluded.updated_by,"
            " updated_at = excluded.updated_at",
            (repo, value, ctx.actor, now),
        )  # column is one of two constants above
        app.audit.append(ctx.actor, "repo_override_set", {"repo": repo, "op": op})
    return Reply(f"🏷 {esc(repo)}: {esc(op.replace('_', ' '))} done.")
