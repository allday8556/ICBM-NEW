"""The OPERATE stock review producer (M6-B, ADR-0023 §4; ADR-0016 §6).

It derives one condition, and only this one: a source product an ACTIVE SmartStore registration
sells is judged SOLD_OUT by its current revision. That is STOCK work for the operator — the listing
is live while the supplier cannot ship — and the operator decides (M6-07); nothing here changes a
listing. A stock field under review is COLLECT's own condition (``SOURCE_STOCK_REVIEW_REQUIRED``)
and is never indexed a second time here.

**Scope** is the source identity (``supplier_key`` + ``source_product_id``), the same canonical
shape COLLECT uses, so 수집관리 and 품절 read one list. **Source identity** is the current revision
id: a new recheck is a new revision, so an unchanged condition moves to it and supersedes the old
item (§4).
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Final

from app.capabilities.review.counts import OPERATE_STOCK_PRODUCER
from app.capabilities.review.model import ReviewCondition, ReviewKind
from app.stages.collect.facts import Availability
from app.stages.operate.stock import StockRecheckService, availability_of

LISTED_SOURCE_SOLD_OUT: Final = "LISTED_SOURCE_SOLD_OUT"


class StockReviewProducer:
    """Reads the listed sources and their current revisions; writes nothing."""

    def __init__(self, stock: StockRecheckService, revisions: object) -> None:
        self._stock = stock
        self._revisions = revisions

    @property
    def name(self) -> str:
        return OPERATE_STOCK_PRODUCER

    def scopes(self) -> Sequence[Mapping[str, str]]:
        return tuple(
            {"supplier_key": s.supplier_key, "source_product_id": s.source_product_id}
            for s in self._stock.listed_sources()
        )

    def truth_token(self) -> str:
        """Every listed source with its registrations and current revision id."""
        rows = []
        for source in self._stock.listed_sources():
            current = self._revisions.current_recorded(  # type: ignore[attr-defined]
                source.supplier_key, source.source_product_id
            )
            rows.append(
                [
                    source.supplier_key,
                    source.source_product_id,
                    list(source.registration_ids),
                    None if current is None else current.revision_id,
                ]
            )
        return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()

    def derive(self, scope: Mapping[str, str]) -> Sequence[ReviewCondition]:
        supplier, product = scope.get("supplier_key"), scope.get("source_product_id")
        conditions: list[ReviewCondition] = []
        for source in self._stock.listed_sources():
            if (supplier, product) != (None, None) and (
                source.supplier_key != supplier or source.source_product_id != product
            ):
                continue
            current = self._revisions.current_recorded(  # type: ignore[attr-defined]
                source.supplier_key, source.source_product_id
            )
            if availability_of(current) != Availability.SOLD_OUT.value:
                continue
            conditions.append(
                ReviewCondition(
                    kind=ReviewKind.STOCK,
                    producer=OPERATE_STOCK_PRODUCER,
                    scope={
                        "supplier_key": source.supplier_key,
                        "source_product_id": source.source_product_id,
                    },
                    subject="listing:sold_out",
                    reason_code=LISTED_SOURCE_SOLD_OUT,
                    source_identity=current.revision_id,
                )
            )
        return tuple(conditions)


__all__ = ["LISTED_SOURCE_SOLD_OUT", "StockReviewProducer"]
