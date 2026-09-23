"""The durable operator-reviewed category metadata (Gate 1 G1-B; ADR-0015 §3, ADR-0014 §4).

One owner per ``marketplace_key × taxonomy_revision × category_id``. No provider category,
attribute, option or notice endpoint is adopted, so this is where the metadata the REGISTER
preflight reads comes from: what an operator recorded from reviewed evidence. This module is its
only owner:

- **Revisions are append-only and server-created.** A save appends one revision: the server
  creates its identity (the ``CategoryMetadata.metadata_revision``), the fingerprint of its
  canonical sanitized content, its recording time and its audit record, and moves the key's one
  explicit current pointer to it in the same unit of work. A client never supplies a revision
  identity or a fingerprint, and history is never overwritten or deleted.
- **Review is explicit and server-owned.** ``reviewed`` is stored on every revision; a reviewed
  revision records the operator as reviewer and the server's time, an unreviewed one neither.
  Reviewing is recording a revision as reviewed — earlier revisions never change.
- **Operator review accepts supported evidence; it never guesses.** Every revision names the
  sanitized reference of the evidence it came from. Content an AI suggested can be recorded only
  as unreviewed: an AI suggestion alone never becomes reviewed metadata (ADR-0014 §4, §18).
- **A revision is validated whole, or not written**, and it materializes the existing
  ``CategoryMetadata`` contract exactly. There is no second category model.

:class:`DurableRegistrationMetadata` is the production ``RegistrationMetadataSource``. It reads the
**current** revision of the exact marketplace, taxonomy revision and category on every call —
never "the newest reviewed one" — so an unreviewed current revision answers
``CATEGORY_METADATA_UNREVIEWED`` in the preflight with no fallback, and a change of the current
revision changes the next evaluation's dependency fingerprint.
"""

import json
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.core.clock import Clock
from app.core.errors import InputValidationError, NotFoundError
from app.db.database import Database
from app.products.model import ReadinessStatus
from app.register import sanitize
from app.register.category_metadata_models import (
    RegistrationCategoryMetadata,
    RegistrationCategoryMetadataCurrent,
    RegistrationCategoryMetadataRevision,
)
from app.register.model import RegistrationConflictError, canonical_json, sanitized_digest
from app.register.policy import (
    CategoryMetadata,
    FieldRule,
    NoticePolicy,
    OptionPolicy,
    Provenance,
)
from app.register.target_policy import EditableSurface

CONTENT_VERSION: Final = "registration-category-metadata/v1"
MAX_RULES: Final = 200

CATEGORY_METADATA_INVALID: Final = "CATEGORY_METADATA_INVALID"
CATEGORY_METADATA_UNSAFE_CONTENT: Final = "CATEGORY_METADATA_UNSAFE_CONTENT"
CATEGORY_METADATA_AI_NOT_REVIEWABLE: Final = "CATEGORY_METADATA_AI_NOT_REVIEWABLE"
CATEGORY_METADATA_MARKETPLACE_UNKNOWN: Final = "CATEGORY_METADATA_MARKETPLACE_UNKNOWN"
CATEGORY_METADATA_CURRENT_MOVED: Final = "CATEGORY_METADATA_CURRENT_MOVED"
CATEGORY_METADATA_UNCHANGED: Final = "CATEGORY_METADATA_UNCHANGED"

# A taxonomy revision or category identifier is a plain token: it is also a path segment.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# ------------------------------------------------------------------ the application contract


class _Strict(BaseModel):
    """Every field required, no unknown field, no coercion of a scalar: a client-invented
    ``metadata_revision``, fingerprint or review time is refused, never repaired."""

    model_config = ConfigDict(extra="forbid")


class FieldRuleView(_Strict):
    key: StrictStr
    required: StrictBool
    detail_page_reference_allowed: StrictBool
    missing_status: Literal["REVIEW_REQUIRED", "BLOCKED"]
    max_length: StrictInt | None


class NoticePolicyView(_Strict):
    notice_type: StrictStr
    fields: list[FieldRuleView]


class OptionPolicyView(_Strict):
    options_supported: StrictBool
    max_options: StrictInt
    max_dimensions: StrictInt


