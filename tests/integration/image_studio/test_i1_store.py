"""ADR-0041 I1: the Image Studio translation store, migration 0065 and the ``image.translate`` job.

Production binds no OCR engine (and, since I4, the registered translation task): the capability
reads NOT_CONFIGURED and every request is refused before anything is written. The owner's
behaviour is proven over a fake OCR engine, a fake composition and a fake AI provider that exist
only in this test:
- an observation is content-addressed and reused by its key;
- the translation fingerprint is ADR-0012 §6 / ADR-0026's: any change to the input, prompt,
  policy, provider, model, routing, proxy or schema changes it; an equal one is reused; a candidate
  of other inputs is STALE on read, with its reason;
- edit revisions are numbered per lineage and their pointer only moves forward;
- a failed item records nothing durable, and no audit row carries OCR text or a translation;
- the database itself refuses an update or a delete, and 0065's downgrade fails closed.
"""

import contextlib
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from alembic import command

from app.capabilities.ai.composer import ComposedRequest
from app.capabilities.ai.execution import AIExecution
from app.capabilities.ai.provider import ProviderOutcome, RequestedIdentity, TaskRequest
from app.capabilities.image_studio.contracts import (
    ObservationKey,
    OcrIdentity,
    OcrRegion,
    SentRegion,
    TranslationInputs,
    relevant_input,
    sent_regions,
    stale_reasons,
)
from app.capabilities.image_studio.store import (
    IMAGE_EDIT_MOVED,
    IMAGE_EDIT_SUGGESTION_ALTERED,
    IMAGE_EDIT_UNCHANGED,
    EditItem,
    EraseDecision,
    Face,
    ImageStudioConflictError,
    ItemOrigin,
    RegionStyle,
    Weight,
)
from app.capabilities.image_studio.translation import (
    IMAGE_TRANSLATE_IMAGE_UNKNOWN,
    IMAGE_TRANSLATE_NOT_CONFIGURED,
    TRANSLATE_JOB,
    ImageTranslateItemsFailedError,
    ImageTranslationService,
    TranslateRequest,
)
from app.capabilities.jobs.models import Job
from app.capabilities.jobs.registry import JobContext
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import (
    AppError,
    ErrorClass,
    InputValidationError,
    NotFoundError,
    PolicyBlockedError,
)
from app.platform.db.database import create_sqlite_engine
from app.platform.db.migrate import alembic_config, current_revision, upgrade_to_head
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from app.stages.connect.state import CapabilityStatus
from app.stages.products.materialization import MaterializationStatus
from tests.integration.ai.test_aif2_provider_port import IDENTITY, _provenance
from tests.support.collect_support import PNG
from tests.support.jobs_support import FakeClock
from tests.support.product_support import Collections, FakeDecoder, count, raw

pytestmark = pytest.mark.integration

OCR = OcrIdentity("fake-ocr", "1.0.0", "pre-1")
TASK = "TASK_IMAGE_TEXT_TRANSLATE_V1"
POLICY = "POLICY_COMMON_MARKET_V1"
# OCR text and Korean text that must never reach an audit row.
REGIONS = (
    OcrRegion("r0", (10, 10, 120, 40), "纯天然芝麻油", "zh", 0.97, 0.92),
    OcrRegion("r1", (10, 50, 120, 80), "净含量350ml", "zh", 0.88, 0.40, ((10, 50), (120, 80))),
    OcrRegion("r2", (10, 90, 120, 120), "厂家直销", "zh", 0.93, 0.95),
)
SECRET_TEXTS = ("纯天然芝麻油", "净含量350ml", "厂家直销", "한국어")
STYLE = RegionStyle(Weight.BOLD, Face.SANS, 28, "#222222")
FONTS = {"NotoSansKR-VF.ttf": "a" * 64}
HEAD = "0065_image_studio_translation"
BEFORE = "0064_fx_snapshots"
TABLES = (
    "image_ocr_observations",
    "image_translation_candidates",
    "image_edit_revisions",
    "image_edit_current",
)


@dataclass
class FakeOcr:
    regions: tuple[OcrRegion, ...] = REGIONS
    engine: OcrIdentity | None = OCR
    raises: Exception | None = None
    reads: list[bytes] = field(default_factory=list)

    def identity(self) -> OcrIdentity | None:
        return self.engine

    def read(self, image: bytes) -> tuple[OcrRegion, ...]:
        self.reads.append(image)
        if self.raises is not None:
            raise self.raises
        return self.regions


