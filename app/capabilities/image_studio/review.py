"""The Studio panel's owner: review, preview, save, apply, QA, select and revert (ADR-0041 §6, I5).

Everything is one product image of one Item: a CONFIRMED source image of the Item's current bound
revision, as the M4 image owner reads it. This owner decides nothing for the operator:

- **Review** (``translation_state``) reads the recorded OCR observation by the engine bound now,
  the candidate the panel may offer (``ImageTranslationService.current_candidate``: a ``STALE``
  one is shown as stale and never offered) and the current edit revision. Every region carries its
  flags, derived on read and never stored: I4's (``LOW_CONFIDENCE``, ``NUMBER_UNIT_DROPPED``,
  ``CONTACT_DETAIL``), ``SCRIPT_MISMATCH`` when its detected script is not the run's source
  language, and ``COMPLEX_BACKGROUND`` when its background-uniformity score does not pass (§4).
  A flagged region, a contact detail and a region on a complex background default to
  keep-original.
- **Preview** (``preview``) renders a draft with I3's renderer and stores nothing (§3, as
  ADR-0033's preview). Its flags are the review's against the draft's own texts, plus what the
  renderer found (``LAYOUT_OVERFLOW``, ``MISSING_GLYPHS``).
- **Save** (``save``) appends the operator's edit revision through ``ImageStudioStore`` (IT-06),
  against the sequence it was read at. Each included region's text is the candidate's suggestion
  (``AI_SUGGESTION``) only when it is that suggestion unchanged, and the operator's
  (``OPERATOR_CONFIRMED``) otherwise; its style is the one matched from the source (§5).
- **「적용」** (``apply``) renders exactly the current revision. Every included flagged region must
  be confirmed one by one in the request (IT-05), the completeness verdict must pass, and only
  then the derived artifact ``image-translate/v1`` is recorded by the M4 image owner
  (``ProductImageService.record_completed_derivation``). Its transformation spec names only the
  edit revision id and its content fingerprint (``MAX_SPEC_BYTES``). The rendering is a pure
  function of the revision (IT-03), so the derivation is dated by the revision it renders:
  applying the same revision again is the same derivation, never a second one. A refusal leaves
  no artifact and the source selected (IT-02).
- **「확인 완료」** is two of the M4 owner's own writes: ``qa`` records the operator's verdict on
  that exact artifact SHA-256 (IT-09; it never inherits the source's QA), and ``select`` moves the
  Item's current selection to ``USE_DERIVED`` for that image, with operator origin, only after a
  ``PASS``. Every other decision and every output of the current selection is kept as it is.
- **「되돌리기」** (``revert``) moves the selection back to ``USE_SOURCE``. Nothing is deleted.

No source is ever written (IT-01). Errors, audit rows and logs name ids, hashes, counts and codes
only: never OCR text or a Korean text (§8).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from PIL import Image

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.capabilities.detail_guidance.renderer import FONT_FILE, FONT_SHA256
from app.capabilities.image_studio import style as matched
from app.capabilities.image_studio.checks import (
    CONTACT_DETAIL,
    LOW_CONFIDENCE,
    NUMBER_UNIT_DROPPED,
    region_flags,
)
from app.capabilities.image_studio.contracts import LANGUAGES, OcrRegion, check_language
from app.capabilities.image_studio.renderer import (
    COMPLEX_BACKGROUND,
    LAYOUT_OVERFLOW,
    MISSING_GLYPHS,
    RENDERER_VERSION,
    RenderRegion,
    RenderResult,
    StyleOverride,
    decode_source,
    render,
)
from app.capabilities.image_studio.store import (
    EditItem,
    EditRevisionRecord,
    EraseDecision,
    Face,
    ImageStudioConflictError,
    ImageStudioStore,
    ItemOrigin,
    ObservationRecord,
    RegionStyle,
    Weight,
)
from app.capabilities.image_studio.translation import (
    CandidateView,
    ImageTranslationService,
    SourceImages,
)
from app.platform.core.clock import Clock
from app.platform.core.errors import InputValidationError, NotFoundError
from app.stages.products.image_model import (
    CompletedDerivation,
    DerivationInput,
    ExecutionClass,
    ImageAssetKind,
    OperationOutcome,
    OperationRecord,
    OutputRole,
    QaVerdict,
    SelectedOutput,
    SourceDecision,
    SourceDecisionKind,
)
from app.stages.products.image_store import (
    DerivationRecord,
    DerivedImageStore,
    QaRecord,
    SelectionRecord,
)
from app.stages.products.images import ProductImageService
from app.stages.products.store import ProductFoundationStore

# The recipe of a translated image in the M4 derived-image chain (ADR-0041 §1, §3).
TRANSFORMATION_VERSION: Final = "image-translate/v1"
# The one operation of that recipe: the deterministic local rendering of an edit revision.
RENDER_CAPABILITY: Final = "IMAGE_TEXT_TRANSLATE_RENDER"
FONT_HASHES: Final = {FONT_FILE.name: FONT_SHA256}

SCRIPT_MISMATCH: Final = "SCRIPT_MISMATCH"
# A region's flags in the order the panel names them. The first five need the region's own
# confirmation (§6, IT-05); the last two fail the completeness verdict and are never applied.
CONFIRM_FLAGS: Final = (
    LOW_CONFIDENCE,
    NUMBER_UNIT_DROPPED,
    CONTACT_DETAIL,
    SCRIPT_MISMATCH,
    COMPLEX_BACKGROUND,
)
FLAG_ORDER: Final = (*CONFIRM_FLAGS, LAYOUT_OVERFLOW, MISSING_GLYPHS)

IMAGE_STUDIO_IMAGE_UNKNOWN: Final = "IMAGE_STUDIO_IMAGE_UNKNOWN"
IMAGE_STUDIO_NOT_OBSERVED: Final = "IMAGE_STUDIO_NOT_OBSERVED"
IMAGE_STUDIO_DRAFT_INVALID: Final = "IMAGE_STUDIO_DRAFT_INVALID"
IMAGE_STUDIO_REVISION_NOT_CURRENT: Final = "IMAGE_STUDIO_REVISION_NOT_CURRENT"
IMAGE_STUDIO_RENDERER_CHANGED: Final = "IMAGE_STUDIO_RENDERER_CHANGED"
IMAGE_STUDIO_NOTHING_INCLUDED: Final = "IMAGE_STUDIO_NOTHING_INCLUDED"
IMAGE_STUDIO_FLAGGED_UNCONFIRMED: Final = "IMAGE_STUDIO_FLAGGED_UNCONFIRMED"
IMAGE_STUDIO_RENDER_INCOMPLETE: Final = "IMAGE_STUDIO_RENDER_INCOMPLETE"
IMAGE_STUDIO_DERIVATION_UNKNOWN: Final = "IMAGE_STUDIO_DERIVATION_UNKNOWN"
IMAGE_STUDIO_QA_REQUIRED: Final = "IMAGE_STUDIO_QA_REQUIRED"
IMAGE_STUDIO_SELECTION_MISSING: Final = "IMAGE_STUDIO_SELECTION_MISSING"
IMAGE_STUDIO_IMAGE_NOT_PLACED: Final = "IMAGE_STUDIO_IMAGE_NOT_PLACED"
IMAGE_STUDIO_NOTHING_TO_REVERT: Final = "IMAGE_STUDIO_NOTHING_TO_REVERT"


@dataclass(frozen=True)
class DraftItem:
    """One region of the operator's draft as the panel sends it. A kept region's text is the
    operator's draft only: it is never rendered or saved."""

    region_id: str
    include: bool
    text: str | None = None
    erase: EraseDecision = EraseDecision.OVERLAY


