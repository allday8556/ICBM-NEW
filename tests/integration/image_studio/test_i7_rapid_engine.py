"""ADR-0041 I7: the real local OCR engine (RapidOCR 3.9.2 on onnxruntime 1.30.0, D2).

The engine reads a synthetic image drawn with the bundled Noto Sans KR: one Chinese line in
characters the Korean font holds (traditional forms and Hanja, never a simplified-only form, which
it lacks) and one Japanese line with kana. It is read through ``EngineOcrProvider`` with every
outbound connection refused, from binding the engine to its first reading, which builds RapidOCR
from the pinned local models. No AI provider is involved.

The tests that read with the real engine are skipped where the optional ``ocr`` extra is not
installed (CI); the binding switch is proven everywhere.
"""

import io
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from app.capabilities.detail_guidance.renderer import FONT_FILE, FONT_SHA256
from app.capabilities.image_studio import review
from app.capabilities.image_studio.contracts import NoOcrProvider
from app.capabilities.image_studio.ocr import EngineOcrProvider, ocr_capability
from app.capabilities.image_studio.rapid import (
    ENGINE_ID,
    MODEL_SHA256S,
    local_ocr_provider,
    rapid_ocr_engine,
)
from app.capabilities.image_studio.renderer import drawable_codepoints
from app.capabilities.image_studio.style import Face
from app.config import AppConfig
from app.container import Container
from app.stages.connect.state import CapabilityStatus
from tests.conftest import LOCAL
from tests.integration.image_studio.test_i5_studio_routes import (  # noqa: F401 - fixtures
    _apply,
    _run,
    _save,
    _state,
    client,
    container,
    item,
)
from tests.support.image_studio_support import StudioProduct, make_ready

pytestmark = pytest.mark.integration

try:  # the optional extra, never imported by the application at module level
    import onnxruntime  # noqa: F401
    import rapidocr  # noqa: F401

    INSTALLED = True
except ImportError:
    INSTALLED = False

needs_engine = pytest.mark.skipif(not INSTALLED, reason="the optional `ocr` extra is not installed")

CHINESE = "天然芝麻油 含量350ml"
JAPANESE = "こちらは天然ごま油です"
LINES = ((CHINESE, (30, 30)), (JAPANESE, (30, 120)), ("Made in Korea 2024", (30, 210)))


def _scene() -> bytes:
    font = ImageFont.truetype(str(FONT_FILE), 40, layout_engine=ImageFont.Layout.BASIC)
    image = Image.new("RGB", (760, 280), "white")
    draw = ImageDraw.Draw(image)
    for text, at in LINES:
        draw.text(at, text, fill=(30, 30, 30), font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _ink(text: str, at: tuple[int, int]) -> tuple[int, int, int, int]:
    font = ImageFont.truetype(str(FONT_FILE), 40, layout_engine=ImageFont.Layout.BASIC)
    left, top, right, bottom = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(at, text, font)
    return (int(left), int(top), int(right), int(bottom))


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """Every outbound connection and name lookup is refused and recorded."""
    attempts: list[object] = []

    def refuse(*args: object, **_kwargs: object) -> None:
        attempts.append(args[1:] or args)
        raise OSError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield attempts


def test_the_scene_uses_only_glyphs_the_bundled_korean_font_draws() -> None:
    drawable = drawable_codepoints(Face.SANS)
    assert all(ord(c) in drawable for text, _ in LINES for c in text)


@needs_engine
def test_the_real_engine_reads_chinese_and_japanese_offline(offline: list[object]) -> None:
    engine = rapid_ocr_engine()
    assert engine is not None, "the installed engine is not the pinned one"
    provider = EngineOcrProvider(engine)
    identity = provider.identity()
    assert identity is not None
    assert (identity.engine_id, identity.engine_version) == (ENGINE_ID, "3.9.2+onnxruntime-1.30.0")
    assert identity.model_sha256s == tuple(sorted(MODEL_SHA256S.values()))
    source = _scene()
    first = provider.observe(source)
    assert offline == []
    assert [r.script for r in first.regions] == ["zh", "ja"]
    zh, ja = first.regions
    # The text is the engine's own reading; the scripts and places are what matter here.
    assert "芝麻油" in zh.text and "350" in zh.text
    assert "ごま油" in ja.text
    for region, (text, at) in zip(first.regions, LINES, strict=False):
        ink = _ink(text, at)
        left, top, right, bottom = region.box
        # The box holds the drawn line's ink, give or take the engine's margin.
        assert left <= ink[0] + 4 and top <= ink[1] + 4, region.region_id
        assert right >= ink[2] - 4 and bottom >= ink[3] - 4, region.region_id
        assert right - left <= ink[2] - ink[0] + 40 and bottom - top <= ink[3] - ink[1] + 30
        assert region.polygon is not None and len(region.polygon) == 4
        assert region.confidence >= 0.8 and region.background_uniformity == 1.0
    assert [s.reason for s in first.skipped] == ["SCRIPT_NOT_CJK"]
    # The same image read again gives the same observation.
    assert provider.observe(source) == first
    assert offline == []


@needs_engine
def test_the_served_application_binds_the_engine_and_reads_ready(config: AppConfig) -> None:
    from app.main import create_app

    enabled = config.with_overrides(image_ocr_engine=True)
    with TestClient(create_app(enabled), base_url=LOCAL) as served:
        ready = served.get("/api/ready", headers={"X-ICBM-Client": "pytest"}).json()
    (ocr,) = [c for c in ready["capabilities"] if c["key"] == "image_ocr"]
    assert ocr["status"] == "READY" and f"engine={ENGINE_ID}" in ocr["detail"]
    assert "image_ocr" not in ready["degraded_capabilities"]
    assert (ready["status"], ready["overall"]) == ("PASS", "READY")


def test_an_edit_saved_under_renderer_v1_is_saved_again_before_it_is_applied(
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    item: StudioProduct,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2 changed the font set: a revision saved under v1 (Noto Sans KR only) names the old
    renderer and fonts, and 「적용」 refuses it until it is saved again (IT-03)."""
    make_ready(container, tmp_path)
    _run(client, container, item)
    state = _state(client, item)
    with monkeypatch.context() as v1:
        v1.setattr(review, "RENDERER_VERSION", "image-translate-renderer/v1")
        v1.setattr(review, "FONT_HASHES", {FONT_FILE.name: FONT_SHA256})
        old = _save(client, item, state, r0=None).json()
    refused = _apply(client, item, old["revision_id"], [])
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == review.IMAGE_STUDIO_RENDERER_CHANGED
    saved = _save(client, item, _state(client, item), r0=None).json()
    applied = _apply(client, item, saved["revision_id"], [])
    assert applied.status_code == 200, applied.json()


def test_the_switch_off_binds_no_engine_even_where_one_is_installed(config: AppConfig) -> None:
    assert config.image_ocr_engine is False  # the test harness's AppConfig
    assert AppConfig(data_dir=config.data_dir).image_ocr_engine is True  # production: automatic
    provider = local_ocr_provider(False)
    assert isinstance(provider, NoOcrProvider)
    assert ocr_capability(provider).status is CapabilityStatus.NOT_CONFIGURED