@dataclass
class FakeComposer:
    prompt: str = "task-rev-1"
    policy: str = "policy-rev-1"

    def compose(self, source_language: str, regions: Any) -> ComposedRequest:
        return ComposedRequest(
            task_key=TASK,
            role_key="ROLE_PRODUCT_MD_V1",
            policy_key=POLICY,
            mode="IMAGE_TEXT_TRANSLATE",
            text=json.dumps(
                {"language": source_language, "regions": [[r.region_id, r.text] for r in regions]}
            ),
            prompt_version={TASK: self.prompt},
            policy_version={POLICY: self.policy},
        )


@dataclass
class FakeTranslator:
    """A deterministic AI provider: one Korean text per sent region, or the scripted failure."""

    identity: RequestedIdentity | None = IDENTITY
    failure: ProviderOutcome | None = None
    answer: dict[str, Any] | None = None
    requests: list[TaskRequest] = field(default_factory=list)

    def requested_identity(self) -> RequestedIdentity | None:
        return self.identity

    def execute(self, request: TaskRequest) -> ProviderOutcome:
        self.requests.append(request)
        assert self.identity is not None
        if self.failure is not None:
            return self.failure
        sent = json.loads(request.text)["regions"]
        value = self.answer or {
            "translations": [{"region_id": rid, "text": f"한국어 {rid}"} for rid, _ in sent]
        }
        return ProviderOutcome(ok=True, provenance=_provenance(self.identity), value=value)


@dataclass
class Rig:
    service: ImageTranslationService
    ocr: FakeOcr
    composer: FakeComposer
    translator: FakeTranslator


@dataclass(frozen=True)
class Product:
    group: str
    images: tuple[str, ...]


@pytest.fixture
def product(container: Container, config: AppConfig) -> Product:
    assets = SourceAssetStore(config.source_assets_dir, container.db, FakeDecoder(), FakeClock())
    detail = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=1,
        host="img.shop.example",
        provenance=".detail img:nth-of-type(1)",
        status=FieldStatus.CONFIRMED,
        sha256=assets.put(PNG + b"i1-detail").sha256,
    )
    run_id, _ = Collections.of(container, config).collect(
        source_product_id="4101", extra_images=(detail,)
    )
    result = container.materializer.materialize_run(run_id)
    assert result.status is MaterializationStatus.MATERIALIZED, result
    return Product(str(result.product_group_id), (hashlib.sha256(PNG).hexdigest(), detail.sha256))


def _rig(
    container: Container,
    *,
    ocr: FakeOcr | None = None,
    composer: FakeComposer | None = None,
    translator: FakeTranslator | None = None,
    schema: str = "image-text-translate/v1",
) -> Rig:
    ocr = ocr or FakeOcr()
    composer = composer or FakeComposer()
    translator = translator or FakeTranslator()
    service = ImageTranslationService(
        clock=container.clock,
        store=container.image_studio,
        audit=container.audit,
        jobs=container.jobs,
        products=container.product_store,
        sources=container.source_assets,
        ocr=ocr,
        composer=composer,
        execution=AIExecution(translator),
        output_schema_version=schema,
    )
    return Rig(service, ocr, composer, translator)


def _run(container: Container, rig: Rig, job_id: str) -> None:
    with container.db.read() as session:
        job = session.get(Job, job_id)
        assert job is not None and job.job_type == TRANSLATE_JOB
        payload, cid, attempts = json.loads(job.payload_json), job.correlation_id, job.max_attempts
    rig.service.job_definition().handler(
        JobContext(
            job_id=job_id,
            job_type=TRANSLATE_JOB,
            attempt_no=1,
            max_attempts=attempts,
            correlation_id=cid,
            target_ref=None,
            payload=payload,
        )
    )


def _translate(container: Container, rig: Rig, product: Product, **kwargs: Any) -> str:
    request = TranslateRequest(
        product_group_id=product.group,
        images=kwargs.pop("images", product.images),
        source_language=kwargs.pop("source_language", "zh"),
        actor="operator",
        **kwargs,
    )
    job_id = rig.service.request(request)
    _run(container, rig, job_id)
    return job_id


