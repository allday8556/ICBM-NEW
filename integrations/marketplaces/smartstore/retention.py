"""Deny-by-default retention of a SmartStore response (ADR-0011 §3, ADR-0014 §15; M5 PR-D).

ADR-0011 §3 left the marketplace safe-query-key profile to M5, and ADR-0014 §15 fixes it: every
adopted endpoint declares the query keys it may send and the response fields that may be kept, and
everything else is removed **before** anything is hashed, stored or logged. This module applies
that profile; the registry declares it, versioned with the endpoint-mapping revision.

The filter keeps a scalar only when its field name is on the endpoint's allow-list. Containers are
traversed but never asserted: an object or an array survives only if something allow-listed remains
inside it, so a response shape the packet does not prove can neither be claimed nor retained. The
result is a plain, sorted, JSON-safe mapping — the only form REGISTER ever sees.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from integrations.marketplaces.smartstore.registry import (
    SAFE_RETENTION_PROFILE_VERSION,
    EndpointContract,
)

__all__ = ["SAFE_RETENTION_PROFILE_VERSION", "retain", "retained_query"]

# A retained scalar is a JSON scalar. Anything else (a nested callable, a byte string) is dropped.
_SCALARS = (str, int, float, bool)


def _keep(value: Any, allowed: frozenset[str]) -> Any | None:
    if isinstance(value, Mapping):
        kept = {
            str(key): child
            for key, raw in sorted(value.items(), key=lambda item: str(item[0]))
            if (child := _child(str(key), raw, allowed)) is not None
        }
        return kept or None
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        items = [child for item in value if (child := _keep(item, allowed)) is not None]
        return items or None
    return None


def _child(key: str, value: Any, allowed: frozenset[str]) -> Any | None:
    """A retained value under ``key``: an allow-listed scalar, or a container that still holds
    something allow-listed. A scalar whose name is not on the list is dropped, whatever it is."""
    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, str | bytes)
    ):
        return _keep(value, allowed)
    if key not in allowed:
        return None
    return value if isinstance(value, _SCALARS) and not isinstance(value, bytes) else None


def retain(contract: EndpointContract, body: object) -> dict[str, Any]:
    """The part of ``body`` this endpoint is allowed to keep. Empty when nothing qualifies."""
    kept = _keep(body, contract.retained_response_fields) if isinstance(body, Mapping) else None
    return dict(kept) if isinstance(kept, dict) else {}


def retained_query(contract: EndpointContract, query: Mapping[str, str]) -> dict[str, str]:
    """The query this endpoint may send. A key outside the allow-list is a local contract
    violation, never a silently dropped parameter: the caller refuses the request."""
    unsafe = sorted(set(query) - contract.safe_query_keys)
    if unsafe:
        raise ValueError(f"{contract.endpoint_id.value} may not send query keys: {unsafe}")
    return {key: query[key] for key in sorted(query)}
