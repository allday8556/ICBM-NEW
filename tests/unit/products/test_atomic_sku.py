"""AtomicSKU identity is semantic and independent of option-authoring revisions."""

import pytest

from app.stages.products.atomic_sku import (
    ATOMIC_SKU_SIGNATURE_VERSION,
    AtomicSKUError,
    atomic_sku_selection_signature,
    source_configuration,
    source_selection,
)


def test_reads_one_explicit_configuration_and_selection() -> None:
    value = (
        '{"configurations":[{"selections":["300mg","30정"],'
        '"supplier_sku_id":"sku-1","sold_out":false}]}'
    )
    configuration = source_configuration(value, "$.configurations[0]")
    assert configuration.selections == ("300mg", "30정")
    assert configuration.supplier_sku_id == "sku-1"
    assert source_selection(value, "$.configurations[0].selections[1]") == "30정"


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ('{"configurations":[]}', "$.configurations[0]"),
        ('{"configurations":[{"selections":[]}]}', "$.configurations[0]"),
        ('{"configurations":[{"selections":[true]}]}', "$.configurations[0]"),
        ('{"configurations":[{"selections":["x"]}]}', "$.configurations[0].selections"),
    ],
)
def test_rejects_missing_or_non_configuration_evidence(value: str, path: str) -> None:
    with pytest.raises(AtomicSKUError):
        source_configuration(value, path)


def test_atomic_sku_semantic_signature_has_a_golden_and_canonical_axis_order() -> None:
    selections = (
        ("per_unit_weight", "300", "mg"),
        ("quantity", "60", "tablet"),
    )

    assert ATOMIC_SKU_SIGNATURE_VERSION == "atomic-sku-set-signature/v2"
    assert atomic_sku_selection_signature(selections) == (
        "bb1b353748d1038ec7a490cec4b684f36e8c816d21d4ac64c0b154e25a907a55"
    )
    assert atomic_sku_selection_signature(tuple(reversed(selections))) == (
        atomic_sku_selection_signature(selections)
    )


def test_atomic_sku_semantic_signature_excludes_labels_ids_and_authored_order() -> None:
    original = (
        ("per_unit_weight", "300", "mg"),
        ("quantity", "60", "tablet"),
    )

    assert atomic_sku_selection_signature(original) != atomic_sku_selection_signature(
        (("per_unit_weight", "301", "mg"), ("quantity", "60", "tablet"))
    )
    assert atomic_sku_selection_signature(original) != atomic_sku_selection_signature(
        (("per_unit_weight", "300", "g"), ("quantity", "60", "tablet"))
    )