def _audit_dump(config: AppConfig) -> str:
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute(
            "SELECT action, target_ref, reason_code, before_json, after_json, details_json"
            " FROM audit_events WHERE event_type = 'IMAGE_STUDIO_RECORDED'"
        ).fetchall()
    return json.dumps(rows, ensure_ascii=False)


def _refused(code: str, call: Any) -> AppError:
    with pytest.raises(AppError) as caught:
        call()
    assert caught.value.code == code, caught.value
    return caught.value


# ---------------------------------------------------------------- provider-zero production


def test_production_binds_nothing_and_refuses_before_writing(
    container: Container, config: AppConfig, product: Product
) -> None:
    report = container.image_translation.capability()
    assert report.status is CapabilityStatus.NOT_CONFIGURED
    # I4 binds the registered task; no OCR engine and no approved AI profile remain.
    assert "ocr" in report.detail and "ai provider" in report.detail
    assert "translation task" not in report.detail
    definition = container.job_registry.get(TRANSLATE_JOB)
    assert definition.idempotent is False
    assert definition.retry_policy is not None and definition.retry_policy.max_attempts == 1
    error = _refused(
        IMAGE_TRANSLATE_NOT_CONFIGURED,
        lambda: container.image_translation.request(
            TranslateRequest(product.group, product.images, "zh", "operator")
        ),
    )
    assert isinstance(error, PolicyBlockedError)
    assert count(config, "jobs", "job_type = ?", TRANSLATE_JOB) == 0
    assert all(count(config, table) == 0 for table in TABLES)


def test_a_request_names_only_the_products_own_images(
    container: Container, config: AppConfig, product: Product
) -> None:
    rig = _rig(container)
    _refused(
        IMAGE_TRANSLATE_IMAGE_UNKNOWN,
        lambda: rig.service.request(TranslateRequest(product.group, ("f" * 64,), "zh", "operator")),
    )
    with pytest.raises(InputValidationError):
        rig.service.request(TranslateRequest(product.group, product.images, "ko", "operator"))
    with pytest.raises(InputValidationError):
        rig.service.request(TranslateRequest(product.group, (), "zh", "operator"))
    with pytest.raises(NotFoundError):
        rig.service.request(TranslateRequest("no-such-product", product.images, "zh", "operator"))
    assert count(config, "jobs", "job_type = ?", TRANSLATE_JOB) == 0


# ---------------------------------------------------------------- observations


def test_an_observation_is_read_once_and_reused_by_its_key(
    container: Container, config: AppConfig, product: Product
) -> None:
    store = container.image_studio
    key = OCR.key(product.images[0])
    first = store.record_observation(key, REGIONS, actor="system:test", correlation_id="c-1")
    again = store.record_observation(key, REGIONS[:1], actor="system:test", correlation_id="c-2")
    assert again == first and store.observation(key) == first
    assert first.regions == REGIONS
    assert count(config, "image_ocr_observations") == 1
    # Another engine version, or other preprocessing, is another reading.
    other = replace(key, engine_version="1.0.1")
    assert (
        store.record_observation(
            other, REGIONS, actor="system:test", correlation_id="c-3"
        ).observation_id
        != first.observation_id
    )
    assert store.observation(replace(key, preprocessing_version="pre-2")) is None
    assert count(config, "image_ocr_observations") == 2
    assert count(config, "audit_events", "action = 'IMAGE_OCR_OBSERVED'") == 2
    with pytest.raises(InputValidationError):
        store.record_observation(
            replace(key, engine_id="bad"),
            (replace(REGIONS[0], confidence=1.5),),
            actor="system:test",
            correlation_id="c-4",
        )
    with pytest.raises(InputValidationError):
        store.record_observation(
            replace(key, engine_id="bad"),
            (REGIONS[0], REGIONS[0]),
            actor="system:test",
            correlation_id="c-5",
        )


# ---------------------------------------------------------------- the translation fingerprint


