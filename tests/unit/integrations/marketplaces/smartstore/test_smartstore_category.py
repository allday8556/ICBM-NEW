"""SmartStore's official leaf-category source over the adopted caller."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.platform.core.errors import PolicyBlockedError
from integrations.marketplaces.smartstore.caller import CategoryListResponse
from integrations.marketplaces.smartstore.category import SmartStoreCategoryCatalogSource
from integrations.marketplaces.smartstore.registry import EndpointId


class Caller:
    def __init__(self, items: object) -> None:
        self.items = items
        self.calls: list[tuple[object, object]] = []

    def call(self, endpoint_id: object, request: object) -> CategoryListResponse:
        self.calls.append((endpoint_id, request))
        return CategoryListResponse(retained={"items": self.items}, http_status=200)


def _bearer() -> Any:
    return SimpleNamespace(access_token="token", credential_generation=3, session_generation=5)


def test_source_returns_only_strict_documented_leaf_facts_in_stable_order() -> None:
    caller = Caller(
        [
            {
                "id": "50000002",
                "name": " 비타민 ",
                "wholeCategoryName": " 식품>건강식품>비타민 ",
                "last": True,
            },
            {
                "id": "50000001",
                "name": "건강기능식품",
                "wholeCategoryName": "식품>건강식품>건강기능식품",
                "last": True,
            },
        ]
    )
    source = SmartStoreCategoryCatalogSource(caller, _bearer)  # type: ignore[arg-type]

    categories = source.leaf_categories()

    assert [item.category_id for item in categories] == ["50000001", "50000002"]
    assert categories[1].name == "비타민"
    assert categories[1].whole_category_name == "식품>건강식품>비타민"
    assert caller.calls[0][0] is EndpointId.SMARTSTORE_CATEGORY_LIST


@pytest.mark.parametrize(
    "items",
    (
        [{"id": "50000001", "name": "식품", "wholeCategoryName": "식품", "last": False}],
        [{"id": "50000001", "name": "", "wholeCategoryName": "식품", "last": True}],
        [
            {"id": "50000001", "name": "식품", "wholeCategoryName": "식품", "last": True},
            {"id": "50000001", "name": "식품", "wholeCategoryName": "식품", "last": True},
        ],
    ),
)
def test_source_refuses_nonleaf_malformed_or_duplicate_provider_results(items: object) -> None:
    source = SmartStoreCategoryCatalogSource(Caller(items), _bearer)  # type: ignore[arg-type]
    with pytest.raises(PolicyBlockedError):
        source.leaf_categories()


def test_source_refuses_without_a_committed_session_before_calling() -> None:
    caller = Caller([])
    source = SmartStoreCategoryCatalogSource(caller, lambda: None)  # type: ignore[arg-type]
    with pytest.raises(PolicyBlockedError) as refused:
        source.leaf_categories()
    assert refused.value.code == "SMARTSTORE_SESSION_UNAVAILABLE"
    assert caller.calls == []
