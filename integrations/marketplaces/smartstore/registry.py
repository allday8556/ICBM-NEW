"""The SmartStore endpoint registry (docs/platforms/smartstore/ENDPOINT_MATRIX.md; M2 PR-A, M5
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

**CREATE adoption (ADR-0020 §4 slice 1).** ``SMARTSTORE_PRODUCT_CREATE_V2`` is now ADOPTED from the
official wire-contract evidence recorded in ENDPOINT_MATRIX.md §4.2 (Issue #89 reviews 5768199984
and 5768247290, release 2.89.0): the method, the path, the bearer, the ``상품`` group, the JSON
request media type, and HTTP 200 answered with the documented identifiers and the stored
``originProduct`` data. The timeouts, the redirect policy and the success predicate are ICBM policy
over those facts, never provider facts; the predicate requires that whole documented success
document, so a 200 carrying less stays an ambiguous outcome rather than a reported success.

The provider's **absent** idempotency is recorded below rather than assumed, and it is bound to the
never-resend rule of ADR-0014 §28: an ``UNKNOWN`` CREATE is never resent, and this registry grants
no retry budget for it. ``SMARTSTORE_PRODUCT_SEARCH`` stays NOT_ADOPTED — the positive-only
reconcile path is its own later slice — and the provider-evidence verdict stays ``INSUFFICIENT``.

Adoption is not LIVE authority and not a session: the application remains DRY_RUN/provider-zero,
``product_registration.write`` stays ``UNVERIFIED``, the real canary stays ``BLOCKED``, and no
application route invokes the upload or the CREATE caller.
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
    # ADOPTED by the bounded M5 IMAGE UPLOAD amendment and by the ADR-0020 §4 CREATE adoption
    # slice. Neither adoption grants LIVE authority. Search and the metadata reads remain
    # NOT_ADOPTED (see ADOPTION_GAPS).
    SMARTSTORE_PRODUCT_CREATE_V2 = "SMARTSTORE_PRODUCT_CREATE_V2"
    SMARTSTORE_PRODUCT_IMAGE_UPLOAD = "SMARTSTORE_PRODUCT_IMAGE_UPLOAD"
    SMARTSTORE_PRODUCT_SEARCH = "SMARTSTORE_PRODUCT_SEARCH"
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


# The CREATE success response's own documented members (review 5768199984, API docs v2.68.0), in
# the order a result reports them. The channel numbers travel with the origin-product number so
# neither provider identity is lost (ADR-0014 §28.2); ``originProduct`` is the product data
# SmartStore says it stored.
CHANNEL_PRODUCT_NO_FIELDS = ("smartstoreChannelProductNo", "windowChannelProductNo")
FIELD_ORIGIN_PRODUCT_RESULT = "originProduct"


def provider_product_no(value: object) -> str | None:
    """One documented provider product number as a usable identifier string, or ``None``.

    The reviews prove the identifier names, never their JSON type, so both a number and a string
    are accepted and normalized to the string form the adopted read-back path takes. Nothing else
    — a bool, a float, an empty or blank string, a non-positive number — is an identifier.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        return value.strip() or None
    return None