class CategoryMetadataContentView(_Strict):
    """Everything the existing ``CategoryMetadata`` contract carries, beyond its identity, its
    ``metadata_revision`` and ``reviewed`` — which the server owns."""

    leaf: StrictBool
    registrable: StrictBool
    name_max_length: StrictInt
    attributes: list[FieldRuleView]
    notice: NoticePolicyView | None
    options: OptionPolicyView
    required_templates: list[StrictStr]


class RecordRevisionRequest(_Strict):
    """One save. ``content_provenance`` says where the rules came from; ``evidence_reference`` is
    the sanitized reference of the reviewed source; ``reviewed`` asks the server to record this
    revision as reviewed by ``actor``. ``expected_current_revision`` is the revision the operator
    edited from (``null`` for a new key); a save against a moved key is refused, never merged."""

    actor: StrictStr = Field(min_length=2, max_length=64)
    expected_current_revision: StrictStr | None
    content_provenance: Literal["OPERATOR_CONFIRMED", "AI_SUGGESTION"]
    evidence_reference: StrictStr
    reviewed: StrictBool
    content: CategoryMetadataContentView


class MetadataRevisionView(BaseModel):
    metadata_revision: str
    revision_no: int
    content_fingerprint: str
    reviewed: bool
    reviewed_by: str | None
    reviewed_at: datetime | None
    content_provenance: str
    evidence_reference: str
    recorded_by: str
    recorded_at: datetime
    current: bool


class CategoryMetadataView(BaseModel):
    """One key's metadata as the server holds it. ``editable`` is the server's answer."""

    marketplace_key: str
    taxonomy_revision: str
    category_id: str
    surface: EditableSurface
    editable: bool
    current: MetadataRevisionView | None
    content: CategoryMetadataContentView | None
    history: list[MetadataRevisionView]


class CategoryMetadataListView(BaseModel):
    marketplace_key: str
    surface: EditableSurface
    entries: list[CategoryMetadataView]


# ------------------------------------------------------------------ content


def _invalid(
    message: str, field: str, code: str = CATEGORY_METADATA_INVALID
) -> InputValidationError:
    return InputValidationError(code, message, details={"field": field})


def _label(value: str, field: str) -> str:
    if not sanitize.safe_label(value):
        raise _invalid("a plain identity or version label is required", field)
    return value


