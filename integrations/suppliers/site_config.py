"""The site configuration of one supplier on a platform template (ADR-0030 §3).

A site configuration is reviewed repository data: one JSON file, schema ``icbm-supplier-site/v1``,
under ``integrations/suppliers/sites/``. It names a supplier, its platform template, its hosts and
paths, the words and regions its skin uses, its limits and its status. It holds no code, no
regular expression, no credential and no fact value (ADR-0030 PT-01): every value is a plain word,
a host name or a number, checked here against a closed set of keys.

This module only parses and validates. Which label slots, region slots, path forms and limits a
platform accepts is the template's own declaration, checked when a site is bound to it
(``integrations/suppliers/sites.py``).
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from integrations.suppliers.base import SUPPLIER_KEY

SCHEMA = "icbm-supplier-site/v1"

_HOST = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)
_REVISION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,23}$")
_SLOT = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_RECORD = re.compile(r"^documents/acceptance/suppliers/[a-z0-9_-]+\.md$")
# A seller-code convention is a fixed prefix, then the source product id, then a fixed suffix.
_CONVENTION = re.compile(r"^[A-Za-z0-9_-]{0,16}\{source_product_id\}[A-Za-z0-9_-]{0,16}$")
_WORD_LIMIT = 40
_WORDS_PER_SLOT = 12

_REQUIRED = frozenset(
    {
        "schema",
        "supplier_key",
        "display_name",
        "platform",
        "base_url",
        "storefront_host",
        "image_hosts",
        "product_path_form",
        "status",
        "revision",
    }
)
_OPTIONAL = frozenset(
    {
        "extra_egress_hosts",
        "label_overrides",
        "region_overrides",
        "limits",
        "limit_decision",
        "seller_code_convention",
        "recon_record",
    }
)
LIMIT_KEYS = frozenset(
    {
        "max_image_refs",
        "max_image_bytes",
        "max_image_requests_per_run",
        "max_new_image_bytes_per_run",
        "same_product_interval_s",
        "max_discovered_links",
        "max_queue_products",
        "min_queue_interval_s",
        "issue_ttl_s",
    }
)
# A larger interval is the stricter one; for every other limit a smaller number is.
INTERVAL_LIMITS = frozenset({"same_product_interval_s", "min_queue_interval_s"})


class SiteStatus(StrEnum):
    """ADR-0030 §7: a ``RECON`` site collects, but its products cannot be prepared for a
    marketplace; only a reviewed PR citing its acceptance record makes it ``ACTIVE``."""

    RECON = "RECON"
    ACTIVE = "ACTIVE"


class SiteConfigError(ValueError):
    """The file is not a valid site configuration. The site is not offered; nothing else stops."""


@dataclass(frozen=True)
class SiteRegion:
    """Where a skin puts a region the template looks for: an ``id`` or a ``class`` token."""

    by: str
    token: str


@dataclass(frozen=True)
class SiteConfig:
    supplier_key: str
    display_name: str
    platform: str
    base_url: str
    storefront_host: str
    image_hosts: frozenset[str]
    extra_egress_hosts: frozenset[str]
    product_path_form: str
    label_overrides: Mapping[str, tuple[str, ...]]
    region_overrides: Mapping[str, SiteRegion]
    limits: Mapping[str, float]
    limit_decision: str | None
    seller_code_convention: str | None
    status: SiteStatus
    recon_record: str | None
    revision: str
    # SHA-256 of the file's bytes with CRLF read as LF: the site half of the extraction identity.
    digest: str = field(repr=False)

    @property
    def active(self) -> bool:
        return self.status is SiteStatus.ACTIVE


def _fail(message: str) -> SiteConfigError:
    return SiteConfigError(message)


def _text(value: Any, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _fail(f"{what} is not valid")
    return value


def _hosts(value: Any, what: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise _fail(f"{what} is a list of host names")
    hosts = [_text(host, _HOST, what) for host in value]
    if len(set(hosts)) != len(hosts):
        raise _fail(f"{what} names a host twice")
    return frozenset(hosts)


def _word(value: Any, what: str) -> str:
    """A plain word or phrase: no pattern syntax, no markup, nothing but what a page would print."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _WORD_LIMIT
        or any(ch in value for ch in "<>{}[]()\\|*+?^$")
    ):
        raise _fail(f"{what} holds a plain word only")
    return value