def _inputs(**overrides: Any) -> TranslationInputs:
    values: dict[str, Any] = {
        "relevant_input": relevant_input("o" * 64, "zh", (SentRegion("r0", "纯天然芝麻油"),)),
        "prompt_version": {TASK: "task-rev-1"},
        "policy_version": {POLICY: "policy-rev-1"},
        "identity": IDENTITY,
        "output_schema_version": "image-text-translate/v1",
    }
    values.update(overrides)
    return TranslationInputs(**values)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            {"relevant_input": relevant_input("p" * 64, "zh", (SentRegion("r0", "纯天然芝麻油"),))},
            "input",
        ),
        (
            {"relevant_input": relevant_input("o" * 64, "ja", (SentRegion("r0", "纯天然芝麻油"),))},
            "input",
        ),
        (
            {"relevant_input": relevant_input("o" * 64, "zh", (SentRegion("r0", "芝麻油"),))},
            "input",
        ),
        ({"relevant_input": relevant_input("o" * 64, "zh", ())}, "input"),
        ({"prompt_version": {TASK: "task-rev-2"}}, "prompt"),
        ({"policy_version": {POLICY: "policy-rev-2"}}, "policy"),
        ({"identity": replace(IDENTITY, requested_provider="other")}, "provider"),
        ({"identity": replace(IDENTITY, requested_model="other-model")}, "provider"),
        ({"identity": replace(IDENTITY, routing_config_version="routing-2")}, "provider"),
        ({"identity": replace(IDENTITY, proxy_version="proxy-2")}, "provider"),
        ({"output_schema_version": "image-text-translate/v2"}, "schema"),
    ],
)
def test_every_part_changes_the_fingerprint_and_names_its_reason(
    change: dict[str, Any], reason: str
) -> None:
    base, changed = _inputs(), _inputs(**change)
    assert changed.fingerprint != base.fingerprint
    assert stale_reasons(base, changed) == (reason,)
    assert _inputs().fingerprint == base.fingerprint
    assert stale_reasons(base, _inputs()) == ()


def test_a_kept_region_is_never_sent() -> None:
    assert sent_regions(REGIONS, frozenset({"r2"})) == (
        SentRegion("r0", "纯天然芝麻油"),
        SentRegion("r1", "净含量350ml"),
    )
    blank = replace(REGIONS[0], region_id="r9", text="  ")
    assert [r.region_id for r in sent_regions((*REGIONS, blank))] == ["r0", "r1", "r2"]


# ---------------------------------------------------------------- the job over fakes


def test_the_job_records_one_observation_and_candidate_per_image_then_reuses_them(
    container: Container, config: AppConfig, product: Product
) -> None:
    rig = _rig(container)
    _translate(container, rig, product)
    assert len(rig.ocr.reads) == 2 and len(rig.translator.requests) == 2
    assert count(config, "image_ocr_observations") == 2
    assert count(config, "image_translation_candidates") == 2
    for sha in product.images:
        observation = container.image_studio.observation(OCR.key(sha))
        assert observation is not None
        (candidate,) = container.image_studio.candidates(observation.observation_id)
        assert candidate.translations == tuple(
            (r.region_id, f"한국어 {r.region_id}") for r in REGIONS
        )
        # Provenance is copied from the outcome, never invented.
        assert candidate.provenance["actual_model"] == "fake-model-1-0101"
        assert candidate.provenance["vendor_cost"] is None
        view = rig.service.candidate_view(observation.observation_id, "zh")
        assert view is not None and (view.status, view.stale_reasons) == ("FRESH", ())
    # A re-run reads nothing and calls nothing: the reading and the fingerprint are equal.
    _translate(container, rig, product)
    assert len(rig.ocr.reads) == 2 and len(rig.translator.requests) == 2
    assert count(config, "image_translation_candidates") == 2
    # No secret, no OCR text and no translation is ever in an audit row.
    dump = _audit_dump(config)
    assert not any(text in dump for text in SECRET_TEXTS)
    assert "IMAGE_TRANSLATION_CANDIDATE_RECORDED" in dump


