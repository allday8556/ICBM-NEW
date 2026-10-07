"""Read SmartStore product orders through the two adopted order reads (M6-C; ADR-0023 §5-§7).

A read only (M6-01): the change listing by time window (``SMARTSTORE_ORDER_CHANGES``) and the
product-order query by ids (``SMARTSTORE_ORDER_DETAILS``), each with the CONNECT owner's committed
bearer. The caller already applied the order allow-list; this module turns what survived into
OPERATE's values and refuses an answer whose allow-listed leaves do not have the documented types.
Request times are sent in KST, as the reference documents; every time read back is kept aware.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from app.platform.core.errors import PolicyBlockedError
from app.stages.operate.order_facts import (
    ChangePage,
    OrderChange,
    ProductOrderFacts,
    ShippingRecord,
)
from integrations.marketplaces.smartstore.caller import (
    ORDER_PAGE_MAX,
    OrderChangesRequest,
    OrderDetailsRequest,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import EndpointId

KST: Final = timezone(timedelta(hours=9))
INVALID: Final = "SMARTSTORE_ORDER_RESPONSE_INVALID"


def kst(moment: datetime) -> str:
    """The documented request date-time, ``yyyy-MM-dd'T'HH:mm:ss.SSS+09:00``."""
    if moment.tzinfo is None:
        raise ValueError("an order window bound must be aware")
    return moment.astimezone(KST).isoformat(timespec="milliseconds")


def _invalid(detail: str) -> PolicyBlockedError:
    return PolicyBlockedError(INVALID, f"the order answer is not the documented shape: {detail}")


def _time(value: object, name: str, *, required: bool = False) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise _invalid(name)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid(name) from None
    if moment.tzinfo is None:
        raise _invalid(name)
    return moment


def _text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(name)
    return value.strip() or None


def _int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(name)
    return value


def _object(value: object, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _invalid(name)
    return value


class SmartStoreOrderSource:
    def __init__(self, caller: SmartStoreEndpointCaller, bearer: Any) -> None:
        self._caller = caller
        self._bearer = bearer

    def available(self) -> bool:
        return self._bearer() is not None

    def _session(self) -> Any:
        bearer = self._bearer()
        if bearer is None:
            raise PolicyBlockedError(
                "SMARTSTORE_SESSION_UNAVAILABLE",
                "a current committed SmartStore session is required to read orders",
            )
        return bearer

    def changes(
        self, *, since: datetime, until: datetime, more_sequence: str | None = None
    ) -> ChangePage:
        bearer = self._session()
        response = self._caller.call(
            EndpointId.SMARTSTORE_ORDER_CHANGES,
            OrderChangesRequest(
                bearer.access_token,
                bearer.credential_generation,
                bearer.session_generation,
                kst(since),
                kst(until),
                more_sequence,
                ORDER_PAGE_MAX,
            ),
        )
        data = _object(response.retained.get("data"), "data")
        entries = data.get("lastChangeStatuses", [])
        if not isinstance(entries, list):
            raise _invalid("lastChangeStatuses")
        changes = []
        for entry in entries:
            entry = _object(entry, "lastChangeStatuses[]")
            product_order_id = _text(entry.get("productOrderId"), "productOrderId")
            changed_at = _time(entry.get("lastChangedDate"), "lastChangedDate", required=True)
            if product_order_id is None or changed_at is None:
                raise _invalid("productOrderId")
            changes.append(
                OrderChange(
                    product_order_id=product_order_id,
                    changed_at=changed_at,
                    change_type=_text(entry.get("lastChangedType"), "lastChangedType"),
                    status=_text(entry.get("productOrderStatus"), "productOrderStatus"),
                    claim_type=_text(entry.get("claimType"), "claimType"),
                    claim_status=_text(entry.get("claimStatus"), "claimStatus"),
                )
            )
        more = _object(data.get("more"), "more")
        more_from = _time(more.get("moreFrom"), "moreFrom")
        sequence = _text(more.get("moreSequence"), "moreSequence")
        if (more_from is None) != (sequence is None):
            raise _invalid("more")
        return ChangePage(tuple(changes), more_from, sequence)

    def details(self, product_order_ids: Sequence[str]) -> tuple[ProductOrderFacts, ...]:
        bearer = self._session()
        response = self._caller.call(
            EndpointId.SMARTSTORE_ORDER_DETAILS,
            OrderDetailsRequest(
                bearer.access_token,
                bearer.credential_generation,
                bearer.session_generation,
                tuple(product_order_ids),
            ),
        )
        entries = response.retained.get("data", [])
        if not isinstance(entries, list):
            raise _invalid("data")
        return tuple(self._facts(_object(entry, "data[]")) for entry in entries)

    @staticmethod
    def _facts(entry: Mapping[str, Any]) -> ProductOrderFacts:
        order = _object(entry.get("order"), "order")
        product = _object(entry.get("productOrder"), "productOrder")
        delivery = _object(entry.get("delivery"), "delivery")
        address = _object(product.get("shippingAddress"), "shippingAddress")
        product_order_id = _text(product.get("productOrderId"), "productOrderId")
        if product_order_id is None:
            raise _invalid("productOrderId")
        return ProductOrderFacts(
            product_order_id=product_order_id,
            order_id=_text(order.get("orderId"), "orderId"),
            status=_text(product.get("productOrderStatus"), "productOrderStatus"),
            claim_type=_text(product.get("claimType"), "claimType"),
            claim_status=_text(product.get("claimStatus"), "claimStatus"),
            place_order_status=_text(product.get("placeOrderStatus"), "placeOrderStatus"),
            ordered_at=_time(order.get("orderDate"), "orderDate"),
            paid_at=_time(order.get("paymentDate"), "paymentDate"),
            decided_at=_time(product.get("decisionDate"), "decisionDate"),
            shipping_due_at=_time(product.get("shippingDueDate"), "shippingDueDate"),
            channel_product_id=_text(product.get("productId"), "productId"),
            original_product_id=_text(product.get("originalProductId"), "originalProductId"),
            option_manage_code=_text(product.get("optionManageCode"), "optionManageCode"),
            seller_product_code=_text(product.get("sellerProductCode"), "sellerProductCode"),
            product_name=_text(product.get("productName"), "productName"),
            product_option=_text(product.get("productOption"), "productOption"),
            quantity=_int(product.get("quantity"), "quantity"),
            unit_price=_int(product.get("unitPrice"), "unitPrice"),
            total_payment_amount=_int(product.get("totalPaymentAmount"), "totalPaymentAmount"),
            delivery_method=_text(delivery.get("deliveryMethod"), "deliveryMethod")
            or _text(product.get("expectedDeliveryMethod"), "expectedDeliveryMethod"),
            delivery_attribute_type=_text(
                product.get("deliveryAttributeType"), "deliveryAttributeType"
            ),
            delivery_status=_text(delivery.get("deliveryStatus"), "deliveryStatus"),
            delivery_company=_text(delivery.get("deliveryCompany"), "deliveryCompany"),
            tracking_number=_text(delivery.get("trackingNumber"), "trackingNumber"),
            sent_at=_time(delivery.get("sendDate"), "sendDate"),
            delivered_at=_time(delivery.get("deliveredDate"), "deliveredDate"),
            shipping=ShippingRecord(
                recipient_name=_text(address.get("name"), "name"),
                phone1=_text(address.get("tel1"), "tel1"),
                phone2=_text(address.get("tel2"), "tel2"),
                base_address=_text(address.get("baseAddress"), "baseAddress"),
                detail_address=_text(address.get("detailedAddress"), "detailedAddress"),
                zip_code=_text(address.get("zipCode"), "zipCode"),
                memo=_text(product.get("shippingMemo"), "shippingMemo"),
            ),
        )


__all__ = ["KST", "SmartStoreOrderSource", "kst"]
