"""M4 PR-B: the canonical product foundation — source identity, pointer history, groups, members,
membership revisions, change events, compositions, Items and bindings.

Revision ID: 0012_m4_product_foundation
Revises: 0011_m3_image_reference_diagnostics
Create Date: 2026-09-18

Issue #80 PR-B, under ADR-0013. It is additive and data-preserving: no M3 table, row or trigger is
touched.

**The only backfill.** One ``source_products`` row is created for each distinct
``(supplier_key, source_product_id)`` that existing revisions already name. That identity is
logically certain. The backfill stops there. It moves no current source revision pointer (nothing is
inferred from the revision sequence), and it creates no group, member, composition, Item or binding:
those are decisions PR-C makes, never guesses a migration makes.

**Where invariants live.** The CHECK literals are frozen with this revision, and the integration
tests compare them with the ORM models. The triggers enforce the cross-row invariants the CHECKs
cannot express:
- a pointer move names a revision of its own source identity, extends the history in order, and
  starts from the current source revision;
- a member joins, or is confirmed into, only an ACTIVE group;
- a membership revision is a complete snapshot of exactly the CONFIRMED members;
- an Item's signature is its composition's own;
- a binding binds a CONFIRMED member of the Item's group, from a revision of that member's
  identity. A ``BASE_PRODUCT`` binding also needs that revision to state ``options`` and
  ``quantity_tiers`` as ABSENT, and a single-unit composition.

**What can change after a row is written.** Only three things:
- a group may be retired;
- a member may move through ``CANDIDATE -> CONFIRMED | REJECTED`` and ``CONFIRMED -> REJECTED``;
- an open binding may be closed.

Everything else rejects UPDATE, and every table rejects DELETE.

**Downgrade fails closed.** It refuses while any M4 table other than ``source_products`` holds a
row. It also refuses while a ``source_products`` row names an identity that no revision states: such
a row is M4 truth, not the backfill, and dropping it would destroy it silently. Only the
re-derivable backfill may be dropped.
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0012_m4_product_foundation"
down_revision: str | None = "0011_m3_image_reference_diagnostics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_PRODUCTS = "source_products"
MOVES = "current_source_revision_moves"
GROUPS = "product_groups"
MEMBERS = "group_members"
MEMBERSHIP = "group_membership_revisions"
CHANGES = "group_change_events"
COMPOSITIONS = "listing_compositions"
ITEMS = "product_items"
BINDINGS = "source_bindings"
REVISIONS = "product_facts_revisions"
FIELDS = "product_facts_fields"
# Creation order; dropped in reverse.
TABLES = (
    SOURCE_PRODUCTS,
    MOVES,
    GROUPS,
    MEMBERS,
    MEMBERSHIP,
    CHANGES,
    COMPOSITIONS,
    ITEMS,
    BINDINGS,
)
# Tables that reject every UPDATE; the other three allow only the changes listed above.
FULLY_APPEND_ONLY = (SOURCE_PRODUCTS, MOVES, MEMBERSHIP, CHANGES, COMPOSITIONS, ITEMS)

# Vocabularies frozen with this revision (app.products.model).
_MOVE_REASONS = ("INITIAL", "NEWER_REVISION", "EXTRACTOR_CHANGED", "EXPLICIT_DECISION")
_GROUP_STATUSES = ("ACTIVE", "RETIRED")
_MEMBER_STATUSES = ("CANDIDATE", "CONFIRMED", "REJECTED")
_CHANGE_TYPES = ("MERGE", "SPLIT")
_BINDING_KINDS = ("SOURCE_OFFER", "BASE_PRODUCT")
_SIGNATURE_VERSION = "composition-signature/v1"


def _in(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _json_array(column: str, *, minimum: int) -> str:
    return (
        f"json_valid({column}) AND json_type({column}) = 'array'"
        f" AND json_array_length({column}) >= {minimum}"
    )


def _decimal(column: str) -> str:
    return f"{column} IS NULL OR ({column} <> '' AND {column} NOT GLOB '*[^0-9.]*')"


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _fk(table: str, column: str, target: str, target_column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.{target_column}"], name=op.f(f"fk_{table}_{column}_{target}")
    )


def _trigger(name: str, event: str, table: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def upgrade() -> None:
    op.create_table(
        SOURCE_PRODUCTS,
        sa.Column("source_product_uid", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("source_product_id", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(SOURCE_PRODUCTS, "supplier_key <> ''", "supplier_key_present"),
        _check(SOURCE_PRODUCTS, "source_product_id <> ''", "source_identity_present"),
        sa.PrimaryKeyConstraint("source_product_uid", name=op.f(f"pk_{SOURCE_PRODUCTS}")),
        sa.UniqueConstraint(
            "supplier_key",
            "source_product_id",
            name=op.f(f"uq_{SOURCE_PRODUCTS}_supplier_key_source_product_id"),
        ),
    )
    op.create_table(
        MOVES,
        sa.Column("move_id", sa.String(length=36), nullable=False),
        sa.Column("source_product_uid", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("previous_revision_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(MOVES, "sequence >= 1", "sequence_positive"),
        _check(MOVES, _in("reason", _MOVE_REASONS), "reason_valid"),
        _check(
            MOVES, "(sequence = 1) = (previous_revision_id IS NULL)", "first_move_has_no_previous"
        ),
        _check(MOVES, "(reason = 'INITIAL') = (sequence = 1)", "initial_opens_history"),
        _check(
            MOVES,
            "previous_revision_id IS NULL OR previous_revision_id <> revision_id",
            "move_changes_revision",
        ),
        _check(MOVES, "rule_version IS NULL OR rule_version <> ''", "rule_version_present"),
        _check(MOVES, "decided_by <> ''", "decided_by_present"),
        _check(MOVES, "correlation_id <> ''", "correlation_present"),
        _fk(MOVES, "source_product_uid", SOURCE_PRODUCTS, "source_product_uid"),
        _fk(MOVES, "revision_id", REVISIONS, "revision_id"),
        _fk(MOVES, "previous_revision_id", REVISIONS, "revision_id"),
        sa.PrimaryKeyConstraint("move_id", name=op.f(f"pk_{MOVES}")),
        sa.UniqueConstraint(
            "source_product_uid", "sequence", name=op.f(f"uq_{MOVES}_source_product_uid_sequence")
        ),
    )
    op.create_table(
        GROUPS,
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        _check(GROUPS, _in("status", _GROUP_STATUSES), "status_valid"),
        _check(
            GROUPS, "(status = 'RETIRED') = (retired_at IS NOT NULL)", "retired_when_retired_at"
        ),
        _check(GROUPS, "decided_by <> ''", "decided_by_present"),
        sa.PrimaryKeyConstraint("product_group_id", name=op.f(f"pk_{GROUPS}")),
    )
    op.create_table(
        MEMBERS,
        sa.Column("member_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("source_product_uid", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("match_method", sa.String(length=40), nullable=True),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("match_strategy_version", sa.String(length=64), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        _check(MEMBERS, _in("status", _MEMBER_STATUSES), "status_valid"),
        _check(MEMBERS, "match_method IS NULL OR match_method <> ''", "match_method_present"),
        _check(
            MEMBERS,
            "match_confidence IS NULL OR (match_confidence >= 0 AND match_confidence <= 1)",
            "match_confidence_range",
        ),
        _check(MEMBERS, "decided_by <> ''", "decided_by_present"),
        _fk(MEMBERS, "product_group_id", GROUPS, "product_group_id"),
        _fk(MEMBERS, "source_product_uid", SOURCE_PRODUCTS, "source_product_uid"),
        sa.PrimaryKeyConstraint("member_id", name=op.f(f"pk_{MEMBERS}")),
        sa.UniqueConstraint(
            "product_group_id",
            "source_product_uid",
            name=op.f(f"uq_{MEMBERS}_product_group_id_source_product_uid"),
        ),
    )
    op.create_index(
        "ux_group_members_confirmed_source",
        MEMBERS,
        ["source_product_uid"],
        unique=True,
        sqlite_where=sa.text("status = 'CONFIRMED'"),
    )
    op.create_table(
        MEMBERSHIP,
        sa.Column("membership_revision_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("members_json", sa.Text(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(MEMBERSHIP, "revision_no >= 1", "revision_no_positive"),
        _check(MEMBERSHIP, _json_array("members_json", minimum=0), "members_is_array"),
        _check(
            MEMBERSHIP,
            "member_count >= 0 AND json_array_length(members_json) = member_count",
            "member_count_matches",
        ),
        _check(MEMBERSHIP, "reason <> ''", "reason_present"),
        _check(MEMBERSHIP, "decided_by <> ''", "decided_by_present"),
        _check(MEMBERSHIP, "correlation_id <> ''", "correlation_present"),
        _fk(MEMBERSHIP, "product_group_id", GROUPS, "product_group_id"),
        sa.PrimaryKeyConstraint("membership_revision_id", name=op.f(f"pk_{MEMBERSHIP}")),
        sa.UniqueConstraint(
            "product_group_id",
            "revision_no",
            name=op.f(f"uq_{MEMBERSHIP}_product_group_id_revision_no"),
        ),
    )
    op.create_table(
        CHANGES,
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=10), nullable=False),
        sa.Column("predecessor_group_ids", sa.Text(), nullable=False),
        sa.Column("successor_group_ids", sa.Text(), nullable=False),
        sa.Column("membership_revision_id", sa.String(length=36), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(CHANGES, _in("event_type", _CHANGE_TYPES), "event_type_valid"),
        _check(CHANGES, _json_array("predecessor_group_ids", minimum=1), "predecessors_is_array"),
        _check(CHANGES, _json_array("successor_group_ids", minimum=1), "successors_is_array"),
        _check(CHANGES, "decided_by <> ''", "decided_by_present"),
        _check(CHANGES, "correlation_id <> ''", "correlation_present"),
        _fk(CHANGES, "membership_revision_id", MEMBERSHIP, "membership_revision_id"),
        sa.PrimaryKeyConstraint("event_id", name=op.f(f"pk_{CHANGES}")),
    )
    op.create_table(
        COMPOSITIONS,
        sa.Column("composition_id", sa.String(length=36), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_amount", sa.String(length=40), nullable=True),
        sa.Column("unit_code", sa.String(length=16), nullable=True),
        sa.Column("pack_count", sa.Integer(), nullable=True),
        sa.Column("units_per_pack", sa.Integer(), nullable=True),
        sa.Column("total_amount", sa.String(length=40), nullable=True),
        sa.Column("composition_signature", sa.String(length=64), nullable=False),
        sa.Column("signature_version", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(COMPOSITIONS, "quantity >= 1", "quantity_positive"),
        _check(COMPOSITIONS, _decimal("unit_amount"), "unit_amount_decimal"),
        _check(
            COMPOSITIONS,
            "unit_code IS NULL OR (unit_code <> '' AND unit_code NOT GLOB '*[^a-z0-9]*')",
            "unit_code_token",
        ),
        _check(COMPOSITIONS, "(unit_amount IS NULL) = (unit_code IS NULL)", "unit_stated_together"),
        _check(COMPOSITIONS, "pack_count IS NULL OR pack_count >= 1", "pack_count_positive"),
        _check(
            COMPOSITIONS, "units_per_pack IS NULL OR units_per_pack >= 1", "units_per_pack_positive"
        ),
        _check(COMPOSITIONS, _decimal("total_amount"), "total_amount_decimal"),
        _check(COMPOSITIONS, "total_amount IS NULL OR unit_code IS NOT NULL", "total_has_unit"),
        _check(COMPOSITIONS, _hex64("composition_signature"), "signature_hex"),
        _check(COMPOSITIONS, f"signature_version = '{_SIGNATURE_VERSION}'", "signature_version"),
        sa.PrimaryKeyConstraint("composition_id", name=op.f(f"pk_{COMPOSITIONS}")),
        sa.UniqueConstraint(
            "composition_signature", name=op.f(f"uq_{COMPOSITIONS}_composition_signature")
        ),
    )
    op.create_table(
        ITEMS,
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("composition_id", sa.String(length=36), nullable=False),
        sa.Column("composition_signature", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(ITEMS, _hex64("composition_signature"), "signature_hex"),
        _fk(ITEMS, "product_group_id", GROUPS, "product_group_id"),
        _fk(ITEMS, "composition_id", COMPOSITIONS, "composition_id"),
        sa.PrimaryKeyConstraint("item_id", name=op.f(f"pk_{ITEMS}")),
        sa.UniqueConstraint(
            "product_group_id",
            "composition_signature",
            name=op.f(f"uq_{ITEMS}_product_group_id_composition_signature"),
        ),
    )
    op.create_table(
        BINDINGS,
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("group_member_id", sa.String(length=36), nullable=False),
        sa.Column("binding_kind", sa.String(length=20), nullable=False),
        sa.Column("fulfillment_quantity", sa.Integer(), nullable=False),
        sa.Column("provenance_revision_id", sa.String(length=36), nullable=False),
        sa.Column("provenance_fields", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        _check(BINDINGS, _in("binding_kind", _BINDING_KINDS), "binding_kind_valid"),
        _check(BINDINGS, "binding_kind <> 'SOURCE_OFFER'", "source_offer_unavailable"),
        _check(BINDINGS, "fulfillment_quantity >= 1", "fulfillment_quantity_positive"),
        _check(
            BINDINGS,
            "binding_kind <> 'BASE_PRODUCT' OR fulfillment_quantity = 1",
            "base_product_single_unit",
        ),
        _check(BINDINGS, _json_array("provenance_fields", minimum=1), "provenance_is_array"),
        _check(BINDINGS, "decided_by <> ''", "decided_by_present"),
        _check(BINDINGS, "correlation_id <> ''", "correlation_present"),
        _check(BINDINGS, "valid_to IS NULL OR valid_to >= valid_from", "validity_ordered"),
        _fk(BINDINGS, "item_id", ITEMS, "item_id"),
        _fk(BINDINGS, "group_member_id", MEMBERS, "member_id"),
        _fk(BINDINGS, "provenance_revision_id", REVISIONS, "revision_id"),
        sa.PrimaryKeyConstraint("binding_id", name=op.f(f"pk_{BINDINGS}")),
    )
    op.create_index(
        "ux_source_bindings_one_open_per_item",
        BINDINGS,
        ["item_id"],
        unique=True,
        sqlite_where=sa.text("valid_to IS NULL"),
    )
    _install_triggers()
    _backfill_source_identities()


def _install_triggers() -> None:
    for table in FULLY_APPEND_ONLY:
        _trigger(f"trg_{table}_no_update", "UPDATE", table, _raise(f"{table} is append-only", "1"))
    for table in TABLES:
        _trigger(f"trg_{table}_no_delete", "DELETE", table, _raise(f"{table} is append-only", "1"))
    same_identity = (
        f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r JOIN {SOURCE_PRODUCTS} s"
        " ON s.supplier_key = r.supplier_key AND s.source_product_id = r.source_product_id"
        " WHERE r.revision_id = NEW.revision_id AND s.source_product_uid = NEW.source_product_uid)"
    )
    in_order = (
        f"NEW.sequence <> (SELECT COALESCE(MAX(sequence), 0) + 1 FROM {MOVES}"
        " WHERE source_product_uid = NEW.source_product_uid)"
    )
    from_current = (
        f"NEW.previous_revision_id IS NOT (SELECT revision_id FROM {MOVES}"
        " WHERE source_product_uid = NEW.source_product_uid ORDER BY sequence DESC LIMIT 1)"
    )
    _trigger(
        f"trg_{MOVES}_chain",
        "INSERT",
        MOVES,
        _raise(f"{MOVES}: the revision belongs to another source identity", same_identity)
        + _raise(f"{MOVES}: moves are appended in order", in_order)
        + _raise(f"{MOVES}: a move starts from the current source revision", from_current),
    )
    group_not_active = (
        f"(SELECT status FROM {GROUPS} WHERE product_group_id = NEW.product_group_id)"
        " IS NOT 'ACTIVE'"
    )
    _trigger(
        f"trg_{GROUPS}_retire_only",
        "UPDATE",
        GROUPS,
        _raise(
            f"{GROUPS}: only ACTIVE to RETIRED is allowed",
            "NOT (OLD.status = 'ACTIVE' AND NEW.status = 'RETIRED'"
            " AND NEW.product_group_id = OLD.product_group_id"
            " AND NEW.decided_by = OLD.decided_by AND NEW.created_at IS OLD.created_at)",
        ),
    )
    _trigger(
        f"trg_{MEMBERS}_active_group",
        "INSERT",
        MEMBERS,
        _raise(f"{MEMBERS}: a member joins only an ACTIVE group", group_not_active),
    )
    _trigger(
        f"trg_{MEMBERS}_transition",
        "UPDATE",
        MEMBERS,
        _raise(
            f"{MEMBERS}: a member identity is immutable",
            "NEW.member_id <> OLD.member_id OR NEW.product_group_id <> OLD.product_group_id"
            " OR NEW.source_product_uid <> OLD.source_product_uid"
            " OR NEW.created_at IS NOT OLD.created_at",
        )
        + _raise(
            f"{MEMBERS}: status transition not allowed",
            "NOT ((OLD.status = 'CANDIDATE' AND NEW.status IN ('CONFIRMED', 'REJECTED'))"
            " OR (OLD.status = 'CONFIRMED' AND NEW.status = 'REJECTED'))",
        )
        + _raise(
            f"{MEMBERS}: a member is confirmed only into an ACTIVE group",
            f"NEW.status = 'CONFIRMED' AND {group_not_active}",
        ),
    )
    confirmed_count = (
        f"(SELECT COUNT(*) FROM {MEMBERS}"
        " WHERE product_group_id = NEW.product_group_id AND status = 'CONFIRMED')"
    )
    _trigger(
        f"trg_{MEMBERSHIP}_snapshot",
        "INSERT",
        MEMBERSHIP,
        _raise(
            f"{MEMBERSHIP}: revisions are appended in order",
            f"NEW.revision_no <> (SELECT COALESCE(MAX(revision_no), 0) + 1 FROM {MEMBERSHIP}"
            " WHERE product_group_id = NEW.product_group_id)",
        )
        + _raise(
            f"{MEMBERSHIP}: a snapshot names only CONFIRMED members of its group",
            "EXISTS (SELECT 1 FROM json_each(NEW.members_json) j WHERE NOT EXISTS ("
            f"SELECT 1 FROM {MEMBERS} m WHERE m.product_group_id = NEW.product_group_id"
            " AND m.source_product_uid = j.value AND m.status = 'CONFIRMED'))",
        )
        + _raise(
            f"{MEMBERSHIP}: a snapshot names each member once",
            "(SELECT COUNT(DISTINCT value) FROM json_each(NEW.members_json)) <> NEW.member_count",
        )
        + _raise(
            f"{MEMBERSHIP}: a snapshot is the complete CONFIRMED set",
            f"{confirmed_count} <> NEW.member_count",
        ),
    )
    _trigger(
        f"trg_{ITEMS}_identity",
        "INSERT",
        ITEMS,
        _raise(
            f"{ITEMS}: the signature is the composition signature",
            "NEW.composition_signature IS NOT (SELECT composition_signature FROM"
            f" {COMPOSITIONS} WHERE composition_id = NEW.composition_id)",
        )
        + _raise(f"{ITEMS}: an Item belongs to an ACTIVE group", group_not_active),
    )
    _trigger(
        f"trg_{BINDINGS}_scope",
        "INSERT",
        BINDINGS,
        _raise(
            f"{BINDINGS}: the member must be CONFIRMED in the group of the Item",
            f"NOT EXISTS (SELECT 1 FROM {MEMBERS} m JOIN {ITEMS} i"
            " ON i.product_group_id = m.product_group_id WHERE m.member_id = NEW.group_member_id"
            " AND i.item_id = NEW.item_id AND m.status = 'CONFIRMED')",
        )
        + _raise(
            f"{BINDINGS}: the provenance revision belongs to another source identity",
            f"NOT EXISTS (SELECT 1 FROM {MEMBERS} m"
            f" JOIN {SOURCE_PRODUCTS} s ON s.source_product_uid = m.source_product_uid"
            f" JOIN {REVISIONS} r ON r.supplier_key = s.supplier_key"
            " AND r.source_product_id = s.source_product_id"
            " WHERE m.member_id = NEW.group_member_id AND r.revision_id = NEW.provenance_revision_id)",
        )
        + _raise(
            f"{BINDINGS}: BASE_PRODUCT needs a revision stating no options and no quantity tiers",
            f"NEW.binding_kind = 'BASE_PRODUCT' AND (SELECT COUNT(*) FROM {FIELDS} f"
            " WHERE f.revision_id = NEW.provenance_revision_id"
            " AND f.field_key IN ('options', 'quantity_tiers') AND f.status = 'ABSENT') <> 2",
        )
        + _raise(
            f"{BINDINGS}: BASE_PRODUCT fulfils only a single-unit composition",
            f"NEW.binding_kind = 'BASE_PRODUCT' AND (SELECT c.quantity FROM {ITEMS} i"
            f" JOIN {COMPOSITIONS} c ON c.composition_id = i.composition_id"
            " WHERE i.item_id = NEW.item_id) IS NOT 1",
        ),
    )
    unchanged = " AND ".join(
        f"NEW.{column} = OLD.{column}"
        for column in (
            "binding_id",
            "item_id",
            "group_member_id",
            "binding_kind",
            "fulfillment_quantity",
            "provenance_revision_id",
            "provenance_fields",
            "decided_by",
            "correlation_id",
        )
    )
    _trigger(
        f"trg_{BINDINGS}_close_only",
        "UPDATE",
        BINDINGS,
        _raise(
            f"{BINDINGS}: a binding is only ever closed",
            f"NOT (OLD.valid_to IS NULL AND NEW.valid_to IS NOT NULL AND {unchanged}"
            " AND NEW.valid_from IS OLD.valid_from)",
        ),
    )


def _backfill_source_identities() -> None:
    """One identity row per pair that existing revisions already name. Nothing else."""
    bind = op.get_bind()
    pairs = bind.execute(
        sa.text(
            f"SELECT DISTINCT supplier_key, source_product_id FROM {REVISIONS}"
            " ORDER BY supplier_key, source_product_id"
        )
    ).all()
    created_at = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    for supplier_key, source_product_id in pairs:
        bind.execute(
            sa.text(
                f"INSERT INTO {SOURCE_PRODUCTS}"
                " (source_product_uid, supplier_key, source_product_id, created_at)"
                " VALUES (:uid, :supplier_key, :source_product_id, :created_at)"
            ),
            {
                "uid": str(uuid.uuid4()),
                "supplier_key": supplier_key,
                "source_product_id": source_product_id,
                "created_at": created_at,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        if table == SOURCE_PRODUCTS:
            continue
        held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if held:
            raise RuntimeError(
                f"cannot drop {table}: {held} row(s) of M4 product truth are held; canonical "
                "product state is never silently destroyed"
            )
    underived = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {SOURCE_PRODUCTS} s WHERE NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
            " WHERE r.supplier_key = s.supplier_key AND r.source_product_id = s.source_product_id)"
        )
    ).scalar_one()
    if underived:
        raise RuntimeError(
            f"cannot drop {SOURCE_PRODUCTS}: {underived} identity row(s) are not re-derivable from "
            "revisions; canonical product state is never silently destroyed"
        )
    for table in reversed(TABLES):
        op.drop_table(table)
