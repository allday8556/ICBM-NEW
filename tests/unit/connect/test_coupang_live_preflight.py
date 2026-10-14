"""Coupang account/category preflight values remain closed records in a SecretStore."""

import json

import pytest

from app.platform.core.errors import ErrorClass
from app.platform.core.secrets import MemorySecretStore
from app.stages.connect.coupang.live_preflight import (
    ACCOUNT_INPUTS_KEY,
    CATEGORY_METADATA_KEY,
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

    store.save_category_metadata_attempt(
        CategoryMetadataAttempt("101", metadata, http_status=200), generation=2
    )
    store.save_category_metadata_attempt(
        CategoryMetadataAttempt("102", None, ErrorClass.TRANSIENT, "COUPANG_TRANSPORT_FAILED", 429),
        generation=2,
    )

    record = json.loads(secrets.get(CATEGORY_METADATA_KEY) or "null")
    assert set(record) == {"v", "credential_generation", "results", "failures"}
    assert record["results"]["101"]["allowed_offer_conditions"] == ["NEW"]
    assert record["failures"]["102"] == {
        "error_class": "TRANSIENT",
        "error_code": "COUPANG_TRANSPORT_FAILED",
        "http_status": 429,
    }
    assert store.require_category_metadata(("101",), generation=2)["101"]


def test_failed_metadata_refresh_invalidates_an_earlier_success() -> None:
    secrets = MemorySecretStore()
    store = CoupangLivePreflightStore(secrets)
    metadata = CategoryMetadata(True, (), (), (), (), ("NEW",))

    store.save_category_metadata_attempt(
        CategoryMetadataAttempt("101", metadata, http_status=200), generation=2
    )
    store.save_category_metadata_attempt(
        CategoryMetadataAttempt("101", None, ErrorClass.TRANSIENT, "COUPANG_TRANSPORT_FAILED"),
        generation=2,
    )

    record = json.loads(secrets.get(CATEGORY_METADATA_KEY) or "null")
    assert "101" not in record["results"]
    assert record["failures"]["101"] == {
        "error_class": "TRANSIENT",
        "error_code": "COUPANG_TRANSPORT_FAILED",
    }
    with pytest.raises(ValueError, match="METADATA_INCOMPLETE"):
        store.require_category_metadata(("101",), generation=2)


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
