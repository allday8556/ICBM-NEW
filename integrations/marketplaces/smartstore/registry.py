"""The SmartStore endpoint registry (documents/contracts/platforms/smartstore/ENDPOINT_MATRIX.md; M2
PR-A, M5
PR-D).

This is the single source of the provider host, the base URL, and every endpoint's method, path,
content type, timeouts, redirect policy, bearer requirement and success predicate (EM §3, §6, §7,
§13 item 3). Only ADOPTED endpoints resolve. A NOT_ADOPTED id fails here, locally, before any
network I/O (EM §2; CAPABILITY_MAPPING E1, §17 #8). No other module may spell the host, the base URL
or a SmartStore path; only the caller composes a URL, from this contract.

Each endpoint also declares its **deny-by-default** safe query keys and retained response fields
(ADR-0011 §3, ADR-0014 §15). Nothing outside those sets may be sent as a query or kept from a
response, and the profile is versioned with the mapping revision below.

The registry owns the endpoint-mapping revision (M2 instructions §5). It is a human-readable
revision bound to a fingerprint of the permission-relevant registry content, so a mapping change
without a revision bump fails CI (§5.3). The revision is never derived from documentation at
runtime.

**M5 PR-D adoption evidence.** Every provider fact below comes from the architect-supplied
official-source packet for Naver Commerce API **2.89.0 (2026-09-15)** (Issue #89 comment
5746489554): the method, the path, the bearer, the ``상품`` API group and the response fields a
read-back may keep. Timeouts, the redirect policy and the success predicates are ICBM policy over
the JSON-object response convention ENDPOINT_MATRIX.md already accepts, never provider facts.

PR-D adopted the two product read-backs. The later IMAGE UPLOAD amendment (Issue #89 comments
5765557497 and 5765663972) adopts only the official one-artifact ``imageFiles`` request and its
returned ``images[].url`` identity.

**The CREATE adoption slice** (ADR-0020 §4 order 1) adds ``SMARTSTORE_PRODUCT_CREATE_V2`` from
the same 2.89.0 contract, recorded field by field in
``documents/evidence/marketplace-apis/PRODUCT_CREATE.md`` § SmartStore: bearer-authenticated
``POST /v2/products``, ``application/json``, the ``상품`` group, bounded ICBM timeouts, no
redirect, a mutation, and a deny-by-default retention profile of the provider identifiers plus the
safe product leaves.

**The SEARCH positive-only reconcile slice** (ADR-0020 §4 order 2; Issue #89 architect resolution
``5904349289``, S1–S4) adds ``SMARTSTORE_PRODUCT_SEARCH``: bearer-authenticated
``POST /v1/products/search``, ``application/json``, the ``상품`` group, no redirect, **not** a
mutation, and a deny-by-default retention profile of the candidate identities, their seller code
and the pagination envelope (``search.py``). It is read for positive-only reconcile and nothing
else: it never authorizes a CREATE, a zero result is never absence, and duplicate lookup stays
fail-closed (``lookup.py``; ADR-0014 §13, §17.2, §28.2).

Adoption is never LIVE authority and never a call. The application remains DRY_RUN/provider-zero,
the ADR-0018 send-time safety stack refuses every mutation, no real canary is authorized, and an
``UNKNOWN`` CREATE outcome is never resent (ADR-0014 §28; ADR-0018 §6, §6.1). The
provider-evidence verdict stays ``INSUFFICIENT`` and is neither overturned nor re-decided here.
"""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

PROVIDER = "SMARTSTORE"
# The provider API group the packet's AI-use guide gives for product registration, lookup and the
# category/attribute reads. No narrower permission name is invented from it (kickoff note).
PRODUCT_GROUP = "상품"
# AUTH.md §2.1: M2 uses the SELF token type only. Switching to SELLER needs its own ADR (§2.2).
AUTH_MODE = "SELF"
PROVIDER_HOST = "api.commerce.naver.com"
BASE_URL = f"https://{PROVIDER_HOST}/external"


