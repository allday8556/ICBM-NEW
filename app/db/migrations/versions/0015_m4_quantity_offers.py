"""M4 PR-Q: product-level quantity offers and exact SOURCE_OFFER bindings.

Revision ID: 0015_m4_quantity_offers
Revises: 0014_m4_derived_image_lineage
Create Date: 2026-09-19

Issue #80 PR-Q (kickoff 5738854211), under ADR-0013 §2 and §5–§7, the quantity-priced rule 5737762202
and the product-level quantity-offer ruling 5738760913. **Nothing is backfilled**: an offer exists
only when the materializer reads it from a current source revision.

**``quantity_offers``** holds immutable, revision-scoped product-level offers. Its trigger refuses
an offer unless:
- its revision belongs to the offer's source product;
- the offer has the revision's currency;
- the revision states ``options`` ABSENT;
- the offer is exactly a CONFIRMED tier of that revision: the same position, quantity and total.

Quantities and positions are unique per revision. There is no SourceSKU column: a product-level offer
has none, and none is fabricated. Every row rejects UPDATE and DELETE.

**``source_bindings`` gains ``quantity_offer_id``**, and ``SOURCE_OFFER`` becomes storable:
- A CHECK makes ``SOURCE_OFFER`` name an offer and ``BASE_PRODUCT`` name none.
- The scope trigger keeps every 0012 rule and adds the SOURCE_OFFER ones. The offer belongs to the
  member's source product. The provenance is the offer's revision **and** the member's current source
  revision. The fulfillment quantity is the offer's quantity. The Item is exactly that quantity with
  every unit and pack field unknown. The provenance never names the generic ``prices`` field.
- A partial unique index keeps at most one open binding per offer, beside the one per Item.
- A binding is still only ever closed. ``quantity_offer_id`` is as immutable as the other columns.

**``trg_pricing_snapshots_prices_current_state``** is replaced. A snapshot still prices the Item's
open binding exactly as 0013 required. A ``SOURCE_OFFER`` snapshot must also price its offer's
total and apply no minimum sale price: the generic minimum names no offer (ruling 5738760913 §7).
The ``BASE_PRODUCT`` conditions are unchanged.

**How the rebuild keeps every row.** SQLite cannot change a CHECK in place, so ``source_bindings``
is rebuilt inside one transaction:
1. The rows are set aside.
2. The table is dropped and its successor renamed into place.
3. The rows are copied back verbatim: every ``binding_id``, window and provenance.
4. Its index and triggers are installed.

``pricing_snapshots`` references this table, and the connection enforces foreign keys. The rebuild
therefore defers the check to its commit, and ``PRAGMA foreign_key_check`` must be clean before it
returns. The rename uses ``legacy_alter_table``, so no other table's trigger or foreign key is
rewritten. They name ``source_bindings`` and keep naming it.

**Downgrade fails closed.** It refuses while any offer or any ``SOURCE_OFFER`` binding exists:
quantity-offer history is never silently destroyed. Otherwise it rebuilds ``source_bindings`` to the
0012 shape, restores the 0013 pricing trigger and drops the empty ``quantity_offers``.
"""

import sqlite3
from collections.abc import Callable, Iterable, Sequence
from typing import cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.schema import SchemaItem

revision: str = "0015_m4_quantity_offers"
down_revision: str | None = "0014_m4_derived_image_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OFFERS = "quantity_offers"
BINDINGS = "source_bindings"
REBUILT = "source_bindings_rebuilt"
HELD = "source_bindings_held"
SNAPSHOTS = "pricing_snapshots"
PRICING_TRIGGER = "trg_pricing_snapshots_prices_current_state"
SOURCE_PRODUCTS = "source_products"
SOURCE_MOVES = "current_source_revision_moves"
GROUPS = "product_groups"
MEMBERS = "group_members"
MEMBERSHIP = "group_membership_revisions"
COMPOSITIONS = "listing_compositions"
ITEMS = "product_items"
REVISIONS = "product_facts_revisions"
FIELDS = "product_facts_fields"

