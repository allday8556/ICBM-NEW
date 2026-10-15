"""The local OCR engine: RapidOCR on onnxruntime (ADR-0041 D2, slice I7).

``rapid_ocr_engine()`` returns the engine only when all of this holds, and ``None`` otherwise (the
container then keeps ``NoOcrProvider`` and ``image_ocr`` reads NOT_CONFIGURED):

1. ``rapidocr`` and ``onnxruntime`` import. They are the optional ``ocr`` extra
   (``pyproject.toml``, exact pins in ``constraints.txt``) and are imported only here, inside the
   functions below, so the application imports and runs without them, as CI does.
2. They are exactly the pinned versions (``RAPIDOCR_VERSION``, ``ONNXRUNTIME_VERSION``), read from
   the imported packages themselves (``rapidocr._version.__version__``,
   ``onnxruntime.__version__``; the repository forbids ``importlib`` in production code). Any
   other version is refused, never adapted to.
3. The three model files that the rapidocr wheel bundles in ``rapidocr/models/`` exist and hash
   to the pinned SHA-256s (``MODEL_SHA256S``, the same values as the package's own
   ``default_models.yaml``): PP-OCRv6 small detection, PP-OCRv6 small multilingual recognition
   (it reads Chinese, simplified and traditional, and Japanese, kana included: no separate
   Japanese model is needed) and the PP-OCR mobile text-direction classifier. A missing or
   changed file is refused.

**It can never download.** RapidOCR fetches a model only when its configuration names none
(``model_path`` unset), a recognition dictionary only when the recognition model carries none,
and a font only to draw a visualisation. Here every model is given by its verified local path,
the PP-OCRv6 recognition model carries its dictionary, and nothing is visualised. The models are
hashed again right before RapidOCR loads them, which happens once, on the first ``recognise``
(the service starts without loading onnxruntime sessions).

**Input.** ``recognise`` takes the RGB tile the provider prepared (``ocr.EngineOcrProvider``) and
hands RapidOCR a NumPy array in **BGR** channel order, which is what RapidOCR (OpenCV) works in:
an ``ndarray`` is used as given, so the channels are reversed here, explicitly, rather than left to
RapidOCR's own PIL conversion.

**Determinism.** The engine's settings are pinned here, not taken from RapidOCR's defaults: the
text-score threshold, and onnxruntime's thread counts (``OCR_THREADS`` intra-op threads, capped by
the machine's CPUs, and one inter-op thread). The CPU execution provider is the only one enabled.
Calls are serialised: one RapidOCR instance reads one tile at a time.

**Output.** Each recognised line becomes an ``EngineLine`` with its text, its score as a float and
its four-point polygon in the tile's pixel grid. Logs carry counts and reasons only, never a
recognised text (§8).
"""

import hashlib
import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from PIL import Image

from app.capabilities.image_studio.contracts import NoOcrProvider, OcrProvider
from app.capabilities.image_studio.ocr import EngineIdentity, EngineLine, EngineOcrProvider

logger = logging.getLogger("icbm.image_studio")

ENGINE_ID: Final = "rapidocr"
RAPIDOCR_VERSION: Final = "3.9.2"
ONNXRUNTIME_VERSION: Final = "1.30.0"
ENGINE_VERSION: Final = f"{RAPIDOCR_VERSION}+onnxruntime-{ONNXRUNTIME_VERSION}"

DET_MODEL: Final = "PP-OCRv6_det_small.onnx"
REC_MODEL: Final = "PP-OCRv6_rec_small.onnx"
CLS_MODEL: Final = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
# The models bundled in the rapidocr 3.9.2 wheel, as its default_models.yaml pins them.
MODEL_SHA256S: Final[Mapping[str, str]] = {
    DET_MODEL: "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    REC_MODEL: "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    CLS_MODEL: "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
}
# RapidOCR's section of each model's path in its configuration.
MODEL_SECTIONS: Final = {"Det": DET_MODEL, "Rec": REC_MODEL, "Cls": CLS_MODEL}

# Pinned engine settings (see the module's Determinism).
TEXT_SCORE: Final = 0.5
OCR_THREADS: Final = 4
LOG_LEVEL: Final = "warning"

# Why the engine is not bound (logged, never shown as an error: the capability reads
# NOT_CONFIGURED).
NOT_INSTALLED: Final = "OCR_ENGINE_NOT_INSTALLED"
VERSION_REFUSED: Final = "OCR_ENGINE_VERSION_REFUSED"
MODEL_MISSING: Final = "OCR_MODEL_MISSING"
MODEL_REFUSED: Final = "OCR_MODEL_SHA256_MISMATCH"


class EngineRefused(RuntimeError):
    """The installed engine is not the pinned one; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def check_versions(rapidocr_version: object, onnxruntime_version: object) -> None:
    """Refuse any version other than the pinned ones."""
    if (rapidocr_version, onnxruntime_version) != (RAPIDOCR_VERSION, ONNXRUNTIME_VERSION):
        raise EngineRefused(
            VERSION_REFUSED,
            f"the OCR engine is pinned to rapidocr {RAPIDOCR_VERSION} on onnxruntime "
            f"{ONNXRUNTIME_VERSION}",
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_models(models_dir: Path, pins: Mapping[str, str] = MODEL_SHA256S) -> dict[str, Path]:
    """Each pinned model's path in ``models_dir``, refused when any is missing or differs."""
    paths: dict[str, Path] = {}
    for name, sha256 in sorted(pins.items()):
        path = models_dir / name
        if not path.is_file():
            raise EngineRefused(MODEL_MISSING, f"the pinned OCR model {name} is missing")
        if _sha256(path) != sha256:
            raise EngineRefused(MODEL_REFUSED, f"the OCR model {name} is not the pinned file")
        paths[name] = path
    return paths


