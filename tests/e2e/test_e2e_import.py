"""M3 end to end (DESIGN §9.13): ``ossctl contribution import`` (POST /v1/contributions/import, bundle
bytes) for ``tobenwarrior/hermes-kiro-provider`` -> IMPORTED -> fetch at base_sha -> FETCHED -> unbundle
(mode=import, real unbundle.sh) -> IMPLEMENTED -> validate -> review -> package (real package.sh) ->
preview with one re-authored commit; nothing pushed."""

from __future__ import annotations

import base64
import hashlib
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from starlette.testclient import TestClient

from ossbot.gatekeeper import db
from ossbot.gatekeeper.app import create_app
from tests.conftest import UID_OPERATOR
from tests.e2e.conftest import E2E
from tests.githarness import GitWorld, tgit
from tests.unit.gatekeeper.api_helpers import InjectPeer

REPO = "tobenwarrior/hermes-kiro-provider"


def test_operator_import_reaches_the_preview(e2e: E2E) -> None:
    other = GitWorld(e2e.world.root.parent / "import-world", e2e.world.remote, upstream=REPO)
    e2e.world.write(other.upstream, {"tests/test_acp.py": "def test_x():\n    assert True\n"})
    base = other.commit(other.upstream, "add tests")
    e2e.upstreams[REPO] = other.upstream
    e2e.gh.add_repo(REPO, default_branch="main")
    work = other.root / "op"
    tgit(other.root, "clone", "-q", str(other.upstream), str(work))
    tgit(work, "checkout", "-q", "-b", "v3-acp")
    other.write(work, {"src/acp_v3.py": "VERSION = 3\n", "tests/test_acp.py": "def test_v3():\n    assert True\n"})
    other.commit(work, "feat: V3 ACP adapter (operator commit)")
    bundle = other.root / "change.bundle"
    tgit(work, "bundle", "create", "-q", str(bundle), f"{base}..refs/heads/v3-acp")
    data = bundle.read_bytes()
    req = {"repo": REPO, "base_ref": "main", "base_sha": base, "bundle_b64": base64.b64encode(data).decode(),
           "bundle_sha256": hashlib.sha256(data).hexdigest(), "title": "feat: V3 ACP adapter",
           "body": "Adds the V3 adapter.", "issue_number": None}  # fmt: skip
    with TestClient(InjectPeer(create_app(e2e.ctx, housekeeping=False), UID_OPERATOR),
                    base_url="http://gatekeeper") as op:  # type: ignore[arg-type]  # fmt: skip
        resp = op.post("/v1/contributions/import", json=req)
    assert resp.status_code == 201, resp.text
    cid = str(resp.json()["cid"])
    assert e2e.state(cid) == "IMPORTED"
    e2e.ready_scheduler().last_discovery = e2e.ctx.clock.now()  # only the import runs here
    # the repo's AI policy is unclear (no policy file) until the owner taps "Allow AI for this repo" (§9.13)
    e2e.ctx.conn.execute("INSERT INTO repo_overrides (upstream, ai_policy_override, updated_at, updated_by)"
                         " VALUES (?, 'allowed', '2026-10-18T00:00:00Z', 'tg:4242')", (REPO,))  # fmt: skip
    assert e2e.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", e2e.why(cid)
    assert e2e.pipeline_log() == ["fetch:build", "unbundle:import", "validate", "kiro:reviewer", "kiro:summarizer",
                                  "package"]  # fmt: skip
    patch = (e2e.ctx.paths.artifacts_dir / cid / "1" / "change.patch").read_text()
    assert patch.count("\nSubject: ") == 1 and "src/acp_v3.py" in patch  # one re-authored commit
    profile = e2e.ctx.config.profile
    assert f"From: {profile.commit_name} <{profile.commit_email}>" in patch
    assert e2e.writes() == [] and e2e.pusher.pushes == []
    assert db.one(e2e.ctx.conn, "SELECT source FROM contributions WHERE id = ?", (cid,))[0] == "operator"