@dataclass(frozen=True)
class SelectionState:
    """What the Item's current selection does with the image now."""

    decision: str | None
    output_role: str | None
    derivation_id: str | None
    derived_sha256: str | None


@dataclass(frozen=True)
class StudioImage:
    sha256: str
    role: str
    ordinal: int
    selection: SelectionState
    observed: bool


@dataclass(frozen=True)
class StudioImages:
    item_id: str
    product_group_id: str
    source_revision_id: str | None
    images: tuple[StudioImage, ...]


@dataclass(frozen=True)
class RegionView:
    region_id: str
    box: tuple[int, int, int, int]
    text: str
    script: str
    confidence: float
    background_uniformity: float
    # The fresh candidate's Korean text for it, or None (not sent, or no fresh candidate).
    suggestion: str | None
    flags: tuple[str, ...]
    default_include: bool
    default_erase: str


@dataclass(frozen=True)
class CandidateState:
    candidate_id: str
    status: str
    stale_reasons: tuple[str, ...]


@dataclass(frozen=True)
class EditState:
    revision_id: str
    seq: int
    candidate_id: str | None
    items: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class TranslationState:
    item_id: str
    product_group_id: str
    source_revision_id: str
    source_sha256: str
    role: str
    ordinal: int
    source_language: str
    observation_id: str | None
    regions: tuple[RegionView, ...]
    candidate: CandidateState | None
    edit: EditState | None
    expected_seq: int
    selection: SelectionState


