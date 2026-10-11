"""The SmartStore upload-fit variants of an Item's selected images (ADR-0037 §2, §3).

REGISTER makes them, because the recipe is marketplace publication work, which the M4 image owner
never performs (ADR-0013 §9; ``app.stages.products.images``). The M4 lineage holds every variant: a
variant is a **completed derivation** of the recipe ``smartstore-upload-fit/v1``
(:mod:`app.stages.register.upload_fit`) handed to the M4 owner, over exactly one
selected binary, recorded through :meth:`ProductImageService.record_completed_derivation` like any
derived image (ADR-0013 §9) — its input SHA-256, recipe and version, one LOCAL operation, and its
output SHA-256, bytes and dimensions on its content-addressed artifact. The source asset is never
written (IC-03).

**Not a selection.** ADR-0013 §9 makes using an edited image an operator decision; for this one
recipe the owner decided it in advance, for every image at or over the threshold (ADR-0037 §2). So
the Item's selection is never changed: it keeps naming the binary the operator or the rule chose,
and REGISTER uploads that binary's variant in its place (``RegistrationPreflightService``). Any
other derivation stays an operator decision.

**Made once, reused.** A variant already recorded for the same input under the same validated
revision is reused, never re-made; equal bytes share one artifact. An image the recipe cannot fit,
or cannot decode, gets nothing — REGISTER then names it ``IMAGE_UPLOAD_FIT_FAILED`` and nothing is
uploaded for it (IC-06). **A failure never fails the save**: whatever goes wrong while one image is
fitted — the recipe's refusal, an unreadable input, the lineage owner's refusal or an unexpected
error — that image gets no variant, the failure is logged and audited
(``REGISTER_UPLOAD_FIT_FAILED``, bounded codes only), and the next image is fitted.

**Its own QA.** Each variant gets the automatic rule QA of its own stored bytes under the rule in
force (``image-qa-auto/v2``, :func:`~app.stages.products.auto_images.file_qa`), bounded by the
upload threshold; it never inherits its source's verdict.
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.platform.core.errors import InputValidationError, NotFoundError
from app.stages.products.auto_images import AUTO_QA_MAX_BYTES, file_qa
from app.stages.products.image_model import (
    AUTO_QA_RULE_VERSION,
    CompletedDerivation,
    DerivationInput,
    ExecutionClass,
    ImageAssetKind,
    OperationOutcome,
    OperationRecord,
    OutputRole,
)
from app.stages.products.image_store import SelectedImage
from app.stages.products.images import DerivedVariant, ProductImageService
from app.stages.register.upload_fit import (
    UPLOAD_FIT_CAPABILITY,
    UPLOAD_FIT_THRESHOLD,
    UPLOAD_FIT_VERSION,
    FitFailure,
    FitRefused,
    fit,
    needs_fit,
    transformation_spec,
)


@dataclass(frozen=True)
class UploadFitState:
    """Whether a selected binary is too large to upload as it is, and its variant if one is
    recorded. ``needed`` without a variant is an image nothing was fitted for."""

    needed: bool
    variant: DerivedVariant | None = None


NOT_NEEDED: Final = UploadFitState(needed=False)


def upload_fit_state(
    images: ProductImageService,
    *,
    asset_kind: ImageAssetKind,
    sha256: str,
    derivation_id: str | None,
    validated_source_revision_id: str,
    role: OutputRole,
) -> UploadFitState:
    """Read only: whether this exact selected binary is at or over the upload-fit threshold, and
    the ``smartstore-upload-fit/v1`` variant the M4 lineage holds for exactly it under the
    selection's validated revision, with the variant's own QA for the ``role`` it serves.
    Nothing is made here."""
    size = images.stored_size(asset_kind, sha256)
    if size is None or not needs_fit(size):
        return NOT_NEEDED
    variant = images.derived_variant(
        transformation_version=UPLOAD_FIT_VERSION,
        asset_kind=asset_kind,
        sha256=sha256,
        derivation_id=derivation_id,
        validated_source_revision_id=validated_source_revision_id,
        role=role,
    )
    if variant is None or needs_fit(variant.byte_size):
        return UploadFitState(needed=True)
    return UploadFitState(needed=True, variant=variant)


logger = logging.getLogger("icbm.register.upload_fit")

# Why an image got no variant, beside the recipe's own FitFailure codes.
SOURCE_UNREADABLE: Final = "SOURCE_UNREADABLE"
LINEAGE_REFUSED: Final = "LINEAGE_REFUSED"
UNEXPECTED_ERROR: Final = "UNEXPECTED_ERROR"


class ArtifactBytes(Protocol):
    """One lineage store: the exact verified bytes of a stored binary, and its stored record."""

    def get(self, sha256: str) -> Any: ...

    def read(self, sha256: str) -> bytes: ...


class FitOutcomeKind(StrEnum):
    NOT_NEEDED = "NOT_NEEDED"  # under the threshold: uploaded byte for byte
    REUSED = "REUSED"  # a variant was already recorded for exactly this input
    RECORDED = "RECORDED"  # a variant was made and recorded now
    FAILED = "FAILED"  # nothing fits, or the image cannot be decoded or read


@dataclass(frozen=True)
class FitOutcome:
    item_id: str
    role: OutputRole
    position: int
    asset_kind: ImageAssetKind
    sha256: str
    outcome: FitOutcomeKind
    failure: FitFailure | None = None
    variant_sha256: str | None = None
    variant_derivation_id: str | None = None
    # A FAILED outcome's bounded code: a FitFailure value, or why nothing reached the recipe.
    reason: str | None = None


class UploadFitVariants:
    """Make, or reuse, the upload-fit variant of each selected image that needs one."""

    def __init__(
        self,
        *,
        images: ProductImageService,
        sources: ArtifactBytes,
        derived: ArtifactBytes,
        clock: Clock,
        audit: AuditLog | None = None,
    ) -> None:
        self._images = images
        self._sources = sources
        self._derived = derived
        self._clock = clock
        self._audit = audit

    def fit_selected(
        self, item_ids: Sequence[str], *, correlation_id: str | None = None
    ) -> tuple[FitOutcome, ...]:
        """Every output of each Item's current selection, fitted where it must be."""
        correlation = correlation_id or get_correlation_id() or new_correlation_id()
        outcomes: list[FitOutcome] = []
        for item_id in dict.fromkeys(item_ids):
            selection = self._images.current_selection(item_id)
            if selection is None:
                continue
            for output in selection.outputs:
                found = self._fit_safely(item_id, selection.source_revision_id, output, correlation)
                if found.outcome is FitOutcomeKind.FAILED:
                    self._report(found, selection.source_revision_id, correlation)
                outcomes.append(found)
        return tuple(outcomes)

    def _fit_safely(
        self, item_id: str, revision_id: str, output: SelectedImage, correlation: str
    ) -> FitOutcome:
        """One image's fit; an unexpected error is that image's failure, never the save's."""
        try:
            return self._fit(item_id, revision_id, output, correlation)
        except Exception:
            logger.exception(
                "register.upload_fit_unexpected_error",
                extra={"item_id": item_id, "sha256": output.sha256},
            )
            return FitOutcome(
                item_id,
                output.role,
                output.position,
                output.asset_kind,
                output.sha256,
                FitOutcomeKind.FAILED,
                reason=UNEXPECTED_ERROR,
            )

    def _report(self, found: FitOutcome, revision_id: str, correlation: str) -> None:
        """Log and audit one image that got no variant. Reporting never fails the save either."""
        details = {
            "item_id": found.item_id,
            "role": found.role.value,
            "position": found.position,
            "asset_kind": found.asset_kind.value,
            "sha256": found.sha256,
            "validated_source_revision_id": revision_id,
            "recipe": UPLOAD_FIT_VERSION,
            "reason": found.reason,
        }
        logger.warning("register.upload_fit_failed", extra=details)
        if self._audit is None:
            return
        try:
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.REGISTER_UPLOAD_FIT_FAILED,
                    action="upload_fit.fit",
                    actor=UPLOAD_FIT_VERSION,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=found.sha256,
                    reason_code=found.reason,
                    details=details,
                    correlation_id=correlation,
                )
            )
        except Exception:
            logger.exception("register.upload_fit_audit_failed", extra=details)

    def _fit(
        self, item_id: str, revision_id: str, output: SelectedImage, correlation: str
    ) -> FitOutcome:
        def outcome(kind: FitOutcomeKind, **more: Any) -> FitOutcome:
            return FitOutcome(
                item_id,
                output.role,
                output.position,
                output.asset_kind,
                output.sha256,
                kind,
                **more,
            )

        state = upload_fit_state(
            self._images,
            asset_kind=output.asset_kind,
            sha256=output.sha256,
            derivation_id=output.derivation_id,
            validated_source_revision_id=revision_id,
            role=output.role,
        )
        if not state.needed:
            return outcome(FitOutcomeKind.NOT_NEEDED)
        if state.variant is not None:
            if state.variant.qa is None:
                # A QA rule version advanced since the variant was made: its own QA again.
                self._record_qa(
                    state.variant.sha256,
                    state.variant.derivation_id,
                    revision_id,
                    correlation,
                    output.role,
                )
            return outcome(
                FitOutcomeKind.REUSED,
                variant_sha256=state.variant.sha256,
                variant_derivation_id=state.variant.derivation_id,
            )
        store = self._sources if output.asset_kind is ImageAssetKind.SOURCE_ASSET else self._derived
        try:
            data = store.read(output.sha256)
        except NotFoundError:
            return outcome(FitOutcomeKind.FAILED, reason=SOURCE_UNREADABLE)
        result = fit(data)
        if isinstance(result, FitRefused):
            return outcome(
                FitOutcomeKind.FAILED, failure=result.failure, reason=result.failure.value
            )
        produced = hashlib.sha256(result.content).hexdigest()
        source = DerivationInput(output.asset_kind, output.sha256, output.derivation_id)
        now = self._clock.now()
        try:
            record = self._images.record_completed_derivation(
                CompletedDerivation(
                    output=result.content,
                    validated_source_revision_id=revision_id,
                    inputs=(source,),
                    transformation_spec=transformation_spec(result.quality, result.colour),
                    transformation_version=UPLOAD_FIT_VERSION,
                    policy_version=None,
                    operations=(
                        OperationRecord(
                            capability=UPLOAD_FIT_CAPABILITY,
                            execution_class=ExecutionClass.LOCAL,
                            outcome=OperationOutcome.COMPLETED,
                            input_digest=output.sha256,
                            output_digest=produced,
                            executed_at=now,
                        ),
                    ),
                    produced_at=now,
                ),
                # The owner's standing decision, recorded as the recipe's own (ADR-0037 §2).
                decided_by=UPLOAD_FIT_VERSION,
                correlation_id=correlation,
            )
        except (InputValidationError, NotFoundError):
            # An input the lineage owner refuses (a derivation of another revision): no variant.
            return outcome(FitOutcomeKind.FAILED, reason=LINEAGE_REFUSED)
        self._record_qa(
            record.artifact_sha256, record.derivation_id, revision_id, correlation, output.role
        )
        return outcome(
            FitOutcomeKind.RECORDED,
            variant_sha256=record.artifact_sha256,
            variant_derivation_id=record.derivation_id,
        )

    def _record_qa(
        self,
        sha256: str,
        derivation_id: str,
        revision_id: str,
        correlation: str,
        role: OutputRole,
    ) -> None:
        """The variant's own automatic QA for the role it serves: under that role's collection cap
        and the upload threshold, whichever is lower, and bound to that role's class."""
        bound = min(AUTO_QA_MAX_BYTES[role], UPLOAD_FIT_THRESHOLD - 1)
        verdict, findings = file_qa(self._derived.get(sha256), bound)
        self._images.record_qa(
            asset_kind=ImageAssetKind.DERIVED_ARTIFACT,
            sha256=sha256,
            derivation_id=derivation_id,
            validated_source_revision_id=revision_id,
            verdict=verdict,
            findings=findings,
            qa_rule_version=AUTO_QA_RULE_VERSION,
            decided_by=AUTO_QA_RULE_VERSION,
            correlation_id=correlation,
            role=role,
        )
