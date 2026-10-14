"""The Image Studio translation owner's store (ADR-0041 §3; IT-01, IT-02, IT-06).

``ImageStudioStore`` is the only writer of ``image_ocr_observations``,
``image_translation_candidates``, ``image_edit_revisions`` and ``image_edit_current``. Every write
runs in one unit with one audit event per row it adds, by the injected clock. An audit event names
ids, hashes, versions and counts only: never OCR text, a translation or image bytes (§8).

**Observations** are content-addressed: a reading whose key is already recorded is reused and
nothing is written.

**Candidates** are recorded only for a successful translation, one per translation fingerprint: an
equal fingerprint is reused and nothing is written. An observation and the candidate made from it
are recorded in one unit (``record_translation``), so a failed item leaves neither (IT-02).

**Edit revisions** are numbered per lineage (product group, source SHA-256). A save names the
sequence it was read at (``expected_seq``, ``0`` before the first) and is refused when the lineage
moved since, or when it would change nothing. The current pointer moves to the new revision in the
same unit, and only ever forward. An ``AI_SUGGESTION`` item carries its candidate's text
unchanged; any other text is the operator's (IT-06).
"""

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities.ai.provider import AIExecutionProvenance, RequestedIdentity
from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.capabilities.image_studio.contracts import (
    ObservationKey,
    OcrRegion,
    SentRegion,
    TranslationInputs,
    canonical_json,
    check_language,
    checked_regions,
    digest,
    observation_fingerprint,
    region_from,
    relevant_input,
)
from app.capabilities.image_studio.models import (
    ImageEditCurrent,
    ImageEditRevision,
    ImageOcrObservation,
    ImageTranslationCandidate,
)
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, InputValidationError, NotFoundError
from app.platform.db.database import Database

IMAGE_OBSERVATION_UNKNOWN: Final = "IMAGE_OBSERVATION_UNKNOWN"
IMAGE_CANDIDATE_UNKNOWN: Final = "IMAGE_CANDIDATE_UNKNOWN"
IMAGE_CANDIDATE_INVALID: Final = "IMAGE_CANDIDATE_INVALID"
IMAGE_EDIT_INVALID: Final = "IMAGE_EDIT_INVALID"
IMAGE_EDIT_SUGGESTION_ALTERED: Final = "IMAGE_EDIT_SUGGESTION_ALTERED"
IMAGE_EDIT_MOVED: Final = "IMAGE_EDIT_MOVED"
IMAGE_EDIT_UNCHANGED: Final = "IMAGE_EDIT_UNCHANGED"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_COLOR = re.compile(r"^#[0-9a-f]{6}$")


class ItemOrigin(StrEnum):
    AI_SUGGESTION = "AI_SUGGESTION"
    OPERATOR_CONFIRMED = "OPERATOR_CONFIRMED"


class EraseDecision(StrEnum):
    """§4: ``ERASE`` only where the erase is clean, ``OVERLAY`` a labelled box beside or over the
    text that never repaints the photo, ``NONE`` for a region kept as it is."""

    ERASE = "ERASE"
    OVERLAY = "OVERLAY"
    NONE = "NONE"


class Weight(StrEnum):
    REGULAR = "regular"
    BOLD = "bold"
    BLACK = "black"


class Face(StrEnum):
    SANS = "sans"
    SERIF = "serif"


class ImageStudioConflictError(AppError):
    """A save the current state refuses: the lineage moved since it was read, or nothing changes."""

    error_class = ErrorClass.CONFLICT


@dataclass(frozen=True)
class ObservationRecord:
    observation_id: str
    key: ObservationKey
    regions: tuple[OcrRegion, ...]
    fingerprint: str


@dataclass(frozen=True)
class CandidateDraft:
    """A successful translation of one observation's sent regions, before it is recorded."""

    source_language: str
    sent: tuple[SentRegion, ...]
    inputs: TranslationInputs
    # (region id, Korean text), in the order sent.
    translations: tuple[tuple[str, str], ...]
    provenance: AIExecutionProvenance
    job_id: str | None = None


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    observation_id: str
    source_language: str
    sent_region_ids: tuple[str, ...]
    inputs: TranslationInputs
    fingerprint: str
    translations: tuple[tuple[str, str], ...]
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class RegionStyle:
    """The matched (or operator-overridden) style of one region's Korean text (§5)."""

    weight: Weight
    face: Face
    size_px: int
    color: str

    def canonical(self) -> dict[str, Any]:
        return {
            "weight": self.weight.value,
            "face": self.face.value,
            "size_px": self.size_px,
            "color": self.color,
        }


