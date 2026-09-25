"""The Adaptive profile and validation persistence owner over a real migrated database (P2).

What this proves (ADR-0017 §3, §7; Issue #110 5822923514), with the invented suppliers of
``tests.adaptive_support`` admitted through an injected gate:
- EPR/PTR revisions are content-addressed and immutable: a save reads back exactly, a repeat save
  writes nothing, and the database refuses any UPDATE or DELETE of every P2 table;
- every read recomputes the digest, and a row changed out of band fails closed
  (``ADAPTIVE_TAMPERED``) for profiles, bundles, samples and runs alike;
- lineage stays inside one kind and one supplier; the lifecycle is an append-only log whose shape
  and order the database enforces, and P2 never enters ``SHADOW`` (review 5311392575 B1);
- an EPR enters ``DRAFT`` with its lint findings recorded, immutable and re-checked on read (B2);
- a sample belongs to one supplier, a run replays only its own EPR's supplier's samples, and every
  read recomputes a run's ordered sample set from its linked rows (B3);
- ``VALIDATED`` is never stored: it is derived from a ``PASS`` run for the exact freshness tuple,
  of an EPR that is not retired;
- a sample is kept locally, re-checked on save and load, retained while a run references it and
  pruned only when none does;
- a profile or sample exists only for a supplier with a registered CONNECT definition and access
  envelope;
- all of it survives a restart, writes no audit event, and migration 0024 is additive with a
  downgrade that never destroys adaptive history.

No supplier is read and no network is reached: the socket layer is refused for every test.
"""

import contextlib
import dataclasses
import json
import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command

