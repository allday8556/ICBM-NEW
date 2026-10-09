"""The CLIProxyAPI adapter (ADR-0027 §4): one OpenAI-compatible chat completion on loopback.

It is the only AI module that opens a connection (a repository rule pins it). The request is the
composed task text as one user message at temperature 0, asking for one JSON object. The answer's
first JSON object is the value; the provenance is copied from the response (the model it names,
the usage it reports) and nothing is invented: an unreported value stays ``None``, and cost is
never reported as 0 (ADR-0012 §6, §7).
"""

import http.client
import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

AI_SIDECAR_UNREACHABLE: Final = "AI_SIDECAR_UNREACHABLE"
AI_SIDECAR_RATE_LIMITED: Final = "AI_SIDECAR_RATE_LIMITED"
AI_SIDECAR_AUTH: Final = "AI_SIDECAR_AUTH"
AI_SIDECAR_HTTP_ERROR: Final = "AI_SIDECAR_HTTP_ERROR"
AI_OUTPUT_NOT_JSON: Final = "AI_OUTPUT_NOT_JSON"
AI_PEER_NOT_APPROVED: Final = "AI_PEER_NOT_APPROVED"

# The sidecar's OpenAI-compatible chat route, relative to the profile's loopback endpoint.
CHAT_COMPLETIONS: Final = "v1/chat/completions"
_INSTRUCTION: Final = "Answer with exactly one JSON object and nothing else."


@dataclass(frozen=True)
class SidecarAnswer:
    """What one call returned: the parsed object or the classified failure, and what the response
    itself reported."""

    value: dict[str, Any] | None
    error_kind: (
        str | None
    )  # TRANSIENT | RATE_LIMITED | AUTH | UNKNOWN | VALIDATION | POLICY_BLOCKED
    error_code: str | None
    actual_model: str | None
    sidecar_version: str | None
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int


def first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for start in (index for index, char in enumerate(text) if char == "{"):
        try:
            value, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _usage(body: dict[str, Any], key: str) -> int | None:
    usage = body.get("usage")
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def complete(
    endpoint: str,
    client_key: str,
    model: str,
    text: str,
    verify: Callable[[int], bool],
    *,
    timeout_s: float = 120.0,
) -> SidecarAnswer:
    """One call on one connection that is proven before anything is sent.

    The connection is opened first; ``verify`` is then asked, with the connection's own local port,
    whether the process at the other end of exactly this connection is the approved one. Only then
    is the request written, on that same connection: a listener swapped after the probe can never
    receive the key or the facts (ADR-0012 §2, ADR-0027 §3). No proxy, no redirect, no second
    connection. ``endpoint`` is already proven loopback by the profile; the key never leaves this
    function except as the request's bearer."""
    started = time.monotonic()

    def answer(**fields: Any) -> SidecarAnswer:
        values: dict[str, Any] = {
            "value": None,
            "error_kind": None,
            "error_code": None,
            "actual_model": None,
            "sidecar_version": None,
            "tokens_in": None,
            "tokens_out": None,
        }
        values.update(fields)
        return SidecarAnswer(latency_ms=int((time.monotonic() - started) * 1000), **values)

    parts = urlsplit(endpoint)
    host = "127.0.0.1" if parts.hostname in (None, "localhost") else parts.hostname
    port = parts.port or 80
    try:
        sock = socket.create_connection((host, port), timeout=timeout_s)
    except OSError:
        return answer(error_kind="TRANSIENT", error_code=AI_SIDECAR_UNREACHABLE)
    try:
        if not verify(sock.getsockname()[1]):
            return answer(error_kind="POLICY_BLOCKED", error_code=AI_PEER_NOT_APPROVED)
        connection = http.client.HTTPConnection(host, port, timeout=timeout_s)
        connection.sock = sock  # this very connection, never a new one
        payload = json.dumps(
            {
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _INSTRUCTION},
                    {"role": "user", "content": text},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            connection.request(
                "POST",
                "/" + CHAT_COMPLETIONS,
                body=payload,
                headers={
                    "Authorization": f"Bearer {client_key}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            raw = response.read()
        except TimeoutError:
            return answer(error_kind="TRANSIENT", error_code=AI_SIDECAR_UNREACHABLE)
        except (OSError, http.client.HTTPException):
            return answer(error_kind="UNKNOWN", error_code=AI_SIDECAR_HTTP_ERROR)
    finally:
        sock.close()
    version = response.getheader("X-CPA-VERSION")
    if response.status == 429:
        return answer(
            error_kind="RATE_LIMITED", error_code=AI_SIDECAR_RATE_LIMITED, sidecar_version=version
        )
    if response.status in (401, 403):
        return answer(error_kind="AUTH", error_code=AI_SIDECAR_AUTH, sidecar_version=version)
    if response.status >= 300:
        # Any other answer, 3xx and 5xx included, is UNKNOWN (ADR-0027 §4): nothing is followed,
        # and the sidecar may have sent the request on, so it is never retried automatically.
        return answer(
            error_kind="UNKNOWN", error_code=AI_SIDECAR_HTTP_ERROR, sidecar_version=version
        )
    try:
        body = json.loads(raw)
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return answer(
            error_kind="VALIDATION", error_code=AI_OUTPUT_NOT_JSON, sidecar_version=version
        )
    reported = {
        "actual_model": body.get("model") if isinstance(body.get("model"), str) else None,
        "sidecar_version": version,
        "tokens_in": _usage(body, "prompt_tokens"),
        "tokens_out": _usage(body, "completion_tokens"),
    }
    value = first_json_object(content) if isinstance(content, str) else None
    if value is None:
        return answer(error_kind="VALIDATION", error_code=AI_OUTPUT_NOT_JSON, **reported)
    return answer(value=value, **reported)
