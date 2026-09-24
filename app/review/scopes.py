"""Which producer's items a canonical scope can name (ADR-0016 §7, §8; review 5810256789 B1).

Each existing screen shows the ReviewItems of its own owner scope; there is no top-level review
screen. A scope read names the canonical identifiers the screen already holds. It sees exactly
the producers whose items carry every one of them, together with those producers' coverage:
- a COLLECT source product (``supplier_key``, ``source_product_id``) on 수집관리;
- an M4 Product or Item (``product_group_id``[, ``item_id``]) on 통합DB;
- a REGISTER account, Draft, Intent or preparation on 등록관리.

Another producer's coverage says nothing about whether this scope's list is current.
"""

from collections.abc import Iterable, Mapping
from typing import Final

from app.review.collect_producer import COLLECT_PRODUCER
from app.review.preflight_producer import PREFLIGHT_PRODUCER
from app.review.products_producer import PRODUCTS_PRODUCER
from app.review.register_producer import REGISTER_PRODUCER

PRODUCER_SCOPE_KEYS: Final[Mapping[str, frozenset[str]]] = {
    COLLECT_PRODUCER: frozenset({"supplier_key", "source_product_id"}),
    PRODUCTS_PRODUCER: frozenset({"product_group_id", "item_id"}),
    REGISTER_PRODUCER: frozenset(
        {"marketplace_key", "marketplace_account_id", "draft_id", "intent_id"}
    ),
    PREFLIGHT_PRODUCER: frozenset(
        {"marketplace_key", "marketplace_account_id", "draft_id", "preparation_id"}
    ),
}


def producers_of(scope: Mapping[str, str], wired: Iterable[str]) -> tuple[str, ...]:
    """The wired producers whose items can carry every identifier of ``scope``, in order."""
    keys = set(scope)
    return tuple(
        name for name in sorted(wired) if keys <= PRODUCER_SCOPE_KEYS.get(name, frozenset())
    )
