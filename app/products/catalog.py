"""The product DB's operator read path (Gate 1 G1-C, ADR-0015 §5; Issue #89 5792525426).

List, search, pagination, detail and registration-target selection over the canonical Product that
M4's :class:`~app.products.store.ProductFoundationStore` owns and the source facts COLLECT owns.
This module decides only how they are read and shown. It owns no truth and writes nothing:

- **The list** names ACTIVE Products in one total order, newest first and then by canonical
  identifier, behind an opaque cursor bound to its own search. A retired Product is never listed and
  stays addressable by its identifier.
- **Search** reads identities and each member's **current** CONFIRMED ``original_name`` only: a
  historical revision never matches.
- **Source facts** are read through each member's exact current source revision and carry that
  revision's identifier. Nothing is copied, nothing is chosen as "the" product name when members
  differ, and a fact under review or absent stays so: no value is filled from another member or
  Product.
- **Selection** is revalidated against the current Product on every request and never persisted:
  one ACTIVE Product, its current membership revision and Items of that Product only, each with a
  current binding held by one of its CONFIRMED members. Whether an Item is ready to register —
  its facts, stock, images, price — is not decided here: that is readiness and the registration
  preflight.
"""

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from app.collect.facts import FieldStatus, ImageRole, ImagesValue, StockValue, TextValue
from app.core.errors import AppError, ErrorClass, InputValidationError
from app.products.contracts import MemberSourceView, SourceFactView, SourceImagesView
from app.products.model import GroupStatus
from app.products.store import (
    NAME_FIELD,
    FieldReading,
    ProductFoundationUnit,
    ProductPageKey,
    ProductReadback,
)

PAGE_LIMIT_DEFAULT = 20
PAGE_LIMIT_MAX = 50
QUERY_MAX_LENGTH = 100
SELECTION_MAX_ITEMS = 50
IDENTIFIER_MAX_LENGTH = 36
CURSOR_VERSION = 1

IMAGES_FIELD = "images"
# The source facts the detail shows, each read through its member's current revision.
DISPLAY_FIELDS = (NAME_FIELD, "brand", "manufacturer", "origin", "stock")

# Request refusals.
QUERY_INVALID = "PRODUCTS_QUERY_INVALID"
PAGE_LIMIT_INVALID = "PRODUCTS_PAGE_LIMIT_INVALID"
CURSOR_INVALID = "PRODUCTS_CURSOR_INVALID"
SELECTION_INVALID = "PRODUCTS_SELECTION_INVALID"
SELECTION_NOT_SELECTABLE = "PRODUCTS_SELECTION_NOT_SELECTABLE"
SELECTION_MEMBERSHIP_STALE = "PRODUCTS_SELECTION_MEMBERSHIP_STALE"
SELECTION_ITEM_OUTSIDE_PRODUCT = "PRODUCTS_SELECTION_ITEM_OUTSIDE_PRODUCT"
SELECTION_ITEM_NOT_SELECTABLE = "PRODUCTS_SELECTION_ITEM_NOT_SELECTABLE"

# Why nothing of a Product is selectable.
PRODUCT_RETIRED = "PRODUCTS_PRODUCT_RETIRED"
MEMBERSHIP_REVISION_MISSING = "PRODUCTS_MEMBERSHIP_REVISION_MISSING"
MEMBERSHIP_REVISION_NOT_CURRENT = "PRODUCTS_MEMBERSHIP_REVISION_NOT_CURRENT"
# Why one Item is not selectable.
ITEM_BINDING_MISSING = "PRODUCTS_ITEM_BINDING_MISSING"
ITEM_BINDING_MEMBER_NOT_CONFIRMED = "PRODUCTS_ITEM_BINDING_MEMBER_NOT_CONFIRMED"


class SelectionConflictError(AppError):
    """A registration-target selection the current Product does not admit."""

    error_class = ErrorClass.CONFLICT


# ---------------------------------------------------------------- list and search


def search_text(query: str | None) -> str | None:
    """The search as the operator typed it, trimmed; ``None`` for no search."""
    if query is None:
        return None
    text = query.strip()
    if len(text) > QUERY_MAX_LENGTH:
        raise InputValidationError(
            QUERY_INVALID, f"a search is at most {QUERY_MAX_LENGTH} characters"
        )
    return text or None


def needle(text: str | None) -> str | None:
    """The search folded the way SQLite's ``lower`` folds a column: ASCII only."""
    if text is None:
        return None
    return "".join(char.lower() if char.isascii() else char for char in text)


def page_limit(limit: int | None) -> int:
    if limit is None:
        return PAGE_LIMIT_DEFAULT
    if isinstance(limit, bool) or not 1 <= limit <= PAGE_LIMIT_MAX:
        raise InputValidationError(
            PAGE_LIMIT_INVALID, f"a page holds between 1 and {PAGE_LIMIT_MAX} products"
        )
    return limit


