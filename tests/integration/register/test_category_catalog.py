"""Official leaf-category catalog persistence and selection validation."""

import pytest
from fastapi.testclient import TestClient

from app.platform.core.errors import InputValidationError
from app.stages.register.category_catalog import (
    CategoryCatalogService,
    CategoryCatalogStore,
    ProviderCategory,
)

pytestmark = pytest.mark.integration
CLIENT = {"X-ICBM-Client": "pytest"}


class Source:
    def __init__(self, entries: tuple[ProviderCategory, ...]) -> None:
        self.entries = entries
        self.calls = 0

    def leaf_categories(self) -> tuple[ProviderCategory, ...]:
        self.calls += 1
        return self.entries


def _service(container: object, entries: tuple[ProviderCategory, ...]) -> CategoryCatalogService:
    return CategoryCatalogService(
        CategoryCatalogStore(container.db, container.clock, container.audit),  # type: ignore[attr-defined]
        Source(entries),
        endpoint_mapping_revision="m5-category-list-r1",
    )


def test_sync_records_an_immutable_searchable_snapshot_and_reuses_unchanged_content(
    container,
) -> None:
    entries = (
        ProviderCategory("50000002", "비타민", "식품>건강식품>비타민", True),
        ProviderCategory("50000001", "건강기능식품", "식품>건강식품>건강기능식품", True),
    )
    service = _service(container, entries)
    first = service.sync(actor="operator", correlation_id="category-sync-1")
    assert first.total == 2
    assert [item.category_id for item in first.entries] == ["50000001", "50000002"]
    assert first.taxonomy_revision == f"smartstore-categories-{first.content_fingerprint[:24]}"
    service.require_current_leaf("smartstore", first.taxonomy_revision, "50000001")
    found = service.catalog("smartstore", query="비타민", limit=10)
    assert [item.category_id for item in found.entries] == ["50000002"]

    again = service.sync(actor="operator", correlation_id="category-sync-2")
    assert again.snapshot_id == first.snapshot_id

    changed = _service(
        container, (ProviderCategory("50000003", "홍삼", "식품>건강식품>홍삼", True),)
    ).sync(actor="operator", correlation_id="category-sync-3")
    assert changed.snapshot_id != first.snapshot_id
    assert service.catalog("smartstore").snapshot_id == changed.snapshot_id


def test_invalid_or_nonleaf_catalog_never_replaces_the_current_snapshot(container) -> None:
    good = _service(
        container, (ProviderCategory("50000001", "건강기능식품", "식품>건강기능식품", True),)
    )
    current = good.sync(actor="operator", correlation_id="category-good")
    bad = _service(container, (ProviderCategory("50000009", "식품", "식품", False),))
    with pytest.raises(InputValidationError, match="valid leaf set"):
        bad.sync(actor="operator", correlation_id="category-bad")
    assert good.catalog("smartstore").snapshot_id == current.snapshot_id


def test_selection_refuses_stale_taxonomy_and_unknown_category(container) -> None:
    service = _service(
        container, (ProviderCategory("50000001", "건강기능식품", "식품>건강기능식품", True),)
    )
    current = service.sync(actor="operator", correlation_id="category-select")
    with pytest.raises(InputValidationError) as stale:
        service.require_current_leaf("smartstore", "old-taxonomy", "50000001")
    assert stale.value.code == "CATEGORY_TAXONOMY_NOT_CURRENT"
    with pytest.raises(InputValidationError) as missing:
        service.require_current_leaf("smartstore", current.taxonomy_revision, "99999999")
    assert missing.value.code == "CATEGORY_NOT_CURRENT_LEAF"


def test_sync_and_search_routes_expose_the_durable_catalog(client: TestClient) -> None:
    served = client.app.state.container
    source = Source(
        (
            ProviderCategory("50000001", "건강기능식품", "식품>건강식품>건강기능식품", True),
            ProviderCategory("50000002", "비타민", "식품>건강식품>비타민", True),
        )
    )
    served.category_catalog._source = source

    synced = client.post("/api/v1/connect/marketplaces/smartstore/categories/sync", headers=CLIENT)
    assert synced.status_code == 200, synced.text
    assert synced.json()["total"] == 2
    assert source.calls == 1

    found = client.get(
        "/api/v1/settings/category-catalog/smartstore?query=비타민&limit=10", headers=CLIENT
    )
    assert found.status_code == 200, found.text
    assert [item["category_id"] for item in found.json()["entries"]] == ["50000002"]
