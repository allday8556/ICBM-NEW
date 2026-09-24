"""Shared synthetic test helpers. The pytest fixtures themselves live in ``tests/conftest.py``.

Kept outside ``conftest.py`` so a test imports exactly one module object for them.
"""

import json
from pathlib import Path
from typing import Any

from prototypes.adaptive_collector.capture import (
    OperatorScope,
    RegionClass,
    ValidationSample,
    capture_sample,
    detect_regions,
)
from prototypes.adaptive_collector.fixtures import profiles, synthetic_hooks
from prototypes.adaptive_collector.hooks import HookManifest, fingerprint_files
from prototypes.adaptive_collector.profile import Bundle, ProfileStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_PAGES = ("simple_on_sale", "simple_sold_out", "optioned")
# Test-level knowledge of which synthetic template each page is, used only to check matching.
# It is never part of an operator expectation: a template key is profile-owned, not a source fact.
TEMPLATE_OF = {"simple_on_sale": "simple", "simple_sold_out": "simple", "optioned": "optioned"}
PRODUCT_BOUNDARY = "product-detail"


class NetworkRefused(AssertionError):
    pass


def page(name: str) -> str:
    return (FIXTURES / "pages" / f"{name}.html").read_text("utf-8")


def expected(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / "expected" / f"{name}.json").read_text("utf-8"))
    return loaded


def scope_for(html: str, boundary: str = PRODUCT_BOUNDARY) -> OperatorScope:
    """The operator approves one product boundary and confirms the regions detected inside it."""
    confirmed = tuple(
        b for b, region in detect_regions(html, boundary) if region is not RegionClass.NAVIGATION
    )
    return OperatorScope("operator:synthetic", "2026-09-25T00:00:00Z", boundary, confirmed)


def sample(name: str) -> ValidationSample:
    html = page(name)
    return capture_sample(html, scope_for(html), expected(name))


def synthetic_bundle(store: ProfileStore) -> Bundle:
    simple = store.put(profiles.ptr("simple", optioned=False))
    optioned = store.put(profiles.ptr("optioned", optioned=True))
    return store.bundle(store.put(profiles.epr([simple, optioned])))


def hook_manifest(revision: str = synthetic_hooks.HOOK_REVISION) -> HookManifest:
    source = FIXTURES / "synthetic_hooks.py"
    return HookManifest(
        supplier_key=profiles.HOOKED_SUPPLIER,
        hook_revision=revision,
        hook_fingerprint=fingerprint_files([source], FIXTURES),
        hooks=synthetic_hooks.HOOKS,
    )


def hooked_bundle(store: ProfileStore, revision: str = synthetic_hooks.HOOK_REVISION) -> Bundle:
    template = store.put(profiles.ptr("simple", optioned=False, supplier=profiles.HOOKED_SUPPLIER))
    return store.bundle(store.put(profiles.hooked_epr([template], revision)))