def _identifier(value: str, field: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise _invalid("a plain identifier: letters, digits, '.', '_' or '-'", field)
    return value


def _rules(rules: list[FieldRuleView], field: str) -> list[dict[str, Any]]:
    if len(rules) > MAX_RULES:
        raise _invalid("too many field rules", field)
    keys = [_label(rule.key, f"{field}.key") for rule in rules]
    if len(set(keys)) != len(keys):
        raise _invalid("a field rule key is listed twice", field)
    encoded = []
    for rule in rules:
        if rule.max_length is not None and rule.max_length < 1:
            raise _invalid("a length limit is at least one", f"{field}.{rule.key}.max_length")
        encoded.append(
            {
                "key": rule.key,
                "required": rule.required,
                "detail_page_reference_allowed": rule.detail_page_reference_allowed,
                "missing_status": rule.missing_status,
                "max_length": rule.max_length,
            }
        )
    return encoded


def encode_content(
    marketplace_key: str, taxonomy_revision: str, category_id: str, request: RecordRevisionRequest
) -> dict[str, Any]:
    """The canonical sanitized content of one revision, or a refusal of the whole save."""
    _identifier(taxonomy_revision, "taxonomy_revision")
    _identifier(category_id, "category_id")
    if request.reviewed and request.content_provenance != Provenance.OPERATOR_CONFIRMED.value:
        raise _invalid(
            "an AI suggestion alone is never reviewed metadata; record it unreviewed, and record"
            " an operator-confirmed revision from reviewed evidence",
            "reviewed",
            CATEGORY_METADATA_AI_NOT_REVIEWABLE,
        )
    evidence = request.evidence_reference
    if not sanitize.safe_label(evidence):
        raise _invalid(
            "the evidence reference is a plain sanitized reference: no URL, credential or session",
            "evidence_reference",
        )
    content = request.content
    if content.name_max_length < 1:
        raise _invalid("the name length limit is at least one", "name_max_length")
    options = content.options
    if options.max_options < 1 or options.max_dimensions < 1:
        raise _invalid("option limits are at least one", "options")
    if not options.options_supported and (options.max_options, options.max_dimensions) != (1, 1):
        raise _invalid(
            "a category without options allows exactly one listing item and one dimension",
            "options",
        )
    templates = [_label(kind, "required_templates") for kind in content.required_templates]
    if len(set(templates)) != len(templates):
        raise _invalid("a required template is listed twice", "required_templates")
    notice = content.notice
    document: dict[str, Any] = {
        "content_version": CONTENT_VERSION,
        "marketplace_key": marketplace_key,
        "taxonomy_revision": taxonomy_revision,
        "category_id": category_id,
        "content_provenance": request.content_provenance,
        "evidence_reference": evidence,
        "leaf": content.leaf,
        "registrable": content.registrable,
        "name_max_length": content.name_max_length,
        "attributes": _rules(content.attributes, "attributes"),
        "notice": None
        if notice is None
        else {
            "notice_type": _label(notice.notice_type, "notice.notice_type"),
            "fields": _rules(notice.fields, "notice.fields"),
        },
        "options": {
            "options_supported": options.options_supported,
            "max_options": options.max_options,
            "max_dimensions": options.max_dimensions,
        },
        "required_templates": sorted(templates),
    }
    try:
        sanitize.require_clean(document, "category_metadata")
    except sanitize.PayloadSanitationError as refused:
        raise InputValidationError(
            CATEGORY_METADATA_UNSAFE_CONTENT,
            "category metadata never holds a URL, a credential or session material",
            details={"found": [list(item) for item in refused.found]},
        ) from refused
    return document


def content_of(document: Mapping[str, Any]) -> CategoryMetadataContentView:
    return CategoryMetadataContentView.model_validate(
        {key: document[key] for key in CategoryMetadataContentView.model_fields}
    )


def _field_rule(rule: Mapping[str, Any]) -> FieldRule:
    return FieldRule(
        key=str(rule["key"]),
        required=bool(rule["required"]),
        detail_page_reference_allowed=bool(rule["detail_page_reference_allowed"]),
        missing_status=ReadinessStatus(str(rule["missing_status"])),
        max_length=None if rule["max_length"] is None else int(rule["max_length"]),
    )


def category_metadata_of(
    metadata_revision: str, reviewed: bool, document: Mapping[str, Any]
) -> CategoryMetadata:
    """The existing ``CategoryMetadata`` contract, materialized from one stored revision."""
    notice = document["notice"]
    options = document["options"]
    return CategoryMetadata(
        taxonomy_revision=str(document["taxonomy_revision"]),
        category_id=str(document["category_id"]),
        metadata_revision=metadata_revision,
        reviewed=reviewed,
        leaf=bool(document["leaf"]),
        registrable=bool(document["registrable"]),
        attributes=tuple(_field_rule(rule) for rule in document["attributes"]),
        notice=None
        if notice is None
        else NoticePolicy(
            notice_type=str(notice["notice_type"]),
            fields=tuple(_field_rule(rule) for rule in notice["fields"]),
        ),
        options=OptionPolicy(
            options_supported=bool(options["options_supported"]),
            max_options=int(options["max_options"]),
            max_dimensions=int(options["max_dimensions"]),
        ),
        name_max_length=int(document["name_max_length"]),
        required_templates=frozenset(str(t) for t in document["required_templates"]),
    )


# ------------------------------------------------------------------ the store


@dataclass(frozen=True)
class MetadataRevisionRecord:
    metadata_revision: str
    revision_no: int
    content: Mapping[str, Any]
    content_fingerprint: str
    reviewed: bool
    reviewed_by: str | None
    reviewed_at: datetime | None
    recorded_by: str
    recorded_at: datetime


@dataclass(frozen=True)
class MetadataRecord:
    marketplace_key: str
    taxonomy_revision: str
    category_id: str
    current: MetadataRevisionRecord | None
    history: tuple[MetadataRevisionRecord, ...]


class CategoryMetadataStore:
    """The only production writer of the three category-metadata tables."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    # -------------------------------------------------------------- reads

    def record(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str
    ) -> MetadataRecord:
        with self._reading() as session:
            row = _key_row(session, marketplace_key, taxonomy_revision, category_id)
            return _metadata_record(session, marketplace_key, taxonomy_revision, category_id, row)

    def records(self, marketplace_key: str) -> tuple[MetadataRecord, ...]:
        with self._reading() as session:
            rows = session.scalars(
                select(RegistrationCategoryMetadata)
                .where(RegistrationCategoryMetadata.marketplace_key == marketplace_key)
                .order_by(
                    RegistrationCategoryMetadata.taxonomy_revision,
                    RegistrationCategoryMetadata.category_id,
                )
            ).all()
            return tuple(
                _metadata_record(
                    session, marketplace_key, row.taxonomy_revision, row.category_id, row
                )
                for row in rows
            )

    def current(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str
    ) -> MetadataRevisionRecord | None:
        with self._reading() as session:
            row = _key_row(session, marketplace_key, taxonomy_revision, category_id)
            return None if row is None else _current_record(session, row.metadata_id)

    def revision(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str, metadata_revision: str
    ) -> MetadataRevisionRecord | None:
        """The exact revision, only if it belongs to this key: a revision of another marketplace,
        taxonomy or category is never returned in its place."""
        with self._reading() as session:
            row = _key_row(session, marketplace_key, taxonomy_revision, category_id)
            if row is None:
                return None
            revision = session.get(RegistrationCategoryMetadataRevision, metadata_revision)
            if revision is None or revision.metadata_id != row.metadata_id:
                return None
            return _revision_record(revision)

    # -------------------------------------------------------------- the one write

    def append(
        self,
        marketplace_key: str,
        taxonomy_revision: str,
        category_id: str,
        content: Mapping[str, Any],
        *,
        reviewed: bool,
        expected_current_revision: str | None,
        recorded_by: str,
        correlation_id: str,
    ) -> MetadataRevisionRecord:
        """Append one revision and make it current, in one unit of work, or change nothing."""
        fingerprint = sanitized_digest(content)
        now = self._clock.now()
        with self._db.write() as session:
            key = _key_row(session, marketplace_key, taxonomy_revision, category_id)
            pointer = (
                None
                if key is None
                else session.get(RegistrationCategoryMetadataCurrent, key.metadata_id)
            )
            if (None if pointer is None else pointer.metadata_revision_id) != (
                expected_current_revision
            ):
                raise RegistrationConflictError(
                    CATEGORY_METADATA_CURRENT_MOVED,
                    "the metadata changed since it was read; reload it before saving",
                    details={
                        "current_revision": None
                        if pointer is None
                        else pointer.metadata_revision_id
                    },
                )
            if pointer is not None:
                previous = session.get(
                    RegistrationCategoryMetadataRevision, pointer.metadata_revision_id
                )
                if (
                    previous is not None
                    and previous.content_fingerprint == fingerprint
                    and bool(previous.reviewed) == reviewed
                ):
                    raise RegistrationConflictError(
                        CATEGORY_METADATA_UNCHANGED,
                        "the saved metadata and review state are identical to the current revision",
                    )
            if key is None:
                key = RegistrationCategoryMetadata(
                    metadata_id=str(uuid.uuid4()),
                    marketplace_key=marketplace_key,
                    taxonomy_revision=taxonomy_revision,
                    category_id=category_id,
                    created_by=recorded_by,
                    correlation_id=correlation_id,
                    created_at=now,
                )
                session.add(key)
                session.flush()
            number = 1 + int(
                session.scalar(
                    select(
                        func.coalesce(func.max(RegistrationCategoryMetadataRevision.revision_no), 0)
                    ).where(RegistrationCategoryMetadataRevision.metadata_id == key.metadata_id)
                )
                or 0
            )
            revision = RegistrationCategoryMetadataRevision(
                metadata_revision_id=str(uuid.uuid4()),
                metadata_id=key.metadata_id,
                revision_no=number,
                content_json=canonical_json(dict(content)),
                content_fingerprint=fingerprint,
                reviewed=1 if reviewed else 0,
                reviewed_by=recorded_by if reviewed else None,
                reviewed_at=now if reviewed else None,
                recorded_by=recorded_by,
                correlation_id=correlation_id,
                recorded_at=now,
            )
            session.add(revision)
            session.flush()
            previous_id = None if pointer is None else pointer.metadata_revision_id
            if pointer is None:
                session.add(
                    RegistrationCategoryMetadataCurrent(
                        metadata_id=key.metadata_id,
                        metadata_revision_id=revision.metadata_revision_id,
                        moved_by=recorded_by,
                        correlation_id=correlation_id,
                        moved_at=now,
                    )
                )
            else:
                pointer.metadata_revision_id = revision.metadata_revision_id
                pointer.moved_by = recorded_by
                pointer.correlation_id = correlation_id
                pointer.moved_at = now
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.REGISTRATION_CATEGORY_METADATA_RECORDED,
                    action="CATEGORY_METADATA_REVISION_APPENDED",
                    actor=recorded_by,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=key.metadata_id,
                    before=None if previous_id is None else {"metadata_revision": previous_id},
                    after={
                        "metadata_revision": revision.metadata_revision_id,
                        "revision_no": number,
                        "content_fingerprint": fingerprint,
                        "reviewed": reviewed,
                    },
                    details={
                        "marketplace_key": marketplace_key,
                        "taxonomy_revision": taxonomy_revision,
                        "category_id": category_id,
                        "content_version": CONTENT_VERSION,
                    },
                    correlation_id=correlation_id,
                ),
                session=session,
            )
            return _revision_record(revision)

    @contextmanager
    def _reading(self) -> Iterator[Session]:
        with self._db.read() as session:
            yield session


def _key_row(
    session: Session, marketplace_key: str, taxonomy_revision: str, category_id: str
) -> RegistrationCategoryMetadata | None:
    return session.scalars(
        select(RegistrationCategoryMetadata).where(
            RegistrationCategoryMetadata.marketplace_key == marketplace_key,
            RegistrationCategoryMetadata.taxonomy_revision == taxonomy_revision,
            RegistrationCategoryMetadata.category_id == category_id,
        )
    ).one_or_none()


def _current_record(session: Session, metadata_id: str) -> MetadataRevisionRecord | None:
    pointer = session.get(RegistrationCategoryMetadataCurrent, metadata_id)
    if pointer is None:
        return None
    revision = session.get(RegistrationCategoryMetadataRevision, pointer.metadata_revision_id)
    return None if revision is None else _revision_record(revision)


def _metadata_record(
    session: Session,
    marketplace_key: str,
    taxonomy_revision: str,
    category_id: str,
    row: RegistrationCategoryMetadata | None,
) -> MetadataRecord:
    if row is None:
        return MetadataRecord(marketplace_key, taxonomy_revision, category_id, None, ())
    history = tuple(
        _revision_record(r)
        for r in session.scalars(
            select(RegistrationCategoryMetadataRevision)
            .where(RegistrationCategoryMetadataRevision.metadata_id == row.metadata_id)
            .order_by(RegistrationCategoryMetadataRevision.revision_no.desc())
        ).all()
    )
    return MetadataRecord(
        marketplace_key,
        taxonomy_revision,
        category_id,
        _current_record(session, row.metadata_id),
        history,
    )


def _revision_record(row: RegistrationCategoryMetadataRevision) -> MetadataRevisionRecord:
    return MetadataRevisionRecord(
        metadata_revision=row.metadata_revision_id,
        revision_no=row.revision_no,
        content=json.loads(row.content_json),
        content_fingerprint=row.content_fingerprint,
        reviewed=bool(row.reviewed),
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        recorded_by=row.recorded_by,
        recorded_at=row.recorded_at,
    )


# ------------------------------------------------------------------ the production metadata source


class DurableRegistrationMetadata:
    """The production ``RegistrationMetadataSource``: the **current** revision of exactly this
    marketplace, taxonomy revision and category, read on every call. Never another marketplace,
    never another taxonomy revision, and never an older reviewed revision in place of an
    unreviewed current one."""

    def __init__(self, store: CategoryMetadataStore) -> None:
        self._store = store

    def category(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str
    ) -> CategoryMetadata | None:
        current = self._store.current(marketplace_key, taxonomy_revision, category_id)
        if current is None:
            return None
        return category_metadata_of(current.metadata_revision, current.reviewed, current.content)

    def revision(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str, metadata_revision: str
    ) -> CategoryMetadata | None:
        """The exact historical revision a frozen Snapshot names — never the current one."""
        found = self._store.revision(
            marketplace_key, taxonomy_revision, category_id, metadata_revision
        )
        if found is None:
            return None
        return category_metadata_of(found.metadata_revision, found.reviewed, found.content)


# ------------------------------------------------------------------ the operator application owner


class CategoryMetadataService:
    """What the operator reads and records for category metadata, provider-zero."""

    def __init__(self, store: CategoryMetadataStore, marketplaces: frozenset[str]) -> None:
        self._store = store
        self._marketplaces = marketplaces

    def entries(self, marketplace_key: str) -> CategoryMetadataListView:
        self._require_marketplace(marketplace_key)
        return CategoryMetadataListView(
            marketplace_key=marketplace_key,
            surface=EditableSurface.REGISTRATION_CATEGORY_METADATA,
            entries=[_view(record) for record in self._store.records(marketplace_key)],
        )

    def metadata(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str
    ) -> CategoryMetadataView:
        self._require_marketplace(marketplace_key)
        _identifier(taxonomy_revision, "taxonomy_revision")
        _identifier(category_id, "category_id")
        return _view(self._store.record(marketplace_key, taxonomy_revision, category_id))

    def record(
        self,
        marketplace_key: str,
        taxonomy_revision: str,
        category_id: str,
        request: RecordRevisionRequest,
        *,
        correlation_id: str,
    ) -> CategoryMetadataView:
        self._require_marketplace(marketplace_key)
        document = encode_content(marketplace_key, taxonomy_revision, category_id, request)
        # The contract materializes before anything is written: no second category model.
        category_metadata_of("pending", request.reviewed, document)
        self._store.append(
            marketplace_key,
            taxonomy_revision,
            category_id,
            document,
            reviewed=request.reviewed,
            expected_current_revision=request.expected_current_revision,
            recorded_by=request.actor,
            correlation_id=correlation_id,
        )
        return _view(self._store.record(marketplace_key, taxonomy_revision, category_id))

    def _require_marketplace(self, marketplace_key: str) -> None:
        if marketplace_key not in self._marketplaces:
            raise NotFoundError(
                CATEGORY_METADATA_MARKETPLACE_UNKNOWN,
                "category metadata belongs to a known marketplace",
            )


def _revision_view(record: MetadataRevisionRecord, *, current: bool) -> MetadataRevisionView:
    return MetadataRevisionView(
        metadata_revision=record.metadata_revision,
        revision_no=record.revision_no,
        content_fingerprint=record.content_fingerprint,
        reviewed=record.reviewed,
        reviewed_by=record.reviewed_by,
        reviewed_at=record.reviewed_at,
        content_provenance=str(record.content["content_provenance"]),
        evidence_reference=str(record.content["evidence_reference"]),
        recorded_by=record.recorded_by,
        recorded_at=record.recorded_at,
        current=current,
    )


def _view(record: MetadataRecord) -> CategoryMetadataView:
    current = record.current
    return CategoryMetadataView(
        marketplace_key=record.marketplace_key,
        taxonomy_revision=record.taxonomy_revision,
        category_id=record.category_id,
        surface=EditableSurface.REGISTRATION_CATEGORY_METADATA,
        editable=True,
        current=None if current is None else _revision_view(current, current=True),
        content=None if current is None else content_of(current.content),
        history=[
            _revision_view(
                revision,
                current=current is not None
                and revision.metadata_revision == current.metadata_revision,
            )
            for revision in record.history
        ],
    )
