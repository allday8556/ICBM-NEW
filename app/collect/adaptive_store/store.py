"""The Adaptive profile and validation persistence owner (ADR-0017 §3, §7; P2).

Two stores, one owner of the six migration-0024 tables:

``AdaptiveProfileStore``
    Saves immutable, content-addressed ``PageTemplateRevision`` and ``ExtractionProfileRevision``
    documents with their lineage, only for a registered supplier. Saving an EPR records its pins
    and opens its lifecycle at ``DRAFT``; ``withdraw_shadow`` and ``retire`` are its further
    designations, and ``AdaptiveValidationStore.designate_shadow`` the one that needs a PASS. Every
    read recomputes the digest from the stored document and refuses a mismatch, and a bundle is
    loaded only through ``resolve_bundle``, which recomputes every pinned digest again.

``AdaptiveValidationStore``
    Saves operator-captured ``ValidationSample`` material in this local database only, re-checking
    its digest and its final safety scan; records validation runs with their exact freshness tuple
    and run digest; and derives ``VALIDATED`` — never stored — from a ``PASS`` run for the exact
    current freshness. A sample is retained while any run references it.

``SHADOW`` is only a designation (ADR-0017 §7.1). It grants nothing by itself: a shadow run also
needs ``VALIDATED`` at run time and the per-supplier shadow switch, which a later slice owns. A
designated EPR whose ``VALIDATED`` lapses keeps its designation and is not eligible.

Nothing here reads a supplier, writes a ``ProductFactsRevision``, activates a profile or runs a
shadow. It writes no audit event: its own append-only tables are the record, and an audit event
would move the review owners' write fence (``AuditLog.owner_writes``) for no owner change.
"""

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collect.adaptive.canonical import NonFiniteValue, canonical_json, parse_json
from app.collect.adaptive.capture import (
    CAPTURE_REVISION,
    ValidationSample,
    final_scan,
    sample_digest,
)
from app.collect.adaptive.extraction_identity import EXTRACTOR_FINGERPRINT, EXTRACTOR_REVISION
from app.collect.adaptive.profiles import (
    SCHEMA_VERSION,
    Bundle,
    BundleRefused,
    ExtractionProfileRevision,
    PageTemplateRevision,
    Profile,
    profile_digest,
    profile_document,
    resolve_bundle,
)
from app.collect.adaptive.validation import (
    Check,
    ValidationRun,
    Verdict,
    is_validated,
    sample_set_digest,
)
from app.collect.adaptive_store.gate import SupplierGate
from app.collect.adaptive_store.models import (
    NOTE_MAX_CHARS,
    AdaptiveProfilePin,
    AdaptiveProfileRevision,
    AdaptiveProfileTransition,
    AdaptiveValidationRun,
    AdaptiveValidationRunSample,
    AdaptiveValidationSample,
)
from app.core.clock import Clock
from app.core.errors import AppError, ErrorClass, InputValidationError, NotFoundError
from app.db.database import Database

ADAPTIVE_SUPPLIER_NOT_REGISTERED = "ADAPTIVE_SUPPLIER_NOT_REGISTERED"
ADAPTIVE_PROFILE_INVALID = "ADAPTIVE_PROFILE_INVALID"
ADAPTIVE_PROFILE_NOT_FOUND = "ADAPTIVE_PROFILE_NOT_FOUND"
ADAPTIVE_TEMPLATE_MISSING = "ADAPTIVE_TEMPLATE_MISSING"
ADAPTIVE_LINEAGE_INVALID = "ADAPTIVE_LINEAGE_INVALID"
ADAPTIVE_LIFECYCLE_REFUSED = "ADAPTIVE_LIFECYCLE_REFUSED"
ADAPTIVE_SAMPLE_REFUSED = "ADAPTIVE_SAMPLE_REFUSED"
ADAPTIVE_SAMPLE_NOT_FOUND = "ADAPTIVE_SAMPLE_NOT_FOUND"
ADAPTIVE_RUN_REFUSED = "ADAPTIVE_RUN_REFUSED"
ADAPTIVE_TAMPERED = "ADAPTIVE_TAMPERED"

Kind = Literal["EXTRACTION_PROFILE", "PAGE_TEMPLATE"]
Origin = Literal["OPERATOR", "AI_PROPOSAL", "IMPORT"]
State = Literal["DRAFT", "SHADOW", "RETIRED"]
# Each designation, and the states it may follow (ADR-0017 §7.1). RETIRED is final.
_FOLLOWS: dict[State, frozenset[State]] = {
    "SHADOW": frozenset({"DRAFT"}),
    "DRAFT": frozenset({"SHADOW"}),
    "RETIRED": frozenset({"DRAFT", "SHADOW"}),
}


