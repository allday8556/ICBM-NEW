"""ADR-0037 C2: a 25 MB description image reaches SmartStore as its upload-fit variant (§2, §3).

Through the application's own owners on a migrated database, provider-zero — the SmartStore image
upload is the production sender over a fake transport, nothing is LIVE:

1. a SmartStore unit whose detail image is a 25 MB-class JPEG is prepared; saving it makes the
   ``smartstore-upload-fit/v1`` variant in the M4 lineage, with its own automatic QA, and the
   source asset is never altered (IC-03);
2. the candidate is READY and publishes the variant — ``DERIVED_ARTIFACT``, its SHA-256 and its
   derivation — in place of the selected source image (IC-05);
3. the ASSET grant names exactly the variant; naming the source instead is refused;
4. the upload run reads the variant's bytes from the M4 lineage store and the fake SmartStore
   receives less than 10^7 bytes per call (IC-02); the attempt records the variant;
5. the Snapshot freezes the variant's SHA-256 with its lineage; saving again reuses the variant;
6. an oversize image the recipe cannot decode is ``REVIEW_REQUIRED`` as
   ``IMAGE_UPLOAD_FIT_FAILED`` and no grant can name it (IC-06).
"""

import contextlib
import hashlib
import io
import json
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
from PIL import Image

from app.capabilities.audit.models import AuditEventType
from app.capabilities.live_safety.assets import AssetUploadService, PreparationCandidateGate
from app.capabilities.live_safety.model import WireHostPolicy
from app.capabilities.live_safety.stack import SafetyStack
from app.capabilities.live_safety.store import ArtifactRef, LiveAuthorityStore
from app.capabilities.live_safety.upload_run import AssetUploadRun
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import InputValidationError
from app.platform.core.execution import ExecutionMode
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from app.stages.collect.imagedecode import HeaderImageDecoder
from app.stages.products.image_model import (
    AUTO_QA_RULE_VERSION,
    ImageAssetKind,
    OutputRole,
    QaVerdict,
    SelectedOutput,
    SourceDecision,
    SourceDecisionKind,
)
from app.stages.products.image_store import DerivedImageStore
from app.stages.products.model import ReadinessStatus
from app.stages.register import upload_fit, upload_variants
from app.stages.register.authoring import AuthoredInputs
from app.stages.register.authoring_revisions import AuthoringRevisionKind
from app.stages.register.model import ListingShape
from app.stages.register.policy import StaticRegistrationMetadata, StaticRegistrationPolicy
from app.stages.register.preparation import (
    IMAGE_UPLOAD_FIT_FAILED,
    CategoryConfirmation,
    CategorySelection,
    DetailComposition,
    PublicationImage,
)
from app.stages.register.upload_fit import (
    UPLOAD_FIT_MAX_WIDTH,
    UPLOAD_FIT_THRESHOLD,
    UPLOAD_FIT_VERSION,
)
from integrations.marketplaces.smartstore import product
from integrations.marketplaces.smartstore.assets import SmartStoreAssetSender
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from tests.support.jobs_support import FakeClock
from tests.support.live_safety_support import PermittedMode, ProvenProofs
from tests.support.product_support import Collections, FakeDecoder, context, raw
from tests.support.register_support import (
    CATEGORY,
    CID,
    MARKET,
    OPERATOR,
    TAXONOMY,
    FakeCapability,
    establish,
    listing,
    metadata,
    target,
)

pytestmark = pytest.mark.integration

SMARTSTORE = "smartstore"
# A synthetic approval reference of the owners' comment-id form; no real approval is implied.
APPROVAL = "1000000002"
HOSTS = WireHostPolicy({SMARTSTORE: "api.commerce.naver.com"})
BODY = "invented body text"
ONE_CALL_LIMIT = 10**7


def big_jpeg() -> bytes:
    """A 25 MB-class description image: random noise compresses poorly, so a 4000 px wide JPEG of
    it is far over the upload-fit threshold. Seeded, so every run makes the same bytes."""
    width, height = 4000, 5400
    noise = random.Random(37).randbytes(width * height * 3)
    buffer = io.BytesIO()
    Image.frombytes("RGB", (width, height), noise).save(buffer, "JPEG", quality=95)
    data = buffer.getvalue()
    assert 20_000_000 < len(data) < 30_000_000, len(data)
    return data


