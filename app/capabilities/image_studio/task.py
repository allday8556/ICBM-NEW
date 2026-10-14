"""The image-text translation task (ADR-0041 §9, the ADR-0026 §3.1 amendment; slice I4).

**The task.** ``TASK_IMAGE_TEXT_TRANSLATE_V1`` is a registry task (``registry.CATALOG``) with its
seed in ``seed_v29.json``, read like every other task from the PromptTemplate and PlatformPolicy
stores at their current revisions. It composes with ``ROLE_PRODUCT_MD_V1`` and the common market
policy: translation has no marketplace. Its input is the recognised text of one image's sent
regions and the source language; its output is one Korean string per sent region
(``OUTPUT_SCHEMA_VERSION``). It stays a suggestion (AIS-08, IT-06).

**Text only (IT-07).** ``RegistryTranslationComposer`` composes the request through the
registry's ``PromptComposer``: the runtime data is the source language and each sent region's id
and text, nothing else. The provider port carries text (``TaskRequest.text``); no image byte ever
reaches it. The data-transfer approval and the daily call cap are the profile's (ADR-0027 §2,
AIS-04): without the approval the provider names no identity, so the capability reads
``NOT_CONFIGURED`` and nothing is called; at the cap the call is refused before anything is sent.

**The answer.** ``parse_translations`` accepts exactly ``{"translations": [{"region_id", "text"}]}``
naming every sent region once and no other, each text non-empty and at most ``MAX_KOREAN_CHARS``.
Anything else refuses the whole answer, and the item fails with nothing durable (IT-02).
"""

from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.capabilities.ai.composer import ComposedRequest, PromptComposer
from app.capabilities.ai.registry import COMMON_POLICY_KEY
from app.capabilities.image_studio.contracts import SentRegion, check_language
from app.platform.core.errors import InputValidationError

TASK_KEY: Final = "TASK_IMAGE_TEXT_TRANSLATE_V1"
POLICY_KEY: Final = COMMON_POLICY_KEY
# The longest Korean text one region may receive: an image line, never a paragraph.
MAX_KOREAN_CHARS: Final = 500

IMAGE_TRANSLATION_OUTPUT_INVALID: Final = "IMAGE_TRANSLATION_OUTPUT_INVALID"
IMAGE_TRANSLATION_NOTHING_SENT: Final = "IMAGE_TRANSLATION_NOTHING_SENT"


class RegistryTranslationComposer:
    """The ``TranslationComposer`` of the registered task. It never calls a provider."""

    def __init__(self, composer: PromptComposer) -> None:
        self._composer = composer

    def compose(self, source_language: str, regions: Sequence[SentRegion]) -> ComposedRequest:
        check_language(source_language)
        if not regions:
            raise InputValidationError(
                IMAGE_TRANSLATION_NOTHING_SENT, "a translation sends at least one region"
            )
        return self._composer.compose(
            TASK_KEY,
            {
                "source_language": source_language,
                "regions": [{"region_id": r.region_id, "text": r.text} for r in regions],
            },
            policy_key=POLICY_KEY,
        )


def parse_translations(
    value: Mapping[str, Any] | None, sent: Sequence[SentRegion]
) -> tuple[tuple[str, str], ...]:
    """The answer's one Korean string per sent region, in the order sent, or refused whole."""
    if value is None or set(value) != {"translations"}:
        raise _invalid("the answer holds exactly one key, translations")
    raw = value["translations"]
    if not isinstance(raw, list) or len(raw) != len(sent):
        raise _invalid("one translation per sent region")
    texts: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"region_id", "text"}:
            raise _invalid("each translation holds exactly region_id and text")
        region_id, text = entry["region_id"], entry["text"]
        if not isinstance(region_id, str) or region_id in texts:
            raise _invalid("each sent region is named once")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_KOREAN_CHARS:
            raise _invalid(f"each text is non-empty and at most {MAX_KOREAN_CHARS} characters")
        texts[region_id] = text
    if set(texts) != {r.region_id for r in sent}:
        raise _invalid("the translations name exactly the sent regions")
    return tuple((r.region_id, texts[r.region_id]) for r in sent)


def _invalid(problem: str) -> InputValidationError:
    return InputValidationError(
        IMAGE_TRANSLATION_OUTPUT_INVALID,
        "the translation does not carry one Korean text per sent region",
        details={"problem": problem},
    )
