"""Capture of the official SmartStore 상품정보제공고시 schema (notice coverage S0).

Owner directive 2026-10-03: the notice coverage is captured once, from the provider, for every
official type — never added type by type and never guessed. This reads the official type list
(``GET /v1/products-for-provided-notice``) and then each listed type's content fields
(``GET /v1/products-for-provided-notice/{type}``), in order, once each, with the committed bearer.

It is a read and nothing else: no retry, no write, no mutation, and nothing is stored here. What
comes back is the retained, allow-listed part of each response (the type identity and name and each
content field's documented members), with the mapping revision it was read under. A type whose read
fails is reported with its code; nothing is filled in for it.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from integrations.marketplaces.smartstore.caller import (
    NoticeCatalogRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import (
    SMARTSTORE_ENDPOINT_MAPPING_REVISION,
    EndpointId,
)

BearerSource = Callable[[], Any]
TYPE_KEY = "productInfoProvidedNoticeType"


def _listed_types(retained: Mapping[str, Any]) -> list[str]:
    """Every type code the list response names, in its own order, once each."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            code = value.get(TYPE_KEY)
            if isinstance(code, str) and code not in found:
                found.append(code)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(retained)
    return found


class SmartStoreNoticeCatalog:
    def __init__(self, caller: SmartStoreEndpointCaller, bearer: BearerSource) -> None:
        self._caller = caller
        self._bearer = bearer

    def capture(self) -> dict[str, Any]:
        bearer = self._bearer()
        if bearer is None:
            return {"captured": False, "code": "SMARTSTORE_SESSION_UNAVAILABLE"}

        def request(notice_type: str | None) -> NoticeCatalogRequest:
            return NoticeCatalogRequest(
                bearer.access_token,
                bearer.credential_generation,
                bearer.session_generation,
                notice_type,
            )

        started = datetime.now(UTC).isoformat()
        try:
            listed = self._caller.call(EndpointId.SMARTSTORE_NOTICE_TYPES, request(None))
        except SmartStoreCallError as failure:
            return {"captured": False, "code": failure.code, "http_status": failure.http_status}
        types: dict[str, Any] = {}
        failures: dict[str, Any] = {}
        for code in _listed_types(listed.retained):
            try:
                read = self._caller.call(EndpointId.SMARTSTORE_NOTICE_TYPE_READ, request(code))
            except SmartStoreCallError as failure:
                failures[code] = {"code": failure.code, "http_status": failure.http_status}
                continue
            types[code] = dict(read.retained)
        return {
            "captured": True,
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "mapping_revision": SMARTSTORE_ENDPOINT_MAPPING_REVISION,
            "list": dict(listed.retained),
            "types": types,
            "failures": failures,
        }


__all__ = ["SmartStoreNoticeCatalog"]
