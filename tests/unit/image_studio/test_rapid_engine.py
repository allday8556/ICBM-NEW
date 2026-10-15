"""ADR-0041 I7: the RapidOCR adapter, without RapidOCR.

These run where the optional ``ocr`` extra is not installed (CI): RapidOCR, onnxruntime and their
result are stood in for by fake modules and objects, and the model files by small local files
pinned by their own SHA-256. What is proven here is the adapter's own logic: the version and model
refusals, the explicit local model paths handed to RapidOCR (so it never downloads), the lazy and
serialised construction, the mapping of a result to engine lines and the production binding. The
real engine is read in ``tests/integration/image_studio/test_i7_rapid_engine.py``.
"""

import hashlib
import io
import logging
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest
from PIL import Image, ImageDraw

from app.capabilities.image_studio import rapid
from app.capabilities.image_studio.contracts import NoOcrProvider
from app.capabilities.image_studio.ocr import (
    SKIP_LINE_INVALID,
    EngineLine,
    EngineOcrProvider,
    ocr_capability,
)
from app.capabilities.image_studio.rapid import (
    CLS_MODEL,
    DET_MODEL,
    ENGINE_ID,
    ENGINE_VERSION,
    MODEL_MISSING,
    MODEL_REFUSED,
    MODEL_SHA256S,
    NOT_INSTALLED,
    REC_MODEL,
    VERSION_REFUSED,
    EngineRefused,
    RapidOcrEngine,
    check_versions,
    engine_identity,
    lines_from,
    local_ocr_provider,
    rapid_ocr_engine,
    rapidocr_params,
    verified_models,
)
from app.stages.connect.state import CapabilityStatus

SECRET = "秘密文字"


class Result:
    """RapidOCR's result shape: parallel columns, or ``None`` when nothing was found."""

    def __init__(
        self,
        boxes: list[Any] | None = None,
        txts: list[Any] | None = None,
        scores: list[Any] | None = None,
    ) -> None:
        self.boxes, self.txts, self.scores = boxes, txts, scores


def _quad(left: float, top: float, right: float, bottom: float) -> list[list[float]]:
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _models(directory: Path, contents: dict[str, bytes] | None = None) -> dict[str, str]:
    """Three model files in ``directory`` and the pins of exactly those bytes."""
    directory.mkdir(parents=True, exist_ok=True)
    contents = contents or {DET_MODEL: b"det", REC_MODEL: b"rec", CLS_MODEL: b"cls"}
    for name, data in contents.items():
        (directory / name).write_bytes(data)
    return {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}


class FakeRapidOCR:
    """Records how it is built and what it is handed; answers one scripted result."""

    built: ClassVar[list[dict[str, Any]]] = []
    handed: ClassVar[list[Any]] = []
    answer: ClassVar[Any] = Result()

    def __init__(self, params: dict[str, Any]) -> None:
        FakeRapidOCR.built.append(params)

    def __call__(self, image: Any) -> Any:
        FakeRapidOCR.handed.append(image)
        return FakeRapidOCR.answer


