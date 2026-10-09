"""ADR-0031 §4.1: the gate a sales-channel reading puts on one marketplace."""

import pytest

from app.stages.collect.facts import FieldStatus, SalesChannelScope, SalesChannelsValue
from app.stages.products.source_gates import (
    SOURCE_CHANNEL_FORBIDDEN,
    SOURCE_CHANNEL_UNRESOLVED,
    channel_verdict,
)


def value(scope: SalesChannelScope, *forbidden: str) -> SalesChannelsValue:
    return SalesChannelsValue(scope=scope, forbidden=forbidden, policy_text="문구")


@pytest.mark.parametrize(
    ("status", "reading", "marketplace", "gate"),
    [
        (FieldStatus.ABSENT, None, "smartstore", None),
        (FieldStatus.CONFIRMED, value(SalesChannelScope.ALL_ALLOWED), "smartstore", None),
        (
            FieldStatus.CONFIRMED,
            value(SalesChannelScope.CLOSED_MALL_ONLY),
            "smartstore",
            SOURCE_CHANNEL_FORBIDDEN,
        ),
        (
            FieldStatus.CONFIRMED,
            value(SalesChannelScope.CLOSED_MALL_ONLY),
            "coupang",
            SOURCE_CHANNEL_FORBIDDEN,
        ),
        (FieldStatus.CONFIRMED, value(SalesChannelScope.LISTED, "coupang"), "smartstore", None),
        (
            FieldStatus.CONFIRMED,
            value(SalesChannelScope.LISTED, "coupang"),
            "coupang",
            SOURCE_CHANNEL_FORBIDDEN,
        ),
        (
            FieldStatus.CONFIRMED,
            value(SalesChannelScope.LISTED, "smartstore"),
            "smartstore",
            SOURCE_CHANNEL_FORBIDDEN,
        ),
        (FieldStatus.REVIEW_REQUIRED, None, "smartstore", SOURCE_CHANNEL_UNRESOLVED),
        (FieldStatus.CONFIRMED, None, "smartstore", SOURCE_CHANNEL_UNRESOLVED),
    ],
)
def test_the_adr_0031_table(
    status: FieldStatus, reading: object, marketplace: str, gate: str | None
) -> None:
    assert channel_verdict(status, reading, marketplace) == gate


def test_a_listed_scope_names_marketplaces_icbm_knows() -> None:
    with pytest.raises(ValueError):
        value(SalesChannelScope.LISTED, "gmarket")
    with pytest.raises(ValueError):
        value(SalesChannelScope.LISTED)
    with pytest.raises(ValueError):
        value(SalesChannelScope.ALL_ALLOWED, "coupang")
