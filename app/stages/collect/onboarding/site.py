"""An onboarded supplier's ``SiteConfig``, built from its database record (ADR-0036 §1, §4).

A repository site's configuration is a reviewed file; an onboarded site's is its identity, its
current words revision, its confirmed hosts and its state. Both become the same ``SiteConfig``, so
a template binds either unchanged (``integrations.suppliers.sites.bind_site``).

- **Words only** (OB-03). The words revision becomes the label overrides. An onboarded site moves
  no region, lowers no limit and declares no seller-code convention: the template's defaults hold
  (OB-06).
- **Hosts** (OB-01, §9). The storefront is the bootstrap host while it stays confirmed; the image
  hosts are the confirmed image hosts; the login hosts and any other confirmed storefront host are
  the extra egress hosts. A disabled host is in none of them.
- **Identity** (ADR-0036 §4, PT-05). The site revision is ``<key>-w<words seq>`` (``-w0`` before
  the first words revision: the template's own words), so the extraction identity reads
  ``<template revision>+<key>-w<seq>``. The digest is the SHA-256 of everything the configuration
  was built from, so its fingerprint moves with any of them.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from integrations.suppliers.platforms import PlatformTemplate
from integrations.suppliers.site_config import SiteConfig, SiteStatus

ONBOARDED_SCHEMA: Final = "icbm-onboarded-site/v1"
SITE_KEY_PREFIX: Final = "site-"


class OnboardingState(StrEnum):
    DRAFT = "DRAFT"
    RECON = "RECON"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class HostRole(StrEnum):
    STOREFRONT = "storefront"
    LOGIN = "login"
    IMAGE = "image"


# ADR-0036 §3, §7: the allowed edges of the state log.
ALLOWED_TRANSITIONS: Final[Mapping[OnboardingState, frozenset[OnboardingState]]] = MappingProxyType(
    {
        OnboardingState.DRAFT: frozenset({OnboardingState.RECON, OnboardingState.DISABLED}),
        OnboardingState.RECON: frozenset({OnboardingState.ACTIVE, OnboardingState.DISABLED}),
        OnboardingState.ACTIVE: frozenset({OnboardingState.RECON, OnboardingState.DISABLED}),
        OnboardingState.DISABLED: frozenset(),
    }
)


@dataclass(frozen=True)
class OnboardedHost:
    host: str
    role: HostRole
    # The trial that observed it; ``None`` only for the bootstrap storefront host (OB-01).
    observed_trial_id: str | None
    confirmed_by: str
    confirmed_at: datetime


@dataclass(frozen=True)
class WordsRevision:
    seq: int
    words: Mapping[str, tuple[str, ...]]
    fingerprint: str
    authored_by: str
    authored_at: datetime


@dataclass(frozen=True)
class OnboardingRecord:
    """One onboarded supplier as it stands now: its identity and the newest of each log."""

    supplier_key: str
    display_name: str
    origin: str
    # The origin's host: the bootstrap storefront host (OB-01).
    storefront_host: str
    template: str
    created_at: datetime
    state: OnboardingState
    state_seq: int
    words: WordsRevision | None
    # Every confirmed host that is not disabled, by (role, host).
    hosts: tuple[OnboardedHost, ...]

    @property
    def words_seq(self) -> int:
        return 0 if self.words is None else self.words.seq

    def hosts_of(self, role: HostRole) -> frozenset[str]:
        return frozenset(h.host for h in self.hosts if h.role is role)


def words_json(words: Mapping[str, tuple[str, ...]]) -> str:
    """The canonical JSON of a words revision: slots sorted, each slot's words in order."""
    return json.dumps(
        {slot: list(words[slot]) for slot in sorted(words)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def words_fingerprint(words: Mapping[str, tuple[str, ...]]) -> str:
    return hashlib.sha256(words_json(words).encode("utf-8")).hexdigest()


def path_form(template: PlatformTemplate) -> str:
    """The template's one product-path form. An onboarded site has no file to name a form in, so
    a template with more than one cannot bind one until it declares which applies."""
    forms = sorted(template.path_forms)
    if len(forms) != 1:
        raise ValueError(f"the {template.platform} template has no single product path form")
    return forms[0]


def site_revision(supplier_key: str, words_seq: int) -> str:
    return f"{supplier_key}-w{words_seq}"


def onboarded_site(
    record: OnboardingRecord,
    template: PlatformTemplate,
    *,
    words: Mapping[str, tuple[str, ...]] | None = None,
    require_storefront: bool = True,
) -> SiteConfig:
    """The site configuration of ``record`` (``words`` overrides its current words, for checking a
    words revision before it is saved). Raises ``ValueError`` when the bootstrap storefront host is
    disabled, unless ``require_storefront`` is false (a words check binds nothing)."""
    labels = dict(record.words.words if record.words is not None else {})
    if words is not None:
        labels = dict(words)
    storefronts = record.hosts_of(HostRole.STOREFRONT)
    if require_storefront and record.storefront_host not in storefronts:
        raise ValueError("the bootstrap storefront host is disabled")
    status = SiteStatus.ACTIVE if record.state is OnboardingState.ACTIVE else SiteStatus.RECON
    seq = record.words_seq
    source = {
        "schema": ONBOARDED_SCHEMA,
        "supplier_key": record.supplier_key,
        "display_name": record.display_name,
        "origin": record.origin,
        "template": record.template,
        "state": record.state.value,
        "words_seq": seq,
        "words": json.loads(words_json(labels)),
        "hosts": sorted([h.host, h.role.value] for h in record.hosts),
    }
    digest = hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return SiteConfig(
        supplier_key=record.supplier_key,
        display_name=record.display_name,
        platform=record.template,
        base_url=record.origin,
        storefront_host=record.storefront_host,
        image_hosts=record.hosts_of(HostRole.IMAGE),
        extra_egress_hosts=record.hosts_of(HostRole.LOGIN)
        | (storefronts - {record.storefront_host}),
        product_path_form=path_form(template),
        label_overrides=MappingProxyType({slot: tuple(w) for slot, w in labels.items()}),
        region_overrides=MappingProxyType({}),
        limits=MappingProxyType({}),
        limit_decision=None,
        seller_code_convention=None,
        status=status,
        recon_record=None,
        revision=site_revision(record.supplier_key, seq),
        digest=digest,
    )
