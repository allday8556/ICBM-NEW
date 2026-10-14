"""ADR-0041 I2: the OCR port, the provider-zero ``image_ocr`` capability and observation scoring.

Production binds ``NoOcrProvider``: ``image_ocr`` reads NOT_CONFIGURED, never a core failure, and a
translation is refused before anything is written. The port is proven over a fake engine that reads
nothing: it is told where the lines of a synthetic image are and answers, per tile it is handed,
the lines inside that tile (clipped at its edges as a real engine would see them). Everything else
is production code: preprocessing, tiling, seam merge, script detection, confidence clamping and
the background-uniformity score from I3's ``read_background``, through to the recorded observation.
No engine, model, network or provider is involved.
"""

import hashlib
import io
import random
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.capabilities.ai.execution import AIExecution
from app.capabilities.image_studio.contracts import (
    IMAGE_OCR_NOT_CONFIGURED,
    NoOcrProvider,
    OcrIdentity,
)
from app.capabilities.image_studio.ocr import (
    IMAGE_OCR_SOURCE_UNREADABLE,
    PREPROCESSING,
    PREPROCESSING_VERSION,
    SKIP_SCRIPT_KOREAN,
    SKIP_SCRIPT_NOT_CJK,
    EngineIdentity,
    EngineLine,
    EngineOcrProvider,
    Preprocessing,
    as_box,
    ocr_capability,
    tile_spans,
)
from app.capabilities.image_studio.renderer import decode_source
from app.capabilities.image_studio.style import CLEAN_FROM, Box, read_background
from app.capabilities.image_studio.translation import (
    IMAGE_TRANSLATE_NOT_CONFIGURED,
    TRANSLATE_JOB,
    ImageTranslationService,
    TranslateRequest,
)
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError, InputValidationError, PolicyBlockedError
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from app.stages.connect.state import CapabilityStatus
from app.stages.products.materialization import MaterializationStatus
from tests.integration.image_studio.test_i1_store import (
    TABLES,
    FakeComposer,
    FakeTranslator,
    Rig,
    _run,
)
from tests.integration.image_studio.test_i3_renderer import _noisy_scene, _png, _text_box
from tests.support.jobs_support import FakeClock
from tests.support.product_support import Collections, FakeDecoder, count

pytestmark = pytest.mark.integration

MODELS = ("1" * 64, "2" * 64)
ENGINE = EngineIdentity("fake-engine", "0.1.0", MODELS)
WHITE = (255, 255, 255)


@dataclass(frozen=True)
class Truth:
    """A line of the synthetic image, in the image's grid, as a perfect reader would see it."""

    box: tuple[int, int, int, int]
    text: str
    confidence: float = 0.9


@dataclass
class FakeEngine:
    """Answers, for each tile in the order the provider hands them, the true lines inside it,
    clipped to the tile: a line crossing a tile edge is seen as a fragment there."""

    lines: Sequence[Truth] = ()
    image_height: int = 0
    engine: EngineIdentity | None = ENGINE
    preprocessing: Preprocessing = PREPROCESSING
    tiles: list[Image.Image] = field(default_factory=list)

    def identity(self) -> EngineIdentity | None:
        return self.engine

    def recognise(self, tile: Image.Image) -> list[EngineLine]:
        spans = tile_spans(
            self.image_height, self.preprocessing.tile_height, self.preprocessing.tile_overlap
        )
        top, bottom = spans[len(self.tiles) % len(spans)]
        self.tiles.append(tile.copy())
        assert tile.height == bottom - top
        seen = []
        for line in self.lines:
            left, upper, right, lower = line.box
            if lower <= top or upper >= bottom:
                continue
            whole = upper >= top and lower <= bottom
            seen.append(
                EngineLine(
                    line.text,
                    line.confidence if whole else line.confidence - 0.3,
                    box=(left, max(upper, top) - top, right, min(lower, bottom) - top),
                )
            )
        return seen


def _provider(
    engine: FakeEngine, preprocessing: Preprocessing = PREPROCESSING
) -> EngineOcrProvider:
    engine.preprocessing = preprocessing
    return EngineOcrProvider(engine, preprocessing)


def _grown(box: Box) -> tuple[int, int, int, int]:
    return (box.x, box.y, box.right, box.bottom)


