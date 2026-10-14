"""Persistence of supplier onboarding (ADR-0036 §3), migration 0060.

- ``onboarded_suppliers``: the immutable identity of one onboarded site — its key
  (``site-<slug>``, never a repository key), display name, ``https`` origin and template.
- ``onboarding_transitions``: the append-only state log, numbered from one by exactly one. The
  first row is ``DRAFT``; every later row moves from the state the row before it reached, along an
  allowed edge only. The newest row is the current state.
- ``onboarding_hosts``: one row per host confirmation or disable, numbered per supplier. The
  newest row of a ``(host, role)`` decides it. Only the bootstrap storefront host has no trial.
- ``onboarding_word_revisions``: words per template slot, numbered from one, with a fingerprint.
- ``onboarding_policy_reads``: the robots.txt and terms reads shown at add time, acknowledged.
- ``onboarding_trials``: one trial read (§5), with the fingerprint of the facts it showed:
  never a ``ProductFactsRevision`` (OB-02).
- ``onboarding_verifications``: the operator's verification of one trial's facts.

The database refuses an update or a delete of any row, and a transition to ``ACTIVE`` that the
trials and verifications do not prove (ADR-0036 §7, OB-04). ``store.OnboardingStore`` is the only
writer. No credential is ever stored here (OB-05).
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime

# Frozen with migration 0060.
KEY_FORM = (
    "length(supplier_key) BETWEEN 6 AND 40 AND supplier_key GLOB 'site-[a-z0-9]*'"
    " AND supplier_key NOT GLOB '*[^a-z0-9-]*'"
)
HOST_FORM = "length(host) BETWEEN 3 AND 253 AND host NOT GLOB '*[^a-z0-9.-]*' AND host GLOB '*.*'"
TEMPLATES = "template IN ('cafe24', 'godomall')"
STATES = "('DRAFT', 'RECON', 'ACTIVE', 'DISABLED')"
ROLES = "role IN ('storefront', 'login', 'image')"
ACTIVE_NAMES_TEMPLATE = (
    "to_state <> 'ACTIVE' OR (template_revision IS NOT NULL AND template_revision <> '')"
)


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _json(column: str, kind: str) -> str:
    return f"json_valid({column}) AND json_type({column}) = '{kind}'"


class OnboardedSupplier(Base):
    __tablename__ = "onboarded_suppliers"
    __table_args__ = (
        CheckConstraint(KEY_FORM, name="key_form"),
        CheckConstraint("display_name <> ''", name="name_present"),
        CheckConstraint("origin GLOB 'https://?*' AND origin NOT GLOB '*/*/*/*'", name="origin"),
        CheckConstraint(TEMPLATES, name="template_known"),
        CheckConstraint("created_by <> ''", name="created_by_present"),
    )

    supplier_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(272), unique=True)
    template: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class OnboardingTransition(Base):
    __tablename__ = "onboarding_transitions"
    __table_args__ = (
        UniqueConstraint("supplier_key", "seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(f"to_state IN {STATES}", name="to_state_known"),
        CheckConstraint(
            f"(seq = 1 AND from_state IS NULL AND to_state = 'DRAFT')"
            f" OR (seq > 1 AND from_state IN {STATES})",
            name="first_is_draft",
        ),
        CheckConstraint("actor <> ''", name="actor_present"),
        CheckConstraint(_json("evidence_json", "array"), name="evidence_is_array"),
        CheckConstraint(ACTIVE_NAMES_TEMPLATE, name="active_names_template"),
    )

    transition_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("onboarded_suppliers.supplier_key")
    )
    seq: Mapped[int] = mapped_column(Integer)
    from_state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    to_state: Mapped[str] = mapped_column(String(8))
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(200))
    evidence_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # An ACTIVE row's template revision: the one its proof was judged under (ADR-0036 §7).
    template_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OnboardingTrial(Base):
    __tablename__ = "onboarding_trials"
    __table_args__ = (
        CheckConstraint("sample_url_key <> ''", name="sample_present"),
        CheckConstraint("template_revision <> ''", name="template_revision_present"),
        CheckConstraint("words_seq >= 0", name="words_seq_known"),
        CheckConstraint(_hex64("capture_sha256"), name="capture_sha256_hex"),
        CheckConstraint(_hex64("facts_fingerprint"), name="facts_fingerprint_hex"),
        CheckConstraint(_json("field_status_json", "object"), name="field_status_is_object"),
        CheckConstraint(_json("unread_rows_json", "array"), name="unread_rows_is_array"),
        CheckConstraint(_json("observed_hosts_json", "array"), name="observed_hosts_is_array"),
        CheckConstraint(_json("structural_json", "array"), name="structural_is_array"),
        CheckConstraint("created_by <> ''", name="created_by_present"),
    )

    trial_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("onboarded_suppliers.supplier_key")
    )
    sample_url_key: Mapped[str] = mapped_column(String(512))
    template_revision: Mapped[str] = mapped_column(String(64))
    words_seq: Mapped[int] = mapped_column(Integer)
    capture_sha256: Mapped[str] = mapped_column(String(64))
    # The fingerprint of the facts the trial showed; a verification names exactly this (§7).
    facts_fingerprint: Mapped[str] = mapped_column(String(64))
    field_status_json: Mapped[str] = mapped_column(Text)
    unread_rows_json: Mapped[str] = mapped_column(Text)
    observed_hosts_json: Mapped[str] = mapped_column(Text)
    structural_json: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class OnboardingHost(Base):
    __tablename__ = "onboarding_hosts"
    __table_args__ = (
        UniqueConstraint("supplier_key", "seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(HOST_FORM, name="host_form"),
        CheckConstraint(ROLES, name="role_known"),
        CheckConstraint("disabled IN (0, 1)", name="disabled_flag"),
        # OB-01: a confirmed host was observed by a trial, except the one bootstrap storefront.
        CheckConstraint(
            "observed_trial_id IS NOT NULL OR role = 'storefront' OR disabled = 1",
            name="observed_or_bootstrap",
        ),
        CheckConstraint("confirmed_by <> ''", name="confirmed_by_present"),
    )

    host_row_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("onboarded_suppliers.supplier_key")
    )
    seq: Mapped[int] = mapped_column(Integer)
    host: Mapped[str] = mapped_column(String(253))
    role: Mapped[str] = mapped_column(String(16))
    observed_trial_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("onboarding_trials.trial_id"), nullable=True
    )
    disabled: Mapped[bool] = mapped_column(Boolean)
    confirmed_by: Mapped[str] = mapped_column(String(64))
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class OnboardingWordRevision(Base):
    __tablename__ = "onboarding_word_revisions"
    __table_args__ = (
        UniqueConstraint("supplier_key", "seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(_json("words_json", "object"), name="words_is_object"),
        CheckConstraint(_hex64("fingerprint"), name="fingerprint_hex"),
        CheckConstraint("authored_by <> ''", name="authored_by_present"),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("onboarded_suppliers.supplier_key")
    )
    seq: Mapped[int] = mapped_column(Integer)
    words_json: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    authored_by: Mapped[str] = mapped_column(String(64))
    authored_at: Mapped[datetime] = mapped_column(UTCDateTime)


class OnboardingPolicyRead(Base):
    __tablename__ = "onboarding_policy_reads"
    __table_args__ = (
        CheckConstraint("document IN ('ROBOTS', 'TERMS')", name="document_known"),
        CheckConstraint("path GLOB '/*'", name="path_absolute"),
        CheckConstraint("http_status BETWEEN 100 AND 599", name="http_status_range"),
        CheckConstraint(
            f"content_sha256 IS NULL OR ({_hex64('content_sha256')})", name="content_sha256_hex"
        ),
        CheckConstraint("acknowledged_by <> ''", name="acknowledged_by_present"),
    )

    read_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("onboarded_suppliers.supplier_key")
    )
    document: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(256))
    http_status: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    read_at: Mapped[datetime] = mapped_column(UTCDateTime)
    acknowledged_by: Mapped[str] = mapped_column(String(64))
    acknowledged_at: Mapped[datetime] = mapped_column(UTCDateTime)


class OnboardingVerification(Base):
    __tablename__ = "onboarding_verifications"
    __table_args__ = (
        CheckConstraint(_hex64("shown_fingerprint"), name="shown_fingerprint_hex"),
        CheckConstraint("verdict IN ('CORRECT', 'WRONG')", name="verdict_known"),
        CheckConstraint("verified_by <> ''", name="verified_by_present"),
    )

    verification_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trial_id: Mapped[str] = mapped_column(String(36), ForeignKey("onboarding_trials.trial_id"))
    supplier_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("onboarded_suppliers.supplier_key")
    )
    shown_fingerprint: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(8))
    verified_by: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(UTCDateTime)
