"""The Detail Guidance owner's store (ADR-0033 §2; DG-03, DG-04, DG-08).

``DetailGuidanceStore`` is the only writer of ``detail_guidances``, ``guidance_image_artifacts``
and ``detail_guidance_revisions``. A save validates the operator's plain text and renders it with
the one renderer; then, in one write unit, it creates the identity when it is new, places the PNG
in the guidance image store, records the image artifact when it is new, appends the revision
``seq + 1`` and appends one audit event that names identities, flags and fingerprints but never
the text.

**Identities.** A placement's standing notice is one identity: its newest revision is the standing
notice, so at most one ``STANDING`` revision is current per placement by structure (a partial
unique index refuses a second standing identity). A ``PERIOD`` notice is its own identity; any
number may exist.

**Change.** Nothing is updated or deleted. Turning a notice off, or ending a period early, appends
a revision with ``enabled = False``. A save names the sequence it was read at
(``expected_current_seq``, ``None`` for a notice with no revision yet) and is refused when the
notice moved since, or when it would change nothing.

**Status.** A period notice is ``SCHEDULED`` before its start, ``ACTIVE`` inside the half-open
``[starts_at, ends_at)`` and ``ENDED`` at or after its end or when turned off, judged only by the
injected clock (DG-04).
"""

import hashlib
import json
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.capabilities.detail_guidance.content import GuidanceContent, guidance_content
from app.capabilities.detail_guidance.image_store import (
    GUIDANCE_IMAGE_UNKNOWN,
    GuidanceImageIntegrityError,
    GuidanceImageStore,
    is_sha256,
)
from app.capabilities.detail_guidance.models import (
    DetailGuidance,
    DetailGuidanceRevision,
    GuidanceImageArtifact,
)
from app.capabilities.detail_guidance.renderer import (
    RenderedGuidance,
    Template,
    drawable_codepoints,
    preview,
    render,
)
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, InputValidationError, NotFoundError
from app.platform.db.database import Database

GUIDANCE_PERIOD_INVALID: Final = "GUIDANCE_PERIOD_INVALID"
GUIDANCE_IDENTITY_MISMATCH: Final = "GUIDANCE_IDENTITY_MISMATCH"
GUIDANCE_UNKNOWN: Final = "GUIDANCE_UNKNOWN"
GUIDANCE_CURRENT_MOVED: Final = "GUIDANCE_CURRENT_MOVED"
GUIDANCE_UNCHANGED: Final = "GUIDANCE_UNCHANGED"
GUIDANCE_IMAGE_RECORD_CONFLICT: Final = "GUIDANCE_IMAGE_RECORD_CONFLICT"

# The bundled font's FreeType face is shared by every rendering of this process; one rendering at
# a time keeps it from being used by two request threads at once.
_RENDER_LOCK: Final = threading.Lock()


class Placement(StrEnum):
    TOP = "TOP"
    BOTTOM = "BOTTOM"


class Kind(StrEnum):
    STANDING = "STANDING"
    PERIOD = "PERIOD"


class GuidanceStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


class DetailGuidanceConflictError(AppError):
    """A save the current state refuses: the notice moved since it was read, or nothing changes."""

    error_class = ErrorClass.CONFLICT


@dataclass(frozen=True)
class GuidanceRevisionRecord:
    revision_id: str
    guidance_id: str
    placement: Placement
    kind: Kind
    seq: int
    template: Template
    content: Mapping[str, Any]
    content_fingerprint: str
    enabled: bool
    starts_at: datetime | None
    ends_at: datetime | None
    image_sha256: str
    image_width: int
    image_height: int
    authored_by: str
    authored_at: datetime


@dataclass(frozen=True)
class CurrentNotice:
    record: GuidanceRevisionRecord
    # ``None`` for a standing notice; a period notice's status at the read's instant.
    status: GuidanceStatus | None


@dataclass(frozen=True)
class PlacementNotices:
    placement: Placement
    standing: CurrentNotice | None
    # Every period notice's current revision, ordered by ``(starts_at, guidance_id)``.
    periods: tuple[CurrentNotice, ...]


