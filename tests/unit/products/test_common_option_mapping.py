"""Pure validation for exact Product Fact leaf references."""

import pytest

from app.stages.products.common_option_mapping import (
    CommonOptionFactMappingError,
    ProductFactPointer,
    canonical_fact_leaf_json,
    fact_leaf,
    validate_fact_pointer,
)


def test_resolves_only_exact_text_or_integer_leaves() -> None:
    value = '{"axes":[{"name":"개별 중량/용량","values":["300mg"]}],"count":60}'
    assert fact_leaf(value, "$.axes[0].name") == "개별 중량/용량"
    assert fact_leaf(value, "$.axes[0].values[0]") == "300mg"
    assert fact_leaf(value, "$.count") == 60
    assert canonical_fact_leaf_json("300mg") == '"300mg"'


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ('{"axes":[]}', "$.axes"),
        ('{"value":null}', "$.value"),
        ('{"value":true}', "$.value"),
        ('{"value":1.5}', "$.value"),
        ('{"value":""}', "$.value"),
        ('{"axes":[]}', "$.axes[0]"),
        ('{"axes":[]}', "$.missing"),
    ],
)
def test_rejects_missing_or_non_scalar_evidence(value: str, path: str) -> None:
    with pytest.raises(CommonOptionFactMappingError):
        fact_leaf(value, path)


@pytest.mark.parametrize("path", ("$", "$..name", "$['name']", "$.name[*]"))
def test_pointer_uses_the_bounded_shared_json_path_grammar(path: str) -> None:
    with pytest.raises(CommonOptionFactMappingError, match="json_path"):
        validate_fact_pointer(ProductFactPointer("r1", "options", path))