@dataclass(frozen=True)
class EditItem:
    """One region of an edit. A kept region (``include`` false) has no text, origin or style and is
    never touched."""

    region_id: str
    include: bool
    text: str | None = None
    origin: ItemOrigin | None = None
    erase: EraseDecision = EraseDecision.NONE
    style: RegionStyle | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "include": self.include,
            "text": self.text,
            "origin": None if self.origin is None else self.origin.value,
            "erase": self.erase.value,
            "style": None if self.style is None else self.style.canonical(),
        }


@dataclass(frozen=True)
class EditRevisionRecord:
    revision_id: str
    product_group_id: str
    source_sha256: str
    seq: int
    observation_id: str
    candidate_id: str | None
    items: tuple[EditItem, ...]
    renderer_version: str
    font_hashes: Mapping[str, str]
    content_fingerprint: str
    authored_by: str
    current: bool


def _invalid(problem: str, **details: Any) -> InputValidationError:
    return InputValidationError(
        IMAGE_EDIT_INVALID, "the edit is malformed", details={"problem": problem, **details}
    )


def _checked_item(item: EditItem) -> EditItem:
    if not item.include:
        if (item.text, item.origin, item.style) != (None, None, None) or (
            item.erase is not EraseDecision.NONE
        ):
            raise _invalid("kept_region_carries_an_edit", region_id=item.region_id)
        return item
    if not item.text or not item.text.strip():
        raise _invalid("text_missing", region_id=item.region_id)
    if item.origin is None:
        raise _invalid("origin_missing", region_id=item.region_id)
    if item.erase is EraseDecision.NONE:
        raise _invalid("erase_missing", region_id=item.region_id)
    style = item.style
    if style is None or not 1 <= style.size_px <= 2000 or not _COLOR.fullmatch(style.color):
        raise _invalid("style_invalid", region_id=item.region_id)
    return item


def content_fingerprint(
    source_sha256: str,
    observation: str,
    items: Sequence[EditItem],
    renderer_version: str,
    font_hashes: Mapping[str, str],
) -> str:
    """What a rendering is made of: the image, the regions it reads, every item, the renderer and
    the font files (IT-03)."""
    return digest(
        {
            "source_sha256": source_sha256,
            "observation": observation,
            "items": [item.canonical() for item in items],
            "renderer_version": renderer_version,
            "font_hashes": dict(font_hashes),
        }
    )


