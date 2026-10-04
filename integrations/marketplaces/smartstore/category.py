"""Read the official SmartStore leaf-category catalog through the adopted caller."""

from typing import Any

from app.platform.core.errors import PolicyBlockedError
from app.stages.register.category_catalog import ProviderCategory
from integrations.marketplaces.smartstore.caller import (
    CategoryListRequest,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import EndpointId


class SmartStoreCategoryCatalogSource:
    """The provider read only. Persistence and catalog identity belong to REGISTER."""

    def __init__(self, caller: SmartStoreEndpointCaller, bearer: Any) -> None:
        self._caller = caller
        self._bearer = bearer

    def leaf_categories(self) -> tuple[ProviderCategory, ...]:
        bearer = self._bearer()
        if bearer is None:
            raise PolicyBlockedError(
                "SMARTSTORE_SESSION_UNAVAILABLE",
                "a current committed SmartStore session is required to sync categories",
            )
        response = self._caller.call(
            EndpointId.SMARTSTORE_CATEGORY_LIST,
            CategoryListRequest(
                bearer.access_token,
                bearer.credential_generation,
                bearer.session_generation,
                True,
            ),
        )
        raw = response.retained.get("items")
        if not isinstance(raw, list):
            raise PolicyBlockedError(
                "SMARTSTORE_CATEGORY_RESPONSE_INVALID",
                "the category response did not retain the documented array",
            )
        categories: list[ProviderCategory] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict) or item.get("last") is not True:
                raise PolicyBlockedError(
                    "SMARTSTORE_CATEGORY_RESPONSE_NOT_LEAF_ONLY",
                    "the leaf-only category response contained a non-leaf entry",
                )
            category_id = item.get("id")
            name = item.get("name")
            whole = item.get("wholeCategoryName")
            if not all(
                isinstance(value, str) and value.strip() for value in (category_id, name, whole)
            ):
                raise PolicyBlockedError(
                    "SMARTSTORE_CATEGORY_RESPONSE_INVALID",
                    "a category entry is missing a documented field",
                )
            assert isinstance(category_id, str) and isinstance(name, str) and isinstance(whole, str)
            if category_id in seen:
                raise PolicyBlockedError(
                    "SMARTSTORE_CATEGORY_RESPONSE_DUPLICATE",
                    "the category response repeated a category id",
                )
            seen.add(category_id)
            categories.append(
                ProviderCategory(category_id.strip(), name.strip(), whole.strip(), True)
            )
        return tuple(sorted(categories, key=lambda item: item.category_id))


__all__ = ["ProviderCategory", "SmartStoreCategoryCatalogSource"]
