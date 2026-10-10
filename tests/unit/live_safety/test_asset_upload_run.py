"""The operator's ASSET upload run (owner decision 5975217061): ``icbm live upload-assets``.

It composes the existing owners and decides nothing itself:
- every artifact's bytes are read before anything else, and a missing or unsupported one stops the
  run before the window, CONNECT or any upload;
- the bounded LIVE window opens first (only a live grant permits it), CONNECT runs inside it, and
  the window is closed when the run ends, whatever happens;
- each artifact is uploaded once through the upload owner, with its exact bytes and media type;
  one terminal failure is logged and the remaining independent artifacts continue;
- an artifact already APPLIED_PROVEN under the grant's revision and candidate is not uploaded again.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from app.capabilities.live_safety.model import (
    GrantState,
    MutationRefused,
    MutationStage,
    UploadAttemptState,
)
from app.capabilities.live_safety.store import ArtifactRef, GrantRecord
from app.capabilities.live_safety.upload_run import (
    UPLOAD_ARTIFACT_MISSING,
    UPLOAD_GRANT_NOT_ASSET,
    UPLOAD_GRANT_NOT_FOUND,
    UPLOAD_MEDIA_UNSUPPORTED,
    AssetUploadRun,
)
from app.platform.core.errors import AppError
from app.platform.core.execution import ExecutionMode
from app.stages.products.image_model import ImageAssetKind
from app.stages.register.model import GuidanceAssetKind

AT = datetime(2026, 10, 4, tzinfo=UTC)
SOURCE = ArtifactRef(ImageAssetKind.SOURCE_ASSET, "a" * 64, None)
DERIVED = ArtifactRef(ImageAssetKind.DERIVED_ARTIFACT, "b" * 64, "deriv-1")
THIRD = ArtifactRef(ImageAssetKind.SOURCE_ASSET, "c" * 64, None)


def grant(*artifacts: ArtifactRef, stage: MutationStage = MutationStage.ASSET) -> GrantRecord:
    return GrantRecord(
        grant_id="grant-1",
        stage=stage,
        marketplace_key="smartstore",
        marketplace_account_id="mpa-1",
        endpoint_group="product_image_upload",
        preparation_revision_id="prep-rev-1",
        candidate_fingerprint="f" * 64,
        artifacts=artifacts,
        asset_profile="profile-1",
        registration_snapshot_id=None,
        intent_id=None,
        idempotency_key=None,
        create_attempt_no=None,
        budget_max=10,
        budget_used=0,
        not_before=AT,
        expires_at=AT,
        state=GrantState.ACTIVE,
        approved_by="owner",
        authorization_ref="1",
    )


@dataclass
class Stored:
    mime_type: str


@dataclass
class Lineage:
    files: dict[str, tuple[bytes, str]]

    def get(self, sha256: str) -> Stored | None:
        found = self.files.get(sha256)
        return None if found is None else Stored(found[1])

    def read(self, sha256: str) -> bytes:
        return self.files[sha256][0]


@dataclass
class Attempt:
    attempt_id: str
    artifact_sha256: str
    asset_kind: ImageAssetKind
    state: UploadAttemptState
    provider_asset_ref: str | None
    asset_profile: str = "profile-1"


@dataclass
class Store:
    record: GrantRecord | None
    applied: tuple[Attempt, ...] = ()

    def grant_record(self, grant_id: str) -> GrantRecord | None:
        return self.record

    def applied_uploads(self, revision: str, candidate: str) -> tuple[Attempt, ...]:
        assert (revision, candidate) == ("prep-rev-1", "f" * 64)
        return self.applied

    def applied_uploads_for_account(self, marketplace: str, account: str) -> tuple[Attempt, ...]:
        assert (marketplace, account) == ("smartstore", "mpa-1")
        return self.applied


@dataclass
class Result:
    attempt: Attempt


@dataclass
class Uploads:
    events: list[tuple[str, Any]]
    outcomes: dict[str, UploadAttemptState] = field(default_factory=dict)
    refuse: bool = False

    def upload(self, request: Any) -> Result:
        self.events.append(("upload", request))
        if self.refuse:
            raise MutationRefused("LIVE_GRANT_EXPIRED", "the owner refused")
        state = self.outcomes.get(request.artifact.sha256, UploadAttemptState.APPLIED_PROVEN)
        ref = (
            "https://shop-phinf.example/x.jpg"
            if state is UploadAttemptState.APPLIED_PROVEN
            else None
        )
        return Result(
            Attempt(
                f"att-{request.artifact.sha256[:4]}",
                request.artifact.sha256,
                request.artifact.asset_kind,
                state,
                ref,
            )
        )


@dataclass
class ModeState:
    mode: ExecutionMode


@dataclass
class Mode:
    events: list[tuple[str, Any]]
    current: ExecutionMode = ExecutionMode.DRY_RUN

    def request_change(
        self, target: ExecutionMode, *, actor: str, reason: str | None, window_s: int | None = None
    ) -> ModeState:
        self.events.append(("mode", (target, window_s)))
        self.current = target
        return ModeState(target)

    def state(self) -> ModeState:
        return ModeState(self.current)


def run_of(store: Store, uploads: Uploads, events: list[tuple[str, Any]]) -> AssetUploadRun:
    files = {
        SOURCE.sha256: (b"source-bytes", "image/jpeg"),
        DERIVED.sha256: (b"derived-bytes", "image/png"),
        THIRD.sha256: (b"third-bytes", "image/webp"),
    }
    return AssetUploadRun(
        store=store,  # type: ignore[arg-type]
        uploads=uploads,  # type: ignore[arg-type]
        mode=Mode(events),  # type: ignore[arg-type]
        connect=lambda: events.append(("connect", None)),
        sources=Lineage({k: v for k, v in files.items() if k != DERIVED.sha256}),
        derived=Lineage({DERIVED.sha256: files[DERIVED.sha256]}),
    )


def kinds(events: list[tuple[str, Any]]) -> list[str]:
    return [e[0] if e[0] != "mode" else f"mode:{e[1][0].value}" for e in events]


def test_every_artifact_is_uploaded_once_inside_a_closed_window() -> None:
    events: list[tuple[str, Any]] = []
    result = run_of(Store(grant(SOURCE, DERIVED)), Uploads(events), events).run(
        "grant-1", window_s=900, actor="op", correlation_id="cid"
    )
    assert kinds(events) == ["mode:LIVE", "connect", "upload", "upload", "mode:DRY_RUN"]
    assert events[0][1] == (ExecutionMode.LIVE, 900)
    first, second = events[2][1], events[3][1]
    assert (first.content, first.media_type, first.file_name) == (
        b"source-bytes",
        "image/jpeg",
        f"{'a' * 16}.jpg",
    )
    assert (second.content, second.media_type, second.file_name) == (
        b"derived-bytes",
        "image/png",
        f"{'b' * 16}.png",
    )
    assert result.complete and result.mode_after == "DRY_RUN"
    assert [i.outcome for i in result.items] == ["APPLIED_PROVEN", "APPLIED_PROVEN"]


def test_the_run_records_an_unproven_upload_and_continues_without_retrying_it() -> None:
    events: list[tuple[str, Any]] = []
    uploads = Uploads(events, outcomes={SOURCE.sha256: UploadAttemptState.UPLOAD_UNKNOWN})
    result = run_of(Store(grant(SOURCE, DERIVED, THIRD)), uploads, events).run(
        "grant-1", window_s=900, actor="op", correlation_id="cid"
    )
    assert kinds(events) == [
        "mode:LIVE",
        "connect",
        "upload",
        "upload",
        "upload",
        "mode:DRY_RUN",
    ]
    assert [i.outcome for i in result.items] == [
        "UPLOAD_UNKNOWN",
        "APPLIED_PROVEN",
        "APPLIED_PROVEN",
    ]
    assert not result.complete


def test_a_refusal_is_the_owners_and_the_window_is_still_closed() -> None:
    events: list[tuple[str, Any]] = []
    with pytest.raises(MutationRefused):
        run_of(Store(grant(SOURCE)), Uploads(events, refuse=True), events).run(
            "grant-1", window_s=900, actor="op", correlation_id="cid"
        )
    assert kinds(events) == ["mode:LIVE", "connect", "upload", "mode:DRY_RUN"]


def test_a_failed_connect_sends_nothing_and_closes_the_window() -> None:
    events: list[tuple[str, Any]] = []

    def broken() -> None:
        events.append(("connect", None))
        raise AppError("SMARTSTORE_TOKEN_REFUSED", "the provider refused the token")

    run = run_of(Store(grant(SOURCE)), Uploads(events), events)
    run._connect = broken
    with pytest.raises(AppError):
        run.run("grant-1", window_s=900, actor="op", correlation_id="cid")
    assert kinds(events) == ["mode:LIVE", "connect", "mode:DRY_RUN"]


def test_an_already_applied_artifact_is_not_uploaded_again() -> None:
    events: list[tuple[str, Any]] = []
    applied = Attempt(
        "att-old",
        SOURCE.sha256,
        ImageAssetKind.SOURCE_ASSET,
        UploadAttemptState.APPLIED_PROVEN,
        "https://shop-phinf.example/old.jpg",
    )
    result = run_of(Store(grant(SOURCE, DERIVED), (applied,)), Uploads(events), events).run(
        "grant-1", window_s=900, actor="op", correlation_id="cid"
    )
    assert [e[1].artifact.sha256 for e in events if e[0] == "upload"] == [DERIVED.sha256]
    assert {i.outcome for i in result.items} == {"ALREADY_APPLIED", "APPLIED_PROVEN"}
    assert result.complete
    # Nothing pending: no window, no CONNECT, no upload.
    events.clear()
    done = run_of(Store(grant(SOURCE), (applied,)), Uploads(events), events).run(
        "grant-1", window_s=900, actor="op", correlation_id="cid"
    )
    assert events == [] and done.complete


@pytest.mark.parametrize(
    ("record", "files", "code"),
    [
        (None, None, UPLOAD_GRANT_NOT_FOUND),
        (grant(SOURCE, stage=MutationStage.CREATE), None, UPLOAD_GRANT_NOT_ASSET),
        (grant(SOURCE), {}, UPLOAD_ARTIFACT_MISSING),
        (grant(SOURCE), {SOURCE.sha256: (b"x", "image/bmp")}, UPLOAD_MEDIA_UNSUPPORTED),
    ],
)
def test_nothing_starts_when_the_run_cannot_be_formed(
    record: GrantRecord | None, files: dict[str, tuple[bytes, str]] | None, code: str
) -> None:
    events: list[tuple[str, Any]] = []
    run = run_of(Store(record), Uploads(events), events)
    if files is not None:
        run._sources = Lineage(files)
    with pytest.raises(AppError) as refused:
        run.run("grant-1", window_s=900, actor="op", correlation_id="cid")
    assert refused.value.code == code
    assert events == []


# ---------------------------------------------------------------- ADR-0033 §8: the guidance source

NOTICE_PNG = b"\x89PNG\r\n\x1a\n" + b"rendered-notice"
NOTICE = ArtifactRef(
    GuidanceAssetKind.GUIDANCE_ARTIFACT, hashlib.sha256(NOTICE_PNG).hexdigest(), None
)


@dataclass
class Notices:
    """The Detail Guidance owner's recorded images, as the run reads them."""

    images: dict[str, bytes]
    reads: list[str] = field(default_factory=list)

    def image(self, sha256: str) -> bytes:
        self.reads.append(sha256)
        if sha256 not in self.images:
            raise AppError("GUIDANCE_IMAGE_UNKNOWN", "no guidance image has that checksum")
        return self.images[sha256]