def product_create_succeeded(status: int, body: object) -> bool:
    """EM §4.3: HTTP 200 AND the whole documented CREATE success document.

    Review 5768199984 proves the documented success status (``200``) and that a successful CREATE
    answers with ``originProductNo``, the channel-product numbers **and** the ``originProduct``
    data SmartStore stored. The predicate requires that whole documented shape, because a 200 that
    carries less is not the documented success response: application cannot be read off it, so it
    fails the predicate and becomes an ambiguous outcome (EM §4.3, §9), never a reported success
    with a missing identity or a missing stored result.

    - ``originProductNo`` is the identity the adopted origin read-back is performed by.
    - The channel-product numbers are required as a **family**, not individually: which channels a
      store has is not a fact any review proves — a seller without 쇼핑윈도 has no
      ``windowChannelProductNo`` — so at least one documented channel number must be usable.
      Demanding both would turn a documented success into a false ``UNKNOWN``, and an ``UNKNOWN``
      CREATE is never resent and is not resolvable by absence (ADR-0014 §28).
    - ``originProduct`` must be a non-empty JSON object: that is the stored result data the
      read-back comparison of ADR-0014 §11 is set against, and an absent or empty one carries none.

    Passing this predicate is **not** registration success. ADR-0014 §11 confirms a registration
    only through read-back and Snapshot comparison; this predicate only establishes that a typed
    result can be formed.
    """
    if status != 200 or not isinstance(body, dict):
        return False
    if provider_product_no(body.get("originProductNo")) is None:
        return False
    if not any(provider_product_no(body.get(name)) for name in CHANNEL_PRODUCT_NO_FIELDS):
        return False
    stored = body.get(FIELD_ORIGIN_PRODUCT_RESULT)
    return isinstance(stored, dict) and bool(stored)


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
_IMAGE_UPLOAD_FIELDS = frozenset({"url"})
# The CREATE success response: the documented identifiers (review 5768199984, API docs v2.68.0) and
# the product data SmartStore says it stored, kept under the same deny-by-default product profile
# as a read-back so a CREATE response can be compared with one without a second retention rule.
_CREATE_IDENTIFIER_FIELDS = frozenset({"originProductNo", *CHANNEL_PRODUCT_NO_FIELDS})
_CREATE_RESULT_FIELDS = _CREATE_IDENTIFIER_FIELDS | _PRODUCT_READ_FIELDS
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
        retained_response_fields=_PRODUCT_READ_FIELDS,
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
    # ---- M5 CREATE adoption slice (ADR-0020 §4; EM §4.2, reviews 5768199984 / 5768247290).
    # Provider facts: POST /v2/products, bearer with AUTH_MODE=SELF, API group 상품, an
    # application/json request body, HTTP 200 with the documented identifiers. ICBM policy: the
    # timeouts, NO_FOLLOW (a 308 would replay a mutation body, ERRORS.md §10.6), the success
    # predicate and the deny-by-default retention profile. No query key is safe: the CREATE
    # carries none.
    EndpointId.SMARTSTORE_PRODUCT_CREATE_V2: EndpointContract(
        endpoint_id=EndpointId.SMARTSTORE_PRODUCT_CREATE_V2,
        method=Method.POST,
        path="/v2/products",
        content_type="application/json",
        requires_bearer=True,
        connect_timeout_s=5.0,
        # A mutation's read bound is generous on purpose: cutting a CREATE short manufactures the
        # very ambiguity ADR-0014 §28 can never resolve by absence. It is still bounded, and an
        # expiry is UNKNOWN, never a rejection (ERRORS.md §14.3, §15.2).
        read_timeout_s=30.0,
        redirect=RedirectPolicy.NO_FOLLOW,
        required_groups=frozenset({PRODUCT_GROUP}),
        mutating=True,
        success_predicate=product_create_succeeded,
        predicate_revision="m5-product-create-r1",
        retained_response_fields=_CREATE_RESULT_FIELDS,
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
    EndpointId.SMARTSTORE_PRODUCT_SEARCH: (
        "existence only: the packet does not prove the request schema, so no strong duplicate key"
        " (sellerManagementCode, barcode/GTIN) and no normalized-name filter is proven; duplicate"
        " lookup stays fail-closed (lookup.py)"
    ),
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


# ---------------------------------------------------------------- mutation replay facts

# ADR-0014 §17.2 and §28, ADR-0018 §6.1: the CREATE adoption records the provider's **actual**
# idempotency instead of assuming one. Review 5768247290 proves there is none — no idempotency
# key, no request-correlation key, no replay rule and no duplicate-prevention guarantee — so the
# endpoint is bound to the never-resend rule: this registry grants it no automatic retry, and an
# ``UNKNOWN`` outcome is reconciled by positive evidence only, never resent (M5-08, M5-33).
CREATE_PROVIDER_IDEMPOTENCY = "NONE_DOCUMENTED"
CREATE_AUTOMATIC_RETRY_BUDGET = 0


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

SMARTSTORE_ENDPOINT_MAPPING_REVISION = "m5-product-create-r1"

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
    # The ADR-0020 §4 CREATE adoption slice: SMARTSTORE_PRODUCT_CREATE_V2 moves to ADOPTED with
    # its own method, path, media type, groups, mutability and retention profile.
    "m5-product-create-r1": "635b1d1c281e2a05f9467a5362ff2f6395318d77c8c969b370a84c8384b1bfea",
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
