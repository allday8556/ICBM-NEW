"""Supplier-generic COLLECT port (ADR-0010 §3, Issue #52 §2).

This is separate from the CONNECT proof port in ``base.py``. A supplier's collection definition is
site knowledge only: a ``CollectionProfile`` and pure parser functions over an immutable
``DocumentView``. Everything that acts — session use, allowlisting, pacing, the request budget,
fetching, attribution — is common infrastructure in ``integrations.suppliers.transport``. The
CONNECT ``SupplierGateway`` is not widened for COLLECT (a repository rule pins its shape).

A profile holds only values that reconnaissance justified (ADR-0010 §5): the product path form,
the public policy documents, the explicit image hosts and safe query keys, and the frozen limits.
There are no defaults for any of them.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from urllib.parse import urlsplit

from integrations.suppliers.base import SupplierProfile, SupplierTransport

_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
# ADR-0010 §4 (ruling on Q2): at least 60 s between real reads of the same product.
MINIMUM_SAME_PRODUCT_INTERVAL_S = 60.0
# The budget subject prefix of a discovered policy read (a public document linked from a product
# page): a separate, bounded read, never a widening of a profile's fixed ``policy_paths``.
DISCOVERED_POLICY_PREFIX = "discovered:"
# An image host is a distinct origin, so its own robots rules are read before anything is
# requested from it. That read is a POLICY_READ — no new kind and no new cap — with a subject of
# its own so the ledger can allow exactly one of them per approved host, in phase B only
# (Issue #52 ruling 5699776908 §3).
IMAGE_ROBOTS_PREFIX = "image-robots:"
IMAGE_ROBOTS_PATH = "/robots.txt"


class ReadKind(StrEnum):
    PRODUCT_READ = "PRODUCT_READ"
    IMAGE_REQUEST = "IMAGE_REQUEST"
    POLICY_READ = "POLICY_READ"


class FetchIssue(StrEnum):
    """Why an image request produced no usable bytes (ADR-0010 §9, failures)."""

    BAD_CONTENT_TYPE = "BAD_CONTENT_TYPE"
    OVERSIZE = "OVERSIZE"
    FETCH_FAILED = "FETCH_FAILED"


@dataclass(frozen=True)
class CollectionLimits:
    """Frozen after reconnaissance (ADR-0010 §4, §9). No field has a default."""

    max_image_refs: int
    max_image_bytes: int
    max_image_requests_per_run: int
    max_new_image_bytes_per_run: int
    same_product_interval_s: float

    def __post_init__(self) -> None:
        counts = (
            self.max_image_refs,
            self.max_image_bytes,
            self.max_image_requests_per_run,
            self.max_new_image_bytes_per_run,
        )
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in counts):
            raise ValueError("collection limits are positive integers")
        if self.same_product_interval_s < MINIMUM_SAME_PRODUCT_INTERVAL_S:
            raise ValueError("the same-product interval is at least 60 s (ADR-0010 §4)")


@dataclass(frozen=True)
class CollectionProfile:
    supplier: SupplierProfile
    # Regular expression that the whole product path must match (the form reconnaissance proved).
    product_path: str
    # Public policy documents on the storefront host, as exact paths (robots.txt, terms).
    policy_paths: frozenset[str]
    # Explicit image/CDN hosts; never a wildcard.
    image_hosts: frozenset[str]
    # Explicitly safe query keys per host; every other query key is secret-bearing.
    safe_query_keys: Mapping[str, frozenset[str]]
    limits: CollectionLimits
    transport: SupplierTransport = SupplierTransport.HTTP

    def __post_init__(self) -> None:
        if not self.product_path.startswith("/"):
            raise ValueError("the product path form is an absolute path pattern")
        re.compile(self.product_path)
        if any(not p.startswith("/") or "?" in p or "#" in p for p in self.policy_paths):
            raise ValueError("policy documents are exact absolute paths without query")
        hosts = set(self.image_hosts) | set(self.safe_query_keys)
        if any(not _HOST.fullmatch(host) for host in hosts):
            raise ValueError("hosts are explicit lowercase host names, never wildcards")
        if self.transport is not SupplierTransport.HTTP:
            # Browser collection exists only once reconnaissance proves rendering is required.
            raise ValueError("collection over HTTP only")

    @property
    def storefront_host(self) -> str:
        host = urlsplit(self.supplier.base_url).hostname
        assert host is not None
        return host

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset({self.storefront_host}) | self.image_hosts

    def is_product_path(self, path: str) -> bool:
        return re.fullmatch(self.product_path, path) is not None


@dataclass(frozen=True)
class DocumentView:
    """The only thing a collection parser ever sees: an immutable view of one response.

    ``body`` is used in memory only; it is never logged, persisted whole or returned.
    """

    kind: ReadKind
    status: int
    path: str
    location: str | None  # redirect target (path only) for a 3xx response
    content_type: str
    body: str = field(repr=False)


@dataclass(frozen=True)
class ImageResponse:
    status: int  # 200, or 304 for a validated reuse
    content_type: str | None
    etag: str | None
    last_modified: str | None
    content: bytes = field(default=b"", repr=False)

    @property
    def not_modified(self) -> bool:
        return self.status == 304


# ---------------------------------------------------------------- image roles and sampling


class ImageRole(StrEnum):
    """What a supplier's own page says an image reference is for (Issue #52 comment 5696242775).

    A role is decided by the supplier's site knowledge from the captured DOM, never from a host
    name, a path word or document order. ``UNKNOWN`` is the default for a reference no rule
    recognised, and it is never promoted to a product image.
    """

    PRIMARY = "PRIMARY"
    DETAIL = "DETAIL"
    THUMBNAIL = "THUMBNAIL"
    PRODUCT_AUX = "PRODUCT_AUX"
    UI_COMMON = "UI_COMMON"
    UNKNOWN = "UNKNOWN"


# The roles a bounded sample may spend a request on, in the order it spends them, and how many of
# each at most (``None`` = whatever the cap leaves). ``UI_COMMON`` and ``UNKNOWN`` are absent on
# purpose: a common layout asset never displaces product evidence, and a reference no rule
# recognised never fails open into product sampling (comment 5696242775 §2).
SAMPLE_PRIORITY: tuple[tuple["ImageRole", int | None], ...] = (
    (ImageRole.PRIMARY, 1),
    (ImageRole.DETAIL, 5),
    (ImageRole.THUMBNAIL, None),
    (ImageRole.PRODUCT_AUX, None),
)
SAMPLED_ROLES = frozenset(role for role, _ in SAMPLE_PRIORITY)
# Network reconnaissance permission is not asset publication or reuse permission: a sampled image
# proves what the page references, and grants nothing about the asset (comment 5696172833 §7).
RECONNAISSANCE_ONLY = (
    "a bounded reconnaissance read of a referenced asset; it is not a publication, reuse or "
    "licence grant for that asset, and it says nothing about who operates its host"
)
_DIGIT_RUN = re.compile(r"\d+")


@dataclass(frozen=True)
class ImageCandidate:
    """One image reference of a product document, with the role its own DOM supports.

    ``url`` is absolute and may carry a query, so it is used in memory only. Everything that
    reaches a findings file goes through :meth:`audit`, which drops the query and masks digits.
    """

    url: str
    role: ImageRole
    order: int  # the reference's index in the document's own stable source order
    rule: str  # which site-knowledge rule assigned the role, for provenance

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def identity(self) -> str:
        """A stable fingerprint of the reference with its query and fragment dropped, so two
        writings of the same asset are one candidate and no token is ever recorded."""
        parts = urlsplit(self.url)
        source = f"{parts.scheme}://{parts.hostname}{parts.path}"
        return sha256(source.encode("utf-8")).hexdigest()[:16]

    def audit(self, reason: str) -> dict[str, object]:
        return {
            "role": self.role.value,
            "host": self.host,
            "order": self.order,
            "identity": self.identity,
            "path_form": _DIGIT_RUN.sub("{n}", urlsplit(self.url).path)[:80],
            "rule": self.rule,
            "reason": reason,
        }


@dataclass(frozen=True)
class ImageRoleRules:
    """A supplier's own image-role classifier, bound to its extraction identity.

    ``identity`` is the supplier's ``EXTRACTOR_REVISION``: a semantic change to the rules must
    advance it, so a findings file always names the rule set that produced it (comment
    5696242775 §4).
    """

    identity: str
    classify: Callable[[str, str], tuple[ImageCandidate, ...]]


@dataclass(frozen=True)
class ImageSamplePlan:
    """Which references a bounded sample spends its requests on, and why the rest were left."""

    rules: str
    limit: int
    selected: tuple[ImageCandidate, ...]
    excluded: tuple[tuple[ImageCandidate, str], ...]

    def _why(self, slot: int, candidate: ImageCandidate) -> str:
        """Why this reference consumed a sample slot, in the sampler's own terms."""
        rank = sum(1 for earlier in self.selected[: slot + 1] if earlier.role is candidate.role)
        return (
            f"slot {slot + 1} of {self.limit}: "
            f"{candidate.role.value} #{rank} in the page's own reference order"
        )

    def audit(self) -> dict[str, object]:
        left: dict[tuple[str, str, str], int] = {}
        for candidate, reason in self.excluded:
            key = (candidate.role.value, candidate.host, reason)
            left[key] = left.get(key, 0) + 1
        return {
            "rules": self.rules,
            "limit": self.limit,
            "policy": [
                {"role": role.value, "at_most": most if most is not None else "the cap"}
                for role, most in SAMPLE_PRIORITY
            ],
            "selected": [
                candidate.audit(self._why(slot, candidate))
                for slot, candidate in enumerate(self.selected)
            ],
            "not_selected": [
                {"role": role, "host": host, "reason": reason, "count": count}
                for (role, host, reason), count in sorted(left.items())
            ],
            "reconnaissance_only": RECONNAISSANCE_ONLY,
        }