def _search_digest(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def encode_cursor(key: ProductPageKey, text: str | None) -> str:
    """An opaque position after ``key``, bound to this exact search."""
    payload = json.dumps(
        {
            "v": CURSOR_VERSION,
            "at": key.created_at.isoformat(),
            "id": key.product_group_id,
            "q": _search_digest(text),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, text: str | None) -> ProductPageKey:
    """The position a cursor names. Anything this module did not issue for this same search is
    refused: a cursor never silently restarts, and never continues another search."""

    def refuse(message: str) -> InputValidationError:
        return InputValidationError(CURSOR_INVALID, message)

    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload: Any = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise refuse("the cursor is not one this list issued") from exc
    if not isinstance(payload, dict) or set(payload) != {"v", "at", "id", "q"}:
        raise refuse("the cursor is not one this list issued")
    if payload["v"] != CURSOR_VERSION or not isinstance(payload["at"], str):
        raise refuse("the cursor is not one this list issued")
    group_id = payload["id"]
    if not isinstance(group_id, str) or not 0 < len(group_id) <= IDENTIFIER_MAX_LENGTH:
        raise refuse("the cursor is not one this list issued")
    if payload["q"] != _search_digest(text):
        raise refuse("the cursor continues another search")
    try:
        created_at = datetime.fromisoformat(payload["at"])
    except ValueError as exc:
        raise refuse("the cursor is not one this list issued") from exc
    if created_at.tzinfo is None:
        raise refuse("the cursor is not one this list issued")
    return ProductPageKey(created_at, group_id)


# ---------------------------------------------------------------- source facts, read through


def _fact(key: str, reading: FieldReading | None) -> SourceFactView:
    if reading is None:
        return SourceFactView(key=key, status=None, value=None)
    value = None
    if reading.status is FieldStatus.CONFIRMED:
        if isinstance(reading.value, TextValue):
            value = reading.value.text
        elif isinstance(reading.value, StockValue):
            value = reading.value.availability.value
    return SourceFactView(key=key, status=reading.status, value=value)


def _images(reading: FieldReading | None) -> SourceImagesView:
    if reading is None:
        return SourceImagesView(status=None, references=0, included=0, representative_sha256=None)
    references = reading.value.references if isinstance(reading.value, ImagesValue) else ()
    included = [ref for ref in references if ref.sha256 is not None]
    representative = next(
        (ref.sha256 for ref in included if ref.role is ImageRole.REPRESENTATIVE), None
    )
    return SourceImagesView(
        status=reading.status,
        references=len(references),
        included=len(included),
        representative_sha256=representative,
    )


def member_sources(
    unit: ProductFoundationUnit,
    product: ProductReadback,
    keys: Sequence[str],
    *,
    images: bool,
) -> tuple[MemberSourceView, ...]:
    """Every CONFIRMED member with these facts of its own current source revision, in the
    Product's member order. A member with no current revision shows no facts at all."""
    views = []
    wanted = (*keys, IMAGES_FIELD) if images else tuple(keys)
    for member in product.members:
        revision = member.current_source_revision_id
        readings = {} if revision is None else unit.source_fields(revision, wanted)
        views.append(
            MemberSourceView(
                member_id=member.member_id,
                supplier_key=member.supplier_key,
                source_product_id=member.source_product_id,
                source_revision_id=revision,
                facts_status=member.current_facts_status,
                facts=() if revision is None else tuple(_fact(k, readings.get(k)) for k in keys),
                images=_images(readings.get(IMAGES_FIELD)) if images and revision else None,
            )
        )
    return tuple(views)


# ---------------------------------------------------------------- registration-target selection


def product_unselectable_reason(product: ProductReadback, membership_current: bool) -> str | None:
    """Why no Item of this Product may be chosen now, if anything stops all of them."""
    if product.status is not GroupStatus.ACTIVE:
        return PRODUCT_RETIRED
    if product.membership_revision_id is None:
        return MEMBERSHIP_REVISION_MISSING
    if not membership_current:
        return MEMBERSHIP_REVISION_NOT_CURRENT
    return None


def item_reasons(product: ProductReadback, membership_current: bool) -> dict[str, str | None]:
    """Each Item of the Product and why it may not be chosen now (``None``: it may be). An Item
    needs a current binding held by one of the Product's own CONFIRMED members."""
    product_reason = product_unselectable_reason(product, membership_current)
    confirmed = {member.member_id for member in product.members}
    reasons: dict[str, str | None] = {}
    for item in product.items:
        if product_reason is not None:
            reasons[item.item_id] = product_reason
        elif item.current_binding is None:
            reasons[item.item_id] = ITEM_BINDING_MISSING
        elif item.current_binding.group_member_id not in confirmed:
            reasons[item.item_id] = ITEM_BINDING_MEMBER_NOT_CONFIRMED
        else:
            reasons[item.item_id] = None
    return reasons


def _identifier(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= IDENTIFIER_MAX_LENGTH


def selection_request(
    membership_revision_id: str | None, item_ids: Iterable[str] | None
) -> tuple[str, tuple[str, ...]]:
    """The selection as sent, refused whole unless it names one membership revision and between
    one and :data:`SELECTION_MAX_ITEMS` distinct Items."""
    chosen = tuple(item_ids or ())
    if not _identifier(membership_revision_id):
        raise InputValidationError(
            SELECTION_INVALID, "a selection names the membership revision it was made on"
        )
    if not chosen or len(chosen) > SELECTION_MAX_ITEMS:
        raise InputValidationError(
            SELECTION_INVALID, f"a selection names between 1 and {SELECTION_MAX_ITEMS} Items"
        )
    if not all(_identifier(item) for item in chosen) or len(set(chosen)) != len(chosen):
        raise InputValidationError(SELECTION_INVALID, "a selection names each Item once")
    assert membership_revision_id is not None
    return membership_revision_id, chosen
