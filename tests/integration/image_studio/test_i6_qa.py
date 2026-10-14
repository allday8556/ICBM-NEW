"""ADR-0041 I6: the leftover-text QA of 「적용」 and the upload-fit chain of translated images.

The application runs with its real worker, as in the I5 suite: ``make_ready`` swaps in the fake OCR
engine, which reads a source image as scripted and any rendering by its pixels (a "glyph" bar of
the scene still at least half ink is source text still there), and answers the application's own
profiled provider through the AIS-1 fakes. Nothing is sent and nothing is LIVE.

1. A region whose recognised box was too tight keeps part of its source text after the rendering:
   「적용」 reads the rendering again, flags it ``LEFTOVER_SOURCE_TEXT`` and records nothing until
   the operator confirms that region; the audit names ids, codes and counts, never a text.
2. A clean rendering passes, and the QA read exactly the rendered artifact; with no OCR engine the
   QA cannot run, so nothing is applied.
3. A tall detail image whose translation is over SmartStore's upload threshold reaches the fake
   SmartStore as its ``smartstore-upload-fit/v1`` variant, derived from the translated artifact as
   its ``DERIVED_ARTIFACT`` parent, with its own automatic QA, through the exact-sha ASSET grant;
   the application stays DRY_RUN and the source stays byte-identical.
"""

import contextlib
import hashlib
import io
import json
import logging
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.capabilities.image_studio.contracts import NoOcrProvider
from app.capabilities.live_safety.store import ArtifactRef, LiveAuthorityStore
from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.platform.core.execution import ExecutionMode
from app.stages.collect.imagedecode import HeaderImageDecoder
from app.stages.products.image_model import (
    AUTO_QA_RULE_VERSION,
    DerivationInput,
    ImageAssetKind,
    OutputRole,
    QaVerdict,
    SourceDecisionKind,
)
from app.stages.products.image_store import DerivedImageStore
from app.stages.products.model import ReadinessStatus
from app.stages.register.authoring_revisions import AuthoringRevisionKind
from app.stages.register.model import ListingShape
from app.stages.register.policy import StaticRegistrationMetadata, StaticRegistrationPolicy
from app.stages.register.upload_fit import (
    UPLOAD_FIT_MAX_WIDTH,
    UPLOAD_FIT_THRESHOLD,
    UPLOAD_FIT_VERSION,
)
from tests.conftest import LOCAL
from tests.integration.register.test_c2_upload_fit import (
    APPROVAL,
    ONE_CALL_LIMIT,
    SMARTSTORE,
    Unit,
    _detail_image,
    _FakeSmartStore,
    _grant,
    _inputs,
    _upload_run,
)
from tests.support.image_studio_support import (
    OPERATOR,
    REGIONS,
    TEXTS,
    TIGHT,
    FakeOcr,
    StudioProduct,
    ink_reading,
    make_ready,
    scene_image,
    studio_product,
)
from tests.support.onboarding_support import wait_until
from tests.support.product_support import context, count, raw
from tests.support.register_support import (
    CID,
    TAXONOMY,
    FakeCapability,
    establish,
    metadata,
    target,
)

pytestmark = pytest.mark.integration

HEADERS = {"X-ICBM-Client": "pytest"}
BASE = "/api/v1/image-studio"


