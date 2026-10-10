"""ADR-0033 G5: the DRY_RUN acceptance of a top and a bottom notice, end to end (§6–§9).

Through the application's own owners on a migrated database, with fakes only — no provider is
reached, nothing is LIVE:

1. the SmartStore detail composition is stamped at content v3 by the authoring-revision owner;
2. the operator saves a STANDING top notice and a PERIOD bottom notice active at the injected clock;
3. a SmartStore unit with a selected detail image is prepared, and its candidate is READY — the
   notices are placed, so ``PUBLICATION_GUIDANCE_UNPLACED`` no longer fires;
4. the ASSET grant names exactly the Item images **plus** the guidance images; an Item-only set is
   ``LIVE_GRANT_ARTIFACT_SET_MISMATCH``;
5. the upload run reads the guidance bytes from the guidance image store (the third byte source)
   and uploads them through the upload owner, behind the safety stack, to a provider-zero sender;
6. the Snapshot is frozen as ``registration-payload/v3`` and pins its ``guidance_assets``;
7. the SmartStore projection renders ``detailContent`` as top notice → detail image → body →
   bottom notice; a guidance mismatch is ``WIRE_GUIDANCE_PLAN_MISMATCH``;
8. after the freeze, a period that ends makes the frozen Snapshot stale, and the Snapshot is never
   edited.
"""

import contextlib
import copy
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command

