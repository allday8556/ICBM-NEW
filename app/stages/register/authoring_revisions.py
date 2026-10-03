"""The durable authoring-revision owner (ADR-0014 §27.1; Issue #89 resolution ``5907626428``).

One append-only table, ``registration_authoring_revisions`` (migration 0032), holds two kinds of
server-owned authoring profile revision:

- ``CATEGORY_MAPPING`` — the category-authoring profile of one ``marketplace_key ×
  taxonomy_revision``. Its v1 content means **operator-confirmed category selection only**: no
  mapping table, no AI ranking, no guessed or provider-derived category (ADR-0014 §4, §18). It is
  what ``CategorySelection.mapping_revision`` names, and it replaces neither the taxonomy
  revision, the category nor the reviewed ``CategoryMetadata`` owner.
- ``DETAIL_COMPOSITION`` — the detail-composition profile of one ``marketplace_key``. Its v1
  content is **BODY-only** (ADR-0014 §19). Its v2 content (B-DETAIL, §19 and §27.1 amendment notes)
  orders ``DETAIL_IMAGES`` → ``BODY`` with a ``PLAIN_TEXT`` body under a pinned renderer version;
  it still holds no product content — a unit's detail images and body are the product-specific
  plan (:mod:`app.stages.register.detail`). It is what ``DetailComposition.composition_revision``
  names, and the server now appends v2.

A revision is a real row, never a label: a server-created identity, a strictly typed canonical
content document and the SHA-256 fingerprint of it. No client authors one, and none holds Product,
price, readiness, Snapshot, Intent, provider or LIVE truth.

**The current revision of a scope is its highest ``seq``.** No pointer exists to move: changing a
profile — a rollback included — appends the next revision with the wanted content, and history is
never rewritten, deleted or backfilled.

**Initialization.** The first revisions of a scope are created by the server when a target-policy
revision of that scope is appended (``stamp``), inside that same unit of work: a refused save
creates nothing, and an existing current revision with the same content is reused, so a repeated
save, startup or migration never creates a duplicate and an identity is stable across restarts.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StrictStr
from sqlalchemy import CheckConstraint, Index, Integer, String, Text, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.db.base import Base
from app.platform.db.database import Database
from app.platform.db.types import UTCDateTime
from app.stages.register import sanitize
from app.stages.register.detail import (
    BODY_FORMAT_PLAIN_TEXT,
    DETAIL_RENDERER_VERSION,
    SECTION_BODY,
    SECTION_DETAIL_IMAGES,
    DetailProfile,
)
from app.stages.register.model import canonical_json, sanitized_digest

CATEGORY_MAPPING_CONTENT_VERSION: Final = "registration-category-mapping/v1"
DETAIL_COMPOSITION_CONTENT_VERSION_V1: Final = "registration-detail-composition/v1"
DETAIL_COMPOSITION_CONTENT_VERSION: Final = "registration-detail-composition/v2"

# The server is the only author of a revision (resolution 5907626428 D1).
SERVER_ACTOR: Final = "system:register-authoring"


class AuthoringRevisionKind(StrEnum):
    CATEGORY_MAPPING = "CATEGORY_MAPPING"
    DETAIL_COMPOSITION = "DETAIL_COMPOSITION"


# ------------------------------------------------------------------ the canonical content


class _Content(BaseModel):
    """Strictly typed and versioned: an unknown member, another version or a widened value is
    refused, so a v1 revision can never carry semantics v1 does not define."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CategoryMappingContentV1(_Content):
    content_version: Literal["registration-category-mapping/v1"]
    kind: Literal["CATEGORY_MAPPING"]
    marketplace_key: StrictStr
    taxonomy_revision: StrictStr
    # The only way a category is chosen under this profile (ADR-0014 §4).
    selection: Literal["OPERATOR_CONFIRMED"]
    # No mapping table, AI ranking or provider-derived mapping exists in v1 (ADR-0014 §18).
    automatic_mapping: Literal[False]


class DetailCompositionContentV1(_Content):
    content_version: Literal["registration-detail-composition/v1"]
    kind: Literal["DETAIL_COMPOSITION"]
    marketplace_key: StrictStr
    # BODY-only (ADR-0014 §19): no Detail Guidance section exists in v1.
    sections: tuple[Literal["BODY"]]
    guidance: Literal[False]