class AdaptiveTampered(AppError):
    """A stored row does not recompute to what it claims: nothing is read from it (fail closed)."""

    error_class = ErrorClass.FATAL


@dataclass(frozen=True)
class ProfileRecord:
    digest: str
    kind: Kind
    supplier_key: str
    parent_digest: str | None
    origin: Origin
    change_note: str | None
    created_by: str
    correlation_id: str
    created_at: datetime


@dataclass(frozen=True)
class TransitionRecord:
    seq: int
    from_state: State | None
    to_state: State
    reason: str
    actor: str
    correlation_id: str
    occurred_at: datetime


@dataclass(frozen=True)
class StoredRun:
    run_id: str
    run: ValidationRun
    sample_digests: tuple[str, ...]
    recorded_by: str
    recorded_at: datetime


def _invalid(code: str, message: str, **details: Any) -> InputValidationError:
    return InputValidationError(code, message, details=details or None)


def _text(document: str | Mapping[str, Any]) -> str:
    return document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)


def _parse(document: str | Mapping[str, Any], model: type[Profile]) -> Profile:
    text = _text(document)
    try:
        parse_json(text)  # non-finite numbers are refused before the model sees the text
        return model.model_validate_json(text)
    except (ValidationError, NonFiniteValue, ValueError) as error:
        raise _invalid(ADAPTIVE_PROFILE_INVALID, f"not a valid {model.__name__}") from error


def _profile_record(row: AdaptiveProfileRevision) -> ProfileRecord:
    return ProfileRecord(
        digest=row.digest,
        kind=row.kind,  # type: ignore[arg-type]
        supplier_key=row.supplier_key,
        parent_digest=row.parent_digest,
        origin=row.origin,  # type: ignore[arg-type]
        change_note=row.change_note,
        created_by=row.created_by,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
    )


def _transition_record(row: AdaptiveProfileTransition) -> TransitionRecord:
    return TransitionRecord(
        seq=row.seq,
        from_state=row.from_state,  # type: ignore[arg-type]
        to_state=row.to_state,  # type: ignore[arg-type]
        reason=row.reason,
        actor=row.actor,
        correlation_id=row.correlation_id,
        occurred_at=row.occurred_at,
    )


def _verified(row: AdaptiveProfileRevision) -> Profile:
    """The row's document, parsed, only if it recomputes to the row's own digest and columns."""
    model: type[Profile] = (
        ExtractionProfileRevision if row.kind == "EXTRACTION_PROFILE" else PageTemplateRevision
    )
    try:
        profile = _parse(row.document, model)
    except InputValidationError as error:
        raise AdaptiveTampered(ADAPTIVE_TAMPERED, "a stored profile no longer parses") from error
    if (
        profile_digest(profile) != row.digest
        or profile.supplier_key != row.supplier_key
        or profile.schema_version != row.schema_version
    ):
        raise AdaptiveTampered(
            ADAPTIVE_TAMPERED,
            "a stored profile does not recompute to its digest",
            details={"digest": row.digest},
        )
    return profile


