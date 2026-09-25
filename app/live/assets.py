"""The ASSET upload path (ADR-0018 §3.1, §3.4, §4.3): the only way an upload reaches a sender.

One upload is one exact artifact under one ASSET grant. The order is fixed:

1. the replay-conflict key is derived — the wire endpoint the sender declares, normalized by the
   server-owned :class:`~app.live.model.WireHostPolicy`, and the SHA-256 of the exact bytes that
   would be sent, which must be the artifact's own identity. An undeterminable key refuses;
2. the current candidate of the grant's preparation is read (§10 'the stage's own gate');
3. in **one write unit**: every layer of the safety stack is checked, and only then is the attempt
   recorded ``STARTED`` together with the grant's budget. A refusal rolls the unit back, is audited
   in its own unit, and nothing is sent;
4. the sender is called once, outside any write unit;
5. in a second unit the attempt is terminalized exactly once. A sender that raises is
   ``UPLOAD_UNKNOWN`` unless it proves with :class:`TransmissionPrecluded` that nothing left the
   process; a crash between 3 and 5 leaves ``STARTED``, which the next process settles as
   ``UPLOAD_UNKNOWN`` (:meth:`AssetUploadService.settle_interrupted`).

Only ``APPLIED_PROVEN`` yields a :class:`~app.register.preparation.PreparedAsset`.
"""

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.clock import Clock
from app.core.errors import InputValidationError, NotFoundError
from app.live.model import (
    ARTIFACT_NOT_GRANTED,
    CONTENT_DIGEST_MISMATCH,
    MutationRefused,
    MutationStage,
    ReplayKey,
    UploadAttemptState,
    WireHostPolicy,
    WireIdentityError,
    content_digest,
    replay_key,
)
from app.live.stack import AssetTarget, CandidateState, SafetyStack, StageReadiness
from app.live.store import (
    ArtifactRef,
    GrantRecord,
    LiveAuthorityStore,
    UploadAttemptRecord,
    UploadProvenance,
)
from app.products.model import ReadinessStatus
from app.register.preparation import PreparedAsset
from app.register.sanitize import PayloadSanitationError

logger = logging.getLogger("icbm.live.assets")


class TransmissionPrecluded(Exception):
    """Raised by a sender only when it proves that no byte of the request left the process."""


@dataclass(frozen=True)
class UploadSendResult:
    """What one send proved. ``provider_asset_ref`` exists exactly when it is applied."""

    state: UploadAttemptState
    provider_asset_ref: str | None = None
    outcome_reason: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


class AssetUploadSender(Protocol):
    """The adapter side of one upload endpoint. It owns no ledger, cache or retry."""

    marketplace_key: str

    def available(self) -> bool:
        """Whether an upload can be handed off at all. ``False`` keeps the stack refusing."""
        ...

    def endpoint_adopted(self) -> bool: ...

    def wire(self) -> tuple[str, str, str]:
        """The raw ``(method, host, path)`` the request would go to; normalized by the server."""
        ...

    def contract_label(self) -> str: ...

    def send(self, *, content: bytes, file_name: str, media_type: str) -> UploadSendResult: ...


class CandidateGate(Protocol):
    def current(self, preparation_revision_id: str) -> CandidateState: ...


@dataclass(frozen=True)
class AssetUploadRequest:
    grant_id: str
    artifact: ArtifactRef
    content: bytes
    file_name: str
    media_type: str
    actor: str
    correlation_id: str


@dataclass(frozen=True)
class AssetUploadResult:
    attempt: UploadAttemptRecord
    prepared: PreparedAsset | None