class ImageStudioStore:
    """The only production writer of the four Image Studio translation tables."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    # ------------------------------------------------------------------ observations

    def observation(self, key: ObservationKey) -> ObservationRecord | None:
        with self._db.read() as session:
            row = _observation_row(session, key)
            return None if row is None else _observation(row)

    def observation_by_id(self, observation_id: str) -> ObservationRecord:
        with self._db.read() as session:
            row = session.get(ImageOcrObservation, observation_id)
            if row is None:
                raise NotFoundError(IMAGE_OBSERVATION_UNKNOWN, "no OCR observation has that id")
            return _observation(row)

    def record_observation(
        self,
        key: ObservationKey,
        regions: Sequence[OcrRegion],
        *,
        actor: str,
        correlation_id: str,
    ) -> ObservationRecord:
        """Record one reading, or reuse the one already recorded under its key."""
        checked = checked_regions(regions)
        with self._db.write() as session:
            return self._observation_in(session, key, checked, actor, correlation_id)

    # ------------------------------------------------------------------ candidates

    def candidate_by_fingerprint(self, fingerprint: str) -> CandidateRecord | None:
        with self._db.read() as session:
            row = session.scalars(
                select(ImageTranslationCandidate).where(
                    ImageTranslationCandidate.translation_fingerprint == fingerprint
                )
            ).one_or_none()
            return None if row is None else _candidate(row)

    def candidates(self, observation_id: str) -> tuple[CandidateRecord, ...]:
        """Every candidate of one observation, newest first."""
        with self._db.read() as session:
            rows = session.scalars(
                select(ImageTranslationCandidate)
                .where(ImageTranslationCandidate.observation_id == observation_id)
                .order_by(
                    ImageTranslationCandidate.created_at.desc(),
                    ImageTranslationCandidate.candidate_id,
                )
            ).all()
            return tuple(_candidate(row) for row in rows)

    def record_translation(
        self,
        key: ObservationKey,
        regions: Sequence[OcrRegion],
        draft: CandidateDraft,
        *,
        actor: str,
        correlation_id: str,
    ) -> tuple[ObservationRecord, CandidateRecord]:
        """Record an observation (or reuse it) and the candidate made from it, in one unit: either
        both are durable or neither is (IT-02). An equal fingerprint is reused."""
        checked = checked_regions(regions)
        with self._db.write() as session:
            observation = self._observation_in(session, key, checked, actor, correlation_id)
            candidate = self._candidate_in(session, observation, draft, actor, correlation_id)
            return observation, candidate

    # ------------------------------------------------------------------ edit revisions

    def current_edit(self, product_group_id: str, source_sha256: str) -> EditRevisionRecord | None:
        with self._db.read() as session:
            pointer = session.get(ImageEditCurrent, (product_group_id, source_sha256))
            if pointer is None:
                return None
            row = session.get(ImageEditRevision, pointer.revision_id)
            assert row is not None
            return _revision(row, current=True)

    def edit_history(
        self, product_group_id: str, source_sha256: str
    ) -> tuple[EditRevisionRecord, ...]:
        """Every revision of one lineage, newest first."""
        with self._db.read() as session:
            pointer = session.get(ImageEditCurrent, (product_group_id, source_sha256))
            rows = session.scalars(
                select(ImageEditRevision)
                .where(
                    ImageEditRevision.product_group_id == product_group_id,
                    ImageEditRevision.source_sha256 == source_sha256,
                )
                .order_by(ImageEditRevision.seq.desc())
            ).all()
            current = None if pointer is None else pointer.revision_id
            return tuple(_revision(row, current=row.revision_id == current) for row in rows)

    def save_edit(
        self,
        *,
        product_group_id: str,
        source_sha256: str,
        observation_id: str,
        candidate_id: str | None,
        items: Sequence[EditItem],
        renderer_version: str,
        font_hashes: Mapping[str, str],
        expected_seq: int,
        actor: str,
        correlation_id: str,
    ) -> EditRevisionRecord:
        """Append the operator's next edit of one image of one product and move its pointer.

        Only an operator's save makes a revision (IT-06): nothing here is ever called by the
        translation job."""
        checked = tuple(_checked_item(item) for item in items)
        if not renderer_version:
            raise _invalid("renderer_version_missing")
        if not font_hashes or not all(_HEX64.fullmatch(v) for v in font_hashes.values()):
            raise _invalid("font_hashes_invalid")
        with self._db.write() as session:
            observation = session.get(ImageOcrObservation, observation_id)
            if observation is None or observation.source_sha256 != source_sha256:
                raise NotFoundError(
                    IMAGE_OBSERVATION_UNKNOWN, "no OCR observation of that image has that id"
                )
            regions = [region_from(r) for r in json.loads(observation.regions_json)]
            ids = [item.region_id for item in checked]
            if sorted(ids) != sorted(r.region_id for r in regions) or len(set(ids)) != len(ids):
                raise _invalid("items_do_not_cover_the_observation")
            suggested = self._suggestions(session, observation_id, candidate_id)
            for item in checked:
                if item.origin is ItemOrigin.AI_SUGGESTION and (
                    suggested.get(item.region_id) != item.text
                ):
                    raise InputValidationError(
                        IMAGE_EDIT_SUGGESTION_ALTERED,
                        "an AI_SUGGESTION item carries its candidate's text unchanged;"
                        " an edited text is the operator's",
                        details={"region_id": item.region_id},
                    )
            pointer = session.get(ImageEditCurrent, (product_group_id, source_sha256))
            current = (
                None if pointer is None else session.get(ImageEditRevision, pointer.revision_id)
            )
            current_seq = 0 if current is None else current.seq
            if current_seq != expected_seq:
                raise ImageStudioConflictError(
                    IMAGE_EDIT_MOVED,
                    "the image's edit changed since it was read; reload it before saving",
                    details={"current_seq": current_seq},
                )
            fingerprint = content_fingerprint(
                source_sha256,
                observation.observation_fingerprint,
                checked,
                renderer_version,
                font_hashes,
            )
            if current is not None and (
                current.content_fingerprint == fingerprint and current.candidate_id == candidate_id
            ):
                raise ImageStudioConflictError(
                    IMAGE_EDIT_UNCHANGED,
                    "the edit is identical to its current revision",
                    details={"current_seq": current_seq},
                )
            now = self._clock.now()
            row = ImageEditRevision(
                revision_id=str(uuid.uuid4()),
                product_group_id=product_group_id,
                source_sha256=source_sha256,
                seq=current_seq + 1,
                observation_id=observation_id,
                candidate_id=candidate_id,
                items_json=canonical_json([item.canonical() for item in checked]),
                renderer_version=renderer_version,
                font_hashes_json=canonical_json(dict(font_hashes)),
                content_fingerprint=fingerprint,
                authored_by=actor,
                correlation_id=correlation_id,
                authored_at=now,
            )
            session.add(row)
            session.flush()
            if pointer is None:
                session.add(
                    ImageEditCurrent(
                        product_group_id=product_group_id,
                        source_sha256=source_sha256,
                        revision_id=row.revision_id,
                        moved_by=actor,
                        correlation_id=correlation_id,
                        moved_at=now,
                    )
                )
            else:
                pointer.revision_id = row.revision_id
                pointer.moved_by = actor
                pointer.correlation_id = correlation_id
                pointer.moved_at = now
            session.flush()
            included = [item for item in checked if item.include]
            self._event(
                session,
                "IMAGE_EDIT_SAVED",
                f"product:{product_group_id}",
                actor,
                correlation_id,
                before=None if current is None else _audited(current),
                after=_audited(row),
                details={
                    "source_sha256": source_sha256,
                    "regions": len(checked),
                    "included": len(included),
                    "operator_confirmed": sum(
                        1 for item in included if item.origin is ItemOrigin.OPERATOR_CONFIRMED
                    ),
                },
            )
            return _revision(row, current=True)

    # ------------------------------------------------------------------ in a unit

    def _observation_in(
        self,
        session: Session,
        key: ObservationKey,
        regions: tuple[OcrRegion, ...],
        actor: str,
        correlation_id: str,
    ) -> ObservationRecord:
        existing = _observation_row(session, key)
        if existing is not None:
            return _observation(existing)
        row = ImageOcrObservation(
            observation_id=str(uuid.uuid4()),
            source_sha256=key.source_sha256,
            ocr_engine_id=key.engine_id,
            ocr_engine_version=key.engine_version,
            preprocessing_version=key.preprocessing_version,
            regions_json=canonical_json([r.canonical() for r in regions]),
            observation_fingerprint=observation_fingerprint(key, regions),
            correlation_id=correlation_id,
            observed_at=self._clock.now(),
        )
        session.add(row)
        session.flush()
        self._event(
            session,
            "IMAGE_OCR_OBSERVED",
            row.observation_id,
            actor,
            correlation_id,
            after={
                "observation_id": row.observation_id,
                "observation_fingerprint": row.observation_fingerprint,
            },
            details={
                "source_sha256": key.source_sha256,
                "engine_id": key.engine_id,
                "engine_version": key.engine_version,
                "preprocessing_version": key.preprocessing_version,
                "regions": len(regions),
            },
        )
        return _observation(row)

    def _candidate_in(
        self,
        session: Session,
        observation: ObservationRecord,
        draft: CandidateDraft,
        actor: str,
        correlation_id: str,
    ) -> CandidateRecord:
        check_language(draft.source_language)
        expected = relevant_input(observation.fingerprint, draft.source_language, draft.sent)
        sent_ids = tuple(r.region_id for r in draft.sent)
        if (
            not draft.sent
            or draft.inputs.relevant_input != expected
            or tuple(region_id for region_id, _ in draft.translations) != sent_ids
            or not all(text.strip() for _, text in draft.translations)
            or not set(sent_ids) <= {r.region_id for r in observation.regions}
        ):
            raise InputValidationError(
                IMAGE_CANDIDATE_INVALID,
                "a candidate translates exactly the regions it sent, from its own observation",
            )
        fingerprint = draft.inputs.fingerprint
        existing = session.scalars(
            select(ImageTranslationCandidate).where(
                ImageTranslationCandidate.translation_fingerprint == fingerprint
            )
        ).one_or_none()
        if existing is not None:
            return _candidate(existing)
        row = ImageTranslationCandidate(
            candidate_id=str(uuid.uuid4()),
            observation_id=observation.observation_id,
            source_language=draft.source_language,
            sent_region_ids_json=canonical_json(list(sent_ids)),
            relevant_input=draft.inputs.relevant_input,
            prompt_version_json=canonical_json(dict(draft.inputs.prompt_version)),
            policy_version_json=canonical_json(dict(draft.inputs.policy_version)),
            requested_identity_json=canonical_json(asdict(draft.inputs.identity)),
            output_schema_version=draft.inputs.output_schema_version,
            translation_fingerprint=fingerprint,
            translations_json=canonical_json(
                [{"region_id": region_id, "text": text} for region_id, text in draft.translations]
            ),
            # Copied from the outcome, never inferred: an unknown value stays null (ADR-0012 §6).
            provenance_json=canonical_json(asdict(draft.provenance)),
            job_id=draft.job_id,
            correlation_id=correlation_id,
            created_at=self._clock.now(),
        )
        session.add(row)
        session.flush()
        self._event(
            session,
            "IMAGE_TRANSLATION_CANDIDATE_RECORDED",
            row.candidate_id,
            actor,
            correlation_id,
            after={"candidate_id": row.candidate_id, "translation_fingerprint": fingerprint},
            details={
                "observation_id": observation.observation_id,
                "source_language": draft.source_language,
                "regions_sent": len(sent_ids),
                "output_schema_version": draft.inputs.output_schema_version,
                "requested_model": draft.provenance.requested_model,
                "actual_model": draft.provenance.actual_model,
                "billing_mode": draft.provenance.billing_mode.value,
                "job_id": draft.job_id,
            },
        )
        return _candidate(row)

    @staticmethod
    def _suggestions(
        session: Session, observation_id: str, candidate_id: str | None
    ) -> dict[str, str]:
        if candidate_id is None:
            return {}
        row = session.get(ImageTranslationCandidate, candidate_id)
        if row is None or row.observation_id != observation_id:
            raise NotFoundError(
                IMAGE_CANDIDATE_UNKNOWN, "no translation candidate of that observation has that id"
            )
        return {t["region_id"]: t["text"] for t in json.loads(row.translations_json)}

    def _event(
        self,
        session: Session,
        action: str,
        target_ref: str,
        actor: str,
        correlation_id: str,
        *,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.IMAGE_STUDIO_RECORDED,
                action=action,
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=target_ref,
                before=before,
                after=after,
                details=details,
                correlation_id=correlation_id,
            ),
            session=session,
        )


def _observation_row(session: Session, key: ObservationKey) -> ImageOcrObservation | None:
    return session.scalars(
        select(ImageOcrObservation).where(
            ImageOcrObservation.source_sha256 == key.source_sha256,
            ImageOcrObservation.ocr_engine_id == key.engine_id,
            ImageOcrObservation.ocr_engine_version == key.engine_version,
            ImageOcrObservation.preprocessing_version == key.preprocessing_version,
        )
    ).one_or_none()


def _observation(row: ImageOcrObservation) -> ObservationRecord:
    return ObservationRecord(
        observation_id=row.observation_id,
        key=ObservationKey(
            row.source_sha256,
            row.ocr_engine_id,
            row.ocr_engine_version,
            row.preprocessing_version,
        ),
        regions=tuple(region_from(r) for r in json.loads(row.regions_json)),
        fingerprint=row.observation_fingerprint,
    )


def _candidate(row: ImageTranslationCandidate) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=row.candidate_id,
        observation_id=row.observation_id,
        source_language=row.source_language,
        sent_region_ids=tuple(json.loads(row.sent_region_ids_json)),
        inputs=TranslationInputs(
            relevant_input=row.relevant_input,
            prompt_version=json.loads(row.prompt_version_json),
            policy_version=json.loads(row.policy_version_json),
            identity=RequestedIdentity(**json.loads(row.requested_identity_json)),
            output_schema_version=row.output_schema_version,
        ),
        fingerprint=row.translation_fingerprint,
        translations=tuple((t["region_id"], t["text"]) for t in json.loads(row.translations_json)),
        provenance=json.loads(row.provenance_json),
    )


def _item(raw: Mapping[str, Any]) -> EditItem:
    style = raw["style"]
    return EditItem(
        region_id=raw["region_id"],
        include=raw["include"],
        text=raw["text"],
        origin=None if raw["origin"] is None else ItemOrigin(raw["origin"]),
        erase=EraseDecision(raw["erase"]),
        style=None
        if style is None
        else RegionStyle(
            Weight(style["weight"]), Face(style["face"]), style["size_px"], style["color"]
        ),
    )


def _revision(row: ImageEditRevision, *, current: bool) -> EditRevisionRecord:
    return EditRevisionRecord(
        revision_id=row.revision_id,
        product_group_id=row.product_group_id,
        source_sha256=row.source_sha256,
        seq=row.seq,
        observation_id=row.observation_id,
        candidate_id=row.candidate_id,
        items=tuple(_item(raw) for raw in json.loads(row.items_json)),
        renderer_version=row.renderer_version,
        font_hashes=json.loads(row.font_hashes_json),
        content_fingerprint=row.content_fingerprint,
        authored_by=row.authored_by,
        current=current,
    )


def _audited(row: ImageEditRevision) -> dict[str, Any]:
    return {
        "revision": row.revision_id,
        "seq": row.seq,
        "observation_id": row.observation_id,
        "candidate_id": row.candidate_id,
        "renderer_version": row.renderer_version,
        "content_fingerprint": row.content_fingerprint,
    }