class AdaptiveProfileStore:
    """The only writer of the profile revision, pin and lifecycle tables."""

    def __init__(self, db: Database, clock: Clock, supplier_gate: SupplierGate) -> None:
        self._db = db
        self._clock = clock
        self._admits = supplier_gate

    # -------------------------------------------------------------- writes

    def save_template(
        self,
        document: str | Mapping[str, Any],
        *,
        created_by: str,
        correlation_id: str,
        origin: Origin = "OPERATOR",
        parent_digest: str | None = None,
        change_note: str | None = None,
    ) -> str:
        template = _parse(document, PageTemplateRevision)
        assert isinstance(template, PageTemplateRevision)
        return self._save(
            template, created_by, correlation_id, origin, parent_digest, change_note, pins=()
        )

    def save_draft(
        self,
        document: str | Mapping[str, Any],
        *,
        created_by: str,
        correlation_id: str,
        origin: Origin = "OPERATOR",
        parent_digest: str | None = None,
        change_note: str | None = None,
    ) -> str:
        """Save an EPR as ``DRAFT``. Every template it pins must already be stored, verify, and
        resolve with it into a bundle; otherwise nothing is written."""
        epr = _parse(document, ExtractionProfileRevision)
        assert isinstance(epr, ExtractionProfileRevision)
        self._gate(epr.supplier_key)
        templates: dict[str, str] = {}
        for pinned in epr.templates:
            try:
                template = self.revision(pinned)
            except NotFoundError:
                raise _invalid(
                    ADAPTIVE_TEMPLATE_MISSING, "the EPR pins a template that is not stored"
                ) from None
            templates[pinned] = profile_document(template)
        try:
            resolve_bundle(profile_document(epr), templates)
        except BundleRefused as refused:
            raise _invalid(ADAPTIVE_PROFILE_INVALID, str(refused)) from None
        return self._save(
            epr,
            created_by,
            correlation_id,
            origin,
            parent_digest,
            change_note,
            pins=epr.templates,
        )

    def _gate(self, supplier_key: str) -> None:
        if not self._admits(supplier_key):
            raise _invalid(
                ADAPTIVE_SUPPLIER_NOT_REGISTERED,
                "a profile exists only for a supplier with a registered CONNECT definition and "
                "access envelope",
                supplier_key=supplier_key,
            )

    def _save(
        self,
        profile: Profile,
        created_by: str,
        correlation_id: str,
        origin: Origin,
        parent_digest: str | None,
        change_note: str | None,
        *,
        pins: Sequence[str],
    ) -> str:
        self._gate(profile.supplier_key)
        if change_note is not None and not 1 <= len(change_note) <= NOTE_MAX_CHARS:
            raise _invalid(ADAPTIVE_PROFILE_INVALID, "a change note has 1 to 500 characters")
        digest = profile_digest(profile)
        if parent_digest is not None:
            parent = self.record(parent_digest)
            if parent.kind != profile.kind or parent.supplier_key != profile.supplier_key:
                raise _invalid(
                    ADAPTIVE_LINEAGE_INVALID, "a parent is a revision of the same kind and supplier"
                )
            if parent_digest == digest:
                raise _invalid(ADAPTIVE_LINEAGE_INVALID, "a revision is never its own parent")
        now = self._clock.now()
        with self._db.write() as session:
            existing = session.get(AdaptiveProfileRevision, digest)
            if existing is not None:
                # Same digest, same content: the first write stands and nothing changes.
                _verified(existing)
                return digest
            session.add(
                AdaptiveProfileRevision(
                    digest=digest,
                    kind=profile.kind,
                    supplier_key=profile.supplier_key,
                    schema_version=profile.schema_version,
                    document=profile_document(profile),
                    parent_digest=parent_digest,
                    origin=origin,
                    change_note=change_note,
                    created_by=created_by,
                    correlation_id=correlation_id,
                    created_at=now,
                )
            )
            session.flush()
            if isinstance(profile, ExtractionProfileRevision):
                for position, pinned in enumerate(pins):
                    session.add(
                        AdaptiveProfilePin(epr_digest=digest, position=position, ptr_digest=pinned)
                    )
                session.flush()
                self._append(session, digest, 1, None, "DRAFT", "SAVED", created_by, correlation_id)
        return digest

    def _append(
        self,
        session: Session,
        epr_digest: str,
        seq: int,
        from_state: State | None,
        to_state: State,
        reason: str,
        actor: str,
        correlation_id: str,
    ) -> None:
        session.add(
            AdaptiveProfileTransition(
                transition_id=str(uuid.uuid4()),
                epr_digest=epr_digest,
                seq=seq,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                actor=actor,
                correlation_id=correlation_id,
                occurred_at=self._clock.now(),
            )
        )
        session.flush()

    def retire(self, epr_digest: str, *, actor: str, reason: str, correlation_id: str) -> None:
        """``DRAFT`` or ``SHADOW`` → ``RETIRED``, final. A retired EPR is kept and readable, and is
        never VALIDATED again."""
        self._transition(epr_digest, "RETIRED", actor, reason, correlation_id)

    def withdraw_shadow(
        self, epr_digest: str, *, actor: str, reason: str, correlation_id: str
    ) -> None:
        """``SHADOW → DRAFT``: the designation is withdrawn; nothing else changes."""
        self._transition(epr_digest, "DRAFT", actor, reason, correlation_id)

    def _designate_shadow(
        self, epr_digest: str, *, actor: str, reason: str, correlation_id: str
    ) -> None:
        # Only AdaptiveValidationStore.designate_shadow calls this, after it derived VALIDATED.
        self._transition(epr_digest, "SHADOW", actor, reason, correlation_id)

    def _transition(
        self, epr_digest: str, to_state: State, actor: str, reason: str, correlation_id: str
    ) -> None:
        self._epr_row(epr_digest)
        with self._db.write() as session:
            last = session.scalars(
                select(AdaptiveProfileTransition)
                .where(AdaptiveProfileTransition.epr_digest == epr_digest)
                .order_by(AdaptiveProfileTransition.seq.desc())
                .limit(1)
            ).first()
            if last is None:
                raise AdaptiveTampered(ADAPTIVE_TAMPERED, "an EPR without a lifecycle")
            from_state: State = last.to_state  # type: ignore[assignment]
            if from_state not in _FOLLOWS[to_state]:
                raise _invalid(
                    ADAPTIVE_LIFECYCLE_REFUSED,
                    f"an EPR in {from_state} is never moved to {to_state}",
                    from_state=from_state,
                    to_state=to_state,
                )
            self._append(
                session,
                epr_digest,
                last.seq + 1,
                from_state,
                to_state,
                reason,
                actor,
                correlation_id,
            )

    # -------------------------------------------------------------- reads

    def _row(self, digest: str) -> AdaptiveProfileRevision:
        with self._db.read() as session:
            row = session.get(AdaptiveProfileRevision, digest)
            if row is None:
                raise NotFoundError(ADAPTIVE_PROFILE_NOT_FOUND, "no such profile revision")
            session.expunge(row)
            return row

    def _epr_row(self, digest: str) -> AdaptiveProfileRevision:
        row = self._row(digest)
        if row.kind != "EXTRACTION_PROFILE":
            raise _invalid(ADAPTIVE_PROFILE_INVALID, "not an ExtractionProfileRevision")
        return row

    def record(self, digest: str) -> ProfileRecord:
        row = self._row(digest)
        _verified(row)
        return _profile_record(row)

    def revision(self, digest: str) -> Profile:
        return _verified(self._row(digest))

    def revisions(self, supplier_key: str, kind: Kind | None = None) -> tuple[ProfileRecord, ...]:
        with self._db.read() as session:
            query = (
                select(AdaptiveProfileRevision)
                .where(AdaptiveProfileRevision.supplier_key == supplier_key)
                .order_by(AdaptiveProfileRevision.created_at, AdaptiveProfileRevision.digest)
            )
            if kind is not None:
                query = query.where(AdaptiveProfileRevision.kind == kind)
            rows = list(session.scalars(query))
        for row in rows:
            _verified(row)
        return tuple(_profile_record(row) for row in rows)

    def lineage(self, digest: str) -> tuple[str, ...]:
        """This revision and its ancestors, newest first."""
        chain: list[str] = []
        current: str | None = digest
        while current is not None:
            if current in chain:
                raise AdaptiveTampered(ADAPTIVE_TAMPERED, "a lineage never loops")
            chain.append(current)
            current = self.record(current).parent_digest
        return tuple(chain)

    def lifecycle(self, epr_digest: str) -> tuple[TransitionRecord, ...]:
        self._epr_row(epr_digest)
        with self._db.read() as session:
            rows = session.scalars(
                select(AdaptiveProfileTransition)
                .where(AdaptiveProfileTransition.epr_digest == epr_digest)
                .order_by(AdaptiveProfileTransition.seq)
            ).all()
            return tuple(_transition_record(row) for row in rows)

    def state(self, epr_digest: str) -> State:
        history = self.lifecycle(epr_digest)
        if not history:
            raise AdaptiveTampered(ADAPTIVE_TAMPERED, "an EPR without a lifecycle")
        return history[-1].to_state

    def load_bundle(self, epr_digest: str) -> Bundle:
        """The offline bundle of a stored EPR, every digest recomputed (tamper fails closed)."""
        row = self._epr_row(epr_digest)
        epr = _verified(row)
        assert isinstance(epr, ExtractionProfileRevision)
        with self._db.read() as session:
            pins = session.scalars(
                select(AdaptiveProfilePin)
                .where(AdaptiveProfilePin.epr_digest == epr_digest)
                .order_by(AdaptiveProfilePin.position)
            ).all()
            pinned = tuple(pin.ptr_digest for pin in pins)
        if pinned != epr.templates:
            raise AdaptiveTampered(ADAPTIVE_TAMPERED, "the stored pins disagree with the EPR")
        documents = {digest: profile_document(self.revision(digest)) for digest in pinned}
        try:
            bundle = resolve_bundle(row.document, documents)
        except BundleRefused as refused:
            raise AdaptiveTampered(ADAPTIVE_TAMPERED, str(refused)) from None
        if bundle.epr_digest != epr_digest:
            raise AdaptiveTampered(ADAPTIVE_TAMPERED, "the bundle does not recompute to its EPR")
        return bundle


