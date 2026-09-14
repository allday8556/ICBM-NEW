"""The SmartStore M2 endpoint registry (docs/platforms/smartstore/ENDPOINT_MATRIX.md; M2 PR-A).

This is the single source of the provider host, the base URL, and every endpoint's method, path,
content type, timeouts, redirect policy, bearer requirement and success predicate (EM §3, §6, §7,
§13 item 3). Only ADOPTED endpoints resolve. A NOT_ADOPTED id fails here, locally, before any
network I/O (EM §2; CAPABILITY_MAPPING E1, §17 #8). No other module may spell the host, the base URL
or a SmartStore path; only the caller composes a URL, from this contract.

The registry also owns the endpoint-mapping revision (M2 instructions §5). It is a human-readable
revision bound to a fingerprint of the permission-relevant registry content, so a mapping change
without a revision bump fails CI (§5.3). The revision is never derived from documentation at
runtime.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

PROVIDER = "SMARTSTORE"
# AUTH.md §2.1: M2 uses the SELF token type only. Switching to SELLER needs its own ADR (§2.2).
AUTH_MODE = "SELF"
PROVIDER_HOST = "api.commerce.naver.com"
BASE_URL = f"https://{PROVIDER_HOST}/external"


class EndpointId(StrEnum):
    # ADOPTED for M2 (EM §4).
    SMARTSTORE_AUTH_TOKEN = "SMARTSTORE_AUTH_TOKEN"
    SMARTSTORE_SELLER_ACCOUNT = "SMARTSTORE_SELLER_ACCOUNT"
    # NOT_ADOPTED: M5 planning metadata only (EM §4); never callable in M2.
    SMARTSTORE_PRODUCT_CREATE_V2 = "SMARTSTORE_PRODUCT_CREATE_V2"
    SMARTSTORE_ORIGIN_PRODUCT_READ_V2 = "SMARTSTORE_ORIGIN_PRODUCT_READ_V2"
    SMARTSTORE_CHANNEL_PRODUCT_READ_V2 = "SMARTSTORE_CHANNEL_PRODUCT_READ_V2"
    SMARTSTORE_PRODUCT_IMAGE_UPLOAD = "SMARTSTORE_PRODUCT_IMAGE_UPLOAD"
    SMARTSTORE_CATEGORY_LIST = "SMARTSTORE_CATEGORY_LIST"
    SMARTSTORE_CATEGORY_READ = "SMARTSTORE_CATEGORY_READ"
    SMARTSTORE_PRODUCT_ATTRIBUTE_LIST = "SMARTSTORE_PRODUCT_ATTRIBUTE_LIST"
    SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES = "SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES"
    SMARTSTORE_STANDARD_OPTIONS = "SMARTSTORE_STANDARD_OPTIONS"
    SMARTSTORE_NOTICE_TYPES = "SMARTSTORE_NOTICE_TYPES"


class Method(StrEnum):
    GET = "GET"
    POST = "POST"


class RedirectPolicy(StrEnum):
    # EM §11: generic automatic redirect following is forbidden for SmartStore clients.
    NO_FOLLOW = "NO_FOLLOW"


SuccessPredicate = Callable[[int, object], bool]


def token_succeeded(status: int, body: object) -> bool:
    """EM §6: HTTP 200 AND the body parses as a JSON object AND ``access_token`` is a non-empty
    string AND ``expires_in`` is a positive integer AND ``token_type`` equals Bearer
    case-insensitively."""
    if status != 200 or not isinstance(body, dict):
        return False
    token, expires_in, token_type = (
        body.get("access_token"),
        body.get("expires_in"),
        body.get("token_type"),
    )
    return (
        isinstance(token, str)
        and token.strip() != ""
        and isinstance(expires_in, int)
        and not isinstance(expires_in, bool)
        and expires_in > 0
        and isinstance(token_type, str)
        and token_type.lower() == "bearer"
    )


def account_succeeded(status: int, body: object) -> bool:
    """EM §7: HTTP 200 AND the body parses as a JSON object AND ``accountUid`` is a non-empty
    string. ``accountId`` is never substituted for a missing ``accountUid``."""
    if status != 200 or not isinstance(body, dict):
        return False
    uid = body.get("accountUid")
    return isinstance(uid, str) and uid.strip() != ""


@dataclass(frozen=True)
class EndpointContract:
    endpoint_id: EndpointId
    method: Method
    # Relative to BASE_URL (EM §3); the caller composes the wire URL exactly once.
    path: str
    content_type: str | None
    requires_bearer: bool
    connect_timeout_s: float
    read_timeout_s: float
    redirect: RedirectPolicy
    # Provider API groups the endpoint needs (EM §5). Permission-relevant: part of the fingerprint.
    required_groups: frozenset[str]
    # A marketplace resource mutation. Neither M2 endpoint is one (EM §4, §6).
    mutating: bool
    success_predicate: SuccessPredicate
    # Revision of the frozen success predicate, recorded in evidence (ERRORS.md §6).
    predicate_revision: str


ADOPTED: Mapping[EndpointId, EndpointContract] = {
    EndpointId.SMARTSTORE_AUTH_TOKEN: EndpointContract(
        endpoint_id=EndpointId.SMARTSTORE_AUTH_TOKEN,
        method=Method.POST,
        path="/v1/oauth2/token",
        content_type="application/x-www-form-urlencoded",
        requires_bearer=False,
        connect_timeout_s=5.0,
        read_timeout_s=30.0,
        redirect=RedirectPolicy.NO_FOLLOW,
        required_groups=frozenset(),
        mutating=False,
        success_predicate=token_succeeded,
        predicate_revision="em6-token-r1",
    ),
    EndpointId.SMARTSTORE_SELLER_ACCOUNT: EndpointContract(
        endpoint_id=EndpointId.SMARTSTORE_SELLER_ACCOUNT,
        method=Method.GET,
        path="/v1/seller/account",
        content_type=None,
        requires_bearer=True,
        connect_timeout_s=5.0,
        read_timeout_s=10.0,
        redirect=RedirectPolicy.NO_FOLLOW,
        required_groups=frozenset({"판매자정보"}),
        mutating=False,
        success_predicate=account_succeeded,
        predicate_revision="em7-account-r1",
    ),
}

NOT_ADOPTED: frozenset[EndpointId] = frozenset(EndpointId) - frozenset(ADOPTED)


class EndpointNotAdoptedError(LookupError):
    """A NOT_ADOPTED or unknown endpoint was requested. Raised before any network I/O."""


def resolve(endpoint_id: object) -> EndpointContract:
    """The contract of an ADOPTED endpoint. Anything else fails locally (EM §2)."""
    contract = ADOPTED.get(endpoint_id) if isinstance(endpoint_id, EndpointId) else None
    if contract is None:
        raise EndpointNotAdoptedError(f"{endpoint_id!s} is not an ADOPTED SmartStore endpoint")
    return contract


# ---------------------------------------------------------------- endpoint-mapping revision

SMARTSTORE_ENDPOINT_MAPPING_REVISION = "m2-connect-r1"

# M2 instructions §5.3: each revision is bound to the fingerprint of the permission-relevant
# registry content it names. Changing that content changes the fingerprint, and CI fails until a
# new revision and its fingerprint are added here in the same PR.
MAPPING_FINGERPRINTS: Mapping[str, str] = {
    "m2-connect-r1": "17d3dfe97b2f4a6c5e0b363c9c83cba014c018619c6197d54cfa395277016ca9",
}


def mapping_fingerprint() -> str:
    """SHA-256 of the permission-relevant registry content: provider, auth mode, base URL, and
    each endpoint's id, adoption, method, path, required groups and mutability."""
    content = {
        "provider": PROVIDER,
        "auth_mode": AUTH_MODE,
        "base_url": BASE_URL,
        "adopted": [
            {
                "id": c.endpoint_id.value,
                "method": c.method.value,
                "path": c.path,
                "required_groups": sorted(c.required_groups),
                "mutating": c.mutating,
            }
            for c in sorted(ADOPTED.values(), key=lambda c: c.endpoint_id.value)
        ],
        "not_adopted": sorted(e.value for e in NOT_ADOPTED),
    }
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RegistryMappingRevision:
    """The authoritative ``EndpointMappingRevisionProvider`` (M2 instructions §5.1, §5.2)."""

    def current_revision(self) -> str:
        return SMARTSTORE_ENDPOINT_MAPPING_REVISION
