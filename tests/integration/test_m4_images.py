"""M4 PR-E: derived image lineage, operator image selection and exact-binary QA (Issue #80
kickoff 5738166312; ADR-0013 §9, ADR-0010 §9), on a migrated database through the real services.

No transformation, OCR, AI, supplier or marketplace call happens: derived outputs are synthetic
completed bytes with a declared completed manifest, as the kickoff allows. No campaign root is
opened.
"""

import contextlib
import hashlib
import sqlite3
import struct
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command

from app.audit.models import AuditEventType
from app.audit.service import AuditLog
from app.collect.assets import SourceAssetStore
from app.collect.facts import ImageRole
from app.collect.imagedecode import HeaderImageDecoder
from app.collect.revisions import ProductFactsRevisionStore, StoredRevision
from app.collect.runs import CollectionRunStore
from app.config import AppConfig
from app.container import Container
from app.core.errors import InputValidationError, NotFoundError
from app.db.database import Database, create_sqlite_engine
from app.db.migrate import alembic_config, current_revision, head_revision, upgrade_to_head
from app.products.image_model import (
    CompletedDerivation,
    DerivationInput,
    ExecutionClass,
    ImageAssetKind,
    OperationOutcome,
    OperationRecord,
    QaVerdict,
    SelectedOutput,
    SelectionMoveReason,
    SourceDecision,
    SourceDecisionKind,
)
from app.products.image_store import (
    DerivedImageIntegrityError,
    DerivedImageStore,
    UnsupportedDerivedImageError,
)
from app.products.images import (
    IMAGE_DERIVATION_STALE,
    IMAGE_QA_FAILED,
    IMAGE_QA_MISSING,
    IMAGE_QA_REVIEW_REQUIRED,
    IMAGE_QA_STALE,
    IMAGE_SELECTION_MISSING,
    IMAGE_SELECTION_STALE,
    ImageQaConflictError,
    ProductImageService,
)
from app.products.materialization import ProductMaterializer
from app.products.model import ReadinessStatus
from app.products.pricing_service import ProductPricingService
from app.products.readiness import BASE_READINESS_RULE_VERSION, Readiness
from app.products.store import ProductFoundationStore
from tests.collect_support import REPRESENTATIVE, SOURCE_URL, collected
from tests.product_support import PRODUCT, SUPPLIER, context, count, product, raw
from tests.support import FakeClock

pytestmark = pytest.mark.integration

