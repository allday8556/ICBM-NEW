"""M4 PR-C: canonical Product materialization and read-back (Issue #80 kickoff 5736827688).

Durably RECORDED source truth becomes the current source revision, a canonical ProductGroup with
its CONFIRMED member, and — only when the current revision proves no options and no tiers — the
default Item and its BASE_PRODUCT binding. Every case of kickoff §11 is proven here on a migrated
database, through the real stores. No supplier, marketplace or AI provider is contacted, and no
campaign root is opened.
"""

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.routes.products import product as product_route
from app.api.routes.products import product_of_source as product_of_source_route
from app.audit.models import AuditEventType
from app.audit.service import AuditLog
from app.collect.assets import DecodedImage, SourceAssetStore
from app.collect.facts import FactsStatus, FieldFact, FieldStatus, PricesValue, SourcePrice
from app.collect.revisions import StoredRevision
from app.collect.runs import CollectionRunStore
from app.config import AppConfig
from app.container import Container, build_container
from app.core.errors import NotFoundError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.main import create_app
from app.products.materialization import (
    CURRENT_IS_NEWER,
    FACTS_STATUS_MISMATCH,
    FINGERPRINTS_BROKEN,
    GROUP_RETIRED,
    NO_RECORDED_REVISION,
    RULE_VERSION,
    RUN_IDENTITY_MISMATCH,
    RUN_NOT_RECORDED,
    RUN_REVISION_MISMATCH,
    BaseProductEvidence,
    Materialization,
    MaterializationStatus,
    SourceDrift,
)
from app.products.model import (
    DEFAULT_SINGLE_UNIT_SIGNATURE,
    BindingKind,
    GroupStatus,
    MemberStatus,
    MoveReason,
)
from app.products.store import ProductFoundationUnit
from app.screens.contracts import EmptyReason, ScreenState
from tests.collect_support import (
    PNG,
    REPRESENTATIVE,
    SOURCE_URL,
    absent,
    base_fields,
    collected,
    confirmed,
    evidence,
)
from tests.support import TEST_JOBS, FakeClock

pytestmark = pytest.mark.integration

SUPPLIER = "kmretail"
PRODUCT = "1234"
M4_TABLES = (
    "source_products",
    "current_source_revision_moves",
    "product_groups",
    "group_members",
    "group_membership_revisions",
    "group_change_events",
    "listing_compositions",
    "product_items",
    "source_bindings",
)
PRODUCT_AUDIT = (
    AuditEventType.PRODUCT_CURRENT_SOURCE_REVISION_MOVED.value,
    AuditEventType.PRODUCT_MATERIALIZED.value,
)


class FakeDecoder:
    def decode(self, data: bytes) -> DecodedImage | None:
        return DecodedImage("image/png", 64, 64) if data.startswith(b"\x89PNG") else None


# ---------------------------------------------------------------- synthetic source truth


def review(locator: str) -> FieldFact:
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        (evidence(locator, FieldStatus.REVIEW_REQUIRED, observed=None),),
    )


def no_options(price: int = 12900) -> dict[str, FieldFact]:
    """States ``options`` and ``quantity_tiers`` ABSENT: the proof ruling B needs. ``options`` is
    a core field, so the revision itself is ``REVIEW_REQUIRED``."""
    fields = base_fields()
    fields["options"] = absent(".options")
    fields["prices"] = confirmed(
        PricesValue(prices=(SourcePrice(label="price", amount_krw=price),)), ".price"
    )
    return fields


def with_options() -> dict[str, FieldFact]:
    """``options`` CONFIRMED (an empty axis list): positive evidence, not proof of none."""
    return base_fields()


def options_under_review() -> dict[str, FieldFact]:
    fields = no_options()
    fields["options"] = review(".options")
    return fields


def tiers_under_review() -> dict[str, FieldFact]:
    fields = no_options()
    fields["quantity_tiers"] = review(".tiers")
    return fields


class Collections:
    """Durable collections as COLLECT makes them: a run opened with its identity, the revision
    appended through the real store, and the outcome settled only when the test says so."""

    def __init__(self, container: Container, config: AppConfig) -> None:
        self._db = container.db
        self._runs = CollectionRunStore(container.db, FakeClock())
        self._revisions = container.revisions
        assets = SourceAssetStore(
            config.source_assets_dir, container.db, FakeDecoder(), FakeClock()
        )
        self._images = (replace(REPRESENTATIVE, sha256=assets.put(PNG).sha256),)

    def open(self, source_product_id: str = PRODUCT, *, note_identity: bool = True) -> str:
        with self._db.write() as session:
            run_id = self._runs.open(
                session,
                job_id=str(uuid.uuid4()),
                correlation_id="cid-run",
                supplier_key=SUPPLIER,
                source_url=SOURCE_URL,
            )
        if note_identity:
            self._runs.note_identity(run_id, source_product_id=source_product_id)
        return run_id

    def append(
        self,
        run_id: str,
        fields: dict[str, FieldFact] | None = None,
        source_product_id: str = PRODUCT,
        **overrides: object,
    ) -> StoredRevision:
        return self._revisions.append(
            collected(
                fields=no_options() if fields is None else fields,
                images=self._images,
                source_product_id=source_product_id,
                collection_run_id=run_id,
                **overrides,
            )
        )

    def record(self, run_id: str, revision: StoredRevision) -> None:
        self._runs.recorded(
            run_id, revision_id=revision.revision_id, facts_status=revision.facts_status
        )

    def fail(self, run_id: str) -> None:
        self._runs.failed(run_id, detail="TEST_FAILED")

    def collect(
        self,
        fields: dict[str, FieldFact] | None = None,
        *,
        recorded: bool = True,
        source_product_id: str = PRODUCT,
        **overrides: object,
    ) -> tuple[str, StoredRevision]:
        run_id = self.open(source_product_id)
        revision = self.append(run_id, fields, source_product_id, **overrides)
        if recorded:
            self.record(run_id, revision)
        return run_id, revision


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections(container, config)


