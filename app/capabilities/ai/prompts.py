"""The PromptTemplate and PlatformPolicy stores and the Settings registry over them (ADR-0026 §3).

The registry is the v29 prototype's (``registry.CATALOG``). Each entry's text lives only in its
store: revision 1 is the newest seed's text (``seed_v29.json``), which the application writes when
it starts on a database that lacks it, and every later revision is an operator's save of one
field, or a reset of one field to the seed. A save names the revision it was read from
(``expected_current_revision``); a save that would change nothing is refused; every save is audited
by identity and fingerprint, never by its text (AIF-04).

**Seed upgrades (ADR-0027 §6, AIS-07).** A seed version after ``v29`` names, per field, the text it
replaces. On startup an existing store is upgraded only where the field still holds exactly that
text: a ``RESET`` revision by ``system:seed``, audited with the seed version. An operator's edit is
never overwritten. An entry's 기본값 is the newest seed: revision 1 with every upgrade whose
replaced text it holds applied, so a reset restores the newest seed and ``modified_fields``
compares with it.

The composition is the prototype's own preview layout. Here it is shown with the runtime-data
placeholder only; filling it, and adding the platform limits a policy references, is AIF-2.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.capabilities.ai.prompt_models import (
    AIPlatformPolicy,
    AIPlatformPolicyCurrent,
    AIPlatformPolicyRevision,
    AIPromptTemplate,
    AIPromptTemplateCurrent,
    AIPromptTemplateRevision,
)
from app.capabilities.ai.registry import (
    BY_KEY,
    CATALOG,
    CONTENT_FIELDS,
    EDITABLE_FIELDS,
    CatalogEntry,
    Layer,
)
from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, InputValidationError, NotFoundError
from app.platform.db.database import Database

AI_PROMPT_UNKNOWN: Final = "AI_PROMPT_UNKNOWN"
AI_PROMPT_FIELD_NOT_EDITABLE: Final = "AI_PROMPT_FIELD_NOT_EDITABLE"
AI_PROMPT_TEXT_INVALID: Final = "AI_PROMPT_TEXT_INVALID"
AI_PROMPT_CURRENT_MOVED: Final = "AI_PROMPT_CURRENT_MOVED"
AI_PROMPT_UNCHANGED: Final = "AI_PROMPT_UNCHANGED"
AI_PROMPT_PREVIEW_INVALID: Final = "AI_PROMPT_PREVIEW_INVALID"

MAX_TEXT: Final = 20_000
# The v29 prototype's texts, verbatim, with the versioned seed upgrades applied: the seed of every
# registry entry (ADR-0026 §3.1). It is only ever written into the stores, as revision 1 or as a
# seed upgrade; runtime reads the stores, and the upgrades only to know an entry's newest seed.
SEED_FILE: Final = Path(__file__).with_name("seed_v29.json")
SEED_ACTOR: Final = "system:seed"
RUNTIME_PLACEHOLDER: Final = "{실행 시 ICBM 실제 데이터가 여기에 주입됩니다.}"
_RULE: Final = "----------------------------------------"


class PromptRegistryConflictError(AppError):
    """A registry save the current state refuses: the entry moved, or nothing would change."""

    error_class = ErrorClass.CONFLICT


class Origin(StrEnum):
    SEED = "SEED"
    OPERATOR = "OPERATOR"
    RESET = "RESET"


def canonical_json(content: Mapping[str, str]) -> str:
    return json.dumps(dict(content), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(content: Mapping[str, str]) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ the two families


@dataclass(frozen=True)
class _Family:
    name: str
    identity: type[Any]
    revisions: type[Any]
    current: type[Any]
    key: str
    event: AuditEventType


TEMPLATES: Final = _Family(
    "template",
    AIPromptTemplate,
    AIPromptTemplateRevision,
    AIPromptTemplateCurrent,
    "template_key",
    AuditEventType.AI_PROMPT_TEMPLATE_REVISED,
)
POLICIES: Final = _Family(
    "policy",
    AIPlatformPolicy,
    AIPlatformPolicyRevision,
    AIPlatformPolicyCurrent,
    "policy_key",
    AuditEventType.AI_PLATFORM_POLICY_REVISED,
)


def _family(entry: CatalogEntry) -> _Family:
    return POLICIES if entry.layer is Layer.POLICY else TEMPLATES


@dataclass(frozen=True)
class RevisionRecord:
    revision_id: str
    revision_no: int
    content: Mapping[str, str]
    content_fingerprint: str
    origin: Origin
    authored_by: str
    authored_at: datetime


@dataclass(frozen=True)
class EntryRecord:
    key: str
    current: RevisionRecord
    seed: RevisionRecord
    history: tuple[RevisionRecord, ...]


def load_seed() -> dict[str, Any]:
    seed: dict[str, Any] = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    return seed


@dataclass(frozen=True)
class SeedUpgrade:
    seed_version: str
    key: str
    field: str
    previous: str
    text: str


@cache
def seed_upgrades() -> tuple[SeedUpgrade, ...]:
    """The seed's upgrades in order, each with the text it replaces and the newest text."""
    seed = load_seed()
    upgrades = []
    for item in seed.get("upgrades", ()):
        family = "policies" if item["key"] in seed["policies"] else "templates"
        upgrades.append(
            SeedUpgrade(
                seed_version=item["seed_version"],
                key=item["key"],
                field=item["field"],
                previous=item["previous"],
                text=seed[family][item["key"]]["content"][item["field"]],
            )
        )
    return tuple(upgrades)


