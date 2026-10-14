"""The supplier onboarding owner's store (ADR-0036 §3; OB-01, OB-03, OB-04, OB-05).

``OnboardingStore`` is the only writer of the onboarding tables. Every write is one write unit
that appends rows and one audit event; nothing is updated or deleted, and the database refuses it
if anything tries. An audit event names keys, states, hosts, roles, sequence numbers and
fingerprints only: never a credential, a word, a reason the operator typed or page text (§9).

- ``create`` writes the identity, the first state ``DRAFT`` and the bootstrap storefront host,
  the origin's own host, which passes the host guard first (OB-01). Since O2 it also writes, in
  the same unit, the policy reads the operator was shown and acknowledged (§4 step 3), and audits
  the detected and the confirmed platform (step 2) by name only.
- ``transition`` appends one allowed edge of the state log (§3, §7). ``RECON → ACTIVE`` is
  appended only when the trials and verifications prove it (§7 D2, OB-04, ``activation``); the
  database refuses an unproven ``ACTIVE`` as well. Verifications have no writer before O5, so no
  site can become ``ACTIVE`` yet.
- ``record_trial`` (O3) appends one trial read of 「사이트 분석」 under the current words revision
  of a ``RECON`` site: never a ``ProductFactsRevision`` (OB-02). Its audit event names the trial,
  its capture's SHA-256, its facts fingerprint and counts, never the page or the sample's values.
- ``save_words`` appends a words revision checked by the site-word rules and the template's own
  slots (OB-03). A words change to an ``ACTIVE`` site appends ``RECON`` in the same unit (OB-04).
- ``confirm_host`` and ``disable_host`` append host rows. A confirmed host other than the
  bootstrap one names the trial of the same supplier that observed it (OB-01).

After a unit commits, every subscriber is told the supplier key, so the running registry rebinds
that one supplier (ADR-0036 §4).
"""

import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, InputValidationError, NotFoundError
from app.platform.db.database import Database
from app.stages.collect.facts import FIELD_REGISTRY, FieldLevel, FieldStatus
from app.stages.collect.onboarding.hosts import onboarding_host, onboarding_origin
from app.stages.collect.onboarding.models import (
    OnboardedSupplier,
    OnboardingHost,
    OnboardingPolicyRead,
    OnboardingTransition,
    OnboardingTrial,
    OnboardingVerification,
    OnboardingWordRevision,
)
from app.stages.collect.onboarding.site import (
    ALLOWED_TRANSITIONS,
    SITE_KEY_PREFIX,
    HostRole,
    OnboardedHost,
    OnboardingRecord,
    OnboardingState,
    WordsRevision,
    onboarded_site,
    words_fingerprint,
    words_json,
)
from integrations.suppliers.platforms import PlatformTemplate
from integrations.suppliers.site_config import SiteConfigError, label_words, plain_word
from integrations.suppliers.sites import TEMPLATES, template_problems

logger = logging.getLogger(__name__)

ONBOARDING_SUPPLIER_UNKNOWN: Final = "ONBOARDING_SUPPLIER_UNKNOWN"
ONBOARDING_NAME_INVALID: Final = "ONBOARDING_NAME_INVALID"
ONBOARDING_TEMPLATE_UNKNOWN: Final = "ONBOARDING_TEMPLATE_UNKNOWN"
ONBOARDING_HOST_TAKEN: Final = "ONBOARDING_HOST_TAKEN"
ONBOARDING_TRANSITION_REFUSED: Final = "ONBOARDING_TRANSITION_REFUSED"
ONBOARDING_SUPPLIER_DISABLED: Final = "ONBOARDING_SUPPLIER_DISABLED"
ONBOARDING_WORDS_INVALID: Final = "ONBOARDING_WORDS_INVALID"
ONBOARDING_WORDS_SLOT_UNKNOWN: Final = "ONBOARDING_WORDS_SLOT_UNKNOWN"
ONBOARDING_WORDS_MOVED: Final = "ONBOARDING_WORDS_MOVED"
ONBOARDING_WORDS_UNCHANGED: Final = "ONBOARDING_WORDS_UNCHANGED"
ONBOARDING_HOST_UNOBSERVED: Final = "ONBOARDING_HOST_UNOBSERVED"
ONBOARDING_HOST_UNCHANGED: Final = "ONBOARDING_HOST_UNCHANGED"
ONBOARDING_HOST_UNKNOWN: Final = "ONBOARDING_HOST_UNKNOWN"
ONBOARDING_ROLE_UNKNOWN: Final = "ONBOARDING_ROLE_UNKNOWN"
ONBOARDING_REASON_INVALID: Final = "ONBOARDING_REASON_INVALID"
ONBOARDING_EVIDENCE_INVALID: Final = "ONBOARDING_EVIDENCE_INVALID"
ONBOARDING_ACTIVATION_NOT_PROVEN: Final = "ONBOARDING_ACTIVATION_NOT_PROVEN"
ONBOARDING_POLICY_READS_INVALID: Final = "ONBOARDING_POLICY_READS_INVALID"
ONBOARDING_NOT_RECON: Final = "ONBOARDING_NOT_RECON"
ONBOARDING_TRIAL_INVALID: Final = "ONBOARDING_TRIAL_INVALID"
ONBOARDING_TRIAL_UNKNOWN: Final = "ONBOARDING_TRIAL_UNKNOWN"
ONBOARDING_TRIAL_NOT_CURRENT: Final = "ONBOARDING_TRIAL_NOT_CURRENT"
ONBOARDING_VERIFICATION_INVALID: Final = "ONBOARDING_VERIFICATION_INVALID"
ONBOARDING_VERIFICATION_STALE: Final = "ONBOARDING_VERIFICATION_STALE"