@dataclass(frozen=True)
class PreviewResult:
    png: bytes
    complete: bool
    failures: tuple[tuple[str, str], ...]
    flags: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class AppliedTranslation:
    derivation_id: str
    artifact_sha256: str
    revision_id: str
    seq: int
    qa_verdict: str | None


@dataclass(frozen=True)
class _Context:
    item_id: str
    product_group_id: str
    source_revision_id: str
    role: str
    ordinal: int
    sha256: str
    # The Item's current selection, when it reviews the current bound revision.
    selection: SelectionRecord | None


def _box(region: OcrRegion) -> matched.Box:
    left, top, right, bottom = region.box
    return matched.Box(left, top, right - left, bottom - top)


def _hex(colour: tuple[int, ...]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*colour[:3])


def _rgb(colour: str) -> matched.Rgb:
    return (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))


def _ordered(flags: set[str]) -> tuple[str, ...]:
    return tuple(flag for flag in FLAG_ORDER if flag in flags)


def _flags_under(
    region: OcrRegion, languages: Sequence[str], korean: str | None
) -> tuple[str, ...]:
    """A region's review flags under each of several source languages, together."""
    return _ordered(
        {flag for language in languages for flag in review_flags(region, language, korean)}
    )


def review_flags(region: OcrRegion, source_language: str, korean: str | None) -> tuple[str, ...]:
    """The flags of one region against the Korean text proposed for it, if any (§6): I4's, the
    script that is not the run's source language, and a background the erase would harm (§4)."""
    flags = set(region_flags(region.text, region.confidence, korean))
    if region.script != source_language:
        flags.add(SCRIPT_MISMATCH)
    if region.background_uniformity < matched.CLEAN_FROM:
        flags.add(COMPLEX_BACKGROUND)
    return _ordered(flags)