class EndpointId(StrEnum):
    # ADOPTED for M2 (EM §4).
    SMARTSTORE_AUTH_TOKEN = "SMARTSTORE_AUTH_TOKEN"
    SMARTSTORE_SELLER_ACCOUNT = "SMARTSTORE_SELLER_ACCOUNT"
    # ADOPTED for M5 PR-D: the two product read-backs.
    SMARTSTORE_ORIGIN_PRODUCT_READ_V2 = "SMARTSTORE_ORIGIN_PRODUCT_READ_V2"
    SMARTSTORE_CHANNEL_PRODUCT_READ_V2 = "SMARTSTORE_CHANNEL_PRODUCT_READ_V2"
    # ADOPTED by the CREATE adoption slice and the SEARCH positive-only reconcile slice. IMAGE
    # UPLOAD was adopted by the earlier bounded M5 amendment. The metadata reads remain NOT_ADOPTED
    # (see ADOPTION_GAPS); adoption does not grant LIVE authority to any of them.
    SMARTSTORE_PRODUCT_CREATE_V2 = "SMARTSTORE_PRODUCT_CREATE_V2"
    SMARTSTORE_PRODUCT_IMAGE_UPLOAD = "SMARTSTORE_PRODUCT_IMAGE_UPLOAD"
    SMARTSTORE_PRODUCT_SEARCH = "SMARTSTORE_PRODUCT_SEARCH"
    # ADOPTED by the DELETE slice (ADR-0018 §3.5): the removal of one ICBM-confirmed listing.
    SMARTSTORE_PRODUCT_DELETE_V2 = "SMARTSTORE_PRODUCT_DELETE_V2"
    SMARTSTORE_CATEGORY_LIST = "SMARTSTORE_CATEGORY_LIST"
    SMARTSTORE_CATEGORY_READ = "SMARTSTORE_CATEGORY_READ"
    SMARTSTORE_PRODUCT_ATTRIBUTE_LIST = "SMARTSTORE_PRODUCT_ATTRIBUTE_LIST"
    SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES = "SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES"
    SMARTSTORE_STANDARD_OPTIONS = "SMARTSTORE_STANDARD_OPTIONS"
    SMARTSTORE_NOTICE_TYPES = "SMARTSTORE_NOTICE_TYPES"
    SMARTSTORE_NOTICE_TYPE_READ = "SMARTSTORE_NOTICE_TYPE_READ"


class Method(StrEnum):
    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"


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


def product_read_succeeded(status: int, body: object) -> bool:
    """HTTP 200 AND the body parses as a JSON object.

    The packet proves the read-back endpoints and the product structure's fields, not the envelope
    those fields arrive in, so the predicate asserts nothing about the shape. Whether the response
    actually carries the product is decided by the read-back normalizer, which fails closed
    (``readback.py``); a call that passes here is never by itself a confirmation (ADR-0014 §11).
    """
    return status == 200 and isinstance(body, dict)


def product_create_succeeded(status: int, body: object) -> bool:
    """HTTP 200 AND the body parses as a JSON object.

    The predicate asserts nothing about the identifiers. Their documented position and type — the
    top-level ``integer<int64>`` members of the value-level packet 5868542027 (E3) — are read by
    the response contract (``create.py``), which decides between an applied mutation and an
    ``UNKNOWN``: a 200 that passes here is never by itself an applied mutation, a missing or
    malformed identifier is never a proven non-application, and a 2xx is never a registration
    confirmation (ADR-0014 §11).
    """
    return status == 200 and isinstance(body, dict)


def product_delete_succeeded(status: int, body: object) -> bool:
    """HTTP 200 AND the body parses as a JSON object (the documented ``CommonResponse``).

    Anything else — another 2xx, an empty or unparsable body — is never a proven deletion: it is
    ``UNKNOWN``, never resent, and only an origin-product read-back resolves it (ADR-0018 §3.5).
    """
    return status == 200 and isinstance(body, dict)


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _search_channel(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and _int(entry.get("originProductNo"))
        and _int(entry.get("channelProductNo"))
        and isinstance(entry.get("channelServiceType"), str)
        and isinstance(entry.get("sellerManagementCode"), str)
    )


