"""Coupang live-readiness facts kept only in the configured secret store."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.platform.core.secrets import SecretStore
from integrations.marketplaces.coupang.auth import Credentials
from integrations.marketplaces.coupang.catalog import category_metadata_to_mapping
from integrations.marketplaces.coupang.live_category_metadata import CategoryMetadataAttempt

CATEGORY_RECOMMENDATION_KEY = "marketplace:coupang:category-preflight-v1"
CATEGORY_METADATA_KEY = "marketplace:coupang:category-metadata-v1"
VENDOR_USER_ID_KEY = "marketplace:coupang:vendor-user-id"
LOGISTICS_SELECTION_KEY = "marketplace:coupang:live-logistics-selection-v1"
ACCOUNT_INPUTS_KEY = "marketplace:coupang:live-account-inputs-v2"
_CATEGORY_CODE = re.compile(r"^[0-9]+$")
_ACCOUNT_RECORD_VERSION = 2


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

    def _account_record(self, credentials: Credentials) -> dict[str, object]:
        raw = self._secrets.get(ACCOUNT_INPUTS_KEY)
        if not raw:
            raise ValueError("COUPANG_ACCOUNT_INPUTS_MISSING")
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise ValueError("COUPANG_ACCOUNT_INPUTS_INVALID") from exc
        if (
            not isinstance(record, dict)
            or record.get("v") != _ACCOUNT_RECORD_VERSION
            or record.get("credential_generation") != credentials.credential_generation
            or record.get("vendor_id") != credentials.vendor_id
        ):
            raise ValueError("COUPANG_ACCOUNT_INPUTS_ACCOUNT_MISMATCH")
        return record

    def bind_account_values(
        self,
        *,
        credentials: Credentials,
        vendor_user_id: str,
        logistics: dict[str, object],
    ) -> None:
        """Atomically replace account inputs in one record bound to active credentials.

        Callers must obtain both values from an authenticated/operator-confirmed preflight.
        Legacy unbound values are intentionally never accepted by the read methods.
        """
        vendor_user_id = vendor_user_id.strip()
        if not vendor_user_id or len(vendor_user_id) > 100:
            raise ValueError("COUPANG_VENDOR_USER_ID_MISSING")
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
        if set(logistics) != required:
            raise ValueError("COUPANG_LOGISTICS_SELECTION_INVALID")
        if not all(
            isinstance(item, (str, int)) and not isinstance(item, bool) and str(item).strip()
            for item in logistics.values()
        ):
            raise ValueError("COUPANG_LOGISTICS_SELECTION_INVALID")
        self._secrets.set(
            ACCOUNT_INPUTS_KEY,
            json.dumps(
                {
                    "v": _ACCOUNT_RECORD_VERSION,
                    "credential_generation": credentials.credential_generation,
                    "vendor_id": credentials.vendor_id,
                    "vendor_user_id": vendor_user_id,
                    "logistics": logistics,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def vendor_user_id(self, *, credentials: Credentials) -> str:
        record = self._account_record(credentials)
        value = record.get("vendor_user_id")
        if not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise ValueError("COUPANG_VENDOR_USER_ID_INVALID")
        return value

    def logistics(self, *, credentials: Credentials) -> dict[str, object]:
        record = self._account_record(credentials)
        value = record.get("logistics")
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
    "ACCOUNT_INPUTS_KEY",
    "CATEGORY_METADATA_KEY",
    "CATEGORY_RECOMMENDATION_KEY",
    "LOGISTICS_SELECTION_KEY",
    "VENDOR_USER_ID_KEY",
    "CategoryRecommendationSet",
    "CoupangLivePreflightStore",
]