# Frozen with this revision.
_BINDING_KINDS = ("SOURCE_OFFER", "BASE_PRODUCT")
_CURRENCY = "KRW"
# 0012's frozen default single-unit signature (app.products.model.DEFAULT_SINGLE_UNIT_SIGNATURE).
_DEFAULT_SINGLE_UNIT_SIGNATURE = "ebd7462b890ae17974bb545171c4ca76d6873dca40a747f819cc400c65782299"
# The 0012 columns, in their order; 0015 appends ``quantity_offer_id``.
_V1_COLUMNS = (
    "binding_id",
    "item_id",
    "group_member_id",
    "binding_kind",
    "fulfillment_quantity",
    "provenance_revision_id",
    "provenance_fields",
    "decided_by",
    "correlation_id",
    "valid_from",
    "valid_to",
)
# The columns 0012's close-only trigger keeps unchanged (``valid_from`` is compared with IS).
_V1_UNCHANGED = _V1_COLUMNS[:9]


def _in(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _json_array(column: str, *, minimum: int) -> str:
    return (
        f"json_valid({column}) AND json_type({column}) = 'array'"
        f" AND json_array_length({column}) >= {minimum}"
    )


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


def _in_one_transaction() -> None:
    """Open a transaction if the driver has not: SQLite DDL opens none, so a rebuild would
    otherwise commit statement by statement. Inside one, the rebuild is atomic and the deferred
    foreign-key check runs at its commit."""
    raw = cast(sqlite3.Connection, op.get_bind().connection.dbapi_connection)
    if not raw.in_transaction:
        op.get_bind().exec_driver_sql("BEGIN")


# ---------------------------------------------------------------- quantity offers


def _create_offers() -> None:
    op.create_table(
        OFFERS,
        sa.Column("quantity_offer_id", sa.String(length=36), nullable=False),
        sa.Column("source_product_uid", sa.String(length=36), nullable=False),
        sa.Column("source_revision_id", sa.String(length=36), nullable=False),
        sa.Column("tier_ordinal", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("total_price_krw", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(OFFERS, "tier_ordinal >= 0", "tier_ordinal_non_negative"),
        _check(OFFERS, "quantity >= 1", "quantity_positive"),
        _check(OFFERS, "total_price_krw >= 0", "total_price_non_negative"),
        _check(OFFERS, f"currency = '{_CURRENCY}'", "currency_krw"),
        _fk(OFFERS, "source_product_uid", SOURCE_PRODUCTS, "source_product_uid"),
        _fk(OFFERS, "source_revision_id", REVISIONS, "revision_id"),
        sa.PrimaryKeyConstraint("quantity_offer_id", name=op.f(f"pk_{OFFERS}")),
        sa.UniqueConstraint(
            "source_revision_id",
            "tier_ordinal",
            name=op.f(f"uq_{OFFERS}_source_revision_id_tier_ordinal"),
        ),
        sa.UniqueConstraint(
            "source_revision_id",
            "quantity",
            name=op.f(f"uq_{OFFERS}_source_revision_id_quantity"),
        ),
    )
    _trigger(f"trg_{OFFERS}_no_update", "UPDATE", OFFERS, _raise(f"{OFFERS} is append-only", "1"))
    _trigger(f"trg_{OFFERS}_no_delete", "DELETE", OFFERS, _raise(f"{OFFERS} is append-only", "1"))
    _trigger(
        f"trg_{OFFERS}_exact_tier",
        "INSERT",
        OFFERS,
        _raise(
            f"{OFFERS}: the revision belongs to the source product of the offer",
            f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r JOIN {SOURCE_PRODUCTS} s"
            " ON s.supplier_key = r.supplier_key AND s.source_product_id = r.source_product_id"
            " WHERE r.revision_id = NEW.source_revision_id"
            " AND s.source_product_uid = NEW.source_product_uid)",
        )
        + _raise(
            f"{OFFERS}: the offer is in the currency of its revision",
            f"(SELECT currency FROM {REVISIONS} WHERE revision_id = NEW.source_revision_id)"
            " IS NOT NEW.currency",
        )
        + _raise(
            f"{OFFERS}: a product-level offer needs a revision stating no options",
            f"NOT EXISTS (SELECT 1 FROM {FIELDS} f WHERE f.revision_id = NEW.source_revision_id"
            " AND f.field_key = 'options' AND f.status = 'ABSENT')",
        )
        + _raise(
            f"{OFFERS}: the offer is exactly a CONFIRMED tier of its revision",
            f"NOT EXISTS (SELECT 1 FROM {FIELDS} f, json_each(f.value_json, '$.tiers') t"
            " WHERE f.revision_id = NEW.source_revision_id AND f.field_key = 'quantity_tiers'"
            " AND f.status = 'CONFIRMED' AND t.key = NEW.tier_ordinal"
            " AND json_extract(t.value, '$.quantity') = NEW.quantity"
            " AND json_extract(t.value, '$.total_price_krw') = NEW.total_price_krw)",
        ),
    )


# ---------------------------------------------------------------- source bindings


def _binding_columns(*, offers: bool) -> list[SchemaItem]:
    """``source_bindings`` as 0012 created it; with ``offers``, as 0015 evolves it."""
    items: list[SchemaItem] = [
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
    ]
    if offers:
        items.append(sa.Column("quantity_offer_id", sa.String(length=36), nullable=True))
    items += [
        _check(BINDINGS, _in("binding_kind", _BINDING_KINDS), "binding_kind_valid"),
        _check(
            BINDINGS,
            "(binding_kind = 'SOURCE_OFFER') = (quantity_offer_id IS NOT NULL)",
            "source_offer_names_its_offer",
        )
        if offers
        else _check(BINDINGS, "binding_kind <> 'SOURCE_OFFER'", "source_offer_unavailable"),
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
    ]
    if offers:
        items.append(_fk(BINDINGS, "quantity_offer_id", OFFERS, "quantity_offer_id"))
    items.append(sa.PrimaryKeyConstraint("binding_id", name=op.f(f"pk_{BINDINGS}")))
    return items


def _base_scope() -> str:
    """0012's scope rules, unchanged."""
    return (
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
            f"{BINDINGS}: BASE_PRODUCT fulfils only the default single-unit composition",
            f"NEW.binding_kind = 'BASE_PRODUCT' AND NOT EXISTS (SELECT 1 FROM {ITEMS} i"
            f" JOIN {COMPOSITIONS} c ON c.composition_id = i.composition_id"
            " WHERE i.item_id = NEW.item_id AND c.quantity = 1 AND c.unit_amount IS NULL"
            " AND c.unit_code IS NULL AND c.pack_count IS NULL AND c.units_per_pack IS NULL"
            " AND c.total_amount IS NULL"
            f" AND c.composition_signature = '{_DEFAULT_SINGLE_UNIT_SIGNATURE}'"
            f" AND i.composition_signature = '{_DEFAULT_SINGLE_UNIT_SIGNATURE}')",
        )
    )


def _offer_scope() -> str:
    """The SOURCE_OFFER rules 0015 adds (ruling 5738760913 §4, kickoff 5738854211 §E)."""
    offer = (
        f"(SELECT o.{{column}} FROM {OFFERS} o WHERE o.quantity_offer_id = NEW.quantity_offer_id)"
    )
    member_uid = f"(SELECT source_product_uid FROM {MEMBERS} WHERE member_id = NEW.group_member_id)"
    source_offer = "NEW.binding_kind = 'SOURCE_OFFER'"
    return (
        _raise(
            f"{BINDINGS}: SOURCE_OFFER names its exact offer",
            f"{source_offer} AND NEW.quantity_offer_id IS NULL",
        )
        + _raise(
            f"{BINDINGS}: SOURCE_OFFER binds an offer of the source product of its member",
            f"{source_offer} AND {offer.format(column='source_product_uid')} IS NOT {member_uid}",
        )
        + _raise(
            f"{BINDINGS}: SOURCE_OFFER provenance is the revision of its offer",
            f"{source_offer} AND {offer.format(column='source_revision_id')}"
            " IS NOT NEW.provenance_revision_id",
        )
        + _raise(
            f"{BINDINGS}: SOURCE_OFFER provenance is the current source revision of its member",
            f"{source_offer} AND NEW.provenance_revision_id IS NOT (SELECT revision_id FROM"
            f" {SOURCE_MOVES} WHERE source_product_uid = {member_uid}"
            " ORDER BY sequence DESC LIMIT 1)",
        )
        + _raise(
            f"{BINDINGS}: SOURCE_OFFER fulfils exactly the quantity of its offer",
            f"{source_offer} AND {offer.format(column='quantity')} IS NOT NEW.fulfillment_quantity",
        )
        + _raise(
            f"{BINDINGS}: SOURCE_OFFER fulfils an Item of exactly that quantity and nothing more",
            f"{source_offer} AND NOT EXISTS (SELECT 1 FROM {ITEMS} i"
            f" JOIN {COMPOSITIONS} c ON c.composition_id = i.composition_id"
            " WHERE i.item_id = NEW.item_id AND c.quantity = NEW.fulfillment_quantity"
            " AND c.unit_amount IS NULL AND c.unit_code IS NULL AND c.pack_count IS NULL"
            " AND c.units_per_pack IS NULL AND c.total_amount IS NULL)",
        )
        + _raise(
            f"{BINDINGS}: SOURCE_OFFER never relies on the generic prices",
            f"{source_offer} AND EXISTS (SELECT 1 FROM json_each(NEW.provenance_fields)"
            " WHERE value = 'prices')",
        )
    )


def _install_binding_rules(*, offers: bool) -> None:
    op.create_index(
        "ux_source_bindings_one_open_per_item",
        BINDINGS,
        ["item_id"],
        unique=True,
        sqlite_where=sa.text("valid_to IS NULL"),
    )
    if offers:
        op.create_index(
            "ux_source_bindings_one_open_per_offer",
            BINDINGS,
            ["quantity_offer_id"],
            unique=True,
            sqlite_where=sa.text("valid_to IS NULL"),
        )
    _trigger(
        f"trg_{BINDINGS}_no_delete", "DELETE", BINDINGS, _raise(f"{BINDINGS} is append-only", "1")
    )
    _trigger(
        f"trg_{BINDINGS}_scope",
        "INSERT",
        BINDINGS,
        _base_scope() + _offer_scope() if offers else _base_scope(),
    )
    unchanged = " AND ".join(f"NEW.{column} = OLD.{column}" for column in _V1_UNCHANGED)
    if offers:
        unchanged += " AND NEW.quantity_offer_id IS OLD.quantity_offer_id"
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


def _rebuild_bindings(*, offers: bool) -> None:
    """Rebuild ``source_bindings`` in the requested shape, keeping every row verbatim."""
    bind = op.get_bind()
    columns = ", ".join(_V1_COLUMNS)
    before = bind.execute(sa.text(f"SELECT COUNT(*) FROM {BINDINGS}")).scalar_one()
    # pricing_snapshots references this table: its check waits for the commit, when every
    # binding_id it names exists again.
    bind.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    op.execute(f"CREATE TEMP TABLE {HELD} AS SELECT {columns} FROM {BINDINGS}")
    op.create_table(REBUILT, *_binding_columns(offers=offers))
    op.drop_table(BINDINGS)
    # Legacy renaming rewrites no other table's trigger or foreign key: they name source_bindings
    # and keep naming it, now this table.
    bind.exec_driver_sql("PRAGMA legacy_alter_table = ON")
    op.execute(f"ALTER TABLE {REBUILT} RENAME TO {BINDINGS}")
    bind.exec_driver_sql("PRAGMA legacy_alter_table = OFF")
    op.execute(f"INSERT INTO {BINDINGS} ({columns}) SELECT {columns} FROM temp.{HELD}")
    op.execute(f"DROP TABLE temp.{HELD}")
    after = bind.execute(sa.text(f"SELECT COUNT(*) FROM {BINDINGS}")).scalar_one()
    if after != before:  # pragma: no cover - the copy is one statement
        raise RuntimeError(f"rebuilding {BINDINGS} kept {after} of {before} rows")
    _install_binding_rules(offers=offers)
    problems = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"foreign key check failed after rebuilding {BINDINGS}: {problems}")


# ---------------------------------------------------------------- pricing snapshots


def _pricing_trigger(*, offers: bool) -> None:
    """0013's snapshot trigger; with ``offers``, any open binding kind and the SOURCE_OFFER rules."""
    binding = f"(SELECT b.{{column}} FROM {BINDINGS} b WHERE b.binding_id = NEW.source_binding_id)"
    member_uid = (
        f"(SELECT m.source_product_uid FROM {BINDINGS} b JOIN {MEMBERS} m"
        " ON m.member_id = b.group_member_id WHERE b.binding_id = NEW.source_binding_id)"
    )
    open_binding = (
        _raise(
            f"{SNAPSHOTS}: the binding is the open binding of the Item",
            f"NOT EXISTS (SELECT 1 FROM {BINDINGS} b WHERE b.binding_id = NEW.source_binding_id"
            " AND b.item_id = NEW.item_id AND b.valid_to IS NULL)",
        )
        if offers
        else _raise(
            f"{SNAPSHOTS}: the binding is the open BASE_PRODUCT binding of the Item",
            f"NOT EXISTS (SELECT 1 FROM {BINDINGS} b WHERE b.binding_id = NEW.source_binding_id"
            " AND b.item_id = NEW.item_id AND b.binding_kind = 'BASE_PRODUCT'"
            " AND b.valid_to IS NULL)",
        )
    )
    body = (
        _raise(
            f"{SNAPSHOTS}: the frozen Item key is that of the Item",
            f"NOT EXISTS (SELECT 1 FROM {ITEMS} i WHERE i.item_id = NEW.item_id"
            " AND i.product_group_id = NEW.product_group_id"
            " AND i.composition_signature = NEW.composition_signature)",
        )
        + _raise(
            f"{SNAPSHOTS}: the group is ACTIVE",
            f"(SELECT status FROM {GROUPS} WHERE product_group_id = NEW.product_group_id)"
            " IS NOT 'ACTIVE'",
        )
        + open_binding
        + _raise(
            f"{SNAPSHOTS}: the binding member is CONFIRMED in the group of the Item",
            f"NOT EXISTS (SELECT 1 FROM {BINDINGS} b JOIN {MEMBERS} m"
            " ON m.member_id = b.group_member_id WHERE b.binding_id = NEW.source_binding_id"
            " AND m.product_group_id = NEW.product_group_id AND m.status = 'CONFIRMED')",
        )
        + _raise(
            f"{SNAPSHOTS}: the source revision is the provenance of the binding",
            f"NEW.source_product_facts_revision_id IS NOT"
            f" {binding.format(column='provenance_revision_id')}",
        )
        + _raise(
            f"{SNAPSHOTS}: the source revision is the current source revision of the member",
            f"NEW.source_product_facts_revision_id IS NOT (SELECT revision_id FROM {SOURCE_MOVES}"
            f" WHERE source_product_uid = {member_uid} ORDER BY sequence DESC LIMIT 1)",
        )
        + _raise(
            f"{SNAPSHOTS}: the membership revision is the current one of the group",
            f"NEW.membership_revision_id IS NOT (SELECT membership_revision_id FROM {MEMBERSHIP}"
            " WHERE product_group_id = NEW.product_group_id ORDER BY revision_no DESC LIMIT 1)",
        )
    )
    if offers:
        body += _raise(
            f"{SNAPSHOTS}: a SOURCE_OFFER snapshot prices the total of its exact offer",
            f"EXISTS (SELECT 1 FROM {BINDINGS} b JOIN {OFFERS} o"
            " ON o.quantity_offer_id = b.quantity_offer_id"
            " WHERE b.binding_id = NEW.source_binding_id"
            " AND o.total_price_krw IS NOT NEW.purchase_cost_krw)",
        ) + _raise(
            f"{SNAPSHOTS}: a SOURCE_OFFER snapshot applies no minimum that names no offer",
            "NEW.minimum_sale_price_krw IS NOT NULL"
            f" AND {binding.format(column='binding_kind')} = 'SOURCE_OFFER'",
        )
    _trigger(PRICING_TRIGGER, "INSERT", SNAPSHOTS, body)


def _replace_pricing_trigger(*, offers: bool) -> None:
    op.execute(f"DROP TRIGGER {PRICING_TRIGGER}")
    _pricing_trigger(offers=offers)


# ---------------------------------------------------------------- upgrade and downgrade


def _step(*actions: Callable[[], None]) -> None:
    _in_one_transaction()
    for action in actions:
        action()


def upgrade() -> None:
    _step(
        _create_offers,
        lambda: _rebuild_bindings(offers=True),
        lambda: _replace_pricing_trigger(offers=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    offers = bind.execute(sa.text(f"SELECT COUNT(*) FROM {OFFERS}")).scalar_one()
    bound = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {BINDINGS}"
            " WHERE binding_kind = 'SOURCE_OFFER' OR quantity_offer_id IS NOT NULL"
        )
    ).scalar_one()
    if offers or bound:
        raise RuntimeError(
            f"cannot drop {OFFERS}: {offers} offer(s) and {bound} SOURCE_OFFER binding(s) of"
            " quantity-offer history are held; source offers and their bindings are never silently"
            " destroyed"
        )
    _step(
        lambda: _rebuild_bindings(offers=False),
        lambda: _replace_pricing_trigger(offers=False),
        lambda: op.drop_table(OFFERS),
    )
