"""A scripted supplier shop for serving the real COLLECT submit path (Gate 1 G1-E).

The application is served with the synthetic ``fakeshop`` collection of ``scripts.m3collect`` and
a transport that answers by product number, so one test can drive every durable outcome through
the real job, run store, revision store and materializer:

- ``/product/sample/<n>/`` — an ordinary product page declaring product ``n``: RECORDED;
- product ``0`` — a page that declares no product number: NO_REVISION;
- product ``9009`` — CONNECT's session is refused: a non-retryable AUTH failure, FAILED;
- product ``7001`` — the read waits until the test releases it: the run stays PENDING.

Nothing here can reach a network: the gateway knows no host and sends nothing.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from app.collect.collection import RegisteredCollection
from app.config import AppConfig
from app.core.errors import AuthError
from app.main import create_app
from integrations.suppliers.collection import CollectionProfile, DocumentView, ReadKind
from integrations.suppliers.transport.collection import RequestBudget
from scripts.m3collect.fake_shop import (
    DETAIL_BYTES,
    DETAIL_URL,
    EXTRACTOR_FINGERPRINT,
    EXTRACTOR_REVISION,
    HOST,
    PRIMARY_BYTES,
    PRIMARY_URL,
    SUPPLIER_KEY,
    FakeGateway,
    StubSessions,
    collection,
    document,
    page,
)
from tests.conftest import LOCAL

NO_REVISION_ID = "0"
FAILED_ID = "9009"
HELD_ID = "7001"
HELD_TIMEOUT_S = 30.0


def product_url(number: str) -> str:
    return f"https://{HOST}/product/sample/{number}/"


def registered() -> RegisteredCollection:
    return RegisteredCollection(
        collection=collection(),
        extractor_revision=EXTRACTOR_REVISION,
        extractor_fingerprint=EXTRACTOR_FINGERPRINT,
    )


@dataclass
class ScriptedShop(FakeGateway):
    """Answers each product page by the number in its URL, as the module docstring lists."""

    release: threading.Event = field(default_factory=threading.Event)
    reads: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.images.update({PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES})

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView:
        budget.reserve(kind, url)
        number = [part for part in url.split("/") if part][-1]
        self.reads.append(number)
        self.document_reads += 1
        if number == FAILED_ID:
            raise AuthError("SUPPLIER_SESSION_EXPIRED", "the supplier refused the stored session")
        if number == HELD_ID:
            self.release.wait(HELD_TIMEOUT_S)
        return document(page(product_id="" if number == NO_REVISION_ID else number))


@contextmanager
def served(config: AppConfig, shop: ScriptedShop) -> Iterator[TestClient]:
    """The real application, collecting from the scripted shop. A held read is released before
    the application stops, so its worker never waits out the hold."""
    app = create_app(
        config,
        collection_gateway=shop,
        collection_sessions=StubSessions(),
        collections=(registered(),),
    )
    try:
        with TestClient(app, base_url=LOCAL) as client:
            try:
                yield client
            finally:
                shop.release.set()
    finally:
        shop.release.set()


__all__ = [
    "FAILED_ID",
    "HELD_ID",
    "NO_REVISION_ID",
    "SUPPLIER_KEY",
    "ScriptedShop",
    "product_url",
    "registered",
    "served",
]
