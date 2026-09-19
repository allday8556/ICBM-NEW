"""Persistence of the M4 image foundation (Issue #80 PR-E, ADR-0013 §9): migration 0014.

PRODUCT-owned, and apart from COLLECT's source truth: ``source_assets`` and
``product_facts_image_refs`` are only referenced, never written.

- ``derived_image_artifacts``: one row per exact output binary, content-addressed apart from
  source assets. It describes the bytes only.
- ``derived_image_derivations``: one completed derivation that produced an artifact, with the
  source revision it was validated against, its canonical input manifest, recipe and operation
  provenance. Many derivations may name one artifact.
- ``derived_image_derivation_inputs`` and ``derived_image_derivation_roots``: each input (a source
  asset, or a parent artifact with the derivation that produced it), and the source assets the
  lineage traces back to.
- ``image_selection_revisions``, ``image_selection_source_decisions``,
  ``image_selection_outputs`` and ``current_image_selection_moves``: an operator's decision for
  every CONFIRMED source reference of one revision, the ordered images the Item uses, and the
  append-only history of which decision is current.
- ``image_qa_results``: one verdict for one exact binary under one QA rule and input.

Every table rejects UPDATE and DELETE. The cross-row invariants are the triggers of migration
0014.
"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from app.collect.facts import ImageRole
from app.db.base import Base
from app.db.types import UTCDateTime
from app.products.image_model import (
    MAX_FINDINGS,
    MAX_INPUTS,
    MAX_OPERATIONS,
    MAX_SPEC_BYTES,
    DecisionOrigin,
    ImageAssetKind,
    QaVerdict,
    SelectionMoveReason,
    SourceDecisionKind,
)


def _in(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _present(column: str) -> str:
    return f"{column} <> ''"


def _kind(column: str, derivation: str) -> str:
    return f"({column} = 'DERIVED_ARTIFACT') = ({derivation} IS NOT NULL)"


class DerivedImageArtifact(Base):
    __tablename__ = "derived_image_artifacts"
    __table_args__ = (
        CheckConstraint(_hex64("artifact_sha256"), name="sha256_hex"),
        CheckConstraint("mime_type LIKE 'image/%'", name="mime_is_image"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("width > 0 AND height > 0", name="dimensions_positive"),
    )

    artifact_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    mime_type: Mapped[str] = mapped_column(String(40))
    byte_size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    stored_at: Mapped[datetime] = mapped_column(UTCDateTime)


class DerivedImageDerivation(Base):
    __tablename__ = "derived_image_derivations"
    __table_args__ = (
        UniqueConstraint("derivation_fingerprint"),
        Index("ix_derived_image_derivations_artifact", "artifact_sha256"),
        CheckConstraint(_hex64("derivation_fingerprint"), name="fingerprint_hex"),
        CheckConstraint(
            "json_valid(input_manifest_json) AND json_type(input_manifest_json) = 'array'"
            f" AND input_count >= 1 AND input_count <= {MAX_INPUTS}"
            " AND json_array_length(input_manifest_json) = input_count",
            name="inputs_manifest",
        ),
        CheckConstraint(
            "json_valid(transformation_spec_json)"
            " AND json_type(transformation_spec_json) = 'object'"
            f" AND length(transformation_spec_json) <= {MAX_SPEC_BYTES}"
            " AND instr(transformation_spec_json, '://') = 0",
            name="spec_bounded",
        ),
        CheckConstraint(
            "json_valid(operations_json) AND json_type(operations_json) = 'array'"
            f" AND json_array_length(operations_json) BETWEEN 1 AND {MAX_OPERATIONS}"
            " AND instr(operations_json, '://') = 0",
            name="operations_bounded",
        ),
        CheckConstraint(_present("transformation_version"), name="transformation_version_present"),
        CheckConstraint(
            "policy_version IS NULL OR policy_version <> ''", name="policy_version_present"
        ),
        CheckConstraint(_present("decided_by"), name="decided_by_present"),
        CheckConstraint(_present("correlation_id"), name="correlation_present"),
    )

    derivation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    artifact_sha256: Mapped[str] = mapped_column(
        String(64), ForeignKey("derived_image_artifacts.artifact_sha256")
    )
    validated_source_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    derivation_fingerprint: Mapped[str] = mapped_column(String(64))
    input_manifest_json: Mapped[str] = mapped_column(Text)
    input_count: Mapped[int] = mapped_column(Integer)
    transformation_spec_json: Mapped[str] = mapped_column(Text)
    transformation_version: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str | None] = mapped_column(String(64))
    operations_json: Mapped[str] = mapped_column(Text)
    produced_at: Mapped[datetime] = mapped_column(UTCDateTime)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))


class DerivedImageDerivationInput(Base):
    __tablename__ = "derived_image_derivation_inputs"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint(_in("input_kind", ImageAssetKind), name="input_kind_valid"),
        CheckConstraint(
            "(input_kind = 'SOURCE_ASSET' AND source_sha256 IS NOT NULL"
            " AND parent_artifact_sha256 IS NULL AND parent_derivation_id IS NULL)"
            " OR (input_kind = 'DERIVED_ARTIFACT' AND source_sha256 IS NULL"
            " AND parent_artifact_sha256 IS NOT NULL AND parent_derivation_id IS NOT NULL)",
            name="input_kind_columns",
        ),
        CheckConstraint(
            "parent_derivation_id IS NULL OR parent_derivation_id <> derivation_id",
            name="not_its_own_parent",
        ),
    )

    derivation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("derived_image_derivations.derivation_id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    input_kind: Mapped[str] = mapped_column(String(20))
    source_sha256: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("source_assets.sha256")
    )
    parent_artifact_sha256: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("derived_image_artifacts.artifact_sha256")
    )
    parent_derivation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("derived_image_derivations.derivation_id")
    )


class DerivedImageDerivationRoot(Base):
    """A source asset a derivation's lineage traces back to."""

    __tablename__ = "derived_image_derivation_roots"

    derivation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("derived_image_derivations.derivation_id"), primary_key=True
    )
    source_sha256: Mapped[str] = mapped_column(
        String(64), ForeignKey("source_assets.sha256"), primary_key=True
    )


