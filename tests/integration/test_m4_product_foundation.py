"""The M4 canonical product foundation (Issue #80 PR-B, ADR-0013), on a migrated database.

Every invariant is proven by the write that would break it: the database, not only the store,
refuses it. No supplier, marketplace or AI provider is contacted, and no campaign root is opened.
"""

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import CheckConstraint

from app.collect.assets import DecodedImage, SourceAssetStore
from app.collect.revisions import ProductFactsRevisionStore
from app.config import AppConfig
from app.core.errors import InputValidationError, NotFoundError
from app.db.database import Database, create_sqlite_engine
from app.db.metadata import metadata
from app.db.migrate import alembic_config, current_revision, head_revision, upgrade_to_head
from app.products.model import (
    DEFAULT_SINGLE_UNIT_SIGNATURE,
    CompositionSpec,
    MemberStatus,
    MoveReason,
)
from app.products.store import MembershipChange, ProductFoundationStore, ProductFoundationUnit
from tests.collect_support import PNG, REPRESENTATIVE, absent, base_fields, collected
from tests.support import FakeClock

pytestmark = pytest.mark.integration

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
M3_TABLES = (
    "product_facts_revisions",
    "product_facts_fields",
    "product_facts_evidence",
    "source_assets",
    "product_facts_image_refs",
    "collection_runs",
)
AT = "'2026-09-18 00:00:00'"


class FakeDecoder:
    def decode(self, data: bytes) -> DecodedImage | None:
        return DecodedImage("image/png", 64, 64) if data.startswith(b"\x89PNG") else None


@pytest.fixture
def database(config: AppConfig) -> Iterator[Database]:
    db = Database(config.database_url)
    try:
        yield db
    finally:
        db.dispose()


@pytest.fixture
def store(database: Database) -> ProductFoundationStore:
    return ProductFoundationStore(database, FakeClock())


class Sources:
    """Synthetic M3 revisions. ``base_product`` states options and tiers ABSENT; ``with_options``
    states options CONFIRMED (an empty axis list), which is not proof of "no options"."""

    def __init__(self, config: AppConfig, database: Database) -> None:
        self._revisions = ProductFactsRevisionStore(database, FakeClock())
        assets = SourceAssetStore(config.source_assets_dir, database, FakeDecoder(), FakeClock())
        self._images = (replace(REPRESENTATIVE, sha256=assets.put(PNG).sha256),)

    def base_product(self, source_product_id: str = "1234", **overrides: object) -> str:
        fields = base_fields()
        fields["options"] = absent(".options")
        return self._revisions.append(
            collected(
                fields=fields,
                images=self._images,
                source_product_id=source_product_id,
                **overrides,
            )
        ).revision_id

    def with_options(self, source_product_id: str = "1234") -> str:
        return self._revisions.append(
            collected(images=self._images, source_product_id=source_product_id)
        ).revision_id


@pytest.fixture
def sources(config: AppConfig, database: Database) -> Sources:
    return Sources(config, database)


def _raw(config: AppConfig) -> sqlite3.Connection:
    raw = sqlite3.connect(config.data_dir / "runtime" / "icbm.db")
    raw.execute("PRAGMA foreign_keys=ON")
    return raw


def _confirm(store: ProductFoundationStore, group: str, source_uid: str) -> str:
    return store.confirm_new_member(
        group, source_uid, reason="MATERIALIZED", decided_by="test", correlation_id="cid"
    ).member_id


def _move(
    store: ProductFoundationStore, member: str, status: MemberStatus, reason: str = "X"
) -> MembershipChange:
    return store.change_member_status(
        member, status, reason=reason, decided_by="test", correlation_id="cid"
    )


def _single_member_item(
    store: ProductFoundationStore, source_uid: str, spec: CompositionSpec | None = None
) -> tuple[str, str, str]:
    group = store.create_group(decided_by="test")
    member = _confirm(store, group, source_uid)
    composition = store.composition(spec or CompositionSpec.default_single_unit())
    return group, member, store.item(group, composition.composition_id).item_id


