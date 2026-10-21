"""cfn-lint on demand (open-discovery design §12.5, follow-up F1 default): the image ships no cfn-lint,
so a planner's validation command fetches the pinned release through pkg_python. The job runs with
``egress: strict`` (pool_strict, the base allowlist), so the check holds in every egress mode.

validate.sh exports ``UV_OFFLINE=1`` for the plan's commands; an on-demand tool must lift it for its
own command, as a planner's ``validation_commands`` entry would. Written in the build, run in the
close-out only (root, Docker; plan decision D5).
"""

from __future__ import annotations

import json
import shlex

from tests.sandbox.conftest import Sandbox

CID = "c_SMOKETEST6"
CFN_LINT = "UV_OFFLINE=0 uvx --from cfn-lint==1.58.0 cfn-lint"  # pin: design §15 V9
TEMPLATE = 'AWSTemplateFormatVersion: "2010-09-09"\nResources:\n  Bucket:\n    Type: AWS::S3::Bucket\n'


def test_cfn_lint_runs_in_a_strict_validate_job(sandbox: Sandbox) -> None:
    try:
        clone = sandbox.run(sandbox.plan(
            CID, "fetch", entrypoint="fetch", repo_mode="rw", create_task=True, egress="strict",
            env={"OSSBOT_ARG_REPO": "octocat/Hello-World", "OSSBOT_ARG_CLONE_DEPTH": "1"},
        ))  # fmt: skip
        assert clone.exit_code == 0, clone.logs[-2000:]
        plan = {"commands": [
            {"name": "cfn-version", "cmd": f"{CFN_LINT} --version", "timeout_s": 900},
            {"name": "cfn-template",
             "cmd": f"printf %s {shlex.quote(TEMPLATE)} > /tmp/template.yaml && {CFN_LINT} /tmp/template.yaml",
             "timeout_s": 900},
        ]}  # fmt: skip
        src = sandbox.root / "inputs" / "cfn.json"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(json.dumps(plan), encoding="utf-8")
        out = sandbox.run(sandbox.plan(
            CID, "validate", entrypoint="validate", repo_mode="rw", egress="strict",
            env={"OSSBOT_ARG_PLAN_FILE": "cfn.json"}, inputs={"cfn.json": src}, timeout=2000,
        ))  # fmt: skip
        assert out.exit_code == 0, out.logs[-2000:]
        commands = {c["name"]: c for c in json.loads(out.files["validation.json"])["commands"]}
        version = commands["cfn-version"]
        assert version["exit_code"] == 0 and "1.58.0" in version["tail"], version
        assert commands["cfn-template"]["exit_code"] == 0, commands["cfn-template"]
    finally:
        sandbox.cleanup(CID)
