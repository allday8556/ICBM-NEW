"""ADR-0038 §6 (O3): the cross-border projection's contract — one documented row per wire member.

The SmartStore CREATE projection contract (``ENDPOINT_MATRIX.md`` §4.1.1, the 구매대행 amendment
note) names every cross-border member with its state, source and rule; these tests hold the
document, the adapter and the REGISTER owners to the same set:

- each projected member is spelled by the adapter's own field constants and documented;
- each uncaptured member is a named adapter gap, documented, and never emitted;
- each member O3b projects from the captured 2.90.1 reference (``SOURCES.md`` §5.11,
  ``NAVER-P0-CROSS-BORDER-2901``) is spelled and valued exactly as captured;
- no endpoint, retained field or permission-relevant registry content changed, so the mapping
  revision and the operator's permission attestation are untouched;
- every new reason is inventoried, area-classified, actionable, indexed for review and has its
  Korean copy;
- the order owner does not retain ``individualCustomUniqueCode`` (out of this slice).
"""

import hashlib
import json
import re
from pathlib import Path

from app.capabilities.review.preflight_producer import REVIEWED
from app.interface.screens.actionability import REGISTER_ACTIONS
from app.stages.register.overseas import OVERSEAS_MARKETPLACES, OVERSEAS_REASON_CODES
from app.stages.register.preparation import REASON_CODES
from app.stages.register.reason_areas import REGISTER_AREAS
from integrations.marketplaces.smartstore import product, registry
from integrations.suppliers import ali1688, kmretail, superdelivery
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
    assert product.WIRE_ENCODING_VERSION == "smartstore-register-wire/v12"
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


# The captured 2.90.1 reference, exactly (SOURCES.md §5.11): the member, its path, its type and
# its enum. A change of the reference is a change of this table and of the adapter together.
CAPTURED: dict[str, tuple[str, str, tuple[str, ...] | None]] = {
    "customsTaxType": (
        "originProduct.detailAttribute.customsTaxType",
        "string",
        ("NOT_APPLICABLE", "INCLUDED", "EXCLUDED"),
    ),
    "businessCustomsClearanceSaleYn": (
        "originProduct.deliveryInfo.businessCustomsClearanceSaleYn",
        "boolean",
        None,
    ),
    "kcCertifiedProductExclusionYn": (
        "originProduct.detailAttribute.certificationTargetExcludeContent."
        "kcCertifiedProductExclusionYn",
        "string",
        ("FALSE", "KC_EXEMPTION_OBJECT", "TRUE"),
    ),
    "kcExemptionType": (
        "originProduct.detailAttribute.certificationTargetExcludeContent.kcExemptionType",
        "string",
        ("OVERSEAS", "SAFE_CRITERION", "PARALLEL_IMPORT"),
    ),
}
SOURCES = REPO_ROOT / "documents/contracts/platforms/smartstore/SOURCES.md"
PRODUCT_CREATE = REPO_ROOT / "documents/evidence/marketplace-apis/PRODUCT_CREATE.md"
CAPTURE = (
    REPO_ROOT / "documents/evidence/marketplace-apis/smartstore-cross-border-2.90.1-2026-10-15.json"
)


def test_the_o3b_members_are_the_captured_ones_exactly() -> None:
    assert CAPTURED["customsTaxType"][2] == product.CUSTOMS_TAX_TYPE_VALUES
    assert (
        (CAPTURED["kcCertifiedProductExclusionYn"][2])
        == product.KC_CERTIFIED_PRODUCT_EXCLUSION_VALUES
    )
    assert CAPTURED["kcExemptionType"][2] == product.KC_EXEMPTION_TYPE_VALUES
    assert product.OVERSEAS_KC_CERTIFIED_PRODUCT_EXCLUSION == "KC_EXEMPTION_OBJECT"
    assert product.OVERSEAS_KC_EXEMPTION_TYPE == "OVERSEAS"
    assert product.OVERSEAS_BUSINESS_CUSTOMS_CLEARANCE_SALE is False
    spelled = {
        "businessCustomsClearanceSaleYn": (
            f"{product.FIELD_ORIGIN_PRODUCT}.{product.FIELD_DELIVERY_INFO}."
            f"{product.FIELD_BUSINESS_CUSTOMS_CLEARANCE_SALE_YN}"
        ),
        "kcCertifiedProductExclusionYn": (
            f"{product.FIELD_ORIGIN_PRODUCT}.{product.FIELD_DETAIL_ATTRIBUTE}."
            f"{product.FIELD_CERTIFICATION_TARGET_EXCLUDE_CONTENT}."
            f"{product.FIELD_KC_CERTIFIED_PRODUCT_EXCLUSION_YN}"
        ),
        "kcExemptionType": (
            f"{product.FIELD_ORIGIN_PRODUCT}.{product.FIELD_DETAIL_ATTRIBUTE}."
            f"{product.FIELD_CERTIFICATION_TARGET_EXCLUDE_CONTENT}.{product.FIELD_KC_EXEMPTION_TYPE}"
        ),
    }
    for member, path in spelled.items():
        assert path == CAPTURED[member][0], member
    # Each captured member is recorded with its path, type and every enum value in the ledger and
    # in the field-level CREATE evidence.
    ledger = SOURCES.read_text("utf-8")
    evidence = PRODUCT_CREATE.read_text("utf-8")
    assert "`NAVER-P0-CROSS-BORDER-2901`" in ledger
    for member, (path, kind, values) in CAPTURED.items():
        for text in (ledger, evidence):
            assert f"`{path}`" in text, (member, path)
        assert f"`{path}` ({kind}" in evidence, member
        for value in values or ():
            assert f"`{value}`" in evidence, (member, value)