# ADR-0036 §2 D2: three distinct samples prove a words revision.
ACTIVATION_SAMPLES: Final = 3
# The fields every proving trial reads: the fact registry's CORE fields (ADR-0030 §2).
CORE_FIELDS: Final = tuple(
    key for key, spec in FIELD_REGISTRY.items() if spec.level is FieldLevel.CORE
)
# A core field is read when CONFIRMED. The options field is read when ABSENT as well: that is the
# page's own statement that the product has no options (ADR-0013 ruling B), and no template ever
# confirms an option axis. Every other core field must be CONFIRMED (ADR-0036 §7, D2).
READ_STATUSES: Final[Mapping[str, frozenset[str]]] = {
    field: frozenset({FieldStatus.CONFIRMED.value, FieldStatus.ABSENT.value})
    if field == "options"
    else frozenset({FieldStatus.CONFIRMED.value})
    for field in CORE_FIELDS
}
_VERIFIED: Final = "CORRECT"
_REFUTED: Final = "WRONG"

_SLUG_LIMIT: Final = 28
_REASON_LIMIT: Final = 200
_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EVIDENCE_LIMIT: Final = 32

Listener = Callable[[str], None]


@dataclass(frozen=True)
class ActivationProof:
    """What the trials under the current words revision prove (ADR-0036 §7).

    A trial proves its sample when it ran under the current words revision and template revision,
    read every core field ``CONFIRMED``, has no structural problem, and carries an operator
    verification ``CORRECT`` of exactly the facts it showed and no ``WRONG`` one."""

    words_seq: int
    # The template revision the proof is judged under; an ACTIVE row records it.
    template_revision: str
    # Trials under the current words revision and template revision.
    trials: int
    # The distinct sample URL keys a trial proves, and one verification id for each.
    proven_samples: tuple[str, ...]
    evidence: tuple[str, ...]
    # Those trials that fail each condition (a trial may fail several).
    core_unread: int
    unverified: int
    refuted: int
    structural_open: int

    @property
    def proven(self) -> bool:
        return len(self.proven_samples) >= ACTIVATION_SAMPLES and self.structural_open == 0

    def details(self) -> dict[str, Any]:
        return {
            "words_seq": self.words_seq,
            "template_revision": self.template_revision,
            "required_samples": ACTIVATION_SAMPLES,
            "proven_samples": len(self.proven_samples),
            "trials": self.trials,
            "core_unread": self.core_unread,
            "unverified": self.unverified,
            "refuted": self.refuted,
            "structural_open": self.structural_open,
        }


@dataclass(frozen=True)
class PolicyReadInput:
    """One policy document read shown to the operator at add time (§4 step 3): which document,
    its path, the HTTP status and the SHA-256 of what was read. Never the text itself."""

    document: str
    path: str
    http_status: int
    content_sha256: str | None
    read_at: datetime


@dataclass(frozen=True)
class StoredPolicyRead:
    read_id: str
    document: str
    path: str
    http_status: int
    content_sha256: str | None
    read_at: datetime
    acknowledged_by: str
    acknowledged_at: datetime


@dataclass(frozen=True)
class TrialInput:
    """One trial read's record (ADR-0036 §3 ``onboarding_trials``, §5): names, statuses, codes,
    hosts and hashes. The sample capture itself stays a local file named by its SHA-256."""

    sample_url_key: str
    template_revision: str
    words_seq: int
    capture_sha256: str
    facts_fingerprint: str
    field_statuses: Mapping[str, str]
    unread_rows: tuple[Mapping[str, Any], ...]
    observed_hosts: tuple[Mapping[str, Any], ...]
    structural: tuple[str, ...]


@dataclass(frozen=True)
class StoredTrial:
    trial_id: str
    sample_url_key: str
    template_revision: str
    words_seq: int
    capture_sha256: str
    facts_fingerprint: str
    field_statuses: Mapping[str, str]
    unread_rows: tuple[Mapping[str, Any], ...]
    observed_hosts: tuple[Mapping[str, Any], ...]
    structural: tuple[str, ...]
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class StoredVerification:
    """The operator's verdict on exactly what one trial showed (§7, OB-04)."""

    verification_id: str
    trial_id: str
    shown_fingerprint: str
    verdict: str
    verified_by: str
    verified_at: datetime


_SAMPLE_LIMIT: Final = 512
_STRUCTURAL_CODE = re.compile(r"^[A-Z][A-Z_]{1,47}$")
_TRIAL_ROWS_LIMIT: Final = 64
_TRIAL_HOSTS_LIMIT: Final = 64
_TRIAL_HOST_ROLES: Final = frozenset({"image", "login", "asset"})
_STATUS_VALUES: Final = frozenset(status.value for status in FieldStatus)