def guided_run(
    store: Store, uploads: Uploads, events: list[tuple[str, Any]], notices: Notices | None
) -> AssetUploadRun:
    run = run_of(store, uploads, events)
    run._guidance = notices
    return run


def test_a_guidance_image_is_read_from_the_guidance_store_only() -> None:
    events: list[tuple[str, Any]] = []
    notices = Notices({NOTICE.sha256: NOTICE_PNG})
    result = guided_run(Store(grant(SOURCE, NOTICE)), Uploads(events), events, notices).run(
        "grant-1", window_s=900, actor="op", correlation_id="cid"
    )
    assert kinds(events) == ["mode:LIVE", "connect", "upload", "upload", "mode:DRY_RUN"]
    sent = [e[1] for e in events if e[0] == "upload"]
    assert (sent[1].artifact, sent[1].content, sent[1].media_type, sent[1].file_name) == (
        NOTICE,
        NOTICE_PNG,
        "image/png",
        f"{NOTICE.sha256[:16]}.png",
    )
    assert notices.reads == [NOTICE.sha256]
    assert result.complete
    assert {i.asset_kind for i in result.items} == {"SOURCE_ASSET", "GUIDANCE_ARTIFACT"}


@pytest.mark.parametrize(
    ("notices", "code"),
    [
        (None, UPLOAD_ARTIFACT_MISSING),
        (Notices({}), UPLOAD_ARTIFACT_MISSING),
        (Notices({NOTICE.sha256: NOTICE_PNG + b"x"}), UPLOAD_ARTIFACT_MISSING),
    ],
    ids=["no-store", "not-recorded", "not-the-granted-bytes"],
)
def test_a_guidance_image_that_cannot_be_read_exactly_stops_the_run_before_anything(
    notices: Notices | None, code: str
) -> None:
    events: list[tuple[str, Any]] = []
    run = guided_run(Store(grant(SOURCE, NOTICE)), Uploads(events), events, notices)
    with pytest.raises(AppError) as refused:
        run.run("grant-1", window_s=900, actor="op", correlation_id="cid")
    assert refused.value.code == code
    assert events == []


