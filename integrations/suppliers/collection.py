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
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from integrations.suppliers.base import SupplierProfile, SupplierTransport

_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
# ADR-0010 §4 (ruling on Q2): at least 60 s between real reads of the same product.
MINIMUM_SAME_PRODUCT_INTERVAL_S = 60.0


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