class AssetUploadService:
    def __init__(
        self,
        *,
        store: LiveAuthorityStore,
        stack: SafetyStack,
        sender: AssetUploadSender,
        hosts: WireHostPolicy,
        candidates: CandidateGate,
        clock: Clock,
        process_run_id: str | None = None,
    ) -> None:
        self._store = store
        self._stack = stack
        self._sender = sender
        self._hosts = hosts
        self._candidates = candidates
        self._clock = clock
        self.process_run_id = process_run_id or str(uuid.uuid4())

    # ------------------------------------------------------------------ the upload

    def upload(self, request: AssetUploadRequest) -> AssetUploadResult:
        grant = self._grant(request)
        try:
            key = self._key(grant, request.content, request.artifact)
            candidate = self._candidates.current(grant.preparation_revision_id or "")
            provenance = UploadProvenance(
                preparation_revision_id=candidate.preparation_revision_id,
                candidate_fingerprint=candidate.fingerprint or "",
                artifact=request.artifact,
                asset_profile=grant.asset_profile or "",
                contract_label=self._sender.contract_label(),
                file_name=request.file_name,
                media_type=request.media_type,
            )
            with self._store.transaction() as unit:
                unit.expire_due(correlation_id=request.correlation_id)
            with self._store.transaction() as unit:
                attempt = self._stack.admit_asset(
                    unit,
                    AssetTarget(
                        grant_id=grant.grant_id,
                        key=key,
                        artifact=request.artifact,
                        candidate=candidate,
                        endpoint_adopted=self._sender.endpoint_adopted(),
                        sender_wired=self._sender.available(),
                    ),
                    provenance,
                    process_run_id=self.process_run_id,
                    actor=request.actor,
                    correlation_id=request.correlation_id,
                )
        except MutationRefused as refusal:
            self._stack.record_refusal(
                refusal,
                stage=MutationStage.ASSET,
                target_ref=f"live_grant:{grant.grant_id}",
                actor=request.actor,
                correlation_id=request.correlation_id,
            )
            raise
        result = self._send(request)
        try:
            settled = self._finish(attempt.attempt_id, result, request)
        except (InputValidationError, PayloadSanitationError):
            # A result the owner cannot record as proven — an unsafe reference, unclean evidence —
            # proves nothing: the attempt is UPLOAD_UNKNOWN, never left open and never applied.
            settled = self._finish(
                attempt.attempt_id,
                UploadSendResult(
                    UploadAttemptState.UPLOAD_UNKNOWN, outcome_reason="UPLOAD_RESULT_UNRECORDABLE"
                ),
                request,
            )
        return AssetUploadResult(settled, _prepared(settled))

    def _finish(
        self, attempt_id: str, result: UploadSendResult, request: AssetUploadRequest
    ) -> UploadAttemptRecord:
        with self._store.transaction() as unit:
            return unit.finish_upload(
                attempt_id,
                state=result.state,
                actor=request.actor,
                correlation_id=request.correlation_id,
                provider_asset_ref=result.provider_asset_ref,
                outcome_reason=result.outcome_reason,
                evidence=result.evidence or None,
            )

    def _send(self, request: AssetUploadRequest) -> UploadSendResult:
        try:
            result = self._sender.send(
                content=request.content,
                file_name=request.file_name,
                media_type=request.media_type,
            )
        except TransmissionPrecluded:
            return UploadSendResult(
                UploadAttemptState.NOT_APPLIED_PROVEN, outcome_reason="TRANSMISSION_PRECLUDED"
            )
        except Exception:
            # Possibly transmitted: never a failure, never a retry. The fence stays closed.
            logger.exception("live.asset_upload.sender_raised")
            return UploadSendResult(
                UploadAttemptState.UPLOAD_UNKNOWN, outcome_reason="SENDER_RAISED"
            )
        if result.state is UploadAttemptState.STARTED:
            return UploadSendResult(
                UploadAttemptState.UPLOAD_UNKNOWN, outcome_reason="SENDER_OUTCOME_NOT_TERMINAL"
            )
        if result.state is UploadAttemptState.APPLIED_PROVEN and not result.provider_asset_ref:
            return UploadSendResult(
                UploadAttemptState.UPLOAD_UNKNOWN, outcome_reason="UPLOAD_NO_REFERENCE_RETURNED"
            )
        return result

    # ------------------------------------------------------------------ readiness (§10)

    def readiness(self, grant_id: str) -> StageReadiness:
        """``ASSET_MUTATION_READY`` of a grant's artifacts. Derived, read-only, not permission."""
        grant = self._store.grant_record(grant_id)
        candidate = (
            CandidateState("", current=False, ready=False, fingerprint=None)
            if grant is None or grant.preparation_revision_id is None
            else self._candidates.current(grant.preparation_revision_id)
        )

        def keys(artifact: ArtifactRef) -> ReplayKey:
            assert grant is not None
            return self._key_for(grant, artifact.sha256)

        return self._stack.asset_readiness(
            grant_id,
            keys=keys,
            candidate=candidate,
            endpoint_adopted=self._sender.endpoint_adopted(),
            sender_wired=self._sender.available(),
        )

    # ------------------------------------------------------------------ restart (§3.4, G3-25)

    def settle_interrupted(self, *, correlation_id: str) -> tuple[str, ...]:
        """Settle every attempt an earlier process left ``STARTED`` as ``UPLOAD_UNKNOWN``."""
        with self._store.transaction() as unit:
            return unit.settle_interrupted(
                process_run_id=self.process_run_id, correlation_id=correlation_id
            )

    # ------------------------------------------------------------------ helpers

    def _grant(self, request: AssetUploadRequest) -> GrantRecord:
        grant = self._store.grant_record(request.grant_id)
        if grant is None or grant.stage is not MutationStage.ASSET:
            raise NotFoundError("LIVE_ASSET_GRANT_NOT_FOUND", "no ASSET grant with this identity")
        return grant

    def _key(self, grant: GrantRecord, content: bytes, artifact: ArtifactRef) -> ReplayKey:
        digest = content_digest(content)
        if digest != artifact.sha256:
            raise MutationRefused(
                CONTENT_DIGEST_MISMATCH, "the bytes to send are not the artifact they claim to be"
            )
        if artifact not in grant.artifacts:
            raise MutationRefused(ARTIFACT_NOT_GRANTED, "the grant does not name this artifact")
        return self._key_for(grant, digest)

    def _key_for(self, grant: GrantRecord, content_sha256: str) -> ReplayKey:
        method, host, path = self._sender.wire()
        try:
            endpoint = self._hosts.endpoint(
                grant.marketplace_key, method=method, host=host, path=path
            )
            return replay_key(
                marketplace_key=grant.marketplace_key,
                marketplace_account_id=grant.marketplace_account_id,
                endpoint=endpoint,
                content_sha256=content_sha256,
            )
        except WireIdentityError as undeterminable:
            # Ambiguity takes the wider scope: no key, no upload (§3.4).
            raise MutationRefused(
                undeterminable.code, undeterminable.message, details=undeterminable.details
            ) from undeterminable