def test_the_table_is_the_retained_capture() -> None:
    # The retained excerpt of the 2.90.1 reference (NAVER-P0-CROSS-BORDER-2901) states each
    # member's path, type and enum exactly as the table above, and it is the recorded bytes.
    digest = hashlib.sha256(CAPTURE.read_bytes()).hexdigest()
    assert f"sha256 {digest}" in SOURCES.read_text("utf-8")
    assert f"(sha256 `{digest}`)" in PRODUCT_CREATE.read_text("utf-8")
    capture = json.loads(CAPTURE.read_text("utf-8"))
    assert capture["source"]["api_version"].startswith("2.90.1")
    assert capture["source"]["observed"] == "2026-10-15"
    assert capture["operation"] == {"method": "POST", "path": "/v2/products"}
    members = {entry["path"]: entry for entry in capture["members"]}
    for path, kind, values in CAPTURED.values():
        assert members[path]["type"] == kind, path
        assert tuple(members[path].get("enum") or ()) == (values or ()), path
    # What keeps the two remaining gaps open, as captured: the import code is a lookup value, not a
    # documented constant, and 구매대행 certification information is stated as required.
    origin = members["originProduct.detailAttribute.originAreaInfo.originAreaCode"]
    assert "02(수입산)" in origin["description"]
    importer = members["originProduct.detailAttribute.originAreaInfo.importer"]
    assert "수입산인 경우 필수" in importer["description"]
    lookups = capture["origin_area_lookups"]
    assert {lookup["path"] for lookup in lookups} == {
        "/v1/product-origin-areas",
        "/v1/product-origin-areas/query",
        "/v1/product-origin-areas/sub-origin-areas",
    }
    assert not any(lookup["code_values_in_documentation"] for lookup in lookups)
    kind = members[
        "originProduct.detailAttribute.productCertificationInfos[].certificationKindType"
    ]
    assert "OVERSEAS" in kind["enum"]
    assert "구매대행 선택 시 인증 정보 필수 등록" in kind["description"]
    # No origin-area lookup is adopted, so ICBM cannot read an import code today.
    assert not any("product-origin-areas" in str(c) for c in registry.ADOPTED.values())


def test_each_o3b_member_has_its_documented_projected_row() -> None:
    clearance = _row("originProduct.deliveryInfo.businessCustomsClearanceSaleYn")
    assert "| projected |" in clearance and "`false`" in clearance
    kc = _row("originProduct.detailAttribute.certificationTargetExcludeContent")
    assert "| projected |" in kc
    assert "`KC_EXEMPTION_OBJECT`" in kc and "`OVERSEAS`" in kc and "FX-05" in kc
    assert "`WIRE_OVERSEAS_MEMBERS_INCOHERENT`" in _amendment()
    assert "NAVER-P0-CROSS-BORDER-2901" in _row("originProduct.detailAttribute.customsTaxType")


def test_each_uncaptured_member_is_a_named_gap_and_documented() -> None:
    names = {
        "GAP_OVERSEAS_ORIGIN_IMPORT": product.GAP_OVERSEAS_ORIGIN_IMPORT,
        "GAP_OVERSEAS_KC_CERTIFICATION_INFO": product.GAP_OVERSEAS_KC_CERTIFICATION_INFO,
    }
    assert tuple(names.values()) == product.OVERSEAS_GAPS
    amendment = _amendment()
    for name in names:
        assert f"**gap** (`{name}`)" in amendment, name
    # The gaps O3b closed are gone from the adapter and from the contract.
    for closed in ("GAP_OVERSEAS_KC_EXEMPTION", "GAP_OVERSEAS_CUSTOMS_CLEARANCE"):
        assert not hasattr(product, closed)
        assert f"`{closed}`" not in amendment
    # None of the uncaptured members is on the request allow-list: a document carrying one is
    # refused (WIRE_DOCUMENT_FIELD_UNKNOWN), so the adapter cannot emit it by accident.
    allowed = (
        product._DELIVERY_INFO_KEYS
        | product._DELIVERY_INFO_OPTIONAL_KEYS
        | product._DETAIL_ATTRIBUTE_KEYS
        | product._ORIGIN_AREA_KEYS
        | product._CERTIFICATION_EXCLUDE_KEYS
    )
    for never in ("importer", "productCertificationInfos", "certificationKindType"):
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
    # Declared by the supplier's own package profile, never derived from a currency or held
    # beside the package (ADR-0038 §2, §6).
    assert (superdelivery.PROFILE.country, ali1688.PROFILE.country) == ("JP", "CN")
    assert kmretail.PROFILE.country is None
    assert dict(SOURCE_COUNTRIES) == {"superdelivery": "JP", "ali1688": "CN"}
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
