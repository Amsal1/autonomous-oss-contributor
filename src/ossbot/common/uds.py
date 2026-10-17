"""httpx clients over Unix domain sockets (gatekeeper and sandboxd APIs)."""

from __future__ import annotations

from pathlib import Path

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


def uds_client(sock: Path, base_url: str = "http://ossbot", timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=str(sock)), base_url=base_url, timeout=timeout, trust_env=False
    )


def uds_async_client(
    sock: Path, base_url: str = "http://ossbot", timeout: httpx.Timeout = DEFAULT_TIMEOUT
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=str(sock)), base_url=base_url, timeout=timeout, trust_env=False
    )