@pytest.fixture
def fake_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Fake ``rapidocr`` (3.9.2, its ``models/`` directory empty) and ``onnxruntime`` (1.30.0)."""
    package = tmp_path / "site" / "rapidocr"
    package.mkdir(parents=True)
    module = types.ModuleType("rapidocr")
    module.__file__ = str(package / "__init__.py")
    version = types.ModuleType("rapidocr._version")
    version.__version__ = "3.9.2"  # type: ignore[attr-defined]
    module._version = version  # type: ignore[attr-defined]
    module.RapidOCR = FakeRapidOCR  # type: ignore[attr-defined]
    runtime = types.ModuleType("onnxruntime")
    runtime.__version__ = "1.30.0"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rapidocr", module)
    monkeypatch.setitem(sys.modules, "rapidocr._version", version)
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)
    FakeRapidOCR.built, FakeRapidOCR.handed, FakeRapidOCR.answer = [], [], Result()
    # No NumPy where the extra is not installed: the tile is handed on as it is.
    monkeypatch.setattr(rapid, "to_bgr", lambda tile: ("bgr", tile.size))
    return package / "models"


def _reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [getattr(r, "reason", "") for r in caplog.records if hasattr(r, "reason")]


# ---------------------------------------------------------------- the pins


def test_the_pins_are_the_wheels_bundled_models_and_the_identity_names_them_sorted() -> None:
    assert dict(MODEL_SHA256S) == {
        "PP-OCRv6_det_small.onnx": (
            "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f"
        ),
        "PP-OCRv6_rec_small.onnx": (
            "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"
        ),
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx": (
            "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"
        ),
    }
    identity = engine_identity()
    assert (identity.engine_id, identity.engine_version) == ("rapidocr", "3.9.2+onnxruntime-1.30.0")
    assert identity.model_sha256s == tuple(sorted(MODEL_SHA256S.values()))
    provider = EngineOcrProvider(RapidOcrEngine(identity, lambda tile: Result()))
    ocr = provider.identity()
    assert ocr is not None and len(ocr.key_engine_version) <= 64
    assert ocr.key_engine_version.startswith(f"{ENGINE_VERSION}+models.")


@pytest.mark.parametrize(
    ("rapidocr_version", "onnxruntime_version"),
    [("3.9.1", "1.30.0"), ("3.9.2", "1.29.0"), ("3.10.0", "1.31.0"), (None, "1.30.0")],
)
def test_any_other_version_is_refused(
    rapidocr_version: object, onnxruntime_version: object
) -> None:
    check_versions("3.9.2", "1.30.0")
    with pytest.raises(EngineRefused) as refused:
        check_versions(rapidocr_version, onnxruntime_version)
    assert refused.value.reason == VERSION_REFUSED


def test_a_missing_or_changed_model_is_refused(tmp_path: Path) -> None:
    pins = _models(tmp_path)
    assert verified_models(tmp_path, pins) == {name: tmp_path / name for name in pins}
    (tmp_path / REC_MODEL).write_bytes(b"rec, changed")
    with pytest.raises(EngineRefused) as changed:
        verified_models(tmp_path, pins)
    assert changed.value.reason == MODEL_REFUSED
    (tmp_path / REC_MODEL).unlink()
    with pytest.raises(EngineRefused) as missing:
        verified_models(tmp_path, pins)
    assert missing.value.reason == MODEL_MISSING
    # The real pins: files of any other bytes are refused.
    with pytest.raises(EngineRefused) as unpinned:
        verified_models(tmp_path)
    assert unpinned.value.reason in (MODEL_MISSING, MODEL_REFUSED)


# ---------------------------------------------------------------- the factory


def test_without_the_extra_nothing_is_bound(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "rapidocr", None)
    caplog.set_level(logging.INFO, "icbm.image_studio")
    assert rapid_ocr_engine() is None
    assert _reasons(caplog) == [NOT_INSTALLED]
    provider = local_ocr_provider(True)
    assert isinstance(provider, NoOcrProvider)
    assert ocr_capability(provider).status is CapabilityStatus.NOT_CONFIGURED


def test_another_version_or_unpinned_models_bind_nothing(
    fake_install: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "icbm.image_studio")
    # The wheel's models are not the pinned files (here: other bytes, then none).
    _models(fake_install)
    assert rapid_ocr_engine() is None
    for name in MODEL_SHA256S:
        (fake_install / name).unlink()
    assert rapid_ocr_engine() is None
    pins = _models(fake_install)
    assert rapid_ocr_engine(pins) is not None
    monkeypatch.setattr(sys.modules["rapidocr._version"], "__version__", "3.9.3")
    assert rapid_ocr_engine(pins) is None
    monkeypatch.setattr(sys.modules["rapidocr._version"], "__version__", "3.9.2")
    monkeypatch.setattr(sys.modules["onnxruntime"], "__version__", "1.22.0")
    assert rapid_ocr_engine(pins) is None
    assert _reasons(caplog) == [MODEL_REFUSED, MODEL_MISSING, VERSION_REFUSED, VERSION_REFUSED]
    assert FakeRapidOCR.built == []


def test_rapidocr_is_built_once_on_first_use_from_the_local_models_only(
    fake_install: Path,
) -> None:
    pins = _models(fake_install)
    engine = rapid_ocr_engine(pins)
    assert engine is not None
    assert engine.identity() == engine_identity(pins)
    assert FakeRapidOCR.built == []  # nothing is loaded until a tile is read
    tile = Image.new("RGB", (40, 20), "white")
    assert engine.recognise(tile) == ()
    assert engine.recognise(tile) == ()
    (params,) = FakeRapidOCR.built
    assert params == rapidocr_params({name: fake_install / name for name in pins})
    # Every model by its verified local path: RapidOCR downloads only a model it is not given.
    assert {params[f"{s}.model_path"] for s in ("Det", "Rec", "Cls")} == {
        str(fake_install / name) for name in (DET_MODEL, REC_MODEL, CLS_MODEL)
    }
    assert params["Global.text_score"] == 0.5 and params["Global.log_level"] == "warning"
    assert 1 <= params["EngineConfig.onnxruntime.intra_op_num_threads"] <= 4
    assert params["EngineConfig.onnxruntime.inter_op_num_threads"] == 1
    assert not any(params[f"EngineConfig.onnxruntime.use_{ep}"] for ep in ("cuda", "dml"))
    assert FakeRapidOCR.handed == [("bgr", (40, 20))] * 2


def test_a_model_changed_after_binding_is_refused_before_it_is_loaded(fake_install: Path) -> None:
    pins = _models(fake_install)
    engine = rapid_ocr_engine(pins)
    assert engine is not None
    (fake_install / DET_MODEL).write_bytes(b"det, swapped")
    with pytest.raises(EngineRefused) as refused:
        engine.recognise(Image.new("RGB", (8, 8)))
    assert refused.value.reason == MODEL_REFUSED and FakeRapidOCR.built == []


def test_the_production_binding(fake_install: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(local_ocr_provider(False), NoOcrProvider)
    # The fake install's models stand in for the pinned ones.
    pins = _models(fake_install)
    monkeypatch.setattr(rapid.rapid_ocr_engine, "__defaults__", (pins,))
    provider = local_ocr_provider(True)
    assert isinstance(provider, EngineOcrProvider)
    report = ocr_capability(provider)
    assert report.status is CapabilityStatus.READY and f"engine={ENGINE_ID}" in report.detail
    assert isinstance(local_ocr_provider(False), NoOcrProvider)


# ---------------------------------------------------------------- the result


def test_a_result_maps_to_lines_with_float_scores_and_four_point_polygons() -> None:
    class Score(float):
        """A float subclass, as NumPy's float64 is: the line carries a plain float."""

    class Number:
        """A number that is no float at all, as NumPy's float32 is not."""

        def __init__(self, value: float) -> None:
            self.value = value

        def __float__(self) -> float:
            return self.value

    lines = lines_from(
        Result(
            [_quad(1, 2, 30, 12), [[Number(0.5), 1], [9, 1], [9, 5], [0.5, 5]]],
            ["中文", "かな"],
            [Score(0.97), Number(0.81)],
        )
    )
    assert lines == (
        EngineLine("中文", 0.97, polygon=((1.0, 2.0), (30.0, 2.0), (30.0, 12.0), (1.0, 12.0))),
        EngineLine("かな", 0.81, polygon=((0.5, 1.0), (9.0, 1.0), (9.0, 5.0), (0.5, 5.0))),
    )
    assert all(type(line.confidence) is float for line in lines)
    assert lines_from(Result()) == () and lines_from(None) == ()
    assert lines_from(Result([], [], [])) == ()