def newest_seed(key: str, content: Mapping[str, str]) -> dict[str, str]:
    """Revision 1's content with every upgrade whose replaced text it holds applied."""
    upgraded = dict(content)
    for upgrade in seed_upgrades():
        if upgrade.key == key and upgraded.get(upgrade.field) == upgrade.previous:
            upgraded[upgrade.field] = upgrade.text
    return upgraded


class PromptRegistryStore:
    """The only production writer of the six registry tables."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    def seed_missing(self, *, correlation_id: str) -> int:
        """Write the v29 seed of every catalog entry the stores lack, as its revision 1, in one
        unit of work, and return how many were written. An entry that exists is never touched, so
        a restart writes nothing. A seed is starting data, not an operator action: it writes no
        audit event, and each seed revision names its origin, seed version and ``system:seed``."""
        seed = load_seed()
        now = self._clock.now()
        written = 0
        with self._db.write() as session:
            for entry in CATALOG:
                family = _family(entry)
                if session.get(family.identity, entry.key) is not None:
                    continue
                if family is POLICIES:
                    content = seed["policies"][entry.key]["content"]
                    session.add(family.identity(created_at=now, **{family.key: entry.key}))
                else:
                    content = seed["templates"][entry.key]["content"]
                    session.add(
                        family.identity(
                            layer=entry.layer.value, created_at=now, **{family.key: entry.key}
                        )
                    )
                session.flush()
                row = family.revisions(
                    revision_id=str(uuid.uuid4()),
                    revision_no=1,
                    content_json=canonical_json(content),
                    content_fingerprint=fingerprint(content),
                    origin=Origin.SEED.value,
                    seed_version=seed["seed_version"],
                    authored_by=SEED_ACTOR,
                    correlation_id=correlation_id,
                    authored_at=now,
                    **{family.key: entry.key},
                )
                session.add(row)
                session.flush()
                session.add(
                    family.current(
                        revision_id=row.revision_id,
                        moved_by=SEED_ACTOR,
                        correlation_id=correlation_id,
                        moved_at=now,
                        **{family.key: entry.key},
                    )
                )
                written += 1
        return written

    def upgrade_seeds(self, *, correlation_id: str) -> int:
        """Apply every seed upgrade to the entries whose field still holds exactly the text it
        replaces, each as an audited ``RESET`` revision by ``system:seed``, and return how many were
        applied. An operator's edit is never overwritten, and a restart applies nothing again."""
        applied = 0
        for upgrade in seed_upgrades():
            catalog = _catalog(upgrade.key)
            family = _family(catalog)
            with self._db.write() as session:
                record = _entry(session, family, upgrade.key)
                if record.current.content.get(upgrade.field) != upgrade.previous:
                    continue
                self._append(
                    session,
                    family,
                    catalog,
                    record,
                    upgrade.field,
                    {**record.current.content, upgrade.field: upgrade.text},
                    Origin.RESET,
                    actor=SEED_ACTOR,
                    correlation_id=correlation_id,
                    details={"seed_version": upgrade.seed_version},
                )
                applied += 1
        return applied

    def entry(self, key: str) -> EntryRecord:
        catalog = _catalog(key)
        family = _family(catalog)
        with self._db.read() as session:
            return _entry(session, family, key)

    def entries(self) -> tuple[EntryRecord, ...]:
        with self._db.read() as session:
            return tuple(_entry(session, _family(entry), entry.key) for entry in CATALOG)

    def revise(
        self,
        key: str,
        field: str,
        text: str | None,
        *,
        expected_current_revision: str,
        actor: str,
        correlation_id: str,
    ) -> RevisionRecord:
        """Append one revision that changes exactly ``field`` and make it current, or change
        nothing. ``text`` is the new text; ``None`` resets the field to the seed's text."""
        catalog = _catalog(key)
        if field not in EDITABLE_FIELDS[catalog.layer]:
            raise InputValidationError(
                AI_PROMPT_FIELD_NOT_EDITABLE,
                f"{field} is not an editable field of a {catalog.layer.value} entry",
                details={"field": field},
            )
        if text is not None and (not text.strip() or len(text) > MAX_TEXT):
            raise InputValidationError(
                AI_PROMPT_TEXT_INVALID,
                f"the text must not be blank and is at most {MAX_TEXT} characters",
                details={"field": field},
            )
        family = _family(catalog)
        with self._db.write() as session:
            record = _entry(session, family, key)
            current = record.current
            if current.revision_id != expected_current_revision:
                raise PromptRegistryConflictError(
                    AI_PROMPT_CURRENT_MOVED,
                    "the entry changed since it was read; reload it before saving",
                    details={"current_revision": current.revision_id},
                )
            origin = Origin.OPERATOR if text is not None else Origin.RESET
            value = text if text is not None else record.seed.content[field]
            content = {**current.content, field: value}
            if fingerprint(content) == current.content_fingerprint:
                raise PromptRegistryConflictError(
                    AI_PROMPT_UNCHANGED,
                    "the text is identical to the current revision",
                    details={"current_revision": current.revision_id},
                )
            return self._append(
                session,
                family,
                catalog,
                record,
                field,
                content,
                origin,
                actor=actor,
                correlation_id=correlation_id,
            )

    def _append(
        self,
        session: Session,
        family: _Family,
        catalog: CatalogEntry,
        record: EntryRecord,
        field: str,
        content: Mapping[str, str],
        origin: Origin,
        *,
        actor: str,
        correlation_id: str,
        details: Mapping[str, str] | None = None,
    ) -> RevisionRecord:
        """Append one revision of ``content`` as the entry's current one, audited."""
        key = record.key
        current = record.current
        now = self._clock.now()
        digest = fingerprint(content)
        number = 1 + int(
            session.scalar(
                select(func.coalesce(func.max(family.revisions.revision_no), 0)).where(
                    getattr(family.revisions, family.key) == key
                )
            )
            or 0
        )
        row = family.revisions(
            revision_id=str(uuid.uuid4()),
            revision_no=number,
            content_json=canonical_json(content),
            content_fingerprint=digest,
            origin=origin.value,
            seed_version=None,
            authored_by=actor,
            correlation_id=correlation_id,
            authored_at=now,
            **{family.key: key},
        )
        session.add(row)
        session.flush()
        pointer = session.get(family.current, key)
        assert pointer is not None  # _entry above proved it exists
        pointer.revision_id = row.revision_id
        pointer.moved_by = actor
        pointer.correlation_id = correlation_id
        pointer.moved_at = now
        session.flush()
        self._audit.append(
            AuditEntry(
                event_type=family.event,
                action=f"AI_{family.name.upper()}_{origin.value}",
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=key,
                before={
                    "revision": current.revision_id,
                    "content_fingerprint": current.content_fingerprint,
                },
                after={
                    "revision": row.revision_id,
                    "revision_no": number,
                    "content_fingerprint": digest,
                },
                details={
                    "layer": catalog.layer.value,
                    "field": field,
                    "origin": origin.value,
                    **(details or {}),
                },
                correlation_id=correlation_id,
            ),
            session=session,
        )
        return _record(row)


