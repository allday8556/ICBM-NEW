"""The Adaptive profile and validation persistence owner (ADR-0017 §3, §7; P2).

Two stores, one owner of the seven migration-0024 tables:

``AdaptiveProfileStore``
    Saves immutable, content-addressed ``PageTemplateRevision`` and ``ExtractionProfileRevision``
    documents with their lineage, only for a registered supplier. Saving an EPR records its pins
    and its DRAFT lint findings and opens its lifecycle at ``DRAFT``; ``retire`` is the one further
    transition P2 performs. Every read recomputes the digest from the stored document and refuses
    a mismatch, and a bundle is loaded only through ``resolve_bundle``, which recomputes every
    pinned digest again.

``AdaptiveValidationStore``
    Saves operator-captured ``ValidationSample`` material in this local database only, re-checking
    its digest and its final safety scan; records validation runs with their exact freshness tuple
    and run digest; and derives ``VALIDATED`` — never stored — from a ``PASS`` run for the exact
    current freshness. A sample belongs to one supplier and is retained while any run references
    it; every read of a run recomputes its ordered sample set from the linked sample rows.

Nothing here enters ``SHADOW`` on its own: ADR-0017 §7.1 requires ``VALIDATED`` plus the
per-supplier shadow switch (review ``5311392575`` B1). Only the shadow-switch owner
(``app.collect.adaptive_shadow.switch``, P3) moves an EPR into or out of ``SHADOW``, inside the same
write unit as the switch entry that justifies it, and the database refuses any other way in.

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
from app.collect.adaptive.capture import ValidationSample, final_scan, sample_digest
from app.collect.adaptive.lint import LINT_REVISION, draft_lint, lint_digest
from app.collect.adaptive.profiles import (
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
    AdaptiveProfileLint,
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
# The transitions this store performs on its own, and the states each may follow. RETIRED is
# final. There is no entry into SHADOW here (module docstring).
_FOLLOWS: dict[State, frozenset[State]] = {"RETIRED": frozenset({"DRAFT", "SHADOW"})}
# The transitions only the shadow-switch owner performs, inside its own write unit, as its switch
# history moves (ADR-0017 §7.1: SHADOW is VALIDATED plus the per-supplier shadow switch; P3).
_SWITCH_FOLLOWS: dict[State, frozenset[State]] = {
    "SHADOW": frozenset({"DRAFT"}),
    "DRAFT": frozenset({"SHADOW"}),
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
class LintRecord:
    """The lint findings recorded when an EPR entered DRAFT, and the rule set that produced them."""

    lint_revision: str
    findings: tuple[str, ...]
    recorded_at: datetime


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
        """Save an EPR as ``DRAFT``, with its lint findings recorded (ADR-0017 §7.1). Every
        template it pins must already be stored, verify, and resolve with it into a bundle;
        otherwise nothing is written. Lint never refuses a DRAFT."""
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
            bundle = resolve_bundle(profile_document(epr), templates)
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
            lint=draft_lint(bundle),
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
        lint: tuple[str, ...] = (),
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
                session.add(
                    AdaptiveProfileLint(
                        epr_digest=digest,
                        lint_revision=LINT_REVISION,
                        findings_json=canonical_json(list(lint)),
                        lint_digest=lint_digest(LINT_REVISION, lint),
                        recorded_at=now,
                    )
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

    def _transition(
        self, epr_digest: str, to_state: State, actor: str, reason: str, correlation_id: str
    ) -> None:
        self._epr_row(epr_digest)
        with self._db.write() as session:
            self._move(session, epr_digest, to_state, actor, reason, correlation_id, _FOLLOWS)

    def _switch_transition(
        self,
        session: Session,
        epr_digest: str,
        to_state: State,
        *,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> None:
        """``DRAFT → SHADOW`` or ``SHADOW → DRAFT`` inside the shadow-switch owner's own write unit,
        which has just appended the switch entry that justifies it. Nothing else calls this."""
        self._move(session, epr_digest, to_state, actor, reason, correlation_id, _SWITCH_FOLLOWS)

    def _move(
        self,
        session: Session,
        epr_digest: str,
        to_state: State,
        actor: str,
        reason: str,
        correlation_id: str,
        follows: dict[State, frozenset[State]],
    ) -> None:
        last = session.scalars(
            select(AdaptiveProfileTransition)
            .where(AdaptiveProfileTransition.epr_digest == epr_digest)
            .order_by(AdaptiveProfileTransition.seq.desc())
            .limit(1)
        ).first()
        if last is None:
            raise AdaptiveTampered(ADAPTIVE_TAMPERED, "an EPR without a lifecycle")
        from_state: State = last.to_state  # type: ignore[assignment]
        if from_state not in follows.get(to_state, frozenset()):
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

    def lint(self, epr_digest: str) -> LintRecord:
        """The lint recorded when this EPR entered DRAFT, verified against its digest and, under
        the running rule set, recomputed from the stored bundle (tamper fails closed)."""
        bundle = self.load_bundle(epr_digest)
        with self._db.read() as session:
            row = session.get(AdaptiveProfileLint, epr_digest)
            if row is None:
                raise AdaptiveTampered(ADAPTIVE_TAMPERED, "a DRAFT EPR without its recorded lint")
            session.expunge(row)
        try:
            loaded = json.loads(row.findings_json)
        except ValueError:
            raise AdaptiveTampered(ADAPTIVE_TAMPERED, "stored lint no longer parses") from None
        findings = tuple(loaded) if isinstance(loaded, list) else ()
        if (
            not isinstance(loaded, list)
            or not all(isinstance(item, str) for item in findings)
            or findings != tuple(sorted(set(findings)))
            or lint_digest(row.lint_revision, findings) != row.lint_digest
            or (row.lint_revision == LINT_REVISION and findings != draft_lint(bundle))
        ):
            raise AdaptiveTampered(ADAPTIVE_TAMPERED, "stored lint does not recompute")
        return LintRecord(row.lint_revision, findings, row.recorded_at)

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
            existing = session.get(AdaptiveValidationSample, sample.digest)
            if existing is not None and existing.supplier_key != supplier_key:
                raise _invalid(
                    ADAPTIVE_SAMPLE_REFUSED,
                    "a sample belongs to one supplier and is never reused for another",
                    supplier_key=supplier_key,
                )
            if existing is None:
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
        return self._stored_sample(digest)[0]

    def sample_supplier(self, digest: str) -> str:
        """The one supplier a stored sample belongs to."""
        return self._stored_sample(digest)[1]

    def _stored_sample(self, digest: str) -> tuple[ValidationSample, str]:
        with self._db.read() as session:
            row = session.get(AdaptiveValidationSample, digest)
            if row is None:
                raise NotFoundError(ADAPTIVE_SAMPLE_NOT_FOUND, "no such validation sample")
            stored = ValidationSample(
                row.structure_json, row.expected_json, row.provenance_json, row.truncated, digest
            )
            supplier_key = row.supplier_key
        if sample_digest(
            stored.structure, stored.expected, stored.provenance, stored.truncated
        ) != digest or final_scan(stored.structure):
            raise AdaptiveTampered(ADAPTIVE_TAMPERED, "a stored sample does not recompute")
        return stored, supplier_key

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
            if self.sample_supplier(sample.digest) != stored.epr.supplier_key:
                raise _invalid(
                    ADAPTIVE_RUN_REFUSED, "a run replays only samples of its own EPR's supplier"
                )
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
                    sample_count=len(samples),
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
        """Every run of this EPR. Each is rebuilt from its row and its run digest, and its ordered
        sample set is recomputed from the linked sample rows: a missing, extra or reordered link,
        or a linked sample of another supplier, fails closed."""
        with self._db.read() as session:
            owner = session.get(AdaptiveProfileRevision, epr_digest)
            rows = session.scalars(
                select(AdaptiveValidationRun)
                .where(AdaptiveValidationRun.epr_digest == epr_digest)
                .order_by(AdaptiveValidationRun.recorded_at, AdaptiveValidationRun.run_id)
            ).all()
            linked: list[tuple[AdaptiveValidationRun, list[tuple[int, str]]]] = []
            for row in rows:
                session.expunge(row)
                found = session.execute(
                    select(
                        AdaptiveValidationRunSample.position,
                        AdaptiveValidationRunSample.sample_digest,
                    )
                    .where(AdaptiveValidationRunSample.run_id == row.run_id)
                    .order_by(AdaptiveValidationRunSample.position)
                ).all()
                linked.append((row, [(position, digest) for position, digest in found]))
        stored: list[StoredRun] = []
        for row, links in linked:
            run = _run_from(row)
            digests = tuple(digest for _, digest in links)
            try:
                replayed = [self._stored_sample(digest) for digest in digests]
            except NotFoundError:
                raise AdaptiveTampered(
                    ADAPTIVE_TAMPERED, "a stored run links a sample that is not stored"
                ) from None
            if (
                owner is None
                or [position for position, _ in links] != list(range(row.sample_count))
                or sample_set_digest([sample for sample, _ in replayed]) != row.sample_set_digest
                or any(supplier != owner.supplier_key for _, supplier in replayed)
            ):
                raise AdaptiveTampered(
                    ADAPTIVE_TAMPERED,
                    "a stored run's linked samples do not recompute to its sample set",
                    details={"run_id": row.run_id},
                )
            stored.append(StoredRun(row.run_id, run, digests, row.recorded_by, row.recorded_at))
        return tuple(stored)

    def is_validated(self, epr_digest: str, current_freshness: Iterable[str]) -> bool:
        """Derived, never stored: a PASS run for exactly the current freshness tuple, of an EPR
        that is not retired."""
        if self._profiles.state(epr_digest) == "RETIRED":
            return False
        return is_validated(
            (stored.run for stored in self.runs(epr_digest)), tuple(current_freshness)
        )
