"""Pytest fixtures. Every test runs with the network refused (ADR-0017 §13: zero reads)."""

import socket
from collections.abc import Iterator

import pytest

from prototypes.adaptive_collector.capture import ValidationSample
from prototypes.adaptive_collector.profile import Bundle, ProfileStore
from prototypes.adaptive_collector.testsupport import (
    SAMPLE_PAGES,
    NetworkRefused,
    page,
    sample,
    synthetic_bundle,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise NetworkRefused("the Phase B prototype makes no network call")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield


@pytest.fixture
def store() -> ProfileStore:
    return ProfileStore()


@pytest.fixture
def bundle(store: ProfileStore) -> Bundle:
    return synthetic_bundle(store)


@pytest.fixture
def samples() -> list[ValidationSample]:
    return [sample(name) for name in SAMPLE_PAGES]


@pytest.fixture
def negatives() -> dict[str, str]:
    return {"login": page("login"), "not_product": page("not_product")}