def _catalog(key: str) -> CatalogEntry:
    entry = BY_KEY.get(key)
    if entry is None:
        raise NotFoundError(AI_PROMPT_UNKNOWN, f"no registry entry is named {key}")
    return entry


def _entry(session: Session, family: _Family, key: str) -> EntryRecord:
    rows = session.scalars(
        select(family.revisions)
        .where(getattr(family.revisions, family.key) == key)
        .order_by(family.revisions.revision_no.desc())
    ).all()
    pointer = session.get(family.current, key)
    if not rows or pointer is None:
        raise NotFoundError(AI_PROMPT_UNKNOWN, f"the store holds no entry named {key}")
    history = tuple(_record(row) for row in rows)
    current = next(record for record in history if record.revision_id == pointer.revision_id)
    first = next(record for record in history if record.origin is Origin.SEED)
    content = newest_seed(key, first.content)
    seed = (
        first
        if content == first.content
        else replace(first, content=content, content_fingerprint=fingerprint(content))
    )
    return EntryRecord(key, current, seed, history)


def _record(row: Any) -> RevisionRecord:
    return RevisionRecord(
        revision_id=row.revision_id,
        revision_no=row.revision_no,
        content=json.loads(row.content_json),
        content_fingerprint=row.content_fingerprint,
        origin=Origin(row.origin),
        authored_by=row.authored_by,
        authored_at=row.authored_at,
    )


