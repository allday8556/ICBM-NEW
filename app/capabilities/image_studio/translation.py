"""The ``image.translate`` job: OCR and translation of one product's chosen images (ADR-0041 §3).

**Request.** Only the operator's request starts a run, for one product (D4, IT-10): at most 30 of
its confirmed source images (D5), with their source language. It is refused before anything is
written while the capability reads ``NOT_CONFIGURED``: no OCR engine, no translation task or no AI
provider. The production container binds none of the first two in I1, so nothing is ever read or
sent (provider-zero).

**Job.** ``image.translate`` is not idempotent (a provider call may cost money) and has one
attempt: an interrupted run is never re-run blindly. The batch only coordinates; each image is its
own item:
- the OCR reading is reused when its key is already recorded, so the same image is read once;
- the translation fingerprint is computed before the call, and an equal one is ``REUSED``: no
  provider is called twice for the same inputs;
- an item records its observation and its candidate in one unit, only when both succeed. A failed
  item records nothing durable but one audit event naming its image and its error code (IT-02).

A run with a failed item ends failed, naming how many failed; re-requesting it re-runs only those,
since every other item is reused. Nothing here ever writes a source (IT-01) or an edit revision
(IT-06): the derived image and its selection are later slices'.

**Reads.** A candidate's staleness is derived on read, never stored, by recomputing the
fingerprint from today's inputs (``REASONS``).
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from app.capabilities.ai.composer import ComposedRequest
from app.capabilities.ai.execution import AIExecution
from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.capabilities.image_studio.contracts import (
    MAX_IMAGES_PER_RUN,
    OUTPUT_SCHEMA_VERSION,
    OcrProvider,
    SentRegion,
    TranslationComposer,
    TranslationInputs,
    check_language,
    checked_regions,
    observation_fingerprint,
    relevant_input,
    sent_regions,
    stale_reasons,
)
from app.capabilities.image_studio.store import (
    CandidateDraft,
    CandidateRecord,
    ImageStudioStore,
)
from app.capabilities.jobs.policy import RetryPolicy
from app.capabilities.jobs.registry import JobContext, JobDefinition
from app.capabilities.jobs.service import JobService
from app.platform.core.clock import Clock
from app.platform.core.errors import (
    AppError,
    ErrorClass,
    InputValidationError,
    NotFoundError,
    PolicyBlockedError,
)
from app.stages.connect.contracts import CapabilityReport
from app.stages.connect.state import CapabilityStatus
from app.stages.products.image_store import ImageUnit
from app.stages.products.store import ProductFoundationStore

logger = logging.getLogger("icbm.image_studio")

TRANSLATE_JOB: Final = "image.translate"
# One attempt: an interrupted or failed run is never re-run by the job system (ADR-0041 §3).
TRANSLATE_POLICY: Final = RetryPolicy(max_attempts=1, base_delay_s=60.0, max_delay_s=60.0)
CAPABILITY_KEY: Final = "image_translate"
ACTOR: Final = "system:image-translate"

IMAGE_TRANSLATE_NOT_CONFIGURED: Final = "IMAGE_TRANSLATE_NOT_CONFIGURED"
IMAGE_TRANSLATE_REQUEST_INVALID: Final = "IMAGE_TRANSLATE_REQUEST_INVALID"
IMAGE_TRANSLATE_IMAGE_UNKNOWN: Final = "IMAGE_TRANSLATE_IMAGE_UNKNOWN"
IMAGE_TRANSLATE_PRODUCT_UNKNOWN: Final = "IMAGE_TRANSLATE_PRODUCT_UNKNOWN"
IMAGE_TRANSLATION_OUTPUT_INVALID: Final = "IMAGE_TRANSLATION_OUTPUT_INVALID"
IMAGE_TRANSLATE_ITEMS_FAILED: Final = "IMAGE_TRANSLATE_ITEMS_FAILED"


class SourceImages(Protocol):
    """The verified bytes of a recorded source image (``SourceAssetStore``). Read only."""

    def read(self, sha256: str) -> bytes: ...


class ImageTranslateItemsFailedError(AppError):
    """Some items of a run failed; each recorded nothing durable and is named in its audit event.
    The class is the first failed item's."""

    def __init__(self, error_class: ErrorClass, failed: int, total: int) -> None:
        super().__init__(
            IMAGE_TRANSLATE_ITEMS_FAILED,
            "some images were not translated; each recorded nothing",
            details={"failed": failed, "total": total},
        )
        self.error_class = error_class


