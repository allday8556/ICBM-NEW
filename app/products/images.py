"""M4 image owner: completed derivations, operator selections and exact-binary QA (PR-E).

Issue #80 kickoff 5738166312, ADR-0013 §9, ADR-0010 §9::

    current Item + bound current ProductFactsRevision + immutable SourceAsset(s)
    → optional immutable DerivedImageArtifact chain → explicit operator selection revision
    → exact selected-binary QA → base readiness image input

This owner performs no transformation, OCR, translation, repair, AI call, upload or marketplace
work. It records **completed** results only, and it never writes a source asset or a source image
reference: those stay COLLECT's immutable truth.

**Derivation.** The output SHA is computed from the bytes; a caller's digest is only checked
against it. Every source input must be a CONFIRMED image of the revision the derivation was
validated against; every derived input is the artifact of a complete parent derivation validated
against that same revision. The deterministic identity is the recipe, its canonical execution
provenance and its output: a retry of one completed derivation is one, while another recipe or
another completed execution producing equal bytes is another derivation sharing one artifact.

**Selection** is an operator decision, never a default: nothing selects an image when a Product
materializes. It accounts for every CONFIRMED source image of the Item's current bound revision,
one decision each (use it, replace it with a derived artifact traced to it, or exclude it), and
orders the images the Item uses. A selection revision, its current-pointer move and both audit
events commit together.

**QA** is bound to one exact binary — kind, SHA, derivation where derived — under one validated
source revision, one QA rule version and one input fingerprint. A verdict never transfers to
another SHA, another derivation or another revision, and one exact input has one verdict.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.collect.models import ProductFactsRevision, SourceAsset
from app.core.clock import Clock
from app.core.correlation import get_correlation_id, new_correlation_id
from app.core.errors import AppError, ErrorClass, InputValidationError, NotFoundError
from app.products.image_model import (
    DEFAULT_QA_RULE_VERSION,
    CompletedDerivation,
    ImageAssetKind,
    ImageError,
    QaVerdict,
    SelectedOutput,
    SourceDecision,
    SourceDecisionKind,
    canonical_json,
    derivation_fingerprint,
    digest,
    qa_input_fingerprint,
    source_decision_fingerprint,
    validate_completed,
    validate_findings,
)
from app.products.image_store import (
    DerivationRecord,
    DerivedImageStore,
    ImageUnit,
    QaRecord,
    SelectionMove,
    SelectionRecord,
)
from app.products.model import ReadinessStatus, Reason
from app.products.pricing_service import current_procurement
from app.products.store import ProductFoundationStore, ProductFoundationUnit

logger = logging.getLogger("icbm.products")

# Reason codes: our own, never page content.
IMAGE_SELECTION_MISSING = "IMAGE_SELECTION_MISSING"
IMAGE_SELECTION_STALE = "IMAGE_SELECTION_STALE"
IMAGE_SELECTION_EMPTY = "IMAGE_SELECTION_EMPTY"
IMAGE_DERIVATION_STALE = "IMAGE_DERIVATION_STALE"
IMAGE_QA_MISSING = "IMAGE_QA_MISSING"
IMAGE_QA_STALE = "IMAGE_QA_STALE"
IMAGE_QA_REVIEW_REQUIRED = "IMAGE_QA_REVIEW_REQUIRED"
IMAGE_QA_FAILED = "IMAGE_QA_FAILED"
IMAGE_FINDING_PREFIX = "IMAGE_FINDING_"


class ImageQaConflictError(AppError):
    """A different verdict for an exact QA input that already has one."""

    error_class = ErrorClass.CONFLICT


@dataclass(frozen=True)
class ImageState:
    """The image input to base readiness: every reason and what they were computed against."""

    reasons: tuple[Reason, ...]
    fingerprint: dict[str, object]


def _refusal(code: str, message: str) -> InputValidationError:
    return InputValidationError(code, message)


class ProductImageService:
    def __init__(
        self,
        *,
        store: ProductFoundationStore,
        artifacts: DerivedImageStore,
        audit: AuditLog,
        clock: Clock,
        qa_rule_version: str = DEFAULT_QA_RULE_VERSION,
    ) -> None:
        self._store = store
        self._artifacts = artifacts
        self._audit = audit
        self._clock = clock
        self.qa_rule_version = qa_rule_version

    def _images(self, unit: ProductFoundationUnit) -> ImageUnit:
        return ImageUnit(unit.session, self._clock)

    # ------------------------------------------------------------------ derivations

    def record_completed_derivation(
        self,
        completed: CompletedDerivation,
        *,
        decided_by: str,
        correlation_id: str | None = None,
    ) -> DerivationRecord:
        """Record one completed derivation and its output artifact; idempotent on its identity.

        Everything is validated before a byte is written: a failed or partial operation, an
        unsupported output, an unknown or untraceable input leaves no file and no row.
        """
        correlation = correlation_id or get_correlation_id() or new_correlation_id()
        try:
            validate_completed(completed)
        except ImageError as refused:
            raise _refusal("PRODUCTS_IMAGE_DERIVATION_INVALID", str(refused)) from None
        artifact = self._artifacts.describe(completed.output)
        if completed.operations[-1].output_digest != artifact.sha256:
            raise _refusal(
                "PRODUCTS_IMAGE_OUTPUT_DIGEST_MISMATCH",
                "the declared output digest is not the digest of the output bytes",
            )
        fingerprint = derivation_fingerprint(
            validated_source_revision_id=completed.validated_source_revision_id,
            inputs=completed.inputs,
            transformation_spec=completed.transformation_spec,
            transformation_version=completed.transformation_version,
            policy_version=completed.policy_version,
            operations=completed.operations,
            produced_at=completed.produced_at,
            artifact_sha256=artifact.sha256,
        )
        with self._store.reading() as foundation:
            roots = self._traced_roots(self._images(foundation), completed)
        self._artifacts.write(artifact, completed.output)
        with self._store.transaction() as unit:
            images = self._images(unit)
            existing = images.derivation_by_fingerprint(fingerprint)
            if existing is not None:
                return existing
            images.ensure_artifact(artifact)
            record = images.record_derivation(
                artifact_sha256=artifact.sha256,
                validated_source_revision_id=completed.validated_source_revision_id,
                fingerprint=fingerprint,
                inputs=completed.inputs,
                roots=roots,
                transformation_spec_json=canonical_json(dict(completed.transformation_spec)),
                transformation_version=completed.transformation_version,
                policy_version=completed.policy_version,
                operations_json=canonical_json([op.canonical() for op in completed.operations]),
                produced_at=completed.produced_at,
                decided_by=decided_by,
                correlation_id=correlation,
            )
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.PRODUCT_DERIVED_IMAGE_RECORDED,
                    action="derived_image.record",
                    actor=decided_by,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=record.derivation_id,
                    details={
                        "artifact_sha256": record.artifact_sha256,
                        "validated_source_revision_id": record.validated_source_revision_id,
                        "derivation_fingerprint": record.derivation_fingerprint,
                        "transformation_version": record.transformation_version,
                        "policy_version": record.policy_version,
                        "inputs": [
                            [item.kind.value, item.sha256, item.parent_derivation_id]
                            for item in record.inputs
                        ],
                        "roots": list(record.roots),
                        "capabilities": [op.capability for op in completed.operations],
                    },
                    correlation_id=correlation,
                ),
                session=unit.session,
            )
        return record

    def read_derivation(self, derivation_id: str) -> DerivationRecord:
        with self._store.reading() as unit:
            record = self._images(unit).derivation(derivation_id)
        if record is None:
            raise NotFoundError("PRODUCTS_IMAGE_DERIVATION_UNKNOWN", "no derivation has that id")
        return record

    def _traced_roots(self, images: ImageUnit, completed: CompletedDerivation) -> tuple[str, ...]:
        """The source assets the new lineage traces back to, after checking every input."""
        revision_id = completed.validated_source_revision_id
        if images.session.get(ProductFactsRevision, revision_id) is None:
            raise NotFoundError("PRODUCTS_SOURCE_REVISION_UNKNOWN", "no revision has that id")
        confirmed = {ref.sha256 for ref in images.confirmed_refs(revision_id)}
        roots: set[str] = set()
        for item in completed.inputs:
            if item.kind is ImageAssetKind.SOURCE_ASSET:
                if images.session.get(SourceAsset, item.sha256) is None:
                    raise NotFoundError(
                        "PRODUCTS_IMAGE_SOURCE_UNKNOWN", "no source asset has that checksum"
                    )
                if item.sha256 not in confirmed:
                    raise _refusal(
                        "PRODUCTS_IMAGE_SOURCE_NOT_IN_REVISION",
                        "a source input is not a CONFIRMED image of the validated revision",
                    )
                roots.add(item.sha256)
                continue
            parent = images.derivation(item.parent_derivation_id or "")
            if parent is None:
                raise NotFoundError(
                    "PRODUCTS_IMAGE_PARENT_UNKNOWN", "no parent derivation has that id"
                )
            if parent.artifact_sha256 != item.sha256:
                raise _refusal(
                    "PRODUCTS_IMAGE_PARENT_MISMATCH",
                    "the parent derivation did not produce the named artifact",
                )
            if parent.validated_source_revision_id != revision_id:
                raise _refusal(
                    "PRODUCTS_IMAGE_PARENT_STALE",
                    "the parent derivation was validated against another source revision",
                )
            roots.update(parent.roots)
        return tuple(sorted(roots))

    # ------------------------------------------------------------------ operator selection

    def record_operator_selection(
        self,
        item_id: str,
        *,
        source_revision_id: str,
        decisions: Sequence[SourceDecision],
        outputs: Sequence[SelectedOutput],
        decided_by: str,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[SelectionRecord, SelectionMove]:
        """Record an operator's full image decision for one Item and make it current, atomically."""
        correlation = correlation_id or get_correlation_id() or new_correlation_id()
        if not decided_by:
            raise _refusal("PRODUCTS_IMAGE_SELECTION_ACTOR", "a selection names its operator")
        with self._store.transaction() as unit:
            procurement = current_procurement(unit, item_id)
            binding = procurement.binding
            if (
                binding is None
                or binding.provenance_revision_id != source_revision_id
                or procurement.current_revision_id != source_revision_id
            ):
                raise _refusal(
                    "PRODUCTS_IMAGE_SELECTION_NOT_CURRENT",
                    "a selection reviews the current bound source revision of the Item",
                )
            images = self._images(unit)
            refs = {
                (ref.role, ref.ordinal): ref for ref in images.confirmed_refs(source_revision_id)
            }
            decided: dict[tuple[object, int], SourceDecision] = {}
            for decision in decisions:
                key = (decision.role, decision.ordinal)
                ref = refs.get(key)
                if key in decided:
                    raise _refusal("PRODUCTS_IMAGE_SELECTION_DUPLICATE", "one decision per image")
                if ref is None or ref.sha256 != decision.sha256:
                    raise _refusal(
                        "PRODUCTS_IMAGE_SELECTION_UNKNOWN_SOURCE",
                        "a decision names a CONFIRMED source image of the reviewed revision",
                    )
                self._check_decision(images, decision, source_revision_id)
                decided[key] = decision
            if set(decided) != set(refs):
                # Leaving an image out is not an exclusion: every image is decided explicitly.
                raise _refusal(
                    "PRODUCTS_IMAGE_SELECTION_INCOMPLETE",
                    "every CONFIRMED source image needs an explicit decision",
                )
            chosen = self._resolve_outputs(images, outputs, decided)
            fingerprint = source_decision_fingerprint(list(refs.values()))
            selection = images.record_selection(
                item_id=item_id,
                product_group_id=procurement.item.product_group_id,
                source_revision_id=source_revision_id,
                source_decision_fingerprint=fingerprint,
                selection_fingerprint=digest(
                    {
                        "source_revision_id": source_revision_id,
                        "source_decision_fingerprint": fingerprint,
                        "decisions": [
                            [d.role.value, d.ordinal, d.sha256, d.decision.value, d.derivation_id]
                            for d in decisions
                        ],
                        "outputs": [
                            [o.role.value, kind.value, sha, derivation]
                            for o, kind, sha, derivation in chosen
                        ],
                    }
                ),
                decisions=list(decided.values()),
                outputs=chosen,
                decided_by=decided_by,
                reason=reason,
                correlation_id=correlation,
            )
            move = images.record_move(selection, decided_by=decided_by, correlation_id=correlation)
            self._audit_selection(unit, selection, move, correlation)
        return selection, move

    @staticmethod
    def _check_decision(
        images: ImageUnit, decision: SourceDecision, source_revision_id: str
    ) -> None:
        if decision.decision is not SourceDecisionKind.USE_DERIVED:
            if decision.derivation_id is not None:
                raise _refusal(
                    "PRODUCTS_IMAGE_SELECTION_DECISION_INVALID",
                    "only a replacement names a derivation",
                )
            return
        derivation = images.derivation(decision.derivation_id or "")
        if derivation is None:
            raise NotFoundError("PRODUCTS_IMAGE_DERIVATION_UNKNOWN", "no derivation has that id")
        if derivation.validated_source_revision_id != source_revision_id:
            raise _refusal(
                "PRODUCTS_IMAGE_DERIVATION_STALE",
                "the derivation was validated against another source revision",
            )
        if decision.sha256 not in derivation.roots:
            raise _refusal(
                "PRODUCTS_IMAGE_SELECTION_NOT_DERIVED_FROM_SOURCE",
                "a replacement derives from the source image it replaces",
            )

    @staticmethod
    def _resolve_outputs(
        images: ImageUnit,
        outputs: Sequence[SelectedOutput],
        decided: dict[tuple[object, int], SourceDecision],
    ) -> list[tuple[SelectedOutput, ImageAssetKind, str, str | None]]:
        chosen: list[tuple[SelectedOutput, ImageAssetKind, str, str | None]] = []
        used: set[tuple[object, int]] = set()
        for output in outputs:
            key = (output.source_role, output.source_ordinal)
            decision = decided.get(key)
            if decision is None or decision.decision is SourceDecisionKind.EXCLUDE:
                raise _refusal(
                    "PRODUCTS_IMAGE_SELECTION_OUTPUT_INVALID",
                    "an output comes from a decision that uses or replaces its image",
                )
            if key in used:
                raise _refusal("PRODUCTS_IMAGE_SELECTION_OUTPUT_INVALID", "one output per image")
            used.add(key)
            if decision.decision is SourceDecisionKind.USE_SOURCE:
                chosen.append((output, ImageAssetKind.SOURCE_ASSET, decision.sha256, None))
            else:
                derivation = images.derivation(decision.derivation_id or "")
                assert derivation is not None
                chosen.append(
                    (
                        output,
                        ImageAssetKind.DERIVED_ARTIFACT,
                        derivation.artifact_sha256,
                        derivation.derivation_id,
                    )
                )
        used_decisions = {
            key for key, d in decided.items() if d.decision is not SourceDecisionKind.EXCLUDE
        }
        if used != used_decisions:
            raise _refusal(
                "PRODUCTS_IMAGE_SELECTION_OUTPUT_INVALID",
                "every used or replaced image is placed exactly once",
            )
        return chosen

    def _audit_selection(
        self,
        unit: ProductFoundationUnit,
        selection: SelectionRecord,
        move: SelectionMove,
        correlation: str,
    ) -> None:
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.PRODUCT_IMAGE_SELECTION_RECORDED,
                action="image_selection.record",
                actor=selection.decided_by,
                outcome=AuditOutcome.RECORDED,
                target_ref=selection.selection_revision_id,
                reason_code=selection.decision_origin.value,
                details={
                    "item_id": selection.item_id,
                    "source_revision_id": selection.source_revision_id,
                    "revision_no": selection.revision_no,
                    "source_decision_fingerprint": selection.source_decision_fingerprint,
                    "selection_fingerprint": selection.selection_fingerprint,
                    "decisions": [
                        [d.role.value, d.ordinal, d.sha256, d.decision.value, d.derivation_id]
                        for d in selection.decisions
                    ],
                    "outputs": [
                        [o.position, o.role.value, o.asset_kind.value, o.sha256, o.derivation_id]
                        for o in selection.outputs
                    ],
                },
                correlation_id=correlation,
            ),
            session=unit.session,
        )
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.PRODUCT_CURRENT_IMAGE_SELECTION_MOVED,
                action="current_image_selection.move",
                actor=selection.decided_by,
                outcome=AuditOutcome.RECORDED,
                target_ref=move.item_id,
                reason_code=move.reason.value,
                before=None
                if move.previous_selection_revision_id is None
                else {"selection_revision_id": move.previous_selection_revision_id},
                after={
                    "selection_revision_id": move.selection_revision_id,
                    "sequence": move.sequence,
                },
                details={"move_id": move.move_id},
                correlation_id=correlation,
            ),
            session=unit.session,
        )

    def current_selection(self, item_id: str) -> SelectionRecord | None:
        with self._store.reading() as unit:
            images = self._images(unit)
            move = images.current_move(item_id)
            return None if move is None else images.selection(move.selection_revision_id)

    # ------------------------------------------------------------------ QA

    def record_qa(
        self,
        *,
        asset_kind: ImageAssetKind,
        sha256: str,
        derivation_id: str | None,
        validated_source_revision_id: str,
        verdict: QaVerdict,
        findings: Sequence[str] = (),
        qa_rule_version: str | None = None,
        decided_by: str,
        correlation_id: str | None = None,
    ) -> QaRecord:
        """Record one verdict for one exact binary under one rule and input. The same verdict
        again is the same result; a different one for the same exact input is a conflict."""
        correlation = correlation_id or get_correlation_id() or new_correlation_id()
        try:
            codes = validate_findings(findings)
        except ImageError as refused:
            raise _refusal("PRODUCTS_IMAGE_QA_FINDINGS_INVALID", str(refused)) from None
        if (asset_kind is ImageAssetKind.DERIVED_ARTIFACT) != (derivation_id is not None):
            raise _refusal(
                "PRODUCTS_IMAGE_QA_IDENTITY_INVALID",
                "a derived verdict names its derivation, and only it does",
            )
        rule = qa_rule_version or self.qa_rule_version
        input_fingerprint = qa_input_fingerprint(
            asset_kind=asset_kind,
            sha256=sha256,
            derivation_id=derivation_id,
            validated_source_revision_id=validated_source_revision_id,
            qa_rule_version=rule,
        )
        with self._store.transaction() as unit:
            images = self._images(unit)
            existing = images.qa_by_input(
                asset_kind=asset_kind,
                sha256=sha256,
                derivation_id=derivation_id,
                validated_source_revision_id=validated_source_revision_id,
                qa_rule_version=rule,
                qa_input_fingerprint=input_fingerprint,
            )
            if existing is not None:
                if (existing.verdict, existing.findings) == (verdict, codes):
                    return existing
                raise ImageQaConflictError(
                    "PRODUCTS_IMAGE_QA_CONFLICT",
                    "this exact QA input already has a different verdict",
                )
            self._check_qa_identity(
                images, asset_kind, sha256, derivation_id, validated_source_revision_id
            )
            record = images.record_qa(
                asset_kind=asset_kind,
                sha256=sha256,
                derivation_id=derivation_id,
                validated_source_revision_id=validated_source_revision_id,
                qa_rule_version=rule,
                qa_input_fingerprint=input_fingerprint,
                verdict=verdict,
                findings=codes,
                decided_by=decided_by,
                correlation_id=correlation,
            )
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.PRODUCT_IMAGE_QA_RECORDED,
                    action="image_qa.record",
                    actor=decided_by,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=record.qa_result_id,
                    reason_code=record.verdict.value,
                    details={
                        "asset_kind": record.asset_kind.value,
                        "sha256": record.sha256,
                        "derivation_id": record.derivation_id,
                        "validated_source_revision_id": record.validated_source_revision_id,
                        "qa_rule_version": record.qa_rule_version,
                        "qa_input_fingerprint": record.qa_input_fingerprint,
                        "findings": list(record.findings),
                    },
                    correlation_id=correlation,
                ),
                session=unit.session,
            )
        return record

    @staticmethod
    def _check_qa_identity(
        images: ImageUnit,
        asset_kind: ImageAssetKind,
        sha256: str,
        derivation_id: str | None,
        revision_id: str,
    ) -> None:
        if asset_kind is ImageAssetKind.SOURCE_ASSET:
            if sha256 not in {ref.sha256 for ref in images.confirmed_refs(revision_id)}:
                raise _refusal(
                    "PRODUCTS_IMAGE_QA_SOURCE_NOT_IN_REVISION",
                    "a source verdict names a CONFIRMED image of its validated revision",
                )
            return
        derivation = images.derivation(derivation_id or "")
        if derivation is None:
            raise NotFoundError("PRODUCTS_IMAGE_DERIVATION_UNKNOWN", "no derivation has that id")
        if derivation.artifact_sha256 != sha256 or (
            derivation.validated_source_revision_id != revision_id
        ):
            raise _refusal(
                "PRODUCTS_IMAGE_QA_DERIVED_MISMATCH",
                "a derived verdict names the artifact of its derivation under that revision",
            )

    def qa_for_selected_asset(
        self,
        *,
        asset_kind: ImageAssetKind,
        sha256: str,
        derivation_id: str | None,
        validated_source_revision_id: str,
    ) -> QaRecord | None:
        """The verdict for this exact binary under the current QA rule, if one exists."""
        with self._store.reading() as unit:
            return self._images(unit).qa_by_input(
                asset_kind=asset_kind,
                sha256=sha256,
                derivation_id=derivation_id,
                validated_source_revision_id=validated_source_revision_id,
                qa_rule_version=self.qa_rule_version,
                qa_input_fingerprint=qa_input_fingerprint(
                    asset_kind=asset_kind,
                    sha256=sha256,
                    derivation_id=derivation_id,
                    validated_source_revision_id=validated_source_revision_id,
                    qa_rule_version=self.qa_rule_version,
                ),
            )

    # ------------------------------------------------------------------ readiness input

    def readiness_truth(self, unit: ProductFoundationUnit) -> dict[str, object]:
        """What :meth:`image_state` can read, as a state that never returns to an earlier value:
        every image table's row count, and the QA rule it evaluates under (Gate 2 G2-C)."""
        return {"qa_rule_version": self.qa_rule_version, **self._images(unit).readiness_truth()}

    def image_state(
        self, unit: ProductFoundationUnit, item_id: str, current_revision_id: str | None
    ) -> ImageState:
        """The image part of base readiness for one Item, read in the caller's unit."""
        images = self._images(unit)
        move = images.current_move(item_id)
        if move is None:
            return ImageState(
                (Reason(IMAGE_SELECTION_MISSING, ReadinessStatus.REVIEW_REQUIRED, "images"),),
                {"selection_revision_id": None, "qa_rule_version": self.qa_rule_version},
            )
        selection = images.selection(move.selection_revision_id)
        assert selection is not None
        fingerprint: dict[str, object] = {
            "selection_revision_id": selection.selection_revision_id,
            "selection_fingerprint": selection.selection_fingerprint,
            "selection_source_revision_id": selection.source_revision_id,
            "current_source_revision_id": current_revision_id,
            "qa_rule_version": self.qa_rule_version,
            "outputs": [[o.asset_kind.value, o.sha256, o.derivation_id] for o in selection.outputs],
        }
        current_refs = (
            () if current_revision_id is None else images.confirmed_refs(current_revision_id)
        )
        if (
            current_revision_id is None
            or selection.source_revision_id != current_revision_id
            or selection.source_decision_fingerprint != source_decision_fingerprint(current_refs)
        ):
            # Decided against another source revision: its decisions and outputs no longer
            # account for the current images, and nothing about them is carried forward. A
            # selected derivation validated against another revision is named as well.
            stale = [Reason(IMAGE_SELECTION_STALE, ReadinessStatus.STALE, "images")]
            for output in selection.outputs:
                derivation = (
                    None
                    if output.derivation_id is None
                    else images.derivation(output.derivation_id)
                )
                if derivation is not None and (
                    derivation.validated_source_revision_id != current_revision_id
                ):
                    stale.append(
                        Reason(
                            IMAGE_DERIVATION_STALE,
                            ReadinessStatus.STALE,
                            f"{output.role.value}:{output.position}",
                        )
                    )
            return ImageState(tuple(stale), fingerprint)
        reasons: list[Reason] = []
        if not selection.outputs:
            reasons.append(Reason(IMAGE_SELECTION_EMPTY, ReadinessStatus.REVIEW_REQUIRED, "images"))
        verdicts: list[object] = []
        for output in selection.outputs:
            subject = f"{output.role.value}:{output.position}"
            if output.derivation_id is not None:
                derivation = images.derivation(output.derivation_id)
                if (
                    derivation is None
                    or derivation.validated_source_revision_id != current_revision_id
                ):
                    reasons.append(Reason(IMAGE_DERIVATION_STALE, ReadinessStatus.STALE, subject))
            qa = images.qa_by_input(
                asset_kind=output.asset_kind,
                sha256=output.sha256,
                derivation_id=output.derivation_id,
                validated_source_revision_id=current_revision_id,
                qa_rule_version=self.qa_rule_version,
                qa_input_fingerprint=qa_input_fingerprint(
                    asset_kind=output.asset_kind,
                    sha256=output.sha256,
                    derivation_id=output.derivation_id,
                    validated_source_revision_id=current_revision_id,
                    qa_rule_version=self.qa_rule_version,
                ),
            )
            if qa is None:
                verdicts.append(None)
                earlier = images.qa_for_asset(
                    asset_kind=output.asset_kind,
                    sha256=output.sha256,
                    derivation_id=output.derivation_id,
                )
                reasons.append(
                    Reason(IMAGE_QA_STALE, ReadinessStatus.STALE, subject)
                    if earlier
                    else Reason(IMAGE_QA_MISSING, ReadinessStatus.REVIEW_REQUIRED, subject)
                )
                continue
            verdicts.append([qa.qa_result_id, qa.qa_rule_version, qa.verdict.value])
            if qa.verdict is QaVerdict.PASS:
                continue
            status = (
                ReadinessStatus.BLOCKED
                if qa.verdict is QaVerdict.FAIL
                else ReadinessStatus.REVIEW_REQUIRED
            )
            code = IMAGE_QA_FAILED if qa.verdict is QaVerdict.FAIL else IMAGE_QA_REVIEW_REQUIRED
            reasons.append(Reason(code, status, subject))
            reasons.extend(
                Reason(f"{IMAGE_FINDING_PREFIX}{finding}", status, subject)
                for finding in qa.findings
            )
        fingerprint["qa"] = verdicts
        return ImageState(tuple(reasons), fingerprint)