# ------------------------------------------------------------------ the composition


def compose(
    global_rules: str,
    role: str,
    policy: str,
    task: Mapping[str, str],
    runtime_data: str = RUNTIME_PLACEHOLDER,
) -> str:
    """One task's request text, in the prototype's layout: Global → Role → Platform Policy → Task
    (its mode and prompt) → input variables → output schema → runtime data."""

    def section(title: str, body: str) -> str:
        return f"{_RULE}\n{title}\n{_RULE}\n{body}"

    return "\n\n".join(
        (
            global_rules,
            section("ROLE_PROFILE", role),
            section("PLATFORM_POLICY", policy),
            section("TASK_MODE", task["mode"]) + "\n\n" + task["prompt"],
            section("INPUT_VARIABLES", task["variables"]),
            section("OUTPUT_SCHEMA", task["output"]),
            section("RUNTIME_DATA", runtime_data),
        )
    )


# ------------------------------------------------------------------ the Settings contract


class RevisionView(BaseModel):
    revision_id: str
    revision_no: int
    origin: Origin
    content_fingerprint: str
    authored_by: str
    authored_at: datetime
    current: bool


class EntryView(BaseModel):
    key: str
    layer: Layer
    title: str
    description: str | None
    screen: str | None
    role_key: str | None
    editable_fields: list[str]
    content: dict[str, str]
    # The fields whose current text differs from the newest seed (the prototype's 사용자 수정본).
    modified_fields: list[str]
    current: RevisionView
    history: list[RevisionView]


class RegistryView(BaseModel):
    entries: list[EntryView]


class ReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: StrictStr = Field(min_length=1, max_length=64)
    expected_current_revision: StrictStr = Field(min_length=1, max_length=36)
    field: StrictStr = Field(min_length=1, max_length=16)
    text: StrictStr = Field(max_length=MAX_TEXT)


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: StrictStr = Field(min_length=1, max_length=64)
    expected_current_revision: StrictStr = Field(min_length=1, max_length=36)
    field: StrictStr = Field(min_length=1, max_length=16)