@dataclass(frozen=True)
class Unit:
    account: str
    item_id: str
    representative: str
    detail: str
    preparation_id: str
    draft_id: str


def _item(container: Container, config: AppConfig, detail_bytes: bytes) -> tuple[str, str, str]:
    """A SmartStore-priced Item whose current selection is a small representative image and the
    given description image, each with an operator PASS for its exact binary."""
    if detail_bytes.startswith(b"\x89PNG"):
        # Stored as the fake decoder reads it: an image the recipe still cannot decode.
        stored = SourceAssetStore(
            config.source_assets_dir, container.db, FakeDecoder(), FakeClock()
        ).put(detail_bytes)
    else:
        stored = container.source_assets.put(detail_bytes)
    detail = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=1,
        host="img.shop.example",
        provenance=".detail img:nth-of-type(1)",
        status=FieldStatus.CONFIRMED,
        sha256=stored.sha256,
    )
    run_id, revision = Collections.of(container, config).collect(
        source_product_id="5506", extra_images=(detail,)
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


def _inputs(stamped: dict[str, Any], composition: str) -> AuthoredInputs:
    return AuthoredInputs(
        CategorySelection(
            CATEGORY,
            stamped["category_mapping_revision"],
            TAXONOMY,
            CategoryConfirmation.OPERATOR_CONFIRMED,
        ),
        listing([]),
        DetailComposition(composition, BODY, ("BODY",)),
    )


def _unit(
    container: Container,
    config: AppConfig,
    detail_bytes: bytes,
    marketplace: str = SMARTSTORE,
) -> Unit:
    """One SmartStore unit, authored through the application's own preparation owner against the
    owner-held category mapping and the detail composition the server stamps for SmartStore."""
    account = establish(container, config, marketplace, f"uid-{marketplace}-c2")
    with container.db.write() as session:
        stamped = container.authoring_revisions.stamp(
            session, marketplace, TAXONOMY, correlation_id=CID
        )
    composition = stamped["detail_composition_revision"]
    current = container.authoring_revisions.current(
        AuthoringRevisionKind.DETAIL_COMPOSITION, marketplace
    )
    assert current is not None and current.revision_id == composition
    pricing = context(marketplace_key=marketplace)
    served = container.registration_preflight
    served._policies = StaticRegistrationPolicy(
        (
            target(
                account,
                marketplace_key=marketplace,
                pricing_context=pricing,
                category_mapping_revision=stamped["category_mapping_revision"],
                detail_composition_revision=composition,
                after_service_telephone="02-000-0000",
            ),
        )
    )
    # Owner ruling #219 6190407624: SmartStore duplicate checking stays on; the store answers
    # "no match" here (synthetic, no provider).
    from tests.support.register_support import FakeDuplicateLookup

    container.registration_preparations._duplicate_lookup = FakeDuplicateLookup(
        marketplace_key=marketplace
    )
    served._metadata = StaticRegistrationMetadata((metadata(),), marketplace_key=marketplace)
    served._capability = FakeCapability()
    item_id, representative, detail = _item(container, config, detail_bytes)
    priced = container.pricing.price(item_id, pricing).snapshot
    assert priced is not None
    with container.registrations.transaction() as unit:
        draft = unit.create_draft(
            marketplace,
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
        inputs=_inputs(stamped, composition),
        actor=OPERATOR,
    )
    return Unit(account, item_id, representative, detail, record.preparation_id, draft.draft_id)


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


class _FakeSmartStore:
    """The SmartStore image upload endpoint, faked at the transport: it records every request and
    answers one provider reference per call."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, sent: httpx.Request) -> httpx.Response:
        self.requests.append(sent)
        reference = f"https://shop-phinf.pstatic.net/20261011_1/c2-{len(self.requests)}.jpg"
        return httpx.Response(200, json={"images": [{"url": reference}]})


class _Session:
    access_token = "fixture-access-token-C2x"
    credential_generation = 1
    session_generation = 1


def _upload_run(
    container: Container, config: AppConfig, provider: _FakeSmartStore
) -> AssetUploadRun:
    store = LiveAuthorityStore(container.db, container.clock, container.audit)
    uploads = AssetUploadService(
        store=store,
        stack=SafetyStack(
            store=store, mode=PermittedMode(), proofs=ProvenProofs(), clock=container.clock
        ),
        sender=SmartStoreAssetSender(
            SmartStoreEndpointCaller(transport=httpx.MockTransport(provider)),
            bearer=lambda: _Session,
        ),
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
    )


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


def _detail_image(images: tuple[PublicationImage, ...]) -> PublicationImage:
    (found,) = [image for image in images if image.role is OutputRole.DETAIL]
    return found


def _derivations(config: AppConfig) -> list[tuple[Any, ...]]:
    with contextlib.closing(raw(config)) as connection:
        return connection.execute(
            "SELECT derivation_id, artifact_sha256, transformation_version, input_manifest_json,"
            " transformation_spec_json, operations_json, decided_by"
            " FROM derived_image_derivations ORDER BY recorded_at"
        ).fetchall()


def test_a_25_mb_description_image_is_uploaded_as_its_upload_fit_variant(
    container: Container, config: AppConfig
) -> None:
    source_bytes = big_jpeg()
    unit = _unit(container, config, source_bytes)
    assert hashlib.sha256(source_bytes).hexdigest() == unit.detail

    # ---- 1. Saving the SmartStore unit made exactly one variant, with recorded lineage (IC-03).
    (row,) = _derivations(config)
    derivation_id, variant_sha, version, inputs, spec, operations, decided_by = row
    assert version == UPLOAD_FIT_VERSION and decided_by == UPLOAD_FIT_VERSION
    assert json.loads(inputs) == [
        {"kind": "SOURCE_ASSET", "sha256": unit.detail, "parent_derivation_id": None}
    ]
    recipe = json.loads(spec)
    assert recipe["recipe"] == "smartstore-upload-fit"
    assert recipe["threshold_bytes"] == UPLOAD_FIT_THRESHOLD
    assert recipe["quality"] in recipe["qualities"]
    (operation,) = json.loads(operations)
    assert (operation["input_digest"], operation["output_digest"]) == (unit.detail, variant_sha)
    assert operation["capability"] == "IMAGE_UPLOAD_FIT"
    assert operation["execution_class"] == "LOCAL"
    derived = DerivedImageStore(config.derived_images_dir, container.db, HeaderImageDecoder())
    stored = derived.get(variant_sha)
    assert stored is not None
    assert stored.mime_type == "image/jpeg"
    assert stored.byte_size < UPLOAD_FIT_THRESHOLD
    assert stored.width == UPLOAD_FIT_MAX_WIDTH
    assert stored.height == round(5400 * UPLOAD_FIT_MAX_WIDTH / 4000)
    variant_bytes = derived.read(variant_sha)
    assert hashlib.sha256(variant_bytes).hexdigest() == variant_sha
    # The source asset is kept, byte for byte, and the selection still names it.
    assert container.source_assets.read(unit.detail) == source_bytes
    selection = container.images.current_selection(unit.item_id)
    assert selection is not None
    assert {o.sha256 for o in selection.outputs} == {unit.representative, unit.detail}
    # The variant's QA is its own: the automatic rule's, under the rule in force, read only by
    # the upload-fit path — a selected derived artifact never reads an automatic verdict.
    variant_read = container.images.derived_variant(
        transformation_version=UPLOAD_FIT_VERSION,
        asset_kind=ImageAssetKind.SOURCE_ASSET,
        sha256=unit.detail,
        derivation_id=None,
        validated_source_revision_id=selection.source_revision_id,
        role=OutputRole.DETAIL,
    )
    assert variant_read is not None and variant_read.sha256 == variant_sha
    qa = variant_read.qa
    assert qa is not None and qa.verdict is QaVerdict.PASS
    assert (
        container.images.qa_for_selected_asset(
            asset_kind=ImageAssetKind.DERIVED_ARTIFACT,
            sha256=variant_sha,
            derivation_id=derivation_id,
            validated_source_revision_id=selection.source_revision_id,
            role=OutputRole.DETAIL,
        )
        is None
    )
    assert qa.qa_rule_version == AUTO_QA_RULE_VERSION == "image-qa-auto/v2"

    # ---- 2. The candidate publishes the variant in place of the selected source image (IC-05).
    candidate = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert candidate.status is ReadinessStatus.READY, candidate.codes
    assert candidate.upload_permitted
    (item,) = candidate.resolved.items
    image = _detail_image(item.images)
    assert (image.asset_kind, image.sha256, image.derivation_id) == (
        ImageAssetKind.DERIVED_ARTIFACT,
        variant_sha,
        derivation_id,
    )
    assert image.qa_verdict is QaVerdict.PASS and image.qa_result_id == qa.qa_result_id
    assert image.upload_fit is not None
    assert image.upload_fit.canonical() == {
        "recipe": UPLOAD_FIT_VERSION,
        "source_asset_kind": "SOURCE_ASSET",
        "source_sha256": unit.detail,
        "source_derivation_id": None,
        "width": stored.width,
        "height": stored.height,
        "byte_size": stored.byte_size,
    }
    assert IMAGE_UPLOAD_FIT_FAILED not in candidate.codes
    # The screen of the prepared unit shows it too, before anything is frozen.
    (prepared_view,) = container.register.units(unit.draft_id)
    (prepared_detail,) = [
        a for a in prepared_view.items[0].publication_assets if a.role == "DETAIL"
    ]
    assert prepared_detail.upload_fit is not None
    assert prepared_detail.upload_fit.height == stored.height

    # ---- 3. The ASSET grant names exactly the variant, never the source (IC-05).
    representative = ArtifactRef(ImageAssetKind.SOURCE_ASSET, unit.representative, None)
    variant = ArtifactRef(ImageAssetKind.DERIVED_ARTIFACT, variant_sha, derivation_id)
    source = ArtifactRef(ImageAssetKind.SOURCE_ASSET, unit.detail, None)
    with pytest.raises(InputValidationError) as named_source:
        _grant(container, unit, [representative, source])
    assert named_source.value.code == "LIVE_GRANT_ARTIFACT_SET_MISMATCH"
    grant = _grant(container, unit, [representative, variant])
    assert set(grant.artifacts) == {representative, variant}
    with LiveAuthorityStore(container.db, container.clock, container.audit).transaction() as live:
        live.release(
            actor=OPERATOR,
            reason_code="CANARY_WINDOW",
            authorization_ref=APPROVAL,
            correlation_id=CID,
        )

    # ---- 4. The fake SmartStore receives the variant, every call under 10^7 bytes (IC-02).
    provider = _FakeSmartStore()
    result = _upload_run(container, config, provider).run(
        grant.grant_id, window_s=900, actor=OPERATOR, correlation_id=CID
    )
    assert result.complete, result.items
    assert len(provider.requests) == 2
    for sent in provider.requests:
        assert len(sent.content) < ONE_CALL_LIMIT
    assert any(variant_bytes in sent.content for sent in provider.requests)
    assert not any(source_bytes[:4096] in sent.content for sent in provider.requests)
    with contextlib.closing(raw(config)) as connection:
        attempts = connection.execute(
            "SELECT asset_kind, artifact_sha256, derivation_id, content_sha256, media_type"
            " FROM asset_upload_attempts WHERE state = 'APPLIED_PROVEN'"
        ).fetchall()
    assert (
        "DERIVED_ARTIFACT",
        variant_sha,
        derivation_id,
        variant_sha,
        "image/jpeg",
    ) in attempts
    assert not any(row[1] == unit.detail for row in attempts)

    # ---- 5. The Snapshot freezes the variant's SHA-256, with its lineage.
    frozen = container.registration_preparations.freeze_current(
        unit.preparation_id, actor=OPERATOR, correlation_id=CID
    )
    payload = container.registrations.snapshot_payload(frozen.snapshot.registration_snapshot_id)
    assert payload is not None
    (frozen_item,) = payload["items"]
    (frozen_detail,) = [a for a in frozen_item["publication_assets"] if a["role"] == "DETAIL"]
    assert frozen_detail["asset_kind"] == "DERIVED_ARTIFACT"
    assert (frozen_detail["sha256"], frozen_detail["derivation_id"]) == (variant_sha, derivation_id)
    assert frozen_detail["upload_fit"]["source_sha256"] == unit.detail
    assert frozen_detail["provider_asset_ref"] is not None
    assert [i["sha256"] for i in payload["detail"]["images"]] == [variant_sha]
    # The SmartStore projection places the uploaded variant in the detail content.
    content = product.project(payload).document.mapping()["originProduct"]["detailContent"]
    assert frozen_detail["provider_asset_ref"] in content
    # The first CREATE copy reproduces the frozen inputs from the owners, the variant included.
    copy_ = container.registration_preparations.execution_copy(
        frozen.snapshot.registration_snapshot_id, spot_check=False
    )
    assert variant_sha in {a.sha256 for a in copy_.final.prepared_assets}

    # The register screen shows, beside the image, that its upload-fit variant is used (§3).
    (view,) = container.register.units(unit.draft_id)
    (item_view,) = view.items
    (shown,) = [a for a in item_view.publication_assets if a.role == "DETAIL"]
    assert shown.sha256 == variant_sha and shown.upload_fit is not None
    assert (shown.upload_fit.source_sha256, shown.upload_fit.width) == (unit.detail, stored.width)
    assert shown.upload_fit.byte_size == stored.byte_size


def test_saving_again_reuses_the_variant(container: Container, config: AppConfig) -> None:
    unit = _unit(container, config, big_jpeg())
    (first,) = _derivations(config)
    before = container.registration_preparations.stage_candidate(unit.preparation_id)
    preparation = container.registrations.preparation(unit.preparation_id)
    assert preparation is not None
    container.registration_preparations.update(
        unit.preparation_id,
        item_ids=[unit.item_id],
        inputs=AuthoredInputs(
            before.request.category,
            before.request.listing,
            before.request.detail,
        ),
        actor=OPERATOR,
    )
    assert _derivations(config) == [first]
    after = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert _detail_image(after.resolved.items[0].images).sha256 == first[1]


def test_an_oversize_image_the_recipe_cannot_decode_is_never_uploaded(
    container: Container, config: AppConfig
) -> None:
    # Stored as an image (the fake decoder reads its signature), but no pixels follow it.
    unusable = b"\x89PNG" + bytes(UPLOAD_FIT_THRESHOLD)
    unit = _unit(container, config, unusable)
    assert _derivations(config) == []
    candidate = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert candidate.status is ReadinessStatus.REVIEW_REQUIRED
    failed = [r for r in candidate.reasons if r.code == IMAGE_UPLOAD_FIT_FAILED]
    assert [(r.status, r.subject) for r in failed] == [
        (ReadinessStatus.REVIEW_REQUIRED, f"item:{unit.item_id}:DETAIL:1")
    ]
    assert not candidate.upload_permitted
    # No grant can name it — neither the source nor anything else (IC-06).
    with pytest.raises(InputValidationError) as refused:
        _grant(
            container,
            unit,
            [
                ArtifactRef(ImageAssetKind.SOURCE_ASSET, unit.representative, None),
                ArtifactRef(ImageAssetKind.SOURCE_ASSET, unit.detail, None),
            ],
        )
    assert refused.value.code == "LIVE_GRANT_CANDIDATE_NOT_READY"


def _failures(config: AppConfig) -> list[tuple[Any, ...]]:
    with contextlib.closing(raw(config)) as connection:
        return connection.execute(
            "SELECT target_ref, reason_code, details_json FROM audit_events"
            " WHERE event_type = ? ORDER BY seq",
            (AuditEventType.REGISTER_UPLOAD_FIT_FAILED.value,),
        ).fetchall()


def test_an_undecodable_failure_is_logged_and_audited(
    container: Container, config: AppConfig
) -> None:
    unit = _unit(container, config, b"\x89PNG" + bytes(UPLOAD_FIT_THRESHOLD))
    ((target_ref, reason, details),) = _failures(config)
    assert (target_ref, reason) == (unit.detail, "UNDECODABLE")
    assert json.loads(details)["item_id"] == unit.item_id


def test_an_image_no_quality_step_fits_is_never_uploaded(
    container: Container, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing smaller is guessed: with no quality step left the recipe fits nothing (NOT_FITTED).
    monkeypatch.setattr(upload_fit, "UPLOAD_FIT_QUALITIES", ())
    unit = _unit(container, config, big_jpeg())
    assert _derivations(config) == []
    ((target_ref, reason, _details),) = _failures(config)
    assert (target_ref, reason) == (unit.detail, "NOT_FITTED")
    candidate = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert candidate.status is ReadinessStatus.REVIEW_REQUIRED
    assert IMAGE_UPLOAD_FIT_FAILED in candidate.codes
    image = _detail_image(candidate.resolved.items[0].images)
    assert (image.asset_kind, image.sha256, image.upload_fit) == (
        ImageAssetKind.SOURCE_ASSET,
        unit.detail,
        None,
    )
    assert image.upload_fit_failed


def test_an_unexpected_error_fails_one_image_and_never_the_save(
    container: Container, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(data: bytes) -> Any:
        raise RuntimeError("an encoder fault")

    monkeypatch.setattr(upload_variants, "fit", broken)
    unit = _unit(container, config, big_jpeg())
    assert container.registrations.preparation(unit.preparation_id) is not None
    ((target_ref, reason, _details),) = _failures(config)
    assert (target_ref, reason) == (unit.detail, "UNEXPECTED_ERROR")
    candidate = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert IMAGE_UPLOAD_FIT_FAILED in candidate.codes


def test_an_image_between_9_5_and_10_mb_is_fitted(container: Container, config: AppConfig) -> None:
    buffer = io.BytesIO()
    width, height = 2400, 3450
    noise = random.Random(41).randbytes(width * height * 3)
    Image.frombytes("RGB", (width, height), noise).save(buffer, "JPEG", quality=95)
    data = buffer.getvalue()
    assert UPLOAD_FIT_THRESHOLD <= len(data) < 10_000_000, len(data)
    unit = _unit(container, config, data)
    ((_derivation, variant_sha, *_rest),) = _derivations(config)
    candidate = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert candidate.status is ReadinessStatus.READY, candidate.codes
    image = _detail_image(candidate.resolved.items[0].images)
    assert (image.asset_kind, image.sha256) == (ImageAssetKind.DERIVED_ARTIFACT, variant_sha)


def test_a_reused_variant_without_qa_under_the_rule_in_force_is_qad_again(
    container: Container, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.stages.products import images

    # The variant made, and QA'd, under an earlier automatic rule version.
    with monkeypatch.context() as earlier:
        earlier.setattr(upload_variants, "AUTO_QA_RULE_VERSION", "image-qa-auto/v1")
        earlier.setattr(images, "AUTO_QA_RULE_VERSION", "image-qa-auto/v1")
        unit = _unit(container, config, big_jpeg())
    (first,) = _derivations(config)
    stale = container.registration_preparations.stage_candidate(unit.preparation_id)
    image = _detail_image(stale.resolved.items[0].images)
    assert image.sha256 == first[1] and image.qa_verdict is None
    assert "PUBLICATION_ASSET_QA_NOT_PASSED" in stale.codes
    # Saving again reuses the variant and records its own QA under the rule in force.
    container.registration_preparations.update(
        unit.preparation_id,
        item_ids=[unit.item_id],
        inputs=AuthoredInputs(stale.request.category, stale.request.listing, stale.request.detail),
        actor=OPERATOR,
    )
    assert _derivations(config) == [first]
    again = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert again.status is ReadinessStatus.READY, again.codes
    recovered = _detail_image(again.resolved.items[0].images)
    assert recovered.sha256 == first[1] and recovered.qa_verdict is QaVerdict.PASS


def test_another_marketplace_makes_no_variant_and_publishes_the_source(
    container: Container, config: AppConfig
) -> None:
    unit = _unit(container, config, big_jpeg(), marketplace=MARKET)
    assert _derivations(config) == [] and _failures(config) == []
    candidate = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert IMAGE_UPLOAD_FIT_FAILED not in candidate.codes
    image = _detail_image(candidate.resolved.items[0].images)
    assert (image.asset_kind, image.sha256, image.upload_fit, image.upload_fit_failed) == (
        ImageAssetKind.SOURCE_ASSET,
        unit.detail,
        None,
        False,
    )
