"""The local Gate 1 owners a registration path needs, set up the way an operator would (Issue #89).

Everything here is invented and provider-zero. The canonical account is established from a
committed M2 binding unit written as an explicit binding would have left it
(``tests.register_support.bind``); the target policy and the reviewed category metadata are
saved through their durable G1-A and G1-B Settings owners, over HTTP, exactly as the Settings
screen saves them. None of it is a ComplianceGate PASS or a provider fact.
"""

from typing import Any

from fastapi.testclient import TestClient

CLIENT = {"X-ICBM-Client": "pytest"}
MARKET = "smartstore"
OPERATOR = "operator-1"
TAXONOMY = "taxonomy-g1-1"
CATEGORY = "50000803"


def pricing_context(account_id: str | None = None) -> dict[str, Any]:
    return {
        "marketplace_key": MARKET,
        "account_id": account_id,
        "fee_table_version": "fee-g1-1",
        "pricing_policy_version": "policy-g1-1",
        "fee_rate": "0.1",
        "fee_fixed_krw": 0,
        "other_cost_rate": "0",
        "other_cost_fixed_krw": 0,
        "cost_rounding": "CEIL_KRW_1",
        "price_rounding": "CEIL_KRW_1",
    }


def save_policy(api: TestClient, account: str, *, account_scoped: bool = False) -> str:
    """The account's target policy through the durable G1-A owner; its new current revision."""
    inputs = {
        "taxonomy_revision": TAXONOMY,
        "pricing_context": pricing_context(account if account_scoped else None),
        "sanitizer_profile_version": "sanitizer-g1-1",
        "asset_policy": {
            "profile": "asset-profile-g1-1",
            "min_images": 1,
            "max_images": 10,
            "requires_representative": True,
            "provider_asset_identity_required": True,
        },
        "templates": {"shipping": "shipping-template-g1", "returns": "returns-template-g1"},
        "duplicate_proof_required": True,
        "duplicate_lookup_keys": ["SELLER_CODE"],
        "category_mapping_revision": None,
        "detail_composition_revision": None,
    }
    current = api.get(f"/api/v1/settings/target-policies/{MARKET}/{account}", headers=CLIENT)
    expected = (current.json().get("current") or {}).get("policy_revision")
    response = api.post(
        f"/api/v1/settings/target-policies/{MARKET}/{account}/revisions",
        json={"actor": OPERATOR, "expected_current_revision": expected, "inputs": inputs},
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    return str(response.json()["current"]["policy_revision"])


def _rule(key: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "key": key,
        "required": False,
        "detail_page_reference_allowed": False,
        "missing_status": "REVIEW_REQUIRED",
        "max_length": None,
    }
    values.update(overrides)
    return values


def record_reviewed_metadata(api: TestClient) -> str:
    """Operator-reviewed metadata of the one category, through the durable G1-B owner; its
    current metadata revision. A non-regulated synthetic category: this is no compliance claim."""
    content = {
        "leaf": True,
        "registrable": True,
        "name_max_length": 100,
        "attributes": [_rule("brand", required=True), _rule("color")],
        "notice": {
            "notice_type": "notice-g1-1",
            "fields": [
                _rule("manufacturer", required=True),
                _rule("origin", required=True, detail_page_reference_allowed=True),
            ],
        },
        "options": {"options_supported": True, "max_options": 5, "max_dimensions": 1},
        "required_templates": ["returns", "shipping"],
    }
    response = api.post(
        f"/api/v1/settings/category-metadata/{MARKET}/{TAXONOMY}/{CATEGORY}/revisions",
        json={
            "actor": OPERATOR,
            "expected_current_revision": None,
            "content_provenance": "OPERATOR_CONFIRMED",
            "evidence_reference": "seller-center/category-50000803/rehearsal",
            "reviewed": True,
            "content": content,
        },
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    return str(response.json()["current"]["metadata_revision"])