def _membership_is_current(config: AppConfig, group: str) -> bool:
    """The detector the atomicity rule rests on: the newest membership revision names exactly
    the group's CONFIRMED set, and a group with a CONFIRMED member always has a revision."""
    with contextlib.closing(_raw(config)) as raw:
        confirmed = sorted(
            r[0]
            for r in raw.execute(
                "SELECT source_product_uid FROM group_members"
                " WHERE product_group_id = ? AND status = 'CONFIRMED'",
                (group,),
            )
        )
        latest = raw.execute(
            "SELECT members_json FROM group_membership_revisions WHERE product_group_id = ?"
            " ORDER BY revision_no DESC LIMIT 1",
            (group,),
        ).fetchone()
    if latest is None:
        return confirmed == []
    return sorted(json.loads(latest[0])) == confirmed


def _revisions(config: AppConfig, group: str) -> int:
    with contextlib.closing(_raw(config)) as raw:
        return int(
            raw.execute(
                "SELECT COUNT(*) FROM group_membership_revisions WHERE product_group_id = ?",
                (group,),
            ).fetchone()[0]
        )


# ---------------------------------------------------------------- schema shape


def test_the_foundation_tables_exist_and_hold_no_second_product_root() -> None:
    assert set(M4_TABLES) <= set(metadata.tables)
    assert "products" not in metadata.tables and "product" not in metadata.tables
    # ADR-0013 §4 / v3.1 §12.1 and §9.4: nothing on the canonical product names a source or a
    # duplicate allowance.
    assert {c.name for c in metadata.tables["product_groups"].columns} == {
        "product_group_id",
        "status",
        "decided_by",
        "created_at",
        "retired_at",
    }


def test_a_source_product_holds_identity_and_no_fact() -> None:
    assert {c.name for c in metadata.tables["source_products"].columns} == {
        "source_product_uid",
        "supplier_key",
        "source_product_id",
        "created_at",
    }


def test_no_source_sku_exists_to_be_fabricated_and_offers_are_product_level() -> None:
    # ADR-0013 §2/§6 (ruling B), clarified by ruling 5738760913 for PR-Q: nothing can hold an
    # invented source SKU. The one offer table holds product-level offers, which have no SKU, and
    # a binding references an offer only through its exact quantity_offer_id.
    assert not [t for t in metadata.tables if "sku" in t]
    assert [t for t in metadata.tables if "offer" in t] == ["quantity_offers"]
    for table in ("quantity_offers", "source_bindings"):
        assert not [c.name for c in metadata.tables[table].columns if "sku" in c.name], table
    binding_columns = {c.name for c in metadata.tables["source_bindings"].columns}
    assert [c for c in binding_columns if "offer" in c] == ["quantity_offer_id"]


def test_the_m4_checks_match_the_orm(config: AppConfig) -> None:
    # The CHECK expressions of 0012 are frozen literals; they must equal the models' expressions.
    with contextlib.closing(_raw(config)) as raw:
        for table in M4_TABLES:
            ddl = raw.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()[0]
            checks = [
                c for c in metadata.tables[table].constraints if isinstance(c, CheckConstraint)
            ]
            assert checks, table
            for check in checks:
                assert f"CHECK ({check.sqltext})" in ddl, f"{table}: {check.name}"


# ---------------------------------------------------------------- source identity


def test_one_source_product_per_identity(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    sources.base_product()
    first = store.source_product("kmretail", "1234")
    assert store.source_product("kmretail", "1234") == first
    with contextlib.closing(_raw(config)) as raw, pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            f"INSERT INTO source_products VALUES ('{uuid.uuid4()}', 'kmretail', '1234', {AT})"
        )


def test_an_identity_no_revision_names_is_never_invented(store: ProductFoundationStore) -> None:
    with pytest.raises(NotFoundError):
        store.source_product("kmretail", "no-such-product")