class ImageStudioReviewService:
    def __init__(
        self,
        *,
        clock: Clock,
        audit: AuditLog,
        store: ImageStudioStore,
        translation: ImageTranslationService,
        images: ProductImageService,
        products: ProductFoundationStore,
        sources: SourceImages,
        derived: DerivedImageStore,
    ) -> None:
        self._clock = clock
        self._audit = audit
        self._store = store
        self._translation = translation
        self._images = images
        self._products = products
        self._sources = sources
        self._derived = derived

    # ------------------------------------------------------------------ reads

    def images(self, item_id: str) -> StudioImages:
        """Every CONFIRMED source image of the Item's current bound revision (all roles, D3), what
        the current selection does with it, and whether the bound OCR engine has read it."""
        found = self._images.candidates(item_id)
        group = self._group(item_id)
        selection = _current(found.current_selection, found.source_revision_id)
        return StudioImages(
            item_id=item_id,
            product_group_id=group,
            source_revision_id=found.source_revision_id,
            images=tuple(
                StudioImage(
                    sha256=image.sha256,
                    role=image.role,
                    ordinal=image.ordinal,
                    selection=_selection_state(selection, image.role, image.ordinal),
                    observed=self._translation.observation_of(image.sha256) is not None,
                )
                for image in found.images
            ),
        )

    def translation_state(
        self, item_id: str, sha256: str, source_language: str
    ) -> TranslationState:
        """The panel's read of one image: regions with their flags and defaults, the candidate on
        offer, the current edit and the selection. Nothing is read from the image or sent."""
        check_language(source_language)
        context = self._context(item_id, sha256)
        observation = self._translation.observation_of(sha256)
        edit = self._store.current_edit(context.product_group_id, sha256)
        view = (
            None
            if observation is None
            else self._translation.current_candidate(observation, source_language)
        )
        suggested = _suggestions(view)
        regions = (
            ()
            if observation is None
            else tuple(_region_view(r, source_language, suggested) for r in observation.regions)
        )
        return TranslationState(
            item_id=item_id,
            product_group_id=context.product_group_id,
            source_revision_id=context.source_revision_id,
            source_sha256=sha256,
            role=context.role,
            ordinal=context.ordinal,
            source_language=source_language,
            observation_id=None if observation is None else observation.observation_id,
            regions=regions,
            candidate=None
            if view is None
            else CandidateState(view.candidate.candidate_id, view.status, view.stale_reasons),
            # Only an edit of this very reading is offered back; another engine's is history.
            edit=None
            if edit is None
            or observation is None
            or edit.observation_id != observation.observation_id
            else EditState(
                edit.revision_id,
                edit.seq,
                edit.candidate_id,
                tuple(item.canonical() for item in edit.items),
            ),
            expected_seq=0 if edit is None else edit.seq,
            selection=_selection_state(context.selection, context.role, context.ordinal),
        )

    def derived_image(self, item_id: str, sha256: str, derivation_id: str) -> bytes:
        """The exact bytes of one translated artifact of the image, for the operator's QA."""
        context = self._context(item_id, sha256)
        derivation = self._derivation(context, derivation_id)
        return self._derived.read(derivation.artifact_sha256)

    # ------------------------------------------------------------------ preview and save

    def preview(
        self,
        item_id: str,
        sha256: str,
        *,
        observation_id: str,
        source_language: str,
        items: Sequence[DraftItem],
    ) -> PreviewResult:
        """Render a draft; nothing is stored (§3)."""
        check_language(source_language)
        context = self._context(item_id, sha256)
        observation = self._observation(context, observation_id)
        source = self._sources.read(sha256)
        styles = _matched_styles(source, observation)
        drafts = _covering(observation, items)
        edits = {
            rid: EditItem(rid, True, d.text, ItemOrigin.OPERATOR_CONFIRMED, d.erase, styles[rid])
            if d.include
            else EditItem(rid, False)
            for rid, d in drafts.items()
        }
        for rid, edit in edits.items():
            if edit.include and not (edit.text or "").strip():
                raise InputValidationError(
                    IMAGE_STUDIO_DRAFT_INVALID,
                    "an included region needs its Korean text",
                    details={"region_id": rid},
                )
        result = render(source, _render_regions(observation, edits, styles))
        texts = {rid: d.text for rid, d in drafts.items()}
        flags = {
            region.region_id: _merged(
                review_flags(region, source_language, texts[region.region_id] or None),
                result,
                region.region_id,
                drafts[region.region_id].include,
            )
            for region in observation.regions
        }
        return PreviewResult(
            png=result.png,
            complete=result.completeness.complete,
            failures=tuple((f.region_id, f.code) for f in result.completeness.failures),
            flags=flags,
        )

    def save(
        self,
        item_id: str,
        sha256: str,
        *,
        observation_id: str,
        source_language: str,
        items: Sequence[DraftItem],
        expected_seq: int,
        actor: str,
        correlation_id: str,
    ) -> EditRevisionRecord:
        """Append the operator's edit of the image (IT-06). An included text is the suggestion's
        only when it is the fresh suggestion unchanged; the style is matched from the source."""
        check_language(source_language)
        context = self._context(item_id, sha256)
        observation = self._observation(context, observation_id)
        view = self._translation.current_candidate(observation, source_language)
        suggested = _suggestions(view)
        styles = _matched_styles(self._sources.read(sha256), observation)
        drafts = _covering(observation, items)
        edits = []
        for region in observation.regions:
            draft = drafts[region.region_id]
            if not draft.include:
                edits.append(EditItem(region.region_id, False))
                continue
            origin = (
                ItemOrigin.AI_SUGGESTION
                if draft.text is not None and suggested.get(region.region_id) == draft.text
                else ItemOrigin.OPERATOR_CONFIRMED
            )
            edits.append(
                EditItem(
                    region.region_id,
                    True,
                    draft.text,
                    origin,
                    draft.erase,
                    styles[region.region_id],
                )
            )
        return self._store.save_edit(
            product_group_id=context.product_group_id,
            source_sha256=sha256,
            observation_id=observation.observation_id,
            candidate_id=None
            if view is None or view.status != "FRESH"
            else view.candidate.candidate_id,
            items=edits,
            renderer_version=RENDERER_VERSION,
            font_hashes=FONT_HASHES,
            expected_seq=expected_seq,
            actor=actor,
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------------ 「적용」

    def apply(
        self,
        item_id: str,
        sha256: str,
        *,
        revision_id: str,
        source_language: str,
        confirmed: frozenset[str],
        actor: str,
        correlation_id: str,
    ) -> AppliedTranslation:
        """Render the current revision and record its artifact through the M4 image owner, only
        when every included flagged region is confirmed and the rendering is complete."""
        check_language(source_language)
        context = self._context(item_id, sha256)
        revision = self._store.current_edit(context.product_group_id, sha256)
        if revision is None or revision.revision_id != revision_id:
            raise ImageStudioConflictError(
                IMAGE_STUDIO_REVISION_NOT_CURRENT,
                "only the image's current edit revision is applied; reload it",
                details={"current_seq": 0 if revision is None else revision.seq},
            )
        if revision.renderer_version != RENDERER_VERSION or dict(revision.font_hashes) != dict(
            FONT_HASHES
        ):
            raise ImageStudioConflictError(
                IMAGE_STUDIO_RENDERER_CHANGED,
                "the renderer or its font changed since the edit was saved; save it again",
            )
        observation = self._store.observation_by_id(revision.observation_id)
        items = {item.region_id: item for item in revision.items}
        if not any(item.include for item in revision.items):
            raise InputValidationError(
                IMAGE_STUDIO_NOTHING_INCLUDED, "an applied edit translates at least one region"
            )
        # The caller's language never narrows the checks: SCRIPT_MISMATCH is judged against the
        # language the server recorded for this edit as well (IT-05).
        languages = self._bound_languages(revision, observation, source_language)
        source = self._sources.read(sha256)
        styles = _matched_styles(source, observation)
        result = render(source, _render_regions(observation, items, styles))
        flagged = {
            region.region_id: tuple(
                flag
                for flag in _merged(
                    _flags_under(region, languages, items[region.region_id].text),
                    result,
                    region.region_id,
                    True,
                )
                if flag in CONFIRM_FLAGS
            )
            for region in observation.regions
            if items[region.region_id].include
        }
        unconfirmed = sorted(
            rid for rid, flags in flagged.items() if flags and rid not in confirmed
        )
        if unconfirmed:
            raise InputValidationError(
                IMAGE_STUDIO_FLAGGED_UNCONFIRMED,
                "a flagged region is applied only with its own confirmation",
                details={"regions": unconfirmed},
            )
        if not result.completeness.complete:
            raise InputValidationError(
                IMAGE_STUDIO_RENDER_INCOMPLETE,
                "the rendering is not complete; nothing was recorded",
                details={"failures": [[f.region_id, f.code] for f in result.completeness.failures]},
            )
        derivation = self._images.record_completed_derivation(
            CompletedDerivation(
                output=result.png,
                validated_source_revision_id=context.source_revision_id,
                inputs=(DerivationInput(ImageAssetKind.SOURCE_ASSET, sha256),),
                # Only the revision's id and fingerprint: the revision holds the detail.
                transformation_spec={
                    "edit_revision_id": revision.revision_id,
                    "content_fingerprint": revision.content_fingerprint,
                },
                transformation_version=TRANSFORMATION_VERSION,
                policy_version=None,
                operations=(
                    OperationRecord(
                        capability=RENDER_CAPABILITY,
                        execution_class=ExecutionClass.LOCAL,
                        outcome=OperationOutcome.COMPLETED,
                        input_digest=sha256,
                        output_digest=result.sha256,
                        executed_at=revision.authored_at,
                    ),
                ),
                produced_at=revision.authored_at,
            ),
            decided_by=actor,
            correlation_id=correlation_id,
        )
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.IMAGE_STUDIO_RECORDED,
                action="IMAGE_TRANSLATION_APPLIED",
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=derivation.derivation_id,
                details={
                    "item_id": item_id,
                    "source_sha256": sha256,
                    "edit_revision_id": revision.revision_id,
                    "seq": revision.seq,
                    "artifact_sha256": derivation.artifact_sha256,
                    "regions_included": sum(1 for item in revision.items if item.include),
                    # Region ids and flag codes only: never the text (§8).
                    "confirmed_flagged": [
                        [rid, list(flags)] for rid, flags in flagged.items() if flags
                    ],
                },
                correlation_id=correlation_id,
            )
        )
        qa = self._operator_qa(context, derivation)
        return AppliedTranslation(
            derivation_id=derivation.derivation_id,
            artifact_sha256=derivation.artifact_sha256,
            revision_id=revision.revision_id,
            seq=revision.seq,
            qa_verdict=None if qa is None else qa.verdict.value,
        )

    def _bound_languages(
        self, revision: EditRevisionRecord, observation: ObservationRecord, claimed: str
    ) -> tuple[str, ...]:
        """The languages an applied edit is checked against: the caller's and the recorded one,
        which is the revision's own candidate's, else the observation's newest candidate's. With
        no candidate at all, the observation's most common script; on a tie, every language."""
        candidates = self._store.candidates(observation.observation_id)
        bound = next(
            (c for c in candidates if c.candidate_id == revision.candidate_id),
            candidates[0] if candidates else None,
        )
        if bound is not None:
            recorded: tuple[str, ...] = (bound.source_language,)
        else:
            counts = {
                language: sum(1 for r in observation.regions if r.script == language)
                for language in LANGUAGES
            }
            top = max(counts.values())
            recorded = tuple(language for language in LANGUAGES if counts[language] == top)
        return tuple(language for language in LANGUAGES if language in {claimed, *recorded})

    # ------------------------------------------------------------ 「확인 완료」, 「되돌리기」

    def qa(
        self,
        item_id: str,
        sha256: str,
        *,
        derivation_id: str,
        verdict: QaVerdict,
        actor: str,
        correlation_id: str,
    ) -> QaRecord:
        """The operator's verdict on exactly the translated artifact (IT-09), by the M4 owner."""
        context = self._context(item_id, sha256)
        derivation = self._derivation(context, derivation_id)
        return self._images.record_qa(
            asset_kind=ImageAssetKind.DERIVED_ARTIFACT,
            sha256=derivation.artifact_sha256,
            derivation_id=derivation.derivation_id,
            validated_source_revision_id=context.source_revision_id,
            verdict=verdict,
            decided_by=actor,
            correlation_id=correlation_id,
        )

    def select(
        self, item_id: str, sha256: str, *, derivation_id: str, actor: str, correlation_id: str
    ) -> SelectionRecord:
        """Use the translated artifact in place of the image (``USE_DERIVED``, operator origin),
        only after the operator's ``PASS`` on that exact artifact. Every other decision and every
        output of the current selection stays as it is."""
        context = self._context(item_id, sha256)
        derivation = self._derivation(context, derivation_id)
        qa = self._operator_qa(context, derivation)
        if qa is None or qa.verdict is not QaVerdict.PASS:
            raise ImageStudioConflictError(
                IMAGE_STUDIO_QA_REQUIRED,
                "a translated image is selected only after the operator's PASS on it",
            )
        selection = self._placed(context)
        if all(
            d.decision is SourceDecisionKind.USE_DERIVED and d.derivation_id == derivation_id
            for d in selection.decisions
            if d.sha256 == sha256 and d.decision is not SourceDecisionKind.EXCLUDE
        ):
            return selection
        return self._reselect(
            context,
            selection,
            lambda d: (
                SourceDecision(
                    d.role, d.ordinal, d.sha256, SourceDecisionKind.USE_DERIVED, derivation_id
                )
                if d.sha256 == sha256 and d.decision is not SourceDecisionKind.EXCLUDE
                else d
            ),
            actor,
            correlation_id,
            "ADR-0041 image translation selected",
        )

    def revert(
        self, item_id: str, sha256: str, *, actor: str, correlation_id: str
    ) -> SelectionRecord:
        """Use the source image again (``USE_SOURCE``). Nothing is deleted."""
        context = self._context(item_id, sha256)
        selection = context.selection
        if selection is None or not any(
            d.sha256 == sha256 and d.decision is SourceDecisionKind.USE_DERIVED
            for d in selection.decisions
        ):
            raise ImageStudioConflictError(
                IMAGE_STUDIO_NOTHING_TO_REVERT, "the image is not replaced by a derived image"
            )
        return self._reselect(
            context,
            selection,
            lambda d: (
                SourceDecision(d.role, d.ordinal, d.sha256, SourceDecisionKind.USE_SOURCE)
                if d.sha256 == sha256 and d.decision is SourceDecisionKind.USE_DERIVED
                else d
            ),
            actor,
            correlation_id,
            "ADR-0041 image translation reverted",
        )

    # ------------------------------------------------------------------ internals

    def _group(self, item_id: str) -> str:
        with self._products.reading() as unit:
            detail = unit.item_detail(item_id)
        if detail is None:
            raise NotFoundError("PRODUCTS_ITEM_UNKNOWN", "no Item has that identifier")
        return str(detail.product_group_id)

    def _context(self, item_id: str, sha256: str) -> _Context:
        found = self._images.candidates(item_id)
        image = next((i for i in found.images if i.sha256 == sha256), None)
        if found.source_revision_id is None or image is None:
            raise NotFoundError(
                IMAGE_STUDIO_IMAGE_UNKNOWN,
                "not a CONFIRMED source image of the Item's current bound revision",
            )
        return _Context(
            item_id=item_id,
            product_group_id=self._group(item_id),
            source_revision_id=found.source_revision_id,
            role=image.role,
            ordinal=image.ordinal,
            sha256=sha256,
            selection=_current(found.current_selection, found.source_revision_id),
        )

    def _observation(self, context: _Context, observation_id: str) -> ObservationRecord:
        observation = self._store.observation_by_id(observation_id)
        if observation.key.source_sha256 != context.sha256:
            raise NotFoundError(IMAGE_STUDIO_NOT_OBSERVED, "no reading of that image has that id")
        return observation

    def _derivation(self, context: _Context, derivation_id: str) -> DerivationRecord:
        """A translated artifact of exactly this image under the Item's current bound revision."""
        try:
            derivation = self._images.read_derivation(derivation_id)
        except NotFoundError:
            derivation = None
        if (
            derivation is None
            or derivation.transformation_version != TRANSFORMATION_VERSION
            or derivation.validated_source_revision_id != context.source_revision_id
            or derivation.inputs != (DerivationInput(ImageAssetKind.SOURCE_ASSET, context.sha256),)
        ):
            raise NotFoundError(
                IMAGE_STUDIO_DERIVATION_UNKNOWN,
                "no translated image of this image under the Item's current revision has that id",
            )
        return derivation

    def _operator_qa(self, context: _Context, derivation: DerivationRecord) -> QaRecord | None:
        # A derived artifact is read under the operator rule only; the role names no class there.
        return self._images.qa_for_selected_asset(
            asset_kind=ImageAssetKind.DERIVED_ARTIFACT,
            sha256=derivation.artifact_sha256,
            derivation_id=derivation.derivation_id,
            validated_source_revision_id=context.source_revision_id,
            role=OutputRole.DETAIL,
        )

    @staticmethod
    def _placed(context: _Context) -> SelectionRecord:
        selection = context.selection
        if selection is None:
            raise ImageStudioConflictError(
                IMAGE_STUDIO_SELECTION_MISSING,
                "the Item has no image selection for its current revision; save one first",
            )
        if not any(
            d.sha256 == context.sha256 and d.decision is not SourceDecisionKind.EXCLUDE
            for d in selection.decisions
        ):
            raise ImageStudioConflictError(
                IMAGE_STUDIO_IMAGE_NOT_PLACED,
                "the image is not used by the current selection; choose its role first",
            )
        return selection

    def _reselect(
        self,
        context: _Context,
        selection: SelectionRecord,
        decide: Callable[[SourceDecision], SourceDecision],
        actor: str,
        correlation_id: str,
        reason: str,
    ) -> SelectionRecord:
        record, _move = self._images.record_operator_selection(
            context.item_id,
            source_revision_id=selection.source_revision_id,
            decisions=[decide(d) for d in selection.decisions],
            outputs=[
                SelectedOutput(o.role, o.source_role, o.source_ordinal)
                for o in sorted(selection.outputs, key=lambda o: o.position)
            ],
            decided_by=actor,
            reason=reason,
            correlation_id=correlation_id,
        )
        return record


