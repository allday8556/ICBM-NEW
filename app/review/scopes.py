"""Which producer's items a canonical scope can name (ADR-0016 §7, §8; review 5810256789 B1).

Each existing screen shows the ReviewItems of its own owner scope; there is no top-level review
screen. A scope read names one of the **canonical scope shapes** below, exactly — never a subset
of one (review 5811564367 B1-R1). Items match a scope by containment, so an incomplete shape such
as ``marketplace_key`` alone would reach across accounts, and ``supplier_key`` alone across source
products; an item never crosses scopes (§8). Every other combination is refused.

- a COLLECT source product: ``supplier_key`` + ``source_product_id`` (수집관리);
- an M4 Product: ``product_group_id``, or one Item of it: ``product_group_id`` + ``item_id``
  (통합DB);
- a REGISTER account: ``marketplace_key`` + ``marketplace_account_id``; one Draft of it:
  + ``draft_id``; one Intent or one preparation of that Draft: + ``intent_id`` or
  + ``preparation_id`` (등록관리).

The read sees exactly the producers a shape names, with those producers' coverage; another
producer's coverage says nothing about whether this scope's list is current.
"""

from collections.abc import Iterable, Mapping
from typing import Final

from app.review.collect_producer import COLLECT_PRODUCER
from app.review.preflight_producer import PREFLIGHT_PRODUCER
from app.review.products_producer import PRODUCTS_PRODUCER
from app.review.register_producer import REGISTER_PRODUCER

_ACCOUNT: Final = ("marketplace_key", "marketplace_account_id")
_REGISTER: Final = (REGISTER_PRODUCER, PREFLIGHT_PRODUCER)

# Every scope shape an owner screen may read, and the producers whose items it names.
SCOPE_SHAPES: Final[Mapping[frozenset[str], tuple[str, ...]]] = {
    frozenset({"supplier_key", "source_product_id"}): (COLLECT_PRODUCER,),
    frozenset({"product_group_id"}): (PRODUCTS_PRODUCER,),
    frozenset({"product_group_id", "item_id"}): (PRODUCTS_PRODUCER,),
    frozenset(_ACCOUNT): _REGISTER,
    frozenset({*_ACCOUNT, "draft_id"}): _REGISTER,
    frozenset({*_ACCOUNT, "draft_id", "intent_id"}): (REGISTER_PRODUCER,),
    frozenset({*_ACCOUNT, "draft_id", "preparation_id"}): (PREFLIGHT_PRODUCER,),
}


def producers_of(scope: Mapping[str, str], wired: Iterable[str]) -> tuple[str, ...]:
    """The wired producers a canonical scope shape names, in order; none for any other shape."""
    named = SCOPE_SHAPES.get(frozenset(scope), ())
    present = set(wired)
    return tuple(sorted(name for name in named if name in present))