class PreviewView(BaseModel):
    role_key: str
    policy_key: str
    task_key: str
    # The revision each composed layer was read at: the composition's prompt and policy version.
    revisions: dict[str, str]
    text: str


class PromptRegistryService:
    """What Settings reads and saves for the AI Prompt Registry, and nothing else."""

    def __init__(self, store: PromptRegistryStore) -> None:
        self._store = store

    def seed_on_startup(self) -> int:
        """The application's startup pass: the newest seed of every entry the stores lack, then
        the seed upgrades of the entries that still hold the text an upgrade replaces."""
        written = self._store.seed_missing(correlation_id="startup-ai-prompt-seed")
        return written + self._store.upgrade_seeds(correlation_id="startup-ai-prompt-seed")

    def registry(self) -> RegistryView:
        return RegistryView(entries=[_view(record) for record in self._store.entries()])

    def revise(
        self, key: str, layer: Literal["template", "policy"], request: ReviseRequest, *, cid: str
    ) -> EntryView:
        _require_family(key, layer)
        self._store.revise(
            key,
            request.field,
            request.text,
            expected_current_revision=request.expected_current_revision,
            actor=request.actor,
            correlation_id=cid,
        )
        return _view(self._store.entry(key))

    def reset(
        self, key: str, layer: Literal["template", "policy"], request: ResetRequest, *, cid: str
    ) -> EntryView:
        _require_family(key, layer)
        self._store.revise(
            key,
            request.field,
            None,
            expected_current_revision=request.expected_current_revision,
            actor=request.actor,
            correlation_id=cid,
        )
        return _view(self._store.entry(key))

    def preview(self, role_key: str, policy_key: str, task_key: str) -> PreviewView:
        # A pair per layer: one key passed for two layers is checked against each of them.
        expected = ((role_key, Layer.ROLE), (policy_key, Layer.POLICY), (task_key, Layer.TASK))
        for key, layer in expected:
            entry = BY_KEY.get(key)
            if entry is None or entry.layer is not layer:
                raise InputValidationError(
                    AI_PROMPT_PREVIEW_INVALID,
                    f"{key} is not a {layer.value} entry",
                    details={"key": key},
                )
        global_key = next(entry.key for entry in CATALOG if entry.layer is Layer.GLOBAL)
        records = {
            key: self._store.entry(key) for key in (global_key, role_key, policy_key, task_key)
        }
        return PreviewView(
            role_key=role_key,
            policy_key=policy_key,
            task_key=task_key,
            revisions={key: record.current.revision_id for key, record in records.items()},
            text=compose(
                records[global_key].current.content["prompt"],
                records[role_key].current.content["prompt"],
                records[policy_key].current.content["prompt"],
                records[task_key].current.content,
            ),
        )


def _require_family(key: str, layer: Literal["template", "policy"]) -> None:
    entry = _catalog(key)
    if (entry.layer is Layer.POLICY) != (layer == "policy"):
        raise NotFoundError(AI_PROMPT_UNKNOWN, f"{key} does not belong to this store")


def _view(record: EntryRecord) -> EntryView:
    entry = BY_KEY[record.key]
    current = record.current
    return EntryView(
        key=entry.key,
        layer=entry.layer,
        title=entry.title,
        description=entry.description,
        screen=entry.screen,
        role_key=entry.role_key,
        editable_fields=list(EDITABLE_FIELDS[entry.layer]),
        content=dict(current.content),
        modified_fields=[
            field
            for field in CONTENT_FIELDS[entry.layer]
            if current.content.get(field) != record.seed.content.get(field)
        ],
        current=_revision_view(current, current=True),
        history=[
            _revision_view(revision, current=revision.revision_id == current.revision_id)
            for revision in record.history
        ],
    )


def _revision_view(record: RevisionRecord, *, current: bool) -> RevisionView:
    return RevisionView(
        revision_id=record.revision_id,
        revision_no=record.revision_no,
        origin=record.origin,
        content_fingerprint=record.content_fingerprint,
        authored_by=record.authored_by,
        authored_at=record.authored_at,
        current=current,
    )
