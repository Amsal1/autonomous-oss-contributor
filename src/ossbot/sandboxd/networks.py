"""Per-task internal networks (DESIGN §10.3): ``ossbot-t-<cid>-<profile>``, ``--internal``, an
allocated /28, the proxy container attached with alias ``proxy``. Released on task destroy
(catalog: at job end)."""

from __future__ import annotations

import logging

from ossbot.sandboxd.profiles import cid_slug, network_name
from ossbot.sandboxd.runtime.base import ContainerRuntime, RuntimeFailure
from ossbot.sandboxd.subnets import SubnetAllocator

log = logging.getLogger(__name__)
PROXY_CONTAINER = "ossbot-proxy"
PROXY_ALIAS = "proxy"


def _exists(exc: RuntimeFailure) -> bool:
    text = str(exc).lower()
    return "already exists" in text or "already attached" in text or "endpoint with name" in text


class Networks:
    def __init__(self, runtime: ContainerRuntime, subnets: SubnetAllocator) -> None:
        self.runtime = runtime
        self.subnets = subnets

    def ensure(self, cid: str, profile: str) -> str:
        name = network_name(cid, profile)
        subnet = self.subnets.allocate(profile, cid)
        labels = {"ossbot.sandbox": "1", "ossbot.cid": cid_slug(cid), "ossbot.profile": profile}
        try:
            self.runtime.create_network(name, subnet, internal=True, labels=labels)
        except RuntimeFailure as exc:
            if not _exists(exc):
                self.subnets.release(cid, profile)
                raise
        try:
            self.runtime.connect(name, PROXY_CONTAINER, alias=PROXY_ALIAS)
        except RuntimeFailure as exc:
            if not _exists(exc):
                raise
        return name

    def release(self, cid: str, profile: str | None = None) -> None:
        profiles = list(self.subnets.allocated(cid)) if profile is None else [profile]
        for p in profiles:
            try:
                self.runtime.disconnect(network_name(cid, p), PROXY_CONTAINER)
            except RuntimeFailure as exc:
                log.info("proxy disconnect: %s", exc, extra={"cid": cid})
            try:
                self.runtime.remove_network(network_name(cid, p))
            except RuntimeFailure as exc:
                log.warning("network removal failed: %s", exc, extra={"cid": cid})
        self.subnets.release(cid, profile)