@dataclass(frozen=True)
class CurrentGuidance:
    now: datetime
    placements: tuple[PlacementNotices, ...]


def canonical_json(content: Mapping[str, Any]) -> str:
    return json.dumps(dict(content), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def status_at(record: GuidanceRevisionRecord, now: datetime) -> GuidanceStatus | None:
    """A period notice's status at ``now``: half-open ``[starts_at, ends_at)`` (DG-04)."""
    if record.kind is Kind.STANDING:
        return None
    assert record.starts_at is not None and record.ends_at is not None
    if not record.enabled or now >= record.ends_at:
        return GuidanceStatus.ENDED
    if now < record.starts_at:
        return GuidanceStatus.SCHEDULED
    return GuidanceStatus.ACTIVE


def parse_content(raw: Mapping[str, Any]) -> GuidanceContent:
    """The operator's plain text, validated against the bundled font (ADR-0033 §1)."""
    return guidance_content(raw, drawable_codepoints())


def render_one(content: GuidanceContent, template: Template) -> RenderedGuidance:
    with _RENDER_LOCK:
        return render(content, template)


def render_all(content: GuidanceContent) -> tuple[RenderedGuidance, ...]:
    with _RENDER_LOCK:
        return preview(content)


def _period(
    kind: Kind, starts_at: datetime | None, ends_at: datetime | None
) -> tuple[datetime | None, datetime | None]:
    if kind is Kind.STANDING:
        if starts_at is not None or ends_at is not None:
            raise InputValidationError(GUIDANCE_PERIOD_INVALID, "a standing notice has no period")
        return None, None
    if starts_at is None or ends_at is None:
        raise InputValidationError(
            GUIDANCE_PERIOD_INVALID, "a period notice needs its start and its end"
        )
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise InputValidationError(
            GUIDANCE_PERIOD_INVALID, "a period's start and end are instants with a UTC offset"
        )
    start, end = starts_at.astimezone(UTC), ends_at.astimezone(UTC)
    if not start < end:
        raise InputValidationError(GUIDANCE_PERIOD_INVALID, "a period's start is before its end")
    return start, end


class DetailGuidanceStore:
    """The only production writer of the three Detail Guidance tables."""

    def __init__(
        self, db: Database, clock: Clock, audit: AuditLog, images: GuidanceImageStore
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._images = images

    @property
    def images(self) -> GuidanceImageStore:
        return self._images

    def now(self) -> datetime:
        """The injected clock's instant, by which every period status is judged (DG-04)."""
        return self._clock.now()

    def save(
        self,
        *,
        placement: Placement,
        kind: Kind,
        guidance_id: str | None,
        template: Template,
        content: Mapping[str, Any],
        enabled: bool,
        starts_at: datetime | None,
        ends_at: datetime | None,
        expected_current_seq: int | None,
        actor: str,
        correlation_id: str,
    ) -> GuidanceRevisionRecord:
        """Append one revision of a notice, rendered and stored with its image, or change nothing.

        ``guidance_id`` ``None`` creates a period notice, or names the placement's standing
        notice (created on its first save)."""
        start, end = _period(kind, starts_at, ends_at)
        parsed = parse_content(content)
        rendered = render_one(parsed, template)
        canonical = parsed.canonical()
        digest = fingerprint(canonical)
        now = self._clock.now()
        with self._db.write() as session:
            identity = self._identity(session, placement, kind, guidance_id)
            current = None if identity is None else _newest(session, identity.guidance_id)
            current_seq = None if current is None else current.seq
            if current_seq != expected_current_seq:
                raise DetailGuidanceConflictError(
                    GUIDANCE_CURRENT_MOVED,
                    "the notice changed since it was read; reload it before saving",
                    details={"current_seq": current_seq},
                )
            if current is not None and (
                current.template == template.value
                and current.content_fingerprint == digest
                and current.enabled == enabled
                and current.starts_at == start
                and current.ends_at == end
            ):
                raise DetailGuidanceConflictError(
                    GUIDANCE_UNCHANGED,
                    "the notice is identical to its current revision",
                    details={"current_seq": current_seq},
                )
            if identity is None:
                identity = DetailGuidance(
                    guidance_id=str(uuid.uuid4()),
                    placement=placement.value,
                    kind=kind.value,
                    created_at=now,
                )
                session.add(identity)
                session.flush()
            # The bytes before the rows that name them, inside the unit: a refused save stores
            # nothing, and a file a rolled-back unit leaves behind is reused by the retry.
            sha256 = self._images.put(rendered.png)
            self._record_artifact(session, rendered, sha256, now)
            row = DetailGuidanceRevision(
                revision_id=str(uuid.uuid4()),
                guidance_id=identity.guidance_id,
                seq=(current_seq or 0) + 1,
                kind=kind.value,
                template=template.value,
                content_json=canonical_json(canonical),
                content_fingerprint=digest,
                enabled=enabled,
                starts_at=start,
                ends_at=end,
                image_sha256=sha256,
                authored_by=actor,
                correlation_id=correlation_id,
                authored_at=now,
            )
            session.add(row)
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.DETAIL_GUIDANCE_REVISED,
                    action="DETAIL_GUIDANCE_CREATED"
                    if current is None
                    else "DETAIL_GUIDANCE_REVISED",
                    actor=actor,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=identity.guidance_id,
                    before=None if current is None else _audited(current),
                    after=_audited(row),
                    details={"placement": placement.value, "kind": kind.value},
                    correlation_id=correlation_id,
                ),
                session=session,
            )
            return _record(row, identity, rendered.width, rendered.height)

    def current(self) -> CurrentGuidance:
        """Each placement's standing notice and every period notice, at their newest revision,
        with each period's status at the injected clock's instant."""
        now = self._clock.now()
        with self._db.read() as session:
            newest = (
                select(
                    DetailGuidanceRevision.guidance_id,
                    func.max(DetailGuidanceRevision.seq).label("seq"),
                )
                .group_by(DetailGuidanceRevision.guidance_id)
                .subquery()
            )
            rows = session.execute(
                select(DetailGuidanceRevision, DetailGuidance, GuidanceImageArtifact)
                .join(
                    newest,
                    (newest.c.guidance_id == DetailGuidanceRevision.guidance_id)
                    & (newest.c.seq == DetailGuidanceRevision.seq),
                )
                .join(
                    DetailGuidance, DetailGuidance.guidance_id == DetailGuidanceRevision.guidance_id
                )
                .join(
                    GuidanceImageArtifact,
                    GuidanceImageArtifact.sha256 == DetailGuidanceRevision.image_sha256,
                )
            ).all()
            records = [_record(r, g, a.width, a.height) for r, g, a in rows]
        placements = []
        for placement in Placement:
            mine = [r for r in records if r.placement is placement]
            standing = next((r for r in mine if r.kind is Kind.STANDING), None)
            periods = sorted(
                (r for r in mine if r.kind is Kind.PERIOD),
                key=lambda r: (r.starts_at, r.guidance_id),
            )
            placements.append(
                PlacementNotices(
                    placement=placement,
                    standing=None if standing is None else CurrentNotice(standing, None),
                    periods=tuple(CurrentNotice(r, status_at(r, now)) for r in periods),
                )
            )
        return CurrentGuidance(now=now, placements=tuple(placements))

    def history(self, guidance_id: str) -> tuple[GuidanceRevisionRecord, ...]:
        """Every revision of one notice, newest first."""
        with self._db.read() as session:
            identity = session.get(DetailGuidance, guidance_id)
            if identity is None:
                raise NotFoundError(GUIDANCE_UNKNOWN, "no detail guidance has that id")
            rows = session.execute(
                select(DetailGuidanceRevision, GuidanceImageArtifact)
                .join(
                    GuidanceImageArtifact,
                    GuidanceImageArtifact.sha256 == DetailGuidanceRevision.image_sha256,
                )
                .where(DetailGuidanceRevision.guidance_id == guidance_id)
                .order_by(DetailGuidanceRevision.seq.desc())
            ).all()
            return tuple(_record(r, identity, a.width, a.height) for r, a in rows)

    def image(self, sha256: str) -> bytes:
        """The bytes of a recorded guidance image, verified; no other file is ever served."""
        if not is_sha256(sha256):
            raise NotFoundError(GUIDANCE_IMAGE_UNKNOWN, "no guidance image has that checksum")
        with self._db.read() as session:
            if session.get(GuidanceImageArtifact, sha256) is None:
                raise NotFoundError(GUIDANCE_IMAGE_UNKNOWN, "no guidance image has that checksum")
        return self._images.get(sha256)

    @staticmethod
    def _identity(
        session: Session, placement: Placement, kind: Kind, guidance_id: str | None
    ) -> DetailGuidance | None:
        if guidance_id is not None:
            identity = session.get(DetailGuidance, guidance_id)
            if identity is None:
                raise NotFoundError(GUIDANCE_UNKNOWN, "no detail guidance has that id")
            if (identity.placement, identity.kind) != (placement.value, kind.value):
                raise InputValidationError(
                    GUIDANCE_IDENTITY_MISMATCH,
                    "a notice keeps its placement and kind",
                    details={"placement": identity.placement, "kind": identity.kind},
                )
            return identity
        if kind is Kind.PERIOD:
            return None
        return session.scalars(
            select(DetailGuidance).where(
                DetailGuidance.placement == placement.value,
                DetailGuidance.kind == Kind.STANDING.value,
            )
        ).one_or_none()

    @staticmethod
    def _record_artifact(
        session: Session, rendered: RenderedGuidance, sha256: str, now: datetime
    ) -> None:
        expected = {
            "width": rendered.width,
            "height": rendered.height,
            "byte_size": len(rendered.png),
            "renderer_version": rendered.renderer_version,
            "template": rendered.template.value,
            "font_sha256": rendered.font_sha256,
        }
        existing = session.get(GuidanceImageArtifact, sha256)
        if existing is None:
            session.add(GuidanceImageArtifact(sha256=sha256, created_at=now, **expected))
            session.flush()
            return
        if {key: getattr(existing, key) for key in expected} != expected:
            raise GuidanceImageIntegrityError(
                GUIDANCE_IMAGE_RECORD_CONFLICT,
                "a recorded guidance image does not describe these bytes",
                details={"sha256": sha256},
            )