class ImageSelectionRevision(Base):
    __tablename__ = "image_selection_revisions"
    __table_args__ = (
        UniqueConstraint("item_id", "revision_no"),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint(_hex64("source_decision_fingerprint"), name="source_fingerprint_hex"),
        CheckConstraint(_hex64("selection_fingerprint"), name="selection_fingerprint_hex"),
        CheckConstraint(_in("decision_origin", DecisionOrigin), name="operator_decision"),
        CheckConstraint(
            "decision_count >= 0 AND output_count >= 0 AND output_count <= decision_count",
            name="counts_valid",
        ),
        CheckConstraint(_present("decided_by"), name="decided_by_present"),
        CheckConstraint(
            "reason IS NULL OR (reason <> '' AND length(reason) <= 200)", name="reason_bounded"
        ),
        CheckConstraint(_present("correlation_id"), name="correlation_present"),
    )

    selection_revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_items.item_id"))
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    source_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    source_decision_fingerprint: Mapped[str] = mapped_column(String(64))
    selection_fingerprint: Mapped[str] = mapped_column(String(64))
    decision_origin: Mapped[str] = mapped_column(String(20))
    decision_count: Mapped[int] = mapped_column(Integer)
    output_count: Mapped[int] = mapped_column(Integer)
    decided_by: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(200))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ImageSelectionSourceDecision(Base):
    __tablename__ = "image_selection_source_decisions"
    __table_args__ = (
        CheckConstraint(_in("source_role", ImageRole), name="source_role_valid"),
        CheckConstraint("source_ordinal >= 0", name="source_ordinal_non_negative"),
        CheckConstraint(_in("decision", SourceDecisionKind), name="decision_valid"),
        CheckConstraint(
            "(decision = 'USE_DERIVED') = (derivation_id IS NOT NULL)",
            name="derived_names_derivation",
        ),
    )

    selection_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("image_selection_revisions.selection_revision_id"),
        primary_key=True,
    )
    source_role: Mapped[str] = mapped_column(String(20), primary_key=True)
    source_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), ForeignKey("source_assets.sha256"))
    decision: Mapped[str] = mapped_column(String(20))
    derivation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("derived_image_derivations.derivation_id")
    )