def _trial(value: TrialInput) -> TrialInput:
    """``ONBOARDING_TRIAL_INVALID`` unless every part of the record has its closed shape."""
    statuses = dict(value.field_statuses)
    ok = (
        isinstance(value.sample_url_key, str)
        and value.sample_url_key.startswith("https://")
        and len(value.sample_url_key) <= _SAMPLE_LIMIT
        and not any(ch.isspace() for ch in value.sample_url_key)
        and isinstance(value.template_revision, str)
        and 0 < len(value.template_revision) <= 64
        and isinstance(value.words_seq, int)
        and value.words_seq >= 0
        and bool(_HEX64.fullmatch(value.capture_sha256))
        and bool(_HEX64.fullmatch(value.facts_fingerprint))
        and set(statuses) <= set(FIELD_REGISTRY)
        and set(CORE_FIELDS) <= set(statuses)
        and all(v in _STATUS_VALUES for v in statuses.values())
        and len(value.unread_rows) <= _TRIAL_ROWS_LIMIT
        and len(value.observed_hosts) <= _TRIAL_HOSTS_LIMIT
        and all(
            isinstance(h, Mapping)
            and isinstance(h.get("host"), str)
            and h.get("role") in _TRIAL_HOST_ROLES
            for h in value.observed_hosts
        )
        and all(
            isinstance(c, str) and _STRUCTURAL_CODE.fullmatch(c) is not None
            for c in value.structural
        )
    )
    if not ok:
        raise InputValidationError(
            ONBOARDING_TRIAL_INVALID, "a trial record is names, statuses, codes and hashes"
        )
    return value


