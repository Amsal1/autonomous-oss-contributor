"""Shared sandbox test helpers: every sandboxd argv variant, rendered as the CLI's create request,
plus a fake ``Lookup`` for the Docker filter rules (DESIGN §10.2, §10.8, M4)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ossbot.dockerfilter import rules
from ossbot.sandboxd.argv import Volumes, build_argv, job_spec, probe_spec, snapshot_spec, stagein_spec
from ossbot.sandboxd.runtime.base import Mount, RunSpec
from ossbot.sandboxd.runtime.cli_model import parse_run

FULL_DIGEST = "a" * 64
LLM_DIGEST = "b" * 64
FULL = f"ossbot/sandbox-full@sha256:{FULL_DIGEST}"
LLM = f"ossbot/sandbox-llm@sha256:{LLM_DIGEST}"
DIGESTS = {"sandbox-full": frozenset({FULL_DIGEST}), "sandbox-llm": frozenset({LLM_DIGEST})}
CID = "c_01ABCDEFGH"
RUN = "r1-plan"
NET = "ossbot-t-c-01abcdefgh-research"
ENVIRON = {"KIRO_API_KEY": "ksk_test_key_value_0123456789", "GITHUB_READ_TOKEN": "github_pat_" + "T" * 30}


def variants() -> dict[str, RunSpec]:
    """One RunSpec per template variant sandboxd can emit."""
    v = Volumes(RUN, CID)
    kiro_env = {"ROLE": "planner", "PROMPT_FILE": "plan.md", "MODEL": "claude-test", "EFFORT": "high",
                "TRUST_TOOLS": "read,web_search,web_fetch", "OSSBOT_TRUST_MODE": "ids"}  # fmt: skip
    coder_env = {**kiro_env, "ROLE": "coder", "TRUST_TOOLS": "@ossbot-exec/run", "REQUIRE_MCP": "1"}
    out = {
        "fetch": job_spec("fetch", run_id=RUN, cid=CID, entrypoint="fetch", image_ref=FULL,
                          network="ossbot-t-c-01abcdefgh-fetch", repo_mode="rw",
                          env={"OSSBOT_ARG_REPO": "owner/repo", "OSSBOT_ARG_CLONE_DEPTH": "200"}),
        "validate": job_spec("validate", run_id=RUN, cid=CID, entrypoint="validate", image_ref=FULL,
                             network="ossbot-t-c-01abcdefgh-validate", repo_mode="rw",
                             env={"OSSBOT_ARG_PLAN_FILE": "validate.json"}),
        "package": job_spec("package", run_id=RUN, cid=CID, entrypoint="package", image_ref=FULL, network="none",
                            repo_mode="rw", env={"OSSBOT_ARG_ROUND_BASE": "1" * 40}),
        "unbundle": job_spec("package", run_id=RUN, cid=CID, entrypoint="unbundle", image_ref=FULL, network="none",
                             repo_mode="rw", env={"OSSBOT_ARG_MODE": "fix_round"}),
        "research-snapshot": job_spec("research", run_id=RUN, cid=CID, entrypoint="kiro", image_ref=LLM, network=NET,
                                      repo_mode="snapshot", env=kiro_env),
        "research-none": job_spec("research", run_id=RUN, cid=CID, entrypoint="kiro", image_ref=LLM, network=NET,
                                  repo_mode="none", env={**kiro_env, "ROLE": "vetter"}),
        # GHMCP-2: scout (and reviewer) get the name-only GITHUB_READ_TOKEN for ossbot-github-mcp
        "research-scout": job_spec("research", run_id=RUN, cid=CID, entrypoint="kiro", image_ref=LLM, network=NET,
                                   repo_mode="snapshot", env={**kiro_env, "ROLE": "scout"}),
        "agent-llm": job_spec("agent-llm", run_id=RUN, cid=CID, entrypoint="kiro", image_ref=LLM,
                              network="ossbot-t-c-01abcdefgh-llm", repo_mode="none", env=coder_env),
        "agent-exec": job_spec("agent-exec", run_id=RUN, cid=CID, entrypoint="exec", image_ref=FULL, network="none",
                               repo_mode="rw"),
        "catalog": job_spec("catalog", run_id=RUN, cid="sys-catalog", entrypoint="list_models", image_ref=LLM,
                            network="ossbot-t-sys-catalog-catalog", repo_mode="none"),
        "extract": job_spec("extract", run_id=RUN, cid="sys-chat", entrypoint="extract_text", image_ref=FULL,
                            network="none", repo_mode="none", cpus=1.0, memory_mb=512),
        "code": job_spec("code", run_id=RUN, cid="sys-chat", entrypoint="run_code", image_ref=FULL, network="none",
                         repo_mode="none", env={"OSSBOT_ARG_LANGUAGE": "python", "OSSBOT_ARG_TIMEOUT_S": "120"},
                         cpus=1.0, memory_mb=1024),
        "stage-in": stagein_spec(RUN, CID, FULL),
        "snapshot": snapshot_spec(RUN, CID, FULL),
        "snapcheck": probe_spec(RUN, CID, FULL, "snapshot_check", (Mount(v.ro, "/work/repo", read_only=True),),
                                suffix="snapcheck"),
        "fixture": probe_spec(RUN, CID, FULL, "extract_fixture", (Mount(v.task, "/work"),), suffix="fixture"),
        "probe-research": job_spec("research", run_id=RUN, cid=CID, entrypoint="probe", image_ref=LLM,
                                   network="none", repo_mode="none", env={"OSSBOT_PROBE": "settings"}),
        "probe-fetch": job_spec("fetch", run_id=RUN, cid=CID, entrypoint="probe", image_ref=FULL,
                                network="ossbot-t-c-01abcdefgh-fetch", repo_mode="rw", env={"OSSBOT_PROBE": "smoke"}),
        "probe-agent-llm": job_spec("agent-llm", run_id=RUN, cid=CID, entrypoint="probe", image_ref=LLM,
                                    network="ossbot-t-c-01abcdefgh-llm", repo_mode="none",
                                    env={"OSSBOT_PROBE": "smoke"}),
    }  # fmt: skip
    return out


def create_request(spec: RunSpec, environ: Mapping[str, str] = ENVIRON) -> rules.Request:
    req = parse_run(build_argv(spec), environ)
    return rules.Request("POST", "/v1.52/containers/create", req.query(), req.body_bytes())


def create_body(spec: RunSpec) -> dict[str, Any]:
    return json.loads(create_request(spec).body)


def body_request(body: Mapping[str, Any] | bytes, name: str = "ossbot-r1-plan") -> rules.Request:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return rules.Request("POST", "/v1.52/containers/create", {"name": [name]}, raw)


@dataclass
class FakeLookup:
    containers: dict[str, rules.ContainerFacts] = field(default_factory=dict)
    networks: dict[str, rules.NetworkFacts] = field(default_factory=dict)

    def container(self, ref: str) -> rules.ContainerFacts | None:
        return self.containers.get(ref) or next((c for c in self.containers.values() if c.name == ref), None)

    def network(self, ref: str) -> rules.NetworkFacts | None:
        return self.networks.get(ref)

    def add(self, cid: str, profile: str | None, status: str = "running", name: str | None = None) -> None:
        labels = {} if profile is None else {
            "ossbot.sandbox": "1", "ossbot.cid": "c_01abcdefgh", "ossbot.run": RUN, "ossbot.profile": profile,
        }  # fmt: skip
        self.containers[cid] = rules.ContainerFacts(id=cid, name=name or f"ossbot-{cid}", labels=labels, status=status)


def policy(tmp: Path) -> rules.FilterPolicy:
    control = tmp / "control"
    control.mkdir(parents=True, exist_ok=True)
    return rules.FilterPolicy(DIGESTS, control)


def set_path(doc: dict[str, Any], dotted: str, value: Any) -> None:
    """Set ``a.b.0.c`` in a JSON document (``value is DELETE`` removes the key)."""
    parts = dotted.split(".")
    node: Any = doc
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node.setdefault(part, {})
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    elif value is DELETE:
        node.pop(last, None)
    else:
        node[last] = value


DELETE = object()