def plan_image_sample(
    candidates: Iterable[ImageCandidate], limit: int, *, rules: str
) -> ImageSamplePlan:
    """Spend at most ``limit`` requests on the strongest product evidence the page offers.

    The order is fixed by :data:`SAMPLE_PRIORITY`: the first primary image, then the detail
    sequence in its own document order, then any thumbnail and product-auxiliary reference that
    the cap still leaves. A reference whose normalized URL was already taken never consumes a
    second slot, and a common or unrecognised reference never consumes one at all.
    """
    if limit < 0:
        raise ValueError("a sample cap is not negative")
    pool = sorted(candidates, key=lambda c: c.order)
    selected: list[ImageCandidate] = []
    excluded: list[tuple[ImageCandidate, str]] = []
    taken: set[str] = set()
    for candidate in pool:
        if candidate.role not in SAMPLED_ROLES:
            excluded.append((candidate, candidate.role.value))
    for role, most in SAMPLE_PRIORITY:
        for candidate in pool:
            if candidate.role is not role:
                continue
            if candidate.identity in taken:
                excluded.append((candidate, "duplicate normalized URL"))
                continue
            room = len(selected) < limit and (
                most is None or sum(1 for chosen in selected if chosen.role is role) < most
            )
            if not room:
                excluded.append((candidate, "lower sampling priority"))
                continue
            taken.add(candidate.identity)
            selected.append(candidate)
    return ImageSamplePlan(
        rules=rules,
        limit=limit,
        selected=tuple(selected),
        excluded=tuple(sorted(excluded, key=lambda pair: pair[0].order)),
    )
