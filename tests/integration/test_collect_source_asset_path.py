"""The durable path from a fetched source image to a read-back revision (Issue #52 ruling
5701574419, stage 1).

Everything here runs against a migrated database and a fake provider. No supplier, image host or
network of any kind is contacted: the images are bytes the test made itself.
"""

import hashlib
import struct
from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.collect.assets import SourceAssetStore
from app.collect.facts import FactsStatus, FieldStatus, ImageIssue, ImageRole
from app.collect.imagedecode import HeaderImageDecoder
from app.collect.readback import SourceTruthReadback
from app.collect.revisions import ProductFactsRevisionStore
from app.collect.sourceassets import FetchedImage, SourceAssetRecorder, UnfetchedImage
from app.config import AppConfig
from app.db.database import Database
from tests.collect_support import collected
from tests.support import FakeClock

pytestmark = pytest.mark.integration

HOST = "img.shop.example"


def png(width: int, height: int, marker: bytes = b"") -> bytes:
    """A PNG header the decoder reads, with a tail that makes these bytes unique."""
    ihdr = b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + b"\x00\x00\x00\x00" + marker


@pytest.fixture
def database(config: AppConfig) -> Iterator[Database]:
    db = Database(config.database_url)
    try:
        yield db
    finally:
        db.dispose()


@pytest.fixture
def assets(config: AppConfig, database: Database) -> SourceAssetStore:
    return SourceAssetStore(config.source_assets_dir, database, HeaderImageDecoder(), FakeClock())


@pytest.fixture
def recorder(assets: SourceAssetStore) -> SourceAssetRecorder:
    return SourceAssetRecorder(assets)


@pytest.fixture
def revisions(database: Database) -> ProductFactsRevisionStore:
    return ProductFactsRevisionStore(database, FakeClock())


def fetched(content: bytes, *, role: ImageRole = ImageRole.REPRESENTATIVE, ordinal: int = 0):
    return FetchedImage(
        role=role,
        ordinal=ordinal,
        host=HOST,
        provenance=f".gallery img:nth-of-type({ordinal + 1})",
        content=content,
    )


# ---------------------------------------------------------------- the asset path


def test_the_bytes_are_kept_exactly_as_they_arrived(
    recorder: SourceAssetRecorder, assets: SourceAssetStore
) -> None:
    # Trailing padding included: bytes a transform might trim are part of the source asset.
    body = png(1920, 1080, b"original source bytes") + b"\x00\x00\x00"
    (reference,) = recorder.record([fetched(body)])
    assert reference.status is FieldStatus.CONFIRMED and reference.issue is None
    assert reference.sha256 == hashlib.sha256(body).hexdigest()
    read_back = assets.read(reference.sha256)
    assert read_back == body, "byte for byte, not re-encoded, resized or trimmed"
    assert len(read_back) == len(body) and read_back.endswith(b"\x00\x00\x00")


def test_the_checksum_comes_from_the_bytes_and_the_size_from_decoding_them(
    recorder: SourceAssetRecorder, assets: SourceAssetStore
) -> None:
    body = png(800, 600, b"sized")
    (reference,) = recorder.record([fetched(body)])
    stored = assets.get(reference.sha256 or "")
    assert stored is not None
    assert stored.sha256 == hashlib.sha256(body).hexdigest()
    assert (stored.width, stored.height) == (800, 600), "the original dimensions, not a thumbnail"
    assert stored.mime_type == "image/png"
    assert stored.byte_size == len(body)


def test_the_same_bytes_are_stored_once(
    recorder: SourceAssetRecorder, assets: SourceAssetStore
) -> None:
    body = png(100, 100, b"same")
    first, second = recorder.record([fetched(body), fetched(body, ordinal=1)])
    assert first.sha256 == second.sha256
    assert assets.path(first.sha256 or "").is_file()
    stored = list((assets.path(first.sha256 or "").parent.parent).rglob("*"))
    assert [p for p in stored if p.is_file()] == [assets.path(first.sha256 or "")]


def test_different_bytes_behind_one_locator_are_two_assets(
    recorder: SourceAssetRecorder, assets: SourceAssetStore
) -> None:
    # The content decides, never the URL: a locator that answers with different bytes later is two
    # distinct source assets, and neither overwrites the other.
    locator = f"https://{HOST}/p/1.png"
    older, newer = png(10, 10, b"older"), png(10, 10, b"newer")
    first = recorder.record([replace(fetched(older), locator=locator)])[0]
    second = recorder.record([replace(fetched(newer), locator=locator)])[0]
    assert first.locator == second.locator == locator
    assert first.sha256 != second.sha256, "one URL, two contents, two assets"
    assert assets.read(first.sha256 or "") == older
    assert assets.read(second.sha256 or "") == newer
    files = sorted(
        p for p in (assets.path(first.sha256 or "").parent.parent).rglob("*") if p.is_file()
    )
    assert len(files) == 2