# ADR-0010 Q1 reconnaissance caps: at most 3 public policy reads per site.
POLICY_READ_CAP: Final = 3
_POLICY_DOCUMENTS: Final = frozenset({"ROBOTS", "TERMS"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _policy_reads(values: Iterable[PolicyReadInput]) -> list[PolicyReadInput]:
    reads = list(values)
    if len(reads) > POLICY_READ_CAP or not all(
        r.document in _POLICY_DOCUMENTS
        and isinstance(r.path, str)
        and r.path.startswith("/")
        and 100 <= r.http_status <= 599
        and (r.content_sha256 is None or _HEX64.fullmatch(r.content_sha256))
        for r in reads
    ):
        raise InputValidationError(
            ONBOARDING_POLICY_READS_INVALID,
            f"at most {POLICY_READ_CAP} policy reads, each a robots or terms document",
        )
    return reads


class OnboardingConflictError(AppError):
    """A write the current record refuses: it moved since it was read, or nothing changes."""

    error_class = ErrorClass.CONFLICT


def _slug(host: str) -> str:
    """The key's slug from the storefront host: ``www.`` dropped, dots as hyphens."""
    name = host.removeprefix("www.")
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:_SLUG_LIMIT].strip("-")
    return slug or "shop"


def _reason(value: str) -> str:
    if not isinstance(value, str) or len(value) > _REASON_LIMIT or "\x00" in value:
        raise InputValidationError(
            ONBOARDING_REASON_INVALID, f"a reason is at most {_REASON_LIMIT} characters"
        )
    return value


def _evidence(values: Iterable[str]) -> list[str]:
    ids = list(values)
    if len(ids) > _EVIDENCE_LIMIT or not all(
        isinstance(v, str) and _EVIDENCE_ID.fullmatch(v) for v in ids
    ):
        raise InputValidationError(
            ONBOARDING_EVIDENCE_INVALID, "evidence is a short list of record identifiers"
        )
    return ids


class OnboardingStore:
    """The only production writer of the supplier onboarding tables."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        *,
        templates: Mapping[str, PlatformTemplate] = TEMPLATES,
        reserved_keys: Iterable[str] = (),
        reserved_hosts: Iterable[str] = (),
    ) -> None:
        """``reserved_keys`` and ``reserved_hosts`` are the repository suppliers' keys and
        storefront hosts: an onboarded site never shares either (ADR-0036 §1)."""
        self._db = db
        self._clock = clock
        self._audit = audit
        self._templates = templates
        self._reserved_keys = frozenset(reserved_keys)
        self._reserved_hosts = frozenset(reserved_hosts)
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        """Tell ``listener`` the key of every supplier a committed write changed."""
        self._listeners.append(listener)

    def template(self, name: str) -> PlatformTemplate:
        try:
            return self._templates[name]
        except KeyError:
            raise InputValidationError(
                ONBOARDING_TEMPLATE_UNKNOWN,
                "an onboarded site runs on a platform ICBM has a template for",
                details={"templates": sorted(self._templates)},
            ) from None

    # ------------------------------------------------------------------ writes

    def create(
        self,
        *,
        name: str,
        origin: str,
        template: str,
        actor: str,
        policy_reads: Iterable[PolicyReadInput] = (),
        detected: str | None = None,
        markers: Iterable[str] = (),
    ) -> OnboardingRecord:
        """A new ``DRAFT`` supplier with its bootstrap storefront host (§4 step 1, OB-01).

        ``policy_reads`` are the reads the operator was shown, written acknowledged by ``actor``
        (step 3). ``detected`` and ``markers`` are what platform detection found (step 2): the
        audit names them beside the template the operator confirmed."""
        reads = _policy_reads(policy_reads)
        marker_names = [m for m in markers if isinstance(m, str) and _EVIDENCE_ID.fullmatch(m)]
        try:
            display_name = plain_word(name, "display_name")
        except SiteConfigError:
            raise InputValidationError(
                ONBOARDING_NAME_INVALID, "the supplier name is a short plain name"
            ) from None
        chosen = self.template(template)
        normalised, host = onboarding_origin(origin)
        if host in self._reserved_hosts:
            raise OnboardingConflictError(
                ONBOARDING_HOST_TAKEN, "that storefront belongs to a repository supplier"
            )
        now = self._clock.now()
        with self._db.write() as session:
            taken_origin = session.scalars(
                select(OnboardedSupplier.supplier_key).where(OnboardedSupplier.origin == normalised)
            ).first()
            if taken_origin is not None:
                raise OnboardingConflictError(
                    ONBOARDING_HOST_TAKEN,
                    "that storefront is already onboarded",
                    details={"supplier_key": taken_origin},
                )
            key = self._free_key(session, host)
            session.add(
                OnboardedSupplier(
                    supplier_key=key,
                    display_name=display_name,
                    origin=normalised,
                    template=chosen.platform,
                    created_by=actor,
                    created_at=now,
                )
            )
            session.flush()
            session.add(
                OnboardingTransition(
                    transition_id=str(uuid.uuid4()),
                    supplier_key=key,
                    seq=1,
                    from_state=None,
                    to_state=OnboardingState.DRAFT.value,
                    actor=actor,
                    reason="",
                    evidence_json="[]",
                    created_at=now,
                )
            )
            session.add(
                OnboardingHost(
                    host_row_id=str(uuid.uuid4()),
                    supplier_key=key,
                    seq=1,
                    host=host,
                    role=HostRole.STOREFRONT.value,
                    observed_trial_id=None,
                    disabled=False,
                    confirmed_by=actor,
                    confirmed_at=now,
                )
            )
            for read in reads:
                session.add(
                    OnboardingPolicyRead(
                        read_id=str(uuid.uuid4()),
                        supplier_key=key,
                        document=read.document,
                        path=read.path,
                        http_status=read.http_status,
                        content_sha256=read.content_sha256,
                        read_at=read.read_at,
                        acknowledged_by=actor,
                        acknowledged_at=now,
                    )
                )
            session.flush()
            self._event(
                session,
                "ONBOARDING_SUPPLIER_CREATED",
                key,
                actor,
                after={
                    "template": chosen.platform,
                    "storefront_host": host,
                    "state": OnboardingState.DRAFT.value,
                },
                details={
                    "detected": detected,
                    "confirmed": chosen.platform,
                    "markers": marker_names[:_EVIDENCE_LIMIT],
                    "policy_reads": [
                        {"document": r.document, "path": r.path, "http_status": r.http_status}
                        for r in reads
                    ],
                },
            )
            record = _current(session, key)
        self._changed(key)
        return record

    def transition(
        self,
        key: str,
        to_state: OnboardingState,
        *,
        actor: str,
        reason: str = "",
        evidence: Iterable[str] = (),
    ) -> OnboardingRecord:
        """Append one allowed edge: ``DRAFT → RECON``, ``RECON → ACTIVE``, ``ACTIVE → RECON``,
        or any state but ``DISABLED`` → ``DISABLED`` (§3, §7).

        ``ACTIVE`` is refused with ``ONBOARDING_ACTIVATION_NOT_PROVEN`` unless the trials prove it
        (:meth:`activation`); its evidence then also names one verification per proven sample."""
        target = OnboardingState(to_state)
        why = _reason(reason)
        ids = _evidence(evidence)
        with self._db.write() as session:
            record = _current(session, key)
            if target not in ALLOWED_TRANSITIONS[record.state]:
                raise OnboardingConflictError(
                    ONBOARDING_TRANSITION_REFUSED,
                    "the supplier cannot move to that state from its current one",
                    details={"from": record.state.value, "to": target.value},
                )
            if target is OnboardingState.ACTIVE:
                proof = self._activation(session, record)
                if not proof.proven:
                    raise OnboardingConflictError(
                        ONBOARDING_ACTIVATION_NOT_PROVEN,
                        f"approval needs {ACTIVATION_SAMPLES} distinct verified samples under the"
                        " current words, with every core field read and no structural problem",
                        details=proof.details(),
                    )
                ids = _evidence([*ids, *(e for e in proof.evidence if e not in ids)])
            self._append_transition(
                session,
                record,
                target,
                actor,
                why,
                ids,
                template_revision=proof.template_revision
                if target is OnboardingState.ACTIVE
                else None,
            )
            record = _current(session, key)
        self._changed(key)
        return record

    def save_words(
        self,
        key: str,
        words: Mapping[str, Any],
        *,
        expected_seq: int,
        actor: str,
    ) -> OnboardingRecord:
        """Append the next words revision (§6, OB-03). ``expected_seq`` is the words revision the
        operator edited (``0`` before the first); a supplier whose words moved since is refused.
        A change to an ``ACTIVE`` supplier returns it to ``RECON`` in the same unit (OB-04)."""
        with self._db.write() as session:
            record = _current(session, key)
            self._require_open(record)
            parsed = self._checked_words(record, words)
            if record.words_seq != expected_seq:
                raise OnboardingConflictError(
                    ONBOARDING_WORDS_MOVED,
                    "the words changed since they were read; reload them before saving",
                    details={"current_seq": record.words_seq},
                )
            fingerprint = words_fingerprint(parsed)
            if record.words is not None and record.words.fingerprint == fingerprint:
                raise OnboardingConflictError(
                    ONBOARDING_WORDS_UNCHANGED,
                    "the words are identical to the current revision",
                    details={"current_seq": record.words_seq},
                )
            seq = record.words_seq + 1
            session.add(
                OnboardingWordRevision(
                    revision_id=str(uuid.uuid4()),
                    supplier_key=key,
                    seq=seq,
                    words_json=words_json(parsed),
                    fingerprint=fingerprint,
                    authored_by=actor,
                    authored_at=self._clock.now(),
                )
            )
            session.flush()
            self._event(
                session,
                "ONBOARDING_WORDS_SAVED",
                key,
                actor,
                before={"words_seq": record.words_seq},
                after={
                    "words_seq": seq,
                    "fingerprint": fingerprint,
                    "slots": sorted(parsed),
                },
            )
            if record.state is OnboardingState.ACTIVE:
                self._append_transition(
                    session, record, OnboardingState.RECON, actor, f"words revision {seq}", []
                )
            record = _current(session, key)
        self._changed(key)
        return record

    def confirm_host(
        self,
        key: str,
        host: str,
        role: HostRole | str,
        *,
        observed_trial_id: str | None,
        actor: str,
    ) -> OnboardingRecord:
        """Confirm a host for ``role`` (D1, OB-01): the bootstrap storefront host, or a host the
        named trial of this supplier observed."""
        name = onboarding_host(host)
        chosen = _role(role)
        with self._db.write() as session:
            record = _current(session, key)
            self._require_open(record)
            if observed_trial_id is None:
                if chosen is not HostRole.STOREFRONT or name != record.storefront_host:
                    raise InputValidationError(
                        ONBOARDING_HOST_UNOBSERVED,
                        "a host other than the storefront origin's was observed by a trial",
                    )
            elif not _observed(session, key, observed_trial_id, name):
                raise InputValidationError(
                    ONBOARDING_HOST_UNOBSERVED,
                    "that trial of this supplier did not observe the host",
                )
            if any(h.host == name and h.role is chosen for h in record.hosts):
                raise OnboardingConflictError(
                    ONBOARDING_HOST_UNCHANGED, "the host is already confirmed for that role"
                )
            if chosen is HostRole.STOREFRONT and name != record.storefront_host:
                self._require_storefront_free(session, key, name)
            self._append_host(session, record, name, chosen, observed_trial_id, False, actor)
            record = _current(session, key)
        self._changed(key)
        return record

    def disable_host(
        self, key: str, host: str, role: HostRole | str, *, actor: str
    ) -> OnboardingRecord:
        """Withdraw a confirmed host from ``role`` (D1). The egress grant follows at once."""
        name = onboarding_host(host)
        chosen = _role(role)
        with self._db.write() as session:
            record = _current(session, key)
            self._require_open(record)
            if not any(h.host == name and h.role is chosen for h in record.hosts):
                raise NotFoundError(
                    ONBOARDING_HOST_UNKNOWN, "no confirmed host of that role has that name"
                )
            self._append_host(session, record, name, chosen, None, True, actor)
            record = _current(session, key)
        self._changed(key)
        return record

    def record_trial(self, key: str, trial: TrialInput, *, actor: str) -> StoredTrial:
        """Append one trial read of a ``RECON`` site under its current words revision (§5).

        It is the trial's only record: never a ``ProductFactsRevision`` (OB-02). A site in any
        other state is refused, and so is a trial under words that are no longer current."""
        checked = _trial(trial)
        with self._db.write() as session:
            record = _current(session, key)
            if record.state is not OnboardingState.RECON:
                raise OnboardingConflictError(
                    ONBOARDING_NOT_RECON,
                    "site analysis reads a supplier that is in analysis (RECON) only",
                    details={"state": record.state.value},
                )
            if checked.words_seq != record.words_seq:
                raise OnboardingConflictError(
                    ONBOARDING_WORDS_MOVED,
                    "the words changed while the sample was analysed; analyse it again",
                    details={"current_seq": record.words_seq},
                )
            row = OnboardingTrial(
                trial_id=str(uuid.uuid4()),
                supplier_key=key,
                sample_url_key=checked.sample_url_key,
                template_revision=checked.template_revision,
                words_seq=checked.words_seq,
                capture_sha256=checked.capture_sha256,
                facts_fingerprint=checked.facts_fingerprint,
                field_status_json=_canonical(dict(checked.field_statuses)),
                unread_rows_json=_canonical([dict(r) for r in checked.unread_rows]),
                observed_hosts_json=_canonical([dict(h) for h in checked.observed_hosts]),
                structural_json=_canonical(list(checked.structural)),
                created_by=actor,
                created_at=self._clock.now(),
            )
            session.add(row)
            session.flush()
            self._event(
                session,
                "ONBOARDING_TRIAL_RECORDED",
                key,
                actor,
                after={
                    "trial_id": row.trial_id,
                    "words_seq": row.words_seq,
                    "template_revision": row.template_revision,
                    "capture_sha256": row.capture_sha256,
                    "facts_fingerprint": row.facts_fingerprint,
                },
                details={
                    "unread_rows": len(checked.unread_rows),
                    "observed_hosts": len(checked.observed_hosts),
                    "structural": list(checked.structural),
                },
            )
            return _stored_trial(row)

    def record_verification(
        self, key: str, trial_id: str, verdict: str, *, shown_fingerprint: str, actor: str
    ) -> StoredVerification:
        """Append the operator's verdict on one trial: ``CORRECT`` or ``WRONG`` of exactly the facts
        it showed (§7, OB-04). Only a trial of a ``RECON`` site under its current words and template
        revision is verified, and only as it was shown: a verdict naming another fingerprint is
        refused, so a verification never outlives the facts it confirmed."""
        if verdict not in (_VERIFIED, _REFUTED) or not (
            isinstance(shown_fingerprint, str) and _HEX64.fullmatch(shown_fingerprint)
        ):
            raise InputValidationError(
                ONBOARDING_VERIFICATION_INVALID,
                "a verification is CORRECT or WRONG of the fingerprint the trial showed",
            )
        with self._db.write() as session:
            record = _current(session, key)
            if record.state is not OnboardingState.RECON:
                raise OnboardingConflictError(
                    ONBOARDING_NOT_RECON,
                    "trials are verified while the supplier is in analysis (RECON)",
                    details={"state": record.state.value},
                )
            trial = session.get(OnboardingTrial, trial_id)
            if trial is None or trial.supplier_key != key:
                raise NotFoundError(
                    ONBOARDING_TRIAL_UNKNOWN, "no trial of this supplier has that id"
                )
            revision = self.template(record.template).revision
            if trial.words_seq != record.words_seq or trial.template_revision != revision:
                raise OnboardingConflictError(
                    ONBOARDING_TRIAL_NOT_CURRENT,
                    "only a trial under the current words and template revision is verified",
                    details={"words_seq": record.words_seq},
                )
            if shown_fingerprint != trial.facts_fingerprint:
                raise OnboardingConflictError(
                    ONBOARDING_VERIFICATION_STALE,
                    "the facts verified are not the facts this trial showed; reload the report",
                )
            row = OnboardingVerification(
                verification_id=str(uuid.uuid4()),
                trial_id=trial_id,
                supplier_key=key,
                shown_fingerprint=shown_fingerprint,
                verdict=verdict,
                verified_by=actor,
                verified_at=self._clock.now(),
            )
            session.add(row)
            session.flush()
            self._event(
                session,
                "ONBOARDING_TRIAL_VERIFIED",
                key,
                actor,
                after={
                    "verification_id": row.verification_id,
                    "trial_id": trial_id,
                    "verdict": verdict,
                    "shown_fingerprint": shown_fingerprint,
                },
            )
            return _stored_verification(row)

    # ------------------------------------------------------------------ reads

    def verifications(self, key: str) -> tuple[StoredVerification, ...]:
        with self._db.read() as session:
            rows = session.scalars(
                select(OnboardingVerification)
                .where(OnboardingVerification.supplier_key == key)
                .order_by(
                    OnboardingVerification.verified_at, OnboardingVerification.verification_id
                )
            ).all()
            return tuple(_stored_verification(row) for row in rows)

    def trials(self, key: str) -> tuple[StoredTrial, ...]:
        """Every trial of ``key``, oldest first (a re-analysis at the same instant after its
        words revision)."""
        with self._db.read() as session:
            _current(session, key)
            rows = session.scalars(
                select(OnboardingTrial)
                .where(OnboardingTrial.supplier_key == key)
                .order_by(
                    OnboardingTrial.created_at, OnboardingTrial.words_seq, OnboardingTrial.trial_id
                )
            ).all()
            return tuple(_stored_trial(row) for row in rows)

    def activation(self, key: str) -> ActivationProof:
        """What the trials under the current words revision prove now (§7)."""
        with self._db.read() as session:
            return self._activation(session, _current(session, key))

    def current(self, key: str) -> OnboardingRecord:
        with self._db.read() as session:
            return _current(session, key)

    def policy_reads(self, key: str) -> tuple[StoredPolicyRead, ...]:
        """The policy reads the operator acknowledged for ``key`` (§4 step 3), oldest first."""
        with self._db.read() as session:
            _current(session, key)
            rows = session.scalars(
                select(OnboardingPolicyRead)
                .where(OnboardingPolicyRead.supplier_key == key)
                .order_by(OnboardingPolicyRead.read_at, OnboardingPolicyRead.document)
            ).all()
            return tuple(
                StoredPolicyRead(
                    read_id=row.read_id,
                    document=row.document,
                    path=row.path,
                    http_status=row.http_status,
                    content_sha256=row.content_sha256,
                    read_at=row.read_at,
                    acknowledged_by=row.acknowledged_by,
                    acknowledged_at=row.acknowledged_at,
                )
                for row in rows
            )

    def supplier_of_origin(self, origin: str) -> str | None:
        """The onboarded supplier key that holds ``origin`` (normalised), in any state."""
        with self._db.read() as session:
            return session.scalars(
                select(OnboardedSupplier.supplier_key).where(OnboardedSupplier.origin == origin)
            ).first()

    def reserved_host(self, host: str) -> bool:
        """Whether ``host`` is a repository supplier's storefront (ADR-0036 §1)."""
        return host in self._reserved_hosts

    def all_current(self) -> tuple[OnboardingRecord, ...]:
        """Every onboarded supplier as it stands now, by key."""
        with self._db.read() as session:
            keys = session.scalars(
                select(OnboardedSupplier.supplier_key).order_by(OnboardedSupplier.supplier_key)
            ).all()
            return tuple(_current(session, key) for key in keys)

    # ------------------------------------------------------------------ internals

    def _activation(self, session: Session, record: OnboardingRecord) -> ActivationProof:
        revision = self.template(record.template).revision
        trials = session.scalars(
            select(OnboardingTrial)
            .where(
                OnboardingTrial.supplier_key == record.supplier_key,
                OnboardingTrial.words_seq == record.words_seq,
            )
            .order_by(OnboardingTrial.created_at, OnboardingTrial.trial_id)
        ).all()
        # Only trials under the current words and the current template revision count, for the
        # proof and for an open structural problem alike: once a template is fixed, an older
        # template's trial neither proves nor blocks (ADR-0036 §7).
        current = [t for t in trials if t.template_revision == revision]
        structural_open = sum(1 for t in current if _json_list(t.structural_json))
        verifications: dict[str, list[OnboardingVerification]] = {}
        for row in session.scalars(
            select(OnboardingVerification)
            .where(OnboardingVerification.trial_id.in_([t.trial_id for t in current]))
            .order_by(OnboardingVerification.verification_id)
        ):
            verifications.setdefault(row.trial_id, []).append(row)
        proven: dict[str, str] = {}
        core_unread = unverified = refuted = 0
        for trial in current:
            statuses = _json_object(trial.field_status_json)
            core_read = all(statuses.get(f) in READ_STATUSES[f] for f in CORE_FIELDS)
            shown = [
                v
                for v in verifications.get(trial.trial_id, [])
                if v.shown_fingerprint == trial.facts_fingerprint
            ]
            correct = [v for v in shown if v.verdict == _VERIFIED]
            wrong = any(v.verdict == _REFUTED for v in shown)
            core_unread += not core_read
            refuted += wrong
            unverified += not correct and not wrong
            if core_read and correct and not wrong and not _json_list(trial.structural_json):
                proven.setdefault(trial.sample_url_key, correct[0].verification_id)
        return ActivationProof(
            words_seq=record.words_seq,
            template_revision=revision,
            trials=len(current),
            proven_samples=tuple(sorted(proven)),
            evidence=tuple(proven[sample] for sample in sorted(proven)),
            core_unread=core_unread,
            unverified=unverified,
            refuted=refuted,
            structural_open=structural_open,
        )

    def _free_key(self, session: Session, host: str) -> str:
        base = f"{SITE_KEY_PREFIX}{_slug(host)}"
        taken = set(
            session.scalars(
                select(OnboardedSupplier.supplier_key).where(
                    OnboardedSupplier.supplier_key.like(f"{base}%")
                )
            ).all()
        )
        taken |= self._reserved_keys
        key, number = base, 1
        while key in taken:
            number += 1
            key = f"{base}-{number}"
        return key

    def _require_storefront_free(self, session: Session, key: str, host: str) -> None:
        if host in self._reserved_hosts:
            raise OnboardingConflictError(
                ONBOARDING_HOST_TAKEN, "that storefront belongs to a repository supplier"
            )
        other = session.scalars(
            select(OnboardedSupplier.supplier_key).where(
                OnboardedSupplier.origin == f"https://{host}",
                OnboardedSupplier.supplier_key != key,
            )
        ).first()
        if other is not None:
            raise OnboardingConflictError(
                ONBOARDING_HOST_TAKEN, "that storefront belongs to another onboarded supplier"
            )

    def check_words(self, key: str, words: Mapping[str, Any]) -> Mapping[str, tuple[str, ...]]:
        """The words exactly as :meth:`save_words` would store them, or the same refusal; nothing
        is written (the editor's preview, §6)."""
        record = self.current(key)
        self._require_open(record)
        return self._checked_words(record, words)

    def _checked_words(
        self, record: OnboardingRecord, words: Mapping[str, Any]
    ) -> Mapping[str, tuple[str, ...]]:
        """Only words pass (ADR-0030 PT-01, §6): plain words per template slot, under the site
        configuration's own word rules, and nothing a template rule refuses."""
        try:
            parsed = label_words(dict(words) if isinstance(words, Mapping) else words)
        except SiteConfigError as exc:
            raise InputValidationError(
                ONBOARDING_WORDS_INVALID, "words are plain words per template slot"
            ) from exc
        template = self.template(record.template)
        if unknown := sorted(set(parsed) - set(template.label_slots)):
            raise InputValidationError(
                ONBOARDING_WORDS_SLOT_UNKNOWN,
                "words go into the template's own slots only",
                details={"slots": unknown},
            )
        candidate = onboarded_site(record, template, words=parsed, require_storefront=False)
        if found := template_problems(candidate, template):
            raise InputValidationError(
                ONBOARDING_WORDS_INVALID,
                "the words break a template rule",
                details={"problems": found},
            )
        return parsed

    @staticmethod
    def _require_open(record: OnboardingRecord) -> None:
        if record.state is OnboardingState.DISABLED:
            raise OnboardingConflictError(
                ONBOARDING_SUPPLIER_DISABLED, "a disabled supplier is not changed"
            )

    def _append_transition(
        self,
        session: Session,
        record: OnboardingRecord,
        target: OnboardingState,
        actor: str,
        reason: str,
        evidence: list[str],
        *,
        template_revision: str | None = None,
    ) -> None:
        session.add(
            OnboardingTransition(
                transition_id=str(uuid.uuid4()),
                supplier_key=record.supplier_key,
                seq=record.state_seq + 1,
                from_state=record.state.value,
                to_state=target.value,
                actor=actor,
                reason=reason,
                evidence_json=json.dumps(evidence),
                created_at=self._clock.now(),
                template_revision=template_revision,
            )
        )
        session.flush()
        self._event(
            session,
            "ONBOARDING_STATE_CHANGED",
            record.supplier_key,
            actor,
            before={"state": record.state.value},
            after={
                "state": target.value,
                "state_seq": record.state_seq + 1,
                "template_revision": template_revision,
            },
            details={"evidence_count": len(evidence)},
        )

    def _append_host(
        self,
        session: Session,
        record: OnboardingRecord,
        host: str,
        role: HostRole,
        trial_id: str | None,
        disabled: bool,
        actor: str,
    ) -> None:
        seq = 1 + (
            session.scalar(
                select(func.max(OnboardingHost.seq)).where(
                    OnboardingHost.supplier_key == record.supplier_key
                )
            )
            or 0
        )
        session.add(
            OnboardingHost(
                host_row_id=str(uuid.uuid4()),
                supplier_key=record.supplier_key,
                seq=seq,
                host=host,
                role=role.value,
                observed_trial_id=trial_id,
                disabled=disabled,
                confirmed_by=actor,
                confirmed_at=self._clock.now(),
            )
        )
        session.flush()
        self._event(
            session,
            "ONBOARDING_HOST_DISABLED" if disabled else "ONBOARDING_HOST_CONFIRMED",
            record.supplier_key,
            actor,
            after={"host": host, "role": role.value, "host_seq": seq},
            details={"observed_trial_id": trial_id},
        )

    def _event(
        self,
        session: Session,
        action: str,
        key: str,
        actor: str,
        *,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.SUPPLIER_ONBOARDING_RECORDED,
                action=action,
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=f"supplier:{key}",
                before=before,
                after=after,
                details=details,
            ),
            session=session,
        )

    def _changed(self, key: str) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(key)
            except Exception:
                # The write is durable and is not undone. The registry withdraws the supplier
                # before it rebuilds it and never raises (fail closed); this only guards a
                # listener that breaks that rule.
                logger.exception("collect.onboarding_rebind_failed", extra={"supplier": key})