def to_bgr(tile: Image.Image) -> Any:
    """The RGB tile as the contiguous ``uint8`` BGR array RapidOCR reads."""
    import numpy

    rgb = numpy.asarray(tile.convert("RGB"), dtype=numpy.uint8)
    return numpy.ascontiguousarray(rgb[:, :, ::-1])


def _point(value: Any) -> tuple[float, float] | None:
    try:
        if len(value) != 2:
            return None
        return (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None


def lines_from(result: Any) -> tuple[EngineLine, ...]:
    """RapidOCR's result as engine lines: text, score and the 4-point polygon of each line.

    An empty result (no text found) is ``()``. A line whose text is not a string or whose polygon
    is not four points is passed on without geometry, so the provider skips it (``LINE_INVALID``).
    Result columns of different lengths are a malformed answer and are refused."""
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is None or texts is None or scores is None:
        return ()
    boxes, texts, scores = list(boxes), list(texts), list(scores)
    if not len(boxes) == len(texts) == len(scores):
        raise RuntimeError("the OCR engine answered columns of different lengths")
    lines: list[EngineLine] = []
    for box, text, score in zip(boxes, texts, scores, strict=True):
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            points = [_point(p) for p in box]
        except TypeError:
            points = []
        polygon = tuple(p for p in points if p is not None)
        if not isinstance(text, str) or len(points) != 4 or len(polygon) != 4:
            lines.append(EngineLine("", confidence))
            continue
        lines.append(EngineLine(text, confidence, polygon=polygon))
    return tuple(lines)


Reader = Callable[[Image.Image], Any]


def rapidocr_params(models: Mapping[str, Path]) -> dict[str, Any]:
    """RapidOCR's configuration: every model by its local path, the pinned settings."""
    threads = max(1, min(OCR_THREADS, os.cpu_count() or 1))
    params: dict[str, Any] = {
        "Global.log_level": LOG_LEVEL,
        "Global.text_score": TEXT_SCORE,
        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.use_cuda": False,
        "EngineConfig.onnxruntime.use_dml": False,
        "EngineConfig.onnxruntime.use_cann": False,
        "EngineConfig.onnxruntime.use_coreml": False,
    }
    for section, name in MODEL_SECTIONS.items():
        params[f"{section}.model_path"] = str(models[name])
    return params


class RapidReader:
    """Reads one tile with RapidOCR, constructed once on first use from the verified models."""

    def __init__(self, models_dir: Path, pins: Mapping[str, str] = MODEL_SHA256S) -> None:
        self._models_dir = models_dir
        self._pins = dict(pins)
        self._lock = threading.Lock()
        self._engine: Any = None

    def _construct(self) -> Any:
        from rapidocr import RapidOCR

        # Hashed again right before loading: the file read is the file verified.
        models = verified_models(self._models_dir, self._pins)
        engine = RapidOCR(params=rapidocr_params(models))
        logger.info("image_ocr.engine_loaded", extra={"engine_id": ENGINE_ID})
        return engine

    def __call__(self, tile: Image.Image) -> Any:
        with self._lock:
            if self._engine is None:
                self._engine = self._construct()
            return self._engine(to_bgr(tile))


class RapidOcrEngine:
    """The ``ocr.OcrEngine`` over RapidOCR: its identity and the lines of one tile."""

    def __init__(self, identity: EngineIdentity, reader: Reader) -> None:
        self._identity = identity
        self._reader = reader

    def identity(self) -> EngineIdentity | None:
        return self._identity

    def recognise(self, tile: Image.Image) -> Sequence[EngineLine]:
        lines = lines_from(self._reader(tile))
        logger.debug("image_ocr.tile", extra={"engine_id": ENGINE_ID, "lines": len(lines)})
        return lines


def engine_identity(pins: Mapping[str, str] = MODEL_SHA256S) -> EngineIdentity:
    return EngineIdentity(ENGINE_ID, ENGINE_VERSION, tuple(sorted(pins.values())))


def rapid_ocr_engine(pins: Mapping[str, str] = MODEL_SHA256S) -> RapidOcrEngine | None:
    """The pinned engine when it is installed and verified, else ``None`` (module docstring)."""
    try:
        import onnxruntime
        import rapidocr
        from rapidocr import _version as rapidocr_version
    except (ImportError, OSError):
        logger.info("image_ocr.engine_unavailable", extra={"reason": NOT_INSTALLED})
        return None
    try:
        check_versions(
            getattr(rapidocr_version, "__version__", None),
            getattr(onnxruntime, "__version__", None),
        )
        models_dir = Path(rapidocr.__file__).parent / "models"
        verified_models(models_dir, pins)
    except EngineRefused as refused:
        logger.warning("image_ocr.engine_refused", extra={"reason": refused.reason})
        return None
    return RapidOcrEngine(engine_identity(pins), RapidReader(models_dir, pins))


def local_ocr_provider(enabled: bool) -> OcrProvider:
    """The OCR port's production binding: the pinned local engine when ``enabled`` and verified,
    otherwise ``NoOcrProvider`` (``image_ocr`` NOT_CONFIGURED)."""
    engine = rapid_ocr_engine() if enabled else None
    return NoOcrProvider() if engine is None else EngineOcrProvider(engine)
