"""The platform tag task's context and filter (ADR-0028 §4; Canonical v3.1 §7.8).

The composed request is the v29 bundle (``TASK_PRODUCT_RECOMMEND_BUNDLE_V1``) with the result key
``tags``. Its enrichment task id is its own (``SMARTSTORE_TAGS_V1``), because its subject is
targeted at one SmartStore account and its inputs include the platform's candidates, while the
product-name result of the same bundle stays target-free (ADR-0027 §6).

The deterministic pipeline, run by the ``enrich.tasks`` job and never inline:
1. keywords from the product's confirmed facts, at most 3 distinct;
2. the platform's recommended tags for each (``SMARTSTORE_TAG_RECOMMEND``), unioned by text with
   each code kept, at most 60;
3. the SearchSignal port's signals (none until a source is adopted, ADR-0028 §3);
4. the AI's selection from the bundle answer's ``tags`` object, at most 10;
5. the filter: trimmed, de-duplicated, the product name and the brand removed, then the
   restricted-tag check (``SMARTSTORE_TAG_RESTRICTED``): a restricted tag and a tag the check did
   not answer are removed (AIT-03); the check's time is recorded (AIT-04);
6. a platform tag carries the pool's code, a direct-input tag none (AIT-05). Nothing usable is a
   FAILED result, never an empty OK. No tag is ever sent (AIT-01).
"""

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Protocol

from app.capabilities.ai.search_signal import SearchSignalPort
from app.capabilities.ai.task_context import FinishResult, TaskContext

TAG_TASK_KEY: Final = "SMARTSTORE_TAGS_V1"
BUNDLE_PROMPT_KEY: Final = "TASK_PRODUCT_RECOMMEND_BUNDLE_V1"
TAG_RESULT_KEY: Final = "tags"
TAG_SCHEMA_VERSION: Final = "tag-stage-1"
TAG_FACT_FIELDS: Final = (
    "original_name",
    "brand",
    "manufacturer",
    "origin",
    "options",
    "detail_description",
)
KEYWORDS_MAX: Final = 5
WORD_MIN_LENGTH: Final = 2
KEYWORD_MAX_LENGTH: Final = 50
POOL_MAX: Final = 60
RECOMMENDED_MAX: Final = 10
AI_TAGS_NONE_USABLE: Final = "AI_TAGS_NONE_USABLE"

_BRACKETED: Final = re.compile(r"[\[\(\{【〔<][^\]\)\}】〕>]*[\]\)\}】〕>]")
_QUANTITY: Final = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:ml|l|g|kg|mg|cm|mm|m|ea|개입|개월|개|매|팩|입|정|포|병|캔|봉|박스|세트"
    r"|캡슐|알|회분|일분|인분|장|롤|켤레|족|p)"
    r"(?![0-9A-Za-z가-힣])",
    re.IGNORECASE,
)
_SPACES: Final = re.compile(r"\s+")


class TagReader(Protocol):
    """The two SmartStore tag reads (``integrations/marketplaces/smartstore/tags.py``)."""

    def recommend(self, keyword: str) -> Sequence[Any]: ...

    def check(self, tags: tuple[str, ...]) -> dict[str, bool | None]: ...


def confirmed_text(facts: Sequence[Mapping[str, Any]], key: str) -> str | None:
    for member in facts:
        reading = member.get("fields", {}).get(key)
        value = reading.get("value") if isinstance(reading, dict) else None
        if (
            isinstance(reading, dict)
            and reading.get("status") == "CONFIRMED"
            and isinstance(value, dict)
            and isinstance(value.get("text"), str)
            and value["text"].strip()
        ):
            return _SPACES.sub(" ", value["text"]).strip()
    return None


def clean_name(name: str) -> str:
    cleaned = _QUANTITY.sub(" ", _BRACKETED.sub(" ", name))
    return _SPACES.sub(" ", cleaned).strip()


def keywords(facts: Sequence[Mapping[str, Any]]) -> list[str]:
    """At most 5 distinct queries from the confirmed facts: the cleaned name, the brand, then the
    cleaned name's own words (at least 2 characters, not a bare number), longest first; each
    trimmed to 50 characters. No fact, no query.

    ADR-0028 §4 implementation note (R0, Issue #219 6079478595): the platform's recommended-tag
    search answers at most 20 tags per keyword, and a whole product name finds few of them, so
    single words are asked as well."""
    name = confirmed_text(facts, "original_name")
    brand = confirmed_text(facts, "brand")
    cleaned = clean_name(name) if name else ""
    words = sorted(
        (
            word
            for word in dict.fromkeys(cleaned.split())
            if len(word) >= WORD_MIN_LENGTH and not word.replace(",", "").replace(".", "").isdigit()
        ),
        key=len,
        reverse=True,
    )
    candidates = [cleaned, brand or "", *words]
    found: list[str] = []
    for candidate in candidates:
        keyword = candidate[:KEYWORD_MAX_LENGTH].strip()
        if keyword and keyword not in found:
            found.append(keyword)
    return found[:KEYWORDS_MAX]


