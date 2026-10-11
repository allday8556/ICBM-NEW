"""The supplier onboarding owner's store (ADR-0036 §3; OB-01, OB-03, OB-04, OB-05).

``OnboardingStore`` is the only writer of the onboarding tables. Every write is one write unit
that appends rows and one audit event; nothing is updated or deleted, and the database refuses it
if anything tries. An audit event names keys, states, hosts, roles, sequence numbers and
fingerprints only: never a credential, a word, a reason the operator typed or page text (§9).

- ``create`` writes the identity, the first state ``DRAFT`` and the bootstrap storefront host,
  the origin's own host, which passes the host guard first (OB-01).
- ``transition`` appends one allowed edge of the state log (§3, §7). ``RECON → ACTIVE`` is
  appended only when the trials and verifications prove it (§7 D2, OB-04, ``activation``); the
  database refuses an unproven ``ACTIVE`` as well. Trials and verifications have no writer before
  O3 and O5, so in O1 no site can become ``ACTIVE``.
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

# ADR-0036 §2 D2: three distinct samples prove a words revision.
ACTIVATION_SAMPLES: Final = 3
# The fields every proving trial reads: the fact registry's CORE fields (ADR-0030 §2).
CORE_FIELDS: Final = tuple(
    key for key, spec in FIELD_REGISTRY.items() if spec.level is FieldLevel.CORE
)
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
    # Trials under the current words revision and template revision.
    trials: int
    # The distinct sample URL keys a trial proves, and one verification id for each.
    proven_samples: tuple[str, ...]
    evidence: tuple[str, ...]
    # Trials under the current words that fail each condition (a trial may fail several).
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
            "required_samples": ACTIVATION_SAMPLES,
            "proven_samples": len(self.proven_samples),
            "trials": self.trials,
            "core_unread": self.core_unread,
            "unverified": self.unverified,
            "refuted": self.refuted,
            "structural_open": self.structural_open,
        }


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

    def create(self, *, name: str, origin: str, template: str, actor: str) -> OnboardingRecord:
        """A new ``DRAFT`` supplier with its bootstrap storefront host (§4 step 1, OB-01)."""
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
            self._append_transition(session, record, target, actor, why, ids)
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
        try:
            parsed = label_words(dict(words) if isinstance(words, Mapping) else words)
        except SiteConfigError as exc:
            raise InputValidationError(
                ONBOARDING_WORDS_INVALID, "words are plain words per template slot"
            ) from exc
        with self._db.write() as session:
            record = _current(session, key)
            self._require_open(record)
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

    # ------------------------------------------------------------------ reads

    def activation(self, key: str) -> ActivationProof:
        """What the trials under the current words revision prove now (§7)."""
        with self._db.read() as session:
            return self._activation(session, _current(session, key))

    def current(self, key: str) -> OnboardingRecord:
        with self._db.read() as session:
            return _current(session, key)

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
        structural_open = sum(1 for t in trials if _json_list(t.structural_json))
        current = [t for t in trials if t.template_revision == revision]
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
            core_read = all(statuses.get(f) == FieldStatus.CONFIRMED.value for f in CORE_FIELDS)
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
            )
        )
        session.flush()
        self._event(
            session,
            "ONBOARDING_STATE_CHANGED",
            record.supplier_key,
            actor,
            before={"state": record.state.value},
            after={"state": target.value, "state_seq": record.state_seq + 1},
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
                # The write is durable; a registry that failed to rebind still holds the previous
                # definition, and the next change or a restart binds it again.
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