def _scene() -> tuple[bytes, list[Truth]]:
    """A flat white top half with Chinese, Japanese, Latin and Korean lines; a noisy photo-like
    bottom half with a Chinese line over it."""
    image = Image.new("RGB", (420, 320), WHITE)
    zh = _text_box(image, (40, 20), "纯天然芝麻油", size=28)
    ja = _text_box(image, (40, 70), "天然ごま油", size=28)
    latin = _text_box(image, (260, 20), "SALE 50%", size=20)
    korean = _text_box(image, (260, 70), "참기름", size=20)
    noisy, noisy_box = _noisy_scene()
    image.paste(noisy, (0, 160))
    photo = Box(noisy_box.x, noisy_box.y + 160, noisy_box.width, noisy_box.height)
    return _png(image), [
        Truth(_grown(zh), "  纯天然芝麻油 ", 1.3),
        Truth(_grown(ja), "天然ごま油", 0.62),
        Truth(_grown(latin), "SALE 50%"),
        Truth(_grown(korean), "참기름"),
        Truth(_grown(photo), "新品上市", -0.4),
    ]


# ---------------------------------------------------------------- provider-zero production


def test_readiness_reports_image_ocr_not_configured_and_never_a_core_failure(
    config: AppConfig,
) -> None:
    from app.main import create_app
    from tests.conftest import LOCAL

    with TestClient(create_app(config), base_url=LOCAL) as client:
        ready = client.get("/api/ready", headers={"X-ICBM-Client": "pytest"}).json()
    assert (ready["status"], ready["overall"]) == ("PASS", "READY")
    ocr = [c for c in ready["capabilities"] if c["key"] == "image_ocr"]
    assert ocr == [
        {
            "key": "image_ocr",
            "status": "NOT_CONFIGURED",
            "detail": "no OCR engine is installed (ADR-0041 D2)",
        }
    ]
    assert "image_ocr" in ready["degraded_capabilities"]
    assert all(check["name"] != "image_ocr" for check in ready["checks"])


def test_production_binds_no_engine_and_a_request_writes_nothing(
    container: Container, config: AppConfig
) -> None:
    assert ocr_capability(NoOcrProvider()).status is CapabilityStatus.NOT_CONFIGURED
    with pytest.raises(PolicyBlockedError) as caught:
        NoOcrProvider().read(b"anything")
    assert caught.value.code == IMAGE_OCR_NOT_CONFIGURED
    report = container.image_translation.capability()
    assert report.status is CapabilityStatus.NOT_CONFIGURED and "ocr" in report.detail
    with pytest.raises(PolicyBlockedError) as refused:
        container.image_translation.request(
            TranslateRequest("any-group", ("0" * 64,), "zh", "operator")
        )
    assert refused.value.code == IMAGE_TRANSLATE_NOT_CONFIGURED
    assert count(config, "jobs", "job_type = ?", TRANSLATE_JOB) == 0
    assert all(count(config, table) == 0 for table in TABLES)


def test_an_engine_that_names_nothing_is_not_configured_and_is_never_asked() -> None:
    engine = FakeEngine(engine=None)
    provider = _provider(engine)
    assert provider.identity() is None
    report = ocr_capability(provider)
    assert (report.key, report.status) == ("image_ocr", CapabilityStatus.NOT_CONFIGURED)
    with pytest.raises(PolicyBlockedError) as caught:
        provider.read(_scene()[0])
    assert caught.value.code == IMAGE_OCR_NOT_CONFIGURED
    assert engine.tiles == []


def test_a_ready_engine_names_itself_and_one_that_cannot_degrades() -> None:
    report = ocr_capability(_provider(FakeEngine()))
    assert report.status is CapabilityStatus.READY
    assert "engine=fake-engine" in report.detail and PREPROCESSING_VERSION in report.detail

    class Broken:
        def identity(self) -> OcrIdentity | None:
            raise RuntimeError("model file missing")

        def read(self, image: bytes) -> tuple[()]:
            return ()

    assert ocr_capability(Broken()).status is CapabilityStatus.DEGRADED


# ---------------------------------------------------------------- reading through the port


def test_a_fake_engine_reads_through_the_port_with_scores_from_the_source() -> None:
    png, truth = _scene()
    engine = FakeEngine(truth, 320)
    reading = _provider(engine).observe(png)

    identity = reading.identity
    assert identity == OcrIdentity("fake-engine", "0.1.0", PREPROCESSING_VERSION, MODELS)
    assert reading.tiles == ((0, 320),)
    (tile,) = engine.tiles
    assert (tile.mode, tile.size) == ("RGB", (420, 320))

    regions = {r.text: r for r in reading.regions}
    assert [r.region_id for r in reading.regions] == ["r0", "r1", "r2"]
    assert [r.text for r in reading.regions] == ["纯天然芝麻油", "天然ごま油", "新品上市"]
    zh, ja, photo = regions["纯天然芝麻油"], regions["天然ごま油"], regions["新品上市"]
    assert (zh.script, ja.script, photo.script) == ("zh", "ja", "zh")
    # The engine's confidence, clamped to 0..1.
    assert (zh.confidence, ja.confidence, photo.confidence) == (1.0, 0.62, 0.0)
    # Boxes in the decoded pixel grid, exactly as the engine placed them.
    assert zh.box == truth[0].box and photo.box == truth[4].box
    # The background score is I3's, on the source as the renderer decodes it.
    source = decode_source(png)
    for region in reading.regions:
        assert region.background_uniformity == read_background(source, as_box(region.box)).score
    assert zh.background_uniformity >= CLEAN_FROM and ja.background_uniformity >= CLEAN_FROM
    assert read_background(source, as_box(zh.box)).clean
    assert photo.background_uniformity < CLEAN_FROM
    assert not read_background(source, as_box(photo.box)).clean
    # Latin and Korean lines are not translatable: skipped with their reason, never their text.
    assert sorted(s.reason for s in reading.skipped) == [SKIP_SCRIPT_KOREAN, SKIP_SCRIPT_NOT_CJK]
    assert {s.box for s in reading.skipped} == {truth[2].box, truth[3].box}
    # ``read`` is the regions of the same reading.
    assert _provider(FakeEngine(truth, 320)).read(png) == reading.regions


