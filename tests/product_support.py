"""Synthetic PRODUCT DB fixtures for M4 tests (Issue #80).

Everything here is invented: no real supplier page, price, marketplace fee or account. Marketplace
keys are placeholders such as ``market_a``: no real marketplace fee is known to the repository, and
none is assumed here.
"""

import contextlib
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path

from app.collect.assets import DecodedImage, SourceAssetStore
from app.collect.facts import (
    Availability,
    FieldFact,
    FieldStatus,
    ImageReference,
    MoneyValue,
    PricesValue,
    ShippingKind,
    ShippingValue,
    SourcePrice,
    StockValue,
)
from app.collect.revisions import ProductFactsRevisionStore, StoredRevision
from app.collect.runs import CollectionRunStore
from app.config import AppConfig
from app.container import Container
from app.db.database import Database
from app.products.pricing import PricingContextInput, Rounding
from tests.collect_support import (
    PNG,
    REPRESENTATIVE,
    SOURCE_URL,
    absent,
    base_fields,
    collected,
    confirmed,
    evidence,
)
from tests.support import FakeClock

SUPPLIER = "kmretail"
PRODUCT = "1234"


class FakeDecoder:
    def decode(self, data: bytes) -> DecodedImage | None:
        return DecodedImage("image/png", 64, 64) if data.startswith(b"\x89PNG") else None


def review(locator: str) -> FieldFact:
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        (evidence(locator, FieldStatus.REVIEW_REQUIRED, observed=None),),
    )


def free() -> FieldFact:
    return confirmed(ShippingValue(kind=ShippingKind.FREE, policy_text="free"), ".delivery")


def fixed(fee: int) -> FieldFact:
    return confirmed(
        ShippingValue(kind=ShippingKind.FIXED, policy_text="fixed", fee_krw=fee), ".delivery"
    )


def conditional() -> FieldFact:
    return confirmed(
        ShippingValue(
            kind=ShippingKind.CONDITIONAL, policy_text="cond", fee_krw=3000, free_over_krw=50000
        ),
        ".delivery",
    )


def unknown_shipping() -> FieldFact:
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        ShippingValue(kind=ShippingKind.UNKNOWN, policy_text="unclear"),
        (evidence(".delivery", FieldStatus.REVIEW_REQUIRED),),
    )


def minimum(amount: int) -> FieldFact:
    return confirmed(MoneyValue(label="minimum", amount_krw=amount), ".minimum-price")


def prices(*amounts: int) -> FieldFact:
    return confirmed(
        PricesValue(
            prices=tuple(SourcePrice(label=f"p{i}", amount_krw=a) for i, a in enumerate(amounts))
        ),
        ".price",
    )


def sold_out() -> FieldFact:
    return confirmed(StockValue(availability=Availability.SOLD_OUT), "button.soldout")


def product(
    *,
    price: int | tuple[int, ...] = 10000,
    shipping: FieldFact | None = None,
    minimum_sale_price: FieldFact | None = None,
    **overrides: FieldFact,
) -> dict[str, FieldFact]:
    """A base product: options and tiers ABSENT, one purchase price, FREE shipping and no minimum
    sale price unless stated otherwise."""
    fields = base_fields()
    fields["options"] = absent(".options")
    fields["prices"] = prices(*(price if isinstance(price, tuple) else (price,)))
    fields["shipping"] = free() if shipping is None else shipping
    fields["minimum_sale_price"] = (
        absent(".minimum-price") if minimum_sale_price is None else minimum_sale_price
    )
    fields.update(overrides)
    return fields


def context(**overrides: object) -> PricingContextInput:
    values: dict[str, object] = {
        "marketplace_key": "market_a",
        "account_id": None,
        "fee_table_version": "fee-test-1",
        "pricing_policy_version": "policy-test-1",
        "fee_rate": "0.1",
        "fee_fixed_krw": 0,
        "other_cost_rate": "0",
        "other_cost_fixed_krw": 0,
        "cost_rounding": Rounding.CEIL_KRW_1,
        "price_rounding": Rounding.CEIL_KRW_1,
    }
    values.update(overrides)
    return PricingContextInput(**values)  # type: ignore[arg-type]


class Collections:
    """Durable collections as COLLECT makes them: a run opened with its identity, the revision
    appended through the real store, and the outcome settled only when the test says so."""

    def __init__(self, db: Database, assets_dir: Path) -> None:
        self._db = db
        self._runs = CollectionRunStore(db, FakeClock())
        self._revisions = ProductFactsRevisionStore(db, FakeClock())
        assets = SourceAssetStore(assets_dir, db, FakeDecoder(), FakeClock())
        self._images = (replace(REPRESENTATIVE, sha256=assets.put(PNG).sha256),)

    @classmethod
    def of(cls, container: Container, config: AppConfig) -> "Collections":
        return cls(container.db, config.source_assets_dir)

    def collect(
        self,
        fields: dict[str, FieldFact] | None = None,
        *,
        source_product_id: str = PRODUCT,
        extra_images: tuple[ImageReference, ...] = (),
        **overrides: object,
    ) -> tuple[str, StoredRevision]:
        """``extra_images`` follow the stored representative image, as the page exposed them."""
        with self._db.write() as session:
            run_id = self._runs.open(
                session,
                job_id=str(uuid.uuid4()),
                correlation_id="cid-run",
                supplier_key=SUPPLIER,
                source_url=SOURCE_URL,
            )
        self._runs.note_identity(run_id, source_product_id=source_product_id)
        revision = self._revisions.append(
            collected(
                fields=product() if fields is None else fields,
                images=(*self._images, *extra_images),
                source_product_id=source_product_id,
                collection_run_id=run_id,
                **overrides,
            )
        )
        self._runs.recorded(
            run_id, revision_id=revision.revision_id, facts_status=revision.facts_status
        )
        return run_id, revision


def raw(config: AppConfig) -> sqlite3.Connection:
    connection = sqlite3.connect(config.data_dir / "runtime" / "icbm.db")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def count(config: AppConfig, table: str, where: str = "1", *args: object) -> int:
    with contextlib.closing(raw(config)) as connection:
        return int(
            connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
        )