def _current(selection: SelectionRecord | None, revision_id: str | None) -> SelectionRecord | None:
    if selection is None or revision_id is None or selection.source_revision_id != revision_id:
        return None
    return selection


def _selection_state(selection: SelectionRecord | None, role: str, ordinal: int) -> SelectionState:
    if selection is None:
        return SelectionState(None, None, None, None)
    decision = next(
        (d for d in selection.decisions if d.role.value == role and d.ordinal == ordinal), None
    )
    output = next(
        (
            o
            for o in selection.outputs
            if o.source_role.value == role and o.source_ordinal == ordinal
        ),
        None,
    )
    derived = output is not None and output.asset_kind is ImageAssetKind.DERIVED_ARTIFACT
    return SelectionState(
        decision=None if decision is None else decision.decision.value,
        output_role=None if output is None else output.role.value,
        derivation_id=output.derivation_id if derived and output is not None else None,
        derived_sha256=output.sha256 if derived and output is not None else None,
    )


def _suggestions(view: CandidateView | None) -> dict[str, str]:
    """The suggestions on offer: a fresh candidate's only (§3)."""
    if view is None or view.status != "FRESH":
        return {}
    return dict(view.candidate.translations)


def _region_view(region: OcrRegion, language: str, suggested: Mapping[str, str]) -> RegionView:
    suggestion = suggested.get(region.region_id)
    flags = review_flags(region, language, suggestion)
    return RegionView(
        region_id=region.region_id,
        box=region.box,
        text=region.text,
        script=region.script,
        confidence=region.confidence,
        background_uniformity=region.background_uniformity,
        suggestion=suggestion,
        flags=flags,
        # A flagged region — a contact detail and a complex background included — is kept until
        # the operator confirms it (§4, §6).
        default_include=suggestion is not None and not flags,
        default_erase=(
            EraseDecision.ERASE
            if region.background_uniformity >= matched.CLEAN_FROM
            else EraseDecision.OVERLAY
        ).value,
    )


