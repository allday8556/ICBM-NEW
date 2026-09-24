"""Gate 2 G2-C: the owner truth token a coverage watermark was published on.

Revision ID: 0023_g2_review_coverage_fence
Revises: 0022_g2_review_coverage
Create Date: 2026-09-24

Issue #89 Gate 2, slice G2-C, under ADR-0016 §4 and §7 and the G2-B merge comment `5808443224`.
It adds exactly one nullable column to ``review_coverage`` and two triggers, and touches no other
table, row, trigger or index.

**What it holds.** ``truth_digest``: the producer's owner truth token that the last complete full
pass was fenced on (review ``5807902325`` B3). Coverage compares it with the owner's truth token
**now**, on every read: once the owner has moved since the watermark, coverage is not current,
whatever the watermark's age. So whole-owner churn can never keep an old watermark authoritative.

**What the database enforces.**
- A watermark is published only together with its token: an insert or update that sets or renews
  ``watermark_at`` must carry a 64-character lowercase hexadecimal ``truth_digest``.
- A token never appears without a watermark.

A row written before this migration has no token, so its coverage is not current until the next
complete pass publishes one.

**Downgrade fails closed.** It refuses while any row holds a token.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_g2_review_coverage_fence"
down_revision: str | None = "0022_g2_review_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COVERAGE = "review_coverage"
_HEX64 = "length(NEW.truth_digest) = 64 AND NEW.truth_digest NOT GLOB '*[^0-9a-f]*'"


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def upgrade() -> None:
    op.add_column(COVERAGE, sa.Column("truth_digest", sa.String(length=64), nullable=True))
    token_rule = _raise(
        f"{COVERAGE}: a watermark is published with the owner truth digest it was fenced on",
        "(NEW.truth_digest IS NULL) <> (NEW.watermark_at IS NULL)"
        f" OR (NEW.truth_digest IS NOT NULL AND NOT ({_HEX64}))",
    )
    op.execute(
        f"CREATE TRIGGER trg_{COVERAGE}_fence_insert BEFORE INSERT ON {COVERAGE}"
        f" BEGIN {token_rule} END"
    )
    renewed = _raise(
        f"{COVERAGE}: a renewed watermark carries a fresh owner truth digest",
        "NEW.watermark_at IS NOT OLD.watermark_at AND NEW.truth_digest IS NULL",
    )
    op.execute(
        f"CREATE TRIGGER trg_{COVERAGE}_fence_update BEFORE UPDATE ON {COVERAGE}"
        f" WHEN NEW.watermark_at IS NOT OLD.watermark_at OR NEW.truth_digest IS NOT OLD.truth_digest"
        f" BEGIN {renewed} {token_rule} END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {COVERAGE} WHERE truth_digest IS NOT NULL")
    ).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot drop the review coverage owner digest: {held} row(s) in {COVERAGE} hold one;"
            " coverage history is never silently destroyed"
        )
    op.execute(f"DROP TRIGGER trg_{COVERAGE}_fence_update")
    op.execute(f"DROP TRIGGER trg_{COVERAGE}_fence_insert")
    op.execute(f"ALTER TABLE {COVERAGE} DROP COLUMN truth_digest")