class DetailCompositionContentV2(_Content):
    """B-DETAIL (ADR-0014 §19, §27.1 amendment notes): the section vocabulary and order, the body
    format and the renderer version — never an image, a body or a URL. The reserved sections
    (guidance, video, option table) stay unrepresentable."""

    content_version: Literal["registration-detail-composition/v2"]
    kind: Literal["DETAIL_COMPOSITION"]
    marketplace_key: StrictStr
    sections: tuple[Literal["DETAIL_IMAGES"], Literal["BODY"]]
    body_format: Literal["PLAIN_TEXT"]
    renderer: Literal["detail-renderer/v1"]
    guidance: Literal[False]


def category_mapping_content(marketplace_key: str, taxonomy_revision: str) -> dict[str, Any]:
    """The v1 category-authoring profile of one marketplace taxonomy."""
    return {
        "content_version": CATEGORY_MAPPING_CONTENT_VERSION,
        "kind": AuthoringRevisionKind.CATEGORY_MAPPING.value,
        "marketplace_key": marketplace_key,
        "taxonomy_revision": taxonomy_revision,
        "selection": "OPERATOR_CONFIRMED",
        "automatic_mapping": False,
    }


def detail_composition_content(marketplace_key: str) -> dict[str, Any]:
    """The current (v2) detail-composition profile of one marketplace (B-DETAIL)."""
    return {
        "content_version": DETAIL_COMPOSITION_CONTENT_VERSION,
        "kind": AuthoringRevisionKind.DETAIL_COMPOSITION.value,
        "marketplace_key": marketplace_key,
        "sections": [SECTION_DETAIL_IMAGES, SECTION_BODY],
        "body_format": BODY_FORMAT_PLAIN_TEXT,
        "renderer": DETAIL_RENDERER_VERSION,
        "guidance": False,
    }


# Every content version a revision of each kind may hold. An earlier version stays readable: a
# revision is never rewritten.
_MODELS: Final[Mapping[AuthoringRevisionKind, Mapping[str, type[_Content]]]] = {
    AuthoringRevisionKind.CATEGORY_MAPPING: {
        CATEGORY_MAPPING_CONTENT_VERSION: CategoryMappingContentV1,
    },
    AuthoringRevisionKind.DETAIL_COMPOSITION: {
        DETAIL_COMPOSITION_CONTENT_VERSION_V1: DetailCompositionContentV1,
        DETAIL_COMPOSITION_CONTENT_VERSION: DetailCompositionContentV2,
    },
}


def detail_profile_of(revision_id: str, content: Mapping[str, Any]) -> DetailProfile:
    """The profile one stored ``DETAIL_COMPOSITION`` revision holds, read strictly."""
    document = canonical_content(
        AuthoringRevisionKind.DETAIL_COMPOSITION,
        str(content.get("marketplace_key")),
        None,
        content,
    )
    return DetailProfile(
        revision_id=revision_id,
        content_version=document["content_version"],
        sections=tuple(document["sections"]),
        body_format=document.get("body_format"),
        renderer=document.get("renderer"),
    )


class AuthoringRevisionError(ValueError):
    """The content is not a canonical profile of its kind and scope. Nothing is written."""


def canonical_content(
    kind: AuthoringRevisionKind,
    marketplace_key: str,
    taxonomy_revision: str | None,
    content: Mapping[str, Any],
) -> dict[str, Any]:
    """The sanitized canonical content of one revision, or a refusal of the whole revision."""
    if (kind is AuthoringRevisionKind.CATEGORY_MAPPING) != (taxonomy_revision is not None):
        raise AuthoringRevisionError(
            "a category mapping is scoped to a taxonomy revision; a detail composition is not"
        )
    labels = (
        [marketplace_key] if taxonomy_revision is None else [marketplace_key, taxonomy_revision]
    )
    if not all(sanitize.safe_label(label) for label in labels):
        raise AuthoringRevisionError("a scope is named by plain labels")
    model = _MODELS[kind].get(str(content.get("content_version")))
    if model is None:
        raise AuthoringRevisionError(f"not a canonical {kind.value} profile version")
    try:
        document = model.model_validate(dict(content)).model_dump(mode="json")
    except ValueError as refused:
        raise AuthoringRevisionError(f"not a canonical {kind.value} profile") from refused
    if (
        document["marketplace_key"] != marketplace_key
        or document.get("taxonomy_revision") != taxonomy_revision
    ):
        raise AuthoringRevisionError("the content names another scope than its revision")
    try:
        sanitize.require_clean(document, "authoring_revision")
    except sanitize.PayloadSanitationError as refused:
        raise AuthoringRevisionError("an authoring profile holds no URL or secret") from refused
    return document


