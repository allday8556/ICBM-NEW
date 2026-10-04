import pytest

from app.stages.products.common_options import (
    CommonSalesOptionAxisSpec,
    CommonSalesOptionError,
    CommonSalesOptionValueSpec,
    canonical_common_sales_options,
    common_sales_option_signature,
)


def value(canonical: str, display: str, unit: str | None = None) -> CommonSalesOptionValueSpec:
    return CommonSalesOptionValueSpec(canonical, display, unit)


def test_common_option_signature_is_canonical_but_preserves_authored_order() -> None:
    first = canonical_common_sales_options(
        (
            CommonSalesOptionAxisSpec(
                "per_unit_weight", " 개별 중량/용량 ", (value(" 300 ", " 300mg ", "mg"),)
            ),
            CommonSalesOptionAxisSpec("quantity", "수량", (value("60", "60정", "tablet"),)),
        )
    )
    normalized = canonical_common_sales_options(
        (
            CommonSalesOptionAxisSpec(
                "per_unit_weight", "개별 중량/용량", (value("300", "300mg", "mg"),)
            ),
            CommonSalesOptionAxisSpec("quantity", "수량", (value("60", "60정", "tablet"),)),
        )
    )
    reversed_axes = tuple(reversed(normalized))
    assert first == normalized
    assert common_sales_option_signature(first) == common_sales_option_signature(normalized)
    assert common_sales_option_signature(first) != common_sales_option_signature(reversed_axes)


@pytest.mark.parametrize(
    "axes, message",
    [
        ((), "at least one axis"),
        (
            (
                CommonSalesOptionAxisSpec("quantity", "수량", (value("30", "30정"),)),
                CommonSalesOptionAxisSpec("quantity", "수량", (value("60", "60정"),)),
            ),
            "duplicate semantic axis",
        ),
        ((CommonSalesOptionAxisSpec("Quantity", "수량", (value("60", "60정"),)),), "token"),
        ((CommonSalesOptionAxisSpec("quantity", "수량", ()),), "at least one value"),
        (
            (
                CommonSalesOptionAxisSpec(
                    "quantity", "수량", (value("60", "60정"), value("60", "sixty"))
                ),
            ),
            "repeats canonical value",
        ),
    ],
)
def test_invalid_common_option_shapes_fail_closed(
    axes: tuple[CommonSalesOptionAxisSpec, ...], message: str
) -> None:
    with pytest.raises(CommonSalesOptionError, match=message):
        canonical_common_sales_options(axes)
