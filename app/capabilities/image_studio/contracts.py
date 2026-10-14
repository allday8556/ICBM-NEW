"""The Image Studio translation contract (ADR-0041 §3): regions, the OCR seam, the translation
seam and the fingerprints.

**Observation.** One OCR reading of one source image is keyed by the source SHA-256, the OCR
engine id and version and the preprocessing version (``ObservationKey``). Its fingerprint is the
digest of that key and its regions, by value.

**Translation fingerprint.** It is ADR-0012 §6 / ADR-0026's, unchanged, computed before the call:

    hash(relevant_input + policy_version + prompt_version + requested_provider + requested_model
         + routing_config_version + proxy_version + output_schema_version)

``relevant_input`` is the digest of exactly what the task reads: the observation's fingerprint,
the source language and the ordered regions that are sent (each its id and its text; a region left
out is not part of it). A candidate is reused only on an equal fingerprint; any other is
``STALE``, derived on read with the parts that changed (``REASONS``). No secret is ever part of
it (AIF-07).

**Seams.** The OCR engine (``OcrProvider``) and the translation task's composition
(``TranslationComposer``) are ports. Production binds ``NoOcrProvider``, which names no engine, so
the capability reads ``NOT_CONFIGURED`` and nothing is read or sent. The full OCR port is ADR-0041
I2's; the registered task and its composer are I4's (``task``).

**What is sent.** A region the operator excludes is never sent, and a contact detail
(``checks.looks_like_contact``) only when the operator includes it (§6).
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final, Protocol

from app.capabilities.ai.composer import ComposedRequest
from app.capabilities.ai.provider import RequestedIdentity
from app.capabilities.image_studio.checks import looks_like_contact
from app.platform.core.errors import InputValidationError, PolicyBlockedError

# The output schema of the translation task: one Korean string per sent region (ADR-0041 §9).
OUTPUT_SCHEMA_VERSION: Final = "image-text-translate/v1"
# D5: one run translates at most 30 images.
MAX_IMAGES_PER_RUN: Final = 30
LANGUAGES: Final = ("zh", "ja")
# The parts of a translation fingerprint's inputs, in the order a stale reason names them.
REASONS: Final = ("input", "prompt", "policy", "provider", "schema")

IMAGE_OCR_NOT_CONFIGURED: Final = "IMAGE_OCR_NOT_CONFIGURED"
IMAGE_OCR_REGION_INVALID: Final = "IMAGE_OCR_REGION_INVALID"
IMAGE_LANGUAGE_UNKNOWN: Final = "IMAGE_LANGUAGE_UNKNOWN"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ObservationKey:
    """What makes two OCR readings the same reading: the image and how it was read."""

    source_sha256: str
    engine_id: str
    engine_version: str
    preprocessing_version: str


@dataclass(frozen=True)
class OcrIdentity:
    engine_id: str
    engine_version: str
    preprocessing_version: str

    def key(self, source_sha256: str) -> ObservationKey:
        return ObservationKey(
            source_sha256, self.engine_id, self.engine_version, self.preprocessing_version
        )


@dataclass(frozen=True)
class OcrRegion:
    """One recognised text region: its box (``left, top, right, bottom``) and optional polygon,
    its text, its script, the engine's confidence and the background-uniformity score."""

    region_id: str
    box: tuple[int, int, int, int]
    text: str
    script: str
    confidence: float
    background_uniformity: float
    polygon: tuple[tuple[int, int], ...] | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "box": list(self.box),
            "polygon": None if self.polygon is None else [list(p) for p in self.polygon],
            "text": self.text,
            "script": self.script,
            "confidence": self.confidence,
            "background_uniformity": self.background_uniformity,
        }


def region_from(raw: Mapping[str, Any]) -> OcrRegion:
    polygon = raw.get("polygon")
    return OcrRegion(
        region_id=raw["region_id"],
        box=(raw["box"][0], raw["box"][1], raw["box"][2], raw["box"][3]),
        text=raw["text"],
        script=raw["script"],
        confidence=raw["confidence"],
        background_uniformity=raw["background_uniformity"],
        polygon=None if polygon is None else tuple((p[0], p[1]) for p in polygon),
    )