def product_search_succeeded(status: int, body: object) -> bool:
    """HTTP 200 AND a JSON object carrying every documented S2 member on the raw page:
    ``contents`` an array of objects, each with an integer ``originProductNo`` and a
    ``channelProducts`` array whose entries each carry integer ``originProductNo`` and
    ``channelProductNo`` and string ``channelServiceType`` and ``sellerManagementCode``; and
    ``page``/``size``/``totalElements``/``totalPages`` integers and ``first``/``last`` booleans.

    It is checked here, on the raw body, because retention drops an empty array and a null — a
    missing member must never read as an empty one. Values, ranges and consistency are read by the
    search contract (``search.py``); nothing a page says is ever remote absence or a CREATE
    authorization (ADR-0014 §17.2, §28.2).
    """
    if status != 200 or not isinstance(body, dict):
        return False
    contents = body.get("contents")
    return (
        isinstance(contents, list)
        and all(
            isinstance(item, dict)
            and _int(item.get("originProductNo"))
            and isinstance(item.get("channelProducts"), list)
            and all(_search_channel(entry) for entry in item["channelProducts"])
            for item in contents
        )
        and all(_int(body.get(key)) for key in ("page", "size", "totalElements", "totalPages"))
        and all(isinstance(body.get(key), bool) for key in ("first", "last"))
    )


def image_upload_succeeded(status: int, body: object) -> bool:
    """HTTP 200 plus the documented ``images[].url`` response shape.

    Exactly-one correspondence is enforced by the adapter's promotion boundary. The endpoint
    predicate only establishes that a typed response can be handed to it.
    """
    if status != 200 or not isinstance(body, dict):
        return False
    images = body.get("images")
    return isinstance(images, list) and all(
        isinstance(image, dict) and isinstance(image.get("url"), str) for image in images
    )


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
    # ADR-0011 §3 / ADR-0014 §15, deny-by-default: the only URL query keys this endpoint may send
    # and the only response leaves that may be retained. Empty means none at all.
    safe_query_keys: frozenset[str] = frozenset()
    retained_response_fields: frozenset[str] = frozenset()

    @property
    def path_params(self) -> frozenset[str]:
        """The placeholders of the path template; the caller must supply exactly these."""
        return frozenset(_PATH_PARAM.findall(self.path))


_PATH_PARAM = re.compile(r"\{([A-Za-z][A-Za-z0-9]*)\}")