def test_a_source_product_is_append_only(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    sources.base_product()
    store.source_product("kmretail", "1234")
    with contextlib.closing(_raw(config)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE source_products SET source_product_id = '9999'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM source_products")


# ---------------------------------------------------------------- current source revision


def test_the_pointer_history_is_appended_in_order_from_the_current_revision(
    store: ProductFoundationStore, sources: Sources
) -> None:
    first = sources.base_product()
    second = sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    assert store.current_source_revision(uid) is None  # nothing is inferred from sequence
    opened = store.record_move(
        uid, first, reason=MoveReason.INITIAL, decided_by="test", correlation_id="cid"
    )
    moved = store.record_move(
        uid, second, reason=MoveReason.NEWER_REVISION, decided_by="test", correlation_id="cid"
    )
    assert (opened.sequence, opened.previous_revision_id) == (1, None)
    assert (moved.sequence, moved.previous_revision_id) == (2, first)
    assert store.current_source_revision(uid) == second


def test_the_pointer_never_crosses_source_identities(
    store: ProductFoundationStore, sources: Sources
) -> None:
    sources.base_product("1234")
    other = sources.base_product("5678")
    uid = store.source_product("kmretail", "1234").source_product_uid
    with pytest.raises(Exception, match="another source identity"):
        store.record_move(
            uid, other, reason=MoveReason.INITIAL, decided_by="test", correlation_id="cid"
        )
    assert store.current_source_revision(uid) is None


def test_a_move_cannot_skip_or_rewrite_the_chain(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    first = sources.base_product()
    second = sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    store.record_move(uid, first, reason=MoveReason.INITIAL, decided_by="t", correlation_id="c")
    insert = (
        "INSERT INTO current_source_revision_moves VALUES ('{m}', '{u}', {seq}, '{r}', {prev},"
        " 'NEWER_REVISION', NULL, 't', 'c', " + AT + ")"
    )
    with contextlib.closing(_raw(config)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="appended in order"):
            raw.execute(insert.format(m=uuid.uuid4(), u=uid, seq=3, r=second, prev=f"'{first}'"))
        with pytest.raises(sqlite3.IntegrityError, match="starts from the current"):
            raw.execute(insert.format(m=uuid.uuid4(), u=uid, seq=2, r=first, prev=f"'{second}'"))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE current_source_revision_moves SET revision_id = revision_id")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM current_source_revision_moves")


def test_initial_opens_the_history_and_nothing_else_does(
    store: ProductFoundationStore, sources: Sources
) -> None:
    first = sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    with pytest.raises(InputValidationError):
        store.record_move(
            uid, first, reason=MoveReason.NEWER_REVISION, decided_by="t", correlation_id="c"
        )


def test_a_pointer_move_creates_no_membership_revision(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    first = sources.base_product()
    second = sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    group, _member, _item = _single_member_item(store, uid)
    assert _revisions(config, group) == 1
    store.record_move(uid, first, reason=MoveReason.INITIAL, decided_by="t", correlation_id="c")
    store.record_move(
        uid, second, reason=MoveReason.NEWER_REVISION, decided_by="t", correlation_id="c"
    )
    assert _revisions(config, group) == 1
    assert _membership_is_current(config, group)


# ---------------------------------------------------------------- groups and membership


def test_a_source_product_is_confirmed_in_at_most_one_group(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    first, second = store.create_group(decided_by="t"), store.create_group(decided_by="t")
    _confirm(store, first, uid)
    # A candidate elsewhere is fine; it is not canonical.
    candidate = store.add_candidate(second, uid, decided_by="t")
    with pytest.raises(Exception, match="UNIQUE"):
        _move(store, candidate, MemberStatus.CONFIRMED)
    # The refused confirmation left nothing behind: no status change and no revision.
    assert _revisions(config, second) == 0
    assert _membership_is_current(config, first) and _membership_is_current(config, second)


def test_member_transitions_are_one_way_and_identity_is_fixed(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    group = store.create_group(decided_by="t")
    member = store.add_candidate(group, uid, decided_by="t")
    _move(store, member, MemberStatus.REJECTED)
    with pytest.raises(InputValidationError):
        _move(store, member, MemberStatus.CONFIRMED)
    with contextlib.closing(_raw(config)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="transition not allowed"):
            raw.execute(
                f"UPDATE group_members SET status = 'CONFIRMED' WHERE member_id = '{member}'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE group_members SET product_group_id = 'elsewhere'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM group_members")


def test_a_group_is_only_ever_retired_and_never_deleted(
    config: AppConfig, store: ProductFoundationStore
) -> None:
    group = store.create_group(decided_by="t")
    with contextlib.closing(_raw(config)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM product_groups")
        raw.execute(
            f"UPDATE product_groups SET status = 'RETIRED', retired_at = {AT}"
            f" WHERE product_group_id = '{group}'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="only ACTIVE to RETIRED"):
            raw.execute("UPDATE product_groups SET status = 'ACTIVE', retired_at = NULL")


def test_a_membership_revision_is_the_complete_confirmed_set_and_immutable(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    sources.base_product("1234")
    sources.base_product("5678")
    confirmed = store.source_product("kmretail", "1234").source_product_uid
    candidate = store.source_product("kmretail", "5678").source_product_uid
    group = store.create_group(decided_by="t")
    revision = store.confirm_new_member(
        group, confirmed, reason="MATERIALIZED", decided_by="t", correlation_id="c"
    ).revision
    store.add_candidate(group, candidate, decided_by="t")
    assert revision is not None
    assert (revision.revision_no, revision.source_product_uids) == (1, (confirmed,))
    insert = (
        "INSERT INTO group_membership_revisions VALUES ('{i}', '{g}', 2, '{m}', {n},"
        " 'X', 't', 'c', " + AT + ")"
    )
    with contextlib.closing(_raw(config)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="only CONFIRMED members"):
            raw.execute(
                insert.format(i=uuid.uuid4(), g=group, m=f'["{confirmed}", "{candidate}"]', n=2)
            )
        with pytest.raises(sqlite3.IntegrityError, match="complete CONFIRMED set"):
            raw.execute(insert.format(i=uuid.uuid4(), g=group, m="[]", n=0))
        with pytest.raises(sqlite3.IntegrityError, match="each member once"):
            raw.execute(
                insert.format(i=uuid.uuid4(), g=group, m=f'["{confirmed}", "{confirmed}"]', n=2)
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE group_membership_revisions SET members_json = '[]'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM group_membership_revisions")


def test_every_confirmed_set_change_appends_its_revision_atomically(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    # PR #82 review 5247426764 blocker 2: the CONFIRMED set and its newest revision never differ.
    for product in ("1111", "2222", "3333"):
        sources.base_product(product)
    a, b, c = (
        store.source_product("kmretail", p).source_product_uid for p in ("1111", "2222", "3333")
    )
    group = store.create_group(decided_by="t")
    assert _revisions(config, group) == 0 and _membership_is_current(config, group)

    first = store.confirm_new_member(
        group, a, reason="MATERIALIZED", decided_by="t", correlation_id="c"
    )
    assert first.revision is not None and first.revision.revision_no == 1
    assert first.revision.source_product_uids == (a,)
    assert store.current_membership_revision(group) == first.revision

    candidate = store.add_candidate(group, b, decided_by="t")  # not canonical: no revision
    assert _revisions(config, group) == 1 and _membership_is_current(config, group)

    promoted = _move(store, candidate, MemberStatus.CONFIRMED, "CONFIRMED")
    assert promoted.revision is not None and promoted.revision.revision_no == 2
    assert set(promoted.revision.source_product_uids) == {a, b}

    rejected_candidate = store.add_candidate(group, c, decided_by="t")
    assert _move(store, rejected_candidate, MemberStatus.REJECTED).revision is None
    assert _revisions(config, group) == 2 and _membership_is_current(config, group)

    demoted = _move(store, first.member_id, MemberStatus.REJECTED, "UNMERGED")
    assert demoted.revision is not None and demoted.revision.revision_no == 3
    assert demoted.revision.source_product_uids == (b,)
    assert _membership_is_current(config, group)


def test_a_failed_snapshot_rolls_the_member_change_back(
    config: AppConfig,
    store: ProductFoundationStore,
    sources: Sources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources.base_product("1111")
    sources.base_product("2222")
    a = store.source_product("kmretail", "1111").source_product_uid
    b = store.source_product("kmretail", "2222").source_product_uid
    group = store.create_group(decided_by="t")
    # The database refuses the snapshot (an empty reason): the new CONFIRMED member goes too.
    with pytest.raises(Exception, match="reason_present"):
        store.confirm_new_member(group, a, reason="", decided_by="t", correlation_id="c")
    with contextlib.closing(_raw(config)) as raw:
        assert raw.execute("SELECT COUNT(*) FROM group_members").fetchone()[0] == 0
    _confirm(store, group, a)
    candidate = store.add_candidate(group, b, decided_by="t")

    # Any failure while appending the snapshot undoes the status change as well.
    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("snapshot refused")

    monkeypatch.setattr(ProductFoundationUnit, "_append_membership_revision", refuse)
    with pytest.raises(RuntimeError, match="snapshot refused"):
        _move(store, candidate, MemberStatus.CONFIRMED)
    with contextlib.closing(_raw(config)) as raw:
        status = raw.execute(
            "SELECT status FROM group_members WHERE member_id = ?", (candidate,)
        ).fetchone()[0]
    assert status == "CANDIDATE"
    assert _revisions(config, group) == 1 and _membership_is_current(config, group)


def test_the_currentness_detector_fires_on_a_bypassed_change(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    # The detector itself must be able to fail: a status change committed outside the store,
    # with no revision, leaves the newest revision behind the CONFIRMED set.
    sources.base_product("1111")
    sources.base_product("2222")
    a = store.source_product("kmretail", "1111").source_product_uid
    b = store.source_product("kmretail", "2222").source_product_uid
    group = store.create_group(decided_by="t")
    _confirm(store, group, a)
    candidate = store.add_candidate(group, b, decided_by="t")
    with contextlib.closing(_raw(config)) as raw:
        raw.execute(
            f"UPDATE group_members SET status = 'CONFIRMED' WHERE member_id = '{candidate}'"
        )
        raw.commit()
    assert not _membership_is_current(config, group)


# ---------------------------------------------------------------- composition and Item


def test_a_composition_is_one_row_per_structure_and_immutable(
    config: AppConfig, store: ProductFoundationStore
) -> None:
    first = store.composition(CompositionSpec(quantity=2, unit_amount="500", unit_code="ml"))
    again = store.composition(CompositionSpec(quantity=2, unit_amount="500.0", unit_code="ml"))
    assert first == again
    with contextlib.closing(_raw(config)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE listing_compositions SET quantity = 3")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM listing_compositions")


def test_an_item_is_unique_per_group_and_composition_signature(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    group, _member, item = _single_member_item(store, uid)
    composition = store.composition(CompositionSpec.default_single_unit())
    assert store.item(group, composition.composition_id).item_id == item
    duplicate = (
        f"INSERT INTO product_items VALUES ('{uuid.uuid4()}', '{group}',"
        f" '{composition.composition_id}', '{composition.composition_signature}', {AT})"
    )
    other = store.composition(CompositionSpec(quantity=2))
    forged = (
        f"INSERT INTO product_items VALUES ('{uuid.uuid4()}', '{group}',"
        f" '{other.composition_id}', '{composition.composition_signature}', {AT})"
    )
    with contextlib.closing(_raw(config)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            raw.execute(duplicate)
        with pytest.raises(sqlite3.IntegrityError, match="composition signature"):
            raw.execute(forged)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM product_items")


# ---------------------------------------------------------------- source binding


def test_a_base_product_binding_references_no_sku_or_offer_and_creates_nothing_else(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    revision = sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    _group, member, item = _single_member_item(store, uid)
    with contextlib.closing(_raw(config)) as raw:
        before = {
            t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in metadata.tables
        }
    binding = store.bind_base_product(item, member, revision, decided_by="t", correlation_id="c")
    with contextlib.closing(_raw(config)) as raw:
        after = {t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in metadata.tables}
        row = raw.execute(
            "SELECT binding_kind, fulfillment_quantity, provenance_revision_id, provenance_fields"
            " FROM source_bindings"
        ).fetchone()
    assert {t for t in after if after[t] != before[t]} == {"source_bindings"}
    assert row[:3] == ("BASE_PRODUCT", 1, revision)
    assert '"options"' in row[3] and '"quantity_tiers"' in row[3]
    assert binding.binding_kind == "BASE_PRODUCT"


def test_source_offer_cannot_be_stored_without_an_exact_offer(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    # PR-Q (ruling 5738760913): SOURCE_OFFER always names its exact QuantityOffer, and a
    # BASE_PRODUCT names none. The full SOURCE_OFFER rules are in test_m4_quantity_offers.
    revision = sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    _group, member, item = _single_member_item(store, uid)
    with contextlib.closing(_raw(config)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="SOURCE_OFFER names its exact offer"):
            raw.execute(
                f"INSERT INTO source_bindings VALUES ('{uuid.uuid4()}', '{item}', '{member}',"
                f" 'SOURCE_OFFER', 1, '{revision}', '[\"shipping\"]', 't', 'c', {AT}, NULL, NULL)"
            )
        with pytest.raises(sqlite3.IntegrityError, match="source_offer_names_its_offer"):
            raw.execute(
                f"INSERT INTO source_bindings VALUES ('{uuid.uuid4()}', '{item}', '{member}',"
                f" 'BASE_PRODUCT', 1, '{revision}', '[\"prices\"]', 't', 'c', {AT}, NULL,"
                f" '{uuid.uuid4()}')"
            )


def test_base_product_needs_a_revision_stating_no_options_and_no_tiers(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    sources.base_product()
    with_options = sources.with_options()
    uid = store.source_product("kmretail", "1234").source_product_uid
    _group, member, item = _single_member_item(store, uid)
    with pytest.raises(InputValidationError):
        store.bind_base_product(item, member, with_options, decided_by="t", correlation_id="c")
    with (
        contextlib.closing(_raw(config)) as raw,
        pytest.raises(sqlite3.IntegrityError, match="no options and no quantity tiers"),
    ):
        raw.execute(
            f"INSERT INTO source_bindings VALUES ('{uuid.uuid4()}', '{item}', '{member}',"
            f" 'BASE_PRODUCT', 1, '{with_options}', '[\"prices\"]', 't', 'c', {AT}, NULL, NULL)"
        )


NON_DEFAULT_UNITS = {
    "two units": CompositionSpec(quantity=2),
    "one pack of two": CompositionSpec(quantity=1, pack_count=2),
    "one with six per pack": CompositionSpec(quantity=1, pack_count=1, units_per_pack=6),
    "one stated unit": CompositionSpec(quantity=1, unit_amount="90", unit_code="tablet"),
    "one stated total": CompositionSpec(
        quantity=1, unit_amount="500", unit_code="g", total_amount="1000"
    ),
}


@pytest.mark.parametrize("spec", NON_DEFAULT_UNITS.values(), ids=NON_DEFAULT_UNITS.keys())
def test_base_product_fulfils_only_the_exact_default_single_unit(
    config: AppConfig, store: ProductFoundationStore, sources: Sources, spec: CompositionSpec
) -> None:
    # PR #82 review 5247426764 blocker 1: quantity 1 is not enough. A quantity-1 pack or unit
    # structure is a seller configuration the source never proved.
    revision = sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    _group, member, item = _single_member_item(store, uid, spec)
    with pytest.raises(InputValidationError):
        store.bind_base_product(item, member, revision, decided_by="t", correlation_id="c")
    with (
        contextlib.closing(_raw(config)) as raw,
        pytest.raises(sqlite3.IntegrityError, match="default single-unit composition"),
    ):
        raw.execute(
            f"INSERT INTO source_bindings VALUES ('{uuid.uuid4()}', '{item}', '{member}',"
            f" 'BASE_PRODUCT', 1, '{revision}', '[\"prices\"]', 't', 'c', {AT}, NULL, NULL)"
        )


@pytest.mark.parametrize(
    ("signature", "pack_count"),
    [("f" * 64, "NULL"), (DEFAULT_SINGLE_UNIT_SIGNATURE, "2")],
    ids=["default fields, forged signature", "default signature, forged pack"],
)
def test_a_forged_default_unit_cannot_carry_a_base_product_binding(
    config: AppConfig,
    store: ProductFoundationStore,
    sources: Sources,
    signature: str,
    pack_count: str,
) -> None:
    revision = sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    group = store.create_group(decided_by="t")
    member = _confirm(store, group, uid)
    composition, item = str(uuid.uuid4()), str(uuid.uuid4())
    with contextlib.closing(_raw(config)) as raw:
        raw.execute(
            f"INSERT INTO listing_compositions VALUES ('{composition}', 1, NULL, NULL,"
            f" {pack_count}, NULL, NULL, '{signature}', 'composition-signature/v1', {AT})"
        )
        raw.execute(
            f"INSERT INTO product_items VALUES ('{item}', '{group}', '{composition}',"
            f" '{signature}', {AT})"
        )
        with pytest.raises(sqlite3.IntegrityError, match="default single-unit composition"):
            raw.execute(
                f"INSERT INTO source_bindings VALUES ('{uuid.uuid4()}', '{item}', '{member}',"
                f" 'BASE_PRODUCT', 1, '{revision}', '[\"prices\"]', 't', 'c', {AT}, NULL, NULL)"
            )


def test_a_binding_stays_inside_its_group_and_its_source_identity(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    revision = sources.base_product("1234")
    foreign = sources.base_product("5678")
    uid = store.source_product("kmretail", "1234").source_product_uid
    other_uid = store.source_product("kmretail", "5678").source_product_uid
    _group, member, item = _single_member_item(store, uid)
    _other_group, other_member, _other_item = _single_member_item(store, other_uid)
    with pytest.raises(Exception, match="CONFIRMED in the group of the Item"):
        store.bind_base_product(item, other_member, foreign, decided_by="t", correlation_id="c")
    with pytest.raises(Exception, match="another source identity"):
        store.bind_base_product(item, member, foreign, decided_by="t", correlation_id="c")
    store.bind_base_product(item, member, revision, decided_by="t", correlation_id="c")


def test_a_binding_is_only_ever_closed_with_one_open_per_item(
    config: AppConfig, store: ProductFoundationStore, sources: Sources
) -> None:
    first = sources.base_product()
    second = sources.base_product()
    uid = store.source_product("kmretail", "1234").source_product_uid
    _group, member, item = _single_member_item(store, uid)
    store.bind_base_product(item, member, first, decided_by="t", correlation_id="c")
    store.bind_base_product(item, member, second, decided_by="t", correlation_id="c")
    with contextlib.closing(_raw(config)) as raw:
        rows = raw.execute(
            "SELECT provenance_revision_id, valid_to IS NULL FROM source_bindings"
        ).fetchall()
        assert sorted(rows, key=lambda r: r[1]) == [(first, 0), (second, 1)]
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            raw.execute(
                f"INSERT INTO source_bindings VALUES ('{uuid.uuid4()}', '{item}', '{member}',"
                f" 'BASE_PRODUCT', 1, '{second}', '[\"prices\"]', 't', 'c', {AT}, NULL, NULL)"
            )
        with pytest.raises(sqlite3.IntegrityError, match="only ever closed"):
            raw.execute("UPDATE source_bindings SET valid_to = NULL")
        with pytest.raises(sqlite3.IntegrityError, match="only ever closed"):
            raw.execute("UPDATE source_bindings SET decided_by = 'someone' WHERE valid_to IS NULL")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM source_bindings")


# ---------------------------------------------------------------- migration and downgrade


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _m3_rows(database: Path) -> dict[str, list[tuple[object, ...]]]:
    with contextlib.closing(sqlite3.connect(database)) as raw:
        return {
            table: raw.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in M3_TABLES
        }


def _count(database: Path, table: str) -> int:
    with contextlib.closing(sqlite3.connect(database)) as raw:
        return int(raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_0011_to_0012_preserves_m3_source_truth_and_backfills_only_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "icbm.db"
    url = _url(database)
    command.upgrade(alembic_config(url), "0011_m3_image_reference_diagnostics")
    db = Database(url)
    try:
        assets = SourceAssetStore(tmp_path / "source-assets", db, FakeDecoder(), FakeClock())
        images = (replace(REPRESENTATIVE, sha256=assets.put(PNG).sha256),)
        revisions = ProductFactsRevisionStore(db, FakeClock())
        for product in ("1234", "1234", "5678"):
            revisions.append(collected(images=images, source_product_id=product))
    finally:
        db.dispose()
    before = _m3_rows(database)
    upgrade_to_head(url)
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()
    assert _m3_rows(database) == before  # every M3 row, field for field
    with contextlib.closing(sqlite3.connect(database)) as raw:
        identities = raw.execute(
            "SELECT supplier_key, source_product_id FROM source_products ORDER BY 2"
        ).fetchall()
    assert identities == [("kmretail", "1234"), ("kmretail", "5678")]
    # No pointer, group, member, composition, Item or binding is guessed by the migration.
    for table in M4_TABLES[1:]:
        assert _count(database, table) == 0, table


def test_downgrade_drops_only_the_re_derivable_backfill(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = _url(database)
    command.upgrade(alembic_config(url), "0011_m3_image_reference_diagnostics")
    db = Database(url)
    try:
        assets = SourceAssetStore(tmp_path / "source-assets", db, FakeDecoder(), FakeClock())
        images = (replace(REPRESENTATIVE, sha256=assets.put(PNG).sha256),)
        ProductFactsRevisionStore(db, FakeClock()).append(collected(images=images))
    finally:
        db.dispose()
    before = _m3_rows(database)
    upgrade_to_head(url)
    command.downgrade(alembic_config(url), "0011_m3_image_reference_diagnostics")
    assert _m3_rows(database) == before
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0011_m3_image_reference_diagnostics"
    finally:
        engine.dispose()


def test_downgrade_refuses_to_destroy_m4_product_truth(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = _url(database)
    upgrade_to_head(url)
    db = Database(url)
    try:
        ProductFoundationStore(db, FakeClock()).create_group(decided_by="t")
    finally:
        db.dispose()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0011_m3_image_reference_diagnostics")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0012_m4_product_foundation"
    finally:
        engine.dispose()
    assert _count(database, "product_groups") == 1


def test_downgrade_refuses_an_identity_that_is_not_re_derivable(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = _url(database)
    upgrade_to_head(url)
    with contextlib.closing(sqlite3.connect(database)) as raw:
        raw.execute(f"INSERT INTO source_products VALUES ('{uuid.uuid4()}', 'kmretail', 'x', {AT})")
        raw.commit()
    with pytest.raises(RuntimeError, match="not re-derivable"):
        command.downgrade(alembic_config(url), "0011_m3_image_reference_diagnostics")
    assert _count(database, "source_products") == 1
