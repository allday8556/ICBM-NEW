"""The per-supplier shadow switch, and the only way into ``SHADOW`` (ADR-0017 §7.1, §10.1, S7).

The switch is an append-only history per supplier. Its absence is *off*: no migration and no
default writes an entry, so every supplier — KM통상 included — is disabled until an explicit,
separately authorized entry enables it.

- ``enable`` appends an ``ENABLE`` that binds the exact EPR, its bundle identity (the Adaptive
  comparability key) and the exact validation freshness it was enabled under. It is refused unless
  that EPR is **currently VALIDATED** for that freshness, under this running engine and this
  running hook manifest. In the same write unit the EPR enters ``SHADOW``, and an EPR the switch
  enabled before it leaves ``SHADOW`` for ``DRAFT``.
- ``disable`` appends a ``DISABLE`` and, in the same unit, returns the enabled EPR to ``DRAFT``.
- ``freeze`` answers a run's shadow decision inside the run store's own reservation unit, with that
  unit's session, and writes nothing. It says ``ENABLED`` only while the supplier's latest entry is
  an ``ENABLE`` whose EPR is ``SHADOW`` and whose freshness still names this running engine and
  hook manifest; anything else is an explicit ``DISABLED``.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collect.adaptive.canonical import canonical_json
from app.collect.adaptive.capture import CAPTURE_REVISION
from app.collect.adaptive.extraction_identity import EXTRACTOR_FINGERPRINT, EXTRACTOR_REVISION
from app.collect.adaptive.hooks import HookManifest
from app.collect.adaptive.profiles import SCHEMA_VERSION, semantic_tuple
from app.collect.adaptive_shadow.models import ShadowSwitchEntry
from app.collect.adaptive_store.gate import SupplierGate
from app.collect.adaptive_store.models import AdaptiveProfileTransition
from app.collect.adaptive_store.store import AdaptiveProfileStore, AdaptiveValidationStore
from app.collect.shadow import DISABLED, FrozenShadow
from app.core.clock import Clock
from app.core.errors import InputValidationError
from app.db.database import Database

ADAPTIVE_SHADOW_REFUSED = "ADAPTIVE_SHADOW_REFUSED"
Action = Literal["ENABLE", "DISABLE"]


@dataclass(frozen=True)
class SwitchEntry:
    entry_id: str
    supplier_key: str
    seq: int
    action: Action
    epr_digest: str | None
    bundle_key: str | None
    freshness: tuple[str, ...] | None
    actor: str
    reason: str
    correlation_id: str
    recorded_at: datetime


def bundle_key_of(epr_digest: str, extractor_revision: str = EXTRACTOR_REVISION) -> str:
    """The exact bundle identity: the canonical text of the Adaptive comparability key
    ``("ADAPTIVE", extractor_revision, profile_schema_version, epr_digest)`` (ADR-0017 §5.3)."""
    return canonical_json(list(semantic_tuple(extractor_revision, epr_digest)))


def parse_bundle_key(bundle_key: str) -> tuple[str, str, str, str]:
    parsed = json.loads(bundle_key)
    if (
        not isinstance(parsed, list)
        or len(parsed) != 4
        or parsed[0] != "ADAPTIVE"
        or not all(isinstance(part, str) for part in parsed)
    ):
        raise ValueError("not an Adaptive bundle key")
    return parsed[0], parsed[1], parsed[2], parsed[3]


def running_bundle(bundle_key: str) -> str | None:
    """The EPR digest of a bundle key that this running engine can execute, or ``None``."""
    try:
        _, revision, schema, epr_digest = parse_bundle_key(bundle_key)
    except ValueError:
        return None
    if (revision, schema) != (EXTRACTOR_REVISION, SCHEMA_VERSION):
        return None
    return epr_digest


def _switch_refused(message: str, **details: object) -> InputValidationError:
    return InputValidationError(ADAPTIVE_SHADOW_REFUSED, message, details=details or None)


def _entry(row: ShadowSwitchEntry) -> SwitchEntry:
    return SwitchEntry(
        entry_id=row.entry_id,
        supplier_key=row.supplier_key,
        seq=row.seq,
        action=row.action,  # type: ignore[arg-type]
        epr_digest=row.epr_digest,
        bundle_key=row.bundle_key,
        freshness=None if row.freshness_json is None else tuple(json.loads(row.freshness_json)),
        actor=row.actor,
        reason=row.reason,
        correlation_id=row.correlation_id,
        recorded_at=row.recorded_at,
    )


def _latest(session: Session, supplier_key: str) -> ShadowSwitchEntry | None:
    return session.scalars(
        select(ShadowSwitchEntry)
        .where(ShadowSwitchEntry.supplier_key == supplier_key)
        .order_by(ShadowSwitchEntry.seq.desc())
        .limit(1)
    ).first()


def _state(session: Session, epr_digest: str) -> str | None:
    return session.scalar(
        select(AdaptiveProfileTransition.to_state)
        .where(AdaptiveProfileTransition.epr_digest == epr_digest)
        .order_by(AdaptiveProfileTransition.seq.desc())
        .limit(1)
    )


class ShadowSwitch:
    """The only writer of the switch history, and the only owner that moves an EPR in or out of
    ``SHADOW``."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        supplier_gate: SupplierGate,
        profiles: AdaptiveProfileStore,
        validation: AdaptiveValidationStore,
        manifests: Mapping[str, HookManifest] | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._admits = supplier_gate
        self._profiles = profiles
        self._validation = validation
        # The running hook manifest of each supplier. None registered means no hooks run.
        self._manifests = dict(manifests or {})

    def _running(self, supplier_key: str) -> tuple[str, str, str, str, str]:
        manifest = self._manifests.get(supplier_key)
        hook = manifest.hook_fingerprint if manifest is not None else ""
        return (SCHEMA_VERSION, EXTRACTOR_REVISION, EXTRACTOR_FINGERPRINT, hook, CAPTURE_REVISION)

    def _current_freshness(self, supplier_key: str, freshness: Sequence[str]) -> bool:
        schema, revision, fingerprint, hook, capture = self._running(supplier_key)
        return (
            len(freshness) == 7
            and tuple(freshness[1:5]) == (schema, revision, fingerprint, hook)
            and freshness[6] == capture
        )

    # -------------------------------------------------------------- writes

    def enable(
        self,
        epr_digest: str,
        freshness: Sequence[str],
        *,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> str:
        """Enable the supplier's shadow for this exact EPR and freshness, and enter ``SHADOW``."""
        record = self._profiles.record(epr_digest)
        if record.kind != "EXTRACTION_PROFILE":
            raise _switch_refused("only an ExtractionProfileRevision is ever shadowed")
        supplier_key = record.supplier_key
        if not self._admits(supplier_key):
            raise _switch_refused("the shadow switch exists only for a registered supplier")
        current = tuple(freshness)
        if (
            not current
            or current[0] != epr_digest
            or not self._current_freshness(supplier_key, current)
        ):
            raise _switch_refused(
                "the freshness names this EPR, this running engine and hook manifest"
            )
        state = self._profiles.state(epr_digest)
        if state == "RETIRED":
            raise _switch_refused("a retired EPR is never shadowed")
        if not self._validation.is_validated(epr_digest, current):
            raise _switch_refused("only a currently VALIDATED EPR is enabled for shadow")
        with self._db.write() as session:
            previous = _latest(session, supplier_key)
            entry_id = self._append(
                session,
                supplier_key,
                "ENABLE",
                epr_digest,
                list(current),
                actor=actor,
                reason=reason,
                correlation_id=correlation_id,
            )
            if (
                previous is not None
                and previous.action == "ENABLE"
                and previous.epr_digest is not None
                and previous.epr_digest != epr_digest
                and _state(session, previous.epr_digest) == "SHADOW"
            ):
                self._profiles._switch_transition(
                    session,
                    previous.epr_digest,
                    "DRAFT",
                    actor=actor,
                    reason="SHADOW_REPLACED",
                    correlation_id=correlation_id,
                )
            if _state(session, epr_digest) == "DRAFT":
                self._profiles._switch_transition(
                    session,
                    epr_digest,
                    "SHADOW",
                    actor=actor,
                    reason=reason,
                    correlation_id=correlation_id,
                )
        return entry_id

    def disable(self, supplier_key: str, *, actor: str, reason: str, correlation_id: str) -> str:
        """Turn the supplier's shadow off; the EPR it enabled returns to ``DRAFT``."""
        with self._db.write() as session:
            previous = _latest(session, supplier_key)
            if previous is None or previous.action != "ENABLE":
                raise _switch_refused("the shadow of this supplier is already off")
            entry_id = self._append(
                session,
                supplier_key,
                "DISABLE",
                None,
                None,
                actor=actor,
                reason=reason,
                correlation_id=correlation_id,
            )
            if previous.epr_digest is not None and _state(session, previous.epr_digest) == "SHADOW":
                self._profiles._switch_transition(
                    session,
                    previous.epr_digest,
                    "DRAFT",
                    actor=actor,
                    reason=reason,
                    correlation_id=correlation_id,
                )
        return entry_id

    def _append(
        self,
        session: Session,
        supplier_key: str,
        action: Action,
        epr_digest: str | None,
        freshness: list[str] | None,
        *,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> str:
        previous = _latest(session, supplier_key)
        entry_id = str(uuid.uuid4())
        session.add(
            ShadowSwitchEntry(
                entry_id=entry_id,
                supplier_key=supplier_key,
                seq=1 if previous is None else previous.seq + 1,
                action=action,
                epr_digest=epr_digest,
                bundle_key=None if epr_digest is None else bundle_key_of(epr_digest),
                freshness_json=None if freshness is None else canonical_json(freshness),
                actor=actor,
                reason=reason,
                correlation_id=correlation_id,
                recorded_at=self._clock.now(),
            )
        )
        session.flush()
        return entry_id

    # -------------------------------------------------------------- reads

    def entries(self, supplier_key: str) -> tuple[SwitchEntry, ...]:
        with self._db.read() as session:
            rows = session.scalars(
                select(ShadowSwitchEntry)
                .where(ShadowSwitchEntry.supplier_key == supplier_key)
                .order_by(ShadowSwitchEntry.seq)
            ).all()
            return tuple(_entry(row) for row in rows)

    def current(self, supplier_key: str) -> SwitchEntry | None:
        with self._db.read() as session:
            row = _latest(session, supplier_key)
            return None if row is None else _entry(row)

    def freeze(self, session: Session, supplier_key: str) -> FrozenShadow:
        """The run store's freezer (``app.collect.shadow.ShadowFreezer``): read-only, in the
        reservation's own unit and session."""
        latest = _latest(session, supplier_key)
        if latest is None or latest.action != "ENABLE" or latest.epr_digest is None:
            return DISABLED
        if not self._admits(supplier_key):
            return DISABLED
        freshness = json.loads(latest.freshness_json or "[]")
        if not isinstance(freshness, list) or not self._current_freshness(supplier_key, freshness):
            return DISABLED  # the engine or hook manifest moved on since the switch was set
        bundle_key = bundle_key_of(latest.epr_digest)
        if latest.bundle_key != bundle_key or _state(session, latest.epr_digest) != "SHADOW":
            return DISABLED
        return FrozenShadow("ENABLED", latest.entry_id, bundle_key)