# The fields a product read-back may keep. Only names the 2.89.0 packet proves: the product's own
# ``name``, ``salePrice`` and ``stockQuantity``, the seller-owned codes, and image ``url`` values.
# Everything else in a response is dropped before anything is hashed or stored.
_PRODUCT_READ_FIELDS = frozenset(
    {"name", "salePrice", "stockQuantity", "sellerManagementCode", "sellerManagerCode", "url"}
)
# The origin-product read additionally keeps the two published-state leaves its documented 200
# response carries (Commerce API 2.90.0; Issue #89 5911962320): ``originProduct.statusType`` and
# ``smartstoreChannelProduct.channelProductDisplayStatusType``. The channel-product read response
# is not captured, so it keeps the narrower set.
_ORIGIN_READ_FIELDS = _PRODUCT_READ_FIELDS | {"statusType", "channelProductDisplayStatusType"}
_IMAGE_UPLOAD_FIELDS = frozenset({"url"})
# The CREATE success response: the provider identifiers the official evidence names, plus the same
# safe product leaves a read-back may keep — the response echoes ``originProduct``, the product
# data SmartStore stored. ``windowChannelProductNo`` is retained although ICBM never emits
# ``windowChannelProduct``: a provider identity that did come back is never dropped
# (ADR-0014 §28.2). Everything else is removed before anything is hashed, stored or logged.
_PRODUCT_CREATE_FIELDS = _PRODUCT_READ_FIELDS | {
    "originProductNo",
    "smartstoreChannelProductNo",
    "windowChannelProductNo",
}
# The product search (S2): the candidate identities, the channel type, the seller code a positive
# reconcile compares, and the pagination envelope. Product names and every other leaf are dropped.
_PRODUCT_SEARCH_FIELDS = frozenset(
    {
        "originProductNo",
        "channelProductNo",
        "channelServiceType",
        "sellerManagementCode",
        "page",
        "size",
        "totalElements",
        "totalPages",
        "first",
        "last",
    }
)
# Category and notice metadata: only the identifiers and labels a selection is made of. The packet
# names 카테고리 and 상품군 reads but no response field, so nothing else survives retention.


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
    # ---- M5 PR-D (packet 5746489554). Group 상품; bearer per the current auth page.
    EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2: EndpointContract(
        endpoint_id=EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2,
        method=Method.GET,
        path="/v2/products/origin-products/{originProductNo}",
        content_type=None,
        requires_bearer=True,
        connect_timeout_s=5.0,
        read_timeout_s=15.0,
        redirect=RedirectPolicy.NO_FOLLOW,
        required_groups=frozenset({PRODUCT_GROUP}),
        mutating=False,
        success_predicate=product_read_succeeded,
        predicate_revision="m5d-origin-read-r1",
        retained_response_fields=_ORIGIN_READ_FIELDS,
    ),
    EndpointId.SMARTSTORE_CHANNEL_PRODUCT_READ_V2: EndpointContract(
        endpoint_id=EndpointId.SMARTSTORE_CHANNEL_PRODUCT_READ_V2,
        method=Method.GET,
        path="/v2/products/channel-products/{channelProductNo}",
        content_type=None,
        requires_bearer=True,
        connect_timeout_s=5.0,
        read_timeout_s=15.0,
        redirect=RedirectPolicy.NO_FOLLOW,
        required_groups=frozenset({PRODUCT_GROUP}),
        mutating=False,
        success_predicate=product_read_succeeded,
        predicate_revision="m5d-channel-read-r1",
        retained_response_fields=_PRODUCT_READ_FIELDS,
    ),
    # ---- M5 CREATE adoption slice (official Commerce API 2.89.0; the field-level record in
    # documents/evidence/marketplace-apis/PRODUCT_CREATE.md § SmartStore). Adoption is not a call
    # and not LIVE authority: execution stays DRY_RUN, the send-time safety stack refuses every
    # mutation, and an UNKNOWN outcome is never resent (ADR-0014 §28; ADR-0018 §6.1).
    EndpointId.SMARTSTORE_PRODUCT_CREATE_V2: EndpointContract(
        endpoint_id=EndpointId.SMARTSTORE_PRODUCT_CREATE_V2,
        method=Method.POST,
        path="/v2/products",
        content_type="application/json",
        requires_bearer=True,
        # ICBM policy, never a provider fact: no endpoint-specific timeout is documented. The
        # same bounds the other adopted mutation uses; a read timeout is UNKNOWN, never a failure.
        connect_timeout_s=5.0,
        read_timeout_s=30.0,
        # EM §11 / ERRORS.md §10.6, §17: a 308 is never followed for a mutation.
        redirect=RedirectPolicy.NO_FOLLOW,
        required_groups=frozenset({PRODUCT_GROUP}),
        mutating=True,
        success_predicate=product_create_succeeded,
        predicate_revision="m5-create-r1",
        retained_response_fields=frozenset(_PRODUCT_CREATE_FIELDS),
    ),
    # ---- M5 SEARCH positive-only reconcile slice (Issue #89 architect resolution 5904349289,
    # S1-S4; official Commerce API 2.89.0). A read, never a mutation: it may only ever recover a
    # presence candidate, which the origin read-back must still prove (ADR-0014 §28.2).
    EndpointId.SMARTSTORE_PRODUCT_SEARCH: EndpointContract(
        endpoint_id=EndpointId.SMARTSTORE_PRODUCT_SEARCH,
        method=Method.POST,
        path="/v1/products/search",
        content_type="application/json",
        requires_bearer=True,
        # ICBM policy, never a provider fact: the read bounds the other product reads use.
        connect_timeout_s=5.0,
        read_timeout_s=15.0,
        redirect=RedirectPolicy.NO_FOLLOW,
        required_groups=frozenset({PRODUCT_GROUP}),
        mutating=False,
        success_predicate=product_search_succeeded,
        predicate_revision="m5-search-r1",
        retained_response_fields=_PRODUCT_SEARCH_FIELDS,
    ),
    # ---- The DELETE slice (ADR-0018 §3.5; documents/evidence/marketplace-apis/PRODUCT_DELETE.md
    # § SmartStore). The path is the adopted origin read's, with the documented method. A deletion
    # is a mutation: execution, the brake, the exact DELETE grant and the send-time stack decide,
    # and an UNKNOWN is never resent. Nothing of the response is retained.
    EndpointId.SMARTSTORE_PRODUCT_DELETE_V2: EndpointContract(
        endpoint_id=EndpointId.SMARTSTORE_PRODUCT_DELETE_V2,
        method=Method.DELETE,
        path="/v2/products/origin-products/{originProductNo}",
        content_type=None,
        requires_bearer=True,
        # ICBM policy, never a provider fact: the bounds of the other adopted mutations.
        connect_timeout_s=5.0,
        read_timeout_s=30.0,
        redirect=RedirectPolicy.NO_FOLLOW,
        required_groups=frozenset({PRODUCT_GROUP}),
        mutating=True,
        success_predicate=product_delete_succeeded,
        predicate_revision="m5-delete-r1",
    ),
    # ---- M5 IMAGE UPLOAD amendment (official Commerce API 2.89.0, 2026-09-15).
    EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD: EndpointContract(
        endpoint_id=EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD,
        method=Method.POST,
        path="/v1/product-images/upload",
        content_type="multipart/form-data",
        requires_bearer=True,
        connect_timeout_s=5.0,
        read_timeout_s=30.0,
        redirect=RedirectPolicy.NO_FOLLOW,
        required_groups=frozenset({PRODUCT_GROUP}),
        mutating=True,
        success_predicate=image_upload_succeeded,
        predicate_revision="m5-image-upload-r1",
        retained_response_fields=_IMAGE_UPLOAD_FIELDS,
    ),
}

