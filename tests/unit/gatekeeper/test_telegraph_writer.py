"""``telegraph_writer`` (DESIGN §14.12.8, U11): fixed base URL and methods, tokens only in form bodies."""

from __future__ import annotations

import httpx
import pytest

from ossbot.gatekeeper.telegraph_writer import BASE_URL, METHODS, TelegraphError, TelegraphWriter, make_client
from tests.telegram.telegraphkit import TOKEN, FakeTelegraph


def test_the_method_set_is_fixed() -> None:
    assert BASE_URL == "https://api.telegra.ph/"
    assert frozenset({"createAccount", "createPage", "editPage", "revokeAccessToken"}) == METHODS


async def test_calls_post_forms_to_api_telegra_ph_only() -> None:
    fake = FakeTelegraph()
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await fake.handler(request)

    w = TelegraphWriter(make_client(transport=httpx.MockTransport(handler)), TOKEN)
    page = await w.create_page("T · ABCDEFGH", [{"tag": "p", "children": ["x"]}])
    await w.edit_page(page.path, "Removed", [{"tag": "p", "children": ["gone"]}])
    assert [r.url.host for r in seen] == ["api.telegra.ph", "api.telegra.ph"]
    assert all(r.method == "POST" and TOKEN not in str(r.url) for r in seen)
    with pytest.raises(TelegraphError):
        await w._call("getViews", {})  # not an allowed method
    with pytest.raises(TelegraphError):
        await w.edit_page("../x", "t", [])


async def test_a_client_with_another_base_url_is_refused() -> None:
    client = httpx.AsyncClient(base_url="https://evil.example/", transport=httpx.MockTransport(FakeTelegraph().handler))
    with pytest.raises(TelegraphError):
        await TelegraphWriter(client, TOKEN).create_page("t", [])
