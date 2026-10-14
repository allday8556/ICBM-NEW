"""ADR-0041 I4: the registered image-text translation task, its text-only composer and the checks.

Offline: the provider is the production ``ProfiledProvider`` over the AIS-1 fake serving process
and a fake sidecar completion that records the text it receives, so the whole production path runs
— the prompt registry, the composition, the data-transfer approval, the daily cap, the call ledger
and the candidate store — with nothing sent. The OCR engine is a fake: production still binds none.
"""

import contextlib
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.capabilities.ai.execution import AIExecution
from app.capabilities.ai.profiles import (
    DataTransferRequest,
    ProfiledProvider,
    ProfileStore,
    ProviderProfileService,
)
from app.capabilities.ai.prompts import ReviseRequest, load_seed
from app.capabilities.ai.registry import (
    BY_KEY,
    COMMON_POLICY_KEY,
    GLOBAL_KEY,
    Layer,
)
from app.capabilities.image_studio.checks import (
    CONTACT_DETAIL,
    LOW_CONFIDENCE,
    NUMBER_UNIT_DROPPED,
)
from app.capabilities.image_studio.contracts import OcrIdentity, OcrRegion, SentRegion
from app.capabilities.image_studio.task import (
    IMAGE_TRANSLATION_OUTPUT_INVALID,
    MAX_KOREAN_CHARS,
    TASK_KEY,
    RegistryTranslationComposer,
    parse_translations,
)
from app.capabilities.image_studio.translation import (
    IMAGE_TRANSLATE_NOT_CONFIGURED,
    IMAGE_TRANSLATE_REQUEST_INVALID,
    TRANSLATE_JOB,
    ImageTranslateItemsFailedError,
    ImageTranslationService,
    TranslateRequest,
)
from app.capabilities.jobs.models import Job
from app.capabilities.jobs.registry import JobContext
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError, InputValidationError, PolicyBlockedError
from app.platform.core.secrets import MemorySecretStore
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from app.stages.connect.state import CapabilityStatus
from app.stages.products.materialization import MaterializationStatus
from integrations.ai import cliproxyapi
from tests.integration.ai.test_ais1_provider_profile import (
    FakeProbe,
    _approve_all,
    _configure,
    _serving,
)
from tests.support.collect_support import PNG
from tests.support.jobs_support import FakeClock
from tests.support.product_support import Collections, FakeDecoder, count, raw

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
OCR = OcrIdentity("fake-ocr", "1.0.0", "pre-1")
ROLE = "ROLE_PRODUCT_MD_V1"
REGIONS = (
    OcrRegion("r0", (10, 10, 120, 40), "纯天然芝麻油", "zh", 0.97, 0.92),
    OcrRegion("r1", (10, 50, 120, 80), "净含量350ml", "zh", 0.88, 0.40),
    OcrRegion("r2", (10, 90, 120, 120), "微信:shop8888", "zh", 0.93, 0.95),
    OcrRegion("r3", (10, 130, 120, 160), "型号AB-1234 尺寸30*40cm", "zh", 0.95, 0.90),
    OcrRegion("r4", (10, 170, 120, 200), "厂家直销", "zh", 0.42, 0.90),
)
KOREAN = {
    "纯天然芝麻油": "순수 천연 참기름",
    "净含量350ml": "용량 350ml",
    "微信:shop8888": "위챗:shop8888",
    "型号AB-1234 尺寸30*40cm": "모델 AB-1234 사이즈 30x40cm",
    "厂家直销": "공장 직판",
}
TABLES = (
    "image_ocr_observations",
    "image_translation_candidates",
    "image_edit_revisions",
    "image_edit_current",
)
RULE = "-" * 40


def _runtime(text: str) -> dict[str, Any]:
    """The runtime data of one composed request: the last section of the prototype's layout."""
    data: dict[str, Any] = json.loads(text.rsplit(RULE + "\n", 1)[1])
    return data


@dataclass
class FakeSidecar:
    """The sidecar completion: it records the exact text it receives and answers one Korean text
    per region it was sent, or the scripted answer."""

    korean: dict[str, str] = field(default_factory=lambda: dict(KOREAN))
    answer: dict[str, Any] | None = None
    texts: list[str] = field(default_factory=list)

    def __call__(
        self, endpoint: str, key: str, model: str, text: str, verify: Any = None
    ) -> cliproxyapi.SidecarAnswer:
        assert verify is None or verify(50000)
        assert isinstance(text, str)
        self.texts.append(text)
        regions = _runtime(text)["regions"]
        value = self.answer or {
            "translations": [
                {"region_id": r["region_id"], "text": self.korean[r["text"]]} for r in regions
            ]
        }
        return cliproxyapi.SidecarAnswer(
            value=value,
            error_kind=None,
            error_code=None,
            actual_model="gpt-5.6-sol-2026",
            sidecar_version="7.2.155",
            tokens_in=300,
            tokens_out=12,
            latency_ms=900,
        )