@pytest.fixture
def client(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as test_client:
        yield test_client


@pytest.fixture
def container(client: TestClient) -> Container:
    served: Container = client.app.state.container  # type: ignore[attr-defined]
    return served


def _image(item: StudioProduct, tail: str = "") -> str:
    return f"{BASE}/items/{item.item_id}/images/{item.detail}{tail}"


def _translate(client: TestClient, container: Container, item: StudioProduct) -> dict[str, Any]:
    """One run for the detail image, then the panel's read of it."""
    response = client.post(
        f"{BASE}/products/{item.group}/translations",
        json={"images": [item.detail], "source_language": "zh", "actor": OPERATOR},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    job_id = str(response.json()["job_id"])
    wait_until(lambda: container.jobs.get(job_id).state in ("SUCCEEDED", "DEAD"))
    assert container.jobs.get(job_id).state == "SUCCEEDED"
    state = client.get(_image(item))
    assert state.status_code == 200, state.json()
    read: dict[str, Any] = state.json()
    return read


def _save(client: TestClient, item: StudioProduct, state: dict[str, Any], *ids: str) -> str:
    """Save r-ids included with their suggestion, every other region kept; the revision id."""
    items = [
        {
            "region_id": r["region_id"],
            "include": True,
            "text": r["suggestion"],
            "erase": r["default_erase"],
        }
        if r["region_id"] in ids
        else {"region_id": r["region_id"], "include": False}
        for r in state["regions"]
    ]
    response = client.post(
        _image(item, "/revisions"),
        json={
            "observation_id": state["observation_id"],
            "source_language": "zh",
            "items": items,
            "expected_seq": state["expected_seq"],
            "actor": OPERATOR,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    return str(response.json()["revision_id"])


def _apply(client: TestClient, item: StudioProduct, revision_id: str, confirmed: list[str]) -> Any:
    return client.post(
        _image(item, "/apply"),
        json={
            "revision_id": revision_id,
            "source_language": "zh",
            "confirmed": confirmed,
            "actor": OPERATOR,
        },
        headers=HEADERS,
    )


def _applied_events(config: AppConfig) -> list[dict[str, Any]]:
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute(
            "SELECT details_json FROM audit_events WHERE action = 'IMAGE_TRANSLATION_APPLIED'"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _dump(config: AppConfig) -> str:
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute("SELECT * FROM audit_events").fetchall()
    return "\n".join(str(row) for row in rows)


def _source_untouched(config: AppConfig, sha: str) -> None:
    stored = config.source_assets_dir / sha[:2] / sha
    files = [stored] if stored.is_file() else list(config.source_assets_dir.rglob(sha + "*"))
    assert files, "the source asset is stored"
    for path in files:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha


# ---------------------------------------------------------------- 1. leftover source text


def test_leftover_source_text_is_flagged_and_blocks_apply_until_the_region_is_confirmed(
    client: TestClient,
    container: Container,
    config: AppConfig,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    item = studio_product(container, config)
    ocr, _ = make_ready(container, tmp_path, FakeOcr(regions=TIGHT))
    state = _translate(client, container, item)
    # Nothing in the review flags r1: its leftover only shows on the rendering.
    assert {r["region_id"]: r["flags"] for r in state["regions"]}["r1"] == []
    revision = _save(client, item, state, "r0", "r1")

    refused = _apply(client, item, revision, [])
    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "IMAGE_STUDIO_FLAGGED_UNCONFIRMED"
    assert error["details"] == {"regions": ["r1"], "flags": {"r1": ["LEFTOVER_SOURCE_TEXT"]}}
    assert not [t for t in TEXTS if t in refused.text]
    # The QA read a rendering, not the source, and nothing was recorded (IT-02).
    assert ocr.reads_of(item.detail) == 1 and len(ocr.reads) == 2
    assert count(config, "derived_image_derivations") == 0 and _applied_events(config) == []
    selection = container.images.current_selection(item.item_id)
    assert selection is not None
    assert all(d.decision is SourceDecisionKind.USE_SOURCE for d in selection.decisions)
    # Confirming another region does not pass it.
    assert _apply(client, item, revision, ["r0"]).status_code == 422

    applied = _apply(client, item, revision, ["r1"])
    assert applied.status_code == 200, applied.json()
    artifact = applied.json()
    # The recorded rendering really holds what is left of r1's line, and only that.
    rendered = container.image_studio_review.derived_image(
        item.item_id, item.detail, artifact["derivation_id"]
    )
    left = {r.text: r.box for r in ink_reading(rendered)}
    assert set(left) == {REGIONS[1].text, REGIONS[2].text, REGIONS[3].text}
    assert left[REGIONS[1].text] == (160, 90, 204, 110)
    (event,) = _applied_events(config)
    assert event["confirmed_flagged"] == [["r1", ["LEFTOVER_SOURCE_TEXT"]]]
    # Three source-script lines are read: r1's leftover and the two regions the operator kept.
    assert event["leftover_qa"] == {
        "engine_id": "fake-ocr",
        "regions": ["r1"],
        "lines": 3,
        "unattributed": 0,
    }

    # No OCR text and no Korean text in an audit row or a log record (§8); the source is intact.
    dump = _dump(config)
    assert not [t for t in TEXTS if t in dump]
    logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert not [t for t in TEXTS if t in logged]
    _source_untouched(config, item.detail)


# ---------------------------------------------------------------- 2. a clean rendering


def test_a_clean_rendering_passes_and_the_qa_read_exactly_the_artifact(
    client: TestClient, container: Container, config: AppConfig, tmp_path: Path
) -> None:
    item = studio_product(container, config)
    ocr, _ = make_ready(container, tmp_path)
    state = _translate(client, container, item)
    revision = _save(client, item, state, "r0", "r1")
    applied = _apply(client, item, revision, [])
    assert applied.status_code == 200, applied.json()
    artifact = applied.json()
    # The QA's one reading is of the exact artifact bytes recorded.
    assert ocr.reads_of(artifact["artifact_sha256"]) == 1
    (event,) = _applied_events(config)
    assert event["confirmed_flagged"] == []
    assert event["leftover_qa"] == {
        "engine_id": "fake-ocr",
        "regions": [],
        "lines": 2,
        "unattributed": 0,
    }
    # QA belongs to the exact artifact: nothing selects it before the operator's PASS (IT-09).
    assert artifact["qa_verdict"] is None
    _source_untouched(config, item.detail)


def test_without_an_ocr_engine_the_qa_cannot_run_and_nothing_is_applied(
    client: TestClient, container: Container, config: AppConfig, tmp_path: Path
) -> None:
    item = studio_product(container, config)
    make_ready(container, tmp_path)
    state = _translate(client, container, item)
    revision = _save(client, item, state, "r0")
    container.image_translation._ocr = NoOcrProvider()  # type: ignore[attr-defined]
    refused = _apply(client, item, revision, [])
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "IMAGE_OCR_NOT_CONFIGURED"
    assert count(config, "derived_image_derivations") == 0 and _applied_events(config) == []


# ---------------------------------------------------------------- 3. the upload-fit chain


def tall_detail() -> bytes:
    """A tall detail image: the scene's text panel on top of seeded noise, which PNG cannot
    compress, so the image and its translation are both over the upload-fit threshold."""
    width, height = 1200, 3000
    canvas = Image.frombytes(
        "RGB", (width, height), random.Random(56).randbytes(width * height * 3)
    )
    canvas.paste(scene_image(), (0, 0))
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    data = buffer.getvalue()
    assert UPLOAD_FIT_THRESHOLD < len(data) < 30_000_000, len(data)
    return data


def _prepare(container: Container, config: AppConfig, item: StudioProduct) -> Unit:
    """The Item's SmartStore unit, authored through the application's own preparation owner as
    the ADR-0037 C2 suite authors it: saving it fits every selected image that needs it."""
    account = establish(container, config, SMARTSTORE, "uid-smartstore-i6")
    with container.db.write() as session:
        stamped = container.authoring_revisions.stamp(
            session, SMARTSTORE, TAXONOMY, correlation_id=CID
        )
    composition = stamped["detail_composition_revision"]
    current = container.authoring_revisions.current(
        AuthoringRevisionKind.DETAIL_COMPOSITION, SMARTSTORE
    )
    assert current is not None and current.revision_id == composition
    pricing = context(marketplace_key=SMARTSTORE)
    served = container.registration_preflight
    served._policies = StaticRegistrationPolicy(  # type: ignore[attr-defined]
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
    served._metadata = StaticRegistrationMetadata(  # type: ignore[attr-defined]
        (metadata(),), marketplace_key=SMARTSTORE
    )
    served._capability = FakeCapability()  # type: ignore[attr-defined]
    priced = container.pricing.price(item.item_id, pricing).snapshot
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
            item.item_id,
            priced.pricing_snapshot_id,
            added_by=OPERATOR,
            correlation_id=CID,
        )
    record = container.registration_preparations.create(
        draft.draft_id,
        item_ids=[item.item_id],
        inputs=_inputs(stamped, composition),
        actor=OPERATOR,
    )
    return Unit(
        account,
        item.item_id,
        item.representative,
        item.detail,
        record.preparation_id,
        draft.draft_id,
    )


def test_a_large_translated_detail_image_reaches_the_fake_smartstore_as_its_upload_fit_variant(
    client: TestClient, container: Container, config: AppConfig, tmp_path: Path
) -> None:
    source_bytes = tall_detail()
    item = studio_product(container, config, image=source_bytes)
    assert hashlib.sha256(source_bytes).hexdigest() == item.detail
    make_ready(container, tmp_path, FakeOcr(sources=frozenset({item.detail})))

    # The translation, its operator PASS on that exact artifact, and its selection (I5).
    state = _translate(client, container, item)
    revision = _save(client, item, state, "r0", "r1")
    applied = _apply(client, item, revision, [])
    assert applied.status_code == 200, applied.json()
    translated = applied.json()
    translated_sha, translated_id = translated["artifact_sha256"], translated["derivation_id"]
    translated_bytes = container.image_studio_review.derived_image(
        item.item_id, item.detail, translated_id
    )
    assert len(translated_bytes) >= UPLOAD_FIT_THRESHOLD
    for step, body in (
        ("/qa", {"derivation_id": translated_id, "verdict": "PASS", "actor": OPERATOR}),
        ("/select", {"derivation_id": translated_id, "actor": OPERATOR}),
    ):
        response = client.post(_image(item, step), json=body, headers=HEADERS)
        assert response.status_code == 200, response.json()
    operator_qa = container.images.qa_for_selected_asset(
        asset_kind=ImageAssetKind.DERIVED_ARTIFACT,
        sha256=translated_sha,
        derivation_id=translated_id,
        validated_source_revision_id=item.revision_id,
        role=OutputRole.DETAIL,
    )
    assert operator_qa is not None and operator_qa.verdict is QaVerdict.PASS

    # Saving the SmartStore unit fits the translated artifact: smartstore-upload-fit/v1 with the
    # translated artifact as its DERIVED_ARTIFACT parent, under the same validated revision.
    unit = _prepare(container, config, item)
    variant = container.images.derived_variant(
        transformation_version=UPLOAD_FIT_VERSION,
        asset_kind=ImageAssetKind.DERIVED_ARTIFACT,
        sha256=translated_sha,
        derivation_id=translated_id,
        validated_source_revision_id=item.revision_id,
        role=OutputRole.DETAIL,
    )
    assert variant is not None
    record = container.images.read_derivation(variant.derivation_id)
    assert record.transformation_version == UPLOAD_FIT_VERSION
    assert record.inputs == (
        DerivationInput(ImageAssetKind.DERIVED_ARTIFACT, translated_sha, translated_id),
    )
    assert record.validated_source_revision_id == item.revision_id
    assert variant.byte_size < UPLOAD_FIT_THRESHOLD and variant.width == UPLOAD_FIT_MAX_WIDTH
    assert variant.height == round(3000 * UPLOAD_FIT_MAX_WIDTH / 1200)
    # Its QA is its own: the automatic rule's on its own bytes, never the translated artifact's.
    assert variant.qa is not None and variant.qa.verdict is QaVerdict.PASS
    assert variant.qa.qa_rule_version == AUTO_QA_RULE_VERSION
    assert variant.qa.qa_result_id != operator_qa.qa_result_id

    # The candidate publishes the variant in place of the translated image (IC-05).
    candidate = container.registration_preparations.stage_candidate(unit.preparation_id)
    assert candidate.status is ReadinessStatus.READY, candidate.codes
    (prepared,) = candidate.resolved.items
    image = _detail_image(prepared.images)
    assert (image.asset_kind, image.sha256, image.derivation_id) == (
        ImageAssetKind.DERIVED_ARTIFACT,
        variant.sha256,
        variant.derivation_id,
    )
    assert image.qa_result_id == variant.qa.qa_result_id
    assert image.upload_fit is not None
    assert (
        image.upload_fit.source_asset_kind,
        image.upload_fit.source_sha256,
        image.upload_fit.source_derivation_id,
    ) == (ImageAssetKind.DERIVED_ARTIFACT, translated_sha, translated_id)

    # The ASSET grant names exactly the variant; the fake SmartStore receives it, under 10^7
    # bytes per call; neither the translated PNG nor the source is ever sent (IC-02, ADR-0018).
    representative = ArtifactRef(ImageAssetKind.SOURCE_ASSET, item.representative, None)
    fitted = ArtifactRef(ImageAssetKind.DERIVED_ARTIFACT, variant.sha256, variant.derivation_id)
    grant = _grant(container, unit, [representative, fitted])
    assert set(grant.artifacts) == {representative, fitted}
    with LiveAuthorityStore(container.db, container.clock, container.audit).transaction() as live:
        live.release(
            actor=OPERATOR,
            reason_code="CANARY_WINDOW",
            authorization_ref=APPROVAL,
            correlation_id=CID,
        )
    provider = _FakeSmartStore()
    result = _upload_run(container, config, provider).run(
        grant.grant_id, window_s=900, actor=OPERATOR, correlation_id=CID
    )
    assert result.complete, result.items
    assert len(provider.requests) == 2
    assert all(len(sent.content) < ONE_CALL_LIMIT for sent in provider.requests)
    derived = DerivedImageStore(config.derived_images_dir, container.db, HeaderImageDecoder())
    variant_bytes = derived.read(variant.sha256)
    assert any(variant_bytes in sent.content for sent in provider.requests)
    assert not any(translated_bytes[:4096] in sent.content for sent in provider.requests)
    assert not any(source_bytes[:4096] in sent.content for sent in provider.requests)
    with contextlib.closing(raw(config)) as connection:
        attempts = connection.execute(
            "SELECT asset_kind, artifact_sha256, derivation_id FROM asset_upload_attempts"
            " WHERE state = 'APPLIED_PROVEN'"
        ).fetchall()
    assert ("DERIVED_ARTIFACT", variant.sha256, variant.derivation_id) in attempts
    assert not any(row[1] in (translated_sha, item.detail) for row in attempts)

    # The application stays DRY_RUN; the source is byte-identical (IT-01).
    assert container.execution_mode.state().mode is ExecutionMode.DRY_RUN
    assert container.source_assets.read(item.detail) == source_bytes
    _source_untouched(config, item.detail)