from app.capabilities.audit.service import AuditLog
from app.capabilities.detail_guidance.image_store import GuidanceImageStore
from app.capabilities.detail_guidance.renderer import Template
from app.capabilities.detail_guidance.store import (
    DetailGuidanceStore,
    GuidanceRevisionRecord,
    Kind,
    Placement,
)
from app.capabilities.live_safety.assets import (
    AssetUploadService,
    PreparationCandidateGate,
    UploadSendResult,
)
from app.capabilities.live_safety.drill import unit_artifacts
from app.capabilities.live_safety.model import UploadAttemptState, WireHostPolicy
from app.capabilities.live_safety.stack import SafetyStack
from app.capabilities.live_safety.store import ArtifactRef, LiveAuthorityStore
from app.capabilities.live_safety.upload_run import AssetUploadRun
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import InputValidationError
from app.platform.core.execution import ExecutionMode
from app.platform.db.migrate import alembic_config, upgrade_to_head
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from app.stages.collect.imagedecode import HeaderImageDecoder
from app.stages.products.image_model import (
    ImageAssetKind,
    OutputRole,
    QaVerdict,
    SelectedOutput,
    SourceDecision,
    SourceDecisionKind,
)
from app.stages.products.image_store import DerivedImageStore
from app.stages.products.model import ReadinessStatus
from app.stages.register.authoring import AuthoredInputs
from app.stages.register.authoring_revisions import AuthoringRevisionKind
from app.stages.register.model import GuidanceAssetKind, ListingShape, RegistrationConflictError
from app.stages.register.policy import StaticRegistrationMetadata, StaticRegistrationPolicy
from app.stages.register.preparation import (
    PREPARED_ASSET_NOT_SELECTED,
    PUBLICATION_GUIDANCE_UNPLACED,
    CategoryConfirmation,
    CategorySelection,
    DetailComposition,
)
from integrations.marketplaces.smartstore import product
from tests.support.collect_support import PNG
from tests.support.jobs_support import FakeClock
from tests.support.live_safety_support import PermittedMode, ProvenProofs, ScriptedSender
from tests.support.product_support import Collections, FakeDecoder, context, raw
from tests.support.register_support import (
    CATEGORY,
    CID,
    OPERATOR,
    TAXONOMY,
    FakeCapability,
    establish,
    listing,
    metadata,
    target,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
SMARTSTORE = "smartstore"
# A synthetic approval reference of the owners' comment-id form; no real approval is implied.
APPROVAL = "1000000001"
HOSTS = WireHostPolicy({SMARTSTORE: "api.commerce.naver.com"})
BODY = "invented body text"
TOP: dict[str, Any] = {"blocks": [{"heading": "당일발송", "lines": ["12시 이전 주문시 당일발송"]}]}
HOLIDAY: dict[str, Any] = {"blocks": [{"heading": "휴무 안내", "lines": ["연휴 기간 출고 휴무"]}]}


def ref(sha256: str) -> str:
    """The provider reference the provider-zero sender answers for exactly these bytes."""
    return f"https://shop-phinf.pstatic.net/20261010_1/{sha256[:12]}.png"


def img(sha256: str) -> str:
    return f'<img src="{ref(sha256)}" alt="">'


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def notices(container: Container, config: AppConfig, clock: FakeClock) -> DetailGuidanceStore:
    """The Detail Guidance owner over the application's database, images and injected clock."""
    return DetailGuidanceStore(
        container.db,
        clock,
        AuditLog(container.db, clock),
        GuidanceImageStore(config.guidance_images_dir),
    )


def _save(store: DetailGuidanceStore, **overrides: Any) -> GuidanceRevisionRecord:
    values: dict[str, Any] = {
        "placement": Placement.TOP,
        "kind": Kind.STANDING,
        "guidance_id": None,
        "template": Template.CLEAN,
        "content": TOP,
        "enabled": True,
        "starts_at": None,
        "ends_at": None,
        "expected_current_seq": None,
        "actor": OPERATOR,
        "correlation_id": CID,
    }
    values.update(overrides)
    return store.save(**values)


@dataclass(frozen=True)
class Unit:
    account: str
    item_id: str
    representative: str
    detail: str
    preparation_id: str


def _item(container: Container, config: AppConfig) -> tuple[str, str, str]:
    """A SmartStore-priced Item whose current selection is a representative and a detail image,
    each with a PASS for its exact binary."""
    assets = SourceAssetStore(config.source_assets_dir, container.db, FakeDecoder(), FakeClock())
    detail = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=1,
        host="img.shop.example",
        provenance=".detail img:nth-of-type(1)",
        status=FieldStatus.CONFIRMED,
        sha256=assets.put(PNG + b"dg5-detail").sha256,
    )
    run_id, revision = Collections.of(container, config).collect(
        source_product_id="5505", extra_images=(detail,)
    )
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None, result
    item_id = result.item_id
    representative = next(i for i in revision.images if i.role is not ImageRole.DETAIL)
    container.images.record_operator_selection(
        item_id,
        source_revision_id=revision.revision_id,
        decisions=[
            SourceDecision(i.role, i.ordinal, i.sha256, SourceDecisionKind.USE_SOURCE)
            for i in (representative, detail)
        ],
        outputs=[
            SelectedOutput(OutputRole.REPRESENTATIVE, representative.role, representative.ordinal),
            SelectedOutput(OutputRole.DETAIL, detail.role, detail.ordinal),
        ],
        decided_by=OPERATOR,
    )
    selection = container.images.current_selection(item_id)
    assert selection is not None
    for output in selection.outputs:
        container.images.record_qa(
            asset_kind=output.asset_kind,
            sha256=output.sha256,
            derivation_id=output.derivation_id,
            validated_source_revision_id=selection.source_revision_id,
            verdict=QaVerdict.PASS,
            decided_by="qa-1",
        )
    return item_id, representative.sha256, detail.sha256