@dataclass
class FakeOcr:
    regions: tuple[OcrRegion, ...] = REGIONS
    reads: list[bytes] = field(default_factory=list)

    def identity(self) -> OcrIdentity | None:
        return OCR

    def read(self, image: bytes) -> tuple[OcrRegion, ...]:
        self.reads.append(image)
        return self.regions


@dataclass
class World:
    service: ImageTranslationService
    sidecar: FakeSidecar
    profiles: ProviderProfileService
    group: str
    images: tuple[str, ...]


@pytest.fixture
def product(container: Container, config: AppConfig) -> tuple[str, tuple[str, ...]]:
    assets = SourceAssetStore(config.source_assets_dir, container.db, FakeDecoder(), FakeClock())
    detail = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=1,
        host="img.shop.example",
        provenance=".detail img:nth-of-type(1)",
        status=FieldStatus.CONFIRMED,
        sha256=assets.put(PNG + b"i4-detail").sha256,
    )
    run_id, _ = Collections.of(container, config).collect(
        source_product_id="4104", extra_images=(detail,)
    )
    result = container.materializer.materialize_run(run_id)
    assert result.status is MaterializationStatus.MATERIALIZED, result
    return str(result.product_group_id), (hashlib.sha256(PNG).hexdigest(), detail.sha256)


def _world(
    container: Container,
    tmp_path: Path,
    product: tuple[str, tuple[str, ...]],
    *,
    cap: int = 3,
    approve: bool = True,
    regions: tuple[OcrRegion, ...] = REGIONS,
) -> World:
    container.prompt_registry.seed_on_startup()
    sidecar = FakeSidecar()
    store = ProfileStore(container.db, container.clock, container.audit)
    provider = ProfiledProvider(
        store, FakeProbe(_serving(tmp_path)), container.clock, MemorySecretStore(), sidecar
    )
    profiles = ProviderProfileService(store, provider, container.clock)
    if approve:
        _approve_all(profiles, _configure(profiles, cap=cap))
    service = ImageTranslationService(
        clock=container.clock,
        store=container.image_studio,
        audit=container.audit,
        jobs=container.jobs,
        products=container.product_store,
        sources=container.source_assets,
        ocr=FakeOcr(regions),
        composer=RegistryTranslationComposer(container.ai_composer),
        execution=AIExecution(provider),
    )
    return World(service, sidecar, profiles, product[0], product[1])