def _checks_json(run: ValidationRun) -> str:
    return canonical_json([[c.name, c.outcome.value, list(c.details)] for c in run.checks])


def _run_from(row: AdaptiveValidationRun) -> ValidationRun:
    checks = tuple(
        Check(name, Verdict(outcome), tuple(details))
        for name, outcome, details in json.loads(row.checks_json)
    )
    freshness = (
        row.epr_digest,
        row.profile_schema_version,
        row.extractor_revision,
        row.extractor_fingerprint,
        row.hook_fingerprint,
        row.sample_set_digest,
        row.capture_revision,
    )
    run = ValidationRun(Verdict(row.verdict), checks, freshness)
    if run.digest() != row.run_digest:
        raise AdaptiveTampered(ADAPTIVE_TAMPERED, "a stored validation run does not recompute")
    return run


class AdaptiveValidationStore:
    """The only writer of the validation sample, run and run-sample tables."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        supplier_gate: SupplierGate,
        profiles: AdaptiveProfileStore,
    ) -> None:
        self._db = db
        self._clock = clock
        self._admits = supplier_gate
        self._profiles = profiles

    # -------------------------------------------------------------- samples

    def save_sample(
        self,
        sample: ValidationSample,
        *,
        supplier_key: str,
        stored_by: str,
        correlation_id: str,
    ) -> str:
        """Keep one sample in this local database, only if it is exactly what its digest says and
        still passes the capture's final safety scan."""
        if not self._admits(supplier_key):
            raise _invalid(ADAPTIVE_SUPPLIER_NOT_REGISTERED, "not a registered supplier")
        structure, expected, provenance = (
            sample.structure,
            sample.expected,
            sample.provenance,
        )
        recomputed = sample_digest(structure, expected, provenance, sample.truncated)
        if recomputed != sample.digest:
            raise _invalid(ADAPTIVE_SAMPLE_REFUSED, "the sample does not recompute to its digest")
        if residual := final_scan(structure):
            raise _invalid(ADAPTIVE_SAMPLE_REFUSED, f"residual private material: {residual}")
        if "profile" in canonical_json(provenance).lower():
            raise _invalid(ADAPTIVE_SAMPLE_REFUSED, "a sample's provenance never names a profile")
        with self._db.write() as session:
            if session.get(AdaptiveValidationSample, sample.digest) is None:
                session.add(
                    AdaptiveValidationSample(
                        sample_digest=sample.digest,
                        supplier_key=supplier_key,
                        capture_revision=str(provenance["capture_revision"]),
                        truncated=sample.truncated,
                        structure_json=sample.structure_json,
                        expected_json=sample.expected_json,
                        provenance_json=sample.provenance_json,
                        stored_by=stored_by,
                        correlation_id=correlation_id,
                        stored_at=self._clock.now(),
                    )
                )
        return sample.digest

    def sample(self, digest: str) -> ValidationSample:
        with self._db.read() as session:
            row = session.get(AdaptiveValidationSample, digest)
            if row is None:
                raise NotFoundError(ADAPTIVE_SAMPLE_NOT_FOUND, "no such validation sample")
            stored = ValidationSample(
                row.structure_json, row.expected_json, row.provenance_json, row.truncated, digest
            )
        if sample_digest(
            stored.structure, stored.expected, stored.provenance, stored.truncated
        ) != digest or final_scan(stored.structure):
            raise AdaptiveTampered(ADAPTIVE_TAMPERED, "a stored sample does not recompute")
        return stored

    def prune_unreferenced_samples(self) -> int:
        """Retention: a sample is kept while any run references it, and only then (ADR-0017
        §7.3). Returns how many unreferenced samples were removed."""
        with self._db.write() as session:
            referenced = select(AdaptiveValidationRunSample.sample_digest)
            rows = session.scalars(
                select(AdaptiveValidationSample).where(
                    AdaptiveValidationSample.sample_digest.not_in(referenced)
                )
            ).all()
            for row in rows:
                session.delete(row)
            return len(rows)

    # -------------------------------------------------------------- runs

    def record_run(
        self,
        bundle: Bundle,
        run: ValidationRun,
        samples: Sequence[ValidationSample],
        *,
        recorded_by: str,
        correlation_id: str,
    ) -> str:
        """Persist one validation run of a stored, unretired EPR, bound to its exact freshness. The
        same run recorded again (same verdict, checks and freshness) returns the first record."""
        stored = self._profiles.load_bundle(bundle.epr_digest)
        if stored.epr_digest != bundle.epr_digest or run.freshness[0] != bundle.epr_digest:
            raise _invalid(ADAPTIVE_RUN_REFUSED, "a run is recorded against its own stored EPR")
        if self._profiles.state(bundle.epr_digest) == "RETIRED":
            raise _invalid(ADAPTIVE_RUN_REFUSED, "a retired EPR is never validated again")
        if len(run.freshness) != 7 or run.freshness[5] != sample_set_digest(samples):
            raise _invalid(ADAPTIVE_RUN_REFUSED, "the run's freshness names other samples")
        for sample in samples:
            if self.sample(sample.digest).digest != sample.digest:
                raise _invalid(ADAPTIVE_RUN_REFUSED, "every replayed sample is stored first")
        run_id = str(uuid.uuid4())
        _, schema, extractor, fingerprint, hook, sample_set, capture = run.freshness
        with self._db.write() as session:
            existing = session.scalars(
                select(AdaptiveValidationRun).where(
                    AdaptiveValidationRun.run_digest == run.digest()
                )
            ).first()
            if existing is not None:
                # The freshness names the samples, so nothing differs: the first record stands.
                _run_from(existing)
                return existing.run_id
            session.add(
                AdaptiveValidationRun(
                    run_id=run_id,
                    epr_digest=bundle.epr_digest,
                    verdict=run.verdict.value,
                    profile_schema_version=schema,
                    extractor_revision=extractor,
                    extractor_fingerprint=fingerprint,
                    hook_fingerprint=hook,
                    sample_set_digest=sample_set,
                    capture_revision=capture,
                    checks_json=_checks_json(run),
                    run_digest=run.digest(),
                    recorded_by=recorded_by,
                    correlation_id=correlation_id,
                    recorded_at=self._clock.now(),
                )
            )
            session.flush()
            for position, sample in enumerate(samples):
                session.add(
                    AdaptiveValidationRunSample(
                        run_id=run_id, position=position, sample_digest=sample.digest
                    )
                )
        return run_id

    def runs(self, epr_digest: str) -> tuple[StoredRun, ...]:
        with self._db.read() as session:
            rows = session.scalars(
                select(AdaptiveValidationRun)
                .where(AdaptiveValidationRun.epr_digest == epr_digest)
                .order_by(AdaptiveValidationRun.recorded_at, AdaptiveValidationRun.run_id)
            ).all()
            stored: list[StoredRun] = []
            for row in rows:
                digests = tuple(
                    session.scalars(
                        select(AdaptiveValidationRunSample.sample_digest)
                        .where(AdaptiveValidationRunSample.run_id == row.run_id)
                        .order_by(AdaptiveValidationRunSample.position)
                    )
                )
                stored.append(
                    StoredRun(row.run_id, _run_from(row), digests, row.recorded_by, row.recorded_at)
                )
        return tuple(stored)

    def is_validated(self, epr_digest: str, current_freshness: Iterable[str]) -> bool:
        """Derived, never stored: a PASS run for exactly the current freshness tuple, of an EPR
        that is not retired."""
        if self._profiles.state(epr_digest) == "RETIRED":
            return False
        return is_validated(
            (stored.run for stored in self.runs(epr_digest)), tuple(current_freshness)
        )

    def designate_shadow(
        self,
        epr_digest: str,
        current_freshness: Sequence[str],
        *,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> None:
        """``DRAFT → SHADOW``, only while the EPR is VALIDATED for a freshness tuple that names it
        and this running engine. The designation alone permits no shadow run (module docstring)."""
        current = tuple(current_freshness)
        running = (SCHEMA_VERSION, EXTRACTOR_REVISION, EXTRACTOR_FINGERPRINT)
        if (
            len(current) != 7
            or current[0] != epr_digest
            or current[1:4] != running
            or current[6] != CAPTURE_REVISION
        ):
            raise _invalid(
                ADAPTIVE_LIFECYCLE_REFUSED,
                "a SHADOW designation names this EPR and the running engine's freshness",
            )
        if not self.is_validated(epr_digest, current):
            raise _invalid(ADAPTIVE_LIFECYCLE_REFUSED, "only a VALIDATED EPR is designated SHADOW")
        self._profiles._designate_shadow(
            epr_digest, actor=actor, reason=reason, correlation_id=correlation_id
        )
