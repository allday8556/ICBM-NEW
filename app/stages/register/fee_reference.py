"""The marketplace fee reference table the target-policy form fills its fee fields from.

Owner direction 2026-10-16: a fee is a property of marketplace × category (and, for SmartStore, of
the store), so the seller picks a preset once instead of typing a fee per product. This module is
that preset's data, served read-only by ``GET /api/v1/settings/fee-reference/{marketplace_key}``.

It is a **reference, never truth**:

- The values come from seller blogs and calculators, not from an official NAVER notice. The form
  shows them as 「참고값 · 기준일 · 출처」 and the seller can change every number before saving.
- Nothing here prices anything or is read by the pricing owner. A value reaches pricing only when
  the seller saves it in a target-policy revision, through that revision's own validation.
- A rate is exact decimal text of a fraction (``"0.0363"`` is 3.63%), the same form as the
  pricing context's ``fee_rate``. ``None`` means no source states it: the seller must type it.

SmartStore since the 2025-06-02 reform charges two rates on a sale, neither of them per category:

- **판매수수료** by store type and inflow. The inflow of a future order is unknown when a product is
  listed, so the preset uses the higher 일반 유입 rate; the 마케팅 링크 rate is shown for reference.
- **네이버페이 주문관리수수료** by the seller's grade (국세청 신고 매출 구간). The 영세·중소
  rates were lowered for payments from 2025-10-01; 일반 was not.

All rates are VAT-included, as they are deducted at settlement.

TODO(next step, Track C): per-category fee tables for the other marketplaces (Coupang 카테고리별
판매수수료 etc.) belong here as further references keyed by marketplace × category. They are not
implemented: Coupang is Track C.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel

from app.platform.core.errors import NotFoundError

FEE_REFERENCE_UNKNOWN: Final = "FEE_REFERENCE_UNKNOWN"


class StoreType(StrEnum):
    """스토어 유형: the store the SmartStore 판매수수료 depends on."""

    SMART_STORE = "SMART_STORE"
    BRAND_STORE = "BRAND_STORE"


class SellerGrade(StrEnum):
    """판매자 등급 (네이버페이 가맹점 매출 구간) the 주문관리수수료 depends on."""

    GENERAL = "GENERAL"
    SMB3 = "SMB3"
    SMB2 = "SMB2"
    SMB1 = "SMB1"
    SMALL = "SMALL"


@dataclass(frozen=True)
class _Source:
    label: str
    url: str
    states: str


@dataclass(frozen=True)
class _Store:
    store_type: StoreType
    label: str
    sales_fee_rate: str | None
    marketing_link_sales_fee_rate: str | None


@dataclass(frozen=True)
class _Grade:
    seller_grade: SellerGrade
    label: str
    revenue_band: str
    order_fee_rate: str | None


@dataclass(frozen=True)
class _Reference:
    version: str
    reference_date: str
    stores: tuple[_Store, ...]
    grades: tuple[_Grade, ...]
    sources: tuple[_Source, ...]


# Fetched 2026-10-16. calctools.co.kr states the 판매수수료 (VAT incl.) and still lists the
# 주문관리수수료 from before 2025-10-01 (영세 1.98%, 중소1 2.585%, 중소2 2.75%, 중소3 3.025%); the
# sellerking.io notice states the rates from 2025-10-01, which are the ones used here. Sources
# disagree, which is why every value stays editable.
_SMARTSTORE: Final = _Reference(
    version="smartstore-fees-20251001",
    reference_date="2025-10-01",
    stores=(
        _Store(StoreType.SMART_STORE, "스마트스토어", "0.03003", "0.01001"),
        _Store(StoreType.BRAND_STORE, "브랜드스토어", "0.04004", "0.02002"),
    ),
    grades=(
        _Grade(SellerGrade.GENERAL, "일반", "연 매출 30억 원 초과", "0.0363"),
        _Grade(SellerGrade.SMB3, "중소3", "연 매출 10억~30억 원", "0.03003"),
        _Grade(SellerGrade.SMB2, "중소2", "연 매출 5억~10억 원", "0.02728"),
        _Grade(SellerGrade.SMB1, "중소1", "연 매출 3억~5억 원", "0.02563"),
        _Grade(SellerGrade.SMALL, "영세", "연 매출 3억 원 이하", "0.01947"),
    ),
    sources=(
        _Source(
            "calctools.co.kr 스마트스토어 수수료 계산기",
            "https://calctools.co.kr/finance/smartstore-fee",
            "판매수수료 (일반 유입·마케팅 링크, VAT 포함)",
        ),
        _Source(
            "sellerking.io 스마트스토어 수수료 인하 안내",
            "https://www.sellerking.io/blog/smartstore-commission-reduction-notice",
            "2025-10-01부터 등급별 주문관리수수료 (VAT 포함)",
        ),
    ),
)

_REFERENCES: Final = {"smartstore": _SMARTSTORE}


class FeeSourceView(BaseModel):
    label: str
    url: str
    states: str


class StoreTypeView(BaseModel):
    store_type: StoreType
    label: str
    sales_fee_rate: str | None
    marketing_link_sales_fee_rate: str | None


class SellerGradeView(BaseModel):
    seller_grade: SellerGrade
    label: str
    revenue_band: str
    order_fee_rate: str | None


class FeeReferenceView(BaseModel):
    """One marketplace's fee reference table, as the target-policy form reads it."""

    marketplace_key: str
    version: str
    reference_date: str
    official: bool
    store_types: list[StoreTypeView]
    seller_grades: list[SellerGradeView]
    sources: list[FeeSourceView]


def has_reference(marketplace_key: str) -> bool:
    return marketplace_key in _REFERENCES


def fee_reference(marketplace_key: str) -> FeeReferenceView:
    """The marketplace's reference table, or ``FEE_REFERENCE_UNKNOWN`` when it has none."""
    found = _REFERENCES.get(marketplace_key)
    if found is None:
        raise NotFoundError(
            FEE_REFERENCE_UNKNOWN,
            "no fee reference table exists for this marketplace",
            details={"marketplace_key": marketplace_key},
        )
    return FeeReferenceView(
        marketplace_key=marketplace_key,
        version=found.version,
        reference_date=found.reference_date,
        official=False,
        store_types=[
            StoreTypeView(
                store_type=store.store_type,
                label=store.label,
                sales_fee_rate=store.sales_fee_rate,
                marketing_link_sales_fee_rate=store.marketing_link_sales_fee_rate,
            )
            for store in found.stores
        ],
        seller_grades=[
            SellerGradeView(
                seller_grade=grade.seller_grade,
                label=grade.label,
                revenue_band=grade.revenue_band,
                order_fee_rate=grade.order_fee_rate,
            )
            for grade in found.grades
        ],
        sources=[
            FeeSourceView(label=source.label, url=source.url, states=source.states)
            for source in found.sources
        ],
    )