@pytest.mark.parametrize(
    "line",
    [
        ([[1, 2], [3, 2], [3, 4]], "中文", 0.9),  # three points
        ([[1, 2], [3, 2], [3, 4], [1, 4], [0, 0]], "中文", 0.9),  # five points
        ([[1, 2, 0], [3, 2, 0], [3, 4, 0], [1, 4, 0]], "中文", 0.9),  # 3-D points
        ([[1, "x"], [3, 2], [3, 4], [1, 4]], "中文", 0.9),  # not a number
        (None, "中文", 0.9),  # no polygon
        (_quad(1, 2, 3, 4), None, 0.9),  # no text
    ],
)
def test_a_malformed_line_is_passed_on_without_geometry(line: tuple[Any, Any, Any]) -> None:
    box, text, score = line
    (mapped,) = lines_from(Result([box], [text], [score]))
    assert (mapped.text, mapped.box, mapped.polygon) == ("", None, None)
    provider = EngineOcrProvider(
        RapidOcrEngine(engine_identity(), lambda tile: Result([box], [text], [score]))
    )
    reading = provider.observe(_png(Image.new("RGB", (40, 20), "white")))
    assert reading.regions == () and [s.reason for s in reading.skipped] == [SKIP_LINE_INVALID]


def test_columns_of_different_lengths_are_refused() -> None:
    with pytest.raises(RuntimeError):
        lines_from(Result([_quad(0, 0, 1, 1)], ["中文", "かな"], [0.9]))