def test_a_changed_prompt_or_exclusion_is_never_served_from_an_old_candidate(
    container: Container, config: AppConfig, product: Product
) -> None:
    rig = _rig(container)
    one = product.images[:1]
    _translate(container, rig, product, images=one)
    observation = container.image_studio.observation(OCR.key(one[0]))
    assert observation is not None
    container.clock.advance(60)  # type: ignore[attr-defined]
    rig.composer.prompt = "task-rev-2"
    view = rig.service.candidate_view(observation.observation_id, "zh")
    assert view is not None and (view.status, view.stale_reasons) == ("STALE", ("prompt",))
    _translate(container, rig, product, images=one)
    assert len(rig.translator.requests) == 2 and len(rig.ocr.reads) == 1
    view = rig.service.candidate_view(observation.observation_id, "zh")
    assert view is not None and view.status == "FRESH"
    # The operator keeps a region: what is sent changes, so the input does.
    stale = rig.service.candidate_view(observation.observation_id, "zh", frozenset({"r2"}))
    assert stale is not None and (stale.status, stale.stale_reasons) == ("STALE", ("input",))
    _translate(container, rig, product, images=one, excluded={one[0]: frozenset({"r2"})})
    sent = json.loads(rig.translator.requests[-1].text)["regions"]
    assert [rid for rid, _ in sent] == ["r0", "r1"]
    assert count(config, "image_translation_candidates") == 3
    # Another model is a provider change; another schema a schema change.
    other = _rig(
        container,
        ocr=rig.ocr,
        composer=rig.composer,
        translator=FakeTranslator(identity=replace(IDENTITY, requested_model="other-model")),
        schema="image-text-translate/v2",
    )
    view = other.service.candidate_view(observation.observation_id, "zh", frozenset({"r2"}))
    assert view is not None and (view.status, view.stale_reasons) == (
        "STALE",
        ("provider", "schema"),
    )
    # With no provider today nothing is fresh.
    gone = _rig(container, translator=FakeTranslator(identity=None))
    view = gone.service.candidate_view(observation.observation_id, "zh")
    assert view is not None and view.status == "STALE" and "provider" in view.stale_reasons


@pytest.mark.parametrize(
    "rig_change",
    [
        {
            "translator": FakeTranslator(
                failure=ProviderOutcome(
                    ok=False,
                    provenance=_provenance(),
                    error_class=ErrorClass.RATE_LIMITED,
                    error_code="AI_RATE_LIMITED",
                )
            )
        },
        {"translator": FakeTranslator(answer={"translations": [{"region_id": "r0", "text": "x"}]})},
        {"ocr": FakeOcr(raises=RuntimeError("engine crashed"))},
        {"ocr": FakeOcr(regions=(replace(REGIONS[0], script="ko"),))},
    ],
    ids=["provider-failed", "output-short", "ocr-crashed", "ocr-malformed"],
)
def test_a_failed_item_records_nothing_durable(
    container: Container, config: AppConfig, product: Product, rig_change: dict[str, Any]
) -> None:
    rig = _rig(container, **rig_change)
    with pytest.raises(ImageTranslateItemsFailedError) as failed:
        _translate(container, rig, product)
    assert failed.value.details == {"failed": 2, "total": 2}
    assert all(count(config, table) == 0 for table in TABLES)
    assert count(config, "audit_events", "action = 'IMAGE_TRANSLATE_ITEM_FAILED'") == 2
    dump = _audit_dump(config)
    assert not any(text in dump for text in SECRET_TEXTS)
    assert "engine crashed" not in dump


def test_only_the_failed_item_is_left_to_redo(
    container: Container, config: AppConfig, product: Product
) -> None:
    rig = _rig(container)
    _translate(container, rig, product, images=product.images[:1])
    rig.translator.failure = ProviderOutcome(
        ok=False,
        provenance=_provenance(),
        error_class=ErrorClass.TRANSIENT,
        error_code="AI_PROVIDER_TIMEOUT",
    )
    with pytest.raises(ImageTranslateItemsFailedError) as failed:
        _translate(container, rig, product)
    assert failed.value.error_class is ErrorClass.TRANSIENT
    assert failed.value.details == {"failed": 1, "total": 2}
    assert count(config, "image_translation_candidates") == 1
    assert count(config, "image_ocr_observations") == 1
    rig.translator.failure = None
    _translate(container, rig, product)
    assert count(config, "image_translation_candidates") == 2


# ---------------------------------------------------------------- edit revisions


def _translated(container: Container, product: Product) -> tuple[str, str]:
    rig = _rig(container)
    _translate(container, rig, product, images=product.images[:1])
    observation = container.image_studio.observation(OCR.key(product.images[0]))
    assert observation is not None
    (candidate,) = container.image_studio.candidates(observation.observation_id)
    return observation.observation_id, candidate.candidate_id