def _raw(config: AppConfig) -> sqlite3.Connection:
    raw = sqlite3.connect(config.data_dir / "runtime" / "icbm.db")
    raw.execute("PRAGMA foreign_keys=ON")
    return raw


def _counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(_raw(config)) as raw:
        counts = {t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in M4_TABLES}
        counts["open_bindings"] = raw.execute(
            "SELECT COUNT(*) FROM source_bindings WHERE valid_to IS NULL"
        ).fetchone()[0]
        for event_type in PRODUCT_AUDIT:
            counts[event_type] = raw.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = ?", (event_type,)
            ).fetchone()[0]
    return counts


def _nothing_materialized(config: AppConfig) -> bool:
    return not any(_counts(config).values())


def _audit(config: AppConfig, event_type: str) -> list[dict[str, Any]]:
    with contextlib.closing(_raw(config)) as raw:
        rows = raw.execute(
            "SELECT target_ref, reason_code, before_json, after_json, details_json,"
            " correlation_id FROM audit_events WHERE event_type = ? ORDER BY seq",
            (event_type,),
        ).fetchall()
    return [
        {
            "target_ref": row[0],
            "reason_code": row[1],
            "before": None if row[2] is None else json.loads(row[2]),
            "after": None if row[3] is None else json.loads(row[3]),
            "details": json.loads(row[4]),
            "correlation_id": row[5],
            "raw": " ".join(str(part) for part in row[2:5]),
        }
        for row in rows
    ]


def _current(container: Container, config: AppConfig) -> str | None:
    with contextlib.closing(_raw(config)) as raw:
        row = raw.execute(
            "SELECT source_product_uid FROM source_products"
            " WHERE supplier_key = ? AND source_product_id = ?",
            (SUPPLIER, PRODUCT),
        ).fetchone()
    return None if row is None else container.product_store.current_source_revision(row[0])


def _moves(config: AppConfig) -> list[tuple[str, str | None, str, str | None]]:
    with contextlib.closing(_raw(config)) as raw:
        return raw.execute(
            "SELECT revision_id, previous_revision_id, reason, rule_version"
            " FROM current_source_revision_moves ORDER BY sequence"
        ).fetchall()


def _materialized(result: Materialization) -> Materialization:
    assert result.status is MaterializationStatus.MATERIALIZED, result
    return result


# ---------------------------------------------------------------- 1–3 eligibility