def _run(container: Container, world: World, job_id: str) -> None:
    with container.db.read() as session:
        job = session.get(Job, job_id)
        assert job is not None and job.job_type == TRANSLATE_JOB
        payload, cid, attempts = json.loads(job.payload_json), job.correlation_id, job.max_attempts
    world.service.job_definition().handler(
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


def _translate(container: Container, world: World, **kwargs: Any) -> str:
    job_id = world.service.request(
        TranslateRequest(
            product_group_id=world.group,
            images=kwargs.pop("images", world.images[:1]),
            source_language="zh",
            actor="operator",
            **kwargs,
        )
    )
    _run(container, world, job_id)
    return job_id


def _observation(container: Container, sha: str) -> str:
    observation = container.image_studio.observation(OCR.key(sha))
    assert observation is not None
    return observation.observation_id


def _audit_dump(config: AppConfig) -> str:
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute("SELECT * FROM audit_events").fetchall()
    return "\n".join(str(row) for row in rows)


def _refused(code: str, call: Any) -> AppError:
    with pytest.raises(AppError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


# ---------------------------------------------------------------- the registered task


def test_the_registry_holds_the_task_with_its_schema_and_the_amendment(
    container: Container,
) -> None:
    entry = BY_KEY[TASK_KEY]
    assert (entry.layer, entry.role_key, entry.screen) == (Layer.TASK, ROLE, "등록관리")
    seed = load_seed()
    assert seed["seed_version"] == "v29-ai4"
    content = seed["templates"][TASK_KEY]["content"]
    assert content["mode"] == "IMAGE_TEXT_TRANSLATE"
    assert content["variables"] == "source_language\nregions[]"
    assert json.loads(content["output"]) == {"translations": [{"region_id": "", "text": ""}]}
    # Written by the startup seed like every task, as revision 1 of the newest seed.
    container.prompt_registry.seed_on_startup()
    stored = container.prompt_registry._store.entry(TASK_KEY)
    assert stored.current.revision_no == 1 and dict(stored.current.content) == content
    # The composition reads the stores' current revisions: they are its prompt and policy version.
    composed = RegistryTranslationComposer(container.ai_composer).compose(
        "ja", (SentRegion("r0", "天然ごま油"),)
    )
    assert (composed.task_key, composed.role_key, composed.policy_key, composed.mode) == (
        TASK_KEY,
        ROLE,
        COMMON_POLICY_KEY,
        "IMAGE_TEXT_TRANSLATE",
    )
    store = container.prompt_registry._store
    assert dict(composed.prompt_version) == {
        key: store.entry(key).current.revision_id for key in (GLOBAL_KEY, ROLE, TASK_KEY)
    }
    assert dict(composed.policy_version) == {
        COMMON_POLICY_KEY: store.entry(COMMON_POLICY_KEY).current.revision_id
    }
    assert _runtime(composed.text) == {
        "source_language": "ja",
        "regions": [{"region_id": "r0", "text": "天然ごま油"}],
    }
    # ADR-0041 §9 is in force: ADR-0026 §3.1 records the amendment.
    adr = (ROOT / "documents/decisions/adr/0026-ai-authoring-foundation.md").read_text("utf-8")
    assert "**Amendment (2026-10-14, ADR-0041 §9).**" in adr and TASK_KEY in adr


def test_the_composer_refuses_nothing_to_send_and_an_unknown_language(
    container: Container,
) -> None:
    container.prompt_registry.seed_on_startup()
    composer = RegistryTranslationComposer(container.ai_composer)
    with pytest.raises(InputValidationError):
        composer.compose("zh", ())
    with pytest.raises(InputValidationError):
        composer.compose("ko", (SentRegion("r0", "x"),))


def test_production_binds_the_task_and_still_reads_not_configured(container: Container) -> None:
    report = container.image_translation.capability()
    assert report.status is CapabilityStatus.NOT_CONFIGURED
    assert "ocr" in report.detail and "ai provider" in report.detail
    assert "translation task" not in report.detail


# ---------------------------------------------------------------- text only, through the profile


def test_the_composer_sends_text_only_and_only_the_sent_regions(
    container: Container,
    config: AppConfig,
    tmp_path: Path,
    product: tuple[str, tuple[str, ...]],
) -> None:
    world = _world(container, tmp_path, product)
    _translate(container, world)
    (text,) = world.sidecar.texts
    # One text request: the source language and each sent region's id and text, nothing else. The
    # contact detail is kept by default and never sent.
    assert _runtime(text) == {
        "source_language": "zh",
        "regions": [
            {"region_id": r.region_id, "text": r.text} for r in REGIONS if r.region_id != "r2"
        ],
    }
    assert "shop8888" not in text
    # No image travels: neither its bytes, nor its hash, nor any image content part.
    image = container.source_assets.read(world.images[0])
    assert world.images[0] not in text
    assert image.decode("latin-1") not in text
    assert "base64" not in text and "data:image" not in text and "image_url" not in text
    (candidate,) = container.image_studio.candidates(_observation(container, world.images[0]))
    assert candidate.sent_region_ids == ("r0", "r1", "r3", "r4")
    assert dict(candidate.translations)["r3"] == "모델 AB-1234 사이즈 30x40cm"
    # Provenance is copied from the outcome: the requested and the reported model; unknown cost
    # stays null.
    assert candidate.provenance["requested_model"] == "gpt-5.6-sol"
    assert candidate.provenance["actual_model"] == "gpt-5.6-sol-2026"
    assert candidate.provenance["vendor_cost"] is None
    assert candidate.inputs.output_schema_version == "image-text-translate/v1"
    # The call is in the profile's ledger under the task, and no audit row carries text.
    assert count(config, "ai_provider_calls", "task_key = ? AND outcome = 'OK'", TASK_KEY) == 1
    dump = _audit_dump(config)
    assert not any(t in dump for t in (*KOREAN, *KOREAN.values(), "GOAL"))


def test_a_contact_detail_is_sent_only_when_the_operator_includes_it(
    container: Container, tmp_path: Path, product: tuple[str, tuple[str, ...]]
) -> None:
    world = _world(container, tmp_path, product)
    sha = world.images[0]
    _translate(container, world)
    observation = _observation(container, sha)
    view = world.service.candidate_view(observation, "zh")
    assert view is not None and view.status == "FRESH"
    # Including it changes what is sent, so the input: the default candidate is stale for it.
    included = frozenset({"r2"})
    stale = world.service.candidate_view(observation, "zh", included=included)
    assert stale is not None and (stale.status, stale.stale_reasons) == ("STALE", ("input",))
    _translate(container, world, included={sha: included})
    sent = [r["region_id"] for r in _runtime(world.sidecar.texts[-1])["regions"]]
    assert sent == ["r0", "r1", "r2", "r3", "r4"]
    fresh = world.service.candidate_view(observation, "zh", included=included)
    assert fresh is not None and fresh.status == "FRESH"
    assert dict(fresh.candidate.translations)["r2"] == "위챗:shop8888"
    # A region both kept and included, or an inclusion outside the run, is refused.
    for request in (
        {"included": {sha: included}, "excluded": {sha: included}},
        {"included": {"f" * 64: included}},
    ):
        _refused(
            IMAGE_TRANSLATE_REQUEST_INVALID,
            lambda request=request: world.service.request(
                TranslateRequest(world.group, (sha,), "zh", "operator", **request)
            ),
        )


def test_without_the_data_transfer_approval_nothing_is_called(
    container: Container,
    config: AppConfig,
    tmp_path: Path,
    product: tuple[str, tuple[str, ...]],
) -> None:
    world = _world(container, tmp_path, product)
    # Approved when the run is queued, withdrawn before it runs: the run refuses before any call.
    job_id = world.service.request(
        TranslateRequest(world.group, world.images[:1], "zh", "operator")
    )
    current = world.profiles.view().current_revision
    assert current is not None
    world.profiles.set_data_transfer(
        DataTransferRequest(actor="owner", expected_current_revision=current, approved=False),
        cid="c-withdraw",
    )
    with pytest.raises(PolicyBlockedError):
        _run(container, world, job_id)
    report = world.service.capability()
    assert report.status is CapabilityStatus.NOT_CONFIGURED and "ai provider" in report.detail
    _refused(
        IMAGE_TRANSLATE_NOT_CONFIGURED,
        lambda: world.service.request(
            TranslateRequest(world.group, world.images[:1], "zh", "operator")
        ),
    )
    assert world.sidecar.texts == []
    assert count(config, "ai_provider_calls") == 0
    assert all(count(config, table) == 0 for table in TABLES)


def test_with_no_profile_at_all_nothing_is_queued_or_called(
    container: Container,
    config: AppConfig,
    tmp_path: Path,
    product: tuple[str, tuple[str, ...]],
) -> None:
    world = _world(container, tmp_path, product, approve=False)
    assert world.service.capability().status is CapabilityStatus.NOT_CONFIGURED
    _refused(
        IMAGE_TRANSLATE_NOT_CONFIGURED,
        lambda: world.service.request(
            TranslateRequest(world.group, world.images[:1], "zh", "operator")
        ),
    )
    assert world.sidecar.texts == []
    assert count(config, "jobs", "job_type = ?", TRANSLATE_JOB) == 0


def test_the_daily_cap_and_the_run_cap_are_respected(
    container: Container,
    config: AppConfig,
    tmp_path: Path,
    product: tuple[str, tuple[str, ...]],
) -> None:
    world = _world(container, tmp_path, product, cap=1)
    with pytest.raises(ImageTranslateItemsFailedError) as failed:
        _translate(container, world, images=world.images)
    assert failed.value.details == {"failed": 1, "total": 2}
    # One call was made; the second image was refused before anything was sent.
    assert len(world.sidecar.texts) == 1
    assert count(config, "ai_provider_calls") == 1
    assert count(config, "image_translation_candidates") == 1
    assert (
        count(
            config,
            "audit_events",
            "action = 'IMAGE_TRANSLATE_ITEM_FAILED' AND reason_code = 'AI_DAILY_CAP_REACHED'",
        )
        == 1
    )
    # One run translates at most 30 images (D5).
    _refused(
        IMAGE_TRANSLATE_REQUEST_INVALID,
        lambda: world.service.request(
            TranslateRequest(world.group, tuple(f"{n:064x}" for n in range(31)), "zh", "operator")
        ),
    )


@pytest.mark.parametrize(
    "answer",
    [
        {"translations": [{"region_id": "r0", "text": "참기름"}]},
        {"translations": [], "warnings": []},
        {"result": []},
        {"translations": [{"region_id": r, "text": "한국어"} for r in ("r0", "r1", "r3", "r3")]},
        {"translations": [{"region_id": r, "text": "한국어"} for r in ("r0", "r1", "r3", "r9")]},
        {
            "translations": [
                {"region_id": r, "text": " " if r == "r1" else "한국어"}
                for r in ("r0", "r1", "r3", "r4")
            ]
        },
        {
            "translations": [
                {"region_id": r, "text": "가" * (MAX_KOREAN_CHARS + 1)}
                for r in ("r0", "r1", "r3", "r4")
            ]
        },
        {"translations": [{"region_id": r, "text": 7} for r in ("r0", "r1", "r3", "r4")]},
        {
            "translations": [
                {"region_id": r, "text": "한국어", "confidence": 1}
                for r in ("r0", "r1", "r3", "r4")
            ]
        },
        {"translations": {"r0": "참기름"}},
    ],
    ids=[
        "short",
        "extra-key",
        "no-translations",
        "repeated",
        "unknown-region",
        "blank",
        "too-long",
        "not-text",
        "entry-extra-key",
        "not-a-list",
    ],
)
def test_a_malformed_answer_is_a_failed_item_with_nothing_durable(
    container: Container,
    config: AppConfig,
    tmp_path: Path,
    product: tuple[str, tuple[str, ...]],
    answer: dict[str, Any],
) -> None:
    world = _world(container, tmp_path, product)
    world.sidecar.answer = answer
    with pytest.raises(ImageTranslateItemsFailedError):
        _translate(container, world)
    assert len(world.sidecar.texts) == 1
    assert all(count(config, table) == 0 for table in TABLES)
    assert (
        count(
            config,
            "audit_events",
            "action = 'IMAGE_TRANSLATE_ITEM_FAILED' AND reason_code = ?",
            IMAGE_TRANSLATION_OUTPUT_INVALID,
        )
        == 1
    )


def test_an_answer_in_another_order_is_read_in_the_order_sent() -> None:
    sent = (SentRegion("r0", "a"), SentRegion("r1", "b"))
    answer = {
        "translations": [{"region_id": "r1", "text": "나"}, {"region_id": "r0", "text": "가"}]
    }
    assert parse_translations(answer, sent) == (("r0", "가"), ("r1", "나"))
    with pytest.raises(InputValidationError):
        parse_translations(None, sent)


# ---------------------------------------------------------------- flags and staleness on read


def test_every_region_carries_its_flags_on_read(
    container: Container, tmp_path: Path, product: tuple[str, tuple[str, ...]]
) -> None:
    world = _world(container, tmp_path, product)
    # The suggestion for r1 drops the quantity and its unit; r3 keeps the model and the size.
    world.sidecar.korean["净含量350ml"] = "순 함량"
    _translate(container, world)
    view = world.service.candidate_view(_observation(container, world.images[0]), "zh")
    assert view is not None and view.status == "FRESH"
    assert dict(view.flags) == {
        "r0": (),
        "r1": (NUMBER_UNIT_DROPPED,),
        # Kept by default, untranslated: flagged as a contact detail only.
        "r2": (CONTACT_DETAIL,),
        "r3": (),
        "r4": (LOW_CONFIDENCE,),
    }


def test_a_changed_prompt_or_policy_makes_the_candidate_stale_and_is_called_again(
    container: Container, tmp_path: Path, product: tuple[str, tuple[str, ...]]
) -> None:
    world = _world(container, tmp_path, product)
    _translate(container, world)
    observation = _observation(container, world.images[0])
    registry = container.prompt_registry

    def revise(key: str, layer: Any, text: str) -> None:
        # Candidates are read newest first: each run is recorded at a later time.
        container.clock.advance(60)  # type: ignore[attr-defined]
        current = registry._store.entry(key).current.revision_id
        registry.revise(
            key,
            layer,
            ReviseRequest(
                actor="operator", expected_current_revision=current, field="prompt", text=text
            ),
            cid="c-revise",
        )

    revise(COMMON_POLICY_KEY, "policy", "PLATFORM_POLICY\n공통 정책의 운영자 수정본")
    view = world.service.candidate_view(observation, "zh")
    assert view is not None and (view.status, view.stale_reasons) == ("STALE", ("policy",))
    _translate(container, world)
    assert len(world.sidecar.texts) == 2
    revise(TASK_KEY, "template", "GOAL\n운영자가 고친 번역 지시")
    view = world.service.candidate_view(observation, "zh")
    assert view is not None and (view.status, view.stale_reasons) == ("STALE", ("prompt",))
    # A stale candidate is never served: the run calls again under the new prompt.
    _translate(container, world)
    assert len(world.sidecar.texts) == 3
    assert "운영자가 고친 번역 지시" in world.sidecar.texts[-1]
    view = world.service.candidate_view(observation, "zh")
    assert view is not None and view.status == "FRESH"
    # And an equal fingerprint is reused, with no call.
    _translate(container, world)
    assert len(world.sidecar.texts) == 3