def _items(**changes: EditItem) -> tuple[EditItem, ...]:
    items = {
        "r0": EditItem(
            "r0", True, "한국어 r0", ItemOrigin.AI_SUGGESTION, EraseDecision.ERASE, STYLE
        ),
        "r1": EditItem(
            "r1", True, "내용량 350ml", ItemOrigin.OPERATOR_CONFIRMED, EraseDecision.OVERLAY, STYLE
        ),
        "r2": EditItem("r2", False),
    }
    items.update(changes)
    return tuple(items.values())


def _save(
    container: Container,
    product: Product,
    observation: str,
    candidate: str | None,
    items: tuple[EditItem, ...],
    expected_seq: int,
) -> Any:
    return container.image_studio.save_edit(
        product_group_id=product.group,
        source_sha256=product.images[0],
        observation_id=observation,
        candidate_id=candidate,
        items=items,
        renderer_version="image-translate-renderer/v1",
        font_hashes=FONTS,
        expected_seq=expected_seq,
        actor="operator",
        correlation_id="c-edit",
    )


def test_edit_revisions_number_per_lineage_and_the_pointer_moves_forward(
    container: Container, config: AppConfig, product: Product
) -> None:
    observation, candidate = _translated(container, product)
    store = container.image_studio
    assert store.current_edit(product.group, product.images[0]) is None
    first = _save(container, product, observation, candidate, _items(), 0)
    assert (first.seq, first.current) == (1, True)
    _refused(
        IMAGE_EDIT_UNCHANGED, lambda: _save(container, product, observation, candidate, _items(), 1)
    )
    moved = _refused(
        IMAGE_EDIT_MOVED,
        lambda: _save(
            container, product, observation, candidate, _items(r2=EditItem("r2", False)), 0
        ),
    )
    assert isinstance(moved, ImageStudioConflictError)
    edited = EditItem(
        "r0", True, "참기름 100%", ItemOrigin.OPERATOR_CONFIRMED, EraseDecision.ERASE, STYLE
    )
    second = _save(container, product, observation, candidate, _items(r0=edited), 1)
    assert second.seq == 2
    current = store.current_edit(product.group, product.images[0])
    assert current is not None and current.revision_id == second.revision_id
    assert current.items == _items(r0=edited)
    assert [(r.seq, r.current) for r in store.edit_history(product.group, product.images[0])] == [
        (2, True),
        (1, False),
    ]
    assert count(config, "image_edit_current") == 1
    assert count(config, "audit_events", "action = 'IMAGE_EDIT_SAVED'") == 2
    dump = _audit_dump(config)
    assert "참기름" not in dump and "내용량" not in dump


def test_an_edit_is_honest_about_its_origin_and_covers_every_region(
    container: Container, config: AppConfig, product: Product
) -> None:
    observation, candidate = _translated(container, product)
    altered = EditItem(
        "r0", True, "참기름 100%", ItemOrigin.AI_SUGGESTION, EraseDecision.ERASE, STYLE
    )
    _refused(
        IMAGE_EDIT_SUGGESTION_ALTERED,
        lambda: _save(container, product, observation, candidate, _items(r0=altered), 0),
    )
    # A suggestion needs its candidate.
    _refused(
        IMAGE_EDIT_SUGGESTION_ALTERED,
        lambda: _save(container, product, observation, None, _items(), 0),
    )
    with pytest.raises(InputValidationError):
        _save(container, product, observation, candidate, _items()[:2], 0)
    with pytest.raises(InputValidationError):
        _save(
            container,
            product,
            observation,
            candidate,
            _items(r2=EditItem("r2", False, text="남김")),
            0,
        )
    with pytest.raises(InputValidationError):
        _save(
            container,
            product,
            observation,
            candidate,
            _items(r2=EditItem("r2", True, "연락처", ItemOrigin.OPERATOR_CONFIRMED)),
            0,
        )
    assert count(config, "image_edit_revisions") == 0


# ---------------------------------------------------------------- the database itself


