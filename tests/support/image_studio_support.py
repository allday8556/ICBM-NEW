"""Synthetic Image Studio fixtures for the I5 suites (ADR-0041 §6, §7).

Everything is invented: a supplier's detail image drawn with Pillow (four lines of "text" on a
flat white background and one on a noisy patch), the OCR engine's reading of it and the AI's
Korean answers. Nothing is read or sent: the served application keeps its production bindings
(``NoOcrProvider``, no approved AI profile) until ``make_ready`` swaps in the fake OCR engine and
answers its own profiled provider through the AIS-1 fakes (a fake serving process and a fake
sidecar completion), exactly as the AIS-2 UI suite does.
"""

import io
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from app.capabilities.image_studio.contracts import OcrIdentity, OcrRegion
from app.config import AppConfig
from app.container import Container
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from app.stages.products.image_model import (
    ImageAssetKind,
    OutputRole,
    QaVerdict,
    SelectedOutput,
    SourceDecision,
    SourceDecisionKind,
)
from app.stages.products.materialization import MaterializationStatus
from integrations.ai import cliproxyapi
from tests.integration.ai.test_ais1_provider_profile import FakeProbe, _approve_all, _serving
from tests.support.jobs_support import FakeClock
from tests.support.product_support import Collections, FakeDecoder, context

OPERATOR = "operator"
OCR = OcrIdentity("fake-ocr", "1.0.0", "pre-1")
WIDTH, HEIGHT = 400, 260
INK = (34, 34, 34)
# The noisy patch r4 sits on: no erase is ever clean there (§4).
NOISE = (250, 10, 400, 130)
REGIONS = (
    OcrRegion("r0", (20, 20, 240, 60), "纯天然芝麻油", "zh", 0.97, 1.0),
    OcrRegion("r1", (20, 80, 240, 120), "净含量350ml", "zh", 0.95, 1.0),
    OcrRegion("r2", (20, 140, 240, 180), "微信:shop8888", "zh", 0.93, 1.0),
    OcrRegion("r3", (20, 200, 240, 240), "厂家直销", "zh", 0.42, 1.0),
    OcrRegion("r4", (260, 20, 390, 120), "天然ごま油", "ja", 0.91, 0.30),
)
KOREAN = {
    "纯天然芝麻油": "순수 천연 참기름",
    "净含量350ml": "용량 350ml",
    "微信:shop8888": "위챗 shop8888",
    "厂家直销": "공장 직판",
    "天然ごま油": "천연 참기름",
}
# Every OCR and Korean text of the scene: none may reach an audit row, a log or an error.
TEXTS = (*KOREAN, *KOREAN.values())
RULE = "-" * 40


def scene() -> bytes:
    """The detail image: dark bars standing for glyphs in each flat region, and a noisy patch."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    noise = random.Random(41)
    for x in range(NOISE[0], NOISE[2]):
        for y in range(NOISE[1], NOISE[3]):
            image.putpixel((x, y), tuple(noise.randrange(40, 230) for _ in range(3)))
    for region in REGIONS[:4]:
        left, top, right, bottom = region.box
        for x in range(left + 10, right - 30, 26):
            draw.rectangle((x, top + 10, x + 17, bottom - 11), fill=INK)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def runtime(text: str) -> dict[str, Any]:
    """The runtime data of one composed request: the last section of the prototype's layout."""
    data: dict[str, Any] = json.loads(text.rsplit(RULE + "\n", 1)[1])
    return data


@dataclass
class FakeSidecar:
    """The sidecar completion: it records the text it receives and answers one Korean text per
    region it was sent."""

    korean: dict[str, str] = field(default_factory=lambda: dict(KOREAN))
    texts: list[str] = field(default_factory=list)

    def __call__(
        self, endpoint: str, key: str, model: str, text: str, verify: Any = None
    ) -> cliproxyapi.SidecarAnswer:
        assert verify is None or verify(50000)
        self.texts.append(text)
        regions = runtime(text)["regions"]
        return cliproxyapi.SidecarAnswer(
            value={
                "translations": [
                    {"region_id": r["region_id"], "text": self.korean[r["text"]]} for r in regions
                ]
            },
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


@dataclass(frozen=True)
class StudioProduct:
    item_id: str
    group: str
    revision_id: str
    representative: str
    detail: str


def studio_product(
    container: Container, config: AppConfig, source_product_id: str = "5105"
) -> StudioProduct:
    """One Item with a representative and a detail image (the scene), both selected by the
    operator and both with a PASS for their exact binary."""
    assets = SourceAssetStore(config.source_assets_dir, container.db, FakeDecoder(), FakeClock())
    detail = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=1,
        host="img.shop.example",
        provenance=".detail img:nth-of-type(1)",
        status=FieldStatus.CONFIRMED,
        sha256=assets.put(scene()).sha256,
    )
    run_id, revision = Collections.of(container, config).collect(
        source_product_id=source_product_id, extra_images=(detail,)
    )
    result = container.materializer.materialize_run(run_id)
    assert result.status is MaterializationStatus.MATERIALIZED, result
    assert result.item_id is not None and result.product_group_id is not None
    representative = revision.images[0]
    container.images.record_operator_selection(
        result.item_id,
        source_revision_id=revision.revision_id,
        decisions=[
            SourceDecision(image.role, image.ordinal, image.sha256, SourceDecisionKind.USE_SOURCE)
            for image in revision.images
        ],
        outputs=[
            SelectedOutput(OutputRole.REPRESENTATIVE, representative.role, representative.ordinal),
            SelectedOutput(OutputRole.DETAIL, ImageRole.DETAIL, 1),
        ],
        decided_by=OPERATOR,
    )
    for image in revision.images:
        container.images.record_qa(
            asset_kind=ImageAssetKind.SOURCE_ASSET,
            sha256=str(image.sha256),
            derivation_id=None,
            validated_source_revision_id=revision.revision_id,
            verdict=QaVerdict.PASS,
            decided_by="qa-1",
        )
    return StudioProduct(
        result.item_id,
        str(result.product_group_id),
        revision.revision_id,
        str(representative.sha256),
        str(detail.sha256),
    )


def priced(container: Container, item_id: str) -> str:
    """The Item's pricing snapshot for the synthetic ``context()``."""
    snapshot = container.pricing.price(item_id, context()).snapshot
    assert snapshot is not None
    return snapshot.pricing_snapshot_id


def make_ready(container: Container, tmp_path: Path) -> tuple[FakeOcr, FakeSidecar]:
    """The served application's capability READY: the fake OCR engine in place of
    ``NoOcrProvider`` and its own profiled provider answered by the AIS-1 fakes, approved."""
    ocr = FakeOcr()
    container.image_translation._ocr = ocr  # type: ignore[attr-defined]
    provider = container.ai_provider._provider  # type: ignore[attr-defined]
    provider._probe = FakeProbe(_serving(tmp_path))
    sidecar = FakeSidecar()
    provider._complete = sidecar
    _approve_all(container.ai_provider)
    container.prompt_registry.seed_on_startup()
    return ocr, sidecar
