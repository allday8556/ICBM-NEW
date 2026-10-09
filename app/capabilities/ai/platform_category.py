"""The SmartStore category task's candidates and filter (ADR-0029 §1–§3).

The composed request is the v29 bundle (``TASK_PRODUCT_RECOMMEND_BUNDLE_V1``) with the result key
``category``, under its own enrichment id ``SMARTSTORE_CATEGORY_V1`` (as the tag task, ADR-0028).

- **Candidates (AIC-01).** They come from ICBM's durable official leaf-category catalog snapshot
  only; the task reads no provider. The terms are the cleaned confirmed name's words without the
  brand's, each at least 2 characters; each leaf is scored by the terms found in its whole name or
  name, plus one when its own name is found within a term (a Korean compound noun: 들기름 in
  생들기름), case-folded with spaces ignored; the top 30 above zero are the candidates.
- **No catalog, no candidate (AIC-03).** The task fails before any AI call.
- **The filter (AIC-02).** The chosen id is one of the candidates and still a current leaf of the
  same taxonomy revision when the job records it; anything else fails, so an invented id is never
  an OK result.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol

from app.capabilities.ai.platform_tags import clean_name, confirmed_text
from app.capabilities.ai.task_context import FinishResult, TaskContext
from app.platform.core.errors import PolicyBlockedError

CATEGORY_TASK_KEY: Final = "SMARTSTORE_CATEGORY_V1"
CATEGORY_RESULT_KEY: Final = "category"
CATEGORY_SCHEMA_VERSION: Final = "category-stage-1"
CATEGORY_FACT_FIELDS: Final = (
    "original_name",
    "brand",
    "manufacturer",
    "origin",
    "options",
    "detail_description",
)
CANDIDATES_MAX: Final = 30
TERM_MIN_LENGTH: Final = 2
REVIEW_BELOW: Final = 0.7
AI_CATEGORY_CATALOG_MISSING: Final = "AI_CATEGORY_CATALOG_MISSING"
AI_CATEGORY_NO_CANDIDATES: Final = "AI_CATEGORY_NO_CANDIDATES"
AI_CATEGORY_NOT_A_CANDIDATE: Final = "AI_CATEGORY_NOT_A_CANDIDATE"


class CatalogSource(Protocol):
    """The durable catalog owner (``CategoryCatalogStore``): its current snapshot, or ``None``."""

    def current(self, marketplace_key: str = ...) -> Any: ...


def _fold(text: str) -> str:
    return "".join(text.split()).casefold()


def terms(facts: Sequence[Mapping[str, Any]]) -> list[str]:
    """The cleaned confirmed name's words, without the brand's, each at least 2 characters."""
    name = confirmed_text(facts, "original_name")
    if not name:
        return []
    brand = confirmed_text(facts, "brand") or ""
    brand_words = {_fold(word) for word in brand.split()}
    found: list[str] = []
    for word in clean_name(name).split():
        folded = _fold(word)
        if len(folded) >= TERM_MIN_LENGTH and folded not in brand_words and folded not in found:
            found.append(folded)
    return found


def candidates(entries: Sequence[Any], asked: Sequence[str]) -> list[dict[str, str]]:
    """The top leaves by matched terms, then the deepest whole name, then the id."""
    scored = []
    for entry in entries:
        if not entry.leaf:
            continue
        haystack = _fold(entry.whole_category_name) + "|" + _fold(entry.name)
        own = _fold(entry.name)
        # A term found in the leaf's names, or the leaf's own name found within a term: Korean
        # compound nouns put the category inside the product word (들기름 in 생들기름).
        score = sum(1 for term in asked if term in haystack) + (
            1 if len(own) >= TERM_MIN_LENGTH and any(own in term for term in asked) else 0
        )
        if score:
            depth = entry.whole_category_name.count(">")
            scored.append((-score, -depth, entry.category_id, entry))
    scored.sort(key=lambda row: row[:3])
    return [
        {"category_id": entry.category_id, "whole_category_name": entry.whole_category_name}
        for *_, entry in scored[:CANDIDATES_MAX]
    ]


class SmartStoreCategoryContext:
    def __init__(self, catalog: CatalogSource, marketplace_key: str) -> None:
        self._catalog = catalog
        self._marketplace = marketplace_key

    def gather(self, facts: Sequence[Mapping[str, Any]], target: Any) -> TaskContext:
        snapshot = self._catalog.current(self._marketplace)
        if snapshot is None:
            raise PolicyBlockedError(
                AI_CATEGORY_CATALOG_MISSING,
                "sync the official leaf-category catalog before asking for a category",
            )
        asked = terms(facts)
        found = candidates(snapshot.entries, asked)
        if not found:
            raise PolicyBlockedError(
                AI_CATEGORY_NO_CANDIDATES,
                "no current leaf category matches the product's confirmed name",
                details={"terms": len(asked)},
            )
        return TaskContext(
            runtime={"category_candidates": found},
            inputs={
                "catalog": {
                    "taxonomy_revision": snapshot.taxonomy_revision,
                    "terms": asked,
                    "candidates": [item["category_id"] for item in found],
                }
            },
            facts=facts,
        )

    def finish(self, part: Mapping[str, Any], context: TaskContext) -> FinishResult:
        chosen = part.get("category_id")
        chosen = str(chosen).strip() if isinstance(chosen, str | int) else None
        offered = {
            item["category_id"]: item["whole_category_name"]
            for item in context.runtime["category_candidates"]
        }
        taxonomy = context.inputs["catalog"]["taxonomy_revision"]
        snapshot = self._catalog.current(self._marketplace)
        current_leaf = (
            snapshot is not None
            and snapshot.taxonomy_revision == taxonomy
            and any(e.category_id == chosen and e.leaf for e in snapshot.entries)
        )
        if chosen not in offered or not current_leaf:
            return FinishResult(None, AI_CATEGORY_NOT_A_CANDIDATE)
        assert chosen is not None
        confidence = part.get("confidence")
        low = not isinstance(confidence, int | float) or confidence < REVIEW_BELOW
        finished = dict(part)
        finished["category_id"] = chosen
        finished["whole_category_name"] = offered[chosen]
        finished["taxonomy_revision"] = taxonomy
        finished["candidate_count"] = len(offered)
        finished["requires_review"] = bool(part.get("requires_review") or low)
        return FinishResult(finished, None)


__all__ = [
    "CATEGORY_FACT_FIELDS",
    "CATEGORY_RESULT_KEY",
    "CATEGORY_SCHEMA_VERSION",
    "CATEGORY_TASK_KEY",
    "SmartStoreCategoryContext",
    "candidates",
    "terms",
]