def _labels(value: Any) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise _fail("label_overrides is an object of slot to words")
    labels: dict[str, tuple[str, ...]] = {}
    for slot, words in value.items():
        _text(slot, _SLOT, "a label slot")
        if not isinstance(words, list) or not 1 <= len(words) <= _WORDS_PER_SLOT:
            raise _fail(f"label slot {slot} lists 1 to {_WORDS_PER_SLOT} words")
        labels[slot] = tuple(_word(word, f"label slot {slot}") for word in words)
    return MappingProxyType(labels)


def _regions(value: Any) -> Mapping[str, SiteRegion]:
    if not isinstance(value, dict):
        raise _fail("region_overrides is an object of slot to region")
    regions: dict[str, SiteRegion] = {}
    for slot, region in value.items():
        _text(slot, _SLOT, "a region slot")
        if not isinstance(region, dict) or set(region) != {"by", "token"}:
            raise _fail(f"region slot {slot} is {{by, token}}")
        if region["by"] not in ("id", "class"):
            raise _fail(f"region slot {slot} is found by id or class only")
        regions[slot] = SiteRegion(region["by"], _text(region["token"], _TOKEN, slot))
    return MappingProxyType(regions)


def _limits(value: Any) -> Mapping[str, float]:
    if not isinstance(value, dict) or not set(value) <= LIMIT_KEYS:
        raise _fail(f"limits names only {sorted(LIMIT_KEYS)}")
    limits: dict[str, float] = {}
    for key, number in value.items():
        if isinstance(number, bool) or not isinstance(number, int | float) or number <= 0:
            raise _fail(f"limit {key} is a positive number")
        if key not in INTERVAL_LIMITS and not isinstance(number, int):
            raise _fail(f"limit {key} is a whole number")
        limits[key] = number
    return MappingProxyType(limits)


def parse_site(raw: bytes, file_stem: str) -> SiteConfig:
    """Validate one site file strictly. ``file_stem`` is the file's name without ``.json``."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _fail("the file is not UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise _fail("the file is one JSON object")
    keys = set(document)
    if missing := sorted(_REQUIRED - keys):
        raise _fail(f"missing keys: {missing}")
    if unknown := sorted(keys - _REQUIRED - _OPTIONAL):
        raise _fail(f"unknown keys: {unknown}")
    if document["schema"] != SCHEMA:
        raise _fail(f"schema is {SCHEMA}")
    key = document["supplier_key"]
    if not isinstance(key, str) or not SUPPLIER_KEY.fullmatch(key) or key != file_stem:
        raise _fail("supplier_key is a supplier key equal to the file name")
    display_name = _word(document["display_name"], "display_name")
    platform = _text(document["platform"], _SLOT, "platform")
    base_url = document["base_url"]
    if (
        not isinstance(base_url, str)
        or not base_url.startswith("https://")
        or base_url.endswith("/")
        or urlsplit(base_url).path
        or urlsplit(base_url).query
    ):
        raise _fail("base_url is an https origin without a path or a trailing slash")
    storefront = _text(document["storefront_host"], _HOST, "storefront_host")
    if urlsplit(base_url).hostname != storefront:
        raise _fail("storefront_host is the host of base_url")
    try:
        status = SiteStatus(document["status"])
    except ValueError:
        raise _fail("status is RECON or ACTIVE") from None
    recon_record = document.get("recon_record")
    if recon_record is not None:
        _text(recon_record, _RECORD, "recon_record")
    if status is SiteStatus.ACTIVE and recon_record is None:
        raise _fail("an ACTIVE site names its reconnaissance record (ADR-0030 §3)")
    convention = document.get("seller_code_convention")
    if convention is not None:
        _text(convention, _CONVENTION, "seller_code_convention")
    decision = document.get("limit_decision")
    if decision is not None:
        _word(decision, "limit_decision")
    image_hosts = _hosts(document["image_hosts"], "image_hosts")
    if not image_hosts:
        raise _fail("image_hosts names at least one observed host")
    return SiteConfig(
        supplier_key=key,
        display_name=display_name,
        platform=platform,
        base_url=base_url,
        storefront_host=storefront,
        image_hosts=image_hosts,
        extra_egress_hosts=_hosts(document.get("extra_egress_hosts", []), "extra_egress_hosts"),
        product_path_form=_text(document["product_path_form"], _SLOT, "product_path_form"),
        label_overrides=_labels(document.get("label_overrides", {})),
        region_overrides=_regions(document.get("region_overrides", {})),
        limits=_limits(document.get("limits", {})),
        limit_decision=decision,
        seller_code_convention=convention,
        status=status,
        recon_record=recon_record,
        revision=_text(document["revision"], _REVISION, "revision"),
        digest=hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
    )
