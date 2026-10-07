"""The operator's ASSET upload run (ADR-0018 §2, §3.4, §4.1 amendment notes; owner decision
``5975217061``): ``icbm live upload-assets --grant-id``.

It is the only production entry point of :class:`~app.capabilities.live_safety.assets.
AssetUploadService`. It runs in the ``icbm live`` process, which owns the data directory (the
server is stopped), and composes existing owners only — it decides nothing itself:

1. the grant's own artifacts, from the grant owner; exact bytes already ``APPLIED_PROVEN`` for the
   same canonical account and image endpoint are reused and never uploaded again;
2. a bounded LIVE window of this process through the execution-mode owner: at most
   ``LIVE_WINDOW_MAX_S``, opened only while a live grant exists, and closed again when the run
   ends — whatever happens — or when the process exits. Nothing reaches a provider before it;
3. one CONNECT pass of the marketplace owner (token, committed session), so the canonical bearer
   source answers in this process;
4. each artifact's exact local bytes, read from the M4 lineage store that holds it, uploaded once
   through the upload owner: every layer of the send-time stack still decides, and the grant's
   budget is spent by the owner. A terminal failure is recorded for that artifact and the run
   continues with the next artifact; an ``UPLOAD_UNKNOWN`` is never retried, and a safety-stack
   refusal still stops the run because the mutation boundary itself is no longer valid.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from app.capabilities.live_safety.assets import AssetUploadRequest, AssetUploadService
from app.capabilities.live_safety.model import MutationStage
from app.capabilities.live_safety.store import ArtifactRef, GrantRecord, LiveAuthorityStore
from app.platform.core.errors import AppError, InputValidationError, NotFoundError
from app.platform.core.execution import ExecutionMode
from app.stages.products.image_model import ImageAssetKind

UPLOAD_GRANT_NOT_FOUND: Final = "LIVE_UPLOAD_GRANT_NOT_FOUND"
UPLOAD_GRANT_NOT_ASSET: Final = "LIVE_UPLOAD_GRANT_NOT_ASSET"
UPLOAD_ARTIFACT_MISSING: Final = "LIVE_UPLOAD_ARTIFACT_MISSING"
UPLOAD_MEDIA_UNSUPPORTED: Final = "LIVE_UPLOAD_MEDIA_UNSUPPORTED"
UPLOAD_RUN_REASON: Final = "LIVE_ASSET_UPLOAD_RUN"

# The media types the M4 lineage stores accept, with the file extension the upload names.
EXTENSIONS: Final = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


class ModeOwner(Protocol):
    """The execution-mode owner (``ExecutionModeService``): read through its own methods only, so
    the live-safety owners stay free of any system or transport import."""

    def request_change(
        self,
        target: ExecutionMode,
        *,
        actor: str,
        reason: str | None,
        window_s: int | None = None,
    ) -> Any: ...

    def state(self) -> Any: ...


class ArtifactBytes(Protocol):
    """One M4 lineage store: its stored record (with the media type) and its exact bytes."""

    def get(self, sha256: str) -> Any: ...

    def read(self, sha256: str) -> bytes: ...


@dataclass(frozen=True)
class UploadRunItem:
    sha256: str
    asset_kind: str
    outcome: str  # APPLIED_PROVEN | NOT_APPLIED_PROVEN | UPLOAD_UNKNOWN | ALREADY_APPLIED
    attempt_id: str | None
    provider_asset_ref: str | None


@dataclass(frozen=True)
class UploadRunResult:
    grant_id: str
    items: tuple[UploadRunItem, ...]
    # Whether every artifact of the grant is now APPLIED_PROVEN.
    complete: bool
    mode_after: str


class AssetUploadRun:
    def __init__(
        self,
        *,
        store: LiveAuthorityStore,
        uploads: AssetUploadService,
        mode: ModeOwner,
        connect: Callable[[], Any],
        sources: ArtifactBytes,
        derived: ArtifactBytes,
    ) -> None:
        self._store = store
        self._uploads = uploads
        self._mode = mode
        self._connect = connect
        self._sources = sources
        self._derived = derived

    def run(
        self, grant_id: str, *, window_s: int, actor: str, correlation_id: str
    ) -> UploadRunResult:
        grant = self._grant(grant_id)
        reusable = {
            attempt.artifact_sha256: attempt
            for attempt in self._store.applied_uploads_for_account(
                grant.marketplace_key, grant.marketplace_account_id
            )
            if attempt.asset_profile == grant.asset_profile
        }
        pending = [a for a in grant.artifacts if a.sha256 not in reusable]
        # Every artifact's bytes are read before anything reaches a provider: a missing one stops
        # the run before CONNECT, the window or any upload.
        contents = {artifact.sha256: self._content(artifact) for artifact in pending}
        selected = {artifact.sha256: artifact for artifact in grant.artifacts}
        items: list[UploadRunItem] = [
            UploadRunItem(
                sha256=sha,
                asset_kind=selected[sha].asset_kind.value,
                outcome="ALREADY_APPLIED",
                attempt_id=attempt.attempt_id,
                provider_asset_ref=attempt.provider_asset_ref,
            )
            for sha, attempt in reusable.items()
            if any(a.sha256 == sha for a in grant.artifacts)
        ]
        if pending:
            # The window first: it opens only while a live grant exists, and nothing reaches a
            # provider before it is open.
            self._mode.request_change(
                ExecutionMode.LIVE, actor=actor, reason=UPLOAD_RUN_REASON, window_s=window_s
            )
            try:
                self._connect()
                items.extend(self._upload(grant, pending, contents, actor, correlation_id))
            finally:
                self._mode.request_change(
                    ExecutionMode.DRY_RUN, actor=actor, reason=UPLOAD_RUN_REASON
                )
        done = {i.sha256 for i in items if i.outcome in ("APPLIED_PROVEN", "ALREADY_APPLIED")}
        return UploadRunResult(
            grant_id=grant_id,
            items=tuple(items),
            complete=all(a.sha256 in done for a in grant.artifacts),
            mode_after=self._mode.state().mode.value,
        )

    def _upload(
        self,
        grant: GrantRecord,
        pending: Sequence[ArtifactRef],
        contents: dict[str, tuple[bytes, str]],
        actor: str,
        correlation_id: str,
    ) -> list[UploadRunItem]:
        items: list[UploadRunItem] = []
        for artifact in pending:
            content, media_type = contents[artifact.sha256]
            result = self._uploads.upload(
                AssetUploadRequest(
                    grant_id=grant.grant_id,
                    artifact=artifact,
                    content=content,
                    file_name=f"{artifact.sha256[:16]}.{EXTENSIONS[media_type]}",
                    media_type=media_type,
                    actor=actor,
                    correlation_id=correlation_id,
                )
            )
            items.append(
                UploadRunItem(
                    sha256=artifact.sha256,
                    asset_kind=artifact.asset_kind.value,
                    outcome=result.attempt.state.value,
                    attempt_id=result.attempt.attempt_id,
                    provider_asset_ref=result.attempt.provider_asset_ref,
                )
            )
        return items

    def _grant(self, grant_id: str) -> GrantRecord:
        grant = self._store.grant_record(grant_id)
        if grant is None:
            raise NotFoundError(UPLOAD_GRANT_NOT_FOUND, "no such grant")
        if grant.stage is not MutationStage.ASSET:
            raise InputValidationError(UPLOAD_GRANT_NOT_ASSET, "the grant is not an ASSET grant")
        return grant

    def _content(self, artifact: ArtifactRef) -> tuple[bytes, str]:
        store = (
            self._sources if artifact.asset_kind is ImageAssetKind.SOURCE_ASSET else self._derived
        )
        stored = store.get(artifact.sha256)
        if stored is None:
            raise AppError(UPLOAD_ARTIFACT_MISSING, "the artifact is not in its lineage store")
        media_type = str(stored.mime_type)
        if media_type not in EXTENSIONS:
            raise AppError(UPLOAD_MEDIA_UNSUPPORTED, f"unsupported media type {media_type}")
        return store.read(artifact.sha256), media_type