def test_a_guidance_image_that_is_not_a_png_is_refused() -> None:
    events: list[tuple[str, Any]] = []
    jpeg = b"\xff\xd8\xffnot-a-png"
    notice = ArtifactRef(
        GuidanceAssetKind.GUIDANCE_ARTIFACT, hashlib.sha256(jpeg).hexdigest(), None
    )
    run = guided_run(Store(grant(notice)), Uploads(events), events, Notices({notice.sha256: jpeg}))
    with pytest.raises(AppError) as refused:
        run.run("grant-1", window_s=900, actor="op", correlation_id="cid")
    assert refused.value.code == UPLOAD_MEDIA_UNSUPPORTED
    assert events == []


def test_a_guidance_image_already_applied_for_the_account_is_reused() -> None:
    """One store-wide notice is uploaded once per account (ADR-0033 §8)."""
    events: list[tuple[str, Any]] = []
    applied = Attempt(
        "att-notice",
        NOTICE.sha256,
        GuidanceAssetKind.GUIDANCE_ARTIFACT,  # type: ignore[arg-type]
        UploadAttemptState.APPLIED_PROVEN,
        "https://shop-phinf.example/notice.png",
    )
    notices = Notices({NOTICE.sha256: NOTICE_PNG})
    result = guided_run(
        Store(grant(SOURCE, NOTICE), (applied,)), Uploads(events), events, notices
    ).run("grant-1", window_s=900, actor="op", correlation_id="cid")
    assert [e[1].artifact.sha256 for e in events if e[0] == "upload"] == [SOURCE.sha256]
    assert notices.reads == []
    assert {(i.sha256, i.outcome) for i in result.items} == {
        (NOTICE.sha256, "ALREADY_APPLIED"),
        (SOURCE.sha256, "APPLIED_PROVEN"),
    }
    assert result.complete
