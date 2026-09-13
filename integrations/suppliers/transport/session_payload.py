"""The authenticated-session payload produced by the browser login and replayed over HTTP.

It is credential-equivalent: core CONNECT treats it as opaque bytes and persists it only through
the encrypted session store. Only cookies scoped to the supplier's own hosts are kept.
"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

FORMAT_VERSION = 1


def _host_matches(domain: str, hosts: Iterable[str]) -> bool:
    bare = domain.lstrip(".").lower()
    return any(bare == host or host.endswith("." + bare) for host in hosts)


def encode_session(
    cookies: Iterable[Mapping[str, Any]], *, user_agent: str, hosts: Iterable[str]
) -> bytes:
    allowed = frozenset(hosts)
    kept = [
        {
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": str(c.get("domain", "")),
            "path": str(c.get("path", "/")),
        }
        for c in cookies
        if _host_matches(str(c.get("domain", "")), allowed)
    ]
    payload = {"v": FORMAT_VERSION, "user_agent": user_agent, "cookies": kept}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def decode_session(payload: bytes) -> tuple[list[dict[str, str]], str]:
    """Return (cookies, user_agent). Raises ValueError for anything that is not a v1 payload."""
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict) or data.get("v") != FORMAT_VERSION:
        raise ValueError("unsupported session payload")
    cookies = data.get("cookies")
    user_agent = data.get("user_agent")
    if not isinstance(cookies, list) or not isinstance(user_agent, str):
        raise ValueError("malformed session payload")
    return [{k: str(v) for k, v in c.items()} for c in cookies if isinstance(c, dict)], user_agent