# ------------------------------------------------------------------ persistence


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class RegistrationAuthoringRevision(Base):
    __tablename__ = "registration_authoring_revisions"
    __table_args__ = (
        CheckConstraint("kind IN ('CATEGORY_MAPPING', 'DETAIL_COMPOSITION')", name="kind_valid"),
        CheckConstraint("marketplace_key <> ''", name="marketplace_key_present"),
        CheckConstraint(
            "(kind = 'CATEGORY_MAPPING') = (taxonomy_revision IS NOT NULL)"
            " AND (taxonomy_revision IS NULL OR taxonomy_revision <> '')",
            name="taxonomy_scope",
        ),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            name="content_is_object",
        ),
        CheckConstraint(
            "json_extract(content_json, '$.kind') IS kind"
            " AND json_extract(content_json, '$.marketplace_key') IS marketplace_key"
            " AND json_extract(content_json, '$.taxonomy_revision') IS taxonomy_revision",
            name="content_names_its_scope",
        ),
        CheckConstraint(_hex64("content_fingerprint"), name="content_fingerprint_hex"),
        CheckConstraint("created_by <> ''", name="created_by_present"),
        # SQLite treats NULLs as distinct in a unique index, so each kind has its own.
        Index(
            "ux_registration_authoring_revisions_category_mapping",
            "marketplace_key",
            "taxonomy_revision",
            "seq",
            unique=True,
            sqlite_where=text("kind = 'CATEGORY_MAPPING'"),
        ),
        Index(
            "ux_registration_authoring_revisions_detail_composition",
            "marketplace_key",
            "seq",
            unique=True,
            sqlite_where=text("kind = 'DETAIL_COMPOSITION'"),
        ),
    )

    # The server-created identity: what a target policy, a preparation and a Snapshot name.
    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    marketplace_key: Mapped[str] = mapped_column(String(40))
    taxonomy_revision: Mapped[str | None] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[str] = mapped_column(Text)
    # SHA-256 of the canonical sanitized content document.
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_by: Mapped[str] = mapped_column(String(64))


@dataclass(frozen=True)
class AuthoringRevisionRecord:
    revision_id: str
    kind: AuthoringRevisionKind
    marketplace_key: str
    taxonomy_revision: str | None
    seq: int
    content: Mapping[str, Any]
    content_fingerprint: str
    created_at: datetime
    created_by: str


def _record(row: RegistrationAuthoringRevision) -> AuthoringRevisionRecord:
    return AuthoringRevisionRecord(
        revision_id=row.revision_id,
        kind=AuthoringRevisionKind(row.kind),
        marketplace_key=row.marketplace_key,
        taxonomy_revision=row.taxonomy_revision,
        seq=row.seq,
        content=json.loads(row.content_json),
        content_fingerprint=row.content_fingerprint,
        created_at=row.created_at,
        created_by=row.created_by,
    )


def _current_row(
    session: Session,
    kind: AuthoringRevisionKind,
    marketplace_key: str,
    taxonomy_revision: str | None,
) -> RegistrationAuthoringRevision | None:
    scope = (
        RegistrationAuthoringRevision.taxonomy_revision.is_(None)
        if taxonomy_revision is None
        else RegistrationAuthoringRevision.taxonomy_revision == taxonomy_revision
    )
    return session.scalars(
        select(RegistrationAuthoringRevision)
        .where(
            RegistrationAuthoringRevision.kind == kind.value,
            RegistrationAuthoringRevision.marketplace_key == marketplace_key,
            scope,
        )
        .order_by(RegistrationAuthoringRevision.seq.desc())
        .limit(1)
    ).one_or_none()