def _role(value: HostRole | str) -> HostRole:
    try:
        return HostRole(value)
    except ValueError:
        raise InputValidationError(
            ONBOARDING_ROLE_UNKNOWN,
            "a host's role is storefront, login or image",
        ) from None


def _json_list(text: str) -> list[Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return ["unreadable"]
    return value if isinstance(value, list) else ["unreadable"]


def _json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stored_trial(row: OnboardingTrial) -> StoredTrial:
    statuses = _json_object(row.field_status_json)
    return StoredTrial(
        trial_id=row.trial_id,
        sample_url_key=row.sample_url_key,
        template_revision=row.template_revision,
        words_seq=row.words_seq,
        capture_sha256=row.capture_sha256,
        facts_fingerprint=row.facts_fingerprint,
        field_statuses={k: str(v) for k, v in statuses.items()},
        unread_rows=tuple(r for r in _json_list(row.unread_rows_json) if isinstance(r, dict)),
        observed_hosts=tuple(h for h in _json_list(row.observed_hosts_json) if isinstance(h, dict)),
        structural=tuple(str(c) for c in _json_list(row.structural_json)),
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _observed(session: Session, key: str, trial_id: str, host: str) -> bool:
    trial = session.get(OnboardingTrial, trial_id)
    if trial is None or trial.supplier_key != key:
        return False
    try:
        observed = json.loads(trial.observed_hosts_json)
    except ValueError:
        return False
    names = {
        entry.get("host") if isinstance(entry, dict) else entry
        for entry in observed
        if isinstance(entry, dict | str)
    }
    return host in names


def _current(session: Session, key: str) -> OnboardingRecord:
    identity = session.get(OnboardedSupplier, key)
    if identity is None:
        raise NotFoundError(ONBOARDING_SUPPLIER_UNKNOWN, "no onboarded supplier has that key")
    state = session.scalars(
        select(OnboardingTransition)
        .where(OnboardingTransition.supplier_key == key)
        .order_by(OnboardingTransition.seq.desc())
        .limit(1)
    ).one()
    words = session.scalars(
        select(OnboardingWordRevision)
        .where(OnboardingWordRevision.supplier_key == key)
        .order_by(OnboardingWordRevision.seq.desc())
        .limit(1)
    ).one_or_none()
    newest: dict[tuple[str, str], OnboardingHost] = {}
    for row in session.scalars(
        select(OnboardingHost)
        .where(OnboardingHost.supplier_key == key)
        .order_by(OnboardingHost.seq)
    ):
        newest[(row.role, row.host)] = row
    hosts = tuple(
        OnboardedHost(
            host=row.host,
            role=HostRole(row.role),
            observed_trial_id=row.observed_trial_id,
            confirmed_by=row.confirmed_by,
            confirmed_at=row.confirmed_at,
        )
        for (_, _), row in sorted(newest.items())
        if not row.disabled
    )
    return OnboardingRecord(
        supplier_key=identity.supplier_key,
        display_name=identity.display_name,
        origin=identity.origin,
        storefront_host=identity.origin.removeprefix("https://"),
        template=identity.template,
        created_at=identity.created_at,
        state=OnboardingState(state.to_state),
        state_seq=state.seq,
        words=None
        if words is None
        else WordsRevision(
            seq=words.seq,
            words={slot: tuple(values) for slot, values in json.loads(words.words_json).items()},
            fingerprint=words.fingerprint,
            authored_by=words.authored_by,
            authored_at=words.authored_at,
        ),
        hosts=hosts,
    )


def _stored_verification(row: OnboardingVerification) -> StoredVerification:
    return StoredVerification(
        verification_id=row.verification_id,
        trial_id=row.trial_id,
        shown_fingerprint=row.shown_fingerprint,
        verdict=row.verdict,
        verified_by=row.verified_by,
        verified_at=row.verified_at,
    )
