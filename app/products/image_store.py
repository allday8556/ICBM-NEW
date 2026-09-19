"""PRODUCT-owned persistence for derived images, operator selections and QA (Issue #80 PR-E).

**Bytes.** :class:`DerivedImageStore` keeps derived artifacts content-addressed under
``<derived-images>/sha256/<aa>/<sha256>``, apart from COLLECT's source assets, following the
proven source-asset pattern: the SHA-256 is computed here from the bytes and never taken from a
caller; MIME and dimensions come from decoding those same bytes; unsupported bytes are refused
before anything is written; the file is written atomically before its row; an existing file is
only ever replaced by bytes matching its name; every read verifies the checksum again. A file left
behind by a rolled-back decision is not a truth row: the same bytes land at the same path, and a
retry reuses it.

**Rows.** :class:`ImageUnit` writes and reads the image tables inside a caller's unit of work and
decides nothing: the image service validates, and migration 0014 refuses any row that breaks
lineage, full source accounting, the current-selection chain or exact QA identity. It never writes
``source_assets`` or ``product_facts_image_refs``.
"""

import hashlib
import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collect.assets import ImageDecoder
from app.collect.facts import FieldStatus, ImageRole
from app.collect.models import ProductFactsImageRef
from app.core.clock import Clock
from app.core.errors import AppError, ErrorClass, NotFoundError
from app.db.database import Database
from app.products.image_model import (
    CURRENT_SELECTION_RULE_VERSION,
    DecisionOrigin,
    DerivationInput,
    ImageAssetKind,
    QaVerdict,
    SelectedOutput,
    SelectionMoveReason,
    SourceDecision,
    SourceDecisionKind,
    SourceRef,
)
from app.products.image_models import (
    CurrentImageSelectionMove,
    DerivedImageArtifact,
    DerivedImageDerivation,
    DerivedImageDerivationInput,
    DerivedImageDerivationRoot,
    ImageQaResult,
    ImageSelectionOutput,
    ImageSelectionRevision,
    ImageSelectionSourceDecision,
)


class UnsupportedDerivedImageError(AppError):
    """The output bytes are not an image the decoder can read; nothing is recorded."""

    error_class = ErrorClass.VALIDATION


class DerivedImageIntegrityError(AppError):
    """Stored derived bytes or metadata no longer match their checksum."""

    error_class = ErrorClass.FATAL


@dataclass(frozen=True)
class StoredArtifact:
    sha256: str
    mime_type: str
    byte_size: int
    width: int
    height: int


class DerivedImageStore:
    """Content-addressed derived image bytes, in their own namespace."""

    def __init__(self, directory: Path, db: Database, decoder: ImageDecoder) -> None:
        self._directory = directory
        self._db = db
        self._decoder = decoder

    @property
    def directory(self) -> Path:
        return self._directory

    def path(self, sha256: str) -> Path:
        return self._directory / "sha256" / sha256[:2] / sha256

    def describe(self, data: bytes) -> StoredArtifact:
        """What these bytes are, computed from them alone. Nothing is written."""
        if not data:
            raise UnsupportedDerivedImageError(
                "PRODUCTS_IMAGE_EMPTY", "an empty output is no image"
            )
        decoded = self._decoder.decode(data)
        if (
            decoded is None
            or not decoded.mime_type.startswith("image/")
            or decoded.width <= 0
            or decoded.height <= 0
        ):
            raise UnsupportedDerivedImageError(
                "PRODUCTS_IMAGE_UNSUPPORTED", "the output is not an image the decoder can read"
            )
        return StoredArtifact(
            hashlib.sha256(data).hexdigest(),
            decoded.mime_type,
            len(data),
            decoded.width,
            decoded.height,
        )

    def write(self, artifact: StoredArtifact, data: bytes) -> None:
        """Place the bytes at their content address, atomically; an existing correct file stays."""
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise DerivedImageIntegrityError(
                "PRODUCTS_IMAGE_CHECKSUM_MISMATCH", "the bytes do not match their description"
            )
        path = self.path(artifact.sha256)
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == artifact.sha256:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f".{artifact.sha256}.{uuid.uuid4().hex}.tmp")
        try:
            with staging.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)

    def get(self, sha256: str) -> StoredArtifact | None:
        with self._db.read() as session:
            row = session.get(DerivedImageArtifact, sha256)
            return None if row is None else _artifact(row)

    def read(self, sha256: str) -> bytes:
        """The stored bytes, verified against their checksum and their row."""
        stored = self.get(sha256)
        if stored is None:
            raise NotFoundError("PRODUCTS_IMAGE_UNKNOWN", "no derived artifact has that checksum")
        try:
            data = self.path(sha256).read_bytes()
        except FileNotFoundError:
            raise DerivedImageIntegrityError(
                "PRODUCTS_IMAGE_MISSING", "a derived artifact's file is missing"
            ) from None
        if hashlib.sha256(data).hexdigest() != sha256 or len(data) != stored.byte_size:
            raise DerivedImageIntegrityError(
                "PRODUCTS_IMAGE_CORRUPT", "a derived artifact no longer matches its checksum"
            )
        return data


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class DerivationRecord:
    derivation_id: str
    artifact_sha256: str
    validated_source_revision_id: str
    derivation_fingerprint: str
    inputs: tuple[DerivationInput, ...]
    roots: tuple[str, ...]
    transformation_version: str
    policy_version: str | None
    produced_at: datetime