def _covering(observation: ObservationRecord, items: Sequence[DraftItem]) -> dict[str, DraftItem]:
    """The draft, one item per region of the reading and no other."""
    drafts = {item.region_id: item for item in items}
    ids = [r.region_id for r in observation.regions]
    if len(drafts) != len(items) or sorted(drafts) != sorted(ids):
        raise InputValidationError(
            IMAGE_STUDIO_DRAFT_INVALID, "a draft names every region of the reading once"
        )
    return drafts


def _matched_styles(source: bytes, observation: ObservationRecord) -> dict[str, RegionStyle]:
    """Each region's style as measured from the source text (§5); the face is always sans."""
    image: Image.Image = decode_source(source)
    styles = {}
    for region in observation.regions:
        box = _box(region)
        if box.width <= 0 or box.height <= 0 or not box.inside(image.width, image.height):
            styles[region.region_id] = RegionStyle(Weight.REGULAR, Face.SANS, 1, "#000000")
            continue
        background = matched.read_background(image, box)
        measured = matched.measure_style(image, box, background.colour)
        styles[region.region_id] = RegionStyle(
            Weight(measured.weight.value.lower()),
            Face(measured.face.value.lower()),
            min(2000, max(1, measured.size)),
            _hex(measured.colour),
        )
    return styles


