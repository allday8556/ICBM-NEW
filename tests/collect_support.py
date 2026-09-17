"""Synthetic COLLECT source-truth builders for tests (M3 PR-B).

Everything here is invented: no real supplier page, URL form, identity rule, price or member data
(Issue #52 §3, §13). The KM통상 URL form and identity rule are frozen only after reconnaissance.
"""

from datetime import UTC, datetime
from itertools import count

from app.collect.facts import (
    Availability,
    CollectedFacts,
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    ImageReference,
    ImageRole,
    NoticeItem,
    NoticeValue,
    OptionsValue,
    PricesValue,
    ShippingKind,
    ShippingValue,
    SourcePrice,
    StockValue,
    TextValue,
)

SHA = "a" * 64
EXTRACTOR_FINGERPRINT = "b" * 64
CAPTURED = datetime(2026, 9, 16, 1, 0, tzinfo=UTC)
SOURCE_URL = "https://shop.example/products/1234"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic image bytes for tests"


def evidence(
    locator: str,
    status: FieldStatus = FieldStatus.CONFIRMED,
    *,
    kind: EvidenceKind = EvidenceKind.DOM_TEXT,
    observed: str | None = "observed",
) -> Evidence:
    return Evidence(kind=kind, locator=locator, status=status, observed=observed)


def confirmed(value: object, locator: str) -> FieldFact:
    return FieldFact(FieldStatus.CONFIRMED, value, (evidence(locator),))  # type: ignore[arg-type]


def absent(locator: str) -> FieldFact:
    return FieldFact(
        FieldStatus.ABSENT, None, (evidence(locator, FieldStatus.ABSENT, observed=None),)
    )


def base_fields() -> dict[str, FieldFact]:
    """A complete, fully valid field set: every core field CONFIRMED, some coverage ABSENT."""
    return {
        "original_name": confirmed(TextValue(text="생들기름 350ml"), ".name"),
        "prices": confirmed(
            PricesValue(prices=(SourcePrice(label="회원가", amount_krw=12900),)), ".price"
        ),
        "options": confirmed(OptionsValue(axes=()), ".options"),
        "stock": confirmed(StockValue(availability=Availability.ON_SALE), "button.buy"),
        "shipping": confirmed(
            ShippingValue(
                kind=ShippingKind.CONDITIONAL,
                policy_text="3,000원 (50,000원 이상 무료)",
                fee_krw=3000,
                free_over_krw=50000,
            ),
            ".delivery",
        ),
        "minimum_sale_price": absent(".minimum-price"),
        "quantity_tiers": absent(".tiers"),
        "brand": confirmed(TextValue(text="예시 브랜드"), ".brand"),
        "manufacturer": absent(".maker"),
        "origin": confirmed(TextValue(text="국산"), ".origin"),
        "notice": confirmed(
            NoticeValue(items=(NoticeItem(label="용량", text="350ml"),)), ".notice"
        ),
        "detail_description": confirmed(TextValue(text="상세 설명"), ".detail"),
    }


REPRESENTATIVE = ImageReference(
    role=ImageRole.REPRESENTATIVE,
    ordinal=0,
    host="img.shop.example",
    provenance=".gallery img:nth-of-type(1)",
    status=FieldStatus.CONFIRMED,
    sha256=SHA,
)


_RUNS = count(1)


def collected(
    fields: dict[str, FieldFact] | None = None,
    images: tuple[ImageReference, ...] = (REPRESENTATIVE,),
    **overrides: object,
) -> CollectedFacts:
    """One synthetic collection. Each call is its own run: a durable run appends one revision, so
    a recollection history is a history of distinct runs (PR #70 review 5231130447)."""
    values: dict[str, object] = {
        "supplier_key": "kmretail",
        "source_product_id": "1234",
        "source_url": SOURCE_URL,
        "captured_at": CAPTURED,
        "extractor_revision": "test-collect-r1",
        "extractor_fingerprint": EXTRACTOR_FINGERPRINT,
        "collection_run_id": f"run-{next(_RUNS)}",
        "correlation_id": "cid-1",
        "fields": base_fields() if fields is None else fields,
        "images": images,
    }
    values.update(overrides)
    return CollectedFacts(**values)  # type: ignore[arg-type]


def with_field(key: str, fact: FieldFact, **overrides: object) -> CollectedFacts:
    fields = base_fields()
    fields[key] = fact
    return collected(fields=fields, **overrides)