from app.collect.adaptive.capture import ValidationSample, sample_digest
from app.collect.adaptive.lint import (
    COVERAGE_FIELD_WITHOUT_RULE,
    LINT_REVISION,
    UNMAPPED_CORE_FIELD,
    draft_lint,
    lint_digest,
)
from app.collect.adaptive.profiles import profile_document
from app.collect.adaptive.validation import (
    ValidationRun,
    Verdict,
    freshness_tuple,
    validate,
)
from app.collect.adaptive_store.gate import build_supplier_gate, registered_suppliers
from app.collect.adaptive_store.store import (
    _FOLLOWS,
    ADAPTIVE_LIFECYCLE_REFUSED,
    ADAPTIVE_LINEAGE_INVALID,
    ADAPTIVE_PROFILE_INVALID,
    ADAPTIVE_PROFILE_NOT_FOUND,
    ADAPTIVE_RUN_REFUSED,
    ADAPTIVE_SAMPLE_NOT_FOUND,
    ADAPTIVE_SAMPLE_REFUSED,
    ADAPTIVE_SUPPLIER_NOT_REGISTERED,
    ADAPTIVE_TEMPLATE_MISSING,
    AdaptiveProfileStore,
    AdaptiveTampered,
    AdaptiveValidationStore,
)
from app.core.errors import AppError, InputValidationError, NotFoundError
from app.db.database import Database, create_sqlite_engine
from app.db.migrate import alembic_config, current_revision, upgrade_to_head
from integrations.suppliers.registry import SUPPLIERS
from tests.adaptive_support import (
    HOOKED_SUPPLIER,
    SUPPLIER,
    Negatives,
    documents,
    epr,
    hook_manifest,
    hooked_bundle,
    negative_pages,
    sample,
    samples,
    synmart_bundle,
    template,
)
from tests.adaptive_support import (
    SAMPLES as SAMPLES_NAMES,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration

AUTHOR = "operator:synthetic"
CORRELATION = "corr-adaptive-p2"
BEFORE_0024 = "0023_g2_review_coverage_fence"
AT_0024 = "0024_adaptive_profile_validation"
REVISIONS = "adaptive_profile_revisions"
PINS = "adaptive_profile_pins"
LINT = "adaptive_profile_lint"
TRANSITIONS = "adaptive_profile_transitions"
SAMPLES = "adaptive_validation_samples"
RUNS = "adaptive_validation_runs"
RUN_SAMPLES = "adaptive_validation_run_samples"
TABLES = (REVISIONS, PINS, LINT, TRANSITIONS, SAMPLES, RUNS, RUN_SAMPLES)


class NetworkRefused(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise NetworkRefused("the Adaptive persistence owner makes no network call")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield


@dataclasses.dataclass
class Stores:
    path: Path
    db: Database
    profiles: AdaptiveProfileStore
    validation: AdaptiveValidationStore


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _open(path: Path, clock: FakeClock) -> Stores:
    db = Database(_url(path))
    gate = build_supplier_gate({SUPPLIER, HOOKED_SUPPLIER})
    profiles = AdaptiveProfileStore(db, clock, gate)
    return Stores(path, db, profiles, AdaptiveValidationStore(db, clock, gate, profiles))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def stores(tmp_path: Path, migrated_template: Path, clock: FakeClock) -> Iterator[Stores]:
    path = tmp_path / "icbm.db"
    path.write_bytes(migrated_template.read_bytes())
    opened = _open(path, clock)
    yield opened
    opened.db.dispose()


@pytest.fixture
def negatives() -> Negatives:
    return negative_pages()


@contextlib.contextmanager
def _raw(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    finally:
        connection.close()


def _count(path: Path, table: str) -> int:
    with _raw(path) as raw:
        return int(raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _counts(path: Path) -> dict[str, int]:
    return {table: _count(path, table) for table in (*TABLES, "audit_events")}


def _out_of_band(path: Path, table: str, statement: str, *args: object) -> None:
    """An edit that bypasses the owner and the database's own guard: what tampering looks like."""
    with _raw(path) as raw:
        raw.execute(f"DROP TRIGGER trg_{table}_no_update")
        raw.execute(statement, args)
        raw.commit()


def _refused(error: pytest.ExceptionInfo[AppError], code: str) -> None:
    assert error.value.code == code, error.value


TEMPLATES = (template("plain", choice=False), template("choice", choice=True))


def _save_templates(stores: Stores) -> list[str]:
    texts = documents(*TEMPLATES)
    saved = [
        stores.profiles.save_template(text, created_by=AUTHOR, correlation_id=CORRELATION)
        for text in texts.values()
    ]
    assert saved == list(texts)
    return saved


def _save_synmart(stores: Stores) -> str:
    pinned = _save_templates(stores)
    return stores.profiles.save_draft(
        epr(pinned), created_by=AUTHOR, correlation_id=CORRELATION, change_note="first profile"
    )


def _passing_run(negatives: Negatives) -> tuple[ValidationRun, ValidationRun]:
    first = validate(synmart_bundle(), samples(), negatives=negatives)
    resolved = frozenset(first.check("V3a").details)
    run = validate(synmart_bundle(), samples(), negatives=negatives, resolved_findings=resolved)
    assert (first.verdict, run.verdict) == (Verdict.INCOMPLETE, Verdict.PASS)
    return first, run


def _save_samples(stores: Stores) -> list[ValidationSample]:
    captured = samples()
    for item in captured:
        stores.validation.save_sample(
            item, supplier_key=SUPPLIER, stored_by=AUTHOR, correlation_id=CORRELATION
        )
    return captured


def _validated_profile(stores: Stores, negatives: Negatives) -> tuple[str, ValidationRun]:
    digest = _save_synmart(stores)
    captured = _save_samples(stores)
    bundle = stores.profiles.load_bundle(digest)
    first, run = _passing_run(negatives)
    for recorded in (first, run):
        stores.validation.record_run(
            bundle, recorded, captured, recorded_by=AUTHOR, correlation_id=CORRELATION
        )
    return digest, run


# ---------------------------------------------------------------- save and read back


def test_a_saved_bundle_reads_back_exactly(stores: Stores, clock: FakeClock) -> None:
    digest = _save_synmart(stores)
    expected = synmart_bundle()
    assert digest == expected.epr_digest
    bundle = stores.profiles.load_bundle(digest)
    assert bundle == expected
    assert [d for d, _ in bundle.templates] == [d for d, _ in expected.templates]

    record = stores.profiles.record(digest)
    assert (record.kind, record.supplier_key, record.origin) == (
        "EXTRACTION_PROFILE",
        SUPPLIER,
        "OPERATOR",
    )
    assert (record.change_note, record.created_by, record.created_at) == (
        "first profile",
        AUTHOR,
        clock.now(),
    )
    assert profile_document(stores.profiles.revision(digest)) == profile_document(expected.epr)
    assert len(stores.profiles.revisions(SUPPLIER)) == 3
    assert len(stores.profiles.revisions(SUPPLIER, "PAGE_TEMPLATE")) == 2
    assert stores.profiles.revisions(HOOKED_SUPPLIER) == ()

    assert stores.profiles.state(digest) == "DRAFT"
    (opened,) = stores.profiles.lifecycle(digest)
    assert (opened.seq, opened.from_state, opened.to_state, opened.reason) == (
        1,
        None,
        "DRAFT",
        "SAVED",
    )
    with _raw(stores.path) as raw:
        pins = raw.execute(f"SELECT position, ptr_digest FROM {PINS} ORDER BY position").fetchall()
    assert pins == [(i, d) for i, (d, _) in enumerate(expected.templates)]


def test_a_repeated_save_writes_nothing_and_the_first_write_stands(
    stores: Stores, clock: FakeClock
) -> None:
    digest = _save_synmart(stores)
    before = _counts(stores.path)
    clock.advance(60)
    again = stores.profiles.save_draft(
        epr([d for d, _ in synmart_bundle().templates]),
        created_by="operator:other",
        correlation_id="corr-other",
    )
    assert again == digest
    assert _counts(stores.path) == before
    record = stores.profiles.record(digest)
    assert (record.created_by, record.change_note) == (AUTHOR, "first profile")
    assert len(stores.profiles.lifecycle(digest)) == 1


def test_a_profile_is_saved_as_its_canonical_document_whatever_its_spelling(
    stores: Stores,
) -> None:
    pinned = _save_templates(stores)
    spelled = json.dumps(epr(pinned), indent=4, sort_keys=False)
    digest = stores.profiles.save_draft(spelled, created_by=AUTHOR, correlation_id=CORRELATION)
    assert digest == synmart_bundle().epr_digest
    with _raw(stores.path) as raw:
        (stored,) = raw.execute(
            f"SELECT document FROM {REVISIONS} WHERE digest = ?", (digest,)
        ).fetchone()
    assert stored == profile_document(synmart_bundle().epr)


def test_an_invalid_document_is_refused_with_nothing_written(stores: Stores) -> None:
    before = _counts(stores.path)
    broken = {**template("plain", choice=False), "unexpected": True}
    for document in (broken, "{not json", '{"kind": "PAGE_TEMPLATE", "x": NaN}'):
        with pytest.raises(InputValidationError) as error:
            stores.profiles.save_template(document, created_by=AUTHOR, correlation_id=CORRELATION)
        _refused(error, ADAPTIVE_PROFILE_INVALID)
    with pytest.raises(InputValidationError) as error:
        stores.profiles.save_template(
            template("plain", choice=False),
            created_by=AUTHOR,
            correlation_id=CORRELATION,
            change_note="x" * 501,
        )
    _refused(error, ADAPTIVE_PROFILE_INVALID)
    assert _counts(stores.path) == before


# ---------------------------------------------------------------- the supplier gate


def test_the_production_gate_admits_exactly_the_registered_suppliers(
    stores: Stores, clock: FakeClock
) -> None:
    registered = registered_suppliers()
    assert "kmretail" in registered
    assert SUPPLIER not in registered and HOOKED_SUPPLIER not in registered
    # A CONNECT definition without its COLLECT access envelope is not registered.
    assert registered_suppliers(SUPPLIERS, ()) == frozenset()
    assert registered_suppliers((), ()) == frozenset()

    production = AdaptiveProfileStore(stores.db, clock, build_supplier_gate())
    before = _counts(stores.path)
    with pytest.raises(InputValidationError) as error:
        production.save_template(
            template("plain", choice=False), created_by=AUTHOR, correlation_id=CORRELATION
        )
    _refused(error, ADAPTIVE_SUPPLIER_NOT_REGISTERED)
    admitted = AdaptiveProfileStore(stores.db, clock, build_supplier_gate({"kmretail"}))
    kmretail = admitted.save_template(
        template("plain", choice=False, supplier="kmretail"),
        created_by=AUTHOR,
        correlation_id=CORRELATION,
    )
    assert production.record(kmretail).supplier_key == "kmretail"
    assert _count(stores.path, REVISIONS) == before[REVISIONS] + 1


def test_a_sample_of_an_unregistered_supplier_is_refused(stores: Stores, clock: FakeClock) -> None:
    validation = AdaptiveValidationStore(stores.db, clock, build_supplier_gate(), stores.profiles)
    with pytest.raises(InputValidationError) as error:
        validation.save_sample(
            sample("on_sale"), supplier_key=SUPPLIER, stored_by=AUTHOR, correlation_id=CORRELATION
        )
    _refused(error, ADAPTIVE_SUPPLIER_NOT_REGISTERED)
    assert _count(stores.path, SAMPLES) == 0


# ---------------------------------------------------------------- EPR pins


def test_an_epr_is_saved_only_after_every_template_it_pins(stores: Stores) -> None:
    pinned = list(documents(*TEMPLATES))
    with pytest.raises(InputValidationError) as error:
        stores.profiles.save_draft(epr(pinned), created_by=AUTHOR, correlation_id=CORRELATION)
    _refused(error, ADAPTIVE_TEMPLATE_MISSING)
    assert _count(stores.path, REVISIONS) == 0


def test_an_epr_never_pins_another_suppliers_template(stores: Stores) -> None:
    foreign = stores.profiles.save_template(
        template("plain", choice=False, supplier=HOOKED_SUPPLIER),
        created_by=AUTHOR,
        correlation_id=CORRELATION,
    )
    before = _counts(stores.path)
    with pytest.raises(InputValidationError) as error:
        stores.profiles.save_draft(epr([foreign]), created_by=AUTHOR, correlation_id=CORRELATION)
    _refused(error, ADAPTIVE_PROFILE_INVALID)
    assert _counts(stores.path) == before


def test_the_database_refuses_a_pin_the_epr_does_not_name(stores: Stores) -> None:
    digest = _save_synmart(stores)
    first, second = (d for d, _ in synmart_bundle().templates)
    foreign = stores.profiles.save_template(
        template("plain", choice=False, supplier=HOOKED_SUPPLIER),
        created_by=AUTHOR,
        correlation_id=CORRELATION,
    )
    with _raw(stores.path) as raw:
        for position, ptr in ((2, first), (5, second), (2, foreign), (2, digest)):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(f"INSERT INTO {PINS} VALUES (?, ?, ?)", (digest, position, ptr))


# ---------------------------------------------------------------- lineage


def test_lineage_stays_inside_one_kind_and_one_supplier(stores: Stores) -> None:
    parent = stores.profiles.save_template(
        template("plain", choice=False), created_by=AUTHOR, correlation_id=CORRELATION
    )
    child = stores.profiles.save_template(
        template("plain-2", choice=False),
        created_by=AUTHOR,
        correlation_id=CORRELATION,
        origin="AI_PROPOSAL",
        parent_digest=parent,
        change_note="renamed",
    )
    grandchild = stores.profiles.save_template(
        template("plain-3", choice=False),
        created_by=AUTHOR,
        correlation_id=CORRELATION,
        parent_digest=child,
    )
    assert stores.profiles.lineage(grandchild) == (grandchild, child, parent)
    assert stores.profiles.record(child).origin == "AI_PROPOSAL"

    foreign = stores.profiles.save_template(
        template("plain", choice=False, supplier=HOOKED_SUPPLIER),
        created_by=AUTHOR,
        correlation_id=CORRELATION,
    )
    epr_digest = _save_synmart(stores)
    before = _counts(stores.path)
    choice = template("choice-2", choice=True)
    for wrong in (foreign, epr_digest):
        with pytest.raises(InputValidationError) as error:
            stores.profiles.save_template(
                choice, created_by=AUTHOR, correlation_id=CORRELATION, parent_digest=wrong
            )
        _refused(error, ADAPTIVE_LINEAGE_INVALID)
    with pytest.raises(NotFoundError) as missing:
        stores.profiles.save_template(
            choice, created_by=AUTHOR, correlation_id=CORRELATION, parent_digest="0" * 64
        )
    assert missing.value.code == ADAPTIVE_PROFILE_NOT_FOUND
    assert _counts(stores.path) == before


def test_the_database_refuses_lineage_across_kinds(stores: Stores) -> None:
    epr_digest = _save_synmart(stores)
    document = profile_document(
        stores.profiles.revision(stores.profiles.revisions(SUPPLIER, "PAGE_TEMPLATE")[0].digest)
    )
    with _raw(stores.path) as raw, pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            f"INSERT INTO {REVISIONS} VALUES (?, 'PAGE_TEMPLATE', ?, 'icbm-profile/v1', ?, ?,"
            " 'OPERATOR', NULL, 'x', 'y', '2026-09-13 00:00:00.000000')",
            ("f" * 64, SUPPLIER, document, epr_digest),
        )


# ---------------------------------------------------------------- lifecycle


def test_the_lifecycle_is_an_append_only_log_of_draft_and_retired(stores: Stores) -> None:
    digest = _save_synmart(stores)
    stores.profiles.retire(
        digest, actor="operator:local", reason="SUPERSEDED", correlation_id="corr-retire"
    )
    assert stores.profiles.state(digest) == "RETIRED"
    _, retired = stores.profiles.lifecycle(digest)
    assert (retired.seq, retired.from_state, retired.to_state, retired.reason) == (
        2,
        "DRAFT",
        "RETIRED",
        "SUPERSEDED",
    )
    assert (retired.actor, retired.correlation_id) == ("operator:local", "corr-retire")
    with pytest.raises(InputValidationError) as error:
        stores.profiles.retire(digest, actor=AUTHOR, reason="AGAIN", correlation_id=CORRELATION)
    _refused(error, ADAPTIVE_LIFECYCLE_REFUSED)
    ptr = stores.profiles.revisions(SUPPLIER, "PAGE_TEMPLATE")[0].digest
    with pytest.raises(InputValidationError) as error:
        stores.profiles.retire(ptr, actor=AUTHOR, reason="NO", correlation_id=CORRELATION)
    _refused(error, ADAPTIVE_PROFILE_INVALID)
    # A retired EPR is kept and still loads.
    assert stores.profiles.load_bundle(digest).epr_digest == digest
    assert len(stores.profiles.lifecycle(digest)) == 2


@pytest.mark.parametrize(
    ("seq", "from_state", "to_state"),
    [
        (2, "DRAFT", "DRAFT"),  # a DRAFT is never reopened
        (2, None, "RETIRED"),  # a later transition names what it leaves
        (2, "DRAFT", "ACTIVE"),  # ADR-0017 authorizes no ACTIVE
        (2, "DRAFT", "VALIDATED"),  # VALIDATED is never a state
        (2, "SHADOW", "RETIRED"),  # a transition leaves the state before it
        (3, "DRAFT", "RETIRED"),  # no gap
        (1, None, "DRAFT"),  # no second opening
    ],
)
def test_the_database_enforces_the_lifecycle_shape_and_order(
    stores: Stores, seq: int, from_state: str | None, to_state: str
) -> None:
    digest = _save_synmart(stores)
    with _raw(stores.path) as raw, pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            f"INSERT INTO {TRANSITIONS} VALUES ('t-x', ?, ?, ?, ?, 'R', 'a', 'c',"
            " '2026-09-13 00:00:00.000000')",
            (digest, seq, from_state, to_state),
        )


def test_the_database_refuses_a_retired_epr_reopened_and_a_template_lifecycle(
    stores: Stores,
) -> None:
    digest = _save_synmart(stores)
    stores.profiles.retire(digest, actor=AUTHOR, reason="DONE", correlation_id=CORRELATION)
    ptr = stores.profiles.revisions(SUPPLIER, "PAGE_TEMPLATE")[0].digest
    with _raw(stores.path) as raw:
        for epr_digest, seq, from_state, to_state in (
            (digest, 3, "RETIRED", "DRAFT"),
            (digest, 3, "RETIRED", "SHADOW"),
            (ptr, 1, None, "DRAFT"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(
                    f"INSERT INTO {TRANSITIONS} VALUES ('t-y', ?, ?, ?, ?, 'R', 'a', 'c',"
                    " '2026-09-13 00:00:00.000000')",
                    (epr_digest, seq, from_state, to_state),
                )


# ---------------------------------------------------------------- no SHADOW entry in P2


def test_p2_never_enters_shadow_even_from_a_pass_run(stores: Stores, negatives: Negatives) -> None:
    # ADR-0017 §7.1: SHADOW is VALIDATED plus the per-supplier shadow switch, which P2 does not
    # own (review 5311392575 B1). A PASS run leaves the EPR a DRAFT, and neither store offers a
    # way in.
    digest, run = _validated_profile(stores, negatives)
    assert stores.validation.is_validated(digest, run.freshness)
    assert stores.profiles.state(digest) == "DRAFT"
    assert [t.to_state for t in stores.profiles.lifecycle(digest)] == ["DRAFT"]
    for owner in (stores.profiles, stores.validation):
        public = {name for name in dir(owner) if not name.startswith("_")}
        assert not {name for name in public if "shadow" in name.lower()}, public
    assert {"RETIRED": frozenset({"DRAFT", "SHADOW"})} == _FOLLOWS
    # Even the owner's own transition refuses it, and nothing is appended.
    with pytest.raises(InputValidationError) as error:
        stores.profiles._transition(digest, "SHADOW", AUTHOR, "READY", CORRELATION)
    _refused(error, ADAPTIVE_LIFECYCLE_REFUSED)
    assert len(stores.profiles.lifecycle(digest)) == 1


# ---------------------------------------------------------------- append-only


def test_no_row_of_any_p2_table_is_ever_updated_or_deleted(
    stores: Stores, negatives: Negatives
) -> None:
    _validated_profile(stores, negatives)
    before = _counts(stores.path)
    assert all(before[table] > 0 for table in TABLES)
    with _raw(stores.path) as raw:
        for table in TABLES:
            with pytest.raises(sqlite3.IntegrityError, match="never updated"):
                raw.execute(f"UPDATE {table} SET rowid = rowid")
            with pytest.raises(sqlite3.IntegrityError, match=r"never deleted|retained"):
                raw.execute(f"DELETE FROM {table}")
    assert _counts(stores.path) == before


# ---------------------------------------------------------------- tamper fails closed


def test_a_profile_changed_out_of_band_fails_closed_on_every_read(stores: Stores) -> None:
    digest = _save_synmart(stores)
    ptr = stores.profiles.revisions(SUPPLIER, "PAGE_TEMPLATE")[0].digest
    swapped = profile_document(
        stores.profiles.revision(ptr).model_copy(update={"template_key": "swapped"})
    )
    _out_of_band(
        stores.path,
        REVISIONS,
        f"UPDATE {REVISIONS} SET document = ? WHERE digest = ?",
        swapped,
        ptr,
    )
    for read in (
        lambda: stores.profiles.record(ptr),
        lambda: stores.profiles.revision(ptr),
        lambda: stores.profiles.revisions(SUPPLIER),
        lambda: stores.profiles.load_bundle(digest),
    ):
        with pytest.raises(AdaptiveTampered):
            read()


def test_an_epr_whose_document_no_longer_parses_fails_closed(stores: Stores) -> None:
    digest = _save_synmart(stores)
    with _raw(stores.path) as raw:
        (document,) = raw.execute(
            f"SELECT document FROM {REVISIONS} WHERE digest = ?", (digest,)
        ).fetchone()
    loaded = json.loads(document)
    loaded["purchase_controls"] = 7
    _out_of_band(
        stores.path,
        REVISIONS,
        f"UPDATE {REVISIONS} SET document = ? WHERE digest = ?",
        json.dumps(loaded),
        digest,
    )
    with pytest.raises(AdaptiveTampered):
        stores.profiles.load_bundle(digest)


def test_a_sample_changed_out_of_band_fails_closed(stores: Stores) -> None:
    (first, *_) = _save_samples(stores)
    changed = {**first.expected, "original_name": "changed"}
    _out_of_band(
        stores.path,
        SAMPLES,
        f"UPDATE {SAMPLES} SET expected_json = ? WHERE sample_digest = ?",
        json.dumps(changed, ensure_ascii=False),
        first.digest,
    )
    with pytest.raises(AdaptiveTampered):
        stores.validation.sample(first.digest)


def test_a_run_changed_out_of_band_fails_closed_and_never_validates(
    stores: Stores, negatives: Negatives
) -> None:
    digest, _ = _validated_profile(stores, negatives)
    current = freshness_tuple(synmart_bundle(), samples(), None)
    assert stores.validation.is_validated(digest, current)
    # Turn the INCOMPLETE run into a PASS behind the owner's back.
    _out_of_band(stores.path, RUNS, f"UPDATE {RUNS} SET verdict = 'PASS' WHERE verdict <> 'PASS'")
    with pytest.raises(AdaptiveTampered):
        stores.validation.runs(digest)
    with pytest.raises(AdaptiveTampered):
        stores.validation.is_validated(digest, current)


# ---------------------------------------------------------------- samples


def test_a_sample_is_stored_locally_exactly_and_idempotently(stores: Stores) -> None:
    captured = _save_samples(stores)
    for item in captured:
        assert stores.validation.sample(item.digest) == item
    _save_samples(stores)
    assert _count(stores.path, SAMPLES) == len(captured)
    with pytest.raises(NotFoundError) as error:
        stores.validation.sample("0" * 64)
    assert error.value.code == ADAPTIVE_SAMPLE_NOT_FOUND


def _redigested(item: ValidationSample, **changes: Any) -> ValidationSample:
    changed = dataclasses.replace(item, **changes)
    return dataclasses.replace(
        changed,
        digest=sample_digest(
            changed.structure, changed.expected, changed.provenance, changed.truncated
        ),
    )


def test_a_sample_is_refused_unless_it_recomputes_and_passes_the_final_scan(
    stores: Stores,
) -> None:
    item = sample("on_sale")
    structure = item.structure
    structure.setdefault("attrs", {})["onclick"] = "go()"
    provenance = {**item.provenance, "note": "captured under profile p-1"}
    for refused in (
        dataclasses.replace(item, expected_json=json.dumps({"original_name": "other"})),
        _redigested(item, structure_json=json.dumps(structure, ensure_ascii=False)),
        _redigested(item, provenance_json=json.dumps(provenance, ensure_ascii=False)),
    ):
        with pytest.raises(InputValidationError) as error:
            stores.validation.save_sample(
                refused, supplier_key=SUPPLIER, stored_by=AUTHOR, correlation_id=CORRELATION
            )
        _refused(error, ADAPTIVE_SAMPLE_REFUSED)
    assert _count(stores.path, SAMPLES) == 0


def test_a_sample_is_retained_while_referenced_and_pruned_only_when_not(
    stores: Stores, negatives: Negatives
) -> None:
    _validated_profile(stores, negatives)
    loose = sample("on_sale")
    extra = _redigested(loose, expected_json=json.dumps({**loose.expected, "brand": "loose"}))
    stores.validation.save_sample(
        extra, supplier_key=SUPPLIER, stored_by=AUTHOR, correlation_id=CORRELATION
    )
    assert _count(stores.path, SAMPLES) == 4
    with _raw(stores.path) as raw:
        for referenced in samples():
            with pytest.raises(sqlite3.IntegrityError, match="retained"):
                raw.execute(f"DELETE FROM {SAMPLES} WHERE sample_digest = ?", (referenced.digest,))
    assert stores.validation.prune_unreferenced_samples() == 1
    assert stores.validation.prune_unreferenced_samples() == 0
    assert _count(stores.path, SAMPLES) == 3
    for referenced in samples():
        assert stores.validation.sample(referenced.digest) == referenced


# ---------------------------------------------------------------- runs and VALIDATED


def test_runs_read_back_exactly_and_validated_is_derived_for_the_exact_freshness(
    stores: Stores, negatives: Negatives
) -> None:
    digest, run = _validated_profile(stores, negatives)
    first, _ = _passing_run(negatives)
    stored = stores.validation.runs(digest)
    # Recorded in the same instant: the owner promises no order between them.
    assert sorted((s.run for s in stored), key=ValidationRun.digest) == sorted(
        (first, run), key=ValidationRun.digest
    )
    assert all(s.sample_digests == tuple(x.digest for x in samples()) for s in stored)
    assert all(s.recorded_by == AUTHOR for s in stored)

    current = freshness_tuple(synmart_bundle(), samples(), None)
    assert run.freshness == current
    assert stores.validation.is_validated(digest, current)
    for index in range(len(current)):
        changed = tuple(f"{p}-changed" if i == index else p for i, p in enumerate(current))
        assert not stores.validation.is_validated(digest, changed), index
    # No column anywhere says VALIDATED or ACTIVE: it is derived, never stored.
    with _raw(stores.path) as raw:
        columns = {
            row[1].lower()
            for table in TABLES
            for row in raw.execute(f"PRAGMA table_info({table})").fetchall()
        }
    assert not {c for c in columns if "validated" in c or "active" in c or "status" in c}


def test_the_same_run_recorded_again_writes_nothing(
    stores: Stores, negatives: Negatives, clock: FakeClock
) -> None:
    digest, run = _validated_profile(stores, negatives)
    (recorded,) = (s for s in stores.validation.runs(digest) if s.run == run)
    before = _counts(stores.path)
    clock.advance(60)
    again = stores.validation.record_run(
        stores.profiles.load_bundle(digest),
        run,
        samples(),
        recorded_by="operator:other",
        correlation_id="corr-other",
    )
    assert again == recorded.run_id
    assert _counts(stores.path) == before
    (still,) = (s for s in stores.validation.runs(digest) if s.run == run)
    assert (still.recorded_by, still.recorded_at) == (AUTHOR, recorded.recorded_at)


def test_an_incomplete_run_alone_never_validates(stores: Stores, negatives: Negatives) -> None:
    digest = _save_synmart(stores)
    captured = _save_samples(stores)
    first, _ = _passing_run(negatives)
    stores.validation.record_run(
        stores.profiles.load_bundle(digest),
        first,
        captured,
        recorded_by=AUTHOR,
        correlation_id=CORRELATION,
    )
    assert not stores.validation.is_validated(digest, first.freshness)


def test_a_run_is_refused_unless_it_is_of_its_own_stored_draft_and_samples(
    stores: Stores, negatives: Negatives
) -> None:
    digest = _save_synmart(stores)
    bundle = stores.profiles.load_bundle(digest)
    _, run = _passing_run(negatives)
    captured = samples()
    with pytest.raises(NotFoundError) as unstored:
        stores.validation.record_run(
            bundle, run, captured, recorded_by=AUTHOR, correlation_id=CORRELATION
        )
    assert unstored.value.code == ADAPTIVE_SAMPLE_NOT_FOUND
    _save_samples(stores)

    other_epr = dataclasses.replace(run, freshness=("0" * 64, *run.freshness[1:]))
    for wrong_run, wrong_samples in ((run, captured[:2]), (other_epr, captured)):
        with pytest.raises(InputValidationError) as error:
            stores.validation.record_run(
                bundle, wrong_run, wrong_samples, recorded_by=AUTHOR, correlation_id=CORRELATION
            )
        _refused(error, ADAPTIVE_RUN_REFUSED)
    unsaved = synmart_bundle(sold_out_words=["품절"])
    with pytest.raises(NotFoundError):
        stores.validation.record_run(
            unsaved, run, captured, recorded_by=AUTHOR, correlation_id=CORRELATION
        )
    assert _count(stores.path, RUNS) == 0 and _count(stores.path, RUN_SAMPLES) == 0


def test_a_retired_epr_is_never_validated_again(stores: Stores, negatives: Negatives) -> None:
    digest, run = _validated_profile(stores, negatives)
    assert stores.validation.is_validated(digest, run.freshness)
    stores.profiles.retire(digest, actor=AUTHOR, reason="SUPERSEDED", correlation_id=CORRELATION)
    assert not stores.validation.is_validated(digest, run.freshness)
    with pytest.raises(InputValidationError) as error:
        stores.validation.record_run(
            stores.profiles.load_bundle(digest),
            run,
            samples(),
            recorded_by=AUTHOR,
            correlation_id=CORRELATION,
        )
    _refused(error, ADAPTIVE_RUN_REFUSED)
    assert len(stores.validation.runs(digest)) == 2


def test_the_database_refuses_a_run_of_a_template(stores: Stores, negatives: Negatives) -> None:
    _validated_profile(stores, negatives)
    ptr = stores.profiles.revisions(SUPPLIER, "PAGE_TEMPLATE")[0].digest
    with _raw(stores.path) as raw, pytest.raises(sqlite3.IntegrityError, match="validates an EPR"):
        raw.execute(
            f"INSERT INTO {RUNS} SELECT 'r-x', ?, verdict, profile_schema_version,"
            " extractor_revision, extractor_fingerprint, hook_fingerprint, sample_set_digest,"
            " capture_revision, checks_json, sample_count, ?, recorded_by, correlation_id,"
            f" recorded_at FROM {RUNS} LIMIT 1",
            (ptr, "e" * 64),
        )


# ---------------------------------------------------------------- DRAFT lint (B2)


def _save_linted(stores: Stores) -> str:
    """An EPR whose template leaves one CORE and two COVERAGE fields unruled: a valid DRAFT."""
    sparse = template("sparse", choice=False)
    for field in ("prices", "brand", "origin"):
        del sparse["fields"][field]
    (pinned,) = documents(sparse)
    stores.profiles.save_template(sparse, created_by=AUTHOR, correlation_id=CORRELATION)
    return stores.profiles.save_draft(epr([pinned]), created_by=AUTHOR, correlation_id=CORRELATION)


def test_an_epr_enters_draft_with_its_lint_recorded(stores: Stores, clock: FakeClock) -> None:
    clean = _save_synmart(stores)
    assert stores.profiles.lint(clean).findings == ()
    digest = _save_linted(stores)
    recorded = stores.profiles.lint(digest)
    assert recorded.lint_revision == LINT_REVISION
    assert recorded.recorded_at == clock.now()
    assert recorded.findings == (
        f"{COVERAGE_FIELD_WITHOUT_RULE}:sparse:brand",
        f"{COVERAGE_FIELD_WITHOUT_RULE}:sparse:origin",
        f"{UNMAPPED_CORE_FIELD}:sparse:prices",
    )
    assert recorded.findings == draft_lint(stores.profiles.load_bundle(digest))
    # Lint is a record, not a verdict: the DRAFT is saved and loads.
    assert stores.profiles.state(digest) == "DRAFT"
    with _raw(stores.path) as raw:
        assert raw.execute(f"SELECT COUNT(*) FROM {LINT}").fetchone()[0] == 2
        (stored,) = raw.execute(
            f"SELECT lint_digest FROM {LINT} WHERE epr_digest = ?", (digest,)
        ).fetchone()
    assert stored == lint_digest(LINT_REVISION, recorded.findings)


def test_a_repeated_draft_save_keeps_the_first_lint(stores: Stores, clock: FakeClock) -> None:
    digest = _save_linted(stores)
    first = stores.profiles.lint(digest)
    before = _counts(stores.path)
    clock.advance(60)
    sparse_pin = stores.profiles.revisions(SUPPLIER, "PAGE_TEMPLATE")[0].digest
    assert (
        stores.profiles.save_draft(
            epr([sparse_pin]), created_by="operator:other", correlation_id="corr-other"
        )
        == digest
    )
    assert _counts(stores.path) == before
    assert stores.profiles.lint(digest) == first


def _set_lint(path: Path, digest: str, revision: str, findings: list[str], stored: str) -> None:
    with _raw(path) as raw:
        raw.execute(f"DROP TRIGGER IF EXISTS trg_{LINT}_no_update")
        raw.execute(
            f"UPDATE {LINT} SET lint_revision = ?, findings_json = ?, lint_digest = ?"
            " WHERE epr_digest = ?",
            (revision, json.dumps(findings), stored, digest),
        )
        raw.commit()


def test_recorded_lint_changed_out_of_band_fails_closed(stores: Stores) -> None:
    digest = _save_linted(stores)
    findings = list(stores.profiles.lint(digest).findings)
    dropped = findings[1:]
    for revision, changed, stored in (
        # a finding removed, its digest recomputed: disagrees with the running rule set
        (LINT_REVISION, dropped, lint_digest(LINT_REVISION, tuple(dropped))),
        # the findings kept, the digest wrong
        (LINT_REVISION, findings, "0" * 64),
        # reordered: never the canonical sorted form
        (LINT_REVISION, findings[::-1], lint_digest(LINT_REVISION, tuple(findings[::-1]))),
    ):
        _set_lint(stores.path, digest, revision, changed, stored)
        with pytest.raises(AdaptiveTampered):
            stores.profiles.lint(digest)


def test_lint_recorded_under_another_rule_set_is_read_as_recorded(stores: Stores) -> None:
    # A later rule set never reinterprets an earlier record: it is checked against its own digest.
    digest = _save_linted(stores)
    older = ["UNMAPPED_CORE_FIELD:sparse:prices"]
    _set_lint(
        stores.path, digest, "adaptive-lint-0", older, lint_digest("adaptive-lint-0", tuple(older))
    )
    assert stores.profiles.lint(digest).findings == tuple(older)
    assert stores.profiles.lint(digest).lint_revision == "adaptive-lint-0"


def test_a_draft_without_its_lint_fails_closed(stores: Stores) -> None:
    digest = _save_linted(stores)
    with _raw(stores.path) as raw:
        raw.execute(f"DROP TRIGGER trg_{LINT}_no_delete")
        raw.execute(f"DELETE FROM {LINT} WHERE epr_digest = ?", (digest,))
        raw.commit()
    with pytest.raises(AdaptiveTampered):
        stores.profiles.lint(digest)


def test_the_database_records_lint_only_for_an_epr(stores: Stores) -> None:
    _save_synmart(stores)
    ptr = stores.profiles.revisions(SUPPLIER, "PAGE_TEMPLATE")[0].digest
    with _raw(stores.path) as raw, pytest.raises(sqlite3.IntegrityError, match="only an EPR"):
        raw.execute(
            f"INSERT INTO {LINT} VALUES (?, ?, '[]', ?, '2026-09-13 00:00:00.000000')",
            (ptr, LINT_REVISION, lint_digest(LINT_REVISION, ())),
        )


# ---------------------------------------------------------------- evidence binding (B3)


def _save_hooked(stores: Stores) -> str:
    bundle = hooked_bundle()
    for _, ptr in bundle.templates:
        stores.profiles.save_template(
            profile_document(ptr), created_by=AUTHOR, correlation_id=CORRELATION
        )
    return stores.profiles.save_draft(
        profile_document(bundle.epr), created_by=AUTHOR, correlation_id=CORRELATION
    )


def test_a_sample_belongs_to_one_supplier(stores: Stores) -> None:
    (first, *_) = _save_samples(stores)
    assert stores.validation.sample_supplier(first.digest) == SUPPLIER
    with pytest.raises(InputValidationError) as error:
        stores.validation.save_sample(
            first, supplier_key=HOOKED_SUPPLIER, stored_by=AUTHOR, correlation_id=CORRELATION
        )
    _refused(error, ADAPTIVE_SAMPLE_REFUSED)
    assert stores.validation.sample_supplier(first.digest) == SUPPLIER
    assert _count(stores.path, SAMPLES) == len(SAMPLES_NAMES)


def test_a_run_replays_only_its_own_suppliers_samples(stores: Stores) -> None:
    hooked = _save_hooked(stores)
    captured = _save_samples(stores)  # synmart's
    bundle = stores.profiles.load_bundle(hooked)
    current = freshness_tuple(bundle, captured, hook_manifest())
    run = ValidationRun(Verdict.FAIL, (), current)
    with pytest.raises(InputValidationError) as error:
        stores.validation.record_run(
            bundle, run, captured, recorded_by=AUTHOR, correlation_id=CORRELATION
        )
    _refused(error, ADAPTIVE_RUN_REFUSED)
    assert _count(stores.path, RUNS) == 0


def test_the_database_links_only_samples_of_the_runs_supplier_inside_its_count(
    stores: Stores, negatives: Negatives
) -> None:
    digest, _ = _validated_profile(stores, negatives)
    foreign = _redigested(sample("on_sale"), expected_json=json.dumps({"x": 1}))
    stores.validation.save_sample(
        foreign, supplier_key=HOOKED_SUPPLIER, stored_by=AUTHOR, correlation_id=CORRELATION
    )
    (run_id,) = {s.run_id for s in stores.validation.runs(digest) if s.run.verdict is Verdict.PASS}
    first = samples()[0].digest
    with _raw(stores.path) as raw:
        for position, linked in ((3, first), (7, first), (0, foreign.digest)):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(
                    f"INSERT INTO {RUN_SAMPLES} VALUES (?, ?, ?)", (run_id, position, linked)
                )


def _pass_run_id(stores: Stores, digest: str) -> str:
    (run_id,) = {s.run_id for s in stores.validation.runs(digest) if s.run.verdict is Verdict.PASS}
    return run_id


def _links_out_of_band(path: Path, *statements: tuple[str, tuple[object, ...]]) -> None:
    with _raw(path) as raw:
        for trigger in ("no_update", "no_delete", "within_the_run"):
            raw.execute(f"DROP TRIGGER trg_{RUN_SAMPLES}_{trigger}")
        for statement, args in statements:
            raw.execute(statement, args)
        raw.commit()


@pytest.mark.parametrize("change", ["extra", "missing", "reordered", "replaced"])
def test_a_runs_links_changed_out_of_band_fail_closed_and_never_validate(
    stores: Stores, negatives: Negatives, change: str
) -> None:
    digest, run = _validated_profile(stores, negatives)
    run_id = _pass_run_id(stores, digest)
    first = samples()[0].digest
    other = _redigested(sample("on_sale"), expected_json=json.dumps({"y": 2}))
    stores.validation.save_sample(
        other, supplier_key=SUPPLIER, stored_by=AUTHOR, correlation_id=CORRELATION
    )
    where = "WHERE run_id = ? AND position = ?"
    statements = {
        "extra": [(f"INSERT INTO {RUN_SAMPLES} VALUES (?, 3, ?)", (run_id, first))],
        "missing": [(f"DELETE FROM {RUN_SAMPLES} {where}", (run_id, 2))],
        "reordered": [
            (f"UPDATE {RUN_SAMPLES} SET position = 9 {where}", (run_id, 0)),
            (f"UPDATE {RUN_SAMPLES} SET position = 0 {where}", (run_id, 1)),
            (f"UPDATE {RUN_SAMPLES} SET position = 1 {where}", (run_id, 9)),
        ],
        "replaced": [
            (f"UPDATE {RUN_SAMPLES} SET sample_digest = ? {where}", (other.digest, run_id, 2))
        ],
    }[change]
    _links_out_of_band(stores.path, *statements)
    with pytest.raises(AdaptiveTampered):
        stores.validation.runs(digest)
    with pytest.raises(AdaptiveTampered):
        stores.validation.is_validated(digest, run.freshness)


# ---------------------------------------------------------------- restart, audit, migration


def test_everything_survives_a_restart(
    stores: Stores, negatives: Negatives, clock: FakeClock
) -> None:
    digest, run = _validated_profile(stores, negatives)
    before = (
        stores.profiles.load_bundle(digest),
        stores.profiles.lifecycle(digest),
        stores.validation.runs(digest),
    )
    stores.db.dispose()
    reopened = _open(stores.path, clock)
    try:
        after = (
            reopened.profiles.load_bundle(digest),
            reopened.profiles.lifecycle(digest),
            reopened.validation.runs(digest),
        )
        assert after == before
        assert reopened.validation.is_validated(digest, run.freshness)
        assert reopened.validation.sample(samples()[0].digest) == samples()[0]
    finally:
        reopened.db.dispose()


def test_the_owner_writes_no_audit_event(stores: Stores, negatives: Negatives) -> None:
    # An audit event would move the review owners' write fence (AuditLog.owner_writes).
    digest, _ = _validated_profile(stores, negatives)
    stores.profiles.retire(digest, actor=AUTHOR, reason="DONE", correlation_id=CORRELATION)
    stores.validation.prune_unreferenced_samples()
    assert _count(stores.path, "audit_events") == 0


def _schema(path: Path) -> dict[str, str]:
    with _raw(path) as raw:
        rows = raw.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
        return dict(rows.fetchall())


def test_0024_is_additive_and_keeps_every_existing_object_and_row(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = _url(database)
    command.upgrade(alembic_config(url), BEFORE_0024)
    with _raw(database) as raw:
        raw.execute(
            "INSERT INTO review_coverage (producer, full_passes, failures_recorded, updated_at)"
            " VALUES ('test.facts', 0, 0, '2026-09-13 00:00:00.000000')"
        )
        raw.commit()
        rows = raw.execute("SELECT * FROM review_coverage").fetchall()
    before = _schema(database)
    command.upgrade(alembic_config(url), AT_0024)
    after = _schema(database)
    assert {name: after[name] for name in before} == before
    assert {name for name in after if name.startswith(("adaptive_", "trg_adaptive_"))} >= set(
        TABLES
    )
    assert not {name for name in after if name not in before and "adaptive" not in name}
    with _raw(database) as raw:
        assert raw.execute("SELECT * FROM review_coverage").fetchall() == rows
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0024_downgrade_never_destroys_adaptive_history(
    tmp_path: Path, clock: FakeClock, negatives: Negatives
) -> None:
    database = tmp_path / "icbm.db"
    url = _url(database)
    upgrade_to_head(url)
    opened = _open(database, clock)
    try:
        opened.validation.save_sample(
            sample("on_sale"), supplier_key=SUPPLIER, stored_by=AUTHOR, correlation_id=CORRELATION
        )
    finally:
        opened.db.dispose()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), BEFORE_0024)
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == AT_0024
    finally:
        engine.dispose()
    assert _count(database, SAMPLES) == 1

    # Only when the owner's own retention leaves nothing does the downgrade drop the tables.
    opened = _open(database, clock)
    try:
        assert opened.validation.prune_unreferenced_samples() == 1
    finally:
        opened.db.dispose()
    command.downgrade(alembic_config(url), BEFORE_0024)
    assert not {name for name in _schema(database) if "adaptive" in name}
    upgrade_to_head(url)
    assert set(TABLES) <= set(_schema(database))