@dataclass(frozen=True)
class SelectedImage:
    position: int
    role: ImageRole
    source_role: ImageRole
    source_ordinal: int
    asset_kind: ImageAssetKind
    sha256: str
    derivation_id: str | None


@dataclass(frozen=True)
class SelectionRecord:
    selection_revision_id: str
    item_id: str
    product_group_id: str
    source_revision_id: str
    revision_no: int
    source_decision_fingerprint: str
    selection_fingerprint: str
    decision_origin: DecisionOrigin
    decided_by: str
    decisions: tuple[SourceDecision, ...]
    outputs: tuple[SelectedImage, ...]


@dataclass(frozen=True)
class SelectionMove:
    move_id: str
    item_id: str
    sequence: int
    selection_revision_id: str
    previous_selection_revision_id: str | None
    reason: SelectionMoveReason


@dataclass(frozen=True)
class QaRecord:
    qa_result_id: str
    asset_kind: ImageAssetKind
    sha256: str
    derivation_id: str | None
    validated_source_revision_id: str
    qa_rule_version: str
    qa_input_fingerprint: str
    verdict: QaVerdict
    findings: tuple[str, ...]


class ImageUnit:
    """Image writes and reads over one caller-owned session. It never commits."""

    def __init__(self, session: Session, clock: Clock) -> None:
        self.session = session
        self._clock = clock

    # ------------------------------------------------------------------ source references

    def confirmed_refs(self, revision_id: str) -> tuple[SourceRef, ...]:
        """Every CONFIRMED image reference of one revision, in source order. Read only."""
        rows = self.session.scalars(
            select(ProductFactsImageRef).where(
                ProductFactsImageRef.revision_id == revision_id,
                ProductFactsImageRef.status == FieldStatus.CONFIRMED.value,
            )
        ).all()
        refs = [
            SourceRef(ImageRole(row.role), row.ordinal, row.sha256)
            for row in rows
            if row.sha256 is not None
        ]
        return tuple(
            sorted(refs, key=lambda ref: (ref.role != ImageRole.REPRESENTATIVE, ref.ordinal))
        )

    # ------------------------------------------------------------------ derivations

    def ensure_artifact(self, artifact: StoredArtifact) -> None:
        existing = self.session.get(DerivedImageArtifact, artifact.sha256)
        if existing is None:
            self.session.add(
                DerivedImageArtifact(
                    artifact_sha256=artifact.sha256,
                    mime_type=artifact.mime_type,
                    byte_size=artifact.byte_size,
                    width=artifact.width,
                    height=artifact.height,
                    stored_at=self._clock.now(),
                )
            )
            self.session.flush()
        elif _artifact(existing) != artifact:
            raise DerivedImageIntegrityError(
                "PRODUCTS_IMAGE_METADATA_CONFLICT",
                "stored artifact metadata differs from a new decode of the same bytes",
            )

    def derivation_by_fingerprint(self, fingerprint: str) -> DerivationRecord | None:
        row = self.session.scalars(
            select(DerivedImageDerivation).where(
                DerivedImageDerivation.derivation_fingerprint == fingerprint
            )
        ).first()
        return None if row is None else self._derivation(row)

    def derivation(self, derivation_id: str) -> DerivationRecord | None:
        row = self.session.get(DerivedImageDerivation, derivation_id)
        return None if row is None else self._derivation(row)

    def record_derivation(
        self,
        *,
        artifact_sha256: str,
        validated_source_revision_id: str,
        fingerprint: str,
        inputs: Sequence[DerivationInput],
        roots: Sequence[str],
        transformation_spec_json: str,
        transformation_version: str,
        policy_version: str | None,
        operations_json: str,
        produced_at: datetime,
        decided_by: str,
        correlation_id: str,
    ) -> DerivationRecord:
        derivation_id = str(uuid.uuid4())
        self.session.add(
            DerivedImageDerivation(
                derivation_id=derivation_id,
                artifact_sha256=artifact_sha256,
                validated_source_revision_id=validated_source_revision_id,
                derivation_fingerprint=fingerprint,
                input_manifest_json=json.dumps([item.canonical() for item in inputs]),
                input_count=len(inputs),
                transformation_spec_json=transformation_spec_json,
                transformation_version=transformation_version,
                policy_version=policy_version,
                operations_json=operations_json,
                produced_at=produced_at,
                recorded_at=self._clock.now(),
                decided_by=decided_by,
                correlation_id=correlation_id,
            )
        )
        self.session.flush()
        for ordinal, item in enumerate(inputs):
            source = item.kind is ImageAssetKind.SOURCE_ASSET
            self.session.add(
                DerivedImageDerivationInput(
                    derivation_id=derivation_id,
                    ordinal=ordinal,
                    input_kind=item.kind.value,
                    source_sha256=item.sha256 if source else None,
                    parent_artifact_sha256=None if source else item.sha256,
                    parent_derivation_id=item.parent_derivation_id,
                )
            )
            self.session.flush()
        for root in sorted(set(roots)):
            self.session.add(
                DerivedImageDerivationRoot(derivation_id=derivation_id, source_sha256=root)
            )
            self.session.flush()
        record = self.derivation(derivation_id)
        assert record is not None
        return record

    def _derivation(self, row: DerivedImageDerivation) -> DerivationRecord:
        inputs = tuple(
            DerivationInput(
                ImageAssetKind(item.input_kind),
                item.source_sha256 or item.parent_artifact_sha256 or "",
                item.parent_derivation_id,
            )
            for item in self.session.scalars(
                select(DerivedImageDerivationInput)
                .where(DerivedImageDerivationInput.derivation_id == row.derivation_id)
                .order_by(DerivedImageDerivationInput.ordinal)
            )
        )
        roots = tuple(
            self.session.scalars(
                select(DerivedImageDerivationRoot.source_sha256)
                .where(DerivedImageDerivationRoot.derivation_id == row.derivation_id)
                .order_by(DerivedImageDerivationRoot.source_sha256)
            )
        )
        return DerivationRecord(
            derivation_id=row.derivation_id,
            artifact_sha256=row.artifact_sha256,
            validated_source_revision_id=row.validated_source_revision_id,
            derivation_fingerprint=row.derivation_fingerprint,
            inputs=inputs,
            roots=roots,
            transformation_version=row.transformation_version,
            policy_version=row.policy_version,
            produced_at=row.produced_at,
        )

    # ------------------------------------------------------------------ selections

    def current_move(self, item_id: str) -> SelectionMove | None:
        row = self.session.scalars(
            select(CurrentImageSelectionMove)
            .where(CurrentImageSelectionMove.item_id == item_id)
            .order_by(CurrentImageSelectionMove.sequence.desc())
            .limit(1)
        ).first()
        return None if row is None else _move(row)

    def selection(self, selection_revision_id: str) -> SelectionRecord | None:
        row = self.session.get(ImageSelectionRevision, selection_revision_id)
        if row is None:
            return None
        decisions = tuple(
            SourceDecision(
                ImageRole(d.source_role),
                d.source_ordinal,
                d.source_sha256,
                SourceDecisionKind(d.decision),
                d.derivation_id,
            )
            for d in self.session.scalars(
                select(ImageSelectionSourceDecision)
                .where(ImageSelectionSourceDecision.selection_revision_id == selection_revision_id)
                .order_by(
                    ImageSelectionSourceDecision.source_role.desc(),
                    ImageSelectionSourceDecision.source_ordinal,
                )
            )
        )
        outputs = tuple(
            SelectedImage(
                o.position,
                ImageRole(o.role),
                ImageRole(o.source_role),
                o.source_ordinal,
                ImageAssetKind(o.asset_kind),
                o.sha256,
                o.derivation_id,
            )
            for o in self.session.scalars(
                select(ImageSelectionOutput)
                .where(ImageSelectionOutput.selection_revision_id == selection_revision_id)
                .order_by(ImageSelectionOutput.position)
            )
        )
        return SelectionRecord(
            selection_revision_id=row.selection_revision_id,
            item_id=row.item_id,
            product_group_id=row.product_group_id,
            source_revision_id=row.source_revision_id,
            revision_no=row.revision_no,
            source_decision_fingerprint=row.source_decision_fingerprint,
            selection_fingerprint=row.selection_fingerprint,
            decision_origin=DecisionOrigin(row.decision_origin),
            decided_by=row.decided_by,
            decisions=decisions,
            outputs=outputs,
        )

    def record_selection(
        self,
        *,
        item_id: str,
        product_group_id: str,
        source_revision_id: str,
        source_decision_fingerprint: str,
        selection_fingerprint: str,
        decisions: Sequence[SourceDecision],
        outputs: Sequence[tuple[SelectedOutput, ImageAssetKind, str, str | None]],
        decided_by: str,
        reason: str | None,
        correlation_id: str,
    ) -> SelectionRecord:
        last = self.session.scalar(
            select(ImageSelectionRevision.revision_no)
            .where(ImageSelectionRevision.item_id == item_id)
            .order_by(ImageSelectionRevision.revision_no.desc())
            .limit(1)
        )
        selection_revision_id = str(uuid.uuid4())
        self.session.add(
            ImageSelectionRevision(
                selection_revision_id=selection_revision_id,
                item_id=item_id,
                product_group_id=product_group_id,
                source_revision_id=source_revision_id,
                revision_no=(last or 0) + 1,
                source_decision_fingerprint=source_decision_fingerprint,
                selection_fingerprint=selection_fingerprint,
                decision_origin=DecisionOrigin.OPERATOR.value,
                decision_count=len(decisions),
                output_count=len(outputs),
                decided_by=decided_by,
                reason=reason,
                correlation_id=correlation_id,
                created_at=self._clock.now(),
            )
        )
        self.session.flush()
        for decision in decisions:
            self.session.add(
                ImageSelectionSourceDecision(
                    selection_revision_id=selection_revision_id,
                    source_role=decision.role.value,
                    source_ordinal=decision.ordinal,
                    source_sha256=decision.sha256,
                    decision=decision.decision.value,
                    derivation_id=decision.derivation_id,
                )
            )
            self.session.flush()
        for position, (output, kind, sha256, derivation_id) in enumerate(outputs):
            self.session.add(
                ImageSelectionOutput(
                    selection_revision_id=selection_revision_id,
                    position=position,
                    role=output.role.value,
                    source_role=output.source_role.value,
                    source_ordinal=output.source_ordinal,
                    asset_kind=kind.value,
                    sha256=sha256,
                    derivation_id=derivation_id,
                )
            )
            self.session.flush()
        record = self.selection(selection_revision_id)
        assert record is not None
        return record

    def record_move(
        self, selection: SelectionRecord, *, decided_by: str, correlation_id: str
    ) -> SelectionMove:
        last = self.current_move(selection.item_id)
        row = CurrentImageSelectionMove(
            move_id=str(uuid.uuid4()),
            item_id=selection.item_id,
            sequence=1 if last is None else last.sequence + 1,
            selection_revision_id=selection.selection_revision_id,
            previous_selection_revision_id=None if last is None else last.selection_revision_id,
            reason=(
                SelectionMoveReason.INITIAL if last is None else SelectionMoveReason.RESELECTED
            ).value,
            rule_version=CURRENT_SELECTION_RULE_VERSION,
            decided_by=decided_by,
            correlation_id=correlation_id,
            moved_at=self._clock.now(),
        )
        self.session.add(row)
        self.session.flush()
        return _move(row)

    # ------------------------------------------------------------------ QA

    def qa_by_input(
        self,
        *,
        asset_kind: ImageAssetKind,
        sha256: str,
        derivation_id: str | None,
        validated_source_revision_id: str,
        qa_rule_version: str,
        qa_input_fingerprint: str,
    ) -> QaRecord | None:
        row = self.session.scalars(
            select(ImageQaResult).where(
                ImageQaResult.asset_kind == asset_kind.value,
                ImageQaResult.sha256 == sha256,
                ImageQaResult.derivation_id.is_(None)
                if derivation_id is None
                else ImageQaResult.derivation_id == derivation_id,
                ImageQaResult.validated_source_revision_id == validated_source_revision_id,
                ImageQaResult.qa_rule_version == qa_rule_version,
                ImageQaResult.qa_input_fingerprint == qa_input_fingerprint,
            )
        ).first()
        return None if row is None else _qa(row)

    def qa_for_asset(
        self, *, asset_kind: ImageAssetKind, sha256: str, derivation_id: str | None
    ) -> tuple[QaRecord, ...]:
        """Every verdict ever recorded for this exact binary identity, whatever its input."""
        return tuple(
            _qa(row)
            for row in self.session.scalars(
                select(ImageQaResult).where(
                    ImageQaResult.asset_kind == asset_kind.value,
                    ImageQaResult.sha256 == sha256,
                    ImageQaResult.derivation_id.is_(None)
                    if derivation_id is None
                    else ImageQaResult.derivation_id == derivation_id,
                )
            )
        )

    def record_qa(
        self,
        *,
        asset_kind: ImageAssetKind,
        sha256: str,
        derivation_id: str | None,
        validated_source_revision_id: str,
        qa_rule_version: str,
        qa_input_fingerprint: str,
        verdict: QaVerdict,
        findings: Sequence[str],
        decided_by: str,
        correlation_id: str,
    ) -> QaRecord:
        row = ImageQaResult(
            qa_result_id=str(uuid.uuid4()),
            asset_kind=asset_kind.value,
            sha256=sha256,
            derivation_id=derivation_id,
            validated_source_revision_id=validated_source_revision_id,
            qa_rule_version=qa_rule_version,
            qa_input_fingerprint=qa_input_fingerprint,
            verdict=verdict.value,
            findings_json=json.dumps(list(findings)),
            decided_by=decided_by,
            correlation_id=correlation_id,
            created_at=self._clock.now(),
        )
        self.session.add(row)
        self.session.flush()
        return _qa(row)


def _artifact(row: DerivedImageArtifact) -> StoredArtifact:
    return StoredArtifact(row.artifact_sha256, row.mime_type, row.byte_size, row.width, row.height)


def _move(row: CurrentImageSelectionMove) -> SelectionMove:
    return SelectionMove(
        row.move_id,
        row.item_id,
        row.sequence,
        row.selection_revision_id,
        row.previous_selection_revision_id,
        SelectionMoveReason(row.reason),
    )


def _qa(row: ImageQaResult) -> QaRecord:
    return QaRecord(
        qa_result_id=row.qa_result_id,
        asset_kind=ImageAssetKind(row.asset_kind),
        sha256=row.sha256,
        derivation_id=row.derivation_id,
        validated_source_revision_id=row.validated_source_revision_id,
        qa_rule_version=row.qa_rule_version,
        qa_input_fingerprint=row.qa_input_fingerprint,
        verdict=QaVerdict(row.verdict),
        findings=tuple(json.loads(row.findings_json)),
    )
