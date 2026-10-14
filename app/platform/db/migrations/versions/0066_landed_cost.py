"""ADR-0038 O2: the landed-cost pins of a source-currency price, and the per-product inputs.

Revision ID: 0066_landed_cost
Revises: 0065_image_studio_translation
Create Date: 2026-10-14

ADR-0038 §4, §8 (FX-02, FX-04). Four changes, nothing else:

- **``overseas_cost_override_revisions``** (§4, §8 phase 1). The append-only revisions of one
  overseas source product's own landed-cost inputs, keyed by ``source_product_uid``: its overseas
  shipping (``overseas_shipping_krw``) and its duty rate (``duty_rate_bp``), each ``NULL`` when the
  settings default applies. The number opens at one and moves by exactly one per source product.
  The database refuses an update, a delete and a gap in the numbering.
- **``pricing_snapshots``** and **``atomic_sku_pricing_snapshots``**: fourteen landed-cost pins,
  added in place (``ADD COLUMN``, each with its column CHECK): the source currency and price as
  stated, the FX snapshot and its publication day, the USD snapshot of the same day, the margin, the
  settings and product-input revisions, the overseas shipping and its source, the duty decision,
  rate and amount, and the declared value in USD. Every existing snapshot is a KRW price and reads
  ``NULL`` in each — exactly what it priced; no row is rewritten, so the append-only triggers
  never fire. The last column carries the cross-column CHECK (``landed_cost_recorded``): a KRW
  snapshot states none of them, a foreign one every one, consistent with its amounts. A trigger
  per table checks that a foreign snapshot names two recorded FX snapshots of its currency and of
  USD, both of its publication day, and a settings revision that exists.
- **``atomic_sku_source_bindings``**: ``source_currency`` and ``source_unit_price`` (the
  configuration's own unit price of a source-currency revision). A foreign binding states no won:
  its three KRW costs are 0 (CHECK). The binding's close-only trigger is recreated to keep the two
  new columns unchanged as well.
- **``atomic_sku_pricing_snapshots``' current-state trigger** is recreated: a KRW snapshot still
  prices its open binding's exact won cost; a foreign one prices its open binding's exact currency
  and unit price (its won cost is the landed cost of the day). Every other check is unchanged.

**Downgrade fails closed** while any product-input revision, any foreign snapshot or any foreign
binding exists: a price's pins are the evidence that explains it (FX-02), and an operator's inputs
are never silently dropped. Empty, it restores both 0047 triggers exactly and drops what it added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_landed_cost"
down_revision: str | None = "0065_image_studio_translation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OVERRIDES = "overseas_cost_override_revisions"
SNAPSHOTS = "pricing_snapshots"
ATOMIC_SNAPSHOTS = "atomic_sku_pricing_snapshots"
BINDINGS = "atomic_sku_source_bindings"

# Frozen with this revision (``app.stages.products.overseas_costs_models.OVERRIDE_CHECKS``).
OVERRIDE_CHECKS = {
    "revision_no_positive": "revision_no >= 1",
    "shipping_range": (
        "overseas_shipping_krw IS NULL OR overseas_shipping_krw BETWEEN 0 AND 10000000"
    ),
    "duty_rate_bp_range": "duty_rate_bp IS NULL OR duty_rate_bp BETWEEN 0 AND 10000",
    "fingerprint_hex": "length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'",
    "changed_by_present": "changed_by <> ''",
    "correlation_present": "correlation_id <> ''",
}


def _decimal_text(column: str) -> str:
    return (
        f"{column} <> '' AND {column} NOT GLOB '*[^0-9.]*' AND {column} NOT GLOB '*.*.*'"
        f" AND {column} NOT GLOB '.*' AND {column} NOT GLOB '*.'"
    )


# Frozen with this revision (``app.stages.products.models.LANDED_COST_CHECKS`` and
# ``LANDED_COST_RECORDED``): each pin, its SQL type, and its column CHECK, in column order.
PINS = (
    (
        "source_currency",
        "VARCHAR(3)",
        "source_currency_valid",
        "source_currency IS NULL OR source_currency IN ('CNY', 'JPY')",
    ),
    (
        "source_price",
        "VARCHAR(24)",
        "source_price_decimal",
        f"source_price IS NULL OR ({_decimal_text('source_price')})",
    ),
    (
        "fx_snapshot_id",
        "VARCHAR(36)",
        "fx_snapshot_present",
        "fx_snapshot_id IS NULL OR fx_snapshot_id <> ''",
    ),
    (
        "fx_publication_date",
        "VARCHAR(10)",
        "fx_publication_date_iso",
        "fx_publication_date IS NULL"
        " OR fx_publication_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'",
    ),
    (
        "usd_fx_snapshot_id",
        "VARCHAR(36)",
        "usd_fx_snapshot_present",
        "usd_fx_snapshot_id IS NULL OR usd_fx_snapshot_id <> ''",
    ),
    (
        "fx_margin_bp",
        "INTEGER",
        "fx_margin_bp_range",
        "fx_margin_bp IS NULL OR fx_margin_bp BETWEEN 0 AND 10000",
    ),
    (
        "overseas_settings_revision_no",
        "INTEGER",
        "overseas_settings_revision_positive",
        "overseas_settings_revision_no IS NULL OR overseas_settings_revision_no >= 1",
    ),
    (
        "overseas_cost_override_revision_no",
        "INTEGER",
        "overseas_cost_override_revision_positive",
        "overseas_cost_override_revision_no IS NULL OR overseas_cost_override_revision_no >= 1",
    ),
    (
        "overseas_shipping_krw",
        "INTEGER",
        "overseas_shipping_non_negative",
        "overseas_shipping_krw IS NULL OR overseas_shipping_krw >= 0",
    ),
    (
        "overseas_shipping_source",
        "VARCHAR(10)",
        "overseas_shipping_source_valid",
        "overseas_shipping_source IS NULL OR overseas_shipping_source IN ('PRODUCT', 'DEFAULT')",
    ),
    (
        "duty_decision",
        "VARCHAR(16)",
        "duty_decision_valid",
        "duty_decision IS NULL OR duty_decision IN ('NOT_OVER_150', 'INCLUDED', 'EXCLUDED')",
    ),
    (
        "duty_rate_bp",
        "INTEGER",
        "duty_rate_bp_range",
        "duty_rate_bp IS NULL OR duty_rate_bp BETWEEN 0 AND 10000",
    ),
    (
        "duties_krw",
        "INTEGER",
        "duties_non_negative",
        "duties_krw IS NULL OR duties_krw >= 0",
    ),
    (
        "declared_value_usd",
        "VARCHAR(24)",
        "declared_value_usd_cents",
        "declared_value_usd IS NULL OR (declared_value_usd GLOB '*.[0-9][0-9]'"
        f" AND {_decimal_text('declared_value_usd')})",
    ),
)
_NAMES = tuple(pin[0] for pin in PINS)
_OPTIONAL = ("overseas_settings_revision_no", "overseas_cost_override_revision_no")
LANDED_COST_RECORDED = (
    "("
    + " AND ".join(f"{column} IS NULL" for column in _NAMES)
    + ") OR ("
    + " AND ".join(f"{column} IS NOT NULL" for column in _NAMES if column not in _OPTIONAL)
    + " AND (source_currency <> 'JPY' OR instr(source_price, '.') = 0)"
    " AND (instr(source_price, '.') = 0"
    " OR length(source_price) - instr(source_price, '.') BETWEEN 1 AND 2)"
    " AND supplier_shipping_krw = overseas_shipping_krw"
    " AND duties_krw <= purchase_cost_krw"
    " AND (duty_decision = 'INCLUDED' OR duties_krw = 0)"
    " AND (overseas_shipping_source = 'DEFAULT' OR overseas_cost_override_revision_no IS NOT NULL))"
)
# Frozen with this revision
# (``app.stages.products.atomic_sku_economics_models.FOREIGN_UNIT_PRICE_RECORDED``).
FOREIGN_UNIT_PRICE_RECORDED = (
    "(source_currency IS NULL AND source_unit_price IS NULL)"
    " OR (source_currency IN ('CNY', 'JPY') AND source_unit_price IS NOT NULL"
    " AND source_unit_price <> '' AND source_unit_price NOT GLOB '*[^0-9.]*'"
    " AND source_unit_price NOT GLOB '*.*.*' AND source_unit_price NOT GLOB '.*'"
    " AND source_unit_price NOT GLOB '*.'"
    " AND (source_currency <> 'JPY' OR instr(source_unit_price, '.') = 0)"
    " AND (instr(source_unit_price, '.') = 0"
    " OR length(source_unit_price) - instr(source_unit_price, '.') BETWEEN 1 AND 2)"
    " AND base_purchase_cost_krw = 0 AND option_additional_price_krw = 0"
    " AND purchase_cost_krw = 0)"
)

# The binding's close-only trigger: 0047's columns, then (0066) the two foreign ones.
_CLOSE_ONLY_COLUMNS = (
    "binding_id",
    "atomic_sku_item_id",
    "atomic_sku_revision_member_id",
    "group_member_id",
    "provenance_revision_id",
    "acquisition_basis",
    "base_purchase_cost_krw",
    "option_additional_price_krw",
    "purchase_cost_krw",
    "decided_by",
    "correlation_id",
    "valid_from",
)


def _close_only(columns: Sequence[str]) -> str:
    kept = "".join(f" AND NEW.{column} IS OLD.{column}" for column in columns)
    return (
        f"CREATE TRIGGER trg_{BINDINGS}_close_only BEFORE UPDATE ON {BINDINGS} BEGIN"
        " SELECT CASE WHEN NOT ("
        f" NEW.valid_to IS NOT NULL AND OLD.valid_to IS NULL{kept})"
        " THEN RAISE(ABORT, 'AtomicSKU source binding may only be closed') END; END"
    )


# 0047's current-state trigger of an AtomicSKU snapshot, with the one condition 0066 changes as a
# parameter: which acquisition of the open binding the snapshot prices.
_KRW_ACQUISITION = " AND b.purchase_cost_krw = NEW.purchase_cost_krw)"
_LANDED_ACQUISITION = (
    " AND ((NEW.source_currency IS NULL AND b.source_currency IS NULL"
    " AND b.purchase_cost_krw = NEW.purchase_cost_krw)"
    " OR (NEW.source_currency IS NOT NULL AND b.source_currency = NEW.source_currency"
    " AND b.source_unit_price = NEW.source_price)))"
)


def _prices_current_state(acquisition: str) -> str:
    return (
        f"CREATE TRIGGER trg_{ATOMIC_SNAPSHOTS}_prices_current_state BEFORE INSERT ON"
        f" {ATOMIC_SNAPSHOTS} BEGIN"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {BINDINGS} b"
        " JOIN atomic_sku_product_items i ON i.atomic_sku_item_id = b.atomic_sku_item_id"
        " WHERE b.binding_id = NEW.source_binding_id AND b.valid_to IS NULL"
        " AND b.atomic_sku_item_id = NEW.atomic_sku_item_id"
        " AND i.product_group_id = NEW.product_group_id"
        " AND i.composition_signature = NEW.composition_signature"
        " AND i.atomic_sku_id = NEW.atomic_sku_id"
        " AND i.atomic_sku_selection_signature = NEW.atomic_sku_selection_signature"
        " AND b.atomic_sku_revision_member_id = NEW.atomic_sku_revision_member_id"
        " AND b.provenance_revision_id = NEW.source_product_facts_revision_id"
        f"{acquisition}"
        " THEN RAISE(ABORT, 'AtomicSKU snapshot does not price its exact open binding') END;"
        " SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM group_membership_revisions mr"
        " WHERE mr.membership_revision_id = NEW.membership_revision_id"
        " AND mr.product_group_id = NEW.product_group_id"
        " AND mr.revision_no = (SELECT MAX(revision_no) FROM group_membership_revisions"
        " WHERE product_group_id = NEW.product_group_id))"
        " THEN RAISE(ABORT, 'AtomicSKU snapshot membership is not current') END; END"
    )


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _fx_pinned(table: str) -> None:
    """A foreign snapshot names recorded rates of its own currency and of USD, both of the day it
    records, and a settings revision that exists."""
    _trigger(
        table,
        "landed_cost_pinned",
        "INSERT",
        _raise(
            f"{table}: a foreign price pins recorded rates of one day",
            "NEW.source_currency IS NOT NULL AND (NOT EXISTS (SELECT 1 FROM fx_snapshots s"
            " WHERE s.snapshot_id = NEW.fx_snapshot_id AND s.currency = NEW.source_currency"
            " AND s.publication_date = NEW.fx_publication_date)"
            " OR NOT EXISTS (SELECT 1 FROM fx_snapshots u"
            " WHERE u.snapshot_id = NEW.usd_fx_snapshot_id AND u.currency = 'USD'"
            " AND u.publication_date = NEW.fx_publication_date)"
            " OR (NEW.overseas_settings_revision_no IS NOT NULL AND NOT EXISTS (SELECT 1"
            " FROM overseas_settings_revisions r"
            " WHERE r.revision_no = NEW.overseas_settings_revision_no)))",
        ),
    )


def upgrade() -> None:
    op.create_table(
        OVERRIDES,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("source_product_uid", sa.String(length=36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("overseas_shipping_krw", sa.Integer(), nullable=True),
        sa.Column("duty_rate_bp", sa.Integer(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        *(_check(OVERRIDES, text, name) for name, text in OVERRIDE_CHECKS.items()),
        sa.ForeignKeyConstraint(
            ["source_product_uid"],
            ["source_products.source_product_uid"],
            name=op.f(f"fk_{OVERRIDES}_source_product_uid_source_products"),
        ),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{OVERRIDES}")),
        sa.UniqueConstraint(
            "source_product_uid",
            "revision_no",
            name=op.f(f"uq_{OVERRIDES}_source_product_uid_revision_no"),
        ),
    )
    _trigger(
        OVERRIDES,
        "revision_follows",
        "INSERT",
        _raise(
            f"{OVERRIDES}: a revision follows the one before it",
            f"NEW.revision_no <> 1 + (SELECT COALESCE(MAX(r.revision_no), 0) FROM {OVERRIDES} r"
            " WHERE r.source_product_uid = NEW.source_product_uid)",
        ),
    )
    _trigger(OVERRIDES, "no_update", "UPDATE", _raise(f"a {OVERRIDES} row is never updated", "1"))
    _trigger(OVERRIDES, "no_delete", "DELETE", _raise(f"a {OVERRIDES} row is never deleted", "1"))

    # Column CHECKs: SQLite adds each column in place and rewrites no row, so the snapshots'
    # append-only triggers never fire. Every existing snapshot is a KRW price: all NULL.
    for table in (SNAPSHOTS, ATOMIC_SNAPSHOTS):
        for column, sql_type, name, check in PINS:
            recorded = (
                f" CONSTRAINT ck_{table}_landed_cost_recorded CHECK ({LANDED_COST_RECORDED})"
                if column == _NAMES[-1]
                else ""
            )
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
                f" CONSTRAINT ck_{table}_{name} CHECK ({check}){recorded}"
            )
        _fx_pinned(table)

    op.execute(
        f"ALTER TABLE {BINDINGS} ADD COLUMN source_currency VARCHAR(3)"
        f" CONSTRAINT ck_{BINDINGS}_source_currency_valid"
        " CHECK (source_currency IS NULL OR source_currency IN ('CNY', 'JPY'))"
    )
    op.execute(
        f"ALTER TABLE {BINDINGS} ADD COLUMN source_unit_price VARCHAR(24)"
        f" CONSTRAINT ck_{BINDINGS}_foreign_unit_price_recorded"
        f" CHECK ({FOREIGN_UNIT_PRICE_RECORDED})"
    )
    op.execute(f"DROP TRIGGER trg_{BINDINGS}_close_only")
    op.execute(_close_only((*_CLOSE_ONLY_COLUMNS, "source_currency", "source_unit_price")))
    op.execute(f"DROP TRIGGER trg_{ATOMIC_SNAPSHOTS}_prices_current_state")
    op.execute(_prices_current_state(_LANDED_ACQUISITION))


def downgrade() -> None:
    bind = op.get_bind()
    revisions = bind.execute(sa.text(f"SELECT COUNT(*) FROM {OVERRIDES}")).scalar_one()
    foreign = sum(
        bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE source_currency IS NOT NULL")
        ).scalar_one()
        for table in (SNAPSHOTS, ATOMIC_SNAPSHOTS, BINDINGS)
    )
    if revisions or foreign:
        raise RuntimeError(
            f"cannot remove the landed-cost pins: {revisions} product input revision(s) in"
            f" {OVERRIDES} and {foreign} foreign pricing snapshot(s) or binding(s) are held; a"
            " price's pins and an operator's inputs are never silently destroyed"
        )
    # The triggers name the new columns, so they are restored (or dropped) first.
    op.execute(f"DROP TRIGGER trg_{ATOMIC_SNAPSHOTS}_prices_current_state")
    op.execute(_prices_current_state(_KRW_ACQUISITION))
    op.execute(f"DROP TRIGGER trg_{BINDINGS}_close_only")
    op.execute(_close_only(_CLOSE_ONLY_COLUMNS))
    # The unit price's CHECK names the currency, so it goes first.
    op.execute(f"ALTER TABLE {BINDINGS} DROP COLUMN source_unit_price")
    op.execute(f"ALTER TABLE {BINDINGS} DROP COLUMN source_currency")
    for table in (ATOMIC_SNAPSHOTS, SNAPSHOTS):
        op.execute(f"DROP TRIGGER trg_{table}_landed_cost_pinned")
        # The last column's CHECK names every other pin, so they go in reverse order.
        for column in reversed(_NAMES):
            op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    op.drop_table(OVERRIDES)