def test_no_exif_orientation_is_applied_and_alpha_is_read_over_white() -> None:
    image = Image.new("RGB", (300, 120), WHITE)
    box = _text_box(image, (20, 30), "芝麻油", size=28)
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° on display: the pixel grid stays 300 x 120
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95, exif=exif)
    engine = FakeEngine([Truth(_grown(box), "芝麻油")], 120)
    (region,) = _provider(engine).read(buffer.getvalue())
    assert engine.tiles[0].size == (300, 120) and region.box == _grown(box)

    clear = Image.new("RGBA", (200, 80), (0, 0, 0, 0))
    ImageDraw.Draw(clear).rectangle((20, 20, 60, 40), fill=(0, 0, 0, 255))
    engine = FakeEngine([Truth((16, 16, 64, 44), "字")], 80)
    _provider(engine).read(_png(clear))
    prepared = engine.tiles[0]
    assert prepared.mode == "RGB"
    assert prepared.getpixel((5, 5)) == WHITE and prepared.getpixel((30, 30)) == (0, 0, 0)


def test_an_unreadable_source_is_refused() -> None:
    with pytest.raises(InputValidationError) as caught:
        _provider(FakeEngine()).read(b"\x89PNG\r\n\x1a\nnot an image")
    assert caught.value.code == IMAGE_OCR_SOURCE_UNREADABLE


def test_a_tall_image_is_tiled_and_a_line_across_a_seam_is_read_once() -> None:
    height = 5000
    image = Image.new("RGB", (300, height), WHITE)
    draw = ImageDraw.Draw(image)
    truth = [
        Truth((20, 100, 180, 140), "第一行"),  # tile 0 only
        Truth((20, 1900, 180, 1940), "第二行", 0.8),  # whole in tiles 0 and 1
        Truth((20, 2030, 180, 2070), "第三行"),  # crosses tile 0's bottom edge
        Truth((200, 1700, 280, 2100), "大字"),  # taller than the overlap: cut in both
        Truth((20, 3700, 180, 3740), "第五行"),  # whole in tiles 1 and 2
        Truth((20, 4900, 180, 4940), "第六行"),  # tile 2 only
    ]
    for line in truth:  # a thin stroke of "ink" across the middle of each line's box
        left, top, right, bottom = line.box
        middle = (top + bottom) // 2
        draw.rectangle((left + 6, middle - 2, right - 6, middle + 2), fill=(30, 30, 30))
    engine = FakeEngine(truth, height)
    reading = _provider(engine).observe(_png(image))

    assert reading.tiles == ((0, 2048), (1792, 3840), (3584, 5000))
    assert [t.size for t in engine.tiles] == [(300, 2048), (300, 2048), (300, 1416)]
    assert [(r.text, r.box) for r in reading.regions] == [
        ("第一行", (20, 100, 180, 140)),
        ("大字", (200, 1700, 280, 2100)),
        ("第二行", (20, 1900, 180, 1940)),
        ("第三行", (20, 2030, 180, 2070)),
        ("第五行", (20, 3700, 180, 3740)),
        ("第六行", (20, 4900, 180, 4940)),
    ]
    by_text = {r.text: r for r in reading.regions}
    # The whole reading wins over the fragment; the line cut in both tiles is their union.
    assert by_text["第三行"].confidence == 0.9 and by_text["大字"].confidence == 0.6
    assert by_text["大字"].polygon is None
    assert all(r.background_uniformity >= CLEAN_FROM for r in reading.regions)
    assert reading.skipped == ()


# ---------------------------------------------------------------- the job, through the port


