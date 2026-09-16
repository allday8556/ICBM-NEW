"""The durable path from a fetched source image to a read-back revision (Issue #52 ruling
5701574419, stage 1).

Everything here runs against a migrated database and a fake provider. No supplier, image host or
network of any kind is contacted: the images are bytes the test made itself.
"""

import contextlib
import hashlib
import sqlite3
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


def test_the_persisted_validators_reach_the_api_unchanged(
    client: TestClient,
    config: AppConfig,
    recorder: SourceAssetRecorder,
    revisions: ProductFactsRevisionStore,
) -> None:
    """Audit 5226242829 blocker 1.

    A later collection may reuse stored content only after the provider confirms it with these,
    so they are durable evidence. The comparison reads the database directly rather than through
    the mapper the route uses, so a field the mapper silently drops cannot pass.
    """
    etag, last_modified = '"v1-abc"', "Wed, 16 Sep 2026 01:00:00 GMT"
    images = recorder.record(
        [
            replace(
                fetched(png(120, 90, b"validators")),
                http_etag=etag,
                http_last_modified=last_modified,
            ),
            UnfetchedImage(
                role=ImageRole.DETAIL,
                ordinal=1,
                host=HOST,
                provenance="#prdDetail img:nth-of-type(2)",
                issue=ImageIssue.FETCH_FAILED,
                http_etag=None,
                http_last_modified=last_modified,
            ),
        ]
    )
    stored = revisions.append(collected(images=images))

    with contextlib.closing(sqlite3.connect(config.database_path)) as raw:
        in_database = list(
            raw.execute(
                "SELECT role, ordinal, http_etag, http_last_modified FROM"
                " product_facts_image_refs WHERE revision_id = ? ORDER BY ordinal",
                (stored.revision_id,),
            )
        )
    assert in_database == [
        (ImageRole.REPRESENTATIVE, 0, etag, last_modified),
        (ImageRole.DETAIL, 1, None, last_modified),
    ], "the validators are what the database holds"

    served = client.get(f"/api/v1/collect/revisions/{stored.revision_id}").json()["images"]
    assert [
        (row["role"], row["ordinal"], row["http_etag"], row["http_last_modified"]) for row in served
    ] == in_database, "and the API says the same, field for field"


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b"GIF89a" + struct.pack("<HH", 640, 480), "the screen descriptor is cut short"),
        (
            b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 8, 8),
            "the image header chunk and its checksum are not all there",
        ),
        (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 99)
            + b"IHDR"
            + struct.pack(">II", 8, 8)
            + b"\x00" * 17,
            "the image header declares a length the format does not have",
        ),
        (
            b"\xff\xd8\xff\xc0" + struct.pack(">H", 8) + struct.pack(">BHH", 8, 300, 200) + b"\x03",
            "the frame declares three components and carries none of them",
        ),
        (
            b"\xff\xd8\xff\xc0"
            + struct.pack(">H", 17)
            + struct.pack(">BHH", 8, 300, 200)
            + b"\x03"
            + b"\x01\x11\x00",
            "the frame segment runs past the end of the bytes",
        ),
        (
            b"RIFF"
            + struct.pack("<I", 4096)
            + b"WEBP"
            + b"VP8 "
            + struct.pack("<I", 10)
            + b"\x00\x00\x00"
            + b"\x9d\x01\x2a"
            + struct.pack("<HH", 50, 40),
            "the chunk is whole but the container declares more bytes than it carries",
        ),
        (
            b"RIFF"
            + struct.pack("<I", 20)
            + b"WEBP"
            + b"VP8 "
            + struct.pack("<I", 4096)
            + b"\x00" * 8,
            "the chunk declares more bytes than it carries",
        ),
        (
            b"RIFF"
            + struct.pack("<I", 16)
            + b"WEBP"
            + b"VP8 "
            + struct.pack("<I", 10)
            + b"\x00\x00\x00"
            + b"\x9d\x01\x2a"
            + struct.pack("<HH", 50, 40),
            "the bytes are all here but the chunk ends past the container the file declared",
        ),
        (
            # An odd-sized lossless payload: the container left no room for its pad byte.
            b"RIFF"
            + struct.pack("<I", 17)
            + b"WEBP"
            + b"VP8L"
            + struct.pack("<I", 5)
            + b"\x2f"
            + struct.pack("<I", 63 | (31 << 14)),
            "an odd-sized chunk has a pad byte, and the container has no room for it",
        ),
    ],
)
def test_a_header_that_is_not_complete_is_not_a_source_asset(
    recorder: SourceAssetRecorder, assets: SourceAssetStore, body: bytes, why: str
) -> None:
    """Audit 5226242829 blocker 2: a few plausible bytes must not become a CONFIRMED asset whose
    dimensions were read from whatever followed the header."""
    (reference,) = recorder.record([fetched(body)])
    assert reference.status is FieldStatus.REVIEW_REQUIRED, why
    assert reference.issue is ImageIssue.UNSUPPORTED_FORMAT
    assert reference.sha256 is None
    assert assets.get(hashlib.sha256(body).hexdigest()) is None, "nothing was stored"


def test_an_unknown_revision_is_not_found(client: TestClient) -> None:
    assert client.get("/api/v1/collect/revisions/does-not-exist").status_code == 404