NOT_ADOPTED: frozenset[EndpointId] = frozenset(EndpointId) - frozenset(ADOPTED)

# Why each remaining endpoint is still NOT_ADOPTED after the 2.89.0 packet. Adoption needs the
# whole transport contract — kickoff §1 lists the request media type and the query contract among
# the facts that must come from the official documentation — and the packet stops short of them.
_NO_RESPONSE_CONTRACT = (
    "the packet proves the endpoint exists but names no response field, so a deny-by-default"
    " retention profile would keep nothing and no typed metadata could be derived from a read"
)

ADOPTION_GAPS: Mapping[EndpointId, str] = {
    EndpointId.SMARTSTORE_PRODUCT_ATTRIBUTE_LIST: (
        "카테고리별 조회 needs a category query key the packet does not name; under a"
        " deny-by-default allow-list the endpoint could only ever be called without it"
    ),
    EndpointId.SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES: (
        "카테고리별 조회 needs a category query key the packet does not name"
    ),
    EndpointId.SMARTSTORE_STANDARD_OPTIONS: (
        "카테고리별 표준형 옵션 조회 needs a category query key the packet does not name"
    ),
    # The metadata reads: the packet names the endpoints but no response field of either, so a
    # deny-by-default retention profile would keep nothing and no typed metadata could be derived.
    # Category, attribute, option and notice metadata therefore stay operator-reviewed Settings
    # data (PR-C ``RegistrationMetadataSource``) until a response contract is proven.
    EndpointId.SMARTSTORE_CATEGORY_LIST: _NO_RESPONSE_CONTRACT,
    EndpointId.SMARTSTORE_CATEGORY_READ: _NO_RESPONSE_CONTRACT,
    EndpointId.SMARTSTORE_NOTICE_TYPES: _NO_RESPONSE_CONTRACT,
    EndpointId.SMARTSTORE_NOTICE_TYPE_READ: _NO_RESPONSE_CONTRACT,
}


class EndpointNotAdoptedError(LookupError):
    """A NOT_ADOPTED or unknown endpoint was requested. Raised before any network I/O."""


def resolve(endpoint_id: object) -> EndpointContract:
    """The contract of an ADOPTED endpoint. Anything else fails locally (EM §2)."""
    contract = ADOPTED.get(endpoint_id) if isinstance(endpoint_id, EndpointId) else None
    if contract is None:
        raise EndpointNotAdoptedError(f"{endpoint_id!s} is not an ADOPTED SmartStore endpoint")
    return contract


# ---------------------------------------------------------------- the wire identity (ADR-0018)

# Other spellings that name this same provider host. None is known: any other host — a proxy, an
# alias, an IP literal — is refused by the server-owned host rule, never given a replay key.
HOST_ALIASES: Mapping[str, str] = {}


