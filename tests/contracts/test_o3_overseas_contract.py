"""ADR-0038 §6 (O3): the cross-border projection's contract — one documented row per wire member.

The SmartStore CREATE projection contract (``ENDPOINT_MATRIX.md`` §4.1.1, the 구매대행 amendment
note) names every cross-border member with its state, source and rule; these tests hold the
document, the adapter and the REGISTER owners to the same set:

- each projected member is spelled by the adapter's own field constants and documented;
- each uncaptured member is a named adapter gap, documented, and never emitted;
- no endpoint, retained field or permission-relevant registry content changed, so the mapping
  revision and the operator's permission attestation are untouched;
- every new reason is inventoried, area-classified, actionable, indexed for review and has its
  Korean copy;
- the order owner does not retain ``individualCustomUniqueCode`` (out of this slice).
"""

import re
from pathlib import Path

from app.capabilities.review.preflight_producer import REVIEWED
from app.interface.screens.actionability import REGISTER_ACTIONS
from app.stages.register.overseas import OVERSEAS_MARKETPLACES, OVERSEAS_REASON_CODES
from app.stages.register.preparation import REASON_CODES
from app.stages.register.reason_areas import REGISTER_AREAS
from integrations.marketplaces.smartstore import product, registry
from integrations.suppliers.base import SupplierProfile
from integrations.suppliers.registry import SOURCE_COUNTRIES, source_country

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = REPO_ROOT / "documents/contracts/platforms/smartstore/ENDPOINT_MATRIX.md"
REGISTER_JS = REPO_ROOT / "ui/web/js/pages/register.js"


def _amendment() -> str:
    text = MATRIX.read_text("utf-8")
    start = text.index("**The 구매대행 cross-border projection**")
    return text[start : text.index("### 4.1.2", start)]


def _row(member: str) -> str:
    rows = [line for line in _amendment().splitlines() if line.startswith(f"| `{member}")]
    assert len(rows) == 1, member
    return rows[0]


def test_the_wire_version_is_the_documented_one() -> None:
    assert product.WIRE_ENCODING_VERSION == "smartstore-register-wire/v11"
    assert f"`{product.WIRE_ENCODING_VERSION}`" in _amendment()
    assert product.OVERSEAS_SECTION_VERSION == "overseas-listing/v1"
    assert f"`{product.OVERSEAS_SECTION_VERSION}`" in _amendment()


def test_each_projected_member_has_its_documented_row() -> None:
    shipping = (
        f"{product.FIELD_ORIGIN_PRODUCT}.{product.FIELD_DELIVERY_INFO}."
        f"{product.FIELD_CLAIM_DELIVERY_INFO}.{product.FIELD_SHIPPING_ADDRESS_ID}"
    )
    customs = (
        f"{product.FIELD_ORIGIN_PRODUCT}.{product.FIELD_DETAIL_ATTRIBUTE}."
        f"{product.FIELD_CUSTOMS_TAX_TYPE}"
    )
    assert "| projected |" in _row(shipping)
    assert "smartstore_overseas_shipping_address_id" in _row(shipping)
    row = _row(customs)
    assert "| projected |" in row
    # FX-04: the duty term of each pinned decision, exactly as the adapter maps it.
    assert dict(product.CUSTOMS_TAX_TYPE_OF_DUTY) == {
        "NOT_OVER_150": "INCLUDED",
        "INCLUDED": "INCLUDED",
        "EXCLUDED": "EXCLUDED",
    }
    for decision, term in product.CUSTOMS_TAX_TYPE_OF_DUTY.items():
        assert f"`{decision}`" in row and f"`{term}`" in row


def test_each_uncaptured_member_is_a_named_gap_and_documented() -> None:
    names = {
        "GAP_OVERSEAS_ORIGIN_IMPORT": product.GAP_OVERSEAS_ORIGIN_IMPORT,
        "GAP_OVERSEAS_KC_EXEMPTION": product.GAP_OVERSEAS_KC_EXEMPTION,
        "GAP_OVERSEAS_CUSTOMS_CLEARANCE": product.GAP_OVERSEAS_CUSTOMS_CLEARANCE,
    }
    assert tuple(names.values()) == product.OVERSEAS_GAPS
    amendment = _amendment()
    for name in names:
        assert f"**gap** (`{name}`)" in amendment, name
    # None of the uncaptured members is on the request allow-list: a document carrying one is
    # refused (WIRE_DOCUMENT_FIELD_UNKNOWN), so the adapter cannot emit it by accident.
    allowed = (
        product._DELIVERY_INFO_KEYS | product._DETAIL_ATTRIBUTE_KEYS | product._ORIGIN_AREA_KEYS
    )
    for never in (
        "businessCustomsClearanceSaleYn",
        "kcCertifiedProductExclusionYn",
        "kcExemptionType",
        "importer",
    ):
        assert never not in allowed


def test_no_endpoint_or_retained_field_changed_so_no_attestation_is_reset() -> None:
    # The cross-border members are request members of the already adopted CREATE: the registry's
    # permission-relevant content still matches the current revision's fingerprint, so neither the
    # revision nor the operator's permission attestation moves (owner #219 6055670693).
    revision = registry.SMARTSTORE_ENDPOINT_MAPPING_REVISION
    assert registry.mapping_fingerprint() == registry.MAPPING_FINGERPRINTS[revision]
    prose = " ".join(_amendment().split())
    assert "endpoint-mapping revision and its fingerprint are unchanged" in prose
    assert "no permission attestation is touched" in prose


def test_the_order_owner_does_not_retain_the_personal_customs_code() -> None:
    for contract in registry.ADOPTED.values():
        assert "individualCustomUniqueCode" not in contract.retained_response_fields
    assert "| `individualCustomUniqueCode` (orders) | not retained |" in _amendment()


def test_every_overseas_reason_is_registered_everywhere_a_reason_must_be() -> None:
    script = REGISTER_JS.read_text("utf-8")
    copy = set(re.findall(r"^\s+(REGISTER_OVERSEAS_[A-Z_]+):", script, flags=re.M))
    assert copy == set(OVERSEAS_REASON_CODES)
    for code in OVERSEAS_REASON_CODES:
        assert code in REASON_CODES
        assert code in REGISTER_AREAS
        assert code in REGISTER_ACTIONS
        # Every overseas reason is REVIEW_REQUIRED, so the review producer indexes each one.
        assert code in REVIEWED
        assert f"`{code}`" in _amendment(), code


def test_the_cross_border_projection_is_smartstores_alone() -> None:
    assert frozenset({"smartstore"}) == OVERSEAS_MARKETPLACES


def test_the_source_country_is_the_supplier_definitions_declaration() -> None:
    # Declared by the supplier, never derived from a currency (ADR-0038 §2, §6).
    assert source_country("superdelivery") == "JP"
    assert source_country("ali1688") == "CN"
    assert source_country("kmretail") is None
    assert source_country("unknown-site") is None
    assert all(re.fullmatch(r"[A-Z]{2}", country) for country in SOURCE_COUNTRIES.values())
    try:
        SupplierProfile(
            "x", "x", "https://x.example", False, frozenset({"x.example"}), country="cn"
        )
    except ValueError:
        pass
    else:  # pragma: no cover - the assertion below names the failure
        raise AssertionError("a lowercase country is not an ISO 3166-1 alpha-2 code")
