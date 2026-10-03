"""The local Gate 1 owners a registration path needs, set up the way an operator would (Issue #89).

Everything here is invented and provider-zero. The canonical account is established from a
committed M2 binding unit written as an explicit binding would have left it
(``tests.support.register_support.bind``); the target policy and the reviewed category metadata are
saved through their durable G1-A and G1-B Settings owners, over HTTP, exactly as the Settings
screen saves them. None of it is a ComplianceGate PASS or a provider fact.
"""

from typing import Any

from fastapi.testclient import TestClient

from app.container import Container
from app.stages.register.target_policy import (
    TargetPolicyInputsView,
    TargetPolicyStore,
    encode_content,
)

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


def policy_inputs(account: str, *, account_scoped: bool = False) -> dict[str, Any]:
    """What Settings sends: both authoring revisions are server-owned, so always ``null``."""
    return {
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


def save_policy(api: TestClient, account: str, *, account_scoped: bool = False) -> str:
    """The account's target policy through the durable G1-A owner; its new current revision. The
    server stamps the authoring-revision owner's current revisions into it (ADR-0014 §27.1)."""
    inputs = policy_inputs(account, account_scoped=account_scoped)
    current = api.get(f"/api/v1/settings/target-policies/{MARKET}/{account}", headers=CLIENT)
    expected = (current.json().get("current") or {}).get("policy_revision")
    response = api.post(
        f"/api/v1/settings/target-policies/{MARKET}/{account}/revisions",
        json={"actor": OPERATOR, "expected_current_revision": expected, "inputs": inputs},
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    return str(response.json()["current"]["policy_revision"])


def save_unowned_policy(container: Container, account: str) -> str:
    """A target-policy revision as one appended **before the authoring-revision owners existed**
    left it: both authoring revisions ``null``. Nothing backfills such a revision (Issue #89
    ``5907626428`` D3), so it is written through the store without the server's stamping."""
    content = encode_content(
        MARKET, account, TargetPolicyInputsView.model_validate(policy_inputs(account))
    )
    store = TargetPolicyStore(
        container.db, container.clock, container.audit, container.authoring_revisions
    )
    current = store.current(MARKET, account)
    return store.append(
        MARKET,
        account,
        content,
        expected_current_revision=None if current is None else current.policy_revision,
        authored_by=OPERATOR,
        correlation_id="cid-unowned-policy",
    ).policy_revision


def owned_revisions(container: Container, account: str) -> tuple[str | None, str | None]:
    """The account's current server-owned authoring revisions, as the authoring form echoes them:
    ``(category mapping, detail composition)``."""
    policy = container.registration_preflight.target_policy(MARKET, account)
    assert policy is not None
    return policy.category_mapping_revision, policy.detail_composition_revision


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


# A reviewed ETC (기타 재화) notice the API stores: every field the provider schema requires,
# and one field left to the provider's own "상품상세 참조".
ETC_NOTICES: dict[str, Any] = {
    "itemName": {"value": "합성 품명"},
    "modelName": {"value": "합성 모델"},
    "manufacturer": {"value": "합성 제조사"},
    "afterServiceDirector": {"value": "합성 A/S 책임자"},
    "returnCostReason": {"detail_page_reference": True},
}
# The ETC fields the provider notice schema declares, in its order.
ETC_NOTICE_KEYS: list[str] = [
    "returnCostReason",
    "noRefundReason",
    "qualityAssuranceStandard",
    "compensationProcedure",
    "troubleShootingContents",
    "itemName",
    "modelName",
    "certificateDetails",
    "manufacturer",
    "afterServiceDirector",
    "customerServicePhoneNumber",
]


def fill_etc_notice(unit: Any) -> None:
    """Fill the reviewed ETC notice on the authoring screen of one unit."""
    for key in ("itemName", "modelName", "manufacturer", "afterServiceDirector"):
        unit.locator(f"input[name='notice.{key}']").fill(ETC_NOTICES[key]["value"])
    unit.locator("input[data-detail-reference='notice'][data-field-key='returnCostReason']").check()


def record_reviewed_metadata(
    api: TestClient, notice_type: str = "ETC", expected: str | None = None
) -> str:
    """Operator-reviewed metadata of the one category, through the durable G1-B owner; its
    current metadata revision. A synthetic category: this is no compliance claim. ``expected`` is
    the current revision a further save edits from."""
    content = {
        "leaf": True,
        "registrable": True,
        "name_max_length": 100,
        "attributes": [_rule("brand", required=True), _rule("color")],
        # A SmartStore category's notice fields are the provider notice schema's own for the
        # reviewed type (notice coverage S3); the type is what the reviewed metadata selects.
        "notice": {"notice_type": notice_type, "fields": []},
        "options": {"options_supported": True, "max_options": 5, "max_dimensions": 1},
        "required_templates": ["returns", "shipping"],
    }
    response = api.post(
        f"/api/v1/settings/category-metadata/{MARKET}/{TAXONOMY}/{CATEGORY}/revisions",
        json={
            "actor": OPERATOR,
            "expected_current_revision": expected,
            "content_provenance": "OPERATOR_CONFIRMED",
            "evidence_reference": "seller-center/category-50000803/rehearsal",
            "reviewed": True,
            "content": content,
        },
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    return str(response.json()["current"]["metadata_revision"])