def _render_regions(
    observation: ObservationRecord,
    items: Mapping[str, EditItem],
    styles: Mapping[str, RegionStyle],
) -> list[RenderRegion]:
    """The renderer's regions of an edit. The matched size shrinks to fit; only a size the
    operator chose is exact. A kept region with no area holds nothing to restore."""
    regions = []
    for region in observation.regions:
        box, item = _box(region), items[region.region_id]
        if not item.include:
            if box.width > 0 and box.height > 0:
                regions.append(RenderRegion(region.region_id, box, region.text, "", False, False))
            continue
        assert item.style is not None and item.text is not None
        style = item.style
        regions.append(
            RenderRegion(
                region.region_id,
                box,
                region.text,
                item.text,
                True,
                item.erase is EraseDecision.ERASE,
                StyleOverride(
                    weight=matched.Weight(style.weight.value.upper()),
                    face=matched.Face(style.face.value.upper()),
                    size=None
                    if style.size_px == styles[region.region_id].size_px
                    else style.size_px,
                    colour=_rgb(style.color),
                ),
            )
        )
    return regions


def _merged(
    flags: tuple[str, ...], result: RenderResult, region_id: str, included: bool
) -> tuple[str, ...]:
    """A region's review flags and, when it is drawn, what the renderer found."""
    found = set(flags)
    if included:
        outcome = next(o for o in result.regions if o.region_id == region_id)
        found |= {f for f in outcome.flags if f in FLAG_ORDER}
    return _ordered(found)
