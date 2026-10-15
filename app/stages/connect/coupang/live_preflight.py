"""Coupang live-readiness facts kept only in the configured secret store."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.platform.core.secrets import SecretStore
from integrations.marketplaces.coupang.auth import Credentials
from integrations.marketplaces.coupang.catalog import category_metadata_to_mapping
from integrations.marketplaces.coupang.live_category_metadata import CategoryMetadataAttempt

CATEGORY_RECOMMENDATION_KEY = "marketplace:coupang:category-preflight-v1"
CATEGORY_METADATA_KEY = "marketplace:coupang:category-metadata-v1"
CATEGORY_METADATA_ATTEMPT_KEY = "marketplace:coupang:category-metadata-attempt-v2"
VENDOR_USER_ID_KEY = "marketplace:coupang:vendor-user-id"
LOGISTICS_SELECTION_KEY = "marketplace:coupang:live-logistics-selection-v1"
ACCOUNT_INPUTS_KEY = "marketplace:coupang:live-account-inputs-v2"
_CATEGORY_CODE = re.compile(r"^[0-9]+$")
_ACCOUNT_RECORD_VERSION = 2
_CATEGORY_ATTEMPT_RECORD_VERSION = 2
_CATEGORY_RECOVERY_SCOPE = "C-LIVE-CATEGORY-RECOVERY-1"
_CATEGORY_RECOVERY_WINDOW = timedelta(minutes=30)
_SHARED_COURIER_CODES = frozenset({"CJGLS", "EPOST", "HANJIN", "HYUNDAI", "ILYANG", "KGB"})


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

    @staticmethod
    def _category_attempt_key(category_code: str, generation: int) -> str:
        if _CATEGORY_CODE.fullmatch(category_code) is None or generation < 1:
            raise ValueError("COUPANG_CATEGORY_METADATA_ATTEMPT_ID_INVALID")
        return f"{CATEGORY_METADATA_ATTEMPT_KEY}:{generation}:{category_code}"

    @staticmethod
    def _serialize(record: dict[str, object]) -> str:
        return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _legacy_metadata_record(self, *, generation: int) -> dict[str, object]:
        raw = self._secrets.get_large(CATEGORY_METADATA_KEY)
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

    def _category_attempt_record(
        self, category_code: str, *, generation: int
    ) -> dict[str, object] | None:
        key = self._category_attempt_key(category_code, generation)
        raw = self._secrets.get_large(key)
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise ValueError("COUPANG_CATEGORY_METADATA_ATTEMPT_SECRET_INVALID") from exc
        if (
            not isinstance(record, dict)
            or record.get("v") != _CATEGORY_ATTEMPT_RECORD_VERSION
            or record.get("credential_generation") != generation
            or record.get("category_code") != category_code
            or record.get("state") not in {"inflight", "succeeded", "failed"}
            or not isinstance(record.get("attempt_no"), int)
            or isinstance(record.get("attempt_no"), bool)
            or record["attempt_no"] not in {1, 2}
        ):
            raise ValueError("COUPANG_CATEGORY_METADATA_ATTEMPT_SECRET_INVALID")
        state = record["state"]
        if state == "succeeded" and not isinstance(record.get("metadata"), dict):
            raise ValueError("COUPANG_CATEGORY_METADATA_ATTEMPT_SECRET_INVALID")
        if state == "failed" and not isinstance(record.get("failure"), dict):
            raise ValueError("COUPANG_CATEGORY_METADATA_ATTEMPT_SECRET_INVALID")
        return record

    def begin_category_metadata_attempt(self, category_code: str, *, generation: int) -> None:
        """Durably consume the one ordinary provider read before transport starts."""
        key = self._category_attempt_key(category_code, generation)
        legacy = self._legacy_metadata_record(generation=generation)
        legacy_results = legacy["results"]
        legacy_failures = legacy["failures"]
        assert isinstance(legacy_results, dict) and isinstance(legacy_failures, dict)
        if self._category_attempt_record(category_code, generation=generation) is not None:
            raise ValueError("COUPANG_CATEGORY_METADATA_ATTEMPT_ALREADY_RECORDED")
        if category_code in legacy_results or category_code in legacy_failures:
            raise ValueError("COUPANG_CATEGORY_METADATA_ATTEMPT_ALREADY_RECORDED")
        self._secrets.set_large(
            key,
            self._serialize(
                {
                    "v": _CATEGORY_ATTEMPT_RECORD_VERSION,
                    "credential_generation": generation,
                    "category_code": category_code,
                    "state": "inflight",
                    "attempt_no": 1,
                }
            ),
        )

    def mark_legacy_category_metadata_ambiguous(
        self, category_code: str, *, generation: int
    ) -> None:
        """Record the known pre-journal provider read without performing another provider call."""
        key = self._category_attempt_key(category_code, generation)
        legacy = self._legacy_metadata_record(generation=generation)
        legacy_results = legacy["results"]
        legacy_failures = legacy["failures"]
        assert isinstance(legacy_results, dict) and isinstance(legacy_failures, dict)
        if self._category_attempt_record(category_code, generation=generation) is not None:
            raise ValueError("COUPANG_CATEGORY_METADATA_ATTEMPT_ALREADY_RECORDED")
        if category_code in legacy_results or category_code in legacy_failures:
            raise ValueError("COUPANG_CATEGORY_METADATA_LEGACY_MARK_NOT_ALLOWED")
        self._secrets.set_large(
            key,
            self._serialize(
                {
                    "v": _CATEGORY_ATTEMPT_RECORD_VERSION,
                    "credential_generation": generation,
                    "category_code": category_code,
                    "state": "inflight",
                    "attempt_no": 1,
                    "origin": "legacy-unrecorded-provider-response",
                }
            ),
        )

    def category_metadata_recovery_target(
        self, category_codes: tuple[str, ...], *, generation: int
    ) -> str:
        ambiguous = [
            code
            for code in category_codes
            if (
                (record := self._category_attempt_record(code, generation=generation)) is not None
                and record["state"] == "inflight"
                and record["attempt_no"] == 1
            )
        ]
        if len(ambiguous) != 1:
            raise ValueError("COUPANG_CATEGORY_METADATA_RECOVERY_TARGET_NOT_UNIQUE")
        return ambiguous[0]

    def begin_category_metadata_recovery(
        self,
        category_code: str,
        *,
        generation: int,
        scope_id: str,
        approved_at: datetime,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        """Consume one explicit recovery approval before the single recovery transport."""
        instants = (approved_at, expires_at, now)
        if any(value.tzinfo is None or value.utcoffset() is None for value in instants):
            raise ValueError("COUPANG_CATEGORY_METADATA_RECOVERY_TIME_INVALID")
        approved_at, expires_at, now = (value.astimezone(UTC) for value in instants)
        if (
            scope_id != _CATEGORY_RECOVERY_SCOPE
            or not approved_at <= now <= expires_at
            or expires_at - approved_at > _CATEGORY_RECOVERY_WINDOW
        ):
            raise ValueError("COUPANG_CATEGORY_METADATA_RECOVERY_APPROVAL_INVALID")
        record = self._category_attempt_record(category_code, generation=generation)
        if record is None or record["state"] != "inflight" or record["attempt_no"] != 1:
            raise ValueError("COUPANG_CATEGORY_METADATA_RECOVERY_NOT_ALLOWED")
        self._secrets.set_large(
            self._category_attempt_key(category_code, generation),
            self._serialize(
                {
                    "v": _CATEGORY_ATTEMPT_RECORD_VERSION,
                    "credential_generation": generation,
                    "category_code": category_code,
                    "state": "inflight",
                    "attempt_no": 2,
                    "recovery_scope_id": scope_id,
                    "recovery_approved_at": approved_at.isoformat(),
                    "recovery_expires_at": expires_at.isoformat(),
                }
            ),
        )

    def require_category_metadata_attempt_inflight(
        self, category_code: str, *, generation: int, attempt_no: int
    ) -> None:
        record = self._category_attempt_record(category_code, generation=generation)
        if record is None or record["state"] != "inflight" or record["attempt_no"] != attempt_no:
            raise ValueError("COUPANG_CATEGORY_METADATA_ATTEMPT_NOT_INFLIGHT")

    def pending_category_metadata_codes(
        self, category_codes: tuple[str, ...], *, generation: int
    ) -> tuple[str, ...]:
        """Return never-called codes, refusing every ambiguous or failed recorded attempt."""
        legacy = self._legacy_metadata_record(generation=generation)
        legacy_results = legacy["results"]
        legacy_failures = legacy["failures"]
        assert isinstance(legacy_results, dict) and isinstance(legacy_failures, dict)
        pending: list[str] = []
        for code in category_codes:
            record = self._category_attempt_record(code, generation=generation)
            if record is not None:
                if record["state"] == "inflight":
                    raise ValueError("COUPANG_CATEGORY_METADATA_RECOVERY_REQUIRED")
                if record["state"] == "failed":
                    raise ValueError("COUPANG_CATEGORY_METADATA_FAILURE_RECORDED")
                continue
            if code in legacy_failures:
                raise ValueError("COUPANG_CATEGORY_METADATA_FAILURE_RECORDED")
            if code not in legacy_results:
                pending.append(code)
        return tuple(pending)

    def save_category_metadata_attempt(
        self, attempt: CategoryMetadataAttempt, *, generation: int
    ) -> None:
        current = self._category_attempt_record(attempt.category_code, generation=generation)
        if current is None or current["state"] != "inflight":
            raise ValueError("COUPANG_CATEGORY_METADATA_ATTEMPT_NOT_INFLIGHT")
        record: dict[str, object] = {
            "v": _CATEGORY_ATTEMPT_RECORD_VERSION,
            "credential_generation": generation,
            "category_code": attempt.category_code,
            "attempt_no": current["attempt_no"],
        }
        if attempt.metadata is not None:
            record["state"] = "succeeded"
            record["metadata"] = category_metadata_to_mapping(attempt.metadata)
        else:
            record["state"] = "failed"
            record["failure"] = {
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
        self._secrets.set_large(
            self._category_attempt_key(attempt.category_code, generation),
            self._serialize(record),
        )

    def require_category_metadata(
        self, category_codes: tuple[str, ...], *, generation: int
    ) -> dict[str, dict[str, object]]:
        legacy = self._legacy_metadata_record(generation=generation)
        legacy_results = legacy["results"]
        assert isinstance(legacy_results, dict)
        selected: dict[str, dict[str, object]] = {}
        for code in category_codes:
            record = self._category_attempt_record(code, generation=generation)
            value = (
                record.get("metadata")
                if record is not None and record["state"] == "succeeded"
                else legacy_results.get(code)
            )
            if not isinstance(value, dict):
                raise ValueError("COUPANG_CATEGORY_METADATA_INCOMPLETE")
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

    def bind_legacy_account_values_from_saved_courier(
        self,
        *,
        credentials: Credentials,
        delivery_company_code: str,
    ) -> None:
        """Bind existing WING/logistics inputs using one explicitly selected saved courier.

        The courier is not a credential.  It comes from a revisioned delivery policy selected by
        the caller, while the remaining account-only values stay inside the OS secret store.  A
        code is accepted only when both adopted marketplace contracts use the exact same token;
        no translation, fallback or default is attempted.
        """
        courier = delivery_company_code.strip()
        if courier not in _SHARED_COURIER_CODES:
            raise ValueError("COUPANG_SAVED_COURIER_UNSUPPORTED")
        vendor_user_id = self._secrets.get(VENDOR_USER_ID_KEY)
        raw = self._secrets.get(LOGISTICS_SELECTION_KEY)
        if not vendor_user_id or not raw:
            raise ValueError("COUPANG_LEGACY_ACCOUNT_INPUTS_MISSING")
        try:
            logistics = json.loads(raw)
        except ValueError as exc:
            raise ValueError("COUPANG_LOGISTICS_SELECTION_INVALID") from exc
        if not isinstance(logistics, dict):
            raise ValueError("COUPANG_LOGISTICS_SELECTION_INVALID")
        existing = logistics.get("deliveryCompanyCode")
        if existing not in (None, "", courier):
            raise ValueError("COUPANG_SAVED_COURIER_CONFLICT")
        selected = dict(logistics)
        selected["deliveryCompanyCode"] = courier
        self.bind_account_values(
            credentials=credentials,
            vendor_user_id=vendor_user_id,
            logistics=selected,
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
    "CATEGORY_METADATA_ATTEMPT_KEY",
    "CATEGORY_METADATA_KEY",
    "CATEGORY_RECOMMENDATION_KEY",
    "LOGISTICS_SELECTION_KEY",
    "VENDOR_USER_ID_KEY",
    "CategoryRecommendationSet",
    "CoupangLivePreflightStore",
]