def _normal(text: str) -> str:
    return _SPACES.sub("", text).casefold()


class SmartStoreTagContext:
    """Gathers the platform candidates and the signals for one product (steps 1–3), and filters
    the AI's selection (steps 5–6)."""

    def __init__(
        self, reader: TagReader, signals: SearchSignalPort, now: Callable[[], datetime]
    ) -> None:
        self._reader = reader
        self._signals = signals
        self._now = now

    def gather(self, facts: Sequence[Mapping[str, Any]], target: Any) -> TaskContext:
        asked = keywords(facts)
        pool: dict[str, int | None] = {}
        for keyword in asked:
            for tag in self._reader.recommend(keyword):
                if tag.text not in pool and len(pool) < POOL_MAX:
                    pool[tag.text] = tag.code
        signals = self._signals.signals(asked)
        candidates = [{"text": text, "code": code} for text, code in pool.items()]
        return TaskContext(
            runtime={
                "tag_keywords": asked,
                "platform_tag_candidates": candidates,
                "search_signals": signals.as_input(),
            },
            inputs={
                "platform": {"keywords": asked, "candidates": candidates},
                "signals": signals.as_input(),
            },
            facts=facts,
        )

    def finish(self, part: Mapping[str, Any], context: TaskContext) -> FinishResult:
        chosen = part.get("recommended")
        if not isinstance(chosen, list):
            return FinishResult(None, "AI_OUTPUT_SCHEMA_INVALID")
        pool = {
            str(item["text"]): item.get("code")
            for item in context.runtime["platform_tag_candidates"]
        }
        excluded = {
            _normal(text)
            for text in (
                confirmed_text(context.facts, "original_name"),
                confirmed_text(context.facts, "brand"),
            )
            if text
        }
        removed: list[dict[str, str]] = []
        texts: list[str] = []
        for item in chosen:
            text = item.get("text") if isinstance(item, dict) else item
            if not isinstance(text, str):
                return FinishResult(None, "AI_OUTPUT_SCHEMA_INVALID")
            text = _SPACES.sub(" ", text).strip()
            if not text or text in texts:
                continue
            if _normal(text) in excluded:
                removed.append({"text": text, "reason": "name_or_brand"})
            elif len(text) > KEYWORD_MAX_LENGTH:
                removed.append({"text": text, "reason": "too_long"})
            elif len(texts) >= RECOMMENDED_MAX:
                removed.append({"text": text, "reason": "over_limit"})
            else:
                texts.append(text)
        checked_at = self._now()
        answers = self._reader.check(tuple(texts)) if texts else {}
        kept: list[dict[str, Any]] = []
        for text in texts:
            answer = answers.get(text)
            if answer is True:
                removed.append({"text": text, "reason": "restricted"})
            elif answer is None:
                removed.append({"text": text, "reason": "not_checked"})
            else:
                platform = text in pool
                kept.append(
                    {
                        "text": text,
                        "code": pool[text] if platform else None,
                        "source": "PLATFORM" if platform else "AI_DIRECT",
                    }
                )
        if not kept:
            return FinishResult(None, AI_TAGS_NONE_USABLE)
        finished = {key: value for key, value in part.items() if key != "recommended"}
        finished["recommended"] = kept
        finished["removed"] = removed
        finished["restricted_checked_at"] = checked_at.isoformat()
        finished["requires_review"] = bool(
            part.get("requires_review")
            or removed
            or any(tag["source"] == "AI_DIRECT" for tag in kept)
        )
        return FinishResult(finished, None)


__all__ = [
    "AI_TAGS_NONE_USABLE",
    "BUNDLE_PROMPT_KEY",
    "TAG_FACT_FIELDS",
    "TAG_RESULT_KEY",
    "TAG_SCHEMA_VERSION",
    "TAG_TASK_KEY",
    "SmartStoreTagContext",
    "TagReader",
    "keywords",
]