class AuthoringRevisionStore:
    """The only production writer of ``registration_authoring_revisions``."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    # -------------------------------------------------------------- reads

    def current(
        self,
        kind: AuthoringRevisionKind,
        marketplace_key: str,
        taxonomy_revision: str | None = None,
    ) -> AuthoringRevisionRecord | None:
        """The current revision of exactly this scope: its highest ``seq``, or none."""
        with self._db.read() as session:
            row = _current_row(session, kind, marketplace_key, taxonomy_revision)
            return None if row is None else _record(row)

    def detail_profile(self, revision_id: str) -> DetailProfile | None:
        """The ``DETAIL_COMPOSITION`` profile a revision identity names, or none when no such
        revision exists. A stored revision that no longer parses is refused, never guessed."""
        with self._db.read() as session:
            row = session.get(RegistrationAuthoringRevision, revision_id)
            if row is None or row.kind != AuthoringRevisionKind.DETAIL_COMPOSITION.value:
                return None
            return detail_profile_of(row.revision_id, json.loads(row.content_json))

    def history(
        self,
        kind: AuthoringRevisionKind,
        marketplace_key: str,
        taxonomy_revision: str | None = None,
    ) -> tuple[AuthoringRevisionRecord, ...]:
        with self._db.read() as session:
            scope = (
                RegistrationAuthoringRevision.taxonomy_revision.is_(None)
                if taxonomy_revision is None
                else RegistrationAuthoringRevision.taxonomy_revision == taxonomy_revision
            )
            rows = session.scalars(
                select(RegistrationAuthoringRevision)
                .where(
                    RegistrationAuthoringRevision.kind == kind.value,
                    RegistrationAuthoringRevision.marketplace_key == marketplace_key,
                    scope,
                )
                .order_by(RegistrationAuthoringRevision.seq)
            ).all()
            return tuple(_record(row) for row in rows)

    # -------------------------------------------------------------- the one write

    def ensure(
        self,
        session: Session,
        kind: AuthoringRevisionKind,
        marketplace_key: str,
        taxonomy_revision: str | None,
        content: Mapping[str, Any],
        *,
        correlation_id: str,
    ) -> str:
        """The identity of the scope's current revision holding exactly ``content``.

        The current revision is reused when its fingerprint is this content's; otherwise the next
        revision is appended — also when an older revision held the same content, so a rollback is
        a new revision and never a pointer moved backward. It runs inside the caller's unit of
        work and writes nothing when that unit is refused.
        """
        document = canonical_content(kind, marketplace_key, taxonomy_revision, content)
        fingerprint = sanitized_digest(document)
        current = _current_row(session, kind, marketplace_key, taxonomy_revision)
        if current is not None and current.content_fingerprint == fingerprint:
            return current.revision_id
        row = RegistrationAuthoringRevision(
            revision_id=str(uuid.uuid4()),
            kind=kind.value,
            marketplace_key=marketplace_key,
            taxonomy_revision=taxonomy_revision,
            seq=1 if current is None else current.seq + 1,
            content_json=canonical_json(document),
            content_fingerprint=fingerprint,
            created_at=self._clock.now(),
            created_by=SERVER_ACTOR,
        )
        session.add(row)
        session.flush()
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.REGISTRATION_AUTHORING_REVISION_APPENDED,
                action="AUTHORING_REVISION_APPENDED",
                actor=SERVER_ACTOR,
                outcome=AuditOutcome.RECORDED,
                target_ref=row.revision_id,
                before=None if current is None else {"revision_id": current.revision_id},
                after={
                    "revision_id": row.revision_id,
                    "seq": row.seq,
                    "content_fingerprint": fingerprint,
                },
                details={
                    "kind": kind.value,
                    "marketplace_key": marketplace_key,
                    "taxonomy_revision": taxonomy_revision,
                    "content_version": document["content_version"],
                },
                correlation_id=correlation_id,
            ),
            session=session,
        )
        return row.revision_id

    def stamp(
        self,
        session: Session,
        marketplace_key: str,
        taxonomy_revision: str,
        *,
        correlation_id: str,
    ) -> dict[str, str]:
        """The two owner-held references a new target-policy revision of this scope carries
        (resolution 5907626428 D3): the current category mapping of exactly this marketplace and
        taxonomy, and the current detail composition of exactly this marketplace."""
        return {
            "category_mapping_revision": self.ensure(
                session,
                AuthoringRevisionKind.CATEGORY_MAPPING,
                marketplace_key,
                taxonomy_revision,
                category_mapping_content(marketplace_key, taxonomy_revision),
                correlation_id=correlation_id,
            ),
            "detail_composition_revision": self.ensure(
                session,
                AuthoringRevisionKind.DETAIL_COMPOSITION,
                marketplace_key,
                None,
                detail_composition_content(marketplace_key),
                correlation_id=correlation_id,
            ),
        }