def _unit(container: Container, config: AppConfig) -> Unit:
    """One SmartStore unit, authored through the application's own preparation owner against the
    owner-held category mapping and the v3 detail composition the server stamps for SmartStore."""
    account = establish(container, config, SMARTSTORE, "uid-smartstore-dg5")
    with container.db.write() as session:
        stamped = container.authoring_revisions.stamp(
            session, SMARTSTORE, TAXONOMY, correlation_id=CID
        )
    composition = stamped["detail_composition_revision"]
    current = container.authoring_revisions.current(
        AuthoringRevisionKind.DETAIL_COMPOSITION, SMARTSTORE
    )
    assert current is not None and current.revision_id == composition
    assert current.content["content_version"] == "registration-detail-composition/v3"
    pricing = context(marketplace_key=SMARTSTORE)
    served = container.registration_preflight
    served._policies = StaticRegistrationPolicy(
        (
            target(
                account,
                marketplace_key=SMARTSTORE,
                pricing_context=pricing,
                category_mapping_revision=stamped["category_mapping_revision"],
                detail_composition_revision=composition,
                after_service_telephone="02-000-0000",
                duplicate_proof_required=False,
            ),
        )
    )
    served._metadata = StaticRegistrationMetadata((metadata(),), marketplace_key=SMARTSTORE)
    served._capability = FakeCapability()
    item_id, representative, detail = _item(container, config)
    priced = container.pricing.price(item_id, pricing).snapshot
    assert priced is not None
    with container.registrations.transaction() as unit:
        draft = unit.create_draft(
            SMARTSTORE,
            account,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR,
            correlation_id=CID,
        )
        unit.add_draft_item(
            draft.draft_id,
            item_id,
            priced.pricing_snapshot_id,
            added_by=OPERATOR,
            correlation_id=CID,
        )
    record = container.registration_preparations.create(
        draft.draft_id,
        item_ids=[item_id],
        inputs=AuthoredInputs(
            CategorySelection(
                CATEGORY,
                stamped["category_mapping_revision"],
                TAXONOMY,
                CategoryConfirmation.OPERATOR_CONFIRMED,
            ),
            listing([]),
            # The client names BODY; the server stores the owned profile's sections (B-DETAIL).
            DetailComposition(composition, BODY, ("BODY",)),
        ),
        actor=OPERATOR,
    )
    return Unit(account, item_id, representative, detail, record.preparation_id)


@dataclass
class _Mode:
    """The run's execution-mode owner, recorded only: nothing here changes the real M0 mode."""

    requested: list[ExecutionMode] = field(default_factory=list)

    @dataclass(frozen=True)
    class State:
        mode: ExecutionMode

    def request_change(
        self, target: ExecutionMode, *, actor: str, reason: str | None, window_s: int | None = None
    ) -> "_Mode.State":
        self.requested.append(target)
        return self.State(target)

    def state(self) -> "_Mode.State":
        return self.State(self.requested[-1] if self.requested else ExecutionMode.DRY_RUN)


@dataclass
class _Sender(ScriptedSender):
    """A provider-zero ASSET sender: every upload is applied, with a reference of its bytes."""

    def send(self, *, content: bytes, file_name: str, media_type: str) -> UploadSendResult:
        self.calls.append({"content": content, "file_name": file_name, "media_type": media_type})
        return UploadSendResult(
            UploadAttemptState.APPLIED_PROVEN,
            provider_asset_ref=ref(hashlib.sha256(content).hexdigest()),
        )


def _upload_run(
    container: Container, config: AppConfig, notices: DetailGuidanceStore, sender: _Sender
) -> AssetUploadRun:
    store = LiveAuthorityStore(container.db, container.clock, container.audit)
    uploads = AssetUploadService(
        store=store,
        stack=SafetyStack(
            store=store, mode=PermittedMode(), proofs=ProvenProofs(), clock=container.clock
        ),
        sender=sender,
        hosts=HOSTS,
        candidates=PreparationCandidateGate(
            container.registration_preparations, container.registrations
        ),
        clock=container.clock,
    )
    return AssetUploadRun(
        store=store,
        uploads=uploads,
        mode=_Mode(),
        connect=lambda: None,
        sources=container.source_assets,
        derived=DerivedImageStore(config.derived_images_dir, container.db, HeaderImageDecoder()),
        guidance=notices,
    )


def _artifacts(unit: Unit, *guidance: str) -> list[ArtifactRef]:
    return [
        ArtifactRef(ImageAssetKind.SOURCE_ASSET, unit.representative, None),
        ArtifactRef(ImageAssetKind.SOURCE_ASSET, unit.detail, None),
        *(ArtifactRef(GuidanceAssetKind.GUIDANCE_ARTIFACT, sha, None) for sha in guidance),
    ]