class ImageSelectionOutput(Base):
    __tablename__ = "image_selection_outputs"
    __table_args__ = (
        UniqueConstraint("selection_revision_id", "source_role", "source_ordinal"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint(_in("role", ImageRole), name="role_valid"),
        CheckConstraint(_in("asset_kind", ImageAssetKind), name="asset_kind_valid"),
        CheckConstraint(_hex64("sha256"), name="sha256_hex"),
        CheckConstraint(_kind("asset_kind", "derivation_id"), name="derived_names_derivation"),
    )

    selection_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("image_selection_revisions.selection_revision_id"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(20))
    source_role: Mapped[str] = mapped_column(String(20))
    source_ordinal: Mapped[int] = mapped_column(Integer)
    asset_kind: Mapped[str] = mapped_column(String(20))
    sha256: Mapped[str] = mapped_column(String(64))
    derivation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("derived_image_derivations.derivation_id")
    )


class CurrentImageSelectionMove(Base):
    __tablename__ = "current_image_selection_moves"
    __table_args__ = (
        UniqueConstraint("item_id", "sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(_in("reason", SelectionMoveReason), name="reason_valid"),
        CheckConstraint(
            "(sequence = 1) = (previous_selection_revision_id IS NULL)",
            name="first_move_has_no_previous",
        ),
        CheckConstraint("(reason = 'INITIAL') = (sequence = 1)", name="initial_opens_history"),
        CheckConstraint(
            "previous_selection_revision_id IS NULL"
            " OR previous_selection_revision_id <> selection_revision_id",
            name="move_changes_selection",
        ),
        CheckConstraint(_present("rule_version"), name="rule_version_present"),
        CheckConstraint(_present("decided_by"), name="decided_by_present"),
        CheckConstraint(_present("correlation_id"), name="correlation_present"),
    )

    move_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_items.item_id"))
    sequence: Mapped[int] = mapped_column(Integer)
    selection_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("image_selection_revisions.selection_revision_id")
    )
    previous_selection_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("image_selection_revisions.selection_revision_id")
    )
    reason: Mapped[str] = mapped_column(String(20))
    rule_version: Mapped[str] = mapped_column(String(64))
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ImageQaResult(Base):
    __tablename__ = "image_qa_results"
    __table_args__ = (
        # One authoritative verdict per exact input: a source asset has no derivation, so its
        # identity is completed by a partial index of its own.
        Index(
            "ux_image_qa_results_source_input",
            "sha256",
            "validated_source_revision_id",
            "qa_rule_version",
            "qa_input_fingerprint",
            unique=True,
            sqlite_where=text("derivation_id IS NULL"),
        ),
        Index(
            "ux_image_qa_results_derived_input",
            "derivation_id",
            "sha256",
            "validated_source_revision_id",
            "qa_rule_version",
            "qa_input_fingerprint",
            unique=True,
            sqlite_where=text("derivation_id IS NOT NULL"),
        ),
        CheckConstraint(_in("asset_kind", ImageAssetKind), name="asset_kind_valid"),
        CheckConstraint(_hex64("sha256"), name="sha256_hex"),
        CheckConstraint(_kind("asset_kind", "derivation_id"), name="derived_names_derivation"),
        CheckConstraint(_present("qa_rule_version"), name="rule_version_present"),
        CheckConstraint(_hex64("qa_input_fingerprint"), name="input_fingerprint_hex"),
        CheckConstraint(_in("verdict", QaVerdict), name="verdict_valid"),
        CheckConstraint(
            "json_valid(findings_json) AND json_type(findings_json) = 'array'"
            f" AND json_array_length(findings_json) <= {MAX_FINDINGS}",
            name="findings_bounded",
        ),
        CheckConstraint(_present("decided_by"), name="decided_by_present"),
        CheckConstraint(_present("correlation_id"), name="correlation_present"),
    )

    qa_result_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_kind: Mapped[str] = mapped_column(String(20))
    sha256: Mapped[str] = mapped_column(String(64))
    derivation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("derived_image_derivations.derivation_id")
    )
    validated_source_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    qa_rule_version: Mapped[str] = mapped_column(String(64))
    qa_input_fingerprint: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(20))
    findings_json: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
