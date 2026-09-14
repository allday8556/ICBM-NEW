"""SMARTSTORE-A0-PERMISSION handling (M2 PR-C).

The local operator records which API groups Commerce API Center shows for the configured
application. The attestation is stored append-only with its whole evidence envelope — including
the record-time ``attested_status`` and the age bound in effect — and audited. Its current use is
re-derived on every read against the current application fingerprint, required groups, mapping
revision and age policy, with ``now`` from the injected Clock, and capability truth converges on
the result (PERMISSIONS_SCOPES §8.1, §8.2; CAPABILITY_MAPPING S6, S7). Current freshness is never
stored.

Nothing here makes a provider call: the service holds no gateway, transport or egress grant, and
the application identity and mapping revision arrive through seams that PR-A implements.
"""

import base64
import logging
import os
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.connect.marketplace.attestation import (
    API_GROUP_LABELS,
    MALFORMED_EVALUATION,
    PRODUCT_REGISTRATION_REQUIRED_GROUPS,
    ApiGroup,
    Attestation,
    AttestationContext,
    AttestationEvaluation,
    AttestationRefusedError,
    EvidenceSource,
    application_fingerprint,
    attest,
    evaluate,
    promotion,
    refusal,
)
from app.connect.marketplace.attestation_contracts import (
    AttestationEvaluationView,
    AttestationRecordView,
    PermissionAttestationView,
)
from app.connect.marketplace.capability import (
    CapabilityInvariantError,
    EvidenceStrength,
    WriteScope,
    WriteScopeStatus,
)
from app.connect.marketplace.contracts import WriteScopeView
from app.connect.marketplace.models import MarketplacePermissionAttestation
from app.connect.marketplace.revision import EndpointMappingRevisionProvider
from app.connect.marketplace.service import CAPABILITY_MARKETPLACES, MarketplaceCapabilityService
from app.connect.marketplace.sources import ApplicationIdentitySource, PermissionEvidence
from app.core.clock import Clock
from app.core.errors import InputValidationError, NotFoundError, PolicyBlockedError
from app.core.safe_payload import safe_payload
from app.core.secrets import SecretStore
from app.db.database import Database

logger = logging.getLogger("icbm.connect.marketplace.attestation")

_FINGERPRINT_KEY_BYTES = 32
Row = MarketplacePermissionAttestation


def _key_name(marketplace_key: str) -> str:
    return f"marketplace:{marketplace_key}:application_fingerprint_key"


def _join(groups: Iterable[ApiGroup]) -> str:
    return ",".join(sorted(g.value for g in groups))


def _split(text: str) -> frozenset[ApiGroup]:
    return frozenset(ApiGroup(value) for value in text.split(",") if value)


def _attestation(row: Row) -> Attestation:
    """Rebuild the envelope; a row that does not rebuild — or whose stored attested_status
    contradicts its stored groups — is MALFORMED evidence."""
    return Attestation(
        observed_at=row.observed_at,
        application_fingerprint=row.application_fingerprint,
        required_groups=_split(row.required_groups),
        observed_groups=_split(row.observed_groups),
        endpoint_mapping_revision=row.endpoint_mapping_revision,
        max_age_days=row.freshness_policy_max_age_days,
        evidence_source=EvidenceSource(row.evidence_source),
        evidence_strength=EvidenceStrength(row.evidence_strength),
        recorded_status=WriteScopeStatus(row.attested_status),
    )


def _scope_view(scope: WriteScope) -> WriteScopeView:
    return WriteScopeView(
        status=scope.status,
        evidence_strength=scope.evidence_strength,
        evidence_grade=scope.evidence_grade,
    )