def _newest(session: Session, guidance_id: str) -> DetailGuidanceRevision | None:
    return session.scalars(
        select(DetailGuidanceRevision)
        .where(DetailGuidanceRevision.guidance_id == guidance_id)
        .order_by(DetailGuidanceRevision.seq.desc())
        .limit(1)
    ).one_or_none()


def _instant(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _audited(row: DetailGuidanceRevision) -> dict[str, Any]:
    return {
        "revision": row.revision_id,
        "seq": row.seq,
        "template": row.template,
        "content_fingerprint": row.content_fingerprint,
        "enabled": row.enabled,
        "starts_at": _instant(row.starts_at),
        "ends_at": _instant(row.ends_at),
        "image_sha256": row.image_sha256,
    }


def _record(
    row: DetailGuidanceRevision, identity: DetailGuidance, width: int, height: int
) -> GuidanceRevisionRecord:
    return GuidanceRevisionRecord(
        revision_id=row.revision_id,
        guidance_id=row.guidance_id,
        placement=Placement(identity.placement),
        kind=Kind(row.kind),
        seq=row.seq,
        template=Template(row.template),
        content=json.loads(row.content_json),
        content_fingerprint=row.content_fingerprint,
        enabled=bool(row.enabled),
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        image_sha256=row.image_sha256,
        image_width=width,
        image_height=height,
        authored_by=row.authored_by,
        authored_at=row.authored_at,
    )
