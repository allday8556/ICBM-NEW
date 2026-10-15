"""Coupang account/category preflight values remain closed records in a SecretStore."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.platform.core.errors import ErrorClass
from app.platform.core.secrets import MemorySecretStore
from app.stages.connect.coupang.live_preflight import (
    ACCOUNT_INPUTS_KEY,
    CATEGORY_METADATA_ATTEMPT_KEY,
    CATEGORY_RECOMMENDATION_KEY,
    LOGISTICS_SELECTION_KEY,
    VENDOR_USER_ID_KEY,
    CoupangLivePreflightStore,
)
from integrations.marketplaces.coupang.auth import Credentials
from integrations.marketplaces.coupang.catalog import CategoryMetadata
from integrations.marketplaces.coupang.live_category_metadata import CategoryMetadataAttempt


def _recommendations() -> str:
    return json.dumps(
        {
            "v": 1,
            "results": {
                str(index): {
                    "result_type": "SUCCESS",
                    "category_id": str(100 + (index % 3)),
                    "category_name": "테스트",
                }
                for index in range(1, 9)
            },
        }
    )


def test_recommendations_are_exactly_eight_and_keep_unique_order() -> None:
    secrets = MemorySecretStore()
    secrets.set(CATEGORY_RECOMMENDATION_KEY, _recommendations())

    result = CoupangLivePreflightStore(secrets).category_recommendations()

    assert len(result.category_codes_by_test) == 8
    assert result.unique_category_codes == ("101", "102", "100")


def test_metadata_attempt_is_saved_immediately_without_raw_response() -> None:
    secrets = MemorySecretStore()
    store = CoupangLivePreflightStore(secrets)
    metadata = CategoryMetadata(True, (), (), (), (), ("NEW",))

    store.begin_category_metadata_attempt("101", generation=2)
    store.save_category_metadata_attempt(
        CategoryMetadataAttempt("101", metadata, http_status=200), generation=2
    )
    store.begin_category_metadata_attempt("102", generation=2)
    store.save_category_metadata_attempt(
        CategoryMetadataAttempt("102", None, ErrorClass.TRANSIENT, "COUPANG_TRANSPORT_FAILED", 429),
        generation=2,
    )

    success = json.loads(secrets.get(f"{CATEGORY_METADATA_ATTEMPT_KEY}:2:101") or "null")
    failure = json.loads(secrets.get(f"{CATEGORY_METADATA_ATTEMPT_KEY}:2:102") or "null")
    assert success["state"] == "succeeded"
    assert success["metadata"]["allowed_offer_conditions"] == ["NEW"]
    assert failure["state"] == "failed"
    assert failure["failure"] == {
        "error_class": "TRANSIENT",
        "error_code": "COUPANG_TRANSPORT_FAILED",
        "http_status": 429,
    }
    assert store.require_category_metadata(("101",), generation=2)["101"]


def test_failed_metadata_refresh_invalidates_an_earlier_success() -> None:
    secrets = MemorySecretStore()
    store = CoupangLivePreflightStore(secrets)

    store.begin_category_metadata_attempt("101", generation=2)
    store.save_category_metadata_attempt(
        CategoryMetadataAttempt("101", None, ErrorClass.TRANSIENT, "COUPANG_TRANSPORT_FAILED"),
        generation=2,
    )

    record = json.loads(secrets.get(f"{CATEGORY_METADATA_ATTEMPT_KEY}:2:101") or "null")
    assert record["state"] == "failed"
    assert record["failure"] == {
        "error_class": "TRANSIENT",
        "error_code": "COUPANG_TRANSPORT_FAILED",
    }
    with pytest.raises(ValueError, match="METADATA_INCOMPLETE"):
        store.require_category_metadata(("101",), generation=2)


def test_inflight_barrier_survives_result_storage_failure() -> None:
    class FailingStore(MemorySecretStore):
        writes = 0

        def set_large(self, key: str, value: str) -> None:
            self.writes += 1
            if self.writes == 2:
                raise OSError("simulated result write failure")
            super().set_large(key, value)

    secrets = FailingStore()
    store = CoupangLivePreflightStore(secrets)
    store.begin_category_metadata_attempt("101", generation=2)

    with pytest.raises(OSError, match="simulated"):
        store.save_category_metadata_attempt(
            CategoryMetadataAttempt("101", CategoryMetadata(True, (), (), (), (), ("NEW",))),
            generation=2,
        )

    record = json.loads(secrets.get(f"{CATEGORY_METADATA_ATTEMPT_KEY}:2:101") or "null")
    assert record["state"] == "inflight"
    with pytest.raises(ValueError, match="RECOVERY_REQUIRED"):
        store.pending_category_metadata_codes(("101", "102"), generation=2)


def test_one_bounded_recovery_is_consumed_before_result_storage() -> None:
    now = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)
    secrets = MemorySecretStore()
    store = CoupangLivePreflightStore(secrets)
    store.mark_legacy_category_metadata_ambiguous("101", generation=2)

    assert store.category_metadata_recovery_target(("101", "102"), generation=2) == "101"
    store.begin_category_metadata_recovery(
        "101",
        generation=2,
        scope_id="C-LIVE-CATEGORY-RECOVERY-1",
        approved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=5),
        now=now,
    )
    store.require_category_metadata_attempt_inflight("101", generation=2, attempt_no=2)
    with pytest.raises(ValueError, match="RECOVERY_NOT_ALLOWED"):
        store.begin_category_metadata_recovery(
            "101",
            generation=2,
            scope_id="C-LIVE-CATEGORY-RECOVERY-1",
            approved_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=5),
            now=now,
        )


def test_completed_shard_is_skipped_while_never_called_code_remains_pending() -> None:
    secrets = MemorySecretStore()
    store = CoupangLivePreflightStore(secrets)
    store.begin_category_metadata_attempt("101", generation=2)
    store.save_category_metadata_attempt(
        CategoryMetadataAttempt(
            "101", CategoryMetadata(True, (), (), (), (), ("NEW",)), http_status=200
        ),
        generation=2,
    )

    assert store.pending_category_metadata_codes(("101", "102"), generation=2) == ("102",)


def test_legacy_result_cannot_be_marked_or_read_again() -> None:
    secrets = MemorySecretStore()
    secrets.set_large(
        "marketplace:coupang:category-metadata-v1",
        json.dumps(
            {
                "v": 1,
                "credential_generation": 2,
                "results": {
                    "101": {
                        "is_allow_single_item": True,
                        "attributes": [],
                        "notice_categories": [],
                        "required_document_names": [],
                        "certifications": [],
                        "allowed_offer_conditions": ["NEW"],
                    }
                },
                "failures": {},
            }
        ),
    )
    store = CoupangLivePreflightStore(secrets)

    assert store.pending_category_metadata_codes(("101", "102"), generation=2) == ("102",)
    with pytest.raises(ValueError, match="LEGACY_MARK_NOT_ALLOWED"):
        store.mark_legacy_category_metadata_ambiguous("101", generation=2)
    with pytest.raises(ValueError, match="ATTEMPT_ALREADY_RECORDED"):
        store.begin_category_metadata_attempt("101", generation=2)


def test_incomplete_metadata_and_malformed_recommendations_fail_closed() -> None:
    secrets = MemorySecretStore()
    store = CoupangLivePreflightStore(secrets)
    secrets.set(CATEGORY_RECOMMENDATION_KEY, '{"v":1,"results":{}}')

    with pytest.raises(ValueError, match="RECOMMENDATIONS_INVALID"):
        store.category_recommendations()
    with pytest.raises(ValueError, match="METADATA_INCOMPLETE"):
        store.require_category_metadata(("101",), generation=1)


def _logistics() -> dict[str, object]:
    return {
        "companyContactNumber": "01000000000",
        "deliveryCompanyCode": "CJGLS",
        "outboundShippingPlaceCode": "1",
        "returnAddress": "부산광역시",
        "returnAddressDetail": "테스트",
        "returnCenterCode": "2",
        "returnChargeName": "테스트 반품지",
        "returnZipCode": "00000",
    }


def test_account_values_are_bound_to_credential_generation_and_vendor() -> None:
    secrets = MemorySecretStore()
    store = CoupangLivePreflightStore(secrets)
    credentials = Credentials("access", "secret", "vendor-1", 3)

    store.bind_account_values(
        credentials=credentials,
        vendor_user_id="wing-user",
        logistics=_logistics(),
    )

    assert store.vendor_user_id(credentials=credentials) == "wing-user"
    assert store.logistics(credentials=credentials) == _logistics()
    record = json.loads(secrets.get(ACCOUNT_INPUTS_KEY) or "null")
    assert record["v"] == 2
    assert record["credential_generation"] == 3
    assert record["vendor_id"] == "vendor-1"


@pytest.mark.parametrize(
    "credentials",
    [
        Credentials("access", "secret", "vendor-1", 4),
        Credentials("access", "secret", "vendor-2", 3),
    ],
)
def test_account_values_fail_closed_for_other_generation_or_vendor(
    credentials: Credentials,
) -> None:
    secrets = MemorySecretStore()
    store = CoupangLivePreflightStore(secrets)
    store.bind_account_values(
        credentials=Credentials("access", "secret", "vendor-1", 3),
        vendor_user_id="wing-user",
        logistics=_logistics(),
    )

    with pytest.raises(ValueError, match="ACCOUNT_MISMATCH"):
        store.vendor_user_id(credentials=credentials)
    with pytest.raises(ValueError, match="ACCOUNT_MISMATCH"):
        store.logistics(credentials=credentials)


def test_legacy_unbound_account_values_are_never_accepted() -> None:
    secrets = MemorySecretStore()
    secrets.set(VENDOR_USER_ID_KEY, "wing-user")
    secrets.set(LOGISTICS_SELECTION_KEY, json.dumps(_logistics()))
    store = CoupangLivePreflightStore(secrets)
    credentials = Credentials("access", "secret", "vendor-1", 3)

    with pytest.raises(ValueError, match="ACCOUNT_INPUTS_MISSING"):
        store.vendor_user_id(credentials=credentials)
    with pytest.raises(ValueError, match="ACCOUNT_INPUTS_MISSING"):
        store.logistics(credentials=credentials)


def test_saved_courier_explicitly_completes_legacy_account_inputs() -> None:
    secrets = MemorySecretStore()
    logistics = _logistics()
    logistics["deliveryCompanyCode"] = ""
    secrets.set(VENDOR_USER_ID_KEY, "wing-user")
    secrets.set(LOGISTICS_SELECTION_KEY, json.dumps(logistics))
    store = CoupangLivePreflightStore(secrets)
    credentials = Credentials("access", "secret", "vendor-1", 3)

    store.bind_legacy_account_values_from_saved_courier(
        credentials=credentials,
        delivery_company_code="CJGLS",
    )

    assert store.vendor_user_id(credentials=credentials) == "wing-user"
    assert store.logistics(credentials=credentials) == _logistics()


@pytest.mark.parametrize("delivery_company_code", ["", "UNKNOWN", "KDEXP"])
def test_saved_courier_binding_rejects_unreviewed_codes(
    delivery_company_code: str,
) -> None:
    secrets = MemorySecretStore()
    logistics = _logistics()
    logistics["deliveryCompanyCode"] = ""
    secrets.set(VENDOR_USER_ID_KEY, "wing-user")
    secrets.set(LOGISTICS_SELECTION_KEY, json.dumps(logistics))

    with pytest.raises(ValueError, match="SAVED_COURIER_UNSUPPORTED"):
        CoupangLivePreflightStore(secrets).bind_legacy_account_values_from_saved_courier(
            credentials=Credentials("access", "secret", "vendor-1", 3),
            delivery_company_code=delivery_company_code,
        )


def test_saved_courier_binding_rejects_conflicting_legacy_value() -> None:
    secrets = MemorySecretStore()
    secrets.set(VENDOR_USER_ID_KEY, "wing-user")
    secrets.set(LOGISTICS_SELECTION_KEY, json.dumps(_logistics()))

    with pytest.raises(ValueError, match="SAVED_COURIER_CONFLICT"):
        CoupangLivePreflightStore(secrets).bind_legacy_account_values_from_saved_courier(
            credentials=Credentials("access", "secret", "vendor-1", 3),
            delivery_company_code="HANJIN",
        )