def test_the_database_refuses_updates_deletes_and_backward_moves(
    container: Container, config: AppConfig, product: Product
) -> None:
    observation, candidate = _translated(container, product)
    _save(container, product, observation, candidate, _items(), 0)
    shorter = EditItem(
        "r1", True, "350ml", ItemOrigin.OPERATOR_CONFIRMED, EraseDecision.OVERLAY, STYLE
    )
    _save(container, product, observation, candidate, _items(r1=shorter), 1)
    with contextlib.closing(raw(config)) as connection:
        for statement in (
            "UPDATE image_ocr_observations SET regions_json = '[]'",
            "DELETE FROM image_ocr_observations",
            "UPDATE image_translation_candidates SET translations_json = '[]'",
            "DELETE FROM image_translation_candidates",
            "UPDATE image_edit_revisions SET authored_by = 'someone'",
            "DELETE FROM image_edit_revisions",
            "DELETE FROM image_edit_current",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="never"):
                connection.execute(statement)
        first, second = (
            row[0]
            for row in connection.execute(
                "SELECT revision_id FROM image_edit_revisions ORDER BY seq"
            ).fetchall()
        )
        with pytest.raises(sqlite3.IntegrityError, match="newest"):
            connection.execute("UPDATE image_edit_current SET revision_id = ?", (first,))
        with pytest.raises(sqlite3.IntegrityError, match="only moves forward"):
            connection.execute("UPDATE image_edit_current SET revision_id = ?", (second,))
        with pytest.raises(sqlite3.IntegrityError, match="follows"):
            connection.execute(
                "INSERT INTO image_edit_revisions SELECT 'gap', product_group_id, source_sha256,"
                " 5, observation_id, candidate_id, items_json, renderer_version, font_hashes_json,"
                " content_fingerprint, authored_by, correlation_id, authored_at"
                " FROM image_edit_revisions WHERE seq = 2"
            )
        with pytest.raises(sqlite3.IntegrityError, match="candidate text unchanged"):
            connection.execute(
                "INSERT INTO image_edit_revisions SELECT 'forged', product_group_id,"
                " source_sha256, 3, observation_id, candidate_id,"
                " replace(items_json, '한국어 r0', '위조'), renderer_version,"
                " font_hashes_json, content_fingerprint, authored_by, correlation_id, authored_at"
                " FROM image_edit_revisions WHERE seq = 2"
            )
        with pytest.raises(sqlite3.IntegrityError, match="order sent"):
            connection.execute(
                "INSERT INTO image_translation_candidates SELECT 'swapped', observation_id,"
                " source_language, sent_region_ids_json, relevant_input, prompt_version_json,"
                " policy_version_json, requested_identity_json, output_schema_version,"
                f" '{'b' * 64}',"
                " json_array(json_extract(translations_json, '$[1]'),"
                " json_extract(translations_json, '$[0]'),"
                " json_extract(translations_json, '$[2]')),"
                " provenance_json, job_id, correlation_id, created_at"
                " FROM image_translation_candidates"
            )
        with pytest.raises(sqlite3.IntegrityError, match="script and scores"):
            connection.execute(
                "INSERT INTO image_ocr_observations VALUES ('o-bad', ?, 'e', 'v', 'p',"
                ' \'[{"region_id": "r", "text": "t", "script": "ko"}]\', ?, \'c\','
                " '2026-10-14 00:00:00')",
                (product.images[1], "c" * 64),
            )
    assert count(config, "image_edit_revisions") == 2


def test_0065_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)

    def tables() -> set[str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            return {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }

    before = tables()
    assert set(TABLES) <= before
    command.downgrade(alembic_config(url), BEFORE)
    assert before - tables() == set(TABLES)
    with contextlib.closing(sqlite3.connect(database)) as connection:
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert not any(name.startswith(tuple(f"trg_{t}_" for t in TABLES)) for name in names)
    assert "ix_image_translation_candidates_observation_id" not in names
    command.upgrade(alembic_config(url), "head")
    assert tables() == before
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO image_ocr_observations VALUES ('o-held', ?, 'e', 'v', 'p', '[]', ?,"
            " 'c', '2026-10-14 00:00:00')",
            ("d" * 64, "e" * 64),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), BEFORE)
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == HEAD
    finally:
        engine.dispose()
    assert tables() == before


def test_observation_key_is_the_reading_identity() -> None:
    key = OCR.key("a" * 64)
    assert key == ObservationKey("a" * 64, "fake-ocr", "1.0.0", "pre-1")
