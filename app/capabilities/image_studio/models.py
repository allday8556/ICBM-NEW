"""Persistence of the Image Studio translation owner (ADR-0041 §3), migration 0065.

- ``image_ocr_observations``: one OCR reading of one source image, content-addressed by the source
  SHA-256, the OCR engine id and version and the preprocessing version, with its regions.
- ``image_translation_candidates``: the AI's Korean suggestion per sent region of one observation,
  with the translation fingerprint of ADR-0012 §6 / ADR-0026 and the provenance copied from the
  outcome. One candidate per fingerprint.
- ``image_edit_revisions``: the operator's saved edit of one image of one product, numbered from one
  by exactly one per lineage (product group, source SHA-256).
- ``image_edit_current``: the forward-only current revision of each lineage.

The database refuses an update or a delete of the first three, malformed regions or items, a
numbering gap and a pointer that moves backwards or to another lineage (migration 0065's triggers).
``store.ImageStudioStore`` is the only writer.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _json(column: str, kind: str) -> str:
    return f"json_valid({column}) AND json_type({column}) = '{kind}'"


class ImageOcrObservation(Base):
    __tablename__ = "image_ocr_observations"
    __table_args__ = (
        UniqueConstraint(
            "source_sha256", "ocr_engine_id", "ocr_engine_version", "preprocessing_version"
        ),
        CheckConstraint(_hex64("source_sha256"), name="source_sha256_hex"),
        CheckConstraint("ocr_engine_id <> ''", name="engine_id_present"),
        CheckConstraint("ocr_engine_version <> ''", name="engine_version_present"),
        CheckConstraint("preprocessing_version <> ''", name="preprocessing_version_present"),
        CheckConstraint(_json("regions_json", "array"), name="regions_is_array"),
        CheckConstraint(_hex64("observation_fingerprint"), name="fingerprint_hex"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), ForeignKey("source_assets.sha256"))
    ocr_engine_id: Mapped[str] = mapped_column(String(64))
    ocr_engine_version: Mapped[str] = mapped_column(String(64))
    preprocessing_version: Mapped[str] = mapped_column(String(64))
    regions_json: Mapped[str] = mapped_column(Text)
    observation_fingerprint: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ImageTranslationCandidate(Base):
    __tablename__ = "image_translation_candidates"
    __table_args__ = (
        UniqueConstraint("translation_fingerprint"),
        CheckConstraint("source_language IN ('zh', 'ja')", name="source_language_known"),
        CheckConstraint(_json("sent_region_ids_json", "array"), name="sent_is_array"),
        CheckConstraint("json_array_length(sent_region_ids_json) > 0", name="sent_not_empty"),
        CheckConstraint(_hex64("relevant_input"), name="relevant_input_hex"),
        CheckConstraint(_json("prompt_version_json", "object"), name="prompt_version_is_object"),
        CheckConstraint(_json("policy_version_json", "object"), name="policy_version_is_object"),
        CheckConstraint(
            _json("requested_identity_json", "object"), name="requested_identity_is_object"
        ),
        CheckConstraint("output_schema_version <> ''", name="schema_version_present"),
        CheckConstraint(_hex64("translation_fingerprint"), name="fingerprint_hex"),
        CheckConstraint(_json("translations_json", "array"), name="translations_is_array"),
        CheckConstraint(
            "json_array_length(translations_json) = json_array_length(sent_region_ids_json)",
            name="one_translation_per_sent_region",
        ),
        CheckConstraint(_json("provenance_json", "object"), name="provenance_is_object"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    observation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("image_ocr_observations.observation_id"), index=True
    )
    source_language: Mapped[str] = mapped_column(String(2))
    sent_region_ids_json: Mapped[str] = mapped_column(Text)
    relevant_input: Mapped[str] = mapped_column(String(64))
    prompt_version_json: Mapped[str] = mapped_column(Text)
    policy_version_json: Mapped[str] = mapped_column(Text)
    requested_identity_json: Mapped[str] = mapped_column(Text)
    output_schema_version: Mapped[str] = mapped_column(String(64))
    translation_fingerprint: Mapped[str] = mapped_column(String(64))
    translations_json: Mapped[str] = mapped_column(Text)
    provenance_json: Mapped[str] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ImageEditRevision(Base):
    __tablename__ = "image_edit_revisions"
    __table_args__ = (
        UniqueConstraint("product_group_id", "source_sha256", "seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(_hex64("source_sha256"), name="source_sha256_hex"),
        CheckConstraint(_json("items_json", "array"), name="items_is_array"),
        CheckConstraint("renderer_version <> ''", name="renderer_version_present"),
        CheckConstraint(_json("font_hashes_json", "object"), name="font_hashes_is_object"),
        CheckConstraint(_hex64("content_fingerprint"), name="content_fingerprint_hex"),
        CheckConstraint("authored_by <> ''", name="authored_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    source_sha256: Mapped[str] = mapped_column(String(64), ForeignKey("source_assets.sha256"))
    seq: Mapped[int] = mapped_column(Integer)
    observation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("image_ocr_observations.observation_id")
    )
    candidate_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("image_translation_candidates.candidate_id"), nullable=True
    )
    items_json: Mapped[str] = mapped_column(Text)
    renderer_version: Mapped[str] = mapped_column(String(64))
    font_hashes_json: Mapped[str] = mapped_column(Text)
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    authored_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    authored_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ImageEditCurrent(Base):
    __tablename__ = "image_edit_current"
    __table_args__ = (
        CheckConstraint("moved_by <> ''", name="moved_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    product_group_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("image_edit_revisions.revision_id")
    )
    moved_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)
