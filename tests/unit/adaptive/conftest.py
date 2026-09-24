"""Every Adaptive core test runs with the network refused (ADR-0017 P1: offline, zero reads)."""

import socket
from collections.abc import Iterator

import pytest

from app.collect.adaptive.validation import NegativeClass
from tests.adaptive_support import Negatives, negative_pages


class NetworkRefused(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise NetworkRefused("the Adaptive core makes no network call")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield


@pytest.fixture
def negatives() -> Negatives:
    pages = negative_pages()
    assert set(pages) == set(NegativeClass)
    return pages
