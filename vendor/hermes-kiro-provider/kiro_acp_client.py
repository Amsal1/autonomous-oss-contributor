"""Kiro ACP client — thin adaptation of Hermes' in-tree Copilot ACP shim.

The upstream ``agent/copilot_acp_client.py`` is a generic JSON-RPC/ACP-over-stdio
bridge parameterized by command + args; only its env-var resolution and error
copy are Copilot-specific. This subclass points those at ``kiro-cli acp``
(Kiro CLI from AWS, kiro.dev) and keeps everything else — session lifecycle,
``session/update`` streaming, permission handling, the file-safety bridge —
inherited, so fixes upstream flow through.

kiro-cli specifics verified against v2.22.0:
- handshake answers ``initialize`` (protocolVersion 1, loadSession, image prompts)
- spawn flags: ``--agent`` / ``--model`` / ``--effort`` / ``-a`` (trust tools)
- auth rides the subprocess (~/.kiro login) — no env keys to manage
"""

from __future__ import annotations

import os
import shlex
from typing import Any

from agent.copilot_acp_client import ACP_MARKER_BASE_URL as _COPILAT_MARKER, CopilotACPClient

KIRO_MARKER_BASE_URL = "acp://kiro"


def resolve_kiro_command() -> str:
    """Binary override order: HERMES_KIRO_ACP_COMMAND → KIRO_CLI_PATH → kiro-cli."""
    return (
        os.getenv("HERMES_KIRO_ACP_COMMAND", "").strip()
        or os.getenv("KIRO_CLI_PATH", "").strip()
        or "kiro-cli"
    )


def resolve_kiro_args() -> list[str]:
    """Argv override (shlex-split), else ``['acp']``. Extra flags like
    ``--agent NAME`` / ``--effort high`` / ``-a`` can be passed wholesale."""
    return shlex.split(os.getenv("HERMES_KIRO_ACP_ARGS", "").strip()) or ["acp"]


class KiroACPClient(CopilotACPClient):
    """OpenAI-client-compatible facade driving ``kiro-cli acp`` over stdio."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        acp_command: str | None = None,
        acp_args: list[str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url or KIRO_MARKER_BASE_URL,
            acp_command=acp_command or command or resolve_kiro_command(),
            acp_args=list(acp_args or args or resolve_kiro_args()),
            **kwargs,
        )

    def _create_chat_completion(self, *, model=None, messages=None, **kwargs):
        # The provider name is not a model id; treat it as "use the session
        # default" instead of attempting a bogus model selection (which the
        # inherited code would log a Copilot-flavored warning for).
        if str(model or "").strip().lower() in ("", "kiro-acp", "kiro", "kiro-cli", "kiro-acp-agent"):
            model = None
        return super()._create_chat_completion(model=model, messages=messages, **kwargs)

    def _spawn(self):  # noqa: ANN202 - matches parent's Popen[str] return
        try:
            return super()._spawn()
        except RuntimeError as exc:
            # Translate the Copilot-flavored install hint into a Kiro one.
            if "Could not start Copilot ACP command" in str(exc):
                raise RuntimeError(
                    f"Could not start Kiro ACP command '{self._acp_command}'. Install Kiro CLI "
                    "(kiro.dev/docs/cli) and log in, or set HERMES_KIRO_ACP_COMMAND/KIRO_CLI_PATH."
                ) from exc
            raise