def test_bytes_the_decoder_cannot_read_are_not_stored(
    recorder: SourceAssetRecorder, assets: SourceAssetStore
) -> None:
    (reference,) = recorder.record([fetched(b"this is not an image")])
    assert reference.status is FieldStatus.REVIEW_REQUIRED
    assert reference.issue is ImageIssue.UNSUPPORTED_FORMAT
    assert reference.sha256 is None, "no checksum is invented for bytes we cannot describe"
    assert not list(assets.path("0" * 64).parent.parent.rglob("*.tmp"))


def test_an_image_that_was_never_fetched_keeps_its_reason(recorder: SourceAssetRecorder) -> None:
    (reference,) = recorder.record(
        [
            UnfetchedImage(
                role=ImageRole.DETAIL,
                ordinal=3,
                host=HOST,
                provenance="#prdDetail img:nth-of-type(4)",
                issue=ImageIssue.BUDGET_EXHAUSTED,
            )
        ]
    )
    assert reference.status is FieldStatus.REVIEW_REQUIRED
    assert reference.issue is ImageIssue.BUDGET_EXHAUSTED
    assert (reference.sha256, reference.ordinal) == (None, 3), "its position is not renumbered"


def test_the_source_order_and_role_survive_a_reference_that_could_not_be_stored(
    recorder: SourceAssetRecorder,
) -> None:
    references = recorder.record(
        [
            fetched(png(4, 4, b"a"), role=ImageRole.REPRESENTATIVE, ordinal=0),
            fetched(b"broken", role=ImageRole.DETAIL, ordinal=1),
            fetched(png(4, 4, b"c"), role=ImageRole.DETAIL, ordinal=2),
        ]
    )
    assert [(r.role, r.ordinal) for r in references] == [
        (ImageRole.REPRESENTATIVE, 0),
        (ImageRole.DETAIL, 1),
        (ImageRole.DETAIL, 2),
    ]
    assert [r.status for r in references] == [
        FieldStatus.CONFIRMED,
        FieldStatus.REVIEW_REQUIRED,
        FieldStatus.CONFIRMED,
    ]


# ---------------------------------------------------------------- revision and read-back


def test_a_revision_is_appended_and_never_rewritten(
    recorder: SourceAssetRecorder, revisions: ProductFactsRevisionStore
) -> None:
    images = recorder.record([fetched(png(300, 300, b"rev"))])
    first = revisions.append(collected(images=images))
    # The same collection again: unchanged evidence still becomes a new immutable revision.
    second = revisions.append(collected(images=images))
    assert (first.sequence, second.sequence) == (1, 2)
    assert first.revision_id != second.revision_id
    history = revisions.history(first.supplier_key, first.source_product_id)
    assert [r.sequence for r in history] == [1, 2]
    assert revisions.get(first.revision_id) is not None, "the earlier revision is still there"
    assert first.fingerprints_intact() and second.fingerprints_intact()


def test_what_the_database_holds_is_what_the_api_returns(
    client: TestClient,
    recorder: SourceAssetRecorder,
    revisions: ProductFactsRevisionStore,
    assets: SourceAssetStore,
) -> None:
    body = png(640, 480, b"readback")
    images = recorder.record(
        [
            fetched(body),
            UnfetchedImage(
                role=ImageRole.DETAIL,
                ordinal=1,
                host=HOST,
                provenance="#prdDetail img:nth-of-type(2)",
                issue=ImageIssue.FETCH_FAILED,
            ),
        ]
    )
    stored = revisions.append(collected(images=images))
    expected = SourceTruthReadback(revisions, assets).revision(stored.revision_id)

    answer = client.get(f"/api/v1/collect/revisions/{stored.revision_id}")
    assert answer.status_code == 200
    assert answer.json() == expected.model_dump(mode="json"), "DB and API say the same thing"

    served = answer.json()
    assert served["facts_status"] in set(FactsStatus)
    assert served["fingerprints_intact"] is True
    confirmed, review = served["images"]
    assert confirmed["asset"] == {
        "sha256": hashlib.sha256(body).hexdigest(),
        "mime_type": "image/png",
        "byte_size": len(body),
        "width": 640,
        "height": 480,
    }
    assert review["status"] == FieldStatus.REVIEW_REQUIRED
    assert review["issue"] == ImageIssue.FETCH_FAILED
    assert review["asset"] is None, "nothing is filled in for bytes that were never stored"


def test_the_history_reads_back_every_revision_of_one_identity(
    client: TestClient, recorder: SourceAssetRecorder, revisions: ProductFactsRevisionStore
) -> None:
    images = recorder.record([fetched(png(50, 50, b"hist"))])
    first = revisions.append(collected(images=images))
    revisions.append(collected(images=images))
    answer = client.get(
        f"/api/v1/collect/products/{first.supplier_key}/{first.source_product_id}/revisions"
    )
    assert answer.status_code == 200
    served = answer.json()
    assert [r["sequence"] for r in served["revisions"]] == [1, 2]
    assert served["supplier_key"] == first.supplier_key


def test_an_unknown_revision_is_not_found(client: TestClient) -> None:
    assert client.get("/api/v1/collect/revisions/does-not-exist").status_code == 404