def test_a_score_that_is_no_number_reads_zero() -> None:
    (line,) = lines_from(Result([_quad(0, 0, 4, 4)], ["中文"], ["high"]))
    assert line.confidence == 0.0


# ---------------------------------------------------------------- through the provider


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_the_provider_reads_the_engines_lines_into_regions_and_logs_no_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    image = Image.new("RGB", (300, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 120, 40), fill="black")
    draw.rectangle((20, 70, 160, 90), fill="black")
    tiles: list[tuple[str, tuple[int, int]]] = []

    def reader(tile: Image.Image) -> Result:
        tiles.append((tile.mode, tile.size))
        return Result(
            [_quad(18.4, 18.6, 122.2, 42.0), _quad(18, 68, 162, 92), _quad(200, 5, 290, 15)],
            [SECRET, "天然ごま油", "350ml"],
            [0.99, 0.875, 0.9],
        )

    caplog.set_level(logging.DEBUG)
    reading = EngineOcrProvider(RapidOcrEngine(engine_identity(), reader)).observe(_png(image))
    assert tiles == [("RGB", (300, 120))]
    assert [(r.region_id, r.script, r.box, r.confidence) for r in reading.regions] == [
        ("r0", "zh", (18, 18, 123, 42), 0.99),
        ("r1", "ja", (18, 68, 162, 92), 0.875),
    ]
    assert reading.regions[0].polygon == ((18, 19), (122, 19), (122, 42), (18, 42))
    assert [s.reason for s in reading.skipped] == ["SCRIPT_NOT_CJK"]
    logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert SECRET not in logged and "ごま" not in logged


def test_the_tile_reaches_rapidocr_as_a_bgr_array() -> None:
    numpy = pytest.importorskip("numpy")
    tile = Image.new("RGB", (2, 1))
    tile.putpixel((0, 0), (10, 20, 30))
    tile.putpixel((1, 0), (200, 100, 0))
    array = rapid.to_bgr(tile)
    assert array.dtype == numpy.uint8 and array.shape == (1, 2, 3)
    assert array.flags["C_CONTIGUOUS"]
    assert array.tolist() == [[[30, 20, 10], [0, 100, 200]]]
    assert rapid.to_bgr(tile.convert("L")).shape == (1, 2, 3)