def test_a_revision_whose_run_is_still_pending_is_not_materialized(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.1: the revision exists, but its run has not settled.
    run_id, _revision = sources.collect(recorded=False)
    by_run = container.materializer.materialize_run(run_id)
    by_source = container.materializer.materialize_source(SUPPLIER, PRODUCT)
    assert (by_run.status, by_run.reason) == (MaterializationStatus.NOT_ELIGIBLE, RUN_NOT_RECORDED)
    assert (by_source.status, by_source.reason) == (
        MaterializationStatus.NOT_ELIGIBLE,
        NO_RECORDED_REVISION,
    )
    assert _nothing_materialized(config)


def test_the_same_run_materializes_once_it_is_recorded(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.2 and §11.12: the run settles RECORDED, and the product appears in one decision.
    run_id, revision = sources.collect(recorded=False)
    assert container.materializer.materialize_run(run_id).status is (
        MaterializationStatus.NOT_ELIGIBLE
    )
    sources.record(run_id, revision)
    result = _materialized(container.materializer.materialize_run(run_id))
    assert result.move is MoveReason.INITIAL and result.previous_revision_id is None
    assert result.current_source_revision_id == revision.revision_id
    assert result.group_created and result.membership_revision_id is not None
    assert _current(container, config) == revision.revision_id
    assert _moves(config) == [(revision.revision_id, None, "INITIAL", RULE_VERSION)]
    counts = _counts(config)
    assert counts["product_groups"] == 1 and counts["group_members"] == 1
    assert counts["group_membership_revisions"] == 1


def test_failed_and_unsettled_runs_are_passed_over(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # A FAILED run's revision is not durable source truth, and neither is a PENDING one: the
    # newest revision whose own run is RECORDED is the one that becomes current.
    _run, recorded = sources.collect()
    failed_run, _failed = sources.collect(recorded=False)
    sources.fail(failed_run)
    _pending_run, _pending = sources.collect(recorded=False)
    assert container.materializer.materialize_run(failed_run).reason == RUN_NOT_RECORDED
    result = _materialized(container.materializer.materialize_source(SUPPLIER, PRODUCT))
    assert result.current_source_revision_id == recorded.revision_id


def test_a_review_required_revision_becomes_current(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.3: REVIEW_REQUIRED does not block the pointer, and nothing asks an operator.
    run_id, revision = sources.collect(options_under_review())
    assert revision.facts_status is FactsStatus.REVIEW_REQUIRED
    result = _materialized(container.materializer.materialize_run(run_id))
    assert result.current_source_revision_id == revision.revision_id
    assert _current(container, config) == revision.revision_id
    # Advancing the pointer changed no fact: the revision reads back exactly as it was stored.
    assert container.revisions.get(revision.revision_id) == revision


# ---------------------------------------------------------------- 4–7 idempotency and recovery


def test_replaying_the_same_run_writes_nothing_new(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.4.
    run_id, _revision = sources.collect()
    first = _materialized(container.materializer.materialize_run(run_id))
    before = _counts(config)
    for again in (
        container.materializer.materialize_run(run_id),
        container.materializer.materialize_source(SUPPLIER, PRODUCT),
        container.materializer.materialize_run(run_id),
    ):
        assert again.status is MaterializationStatus.UNCHANGED
        assert again.move is None and not again.group_created and not again.item_created
        assert again.binding_opened is None and again.bindings_closed == ()
        assert (again.product_group_id, again.item_id) == (first.product_group_id, first.item_id)
    assert _counts(config) == before


def test_a_late_older_run_never_moves_the_pointer_back(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.5: the older run settles after the newer one was materialized.
    older_run, older = sources.collect(recorded=False)
    newer_run, newer = sources.collect()
    _materialized(container.materializer.materialize_run(newer_run))
    sources.record(older_run, older)
    late = container.materializer.materialize_run(older_run)
    assert late.status is MaterializationStatus.UNCHANGED
    assert _current(container, config) == newer.revision_id
    assert [move[0] for move in _moves(config)] == [newer.revision_id]


def test_the_automatic_path_never_moves_backwards_from_a_newer_current(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # The pointer already names a revision newer than any eligible one: the automatic rule
    # leaves it, and writes nothing at all. Moving back is an explicit decision it never makes.
    _older_run, _older = sources.collect()
    _newer_run, newer = sources.collect(recorded=False)
    store = container.product_store
    uid = store.source_product(SUPPLIER, PRODUCT).source_product_uid
    store.record_move(
        uid, newer.revision_id, reason=MoveReason.INITIAL, decided_by="test", correlation_id="c"
    )
    before = _counts(config)
    result = container.materializer.materialize_source(SUPPLIER, PRODUCT)
    assert (result.status, result.reason) == (MaterializationStatus.UNCHANGED, CURRENT_IS_NEWER)
    assert store.current_source_revision(uid) == newer.revision_id
    assert _counts(config) == before


def test_the_newest_eligible_recorded_revision_wins(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.6: whichever run is handed in, the identity's newest RECORDED revision is the
    # one that becomes current, so out-of-order completion cannot leave an older one current.
    first_run, _first = sources.collect()
    _second_run, second = sources.collect()
    _third_run, _third = sources.collect(recorded=False)
    result = _materialized(container.materializer.materialize_run(first_run))
    assert result.move is MoveReason.INITIAL
    assert result.current_source_revision_id == second.revision_id
    assert [move[0] for move in _moves(config)] == [second.revision_id]


TAMPERING = {
    "evidence text rewritten": (
        "DROP TRIGGER trg_product_facts_evidence_no_update",
        "UPDATE product_facts_evidence SET observed = 'tampered' WHERE revision_id = ?",
    ),
    "field value malformed": (
        "DROP TRIGGER trg_product_facts_fields_no_update",
        "UPDATE product_facts_fields SET value_json = '{\"bad\": 1}'"
        " WHERE revision_id = ? AND field_key = 'prices'",
    ),
}


@pytest.mark.parametrize("statements", TAMPERING.values(), ids=TAMPERING.keys())
def test_a_broken_fingerprint_fails_closed_with_nothing_written(
    container: Container, config: AppConfig, sources: Collections, statements: tuple[str, str]
) -> None:
    # Kickoff §11.7: the newest RECORDED revision does not recompute. Nothing moves, and an older
    # intact revision is not chosen in its place.
    _older_run, _older = sources.collect()
    newer_run, newer = sources.collect()
    drop, tamper = statements
    with contextlib.closing(_raw(config)) as raw:
        raw.execute(drop)
        raw.execute(tamper, (newer.revision_id,))
        raw.commit()
    for result in (
        container.materializer.materialize_run(newer_run),
        container.materializer.materialize_source(SUPPLIER, PRODUCT),
    ):
        assert (result.status, result.reason) == (
            MaterializationStatus.REFUSED,
            FINGERPRINTS_BROKEN,
        )
    assert _nothing_materialized(config)


def test_a_broken_newer_revision_leaves_the_current_one_and_its_binding(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    run_id, current = sources.collect()
    _materialized(container.materializer.materialize_run(run_id))
    before = _counts(config)
    newer_run, newer = sources.collect(no_options(price=9900))
    drop, tamper = TAMPERING["evidence text rewritten"]
    with contextlib.closing(_raw(config)) as raw:
        raw.execute(drop)
        raw.execute(tamper, (newer.revision_id,))
        raw.commit()
    assert container.materializer.materialize_run(newer_run).status is (
        MaterializationStatus.REFUSED
    )
    assert _current(container, config) == current.revision_id
    assert _counts(config) == before


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        (
            "UPDATE collection_runs SET facts_status = 'CONFIRMED' WHERE collection_run_id = ?",
            FACTS_STATUS_MISMATCH,
        ),
        (
            "UPDATE collection_runs SET source_product_id = '9999' WHERE collection_run_id = ?",
            RUN_IDENTITY_MISMATCH,
        ),
        (
            "UPDATE collection_runs SET source_product_id = NULL WHERE collection_run_id = ?",
            RUN_IDENTITY_MISMATCH,
        ),
    ],
    ids=["facts status disagrees", "identity disagrees", "identity never stated"],
)
def test_a_run_that_disagrees_with_its_revision_fails_closed(
    container: Container, config: AppConfig, sources: Collections, statement: str, reason: str
) -> None:
    run_id, revision = sources.collect()
    assert revision.facts_status is FactsStatus.REVIEW_REQUIRED
    with contextlib.closing(_raw(config)) as raw:
        raw.execute(statement, (run_id,))
        raw.commit()
    result = container.materializer.materialize_run(run_id)
    assert (result.status, result.reason) == (MaterializationStatus.REFUSED, reason)
    assert _nothing_materialized(config)


FOREIGN_POINTER = {
    "revision pointer only": (
        "UPDATE collection_runs SET revision_id = :b WHERE collection_run_id = :a"
    ),
    "every other field made to agree": (
        "UPDATE collection_runs SET revision_id = :b, source_product_id = '5678',"
        " facts_status = :status WHERE collection_run_id = :a"
    ),
}


@pytest.mark.parametrize("statement", FOREIGN_POINTER.values(), ids=FOREIGN_POINTER.keys())
def test_a_run_pointing_at_another_runs_revision_is_refused(
    container: Container, config: AppConfig, sources: Collections, statement: str
) -> None:
    # PR #83 review 5253314334 blocker 1: RECORDED run A is made to point at run B's valid
    # revision. The requested run owns nothing, so it drives nothing: neither A's product nor B's
    # is materialized through it.
    run_a, _revision_a = sources.collect(no_options())
    run_b, revision_b = sources.collect(with_options(), source_product_id="5678")
    with contextlib.closing(_raw(config)) as raw:
        raw.execute(
            statement,
            {"a": run_a, "b": revision_b.revision_id, "status": revision_b.facts_status.value},
        )
        raw.commit()
    refused = container.materializer.materialize_run(run_a)
    assert (refused.status, refused.reason) == (
        MaterializationStatus.REFUSED,
        RUN_REVISION_MISMATCH,
    )
    assert _nothing_materialized(config)
    # A's own revision is no longer named by its run either, so A's identity fails closed too.
    assert container.materializer.materialize_source(SUPPLIER, PRODUCT).status is (
        MaterializationStatus.REFUSED
    )
    assert _nothing_materialized(config)

    # Only the malformed invocation is refused: B, reached through its own run or its identity,
    # is sound.
    direct = _materialized(container.materializer.materialize_source(SUPPLIER, "5678"))
    assert direct.current_source_revision_id == revision_b.revision_id
    assert container.materializer.materialize_run(run_b).status is MaterializationStatus.UNCHANGED
    assert container.products.product_count() == 1


# ---------------------------------------------------------------- 8–9 drift vs extractor change


def test_the_same_extractor_moves_as_newer_revision_with_drift_evidence(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.8.
    first_run, first = sources.collect(no_options(price=12900))
    _materialized(container.materializer.materialize_run(first_run))
    changed_run, changed = sources.collect(no_options(price=9900))
    moved = _materialized(container.materializer.materialize_run(changed_run))
    assert (moved.move, moved.source_drift) == (MoveReason.NEWER_REVISION, SourceDrift.CHANGED)
    assert moved.previous_revision_id == first.revision_id
    same_run, same = sources.collect(no_options(price=9900))
    assert same.source_fingerprint == changed.source_fingerprint
    again = _materialized(container.materializer.materialize_run(same_run))
    assert (again.move, again.source_drift) == (MoveReason.NEWER_REVISION, SourceDrift.UNCHANGED)
    audited = _audit(config, AuditEventType.PRODUCT_CURRENT_SOURCE_REVISION_MOVED.value)
    assert [(a["reason_code"], a["details"]["source_drift"]) for a in audited] == [
        ("INITIAL", None),
        ("NEWER_REVISION", "CHANGED"),
        ("NEWER_REVISION", "UNCHANGED"),
    ]
    assert [move[2] for move in _moves(config)] == ["INITIAL", "NEWER_REVISION", "NEWER_REVISION"]


@pytest.mark.parametrize("price", [12900, 9900], ids=["same content", "different content"])
def test_an_extractor_change_moves_without_a_drift_inference(
    container: Container, config: AppConfig, sources: Collections, price: int
) -> None:
    # Kickoff §11.9: across an extractor change, equal fingerprints prove no absence of drift and
    # different ones prove no drift. The move says EXTRACTOR_CHANGED and classifies nothing.
    first_run, first = sources.collect(no_options(price=12900))
    _materialized(container.materializer.materialize_run(first_run))
    run_id, newer = sources.collect(no_options(price=price), extractor_revision="test-collect-r2")
    assert (newer.source_fingerprint == first.source_fingerprint) is (price == 12900)
    moved = _materialized(container.materializer.materialize_run(run_id))
    assert (moved.move, moved.source_drift) == (
        MoveReason.EXTRACTOR_CHANGED,
        SourceDrift.NOT_COMPARABLE,
    )
    audited = _audit(config, AuditEventType.PRODUCT_CURRENT_SOURCE_REVISION_MOVED.value)[-1]
    assert audited["reason_code"] == "EXTRACTOR_CHANGED"
    assert audited["details"]["source_drift"] == "NOT_COMPARABLE"


# ---------------------------------------------------------------- 10–12 the canonical group


def test_a_candidate_membership_is_never_auto_confirmed(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.10: a CANDIDATE elsewhere is neither promoted nor merged into.
    run_id, _revision = sources.collect()
    store = container.product_store
    uid = store.source_product(SUPPLIER, PRODUCT).source_product_uid
    other = store.create_group(decided_by="test")
    candidate = store.add_candidate(other, uid, decided_by="test", match_method="NAME")
    result = _materialized(container.materializer.materialize_run(run_id))
    assert result.group_created and result.product_group_id != other
    with contextlib.closing(_raw(config)) as raw:
        status = raw.execute(
            "SELECT status FROM group_members WHERE member_id = ?", (candidate,)
        ).fetchone()[0]
        revisions_of_other = raw.execute(
            "SELECT COUNT(*) FROM group_membership_revisions WHERE product_group_id = ?",
            (other,),
        ).fetchone()[0]
    assert status == MemberStatus.CANDIDATE.value and revisions_of_other == 0
    assert store.group_of_source(SUPPLIER, PRODUCT) == result.product_group_id


def test_an_active_confirmed_group_is_reused(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.11.
    run_id, _revision = sources.collect()
    store = container.product_store
    uid = store.source_product(SUPPLIER, PRODUCT).source_product_uid
    group = store.create_group(decided_by="test")
    store.confirm_new_member(group, uid, reason="TEST", decided_by="test", correlation_id="c")
    result = _materialized(container.materializer.materialize_run(run_id))
    assert result.product_group_id == group and not result.group_created
    assert result.membership_revision_id is None
    revision = store.current_membership_revision(group)
    assert revision is not None and revision.revision_no == 1
    assert _counts(config)["product_groups"] == 1


def test_no_confirmed_group_gets_one_singleton_group_and_membership_revision_1(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.12.
    run_id, _revision = sources.collect()
    result = _materialized(container.materializer.materialize_run(run_id))
    store = container.product_store
    uid = store.source_product(SUPPLIER, PRODUCT).source_product_uid
    revision = store.current_membership_revision(str(result.product_group_id))
    assert revision is not None
    assert (revision.revision_no, revision.source_product_uids) == (1, (uid,))
    assert revision.membership_revision_id == result.membership_revision_id
    with contextlib.closing(_raw(config)) as raw:
        assert raw.execute(
            "SELECT reason, decided_by FROM group_membership_revisions"
        ).fetchall() == [("MATERIALIZED", "products.materializer")]


def test_a_retired_confirmed_group_needs_review_and_nothing_is_written(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    run_id, _revision = sources.collect()
    store = container.product_store
    uid = store.source_product(SUPPLIER, PRODUCT).source_product_uid
    group = store.create_group(decided_by="test")
    store.confirm_new_member(group, uid, reason="TEST", decided_by="test", correlation_id="c")
    with contextlib.closing(_raw(config)) as raw:
        raw.execute(
            "UPDATE product_groups SET status = 'RETIRED', retired_at = '2026-09-19 00:00:00'"
        )
        raw.commit()
    before = _counts(config)
    result = container.materializer.materialize_run(run_id)
    assert (result.status, result.reason) == (MaterializationStatus.REVIEW_REQUIRED, GROUP_RETIRED)
    assert result.product_group_id == group
    assert _counts(config) == before and store.current_source_revision(uid) is None


# ---------------------------------------------------------------- 13–16 Item and binding


def test_absent_options_and_tiers_materialize_the_default_item_and_base_product_binding(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.13, ruling B: exactly the default unit, one Item, one BASE_PRODUCT binding.
    run_id, revision = sources.collect()
    result = _materialized(container.materializer.materialize_run(run_id))
    assert result.base_product is BaseProductEvidence.PROVEN and result.item_created
    product = container.products.product(str(result.product_group_id))
    (item,) = product.items
    assert item.item_id == result.item_id
    assert item.composition.composition_signature == DEFAULT_SINGLE_UNIT_SIGNATURE
    assert (
        item.composition.quantity,
        item.composition.unit_amount,
        item.composition.unit_code,
    ) == (
        1,
        None,
        None,
    )
    assert item.composition.pack_count is None and item.composition.units_per_pack is None
    assert item.composition.total_amount is None
    binding = item.current_binding
    assert binding is not None and binding.binding_id == result.binding_opened
    assert binding.binding_kind is BindingKind.BASE_PRODUCT
    assert binding.provenance_revision_id == revision.revision_id
    assert binding.group_member_id == product.members[0].member_id
    counts = _counts(config)
    assert (counts["listing_compositions"], counts["product_items"], counts["source_bindings"]) == (
        1,
        1,
        1,
    )


AMBIGUOUS = {
    "options stated": with_options,
    "options under review": options_under_review,
    "tiers under review": tiers_under_review,
}


@pytest.mark.parametrize("fields", AMBIGUOUS.values(), ids=AMBIGUOUS.keys())
def test_option_or_tier_evidence_that_is_not_absent_infers_no_item(
    container: Container, config: AppConfig, sources: Collections, fields: object
) -> None:
    # Kickoff §11.14: no default unit, no composition from name or detail, no SOURCE_OFFER.
    run_id, _revision = sources.collect(fields())  # type: ignore[operator]
    result = _materialized(container.materializer.materialize_run(run_id))
    assert result.base_product is BaseProductEvidence.NOT_PROVEN
    assert result.item_id is None and result.binding_opened is None
    counts = _counts(config)
    assert counts["product_groups"] == 1 and counts["group_members"] == 1
    assert (counts["listing_compositions"], counts["product_items"], counts["source_bindings"]) == (
        0,
        0,
        0,
    )
    assert container.products.product(str(result.product_group_id)).items == ()


def test_a_newer_proven_revision_moves_the_binding_to_itself(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.15: the same immutable Item; the old binding closed, a new one on the new
    # revision.
    first_run, first = sources.collect(no_options(price=12900))
    initial = _materialized(container.materializer.materialize_run(first_run))
    newer_run, newer = sources.collect(no_options(price=9900))
    moved = _materialized(container.materializer.materialize_run(newer_run))
    assert moved.item_id == initial.item_id and not moved.item_created
    assert moved.bindings_closed == (initial.binding_opened,)
    assert moved.binding_opened is not None and moved.binding_opened != initial.binding_opened
    with contextlib.closing(_raw(config)) as raw:
        rows = raw.execute(
            "SELECT binding_id, provenance_revision_id, valid_to IS NULL FROM source_bindings"
            " ORDER BY valid_from, valid_to IS NULL"
        ).fetchall()
    assert rows == [
        (initial.binding_opened, first.revision_id, 0),
        (moved.binding_opened, newer.revision_id, 1),
    ]
    counts = _counts(config)
    assert (counts["listing_compositions"], counts["product_items"]) == (1, 1)


def test_a_newer_ambiguous_revision_closes_the_binding_and_opens_none(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.16: history stays; there is no valid binding now, and read-back says so.
    first_run, _first = sources.collect()
    initial = _materialized(container.materializer.materialize_run(first_run))
    newer_run, newer = sources.collect(options_under_review())
    moved = _materialized(container.materializer.materialize_run(newer_run))
    assert moved.base_product is BaseProductEvidence.NOT_PROVEN
    assert moved.bindings_closed == (initial.binding_opened,) and moved.binding_opened is None
    counts = _counts(config)
    assert (counts["product_items"], counts["source_bindings"], counts["open_bindings"]) == (
        1,
        1,
        0,
    )
    product = container.products.product(str(moved.product_group_id))
    (item,) = product.items
    assert item.item_id == initial.item_id and item.current_binding is None
    assert product.members[0].current_source_revision_id == newer.revision_id


def test_no_open_binding_ever_claims_an_obsolete_provenance(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # After every move, each open binding names its member's current source revision.
    for fields in (no_options(), options_under_review(), no_options(price=500), with_options()):
        run_id, _revision = sources.collect(fields)
        _materialized(container.materializer.materialize_run(run_id))
        with contextlib.closing(_raw(config)) as raw:
            stale = raw.execute(
                "SELECT COUNT(*) FROM source_bindings b"
                " JOIN group_members m ON m.member_id = b.group_member_id"
                " WHERE b.valid_to IS NULL AND b.provenance_revision_id <> ("
                "  SELECT revision_id FROM current_source_revision_moves"
                "  WHERE source_product_uid = m.source_product_uid"
                "  ORDER BY sequence DESC LIMIT 1)"
            ).fetchone()[0]
        assert stale == 0


# ---------------------------------------------------------------- 17 audit and atomicity


def test_every_pointer_move_is_audited_with_identifiers_only(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    for fields in (no_options(), no_options(price=500)):
        run_id, _revision = sources.collect(fields)
        _materialized(container.materializer.materialize_run(run_id, correlation_id="cid-mat"))
    with contextlib.closing(_raw(config)) as raw:
        moves = raw.execute(
            "SELECT move_id, source_product_uid, revision_id, previous_revision_id, reason"
            " FROM current_source_revision_moves ORDER BY sequence"
        ).fetchall()
    audited = _audit(config, AuditEventType.PRODUCT_CURRENT_SOURCE_REVISION_MOVED.value)
    assert len(audited) == len(moves) == 2
    seen: list[str] = []
    for (move_id, uid, revision_id, previous, reason), event in zip(moves, audited, strict=True):
        assert event["details"]["move_id"] == move_id and event["target_ref"] == uid
        assert event["reason_code"] == reason and event["correlation_id"] == "cid-mat"
        assert event["after"] == {"revision_id": revision_id, "sequence": len(seen) + 1}
        assert event["before"] == (None if previous is None else {"revision_id": previous})
        seen.append(move_id)
    materialized = _audit(config, AuditEventType.PRODUCT_MATERIALIZED.value)
    assert len(materialized) == 2
    # No page text, evidence text, URL or source value ever reaches the audit log.
    for event in audited + materialized:
        text = str(event["raw"])
        for forbidden in ("https://", "observed", "shop.example", "price", ".options", "name"):
            assert forbidden not in text, forbidden


@pytest.mark.parametrize(
    "failing",
    [
        AuditEventType.PRODUCT_CURRENT_SOURCE_REVISION_MOVED,
        AuditEventType.PRODUCT_MATERIALIZED,
    ],
    ids=["the move's audit", "the materialization audit"],
)
def test_an_audit_failure_rolls_the_whole_decision_back(
    container: Container,
    config: AppConfig,
    sources: Collections,
    monkeypatch: pytest.MonkeyPatch,
    failing: AuditEventType,
) -> None:
    # Kickoff §11.17: the pointer move and its audit, and everything else the decision writes,
    # commit together or not at all.
    run_id, _revision = sources.collect()
    append = AuditLog.append

    def refuse(self: AuditLog, entry: object, **kwargs: object) -> object:
        if getattr(entry, "event_type", None) is failing:
            raise RuntimeError("audit refused")
        return append(self, entry, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(AuditLog, "append", refuse)
    with pytest.raises(RuntimeError, match="audit refused"):
        container.materializer.materialize_run(run_id)
    assert _nothing_materialized(config)
    monkeypatch.undo()
    _materialized(container.materializer.materialize_run(run_id))


def test_a_failure_after_the_move_leaves_no_half_state(
    container: Container,
    config: AppConfig,
    sources: Collections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The binding is the last write of the decision: its failure undoes the move, the group, the
    # member, the membership revision, the Item and both audit events.
    run_id, _revision = sources.collect()

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("binding refused")

    monkeypatch.setattr(ProductFoundationUnit, "bind_base_product", refuse)
    with pytest.raises(RuntimeError, match="binding refused"):
        container.materializer.materialize_run(run_id)
    assert _nothing_materialized(config)

    # A later move that fails the same way leaves the earlier state exactly as it was.
    monkeypatch.undo()
    _materialized(container.materializer.materialize_run(run_id))
    before = _counts(config)
    current = _current(container, config)
    newer_run, _newer = sources.collect(no_options(price=500))
    monkeypatch.setattr(ProductFoundationUnit, "bind_base_product", refuse)
    with pytest.raises(RuntimeError, match="binding refused"):
        container.materializer.materialize_run(newer_run)
    assert _counts(config) == before and _current(container, config) == current


# ---------------------------------------------------------------- 18 read-back


def test_product_count_and_read_back_follow_canonical_state_not_revisions(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.18.
    assert container.products.product_count() == 0
    for price in (100, 200, 300):
        run_id, latest = sources.collect(no_options(price=price))
    other_run, other = sources.collect(with_options(), source_product_id="5678")
    assert container.products.product_count() == 0, "a revision is not a product"
    one = _materialized(container.materializer.materialize_run(run_id))
    two = _materialized(container.materializer.materialize_run(other_run))
    assert container.products.product_count() == 2
    assert container.screens.product_db().products_total == 2

    product = product_route(str(one.product_group_id), container)
    assert product.status is GroupStatus.ACTIVE and product.retired_at is None
    assert (product.membership_revision_id, product.membership_revision_no) == (
        one.membership_revision_id,
        1,
    )
    (member,) = product.members
    assert (member.supplier_key, member.source_product_id) == (SUPPLIER, PRODUCT)
    assert member.current_source_revision_id == latest.revision_id
    assert member.current_facts_status is FactsStatus.REVIEW_REQUIRED
    (item,) = product.items
    assert item.current_binding is not None
    assert item.current_binding.provenance_revision_id == latest.revision_id
    assert product_of_source_route(SUPPLIER, PRODUCT, container) == product

    second = product_of_source_route(SUPPLIER, "5678", container)
    assert second.product_group_id == two.product_group_id and second.items == ()
    assert second.members[0].current_source_revision_id == other.revision_id
    assert second.members[0].current_facts_status is FactsStatus.CONFIRMED

    # A retired group is history: it is no longer counted, but it stays addressable.
    with contextlib.closing(_raw(config)) as raw:
        raw.execute(
            "UPDATE product_groups SET status = 'RETIRED', retired_at = '2026-09-19 00:00:00'"
            " WHERE product_group_id = ?",
            (two.product_group_id,),
        )
        raw.commit()
    assert container.products.product_count() == 1
    retired = product_route(str(two.product_group_id), container)
    assert retired.status is GroupStatus.RETIRED and retired.retired_at is not None

    with pytest.raises(NotFoundError):
        product_route(str(uuid.uuid4()), container)
    with pytest.raises(NotFoundError):
        product_of_source_route(SUPPLIER, "0000", container)


def test_canonical_products_are_not_registration_candidates(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # PR #83 review 5253314334 blocker 2: an ACTIVE Product is not a registration candidate.
    # Until PR-D/M5 derive candidacy, REGISTER has none, while the product screens count the
    # canonical Products.
    for source_product_id, fields in (("1234", no_options()), ("5678", with_options())):
        run_id, _revision = sources.collect(fields, source_product_id=source_product_id)
        _materialized(container.materializer.materialize_run(run_id))
    assert container.products.product_count() == 2
    assert container.screens.product_db().products_total == 2
    assert container.screens.dashboard().products_total == 2
    assert container.screens.insight().products_total == 2
    register = container.screens.register()
    assert (register.registration_candidates_total, register.registrations_total) == (0, 0)
    assert register.meta.state is ScreenState.EMPTY
    assert register.meta.empty_reason is EmptyReason.NO_REGISTRATION_CANDIDATES


def test_the_products_api_is_read_only(client: TestClient) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    products = {
        path: set(ops) for path, ops in paths.items() if path.startswith("/api/v1/products")
    }
    assert products == {
        "/api/v1/products/{product_group_id}": {"get"},
        "/api/v1/products/by-source/{supplier_key}/{source_product_id}": {"get"},
    }
    unknown = client.get(f"/api/v1/products/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "PRODUCTS_PRODUCT_UNKNOWN"


def test_nothing_is_materialized_at_application_startup(config: AppConfig) -> None:
    # Kickoff §9: an existing database is never swept. Only an explicit call, or a run that is
    # RECORDED from now on, materializes anything.
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config, ownership=lease, clock=FakeClock(), secret_store=MemorySecretStore()
        )
        try:
            Collections(built, config).collect()
        finally:
            built.db.dispose()
    app = create_app(config, extra_jobs=TEST_JOBS)
    with TestClient(app, base_url="http://127.0.0.1") as started:
        assert started.get("/api/v1/screens/db").json()["products_total"] == 0
    assert _nothing_materialized(config)


def test_an_unknown_run_is_refused(container: Container) -> None:
    with pytest.raises(NotFoundError):
        container.materializer.materialize_run(str(uuid.uuid4()))


# ---------------------------------------------------------------- the COLLECT completion path


@pytest.fixture
def collecting(config: AppConfig, clock: FakeClock) -> Iterator[Container]:
    from app.collect.collection import RegisteredCollection
    from scripts.m3collect.fake_shop import (
        DETAIL_BYTES,
        DETAIL_URL,
        EXTRACTOR_FINGERPRINT,
        EXTRACTOR_REVISION,
        PRIMARY_BYTES,
        PRIMARY_URL,
        FakeGateway,
        StubSessions,
        collection,
        page,
    )

    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            collection_gateway=FakeGateway(
                documents=[page(), page()],
                images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES},
            ),
            collection_sessions=StubSessions(),
            collections=(
                RegisteredCollection(
                    collection=collection(),
                    extractor_revision=EXTRACTOR_REVISION,
                    extractor_fingerprint=EXTRACTOR_FINGERPRINT,
                ),
            ),
        )
        try:
            yield built
        finally:
            built.db.dispose()


def _replay(container: Container, job_id: str, correlation_id: str) -> None:
    from app.collect.collection import COLLECT_PRODUCT_JOB
    from app.jobs.registry import JobContext

    container.collection._run_job(
        JobContext(
            job_id=job_id,
            job_type=COLLECT_PRODUCT_JOB,
            attempt_no=2,
            max_attempts=3,
            correlation_id=correlation_id,
            target_ref="replay",
            payload={},
        )
    )


def test_a_recorded_collection_materializes_its_product(
    collecting: Container, config: AppConfig
) -> None:
    # Kickoff §9: the normal COLLECT path hands a run on only after it is durably RECORDED.
    from scripts.m3collect.fake_shop import PRODUCT_URL, SUPPLIER_KEY

    submitted = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    assert collecting.runner.run_next() is not None
    run = collecting.collection.run(submitted.collection_run_id)
    assert run.revision_id is not None
    stored = collecting.revisions.get(run.revision_id)
    assert stored is not None
    product = collecting.products.product_of_source(SUPPLIER_KEY, stored.source_product_id)
    (member,) = product.members
    assert member.current_source_revision_id == run.revision_id
    assert member.current_facts_status is run.facts_status
    proven = all(
        stored.fields[key].status is FieldStatus.ABSENT for key in ("options", "quantity_tiers")
    )
    assert bool(product.items) is proven
    moved = _audit(config, AuditEventType.PRODUCT_CURRENT_SOURCE_REVISION_MOVED.value)
    assert [event["correlation_id"] for event in moved] == [submitted.correlation_id]

    # A replayed attempt of the RECORDED run changes nothing.
    before = _counts(config)
    _replay(collecting, submitted.job_id, submitted.correlation_id)
    assert _counts(config) == before


def test_a_recorded_run_whose_materialization_failed_completes_it_on_replay(
    collecting: Container, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.products.materialization import ProductMaterializer
    from scripts.m3collect.fake_shop import PRODUCT_URL, SUPPLIER_KEY

    materialize = ProductMaterializer.materialize_source

    def fail_once(self: ProductMaterializer, *args: object, **kwargs: object) -> object:
        monkeypatch.setattr(ProductMaterializer, "materialize_source", materialize)
        raise RuntimeError("materialization interrupted")

    monkeypatch.setattr(ProductMaterializer, "materialize_source", fail_once)
    submitted = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    assert collecting.runner.run_next() is not None
    run = collecting.collection.run(submitted.collection_run_id)
    # The source truth is durable whatever happened after it, and nothing canonical was half-made.
    assert run.outcome.value == "RECORDED" and run.revision_id is not None
    assert _nothing_materialized(config)

    _replay(collecting, submitted.job_id, submitted.correlation_id)
    stored = collecting.revisions.get(run.revision_id)
    assert stored is not None
    product = collecting.products.product_of_source(SUPPLIER_KEY, stored.source_product_id)
    assert product.members[0].current_source_revision_id == run.revision_id
