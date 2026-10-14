"""Coupang live-readiness facts kept only in the configured secret store."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.platform.core.secrets import SecretStore
from integrations.marketplaces.coupang.catalog import category_metadata_to_mapping
from integrations.marketplaces.coupang.live_category_metadata import CategoryMetadataAttempt

CATEGORY_RECOMMENDATION_KEY = "marketplace:coupang:category-preflight-v1"
CATEGORY_METADATA_KEY = "marketplace:coupang:category-metadata-v1"
VENDOR_USER_ID_KEY = "marketplace:coupang:vendor-user-id"
LOGISTICS_SELECTION_KEY = "marketplace:coupang:live-logistics-selection-v1"
_CATEGORY_CODE = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class CategoryRecommendationSet:
    category_codes_by_test: tuple[str, ...]

    @property
    def unique_category_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.category_codes_by_test))


class CoupangLivePreflightStore:
    """Parse and atomically replace closed non-credential preflight records in the keyring."""

    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def category_recommendations(self) -> CategoryRecommendationSet:
        raw = self._secrets.get(CATEGORY_RECOMMENDATION_KEY)
        if not raw:
            raise ValueError("COUPANG_CATEGORY_RECOMMENDATIONS_MISSING")
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise ValueError("COUPANG_CATEGORY_RECOMMENDATIONS_INVALID") from exc
        if not isinstance(record, dict) or record.get("v") != 1:
            raise ValueError("COUPANG_CATEGORY_RECOMMENDATIONS_INVALID")
        results = record.get("results")
        if not isinstance(results, dict) or set(results) != {str(i) for i in range(1, 9)}:
            raise ValueError("COUPANG_CATEGORY_RECOMMENDATIONS_INVALID")
        codes: list[str] = []
        for index in range(1, 9):
            result = results[str(index)]
            if not isinstance(result, dict) or result.get("result_type") != "SUCCESS":
                raise ValueError("COUPANG_CATEGORY_RECOMMENDATIONS_INCOMPLETE")
            code = result.get("category_id")
            if not isinstance(code, str) or _CATEGORY_CODE.fullmatch(code) is None:
                raise ValueError("COUPANG_CATEGORY_RECOMMENDATIONS_INVALID")
            codes.append(code)
        return CategoryRecommendationSet(tuple(codes))

    def _metadata_record(self, *, generation: int) -> dict[str, object]:
        raw = self._secrets.get(CATEGORY_METADATA_KEY)
        if not raw:
            return {"v": 1, "credential_generation": generation, "results": {}, "failures": {}}
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise ValueError("COUPANG_CATEGORY_METADATA_SECRET_INVALID") from exc
        if (
            not isinstance(record, dict)
            or record.get("v") != 1
            or record.get("credential_generation") != generation
            or not isinstance(record.get("results"), dict)
            or not isinstance(record.get("failures"), dict)
        ):
            raise ValueError("COUPANG_CATEGORY_METADATA_SECRET_INVALID")
        return record

    def save_category_metadata_attempt(
        self, attempt: CategoryMetadataAttempt, *, generation: int
    ) -> None:
        record = self._metadata_record(generation=generation)
        results = record["results"]
        failures = record["failures"]
        assert isinstance(results, dict) and isinstance(failures, dict)
        if attempt.metadata is not None:
            results[attempt.category_code] = category_metadata_to_mapping(attempt.metadata)
            failures.pop(attempt.category_code, None)
        else:
            failures[attempt.category_code] = {
                name: value
                for name, value in {
                    "error_class": None
                    if attempt.error_class is None
                    else attempt.error_class.value,
                    "error_code": attempt.error_code,
                    "http_status": attempt.http_status,
                }.items()
                if value is not None
            }
        self._secrets.set(
            CATEGORY_METADATA_KEY,
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    def require_category_metadata(
        self, category_codes: tuple[str, ...], *, generation: int
    ) -> dict[str, dict[str, object]]:
        record = self._metadata_record(generation=generation)
        results = record["results"]
        assert isinstance(results, dict)
        if set(category_codes) - set(results):
            raise ValueError("COUPANG_CATEGORY_METADATA_INCOMPLETE")
        selected: dict[str, dict[str, object]] = {}
        for code in category_codes:
            value = results[code]
            if not isinstance(value, dict):
                raise ValueError("COUPANG_CATEGORY_METADATA_SECRET_INVALID")
            selected[code] = value
        return selected

    def vendor_user_id(self) -> str:
        value = self._secrets.get(VENDOR_USER_ID_KEY)
        if not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise ValueError("COUPANG_VENDOR_USER_ID_MISSING")
        return value

    def logistics(self) -> dict[str, object]:
        raw = self._secrets.get(LOGISTICS_SELECTION_KEY)
        if not raw:
            raise ValueError("COUPANG_LOGISTICS_SELECTION_MISSING")
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ValueError("COUPANG_LOGISTICS_SELECTION_INVALID") from exc
        required = {
            "companyContactNumber",
            "deliveryCompanyCode",
            "outboundShippingPlaceCode",
            "returnAddress",
            "returnAddressDetail",
            "returnCenterCode",
            "returnChargeName",
            "returnZipCode",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("COUPANG_LOGISTICS_SELECTION_INVALID")
        if not all(
            isinstance(item, (str, int)) and not isinstance(item, bool) and str(item).strip()
            for item in value.values()
        ):
            raise ValueError("COUPANG_LOGISTICS_SELECTION_INVALID")
        return value


__all__ = [
    "CATEGORY_METADATA_KEY",
    "CATEGORY_RECOMMENDATION_KEY",
    "LOGISTICS_SELECTION_KEY",
    "VENDOR_USER_ID_KEY",
    "CategoryRecommendationSet",
    "CoupangLivePreflightStore",
]