AT = datetime(2026, 9, 19, 1, 0, tzinfo=UTC)
REP, DET = ImageRole.REPRESENTATIVE, ImageRole.DETAIL
IMAGE_TABLES = (
    "derived_image_artifacts",
    "derived_image_derivations",
    "derived_image_derivation_inputs",
    "derived_image_derivation_roots",
    "image_selection_revisions",
    "image_selection_source_decisions",
    "image_selection_outputs",
    "current_image_selection_moves",
    "image_qa_results",
)
IMAGE_AUDIT = (
    AuditEventType.PRODUCT_DERIVED_IMAGE_RECORDED.value,
    AuditEventType.PRODUCT_IMAGE_SELECTION_RECORDED.value,
    AuditEventType.PRODUCT_CURRENT_IMAGE_SELECTION_MOVED.value,
    AuditEventType.PRODUCT_IMAGE_QA_RECORDED.value,
)


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def png(salt: str, width: int = 40, height: int = 40) -> bytes:
    """A minimal real PNG header the vetted decoder reads; ``salt`` makes the bytes distinct."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"tEXt", b"salt\x00" + salt.encode())
        + _chunk(b"IEND", b"")
    )


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Shop:
    """Source truth with several real stored images: every revision's references are CONFIRMED
    and point at bytes the source-asset store holds."""

    def __init__(self, db: Database, assets_dir: Path) -> None:
        self._db = db
        self._runs = CollectionRunStore(db, FakeClock())
        self._revisions = ProductFactsRevisionStore(db, FakeClock())
        self.assets = SourceAssetStore(assets_dir, db, HeaderImageDecoder(), FakeClock())

    def source(self, salt: str) -> str:
        return self.assets.put(png(salt)).sha256

    def collect(
        self, images: Sequence[tuple[ImageRole, int, str]], price: int = 10000
    ) -> tuple[str, StoredRevision]:
        with self._db.write() as session:
            run_id = self._runs.open(
                session,
                job_id=str(uuid.uuid4()),
                correlation_id="cid-run",
                supplier_key=SUPPLIER,
                source_url=SOURCE_URL,
            )
        self._runs.note_identity(run_id, source_product_id=PRODUCT)
        refs = tuple(
            replace(
                REPRESENTATIVE,
                role=role,
                ordinal=ordinal,
                provenance=f".{role.value.lower()} img:nth-of-type({ordinal + 1})",
                sha256=image,
            )
            for role, ordinal, image in images
        )
        revision = self._revisions.append(
            collected(fields=product(price=price), images=refs, collection_run_id=run_id)
        )
        self._runs.recorded(
            run_id, revision_id=revision.revision_id, facts_status=revision.facts_status
        )
        return run_id, revision


@pytest.fixture
def shop(container: Container, config: AppConfig) -> Shop:
    return Shop(container.db, config.source_assets_dir)


@pytest.fixture
def listing(container: Container, shop: Shop) -> "Listing":
    return Listing.create(container, shop)


class Listing:
    """One Item whose current bound revision has three CONFIRMED source images."""

    def __init__(
        self, container: Container, item: str, revision: StoredRevision, images: list[str]
    ) -> None:
        self.container = container
        self.item = item
        self.revision = revision
        self.images = images  # the REPRESENTATIVE, then two DETAIL sources

    @classmethod
    def create(cls, container: Container, shop: Shop, salts: Sequence[str] = ("r", "d0", "d1")):
        images = [shop.source(salt) for salt in salts]
        run_id, revision = shop.collect(
            [(REP, 0, images[0]), (DET, 0, images[1]), (DET, 1, images[2])]
        )
        result = container.materializer.materialize_run(run_id)
        assert result.item_id is not None
        return cls(container, result.item_id, revision, images)

    @property
    def refs(self) -> list[tuple[ImageRole, int, str]]:
        return [(REP, 0, self.images[0]), (DET, 0, self.images[1]), (DET, 1, self.images[2])]

    def use_all(self) -> list[SourceDecision]:
        return [
            SourceDecision(role, ordinal, image, SourceDecisionKind.USE_SOURCE)
            for role, ordinal, image in self.refs
        ]


def operation(output: bytes, outcome: OperationOutcome = OperationOutcome.COMPLETED):
    return OperationRecord(
        capability="TEST_CROP",
        execution_class=ExecutionClass.LOCAL,
        outcome=outcome,
        input_digest="0" * 64,
        output_digest=sha(output),
        executed_at=AT,
    )


def completed(
    revision_id: str,
    inputs: Sequence[DerivationInput],
    output: bytes,
    *,
    spec: dict[str, object] | None = None,
    outcome: OperationOutcome = OperationOutcome.COMPLETED,
) -> CompletedDerivation:
    return CompletedDerivation(
        output=output,
        validated_source_revision_id=revision_id,
        inputs=tuple(inputs),
        transformation_spec={"crop": [0, 0, 20, 20]} if spec is None else spec,
        transformation_version="test-crop/v1",
        policy_version=None,
        operations=(operation(output, outcome),),
        produced_at=AT,
    )


def source_input(image: str) -> DerivationInput:
    return DerivationInput(ImageAssetKind.SOURCE_ASSET, image)


def derived_input(record: Any) -> DerivationInput:
    return DerivationInput(
        ImageAssetKind.DERIVED_ARTIFACT, record.artifact_sha256, record.derivation_id
    )


def _counts(config: AppConfig) -> dict[str, int]:
    counts = {table: count(config, table) for table in IMAGE_TABLES}
    for event_type in IMAGE_AUDIT:
        counts[event_type] = count(config, "audit_events", "event_type = ?", event_type)
    return counts


def _codes(readiness: Readiness) -> list[tuple[str, str | None]]:
    return [(reason.code, reason.subject) for reason in readiness.reasons]


def _select_all_sources(listing: Listing) -> Any:
    selection, _move = listing.container.images.record_operator_selection(
        listing.item,
        source_revision_id=listing.revision.revision_id,
        decisions=listing.use_all(),
        outputs=[
            SelectedOutput(REP, REP, 0),
            SelectedOutput(DET, DET, 0),
            SelectedOutput(DET, DET, 1),
        ],
        decided_by="operator-1",
    )
    return selection


def _pass_all(listing: Listing, verdict: QaVerdict = QaVerdict.PASS) -> None:
    selection = listing.container.images.current_selection(listing.item)
    assert selection is not None
    for output in selection.outputs:
        listing.container.images.record_qa(
            asset_kind=output.asset_kind,
            sha256=output.sha256,
            derivation_id=output.derivation_id,
            validated_source_revision_id=selection.source_revision_id,
            verdict=verdict,
            decided_by="qa-1",
        )


# ---------------------------------------------------------------- 1–4 source immutability


def _source_state(config: AppConfig) -> tuple[list[tuple[object, ...]], dict[str, bytes]]:
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute("SELECT * FROM source_assets ORDER BY sha256").fetchall()
    files = {
        path.name: path.read_bytes()
        for path in (config.source_assets_dir / "sha256").rglob("*")
        if path.is_file()
    }
    return rows, files


def test_source_assets_are_untouched_and_derived_bytes_live_apart(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 1, 3.
    before = _source_state(config)
    output = png("derived-a")
    record = container.images.record_completed_derivation(
        completed(listing.revision.revision_id, [source_input(listing.images[0])], output),
        decided_by="editor",
    )
    assert _source_state(config) == before
    stored = config.derived_images_dir / "sha256" / record.artifact_sha256[:2]
    assert (stored / record.artifact_sha256).read_bytes() == output
    assert (
        not (config.source_assets_dir / "sha256" / record.artifact_sha256[:2])
        .joinpath(record.artifact_sha256)
        .exists()
    )


def test_a_derivation_never_overwrites_a_source_asset(
    container: Container, config: AppConfig, shop: Shop, listing: Listing
) -> None:
    # Kickoff §J 2: an identity copy of a source's bytes is a derived artifact of its own, stored
    # in the derived namespace; the source row and file stay exactly as they were.
    source_bytes = shop.assets.read(listing.images[0])
    before = _source_state(config)
    record = container.images.record_completed_derivation(
        completed(listing.revision.revision_id, [source_input(listing.images[0])], source_bytes),
        decided_by="editor",
    )
    assert record.artifact_sha256 == listing.images[0]
    assert _source_state(config) == before
    assert count(config, "derived_image_artifacts") == 1
    assert container.images.read_derivation(record.derivation_id).roots == (listing.images[0],)


def test_a_corrupt_or_missing_derived_file_fails_integrity(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 4.
    output = png("derived-a")
    record = container.images.record_completed_derivation(
        completed(listing.revision.revision_id, [source_input(listing.images[0])], output),
        decided_by="editor",
    )
    store = container.images._artifacts
    assert store.read(record.artifact_sha256) == output
    path = store.path(record.artifact_sha256)
    path.write_bytes(png("tampered"))
    with pytest.raises(DerivedImageIntegrityError, match="checksum"):
        store.read(record.artifact_sha256)
    path.unlink()
    with pytest.raises(DerivedImageIntegrityError, match="missing"):
        store.read(record.artifact_sha256)


# ---------------------------------------------------------------- 5–14 derived binary and lineage


def test_the_output_sha_is_computed_from_the_bytes(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 5: a declared digest is only checked, never trusted.
    output = png("derived-a")
    lying = completed(listing.revision.revision_id, [source_input(listing.images[0])], output)
    lying = replace(lying, operations=(replace(operation(output), output_digest="f" * 64),))
    with pytest.raises(InputValidationError, match="digest"):
        container.images.record_completed_derivation(lying, decided_by="editor")
    assert not any(_counts(config).values())
    good = container.images.record_completed_derivation(
        completed(listing.revision.revision_id, [source_input(listing.images[0])], output),
        decided_by="editor",
    )
    assert good.artifact_sha256 == sha(output)


def test_unsupported_bytes_create_nothing(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 6.
    with pytest.raises(UnsupportedDerivedImageError):
        container.images.record_completed_derivation(
            completed(listing.revision.revision_id, [source_input(listing.images[0])], b"text"),
            decided_by="editor",
        )
    assert not any(_counts(config).values())
    assert not any(path.is_file() for path in config.derived_images_dir.rglob("*"))


def test_one_source_to_derived_then_a_chain_and_a_multi_input_derivation(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 7–9: source → A → B, and one derivation of two sources.
    revision = listing.revision.revision_id
    a = container.images.record_completed_derivation(
        completed(revision, [source_input(listing.images[0])], png("a")), decided_by="editor"
    )
    b = container.images.record_completed_derivation(
        completed(revision, [derived_input(a)], png("b")), decided_by="editor"
    )
    assert b.inputs == (derived_input(a),)
    assert b.roots == (listing.images[0],)
    both = container.images.record_completed_derivation(
        completed(
            revision, [source_input(listing.images[1]), source_input(listing.images[2])], png("m")
        ),
        decided_by="editor",
    )
    assert set(both.roots) == {listing.images[1], listing.images[2]}
    assert count(config, "derived_image_derivation_inputs") == 4


def test_a_parent_that_did_not_produce_the_named_artifact_is_refused(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 10, in the service and in the database.
    revision = listing.revision.revision_id
    a = container.images.record_completed_derivation(
        completed(revision, [source_input(listing.images[0])], png("a")), decided_by="editor"
    )
    wrong = DerivationInput(ImageAssetKind.DERIVED_ARTIFACT, sha(png("other")), a.derivation_id)
    with pytest.raises(InputValidationError, match="did not produce"):
        container.images.record_completed_derivation(
            completed(revision, [wrong], png("b")), decided_by="editor"
        )
    with pytest.raises(NotFoundError):
        container.images.record_completed_derivation(
            completed(
                revision,
                [DerivationInput(ImageAssetKind.DERIVED_ARTIFACT, a.artifact_sha256, "missing")],
                png("b"),
            ),
            decided_by="editor",
        )
    with contextlib.closing(raw(config)) as connection:
        connection.execute(
            "INSERT INTO derived_image_derivations VALUES (?, ?, ?, ?, ?, 1, '{}', 'v', NULL,"
            " '[{}]', '2026-09-19 00:00:00', '2026-09-19 00:00:00', 't', 'c')",
            (
                "forged",
                a.artifact_sha256,
                revision,
                "e" * 64,
                '[{"kind": "DERIVED_ARTIFACT", "sha256": "'
                + "d" * 64
                + '", "parent_derivation_id": "'
                + a.derivation_id
                + '"}]',
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO derived_image_derivation_inputs VALUES ('forged', 0,"
                " 'DERIVED_ARTIFACT', NULL, ?, ?)",
                ("d" * 64, a.derivation_id),
            )


def test_an_unknown_or_untraceable_source_is_refused(
    container: Container, config: AppConfig, shop: Shop, listing: Listing
) -> None:
    # Kickoff §J 11–12.
    revision = listing.revision.revision_id
    with pytest.raises(NotFoundError, match="source asset"):
        container.images.record_completed_derivation(
            completed(revision, [source_input("9" * 64)], png("a")), decided_by="editor"
        )
    elsewhere = shop.source("not-in-this-revision")
    with pytest.raises(InputValidationError, match="CONFIRMED image"):
        container.images.record_completed_derivation(
            completed(revision, [source_input(elsewhere)], png("a")), decided_by="editor"
        )
    assert not any(_counts(config).values())


def test_equal_output_from_two_recipes_is_one_artifact_and_two_derivations(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 13.
    revision = listing.revision.revision_id
    output = png("same")
    one = container.images.record_completed_derivation(
        completed(revision, [source_input(listing.images[0])], output, spec={"crop": [0, 0, 1, 1]}),
        decided_by="editor",
    )
    two = container.images.record_completed_derivation(
        completed(revision, [source_input(listing.images[0])], output, spec={"crop": [1, 1, 2, 2]}),
        decided_by="editor",
    )
    assert one.derivation_id != two.derivation_id
    assert one.artifact_sha256 == two.artifact_sha256
    assert count(config, "derived_image_artifacts") == 1
    assert count(config, "derived_image_derivations") == 2


def test_image_history_is_append_only(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 14, 25, 38.
    derivation = container.images.record_completed_derivation(
        completed(listing.revision.revision_id, [source_input(listing.images[0])], png("a")),
        decided_by="editor",
    )
    assert derivation.derivation_id
    _select_all_sources(listing)
    _pass_all(listing)
    with contextlib.closing(raw(config)) as connection:
        for table in IMAGE_TABLES:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0, table
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(f"UPDATE {table} SET rowid = rowid")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(f"DELETE FROM {table}")


# ---------------------------------------------------------------- 15–16 completed operations


@pytest.mark.parametrize("outcome", [OperationOutcome.FAILED, OperationOutcome.PARTIAL])
def test_a_failed_or_partial_operation_leaves_nothing(
    container: Container, config: AppConfig, listing: Listing, outcome: OperationOutcome
) -> None:
    # Kickoff §J 15: no row and no file.
    output = png("failed")
    with pytest.raises(InputValidationError, match="completed operation"):
        container.images.record_completed_derivation(
            completed(
                listing.revision.revision_id,
                [source_input(listing.images[0])],
                output,
                outcome=outcome,
            ),
            decided_by="editor",
        )
    assert not any(_counts(config).values())
    assert not container.images._artifacts.path(sha(output)).exists()


def test_a_retried_completed_derivation_is_one_derivation(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 16: the recipe and its output are its identity.
    manifest = completed(listing.revision.revision_id, [source_input(listing.images[0])], png("a"))
    first = container.images.record_completed_derivation(manifest, decided_by="editor")
    before = _counts(config)
    again = container.images.record_completed_derivation(manifest, decided_by="editor")
    assert again == first
    assert _counts(config) == before


def test_distinct_completed_executions_are_distinct_derivations_of_one_artifact(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # PR #85 review 5254146288 blocker 1: the same recipe and bytes executed LOCAL, then CLOUD,
    # then CLOUD again later, are three derivations of one artifact, each with its own provenance.
    output = png("same")
    local = completed(listing.revision.revision_id, [source_input(listing.images[0])], output)
    cloud = replace(
        local,
        operations=(
            replace(
                operation(output),
                execution_class=ExecutionClass.CLOUD,
                provider="provider-x",
                model="model-y",
            ),
        ),
    )
    later = replace(
        cloud, operations=(replace(cloud.operations[0], executed_at=AT + timedelta(minutes=5)),)
    )
    records = [
        container.images.record_completed_derivation(manifest, decided_by="editor")
        for manifest in (local, cloud, later)
    ]
    assert len({record.derivation_id for record in records}) == 3
    assert {record.artifact_sha256 for record in records} == {sha(output)}
    assert count(config, "derived_image_artifacts") == 1
    assert count(config, "derived_image_derivations") == 3
    read = [container.images.read_derivation(record.derivation_id) for record in records]
    assert [(r.operations[0].execution_class, r.operations[0].provider) for r in read] == [
        (ExecutionClass.LOCAL, None),
        (ExecutionClass.CLOUD, "provider-x"),
        (ExecutionClass.CLOUD, "provider-x"),
    ]
    assert read[1].operations[0].model == "model-y"
    assert [r.operations[0].executed_at for r in read] == [AT, AT, AT + timedelta(minutes=5)]
    # Retrying any one of them exactly is still that one derivation.
    before = _counts(config)
    assert container.images.record_completed_derivation(cloud, decided_by="editor") == records[1]
    assert _counts(config) == before


def test_a_conflicting_file_at_the_content_address_fails_closed_untouched(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # PR #85 review 5254146288 blocker 2: whatever already occupies the address is evidence of
    # broken storage. It is never overwritten, and nothing is recorded.
    output = png("target")
    path = container.images._artifacts.path(sha(output))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"conflicting bytes")
    with pytest.raises(DerivedImageIntegrityError, match="content address"):
        container.images.record_completed_derivation(
            completed(listing.revision.revision_id, [source_input(listing.images[0])], output),
            decided_by="editor",
        )
    assert path.read_bytes() == b"conflicting bytes"
    assert not any(_counts(config).values())


# ---------------------------------------------------------------- 17–28 operator selection


def test_materialization_selects_no_image(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 17.
    assert container.images.current_selection(listing.item) is None
    assert count(config, "image_selection_revisions") == 0
    readiness = container.product_readiness.base_readiness(listing.item)
    assert _codes(readiness) == [(IMAGE_SELECTION_MISSING, "images")]


def test_a_selection_is_an_explicit_operator_decision(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 18: the database admits no other origin, and the service names its operator.
    selection = _select_all_sources(listing)
    assert selection.decision_origin.value == "OPERATOR" and selection.decided_by == "operator-1"
    with pytest.raises(InputValidationError, match="operator"):
        container.images.record_operator_selection(
            listing.item,
            source_revision_id=listing.revision.revision_id,
            decisions=listing.use_all(),
            outputs=[SelectedOutput(REP, REP, 0)],
            decided_by="",
        )
    with contextlib.closing(raw(config)) as connection:
        row = connection.execute(
            "SELECT * FROM image_selection_revisions WHERE selection_revision_id = ?",
            (selection.selection_revision_id,),
        ).fetchone()
        forged = list(row)
        forged[0], forged[4], forged[7] = str(uuid.uuid4()), 2, "SYSTEM"
        with pytest.raises(sqlite3.IntegrityError, match="operator_decision"):
            connection.execute(
                f"INSERT INTO image_selection_revisions VALUES ({', '.join('?' * len(forged))})",
                forged,
            )


def test_every_confirmed_source_image_is_decided_explicitly(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 19–24: use one, replace one, exclude one; order and role are the operator's.
    revision = listing.revision.revision_id
    derived = container.images.record_completed_derivation(
        completed(revision, [source_input(listing.images[1])], png("clean-detail")),
        decided_by="editor",
    )
    decisions = [
        SourceDecision(REP, 0, listing.images[0], SourceDecisionKind.USE_SOURCE),
        SourceDecision(
            DET, 0, listing.images[1], SourceDecisionKind.USE_DERIVED, derived.derivation_id
        ),
        SourceDecision(DET, 1, listing.images[2], SourceDecisionKind.EXCLUDE),
    ]
    selection, move = container.images.record_operator_selection(
        listing.item,
        source_revision_id=revision,
        decisions=decisions,
        outputs=[SelectedOutput(REP, DET, 0), SelectedOutput(DET, REP, 0)],
        decided_by="operator-1",
        reason="swap the cleaned detail to the front",
    )
    assert [
        (o.position, o.role, o.asset_kind, o.sha256, o.derivation_id) for o in selection.outputs
    ] == [
        (0, REP, ImageAssetKind.DERIVED_ARTIFACT, derived.artifact_sha256, derived.derivation_id),
        (1, DET, ImageAssetKind.SOURCE_ASSET, listing.images[0], None),
    ]
    assert {(d.role, d.ordinal): d.decision for d in selection.decisions} == {
        (REP, 0): SourceDecisionKind.USE_SOURCE,
        (DET, 0): SourceDecisionKind.USE_DERIVED,
        (DET, 1): SourceDecisionKind.EXCLUDE,
    }
    assert (move.sequence, move.reason) == (1, SelectionMoveReason.INITIAL)


REFUSED_SELECTIONS = {
    "an image left out": (
        lambda listing: listing.use_all()[:2],
        lambda listing: [SelectedOutput(REP, REP, 0), SelectedOutput(DET, DET, 0)],
        "explicit decision",
    ),
    "a use of another sha": (
        lambda listing: [
            replace(listing.use_all()[0], sha256=listing.images[1]),
            *listing.use_all()[1:],
        ],
        lambda listing: [SelectedOutput(REP, REP, 0)],
        "CONFIRMED source image",
    ),
    "an excluded image placed": (
        lambda listing: [
            *listing.use_all()[:2],
            replace(listing.use_all()[2], decision=SourceDecisionKind.EXCLUDE),
        ],
        lambda listing: [
            SelectedOutput(REP, REP, 0),
            SelectedOutput(DET, DET, 0),
            SelectedOutput(DET, DET, 1),
        ],
        "uses or replaces",
    ),
    "a used image not placed": (
        lambda listing: listing.use_all(),
        lambda listing: [SelectedOutput(REP, REP, 0)],
        "exactly once",
    ),
}


@pytest.mark.parametrize(
    ("decisions", "outputs", "message"), REFUSED_SELECTIONS.values(), ids=REFUSED_SELECTIONS.keys()
)
def test_an_incomplete_or_inconsistent_selection_is_refused(
    container: Container,
    config: AppConfig,
    listing: Listing,
    decisions: Any,
    outputs: Any,
    message: str,
) -> None:
    # Kickoff §J 20–23: leaving an image out is never an automatic exclusion.
    with pytest.raises(InputValidationError, match=message):
        container.images.record_operator_selection(
            listing.item,
            source_revision_id=listing.revision.revision_id,
            decisions=decisions(listing),
            outputs=outputs(listing),
            decided_by="operator-1",
        )
    assert not any(_counts(config).values())


def test_a_replacement_must_derive_from_the_image_it_replaces(
    container: Container, listing: Listing
) -> None:
    # Kickoff §J 22.
    revision = listing.revision.revision_id
    from_detail = container.images.record_completed_derivation(
        completed(revision, [source_input(listing.images[1])], png("detail")), decided_by="editor"
    )
    decisions = listing.use_all()
    decisions[0] = SourceDecision(
        REP, 0, listing.images[0], SourceDecisionKind.USE_DERIVED, from_detail.derivation_id
    )
    outputs = [
        SelectedOutput(REP, REP, 0),
        SelectedOutput(DET, DET, 0),
        SelectedOutput(DET, DET, 1),
    ]
    with pytest.raises(InputValidationError, match="derives from the source image"):
        container.images.record_operator_selection(
            listing.item,
            source_revision_id=revision,
            decisions=decisions,
            outputs=outputs,
            decided_by="operator-1",
        )
    decisions[0] = replace(decisions[0], derivation_id="no-such-derivation")
    with pytest.raises(NotFoundError):
        container.images.record_operator_selection(
            listing.item,
            source_revision_id=revision,
            decisions=decisions,
            outputs=outputs,
            decided_by="operator-1",
        )


def test_the_current_selection_pointer_is_explicit_and_append_only(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 26: reselecting appends a move; the database refuses any other chain.
    first = _select_all_sources(listing)
    decisions = listing.use_all()
    decisions[2] = replace(decisions[2], decision=SourceDecisionKind.EXCLUDE)
    second, move = listing.container.images.record_operator_selection(
        listing.item,
        source_revision_id=listing.revision.revision_id,
        decisions=decisions,
        outputs=[SelectedOutput(REP, REP, 0), SelectedOutput(DET, DET, 0)],
        decided_by="operator-2",
    )
    assert (move.sequence, move.reason) == (2, SelectionMoveReason.RESELECTED)
    assert move.previous_selection_revision_id == first.selection_revision_id
    assert container.images.current_selection(listing.item) == second
    with contextlib.closing(raw(config)) as connection:

        def move_row(sequence: int, selection: str, previous: str | None) -> tuple[object, ...]:
            return (
                str(uuid.uuid4()),
                listing.item,
                sequence,
                selection,
                previous,
                "RESELECTED",
                "t",
                "t",
                "c",
                "2026-09-19 00:00:00",
            )

        insert = "INSERT INTO current_image_selection_moves VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        with pytest.raises(sqlite3.IntegrityError, match="never returns"):
            connection.execute(
                insert, move_row(3, first.selection_revision_id, second.selection_revision_id)
            )
        with pytest.raises(sqlite3.IntegrityError, match="in order"):
            connection.execute(
                insert, move_row(7, first.selection_revision_id, second.selection_revision_id)
            )


@pytest.mark.parametrize("failing", IMAGE_AUDIT[1:3], ids=["selection audit", "move audit"])
def test_a_selection_its_move_and_its_audit_commit_together(
    container: Container,
    config: AppConfig,
    listing: Listing,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    # Kickoff §J 27.
    append = AuditLog.append

    def refuse(self: AuditLog, entry: Any, **kwargs: Any) -> Any:
        if entry.event_type == failing:
            raise RuntimeError("audit refused")
        return append(self, entry, **kwargs)

    monkeypatch.setattr(AuditLog, "append", refuse)
    with pytest.raises(RuntimeError, match="audit refused"):
        _select_all_sources(listing)
    assert not any(_counts(config).values())


def test_a_new_source_revision_makes_the_selection_stale_and_nothing_carries_over(
    container: Container, shop: Shop, listing: Listing
) -> None:
    # Kickoff §J 28–30: even with the very same image SHAs, a selection and a derivation decided
    # against the old revision are STALE; equal hashes never clear it.
    revision = listing.revision.revision_id
    derived = container.images.record_completed_derivation(
        completed(revision, [source_input(listing.images[0])], png("a")), decided_by="editor"
    )
    decisions = listing.use_all()
    decisions[0] = replace(
        decisions[0], decision=SourceDecisionKind.USE_DERIVED, derivation_id=derived.derivation_id
    )
    listing.container.images.record_operator_selection(
        listing.item,
        source_revision_id=revision,
        decisions=decisions,
        outputs=[
            SelectedOutput(REP, REP, 0),
            SelectedOutput(DET, DET, 0),
            SelectedOutput(DET, DET, 1),
        ],
        decided_by="operator-1",
    )
    _pass_all(listing)
    assert container.product_readiness.base_readiness(listing.item).status is ReadinessStatus.READY
    run_id, newer = shop.collect(listing.refs, price=9000)
    container.materializer.materialize_run(run_id)
    readiness = container.product_readiness.base_readiness(listing.item)
    assert readiness.status is ReadinessStatus.STALE
    assert _codes(readiness) == [
        (IMAGE_DERIVATION_STALE, "REPRESENTATIVE:0"),
        (IMAGE_SELECTION_STALE, "images"),
    ]
    # The old selection is still what the pointer names; nothing moved it.
    current = container.images.current_selection(listing.item)
    assert current is not None and current.source_revision_id == revision
    # The old derivation cannot be used against the new revision; it is revalidated instead.
    decisions = [replace(d, derivation_id=d.derivation_id) for d in decisions]
    with pytest.raises(InputValidationError, match="another source revision"):
        container.images.record_operator_selection(
            listing.item,
            source_revision_id=newer.revision_id,
            decisions=decisions,
            outputs=[
                SelectedOutput(REP, REP, 0),
                SelectedOutput(DET, DET, 0),
                SelectedOutput(DET, DET, 1),
            ],
            decided_by="operator-1",
        )


def test_revalidation_is_a_new_derivation_of_the_same_artifact(
    container: Container, config: AppConfig, shop: Shop, listing: Listing
) -> None:
    # Kickoff §J 31: the old derivation stays addressable; the new one reuses the artifact.
    output = png("a")
    old = container.images.record_completed_derivation(
        completed(listing.revision.revision_id, [source_input(listing.images[0])], output),
        decided_by="editor",
    )
    run_id, newer = shop.collect(listing.refs, price=9000)
    container.materializer.materialize_run(run_id)
    new = container.images.record_completed_derivation(
        completed(newer.revision_id, [source_input(listing.images[0])], output),
        decided_by="editor",
    )
    assert new.derivation_id != old.derivation_id
    assert new.artifact_sha256 == old.artifact_sha256
    assert new.validated_source_revision_id == newer.revision_id
    assert container.images.read_derivation(old.derivation_id) == old
    assert count(config, "derived_image_artifacts") == 1


# ---------------------------------------------------------------- 32–39 QA


def test_qa_is_bound_to_the_exact_binary(container: Container, listing: Listing) -> None:
    # Kickoff §J 32–33: a PASS on one SHA never covers another.
    revision = listing.revision.revision_id
    a = container.images.record_completed_derivation(
        completed(revision, [source_input(listing.images[0])], png("a")), decided_by="editor"
    )
    b = container.images.record_completed_derivation(
        completed(revision, [source_input(listing.images[0])], png("b")), decided_by="editor"
    )
    container.images.record_qa(
        asset_kind=ImageAssetKind.DERIVED_ARTIFACT,
        sha256=a.artifact_sha256,
        derivation_id=a.derivation_id,
        validated_source_revision_id=revision,
        verdict=QaVerdict.PASS,
        decided_by="qa-1",
    )
    decisions = listing.use_all()
    decisions[0] = replace(
        decisions[0], decision=SourceDecisionKind.USE_DERIVED, derivation_id=b.derivation_id
    )
    container.images.record_operator_selection(
        listing.item,
        source_revision_id=revision,
        decisions=decisions,
        outputs=[SelectedOutput(REP, REP, 0)] + [SelectedOutput(DET, DET, i) for i in (0, 1)],
        decided_by="operator-1",
    )
    for image in listing.images[1:]:
        container.images.record_qa(
            asset_kind=ImageAssetKind.SOURCE_ASSET,
            sha256=image,
            derivation_id=None,
            validated_source_revision_id=revision,
            verdict=QaVerdict.PASS,
            decided_by="qa-1",
        )
    readiness = container.product_readiness.base_readiness(listing.item)
    assert _codes(readiness) == [(IMAGE_QA_MISSING, "REPRESENTATIVE:0")]
    assert (
        container.images.qa_for_selected_asset(
            asset_kind=ImageAssetKind.DERIVED_ARTIFACT,
            sha256=b.artifact_sha256,
            derivation_id=b.derivation_id,
            validated_source_revision_id=revision,
        )
        is None
    )


def test_a_pass_on_one_source_image_never_covers_another(
    container: Container, listing: Listing
) -> None:
    # Kickoff §J 32–33, for source assets: every selected binary needs its own verdict.
    _select_all_sources(listing)
    container.images.record_qa(
        asset_kind=ImageAssetKind.SOURCE_ASSET,
        sha256=listing.images[0],
        derivation_id=None,
        validated_source_revision_id=listing.revision.revision_id,
        verdict=QaVerdict.PASS,
        decided_by="qa-1",
    )
    readiness = container.product_readiness.base_readiness(listing.item)
    assert readiness.status is ReadinessStatus.REVIEW_REQUIRED
    assert _codes(readiness) == [(IMAGE_QA_MISSING, "DETAIL:1"), (IMAGE_QA_MISSING, "DETAIL:2")]


def test_qa_on_stale_provenance_never_validates_the_current_one(
    container: Container, shop: Shop, listing: Listing
) -> None:
    # Kickoff §J 34: a PASS for the old revision's source image is STALE after reselection.
    _select_all_sources(listing)
    _pass_all(listing)
    run_id, newer = shop.collect(listing.refs, price=9000)
    container.materializer.materialize_run(run_id)
    container.images.record_operator_selection(
        listing.item,
        source_revision_id=newer.revision_id,
        decisions=listing.use_all(),
        outputs=[SelectedOutput(REP, REP, 0)] + [SelectedOutput(DET, DET, i) for i in (0, 1)],
        decided_by="operator-1",
    )
    readiness = container.product_readiness.base_readiness(listing.item)
    assert readiness.status is ReadinessStatus.STALE
    assert _codes(readiness) == [
        (IMAGE_QA_STALE, "DETAIL:1"),
        (IMAGE_QA_STALE, "DETAIL:2"),
        (IMAGE_QA_STALE, "REPRESENTATIVE:0"),
    ]


def test_one_exact_qa_input_has_one_verdict(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 35: the same verdict is the same result; another is a conflict.
    arguments: dict[str, Any] = {
        "asset_kind": ImageAssetKind.SOURCE_ASSET,
        "sha256": listing.images[0],
        "derivation_id": None,
        "validated_source_revision_id": listing.revision.revision_id,
        "decided_by": "qa-1",
    }
    first = container.images.record_qa(verdict=QaVerdict.PASS, **arguments)
    assert container.images.record_qa(verdict=QaVerdict.PASS, **arguments) == first
    with pytest.raises(ImageQaConflictError):
        container.images.record_qa(verdict=QaVerdict.FAIL, findings=["TEXT_CUT"], **arguments)
    assert count(config, "image_qa_results") == 1
    with contextlib.closing(raw(config)) as connection:
        row = list(connection.execute("SELECT * FROM image_qa_results").fetchone())
        row[0], row[7] = str(uuid.uuid4()), "FAIL"
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                f"INSERT INTO image_qa_results VALUES ({', '.join('?' * len(row))})", row
            )


def test_a_qa_rule_change_needs_a_new_verdict(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    # Kickoff §J 36.
    _select_all_sources(listing)
    _pass_all(listing)
    assert container.product_readiness.base_readiness(listing.item).status is ReadinessStatus.READY
    before = container.product_readiness.base_readiness(listing.item).dependency_fingerprint
    container.images.qa_rule_version = "image-qa/v2"
    readiness = container.product_readiness.base_readiness(listing.item)
    assert readiness.status is ReadinessStatus.STALE
    assert {code for code, _ in _codes(readiness)} == {IMAGE_QA_STALE}
    assert readiness.dependency_fingerprint != before
    _pass_all(listing)
    assert container.product_readiness.base_readiness(listing.item).status is ReadinessStatus.READY


@pytest.mark.parametrize(
    "findings",
    [["lowercase"], ["TEXT https://x"], [f"F{i}" for i in range(17)], ["DUP", "DUP"]],
    ids=["lowercase", "text", "too many", "duplicated"],
)
def test_qa_findings_are_bounded_codes(
    container: Container, config: AppConfig, listing: Listing, findings: list[str]
) -> None:
    # Kickoff §J 37.
    with pytest.raises(InputValidationError):
        container.images.record_qa(
            asset_kind=ImageAssetKind.SOURCE_ASSET,
            sha256=listing.images[0],
            derivation_id=None,
            validated_source_revision_id=listing.revision.revision_id,
            verdict=QaVerdict.REVIEW_REQUIRED,
            findings=findings,
            decided_by="qa-1",
        )
    assert count(config, "image_qa_results") == 0


def test_an_audit_failure_rolls_the_qa_verdict_back(
    container: Container, config: AppConfig, listing: Listing, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kickoff §J 39.
    append = AuditLog.append

    def refuse(self: AuditLog, entry: Any, **kwargs: Any) -> Any:
        if entry.event_type == AuditEventType.PRODUCT_IMAGE_QA_RECORDED:
            raise RuntimeError("audit refused")
        return append(self, entry, **kwargs)

    monkeypatch.setattr(AuditLog, "append", refuse)
    with pytest.raises(RuntimeError, match="audit refused"):
        container.images.record_qa(
            asset_kind=ImageAssetKind.SOURCE_ASSET,
            sha256=listing.images[0],
            derivation_id=None,
            validated_source_revision_id=listing.revision.revision_id,
            verdict=QaVerdict.PASS,
            decided_by="qa-1",
        )
    assert count(config, "image_qa_results") == 0


# ---------------------------------------------------------------- 40–51 readiness


@pytest.mark.parametrize(
    ("verdict", "findings", "status", "code"),
    [
        (
            QaVerdict.REVIEW_REQUIRED,
            ["TEXT_OVERLAP"],
            ReadinessStatus.REVIEW_REQUIRED,
            IMAGE_QA_REVIEW_REQUIRED,
        ),
        (QaVerdict.FAIL, ["BADGE_DAMAGED"], ReadinessStatus.BLOCKED, IMAGE_QA_FAILED),
    ],
    ids=["review", "fail"],
)
def test_a_non_pass_verdict_keeps_its_finding_codes(
    container: Container,
    listing: Listing,
    verdict: QaVerdict,
    findings: list[str],
    status: ReadinessStatus,
    code: str,
) -> None:
    # Kickoff §J 44–45.
    _select_all_sources(listing)
    _pass_all(listing)
    selection = container.images.current_selection(listing.item)
    assert selection is not None
    container.images.qa_rule_version = "image-qa/v2"
    for output in selection.outputs:
        container.images.record_qa(
            asset_kind=output.asset_kind,
            sha256=output.sha256,
            derivation_id=None,
            validated_source_revision_id=selection.source_revision_id,
            verdict=verdict if output.position == 1 else QaVerdict.PASS,
            findings=findings if output.position == 1 else (),
            decided_by="qa-1",
        )
    readiness = container.product_readiness.base_readiness(listing.item)
    assert readiness.status is status
    assert sorted(_codes(readiness)) == sorted(
        [(code, "DETAIL:1"), (f"IMAGE_FINDING_{findings[0]}", "DETAIL:1")]
    )


def test_with_clean_facts_and_every_selected_binary_passing_base_is_ready(
    container: Container, listing: Listing
) -> None:
    # Kickoff §J 40, 43, 46–49: missing selection, missing QA, then READY; the fingerprint
    # follows the selection and the QA.
    readiness = container.product_readiness.base_readiness(listing.item)
    assert readiness.rule_version == BASE_READINESS_RULE_VERSION == "base-readiness/v2"
    assert _codes(readiness) == [(IMAGE_SELECTION_MISSING, "images")]
    _select_all_sources(listing)
    missing = container.product_readiness.base_readiness(listing.item)
    assert missing.status is ReadinessStatus.REVIEW_REQUIRED
    assert {code for code, _ in _codes(missing)} == {IMAGE_QA_MISSING}
    _pass_all(listing)
    ready = container.product_readiness.base_readiness(listing.item)
    assert ready.status is ReadinessStatus.READY and ready.reasons == ()
    fingerprints = {readiness.dependency_fingerprint, missing.dependency_fingerprint}
    fingerprints.add(ready.dependency_fingerprint)
    assert len(fingerprints) == 3
    decisions = listing.use_all()
    decisions[2] = replace(decisions[2], decision=SourceDecisionKind.EXCLUDE)
    container.images.record_operator_selection(
        listing.item,
        source_revision_id=listing.revision.revision_id,
        decisions=decisions,
        outputs=[SelectedOutput(REP, REP, 0), SelectedOutput(DET, DET, 0)],
        decided_by="op",
    )
    reselected = container.product_readiness.base_readiness(listing.item)
    assert reselected.status is ReadinessStatus.READY
    assert reselected.dependency_fingerprint != ready.dependency_fingerprint


def test_a_newer_child_derivation_never_becomes_current_by_lineage(
    container: Container, listing: Listing
) -> None:
    # A parent link is not current-state ownership: only an operator selection moves the pointer.
    revision = listing.revision.revision_id
    a = container.images.record_completed_derivation(
        completed(revision, [source_input(listing.images[0])], png("a")), decided_by="editor"
    )
    decisions = listing.use_all()
    decisions[0] = replace(
        decisions[0], decision=SourceDecisionKind.USE_DERIVED, derivation_id=a.derivation_id
    )
    container.images.record_operator_selection(
        listing.item,
        source_revision_id=revision,
        decisions=decisions,
        outputs=[SelectedOutput(REP, REP, 0)] + [SelectedOutput(DET, DET, i) for i in (0, 1)],
        decided_by="op",
    )
    _pass_all(listing)
    child = container.images.record_completed_derivation(
        completed(revision, [derived_input(a)], png("child")), decided_by="editor"
    )
    assert child.inputs == (derived_input(a),)
    current = container.images.current_selection(listing.item)
    assert current is not None and current.outputs[0].derivation_id == a.derivation_id
    assert container.product_readiness.base_readiness(listing.item).status is ReadinessStatus.READY


def test_pricing_readiness_is_unaffected_by_images(container: Container, listing: Listing) -> None:
    # Kickoff §J 50–51.
    before = container.product_readiness.pricing_readiness(listing.item, context())
    container.pricing.price(listing.item, context())
    priced = container.product_readiness.pricing_readiness(listing.item, context())
    _select_all_sources(listing)
    _pass_all(listing)
    after = container.product_readiness.pricing_readiness(listing.item, context())
    assert before.status is ReadinessStatus.STALE
    assert (after.status, after.dependency_fingerprint) == (
        priced.status,
        priced.dependency_fingerprint,
    )
    assert container.product_readiness.base_readiness(listing.item).status is ReadinessStatus.READY
    assert after.status is ReadinessStatus.READY
    assert container.screens.register().registration_candidates_total == 0


def test_image_audit_holds_identifiers_and_codes_only(
    container: Container, config: AppConfig, listing: Listing
) -> None:
    derived = container.images.record_completed_derivation(
        completed(listing.revision.revision_id, [source_input(listing.images[0])], png("a")),
        decided_by="editor",
    )
    assert derived.derivation_id
    _select_all_sources(listing)
    _pass_all(listing)
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute(
            "SELECT event_type, coalesce(before_json, '') || coalesce(after_json, '')"
            " || details_json FROM audit_events WHERE event_type IN (?, ?, ?, ?)",
            IMAGE_AUDIT,
        ).fetchall()
    assert {row[0] for row in rows} == set(IMAGE_AUDIT)
    text = " ".join(row[1] for row in rows)
    # Versions and capability codes are identifiers; the recipe content (its "crop" key) is not.
    for forbidden in ("https://", "shop.example", "observed", "img:nth-of-type", '"crop"'):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------- 52–55 migration


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _all_rows(database: Path, tables: Sequence[str]) -> dict[str, list[tuple[object, ...]]]:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in tables
        }


EARLIER_TABLES = (
    "product_facts_revisions",
    "product_facts_image_refs",
    "source_assets",
    "collection_runs",
    "source_products",
    "current_source_revision_moves",
    "product_groups",
    "group_members",
    "group_membership_revisions",
    "listing_compositions",
    "product_items",
    "source_bindings",
    "pricing_snapshots",
    "current_pricing_snapshot_moves",
    "audit_events",
)


def _priced_at_0013(tmp_path: Path) -> Path:
    database = tmp_path / "icbm.db"
    url = _url(database)
    command.upgrade(alembic_config(url), "0013_m4_pricing_snapshots")
    db = Database(url)
    try:
        clock = FakeClock()
        store = ProductFoundationStore(db, clock)
        revisions = ProductFactsRevisionStore(db, clock)
        audit = AuditLog(db, clock)
        materializer = ProductMaterializer(db=db, store=store, revisions=revisions, audit=audit)
        pricing = ProductPricingService(store=store, revisions=revisions, audit=audit, clock=clock)
        shop = Shop(db, tmp_path / "source-assets")
        run_id, _revision = shop.collect([(REP, 0, shop.source("r")), (DET, 0, shop.source("d"))])
        item = materializer.materialize_run(run_id).item_id
        assert item is not None
        assert pricing.price(item, context()).snapshot is not None
    finally:
        db.dispose()
    return database


def test_0013_to_0014_keeps_every_earlier_row_and_guesses_no_image(tmp_path: Path) -> None:
    # Kickoff §J 52–54.
    database = _priced_at_0013(tmp_path)
    before = _all_rows(database, EARLIER_TABLES)
    assert before["pricing_snapshots"] and before["product_facts_image_refs"]
    upgrade_to_head(_url(database))
    engine = create_sqlite_engine(_url(database))
    try:
        assert current_revision(engine) == head_revision() == "0014_m4_derived_image_lineage"
    finally:
        engine.dispose()
    assert _all_rows(database, EARLIER_TABLES) == before
    assert not any(_all_rows(database, IMAGE_TABLES).values())


def test_downgrade_refuses_to_destroy_image_history(tmp_path: Path) -> None:
    # Kickoff §J 55.
    database = _priced_at_0013(tmp_path)
    url = _url(database)
    upgrade_to_head(url)
    command.downgrade(alembic_config(url), "0013_m4_pricing_snapshots")
    upgrade_to_head(url)
    db = Database(url)
    try:
        clock = FakeClock()
        images = ProductImageService(
            store=ProductFoundationStore(db, clock),
            artifacts=DerivedImageStore(tmp_path / "derived-images", db, HeaderImageDecoder()),
            audit=AuditLog(db, clock),
            clock=clock,
        )
        with contextlib.closing(sqlite3.connect(database)) as connection:
            revision, image = connection.execute(
                "SELECT revision_id, sha256 FROM product_facts_image_refs LIMIT 1"
            ).fetchone()
        images.record_qa(
            asset_kind=ImageAssetKind.SOURCE_ASSET,
            sha256=image,
            derivation_id=None,
            validated_source_revision_id=revision,
            verdict=QaVerdict.PASS,
            decided_by="qa",
        )
    finally:
        db.dispose()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0013_m4_pricing_snapshots")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0014_m4_derived_image_lineage"
    finally:
        engine.dispose()


def test_the_image_checks_match_the_orm(config: AppConfig) -> None:
    from sqlalchemy import CheckConstraint

    from app.db.metadata import metadata

    with contextlib.closing(raw(config)) as connection:
        for table in IMAGE_TABLES:
            ddl = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()[0]
            for check in metadata.tables[table].constraints:
                if isinstance(check, CheckConstraint):
                    assert f"CHECK ({check.sqltext})" in ddl, f"{table}: {check.name}"
