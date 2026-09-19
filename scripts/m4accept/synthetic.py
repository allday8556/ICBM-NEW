"""Synthetic source truth for the M4 acceptance run (Issue #80 PR-F kickoff 5739459941 §D).

Everything here is invented: the supplier key, product identities, names, amounts and image
bytes. No real supplier page, URL, cookie, member price or business data is used, and none of it
enters the report.

Source truth is written only through COLLECT's accepted owners, and never as raw rows:
- the source-asset store keeps the image bytes;
- the collection-run store opens a run and settles it RECORDED;
- the revision store validates, fingerprints and appends the immutable revision.

That is the durable, RECORDED truth the M4 materializer requires.
"""

import hashlib
import struct
import uuid
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.collect.facts import (
    Availability,
    CollectedFacts,
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    ImageReference,
    ImageRole,
    MoneyValue,
    PricesValue,
    QuantityTier,
    QuantityTiersValue,
    ShippingKind,
    ShippingValue,
    SourcePrice,
    StockValue,
    TextValue,
)
from app.collect.revisions import StoredRevision
from scripts.m4accept.owners import Owners

SUPPLIER = "m4-synthetic"
SOURCE_URL = "https://m4-acceptance.example/products/{product}"
IMAGE_HOST = "img.m4-acceptance.example"
EXTRACTOR_REVISION = "m4-acceptance-extractor-v1"
EXTRACTOR_FINGERPRINT = hashlib.sha256(EXTRACTOR_REVISION.encode("ascii")).hexdigest()
CAPTURED = datetime(2026, 9, 19, tzinfo=UTC)


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def png(salt: str, width: int = 48, height: int = 48) -> bytes:
    """A minimal real PNG the vetted header decoder reads; ``salt`` makes the bytes distinct."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"tEXt", b"salt\x00" + salt.encode("ascii"))
        + _chunk(b"IEND", b"")
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Facts:
    """One synthetic revision. ``shipping_fee`` of ``None`` is FREE shipping; ``tiers`` of
    ``None`` is ``quantity_tiers`` ABSENT; ``minimum`` of ``None`` is no minimum sale price."""

    prices: tuple[int, ...]
    shipping_fee: int | None
    minimum: int | None
    tiers: tuple[tuple[int, int], ...] | None
    sold_out: bool = False


def _evidence(locator: str, status: FieldStatus) -> tuple[Evidence, ...]:
    return (Evidence(kind=EvidenceKind.DOM_TEXT, locator=locator, status=status),)


def _confirmed(value: object, locator: str) -> FieldFact:
    return FieldFact(FieldStatus.CONFIRMED, value, _evidence(locator, FieldStatus.CONFIRMED))  # type: ignore[arg-type]


def _absent(locator: str) -> FieldFact:
    return FieldFact(FieldStatus.ABSENT, None, _evidence(locator, FieldStatus.ABSENT))


def fields(facts: Facts) -> dict[str, FieldFact]:
    shipping = (
        ShippingValue(kind=ShippingKind.FREE, policy_text="synthetic free delivery")
        if facts.shipping_fee is None
        else ShippingValue(
            kind=ShippingKind.FIXED,
            policy_text="synthetic fixed delivery",
            fee_krw=facts.shipping_fee,
        )
    )
    availability = Availability.SOLD_OUT if facts.sold_out else Availability.ON_SALE
    return {
        "original_name": _confirmed(TextValue(text="M4 synthetic product"), ".m4-name"),
        "prices": _confirmed(
            PricesValue(
                prices=tuple(
                    SourcePrice(label=f"synthetic price {index + 1}", amount_krw=amount)
                    for index, amount in enumerate(facts.prices)
                )
            ),
            ".m4-price",
        ),
        "options": _absent(".m4-options"),
        "stock": _confirmed(StockValue(availability=availability), ".m4-buy"),
        "shipping": _confirmed(shipping, ".m4-delivery"),
        "minimum_sale_price": _absent(".m4-minimum")
        if facts.minimum is None
        else _confirmed(
            MoneyValue(label="synthetic minimum", amount_krw=facts.minimum), ".m4-minimum"
        ),
        "quantity_tiers": _absent(".m4-tiers")
        if facts.tiers is None
        else _confirmed(
            QuantityTiersValue(
                tiers=tuple(
                    QuantityTier(quantity=quantity, total_price_krw=total, label=f"tier {quantity}")
                    for quantity, total in facts.tiers
                )
            ),
            ".m4-tiers",
        ),
        "brand": _absent(".m4-brand"),
        "manufacturer": _absent(".m4-maker"),
        "origin": _absent(".m4-origin"),
        "notice": _absent(".m4-notice"),
        "detail_description": _absent(".m4-detail"),
    }


@dataclass(frozen=True)
class Recorded:
    run_id: str
    revision: StoredRevision


def record_revision(
    owners: Owners,
    *,
    product: str,
    facts: Facts,
    images: tuple[tuple[ImageRole, bytes], ...],
    sequence: int,
) -> Recorded:
    """Append one durably RECORDED synthetic revision through COLLECT's own stores."""
    refs = []
    ordinals: dict[ImageRole, int] = {}
    for role, data in images:
        stored = owners.source_assets.put(data)
        ordinal = ordinals.get(role, 0)
        ordinals[role] = ordinal + 1
        refs.append(
            ImageReference(
                role=role,
                ordinal=ordinal,
                host=IMAGE_HOST,
                provenance=f".m4-{role.value.lower()} img:nth-of-type({ordinal + 1})",
                status=FieldStatus.CONFIRMED,
                sha256=stored.sha256,
            )
        )
    source_url = SOURCE_URL.format(product=product)
    correlation = f"m4-accept-{product}-{sequence}"
    with owners.db.write() as session:
        run_id = owners.runs.open(
            session,
            job_id=str(uuid.uuid4()),
            correlation_id=correlation,
            supplier_key=SUPPLIER,
            source_url=source_url,
        )
    owners.runs.note_identity(run_id, source_product_id=product)
    revision = owners.revisions.append(
        CollectedFacts(
            supplier_key=SUPPLIER,
            source_product_id=product,
            source_url=source_url,
            captured_at=CAPTURED + timedelta(hours=sequence),
            extractor_revision=EXTRACTOR_REVISION,
            extractor_fingerprint=EXTRACTOR_FINGERPRINT,
            collection_run_id=run_id,
            correlation_id=correlation,
            fields=fields(facts),
            images=tuple(refs),
        )
    )
    owners.runs.recorded(
        run_id, revision_id=revision.revision_id, facts_status=revision.facts_status
    )
    return Recorded(run_id, revision)