def canonical_host() -> str:
    """The provider's one canonical host, for the server-owned host rule (ADR-0018 §3.4)."""
    return PROVIDER_HOST


def wire_identity(endpoint_id: EndpointId) -> tuple[str, str, str]:
    """The raw ``(method, host, path)`` one ADOPTED endpoint's request goes to.

    The path is the one actually on the wire — the base path plus the endpoint path — because
    the ASSET replay key is the wire boundary (ADR-0018 §3.4). The server normalizes it.
    """
    contract = resolve(endpoint_id)
    return str(contract.method), PROVIDER_HOST, urlsplit(BASE_URL).path + contract.path


# ---------------------------------------------------------------- endpoint-mapping revision

SMARTSTORE_ENDPOINT_MAPPING_REVISION = "m5-delete-r1"

# ADR-0014 §15: the safe query-key / retained-response-field profile is versioned together with
# the mapping revision, so it is part of the fingerprint below and cannot drift on its own.
SAFE_RETENTION_PROFILE_VERSION = "smartstore-safe-retention/v1"

# M2 instructions §5.3: each revision is bound to the fingerprint of the permission-relevant
# registry content it names. Changing that content changes the fingerprint, and CI fails until a
# new revision and its fingerprint are added here in the same PR. Superseded revisions stay, so a
# stored evidence revision can still be resolved.
MAPPING_FINGERPRINTS: Mapping[str, str] = {
    "m2-connect-r1": "17d3dfe97b2f4a6c5e0b363c9c83cba014c018619c6197d54cfa395277016ca9",
    "m5-register-r1": "fbf07a8784557b45c5e282464a20d2b41534642a34076e1827b656fba8710648",
    # Filled from ``mapping_fingerprint()`` in the same reviewed change.
    "m5-image-upload-r1": "4717169646fe3b53725a4c31ff93e15d657dc6a85b29295a8d955969f090d02b",
    "m5-create-r1": "635b1d1c281e2a05f9467a5362ff2f6395318d77c8c969b370a84c8384b1bfea",
    # The CREATE reconciliation to the value-level packet 5868542027 (E1-E3): statusType SALE is
    # projected and the top-level int64 success identifiers are read. None of that is
    # permission-relevant registry content, so the fingerprint is the one m5-create-r1 names; the
    # revision still moves, because the mapping it names now reads and sends differently.
    "m5-create-r2": "635b1d1c281e2a05f9467a5362ff2f6395318d77c8c969b370a84c8384b1bfea",
    # The SEARCH positive-only reconcile slice adopts POST /v1/products/search and its retention
    # profile (Issue #89 5904349289).
    "m5-search-r1": "0d5934ab1430543016b3c31a8948635d11124711cf058c77e2eb74335bd9a73b",
    # The published-state read slice: the origin-product read retains the two documented status
    # leaves (Issue #89 5911962320). No endpoint is adopted or re-adopted by it.
    "m5-published-state-r1": "c20e9369999e1c38db61df874d47ea354670f41f6180cec7bce0dc36e8ced138",
    # The DELETE slice adopts DELETE /v2/products/origin-products/{originProductNo} (ADR-0018 §3.5).
    "m5-delete-r1": "6b23aeaf7d315c87fc97ddc7c431e4e6e7b3ae2e09074f87191012ed2ae0869f",
}


def mapping_fingerprint() -> str:
    """SHA-256 of the permission-relevant registry content: provider, auth mode, base URL, the
    safe-retention profile version, and each endpoint's id, adoption, method, path, media type,
    bearer requirement, required groups, mutability and safe query/retention allow-lists."""
    content = {
        "provider": PROVIDER,
        "auth_mode": AUTH_MODE,
        "base_url": BASE_URL,
        "safe_retention_profile_version": SAFE_RETENTION_PROFILE_VERSION,
        "adopted": [
            {
                "id": c.endpoint_id.value,
                "method": c.method.value,
                "path": c.path,
                "content_type": c.content_type,
                "requires_bearer": c.requires_bearer,
                "required_groups": sorted(c.required_groups),
                "mutating": c.mutating,
                "safe_query_keys": sorted(c.safe_query_keys),
                "retained_response_fields": sorted(c.retained_response_fields),
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