@pytest.fixture
def scene_product(container: Container, config: AppConfig) -> tuple[str, str, bytes, list[Truth]]:
    png, truth = _scene()
    assets = SourceAssetStore(config.source_assets_dir, container.db, FakeDecoder(), FakeClock())
    sha = assets.put(png).sha256
    detail = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=1,
        host="img.shop.example",
        provenance=".detail img:nth-of-type(1)",
        status=FieldStatus.CONFIRMED,
        sha256=sha,
    )
    run_id, _ = Collections.of(container, config).collect(
        source_product_id="4102", extra_images=(detail,)
    )
    result = container.materializer.materialize_run(run_id)
    assert result.status is MaterializationStatus.MATERIALIZED, result
    assert sha == hashlib.sha256(png).hexdigest()
    return str(result.product_group_id), sha, png, truth


def _service(container: Container, provider: EngineOcrProvider) -> Rig:
    composer, translator = FakeComposer(), FakeTranslator()
    service = ImageTranslationService(
        clock=container.clock,
        store=container.image_studio,
        audit=container.audit,
        jobs=container.jobs,
        products=container.product_store,
        sources=container.source_assets,
        ocr=provider,
        composer=composer,
        execution=AIExecution(translator),
    )
    return Rig(service, provider, composer, translator)  # type: ignore[arg-type]


def _translate(container: Container, rig: Rig, group: str, sha: str) -> None:
    job_id = rig.service.request(TranslateRequest(group, (sha,), "zh", "operator"))
    _run(container, rig, job_id)


def test_the_job_records_the_scored_observation_under_the_engine_and_preprocessing_key(
    container: Container, config: AppConfig, scene_product: tuple[str, str, bytes, list[Truth]]
) -> None:
    group, sha, png, truth = scene_product
    engine = FakeEngine(truth, 320)
    provider = _provider(engine)
    _translate(container, _service(container, provider), group, sha)
    identity = provider.identity()
    assert identity is not None
    key = identity.key(sha)
    assert key.preprocessing_version == PREPROCESSING_VERSION
    assert key.engine_version.startswith("0.1.0+models.")
    observation = container.image_studio.observation(key)
    assert observation is not None
    assert observation.regions == _provider(FakeEngine(truth, 320)).read(png)
    assert len(engine.tiles) == 1

    # The same engine and preprocessing: the observation is reused, the engine is not asked.
    _translate(container, _service(container, provider), group, sha)
    assert len(engine.tiles) == 1
    assert count(config, "image_ocr_observations") == 1

    # Another engine version, or another preprocessing, is another reading under its own key.
    newer = FakeEngine(truth, 320, engine=EngineIdentity("fake-engine", "0.2.0", MODELS))
    _translate(container, _service(container, _provider(newer)), group, sha)
    other_models = FakeEngine(
        truth, 320, engine=EngineIdentity("fake-engine", "0.1.0", ("3" * 64,))
    )
    _translate(container, _service(container, _provider(other_models)), group, sha)
    preprocessing = Preprocessing("ocr-preprocess/v2-test", 2048, 256)
    reprocessed = FakeEngine(truth, 320)
    _translate(container, _service(container, _provider(reprocessed, preprocessing)), group, sha)
    assert [len(e.tiles) for e in (newer, other_models, reprocessed)] == [1, 1, 1]
    keys = {
        p.identity().key(sha)  # type: ignore[union-attr]
        for p in (
            provider,
            _provider(newer),
            _provider(other_models),
            _provider(reprocessed, preprocessing),
        )
    }
    assert len(keys) == 4
    assert all(container.image_studio.observation(k) is not None for k in keys)
    assert count(config, "image_ocr_observations") == 4


def test_an_engine_fault_fails_only_its_item_and_records_nothing(
    container: Container, config: AppConfig, scene_product: tuple[str, str, bytes, list[Truth]]
) -> None:
    group, sha, _, truth = scene_product

    class Exploding(FakeEngine):
        def recognise(self, tile: Image.Image) -> list[EngineLine]:
            raise RuntimeError("onnxruntime fault")

    rig = _service(container, _provider(Exploding(truth, 320)))
    with pytest.raises(AppError):
        _translate(container, rig, group, sha)
    assert all(count(config, table) == 0 for table in TABLES)


def test_scores_are_deterministic_for_a_noisy_background() -> None:
    rng = random.Random(5)
    image = Image.new("RGB", (240, 120))
    image.putdata(
        [tuple(rng.randint(0, 255) for _ in range(3)) for _ in range(240 * 120)]  # type: ignore[misc]
    )
    png = _png(image)
    truth = [Truth((40, 40, 200, 80), "噪声背景")]
    first = _provider(FakeEngine(truth, 120)).read(png)
    again = _provider(FakeEngine(truth, 120)).read(png)
    assert first == again and first[0].background_uniformity < CLEAN_FROM