def _prepared(attempt: UploadAttemptRecord) -> PreparedAsset | None:
    if attempt.state is not UploadAttemptState.APPLIED_PROVEN:
        return None
    assert attempt.provider_asset_ref is not None
    return PreparedAsset(
        asset_kind=attempt.asset_kind,
        sha256=attempt.artifact_sha256,
        derivation_id=attempt.derivation_id,
        asset_profile=attempt.asset_profile,
        candidate_fingerprint=attempt.candidate_fingerprint,
        provider_asset_ref=attempt.provider_asset_ref,
    )


class PreparationCandidateGate:
    """The candidate preflight of the preparation a revision belongs to, evaluated now."""

    def __init__(self, preparations: Any, registrations: Any) -> None:
        self._preparations = preparations
        self._registrations = registrations

    def current(self, preparation_revision_id: str) -> CandidateState:
        preparation = self._registrations.preparation_of_revision(preparation_revision_id)
        if preparation is None:
            return CandidateState(preparation_revision_id, False, False, None)
        current = preparation.current.preparation_revision_id == preparation_revision_id
        result = self._preparations.evaluate(preparation.preparation_id)
        return CandidateState(
            preparation_revision_id,
            current=current,
            ready=result.status is ReadinessStatus.READY,
            fingerprint=result.dependency_fingerprint,
        )


class UnwiredAssetSender:
    """Production at this main: the upload path exists, but no sender is wired to a provider.

    It declares the adopted wire endpoint so the replay key and readiness are real, and refuses to
    send: the safety stack refuses before ever calling it (``LIVE_SENDER_NOT_WIRED``).
    """

    def __init__(
        self,
        *,
        marketplace_key: str,
        wire: tuple[str, str, str],
        contract_label: str,
        adopted: bool,
    ) -> None:
        self.marketplace_key = marketplace_key
        self._wire = wire
        self._label = contract_label
        self._adopted = adopted

    def available(self) -> bool:
        return False

    def endpoint_adopted(self) -> bool:
        return self._adopted

    def wire(self) -> tuple[str, str, str]:
        return self._wire

    def contract_label(self) -> str:
        return self._label

    def send(self, *, content: bytes, file_name: str, media_type: str) -> UploadSendResult:
        raise TransmissionPrecluded("no provider sender is wired at this main")


__all__ = [
    "AssetUploadRequest",
    "AssetUploadResult",
    "AssetUploadSender",
    "AssetUploadService",
    "CandidateGate",
    "PreparationCandidateGate",
    "TransmissionPrecluded",
    "UnwiredAssetSender",
    "UploadSendResult",
]
