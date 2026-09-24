"""Shared synthetic setup. Every test runs with the network refused (ADR-0017 §13: zero reads)."""

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

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

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE_PAGES = ("simple_on_sale", "simple_sold_out", "optioned")


class NetworkRefused(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise NetworkRefused("the Phase B prototype makes no network call")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield


def page(name: str) -> str:
    return (FIXTURES / "pages" / f"{name}.html").read_text("utf-8")


def expected(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / "expected" / f"{name}.json").read_text("utf-8"))
    return loaded


def scope_for(html: str) -> OperatorScope:
    """The operator confirms exactly the regions the generic rules detected."""
    confirmed = tuple(
        b for b, region in detect_regions(html) if region is not RegionClass.NAVIGATION
    )
    return OperatorScope("operator:synthetic", "2026-09-25T00:00:00Z", confirmed)


def sample(name: str) -> ValidationSample:
    html = page(name)
    return capture_sample(html, scope_for(html), expected(name))


@pytest.fixture
def store() -> ProfileStore:
    return ProfileStore()


def synthetic_bundle(store: ProfileStore) -> Bundle:
    simple = store.put(profiles.ptr("simple", optioned=False))
    optioned = store.put(profiles.ptr("optioned", optioned=True))
    return store.bundle(store.put(profiles.epr([simple, optioned])))


@pytest.fixture
def bundle(store: ProfileStore) -> Bundle:
    return synthetic_bundle(store)


@pytest.fixture
def samples() -> list[ValidationSample]:
    return [sample(name) for name in SAMPLE_PAGES]


@pytest.fixture
def negatives() -> dict[str, str]:
    return {"login": page("login"), "not_product": page("not_product")}


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
