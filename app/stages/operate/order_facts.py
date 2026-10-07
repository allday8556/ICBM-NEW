"""What a marketplace order read hands OPERATE (ADR-0023 §5, §7): pure values, no I/O.

The marketplace adapter builds these from its allow-listed answer; OPERATE never imports the
adapter. ``ShippingRecord`` is the only personal data: OPERATE encrypts it before anything is
stored and never logs it, so it never prints its values.
"""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class OrderChange:
    """One product order the change listing named, with the change it reported."""

    product_order_id: str
    changed_at: datetime
    change_type: str | None = None
    status: str | None = None
    claim_type: str | None = None
    claim_status: str | None = None


@dataclass(frozen=True)
class ChangePage:
    """One page of a change window. ``more_from``/``more_sequence`` continue it when present."""

    changes: tuple[OrderChange, ...]
    more_from: datetime | None = None
    more_sequence: str | None = None


@dataclass(frozen=True, repr=False)
class ShippingRecord:
    """The recipient and address the shipment needs (ADR-0023 §7). Never printed or logged."""

    recipient_name: str | None = None
    phone1: str | None = None
    phone2: str | None = None
    base_address: str | None = None
    detail_address: str | None = None
    zip_code: str | None = None
    memo: str | None = None

    def __repr__(self) -> str:
        return "ShippingRecord(<redacted>)"

    @property
    def empty(self) -> bool:
        return not any(asdict(self).values())

    def encoded(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode("utf-8")

    @classmethod
    def decoded(cls, raw: bytes) -> "ShippingRecord":
        values = json.loads(raw.decode("utf-8"))
        return cls(**{key: values.get(key) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ProductOrderFacts:
    """One product order as the detail read showed it, inside the allow-list only."""

    product_order_id: str
    order_id: str | None = None
    status: str | None = None
    claim_type: str | None = None
    claim_status: str | None = None
    place_order_status: str | None = None
    ordered_at: datetime | None = None
    paid_at: datetime | None = None
    decided_at: datetime | None = None
    shipping_due_at: datetime | None = None
    channel_product_id: str | None = None
    original_product_id: str | None = None
    option_manage_code: str | None = None
    seller_product_code: str | None = None
    product_name: str | None = None
    product_option: str | None = None
    quantity: int | None = None
    unit_price: int | None = None
    total_payment_amount: int | None = None
    delivery_method: str | None = None
    delivery_attribute_type: str | None = None
    delivery_status: str | None = None
    delivery_company: str | None = None
    tracking_number: str | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    shipping: ShippingRecord = field(default_factory=ShippingRecord, repr=False)


class OrderSource(Protocol):
    """The adopted read-only order reads of one marketplace, wired by the composition root."""

    def available(self) -> bool: ...

    def changes(
        self, *, since: datetime, until: datetime, more_sequence: str | None = None
    ) -> ChangePage: ...

    def details(self, product_order_ids: Sequence[str]) -> tuple[ProductOrderFacts, ...]: ...


__all__ = ["ChangePage", "OrderChange", "OrderSource", "ProductOrderFacts", "ShippingRecord"]