class PermissionAttestationService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        secrets: SecretStore,
        capability: MarketplaceCapabilityService,
        identity: ApplicationIdentitySource | None,
        revision: EndpointMappingRevisionProvider | None,
        max_age_days: int,
        marketplace_keys: Sequence[str] = CAPABILITY_MARKETPLACES,
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._secrets = secrets
        self._capability = capability
        self._identity = identity
        self._revision = revision
        self._max_age_days = max_age_days
        self._keys = tuple(marketplace_keys)

    def _known(self, marketplace_key: str) -> None:
        if marketplace_key not in self._keys:
            raise NotFoundError(
                "MARKETPLACE_CAPABILITY_UNKNOWN",
                f"no capability contract for marketplace {marketplace_key!r}",
            )

    # ------------------------------------------------------------------ current context

    def _fingerprint_key(self, marketplace_key: str) -> bytes:
        """The keyed-fingerprint key lives in the OS secret store, never beside the evidence."""
        stored = self._secrets.get(_key_name(marketplace_key))
        if stored:
            key = base64.b64decode(stored)
            if len(key) >= _FINGERPRINT_KEY_BYTES:
                return key
        key = os.urandom(_FINGERPRINT_KEY_BYTES)
        self._secrets.set(_key_name(marketplace_key), base64.b64encode(key).decode("ascii"))
        return key

    def context(self, marketplace_key: str) -> AttestationContext:
        identity = self._identity.current_identity() if self._identity is not None else None
        fingerprint = (
            application_fingerprint(identity, self._fingerprint_key(marketplace_key))
            if identity is not None
            else None
        )
        revision = self._revision.current_revision() if self._revision is not None else None
        return AttestationContext(
            now=self._clock.now(),  # the injected Clock, never the wall clock (§8.1)
            application_fingerprint=fingerprint,
            required_groups=PRODUCT_REGISTRATION_REQUIRED_GROUPS,
            endpoint_mapping_revision=revision or None,
            max_age_days=self._max_age_days,
        )

    @staticmethod
    def _latest(session: Session, marketplace_key: str) -> Row | None:
        return session.scalars(
            select(Row)
            .where(Row.marketplace_key == marketplace_key)
            .order_by(Row.seq.desc())
            .limit(1)
        ).first()

    @staticmethod
    def _evaluate(
        row: Row, context: AttestationContext
    ) -> tuple[Attestation | None, AttestationEvaluation]:
        try:
            record = _attestation(row)
        except ValueError:  # includes CapabilityInvariantError and an unknown stored value
            return None, MALFORMED_EVALUATION
        return record, evaluate(record, context)

    # ------------------------------------------------------------------ PermissionEvidenceSource

    def current_permission(self, marketplace_key: str) -> PermissionEvidence | None:
        with self._db.read() as session:
            row = self._latest(session, marketplace_key)
        if row is None:
            return None
        _, evaluation = self._evaluate(row, self.context(marketplace_key))
        return PermissionEvidence(
            write_scope=evaluation.write_scope,
            invalidations=tuple(sorted(evaluation.invalidations)),
        )

    # ------------------------------------------------------------------ read and record

    def attestation(self, marketplace_key: str) -> PermissionAttestationView:
        self._known(marketplace_key)
        capability = self._capability.capability(marketplace_key)  # converges first (S7)
        context = self.context(marketplace_key)
        with self._db.read() as session:
            row = self._latest(session, marketplace_key)
        record, evaluation = self._evaluate(row, context) if row is not None else (None, None)
        capability_scope = WriteScope(
            capability.write_scope.status, capability.write_scope.evidence_strength
        )
        blocked = refusal(context)
        return PermissionAttestationView(
            marketplace_key=marketplace_key,
            required_groups=sorted(context.required_groups),
            selectable_groups=list(ApiGroup),
            group_labels=dict(API_GROUP_LABELS),
            recording_available=blocked is None,
            recording_refusal=blocked,
            max_age_days=self._max_age_days,
            attestation=(
                AttestationRecordView(
                    attestation_seq=row.seq,
                    evidence_source=record.evidence_source,
                    evidence_strength=record.evidence_strength,
                    observed_at=record.observed_at,
                    required_groups=sorted(record.required_groups),
                    observed_groups=sorted(record.observed_groups),
                    endpoint_mapping_revision=record.endpoint_mapping_revision,
                    freshness_policy_max_age_days=record.max_age_days,
                    attested_status=record.attested_status,
                    recorded_by=row.recorded_by,
                )
                if row is not None and record is not None
                else None
            ),
            evaluation=(
                AttestationEvaluationView(
                    write_scope=_scope_view(evaluation.write_scope),
                    freshness_status=evaluation.freshness,
                    applicable_max_age_days=evaluation.max_age_days,
                    invalidations=sorted(evaluation.invalidations),
                )
                if evaluation is not None
                else None
            ),
            promotion=promotion(evaluation, capability_scope, capability.contract_freshness),
            contract_freshness=capability.contract_freshness,
            capability_write_scope=capability.write_scope,
        )

    def attest(
        self, marketplace_key: str, observed_groups: Iterable[ApiGroup], *, actor: str
    ) -> PermissionAttestationView:
        """Record what the operator observed. Only the observed groups come from the operator;
        the time, fingerprint, required groups, mapping revision and age bound come from ICBM
        (§6.7, §8.1).

        A refusal stores nothing. A stored attestation whose READY promotion the contract
        freshness blocks is kept and reported as blocked (F5) — never silently discarded.
        """
        self._known(marketplace_key)
        context = self.context(marketplace_key)
        try:
            record = attest(observed_groups, context)
        except AttestationRefusedError as exc:
            raise PolicyBlockedError(
                "MARKETPLACE_ATTESTATION_REFUSED",
                f"no attestation can be recorded: {exc.reason}",
                details={"reason": exc.reason.value},
            ) from exc
        except CapabilityInvariantError as exc:
            raise InputValidationError("MARKETPLACE_ATTESTATION_INVALID", str(exc)) from exc
        with self._db.write() as session:
            row = Row(
                marketplace_key=marketplace_key,
                evidence_source=record.evidence_source,
                evidence_strength=record.evidence_strength,
                observed_at=record.observed_at,
                application_fingerprint=record.application_fingerprint,
                required_groups=_join(record.required_groups),
                observed_groups=_join(record.observed_groups),
                endpoint_mapping_revision=record.endpoint_mapping_revision,
                attested_status=record.attested_status,
                freshness_policy_max_age_days=record.max_age_days,
                recorded_by=actor,
            )
            session.add(row)
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.MARKETPLACE_PERMISSION_ATTESTED,
                    action="RECORD_PERMISSION_ATTESTATION",
                    actor=actor,
                    outcome=AuditOutcome.ALLOWED,
                    target_ref=f"marketplace:{marketplace_key}",
                    details=safe_payload(
                        marketplace_key=marketplace_key,
                        attestation_seq=row.seq,
                        evidence_source=record.evidence_source,
                        evidence_strength=record.evidence_strength,
                        attested_status=record.attested_status,
                        required_groups=sorted(g.value for g in record.required_groups),
                        observed_groups=sorted(g.value for g in record.observed_groups),
                        endpoint_mapping_revision=record.endpoint_mapping_revision,
                        freshness_policy_max_age_days=record.max_age_days,
                    ),
                ),
                session=session,
            )
            logger.info(
                "marketplace.permission.attested",
                extra=safe_payload(
                    marketplace_key=marketplace_key,
                    attestation_seq=row.seq,
                    attested_status=record.attested_status,
                ),
            )
        self._capability.converge_permission(marketplace_key)
        return self.attestation(marketplace_key)
