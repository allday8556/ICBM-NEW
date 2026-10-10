"""The configured sites of this build, each bound to its platform template (ADR-0030 §1, §3, §4).

Every ``integrations/suppliers/sites/<supplier_key>.json`` is parsed, checked against its
template's closed lists, and bound. A file that fails is reported and its site is not offered; it
never stops the application, and it never stops another site.

A site's extraction identity is the pair (template revision, site revision): the revision string
joins them as ``<template>+<site>``, and the fingerprint is the SHA-256 over the template
manifest's fingerprint and the site file's digest. A change to either therefore advances it, so a
revision records exactly which rules produced it (ADR-0030 §4, PT-05).
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from integrations.suppliers.base import SupplierDefinition
from integrations.suppliers.collection import SupplierCollection
from integrations.suppliers.extraction import MANIFEST_NAME, read_manifest
from integrations.suppliers.platforms import PlatformTemplate
from integrations.suppliers.platforms.cafe24 import TEMPLATE as CAFE24
from integrations.suppliers.platforms.godomall import TEMPLATE as GODOMALL
from integrations.suppliers.site_config import (
    INTERVAL_LIMITS,
    SiteConfig,
    SiteConfigError,
    parse_site,
)

SITE_DIRECTORY = Path(__file__).resolve().parent / "sites"
PLATFORM_DIRECTORY = Path(__file__).resolve().parent / "platforms"
CAPTURE_POLICY_FILE = "browser_capture_policy.json"
TEMPLATES: Mapping[str, PlatformTemplate] = MappingProxyType(
    {CAFE24.platform: CAFE24, GODOMALL.platform: GODOMALL}
)
# Supplier keys that have their own package, and their storefront hosts; a site takes neither.
PACKAGED_SUPPLIERS = frozenset({"kmretail"})
PACKAGED_HOSTS = frozenset({"kmretail.co.kr"})


@dataclass(frozen=True)
class BoundSite:
    config: SiteConfig
    template: PlatformTemplate
    definition: SupplierDefinition
    collection: SupplierCollection
    extractor_revision: str
    extractor_fingerprint: str


def template_problems(site: SiteConfig, template: PlatformTemplate) -> list[str]:
    """Why ``site`` asks for something its template does not offer; empty if it does not."""
    problems = []
    if unknown := sorted(set(site.label_overrides) - set(template.label_slots)):
        problems.append(f"label slots the template does not have: {unknown}")
    if unknown := sorted(set(site.region_overrides) - set(template.region_slots)):
        problems.append(f"region slots the template does not have: {unknown}")
    # ADR-0032 §2: a label names one price role at most.
    both = set(site.label_overrides.get("purchase_price", ())) & set(
        site.label_overrides.get("list_price", ())
    )
    if both:
        problems.append(f"a label is both a purchase and a list price: {sorted(both)}")
    if site.product_path_form not in template.path_forms:
        problems.append(f"the template has no product path form {site.product_path_form!r}")
    raised = sorted(
        key
        for key, value in site.limits.items()
        if (
            value < template.default_limits[key]
            if key in INTERVAL_LIMITS
            else value > template.default_limits[key]
        )
    )
    if raised and site.limit_decision is None:
        problems.append(f"limits above the template default need the owner's decision: {raised}")
    return problems


def template_fingerprint(template: PlatformTemplate, platforms: Path = PLATFORM_DIRECTORY) -> str:
    manifest = read_manifest(platforms / template.platform / MANIFEST_NAME)
    if manifest.revision != template.revision:
        raise ValueError(f"the {template.platform} manifest and template disagree on the revision")
    return manifest.fingerprint


def extraction_identity(
    site: SiteConfig, template: PlatformTemplate, platforms: Path = PLATFORM_DIRECTORY
) -> tuple[str, str]:
    fingerprint = hashlib.sha256(
        f"{template_fingerprint(template, platforms)}\n{site.digest}\n".encode("ascii")
    ).hexdigest()
    return f"{template.revision}+{site.revision}", fingerprint


def bind_sites(
    directory: Path = SITE_DIRECTORY,
    templates: Mapping[str, PlatformTemplate] = TEMPLATES,
    platforms: Path = PLATFORM_DIRECTORY,
) -> tuple[tuple[BoundSite, ...], tuple[str, ...]]:
    """Every valid configured site, bound, and why each invalid file was left out."""
    bound: list[BoundSite] = []
    problems: list[str] = []
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    hosts = set(PACKAGED_HOSTS)
    for path in paths:
        try:
            site = parse_site(path.read_bytes(), path.stem)
        except (OSError, SiteConfigError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        if site.supplier_key in PACKAGED_SUPPLIERS:
            problems.append(f"{path.name}: {site.supplier_key} has its own package")
            continue
        if site.storefront_host in hosts:
            # One host, one supplier: a capture is attributed by its host (ADR-0030 §6).
            problems.append(f"{path.name}: storefront host {site.storefront_host} is taken")
            continue
        template = templates.get(site.platform)
        if template is None:
            problems.append(f"{path.name}: no template for platform {site.platform!r}")
            continue
        if found := template_problems(site, template):
            problems.extend(f"{path.name}: {problem}" for problem in found)
            continue
        try:
            revision, fingerprint = extraction_identity(site, template, platforms)
            binding = template.bind(site, revision)
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        hosts.add(site.storefront_host)
        bound.append(
            BoundSite(
                config=site,
                template=template,
                definition=binding.definition,
                collection=binding.collection,
                extractor_revision=revision,
                extractor_fingerprint=fingerprint,
            )
        )
    return tuple(bound), tuple(problems)


def capture_policy_bytes(site: BoundSite, platforms: Path = PLATFORM_DIRECTORY) -> bytes:
    """The site's browser capture policy (ADR-0019 §5), derived from its template's: the same
    topology, with the site's key, host and revision, and the site's description region."""
    path = platforms / site.template.platform / CAPTURE_POLICY_FILE
    document = json.loads(path.read_text("utf-8"))
    default_detail = site.template.region_slots.get("detail")
    detail = site.config.region_overrides.get("detail")
    if default_detail is not None and detail is not None:
        document["allowed_regions"] = [
            {"by": detail.by, "token": detail.token}
            if region == {"by": default_detail.by, "token": default_detail.token}
            else region
            for region in document["allowed_regions"]
        ]
    document["supplier_key"] = site.config.supplier_key
    document["host"] = site.config.storefront_host
    document["revision"] = f"{site.template.capture_revision}.{site.config.revision}"
    return json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
