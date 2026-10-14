"""ADR-0038 O1b: the FX snapshots and the 해외 구매대행 settings.

Revision ID: 0064_fx_snapshots
Revises: 0063_source_currency
Create Date: 2026-10-14

ADR-0038 §3, §5. Two new tables, nothing else; no existing table is touched. Both belong to the one
O1b slice (the FX snapshot owner and the settings card the landed cost of O2 will read beside it),
so they share one revision rather than claiming two numbers.

- **``fx_snapshots``** (§3). One immutable row per publication day and currency of Korea
  Eximbank's 현재환율 (``koreaexim/AP01``), rate type TTS, for ``CNY`` (published ``CNH``), ``JPY``
  (published ``JPY(100)``, per 100 yen: ``unit = 100``) and ``USD``. The TTS is exact decimal text
  per its publication unit; the published code, ``deal_bas_r`` and the whole row are kept as
  fetched. A day holds at most one row per currency.
- **``overseas_settings_revisions``** (§5). The append-only revisions of the 해외 구매대행 settings:
  the FX margin and the duty rate as integer basis points, the default overseas shipping, the
  duties-included switch, the SmartStore overseas 출고지 address id, the delivery days and the
  importer label. The number opens at one and moves by exactly one. With no revision the defaults
  apply: nothing is seeded.

The database refuses an update or a delete of either, and a gap in the settings numbering.

**Downgrade fails closed** while any snapshot or settings revision exists: a recorded rate is the
evidence a price will pin (FX-02), and an operator's settings history is never silently dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_fx_snapshots"
down_revision: str | None = "0063_source_currency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SNAPSHOTS = "fx_snapshots"
SETTINGS = "overseas_settings_revisions"

# Frozen with this revision (``app.capabilities.overseas.fx_models`` and ``settings_models``).
CURRENCY_CODES = (
    "(currency = 'CNY' AND source_code = 'CNH' AND unit = 1)"
    " OR (currency = 'JPY' AND source_code = 'JPY(100)' AND unit = 100)"
    " OR (currency = 'USD' AND source_code = 'USD' AND unit = 1)"
)
ISO_DAY = "publication_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'"
EXACT_DECIMAL = (
    "tts <> '' AND tts NOT GLOB '*[^0-9.]*' AND tts NOT GLOB '*.*.*'"
    " AND tts NOT GLOB '.*' AND tts NOT GLOB '*.' AND CAST(tts AS REAL) > 0"
)
SETTINGS_CHECKS = {
    "revision_no_positive": "revision_no >= 1",
    "fx_margin_bp_range": "fx_margin_bp BETWEEN 0 AND 10000",
    "duty_rate_bp_range": "duty_rate_bp BETWEEN 0 AND 10000",
    "shipping_default_range": (
        "overseas_shipping_default_krw IS NULL"
        " OR overseas_shipping_default_krw BETWEEN 0 AND 10000000"
    ),
    "duties_flag": "duties_included_over_usd150 IN (0, 1)",
    "delivery_days_range": (
        "overseas_delivery_days_min >= 1"
        " AND overseas_delivery_days_min <= overseas_delivery_days_max"
        " AND overseas_delivery_days_max <= 60"
    ),
    "address_id_digits": (
        "smartstore_overseas_shipping_address_id IS NULL"
        " OR (length(smartstore_overseas_shipping_address_id) BETWEEN 1 AND 20"
        " AND smartstore_overseas_shipping_address_id NOT GLOB '*[^0-9]*')"
    ),
    "importer_label_length": "importer_label IS NULL OR length(importer_label) BETWEEN 1 AND 50",
    "fingerprint_hex": "length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'",
    "changed_by_present": "changed_by <> ''",
    "correlation_present": "correlation_id <> ''",
}


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def upgrade() -> None:
    op.create_table(
        SNAPSHOTS,
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("publication_date", sa.String(length=10), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("rate_type", sa.String(length=8), nullable=False),
        sa.Column("source_code", sa.String(length=16), nullable=False),
        sa.Column("unit", sa.Integer(), nullable=False),
        sa.Column("tts", sa.String(length=32), nullable=False),
        sa.Column("deal_bas_r", sa.String(length=32), nullable=False),
        sa.Column("published_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        _check(SNAPSHOTS, ISO_DAY, "publication_date_iso"),
        _check(SNAPSHOTS, CURRENCY_CODES, "currency_code_unit"),
        _check(SNAPSHOTS, "rate_type = 'TTS'", "rate_type_tts"),
        _check(SNAPSHOTS, EXACT_DECIMAL, "tts_exact_decimal"),
        _check(SNAPSHOTS, "deal_bas_r <> ''", "deal_bas_r_present"),
        _check(SNAPSHOTS, "json_valid(published_json)", "published_json_valid"),
        _check(SNAPSHOTS, "source = 'koreaexim/AP01'", "source_known"),
        _check(SNAPSHOTS, "correlation_id <> ''", "correlation_present"),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f(f"pk_{SNAPSHOTS}")),
        sa.UniqueConstraint(
            "publication_date",
            "currency",
            name=op.f(f"uq_{SNAPSHOTS}_publication_date_currency"),
        ),
    )
    _trigger(SNAPSHOTS, "no_update", "UPDATE", _raise(f"a {SNAPSHOTS} row is never updated", "1"))
    _trigger(SNAPSHOTS, "no_delete", "DELETE", _raise(f"a {SNAPSHOTS} row is never deleted", "1"))

    op.create_table(
        SETTINGS,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("fx_margin_bp", sa.Integer(), nullable=False),
        sa.Column("overseas_shipping_default_krw", sa.Integer(), nullable=True),
        sa.Column("duties_included_over_usd150", sa.Boolean(), nullable=False),
        sa.Column("duty_rate_bp", sa.Integer(), nullable=False),
        sa.Column("smartstore_overseas_shipping_address_id", sa.String(length=20), nullable=True),
        sa.Column("overseas_delivery_days_min", sa.Integer(), nullable=False),
        sa.Column("overseas_delivery_days_max", sa.Integer(), nullable=False),
        sa.Column("importer_label", sa.String(length=50), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        *(_check(SETTINGS, text, name) for name, text in SETTINGS_CHECKS.items()),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{SETTINGS}")),
        sa.UniqueConstraint("revision_no", name=op.f(f"uq_{SETTINGS}_revision_no")),
    )
    _trigger(
        SETTINGS,
        "revision_follows",
        "INSERT",
        _raise(
            f"{SETTINGS}: a revision follows the one before it",
            f"NEW.revision_no <> 1 + (SELECT COALESCE(MAX(r.revision_no), 0) FROM {SETTINGS} r)",
        ),
    )
    _trigger(SETTINGS, "no_update", "UPDATE", _raise(f"a {SETTINGS} row is never updated", "1"))
    _trigger(SETTINGS, "no_delete", "DELETE", _raise(f"a {SETTINGS} row is never deleted", "1"))


def downgrade() -> None:
    bind = op.get_bind()
    snapshots = bind.execute(sa.text(f"SELECT COUNT(*) FROM {SNAPSHOTS}")).scalar_one()
    revisions = bind.execute(sa.text(f"SELECT COUNT(*) FROM {SETTINGS}")).scalar_one()
    if snapshots or revisions:
        raise RuntimeError(
            f"cannot remove the FX snapshots and the overseas settings: {snapshots} snapshot(s) in"
            f" {SNAPSHOTS} and {revisions} settings revision(s) in {SETTINGS} are held; a"
            " recorded rate and an operator's settings history are never silently destroyed"
        )
    op.drop_table(SETTINGS)
    op.drop_table(SNAPSHOTS)