def _grant(container: Container, unit: Unit, artifacts: list[ArtifactRef]) -> Any:
    candidate = container.registration_preparations.stage_candidate(unit.preparation_id)
    preparation = container.registrations.preparation(unit.preparation_id)
    assert preparation is not None
    now = container.clock.now()
    return container.live_authority.issue_asset_grant(
        marketplace_key=SMARTSTORE,
        marketplace_account_id=unit.account,
        preparation_revision_id=preparation.current.preparation_revision_id,
        candidate_fingerprint=candidate.candidate_fingerprint,
        artifacts=artifacts,
        asset_profile=candidate.resolved.target.asset_policy.profile,
        budget=len(artifacts),
        not_before=now,
        expires_at=now + timedelta(hours=1),
        approved_by=OPERATOR,
        authorization_ref=APPROVAL,
        correlation_id=CID,
    )


def test_a_top_and_a_bottom_notice_reach_the_detail_content_end_to_end(
    container: Container, config: AppConfig, notices: DetailGuidanceStore, clock: FakeClock
) -> None:
    standing = _save(notices)
    holiday = _save(
        notices,
        placement=Placement.BOTTOM,
        kind=Kind.PERIOD,
        template=Template.WARM,
        content=HOLIDAY,
        starts_at=T0 - timedelta(hours=1),
        ends_at=T0 + timedelta(hours=2),
    )
    top, bottom = standing.image_sha256, holiday.image_sha256
    unit = _unit(container, config)

    # The candidate places both notices: READY, and the unit's plan is the v3 one.
    candidate = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert candidate.status is ReadinessStatus.READY, candidate.codes
    assert candidate.upload_permitted
    assert PUBLICATION_GUIDANCE_UNPLACED not in candidate.codes
    assert [e.sha256 for e in candidate.resolved.guidance.top] == [top]
    assert [e.sha256 for e in candidate.resolved.guidance.bottom] == [bottom]
    assert candidate.request.detail is not None
    assert candidate.request.detail.sections == (
        "TOP_GUIDANCE",
        "DETAIL_IMAGES",
        "BODY",
        "BOTTOM_GUIDANCE",
    )

    # The ASSET restore drill copies and verifies every artifact of the unit, the guidance images
    # included, from where each one's own store holds it.
    drilled = unit_artifacts(candidate.resolved)
    assert {(a.asset_kind.value, a.sha256) for a in drilled} == {
        ("SOURCE_ASSET", unit.representative),
        ("SOURCE_ASSET", unit.detail),
        ("GUIDANCE_ARTIFACT", top),
        ("GUIDANCE_ARTIFACT", bottom),
    }
    for artifact in drilled:
        stored = (config.data_dir / artifact.relative).read_bytes()
        assert hashlib.sha256(stored).hexdigest() == artifact.sha256

    # The ASSET grant names exactly the Item images plus the guidance images (§8).
    with pytest.raises(InputValidationError) as item_only:
        _grant(container, unit, _artifacts(unit))
    assert item_only.value.code == "LIVE_GRANT_ARTIFACT_SET_MISMATCH"
    assert item_only.value.details == {"missing": 2, "extra": 0}
    grant = _grant(container, unit, _artifacts(unit, top, bottom))
    with LiveAuthorityStore(container.db, container.clock, container.audit).transaction() as live:
        live.release(
            actor=OPERATOR,
            reason_code="CANARY_WINDOW",
            authorization_ref=APPROVAL,
            correlation_id=CID,
        )

    # The upload run reads the guidance bytes from the guidance image store and uploads them.
    sender = _Sender()
    result = _upload_run(container, config, notices, sender).run(
        grant.grant_id, window_s=900, actor=OPERATOR, correlation_id=CID
    )
    assert result.complete, result.items
    assert {i.asset_kind for i in result.items if i.sha256 in (top, bottom)} == {
        "GUIDANCE_ARTIFACT"
    }
    sent = {hashlib.sha256(call["content"]).hexdigest(): call for call in sender.calls}
    assert set(sent) == {unit.representative, unit.detail, top, bottom}
    for sha in (top, bottom):
        assert sent[sha]["content"] == notices.image(sha)
        assert sent[sha]["media_type"] == "image/png"
    with contextlib.closing(raw(config)) as connection:
        kinds = dict(
            connection.execute(
                "SELECT artifact_sha256, asset_kind FROM asset_upload_attempts"
                " WHERE state = 'APPLIED_PROVEN'"
            ).fetchall()
        )
    assert kinds[top] == kinds[bottom] == "GUIDANCE_ARTIFACT"

    # The Snapshot is frozen as registration-payload/v3 and pins its guidance assets.
    frozen = container.registration_preparations.freeze_current(
        unit.preparation_id, actor=OPERATOR, correlation_id=CID
    )
    snapshot_id = frozen.snapshot.registration_snapshot_id
    payload = container.registrations.snapshot_payload(snapshot_id)
    assert payload is not None
    assert payload["builder_version"] == "registration-payload/v3"
    pinned = [
        {
            "asset_kind": "GUIDANCE_ARTIFACT",
            "sha256": sha,
            "derivation_id": None,
            "asset_profile": candidate.resolved.target.asset_policy.profile,
            "provider_asset_ref": ref(sha),
        }
        for sha in (top, bottom)
    ]
    assert payload["guidance_assets"] == pinned
    assert [e["sha256"] for e in payload["detail"]["guidance"]["top"]] == [top]
    assert payload["detail"]["guidance"]["bottom"] == [
        {
            "source": "STORE",
            "guidance_revision_id": holiday.revision_id,
            "template": "WARM",
            "sha256": bottom,
        }
    ]
    # URL-free but for the uploaded provider references (DG-06).
    assert "://" not in json.dumps(payload["detail"])
    with contextlib.closing(raw(config)) as connection:
        column = connection.execute(
            "SELECT guidance_assets_json FROM registration_snapshots"
            " WHERE registration_snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()[0]
    assert json.loads(column) == pinned

    # The projection: top notice → detail image → body → bottom notice (§7).
    projection = product.project(payload)
    assert projection.encoding_version == "smartstore-register-wire/v10"
    content = projection.document.mapping()["originProduct"]["detailContent"]
    assert content == "\n".join([img(top), img(unit.detail), f"<p>{BODY}</p>", img(bottom)])
    # Neither a notice nor a detail image is ever a gallery image.
    assert projection.image_references == (ref(unit.representative),)
    # A guidance plan that is not exactly the Snapshot's guidance assets is refused.
    mismatched = copy.deepcopy(payload)
    mismatched["guidance_assets"].reverse()
    with pytest.raises(product.WireContractError) as refused:
        product.project(mismatched)
    assert refused.value.code == "WIRE_GUIDANCE_PLAN_MISMATCH"

    # The first CREATE copy reproduces the frozen inputs from the owners, guidance included.
    copy_ = container.registration_preparations.execution_copy(snapshot_id)
    assert {a.sha256 for a in copy_.final.prepared_assets} >= {top, bottom}

    # After the freeze, the period ends: the frozen Snapshot is stale and is never edited (DG-05).
    clock.advance(2 * 60 * 60)
    with pytest.raises(RegistrationConflictError) as stale:
        container.registration_preparations.execution_copy(snapshot_id)
    assert stale.value.code == "REGISTER_FIRST_CREATE_INPUTS_NOT_READY"
    assert PREPARED_ASSET_NOT_SELECTED in stale.value.details["reasons"]
    later = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert later.resolved.guidance.bottom == ()
    assert later.candidate_fingerprint != candidate.candidate_fingerprint
    assert container.registrations.snapshot_payload(snapshot_id) == payload


def test_a_unit_whose_notices_are_off_freezes_v3_with_no_guidance_asset(
    container: Container, config: AppConfig, notices: DetailGuidanceStore, tmp_path: Path
) -> None:
    """A v3 unit without a notice: empty lists, no guidance asset, nothing more to upload, and
    exactly the detail content a v2 unit renders — the detail image, then the body."""
    unit = _unit(container, config)
    candidate = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert candidate.status is ReadinessStatus.READY, candidate.codes
    assert candidate.resolved.guidance.empty
    grant = _grant(container, unit, _artifacts(unit))
    with LiveAuthorityStore(container.db, container.clock, container.audit).transaction() as live:
        live.release(
            actor=OPERATOR,
            reason_code="CANARY_WINDOW",
            authorization_ref=APPROVAL,
            correlation_id=CID,
        )
    assert (
        _upload_run(container, config, notices, _Sender())
        .run(grant.grant_id, window_s=900, actor=OPERATOR, correlation_id=CID)
        .complete
    )
    frozen = container.registration_preparations.freeze_current(
        unit.preparation_id, actor=OPERATOR, correlation_id=CID
    )
    payload = container.registrations.snapshot_payload(frozen.snapshot.registration_snapshot_id)
    assert payload is not None
    assert payload["builder_version"] == "registration-payload/v3"
    assert payload["guidance_assets"] == []
    assert payload["detail"]["guidance"] == {"top": [], "bottom": []}
    content = product.project(payload).document.mapping()["originProduct"]["detailContent"]
    assert content == "\n".join([img(unit.detail), f"<p>{BODY}</p>"])

    # ---- migration 0059: the pin is tied to the payload, and its evidence is never dropped.
    snapshot_id = frozen.snapshot.registration_snapshot_id

    def copied(guidance: str | None) -> None:
        """A raw copy of the frozen v3 row under a new identity, with another pin."""
        with contextlib.closing(raw(config)) as connection:
            connection.execute(
                "INSERT INTO registration_snapshots SELECT ?, draft_id, draft_revision,"
                " marketplace_key, marketplace_account_id, listing_shape, ?,"
                " preflight_rule_version, preflight_fingerprint, category_mapping_revision,"
                " taxonomy_revision, policy_revisions_json, detail_composition_revision,"
                " sanitizer_profile_version, payload_hash, payload_json, created_by,"
                " correlation_id, created_at, ?"
                " FROM registration_snapshots WHERE registration_snapshot_id = ?",
                (str(uuid.uuid4()), f"icbm-{uuid.uuid4().hex}", guidance, snapshot_id),
            )
            connection.commit()

    for unpinned in (None, "{}", "not json"):
        with pytest.raises(sqlite3.IntegrityError, match="guidance_assets_pinned"):
            copied(unpinned)
    # The downgrade runs on a copy: the application's process owns its own data directory.
    database = tmp_path / "icbm.db"
    with (
        contextlib.closing(raw(config)) as source,
        contextlib.closing(sqlite3.connect(database)) as target,
    ):
        source.backup(target)
    url = f"sqlite:///{database.as_posix()}"
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0058_detail_guidance")
    with contextlib.closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0059_detail_guidance_placement",
        )
        assert connection.execute(
            "SELECT guidance_assets_json FROM registration_snapshots"
            " WHERE registration_snapshot_id = ?",
            (snapshot_id,),
        ).fetchone() == ("[]",)


def test_0059_steps_down_empty_keeping_every_index_and_trigger(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)

    def schema(name: str) -> str:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            return str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
                ).fetchone()[0]
            )

    def objects() -> set[str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            return {
                r[0]
                for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE tbl_name = 'asset_upload_attempts'"
                )
            }

    kept = objects()
    assert "GUIDANCE_ARTIFACT" in schema("asset_upload_attempts")
    assert "guidance_assets_json" in schema("registration_snapshots")
    # Empty, it steps down to exactly the schema before it — every index and trigger kept — and
    # back up.
    command.downgrade(alembic_config(url), "0058_detail_guidance")
    assert "GUIDANCE_ARTIFACT" not in schema("asset_upload_attempts")
    assert "guidance_assets_json" not in schema("registration_snapshots")
    assert objects() == kept
    command.upgrade(alembic_config(url), "head")
    assert "GUIDANCE_ARTIFACT" in schema("asset_upload_attempts")
    assert objects() == kept
