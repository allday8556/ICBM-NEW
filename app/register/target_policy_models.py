"""Persistence of the durable registration target policy (Gate 1 G1-A, ADR-0015 §2).

Three tables, created by migration 0019:

- ``registration_target_policies``: the identity of one policy per ``marketplace_key ×
  marketplace_account_id``, scoped by the canonical account's composite foreign key. Immutable.
- ``registration_target_policy_revisions``: the append-only revisions. Each holds one canonical,
  sanitized content document and the server-computed fingerprint of it. A revision is never updated
  or deleted, and its number moves by exactly one.
- ``registration_target_policy_current``: the one current revision of each policy. It moves only
  forward, to the newest revision of its own policy, and is never deleted.

A revision owns only the policy inputs ADR-0015 §2 names. There is no readiness, status or reason
code here, no price, no Snapshot and no provider truth. ``marketplace_account_id`` is the canonical
account (``docs/GLOSSARY.md`` §1); the M4 pricing context's ``account_id`` discriminator lives only
inside the content document, never as a column.
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class RegistrationTargetPolicy(Base):
    __tablename__ = "registration_target_policies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["marketplace_key", "marketplace_account_id"],
            ["marketplace_accounts.marketplace_key", "marketplace_accounts.marketplace_account_id"],
        ),
        UniqueConstraint("marketplace_key", "marketplace_account_id"),
        CheckConstraint("marketplace_key <> ''", name="marketplace_key_present"),
        CheckConstraint("created_by <> ''", name="created_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    policy_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    marketplace_account_id: Mapped[str] = mapped_column(String(40))
    created_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RegistrationTargetPolicyRevision(Base):
    __tablename__ = "registration_target_policy_revisions"
    __table_args__ = (
        UniqueConstraint("policy_id", "revision_no"),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint(
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            name="content_is_object",
        ),
        CheckConstraint(_hex64("content_fingerprint"), name="content_fingerprint_hex"),
        CheckConstraint("authored_by <> ''", name="authored_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    # The server-created identity of the revision: the TargetPolicy ``policy_revision``.
    policy_revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    policy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_target_policies.policy_id")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[str] = mapped_column(Text)
    # SHA-256 of the canonical sanitized content document, never of request bytes.
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    authored_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    authored_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RegistrationTargetPolicyCurrent(Base):
    __tablename__ = "registration_target_policy_current"
    __table_args__ = (
        CheckConstraint("moved_by <> ''", name="moved_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    policy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_target_policies.policy_id"), primary_key=True
    )
    policy_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_target_policy_revisions.policy_revision_id")
    )
    moved_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)