@dataclass(frozen=True)
class TranslateRequest:
    product_group_id: str
    images: tuple[str, ...]
    source_language: str
    actor: str
    # Per image, the regions the operator keeps and never sends.
    excluded: Mapping[str, frozenset[str]] | None = None


@dataclass(frozen=True)
class ItemOutcome:
    source_sha256: str
    # RECORDED | REUSED | NO_TEXT | FAILED
    decision: str
    observation_id: str | None = None
    candidate_id: str | None = None
    error_class: ErrorClass | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class CandidateView:
    candidate: CandidateRecord
    # FRESH on an equal fingerprint; STALE otherwise, with the parts that changed.
    status: str
    stale_reasons: tuple[str, ...]


class ImageTranslationService:
    def __init__(
        self,
        *,
        clock: Clock,
        store: ImageStudioStore,
        audit: AuditLog,
        jobs: JobService,
        products: ProductFoundationStore,
        sources: SourceImages,
        ocr: OcrProvider,
        composer: TranslationComposer | None,
        execution: AIExecution,
        output_schema_version: str = OUTPUT_SCHEMA_VERSION,
    ) -> None:
        self._clock = clock
        self._store = store
        self._audit = audit
        self._jobs = jobs
        self._products = products
        self._sources = sources
        self._ocr = ocr
        self._composer = composer
        self._execution = execution
        self._schema = output_schema_version

    def job_definition(self) -> JobDefinition:
        return JobDefinition(
            job_type=TRANSLATE_JOB,
            handler=self._run,
            description="OCR and translate the text inside one product's chosen images",
            # A provider call can cost money, so an interrupted attempt is never re-run blindly.
            idempotent=False,
            retry_policy=TRANSLATE_POLICY,
        )

    def capability(self) -> CapabilityReport:
        """``NOT_CONFIGURED`` while any of the OCR engine, the translation task or the AI
        provider is missing: never a core failure."""
        missing = self._missing()
        if missing:
            return CapabilityReport(
                key=CAPABILITY_KEY,
                status=CapabilityStatus.NOT_CONFIGURED,
                detail="not configured: " + ", ".join(missing),
            )
        return CapabilityReport(
            key=CAPABILITY_KEY, status=CapabilityStatus.READY, detail="image translation ready"
        )

    # -------------------------------------------------------------- request

    def request(self, request: TranslateRequest) -> str:
        """Queue one run for the operator's chosen images; the job id."""
        self._require_configured()
        check_language(request.source_language)
        images = request.images
        if not 1 <= len(images) <= MAX_IMAGES_PER_RUN or len(set(images)) != len(images):
            raise InputValidationError(
                IMAGE_TRANSLATE_REQUEST_INVALID,
                f"a run translates 1 to {MAX_IMAGES_PER_RUN} distinct images",
                details={"images": len(images)},
            )
        excluded = dict(request.excluded or {})
        if not set(excluded) <= set(images):
            raise InputValidationError(
                IMAGE_TRANSLATE_REQUEST_INVALID, "an exclusion names an image outside the run"
            )
        owned = self._product_images(request.product_group_id)
        foreign = [sha for sha in images if sha not in owned]
        if foreign:
            raise NotFoundError(
                IMAGE_TRANSLATE_IMAGE_UNKNOWN,
                "an image is not one of the product's confirmed source images",
                details={"images": foreign},
            )
        job = self._jobs.enqueue(
            TRANSLATE_JOB,
            payload={
                "product_group_id": request.product_group_id,
                "source_language": request.source_language,
                "images": [
                    {"source_sha256": sha, "excluded": sorted(excluded.get(sha, frozenset()))}
                    for sha in images
                ],
                "actor": request.actor,
            },
            target_ref=f"product:{request.product_group_id}",
        )
        return job.job_id

    # -------------------------------------------------------------- the job

    def _run(self, ctx: JobContext) -> None:
        self._require_configured()
        language = check_language(str(ctx.payload["source_language"]))
        items = list(ctx.payload["images"])[:MAX_IMAGES_PER_RUN]
        outcomes = [
            self._item(
                ctx,
                str(item["source_sha256"]),
                language,
                frozenset(str(r) for r in item.get("excluded", ())),
            )
            for item in items
        ]
        failed = [o for o in outcomes if o.decision == "FAILED"]
        logger.info(
            "image.translate.finished",
            extra={
                "job_id": ctx.job_id,
                "items": len(outcomes),
                "failed": len(failed),
                "reused": sum(1 for o in outcomes if o.decision == "REUSED"),
            },
        )
        if failed:
            raise ImageTranslateItemsFailedError(
                failed[0].error_class or ErrorClass.UNKNOWN, len(failed), len(outcomes)
            )

    def _item(
        self, ctx: JobContext, source_sha256: str, language: str, excluded: frozenset[str]
    ) -> ItemOutcome:
        try:
            return self._translate(ctx, source_sha256, language, excluded)
        except AppError as error:
            return self._failed(ctx, source_sha256, error.error_class, error.code)
        except Exception as error:  # an engine or adapter fault fails its item, never the batch
            logger.warning(
                "image.translate.item_fault",
                extra={"job_id": ctx.job_id, "exception": type(error).__name__},
            )
            return self._failed(ctx, source_sha256, ErrorClass.UNKNOWN, "IMAGE_TRANSLATE_FAULT")

    def _translate(
        self, ctx: JobContext, source_sha256: str, language: str, excluded: frozenset[str]
    ) -> ItemOutcome:
        identity = self._ocr.identity()
        if identity is None:
            raise PolicyBlockedError(IMAGE_TRANSLATE_NOT_CONFIGURED, "no OCR engine is installed")
        key = identity.key(source_sha256)
        existing = self._store.observation(key)
        if existing is None:
            regions = checked_regions(self._ocr.read(self._sources.read(source_sha256)))
            observed = observation_fingerprint(key, regions)
        else:
            regions, observed = existing.regions, existing.fingerprint
        sent = sent_regions(regions, excluded)
        if not sent:
            # Nothing to translate: the reading alone is the whole, successful result.
            record = self._store.record_observation(
                key, regions, actor=ACTOR, correlation_id=ctx.correlation_id
            )
            return ItemOutcome(source_sha256, "NO_TEXT", observation_id=record.observation_id)
        composed, inputs = self._inputs(observed, language, sent)
        reused = self._store.candidate_by_fingerprint(inputs.fingerprint)
        if reused is not None:
            return ItemOutcome(
                source_sha256,
                "REUSED",
                observation_id=reused.observation_id,
                candidate_id=reused.candidate_id,
            )
        execution = self._execution.run(composed, correlation_id=ctx.correlation_id)
        outcome = execution.outcome
        if not outcome.ok:
            return self._failed(
                ctx,
                source_sha256,
                outcome.error_class or ErrorClass.UNKNOWN,
                outcome.error_code or "AI_PROVIDER_FAILED",
            )
        translations = _translations(outcome.value, sent)
        observation, candidate = self._store.record_translation(
            key,
            regions,
            CandidateDraft(
                source_language=language,
                sent=sent,
                inputs=inputs,
                translations=translations,
                provenance=outcome.provenance,
                job_id=ctx.job_id,
            ),
            actor=ACTOR,
            correlation_id=ctx.correlation_id,
        )
        return ItemOutcome(
            source_sha256,
            "RECORDED",
            observation_id=observation.observation_id,
            candidate_id=candidate.candidate_id,
        )

    def _failed(
        self, ctx: JobContext, source_sha256: str, error_class: ErrorClass, error_code: str
    ) -> ItemOutcome:
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.IMAGE_STUDIO_RECORDED,
                action="IMAGE_TRANSLATE_ITEM_FAILED",
                actor=ACTOR,
                outcome=AuditOutcome.RECORDED,
                target_ref=str(ctx.payload.get("product_group_id")),
                reason_code=error_code,
                details={
                    "job_id": ctx.job_id,
                    "source_sha256": source_sha256,
                    "error_class": error_class.value,
                    "error_code": error_code,
                    "requested_by": ctx.payload.get("actor"),
                },
                correlation_id=ctx.correlation_id,
            )
        )
        return ItemOutcome(source_sha256, "FAILED", error_class=error_class, error_code=error_code)

    # -------------------------------------------------------------- reads

    def candidate_view(
        self, observation_id: str, source_language: str, excluded: frozenset[str] = frozenset()
    ) -> CandidateView | None:
        """The candidate for today's inputs: ``FRESH`` on an equal fingerprint, else the newest
        candidate of the observation in that language, ``STALE`` with what changed. A stale
        candidate is never shown as a fresh suggestion."""
        check_language(source_language)
        observation = self._store.observation_by_id(observation_id)
        sent = sent_regions(observation.regions, excluded)
        mine = [
            c
            for c in self._store.candidates(observation_id)
            if c.source_language == source_language
        ]
        if not mine:
            return None
        now = self._current_inputs(observation.fingerprint, source_language, sent)
        if now is not None:
            fresh = next((c for c in mine if c.fingerprint == now.fingerprint), None)
            if fresh is not None:
                return CandidateView(fresh, "FRESH", ())
            return CandidateView(mine[0], "STALE", stale_reasons(mine[0].inputs, now))
        # With no composer or no provider today, nothing can be fresh.
        reasons = ("prompt", "policy") if self._composer is None else ()
        if self._execution.identity() is None:
            reasons = (*reasons, "provider")
        return CandidateView(mine[0], "STALE", reasons)

    # -------------------------------------------------------------- inputs

    def _current_inputs(
        self, observed: str, language: str, sent: Sequence[SentRegion]
    ) -> TranslationInputs | None:
        if self._composer is None or self._execution.identity() is None or not sent:
            return None
        return self._inputs(observed, language, sent)[1]

    def _inputs(
        self, observed: str, language: str, sent: Sequence[SentRegion]
    ) -> tuple[ComposedRequest, TranslationInputs]:
        identity = self._execution.identity()
        if self._composer is None or identity is None:
            raise PolicyBlockedError(
                IMAGE_TRANSLATE_NOT_CONFIGURED, "no translation task or AI provider is configured"
            )
        composed = self._composer.compose(language, sent)
        return composed, TranslationInputs(
            relevant_input=relevant_input(observed, language, sent),
            prompt_version=dict(composed.prompt_version),
            policy_version=dict(composed.policy_version),
            identity=identity,
            output_schema_version=self._schema,
        )

    def _missing(self) -> list[str]:
        missing = []
        if self._ocr.identity() is None:
            missing.append("ocr")
        if self._composer is None:
            missing.append("translation task")
        if self._execution.identity() is None:
            missing.append("ai provider")
        return missing

    def _require_configured(self) -> None:
        missing = self._missing()
        if missing:
            raise PolicyBlockedError(
                IMAGE_TRANSLATE_NOT_CONFIGURED,
                "image translation is not configured",
                details={"missing": missing},
            )

    def _product_images(self, product_group_id: str) -> frozenset[str]:
        """The SHA-256 of every confirmed source image of the product's current revisions."""
        with self._products.reading() as unit:
            product = unit.readback(product_group_id)
            if product is None:
                raise NotFoundError(
                    IMAGE_TRANSLATE_PRODUCT_UNKNOWN, "no canonical product has that id"
                )
            images = ImageUnit(unit.session, self._clock)
            return frozenset(
                ref.sha256
                for member in product.members
                if member.current_source_revision_id is not None
                for ref in images.confirmed_refs(member.current_source_revision_id)
            )


def _translations(
    value: Mapping[str, Any] | None, sent: Sequence[SentRegion]
) -> tuple[tuple[str, str], ...]:
    """The answer's one Korean string per sent region, in the order sent, or refused whole."""
    raw = None if value is None else value.get("translations")
    if not isinstance(raw, list) or len(raw) != len(sent):
        raise _output_invalid()
    pairs = []
    for region, entry in zip(sent, raw, strict=True):
        if (
            not isinstance(entry, dict)
            or entry.get("region_id") != region.region_id
            or not isinstance(entry.get("text"), str)
            or not entry["text"].strip()
        ):
            raise _output_invalid()
        pairs.append((region.region_id, entry["text"]))
    return tuple(pairs)


def _output_invalid() -> InputValidationError:
    return InputValidationError(
        IMAGE_TRANSLATION_OUTPUT_INVALID,
        "the translation does not carry one Korean text per sent region, in order",
    )