def _unit(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _point(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    )


def checked_regions(regions: Sequence[OcrRegion]) -> tuple[OcrRegion, ...]:
    """The engine's regions, refused whole when any is malformed: a distinct non-empty id, a box
    with ``left <= right`` and ``top <= bottom``, a ``zh`` or ``ja`` script and both scores in
    0..1."""
    seen: set[str] = set()
    for region in regions:
        box = region.box
        problems = [
            name
            for name, ok in (
                ("region_id", isinstance(region.region_id, str) and region.region_id != ""),
                (
                    "box",
                    len(box) == 4
                    and all(isinstance(v, int) and not isinstance(v, bool) for v in box)
                    and box[0] <= box[2]
                    and box[1] <= box[3],
                ),
                (
                    "polygon",
                    region.polygon is None or all(_point(p) for p in region.polygon),
                ),
                ("text", isinstance(region.text, str)),
                ("script", region.script in LANGUAGES),
                ("confidence", _unit(region.confidence)),
                ("background_uniformity", _unit(region.background_uniformity)),
            )
            if not ok
        ]
        if region.region_id in seen:
            problems.append("region_id_repeated")
        if problems:
            raise InputValidationError(
                IMAGE_OCR_REGION_INVALID,
                "an OCR region is malformed",
                details={"problems": problems},
            )
        seen.add(region.region_id)
    return tuple(regions)


def observation_fingerprint(key: ObservationKey, regions: Sequence[OcrRegion]) -> str:
    return digest({**asdict(key), "regions": [r.canonical() for r in regions]})


@dataclass(frozen=True)
class SentRegion:
    region_id: str
    text: str


def sent_regions(
    regions: Sequence[OcrRegion],
    excluded: frozenset[str] = frozenset(),
    included: frozenset[str] = frozenset(),
) -> tuple[SentRegion, ...]:
    """The regions a translation sends, in observation order: every region with text that is not
    excluded. A region the operator keeps is never sent (ADR-0041 §3), and a contact detail is
    kept by default: it is sent only when the operator includes it (§6)."""
    return tuple(
        SentRegion(r.region_id, r.text)
        for r in regions
        if r.region_id not in excluded
        and r.text.strip()
        and (r.region_id in included or not looks_like_contact(r.text))
    )


def relevant_input(observation: str, source_language: str, regions: Sequence[SentRegion]) -> str:
    """The digest, by value, of exactly what the translation task reads."""
    return digest(
        {
            "observation": observation,
            "source_language": source_language,
            "regions": [{"region_id": r.region_id, "text": r.text} for r in regions],
        }
    )


@dataclass(frozen=True)
class TranslationInputs:
    """Every part of a translation fingerprint, before the call."""

    relevant_input: str
    prompt_version: Mapping[str, str]
    policy_version: Mapping[str, str]
    identity: RequestedIdentity
    output_schema_version: str

    def parts(self) -> dict[str, Any]:
        return {
            "input": self.relevant_input,
            "prompt_version": dict(self.prompt_version),
            "policy_version": dict(self.policy_version),
            "provider": asdict(self.identity),
            "schema": self.output_schema_version,
        }

    @property
    def fingerprint(self) -> str:
        return digest(self.parts())


def stale_reasons(stored: TranslationInputs, now: TranslationInputs) -> tuple[str, ...]:
    """The parts that differ, by name: input, prompt, policy, provider (the requested provider,
    model, routing or proxy revision) or schema."""
    before, after = stored.parts(), now.parts()
    return tuple(
        reason
        for reason, part in zip(
            REASONS,
            ("input", "prompt_version", "policy_version", "provider", "schema"),
            strict=True,
        )
        if before[part] != after[part]
    )


def check_language(source_language: str) -> str:
    if source_language not in LANGUAGES:
        raise InputValidationError(
            IMAGE_LANGUAGE_UNKNOWN,
            "the source language is Chinese (zh) or Japanese (ja)",
            details={"source_language": source_language},
        )
    return source_language


class OcrProvider(Protocol):
    """The local OCR engine seam (ADR-0041 D1, D2). Only its minimal shape lands with I1; the
    port, its region model and its scoring are I2's."""

    def identity(self) -> OcrIdentity | None:
        """The engine a reading would use, or ``None`` when no engine is installed."""
        ...

    def read(self, image: bytes) -> Sequence[OcrRegion]: ...


class NoOcrProvider:
    """The production binding until an engine is installed (D2, I7): nothing is ever read."""

    def identity(self) -> OcrIdentity | None:
        return None

    def read(self, image: bytes) -> Sequence[OcrRegion]:
        raise PolicyBlockedError(IMAGE_OCR_NOT_CONFIGURED, "no OCR engine is installed")


class TranslationComposer(Protocol):
    """Composes the translation task's request for the regions sent (ADR-0041 §9): text only.
    The registered task's composer is ``task.RegistryTranslationComposer`` (I4)."""

    def compose(self, source_language: str, regions: Sequence[SentRegion]) -> ComposedRequest: ...
