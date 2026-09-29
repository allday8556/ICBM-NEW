"""The SmartStore endpoint registry (ENDPOINT_MATRIX.md §4-§7, §10, §11, §13; M2 PR-A) and the
endpoint-mapping revision invariant (M2 instructions §5)."""

import dataclasses
import inspect

import pytest

from integrations.marketplaces.smartstore import registry
from integrations.marketplaces.smartstore.registry import (
    ADOPTED,
    BASE_URL,
    MAPPING_FINGERPRINTS,
    NOT_ADOPTED,
    SMARTSTORE_ENDPOINT_MAPPING_REVISION,
    EndpointId,
    EndpointNotAdoptedError,
    Method,
    RedirectPolicy,
    RegistryMappingRevision,
    account_succeeded,
    image_upload_succeeded,
    mapping_fingerprint,
    resolve,
    token_succeeded,
)

TOKEN = EndpointId.SMARTSTORE_AUTH_TOKEN
ACCOUNT = EndpointId.SMARTSTORE_SELLER_ACCOUNT
ORIGIN_READ = EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2
CHANNEL_READ = EndpointId.SMARTSTORE_CHANNEL_PRODUCT_READ_V2
IMAGE_UPLOAD = EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD
# M5 PR-D adopts the two product read-backs; everything else the 2.89.0 packet leaves unproven
# stays NOT_ADOPTED with a named gap (registry.ADOPTION_GAPS).
STILL_NOT_ADOPTED = {
    "SMARTSTORE_PRODUCT_CREATE_V2",
    "SMARTSTORE_PRODUCT_SEARCH",
    "SMARTSTORE_CATEGORY_LIST",
    "SMARTSTORE_CATEGORY_READ",
    "SMARTSTORE_PRODUCT_ATTRIBUTE_LIST",
    "SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES",
    "SMARTSTORE_STANDARD_OPTIONS",
    "SMARTSTORE_NOTICE_TYPES",
    "SMARTSTORE_NOTICE_TYPE_READ",
}


# ---------------------------------------------------------------- adoption


def test_em13_1_the_runtime_registry_adopts_m2_connect_and_the_m5_read_backs() -> None:
    assert set(ADOPTED) == {TOKEN, ACCOUNT, ORIGIN_READ, CHANNEL_READ, IMAGE_UPLOAD}
    assert {e.value for e in NOT_ADOPTED} == STILL_NOT_ADOPTED
    assert set(ADOPTED) | NOT_ADOPTED == set(EndpointId)
    assert not set(ADOPTED) & NOT_ADOPTED


def test_every_not_adopted_endpoint_records_why_it_is_not_adopted() -> None:
    # PR-D §1: reviewing a candidate and declining it is an evidence act, not a silent omission.
    assert set(registry.ADOPTION_GAPS) == NOT_ADOPTED
    assert all(gap.strip() for gap in registry.ADOPTION_GAPS.values())


@pytest.mark.parametrize("endpoint", sorted(NOT_ADOPTED))
def test_em13_2_a_not_adopted_endpoint_never_resolves(endpoint: EndpointId) -> None:
    with pytest.raises(EndpointNotAdoptedError):
        resolve(endpoint)


@pytest.mark.parametrize(
    "value", ["SMARTSTORE_AUTH_TOKEN", "SMARTSTORE_PRODUCT_CREATE_V2", "/v2/products", None, 7]
)
def test_only_a_typed_adopted_endpoint_id_resolves(value: object) -> None:
    # Free text equal to an adopted id is still not the typed id: nothing resolves by string.
    with pytest.raises(EndpointNotAdoptedError):
        resolve(value)


def test_not_adopted_endpoints_carry_no_wire_contract() -> None:
    # EM §12: M5 rows freeze nothing, so the registry holds no method, path or timeout for them.
    assert not NOT_ADOPTED & set(ADOPTED)
    assert all(contract.endpoint_id in ADOPTED for contract in ADOPTED.values())


# ---------------------------------------------------------------- contract values


def test_em13_3_method_path_and_base_url_come_from_one_contract() -> None:
    token, account = resolve(TOKEN), resolve(ACCOUNT)
    assert BASE_URL == "https://api.commerce.naver.com/external"
    assert (token.method, token.path, token.content_type, token.requires_bearer) == (
        Method.POST,
        "/v1/oauth2/token",
        "application/x-www-form-urlencoded",
        False,
    )
    assert (account.method, account.path, account.content_type, account.requires_bearer) == (
        Method.GET,
        "/v1/seller/account",
        None,
        True,
    )
    # EM §3: paths are relative to the base URL, so /external can never be doubled or dropped.
    assert all(not c.path.startswith("/external") for c in ADOPTED.values())


def test_em13_6_timeouts_are_endpoint_contract_fields() -> None:
    assert (resolve(TOKEN).connect_timeout_s, resolve(TOKEN).read_timeout_s) == (5.0, 30.0)
    assert (resolve(ACCOUNT).connect_timeout_s, resolve(ACCOUNT).read_timeout_s) == (5.0, 10.0)


def test_em13_7_every_adopted_endpoint_is_no_follow() -> None:
    assert {c.redirect for c in ADOPTED.values()} == {RedirectPolicy.NO_FOLLOW}


def test_em5_the_adopted_group_union_and_the_only_adopted_mutation() -> None:
    union = set().union(*(c.required_groups for c in ADOPTED.values()))
    # The packet's AI-use guide gives the API group 상품 for the product reads; no narrower
    # permission name is invented from it.
    assert union == {"판매자정보", "상품"}
    assert resolve(TOKEN).required_groups == frozenset()
    assert resolve(ORIGIN_READ).required_groups == frozenset({"상품"})
    assert [c.endpoint_id for c in ADOPTED.values() if c.mutating] == [IMAGE_UPLOAD]


def test_the_image_upload_contract_is_exactly_the_approved_scope() -> None:
    contract = resolve(IMAGE_UPLOAD)
    assert (contract.method, contract.path, contract.content_type) == (
        Method.POST,
        "/v1/product-images/upload",
        "multipart/form-data",
    )
    assert contract.requires_bearer and contract.mutating
    assert contract.required_groups == frozenset({"상품"})
    assert contract.safe_query_keys == frozenset()
    assert contract.retained_response_fields == frozenset({"url"})
    assert contract.predicate_revision == "m5-image-upload-r1"


@pytest.mark.parametrize(
    ("status", "body", "accepted"),
    [
        (200, {"images": [{"url": "https://shop-phinf.example/a.jpg"}]}, True),
        (200, {"images": []}, True),
        (200, {"images": [{"url": 7}]}, False),
        (200, {}, False),
        (201, {"images": [{"url": "https://shop-phinf.example/a.jpg"}]}, False),
    ],
)
def test_image_upload_success_predicate(status: int, body: object, accepted: bool) -> None:
    assert image_upload_succeeded(status, body) is accepted


@pytest.mark.parametrize(
    ("endpoint", "path", "placeholder"),
    [
        (ORIGIN_READ, "/v2/products/origin-products/{originProductNo}", "originProductNo"),
        (CHANNEL_READ, "/v2/products/channel-products/{channelProductNo}", "channelProductNo"),
    ],
)
def test_the_adopted_read_backs_carry_exactly_the_packet_contract(
    endpoint: EndpointId, path: str, placeholder: str
) -> None:
    contract = resolve(endpoint)
    assert (contract.method, contract.path, contract.content_type) == (Method.GET, path, None)
    assert (contract.requires_bearer, contract.mutating) == (True, False)
    assert contract.required_groups == frozenset({"상품"})
    assert contract.redirect is RedirectPolicy.NO_FOLLOW
    assert (contract.connect_timeout_s, contract.read_timeout_s) == (5.0, 15.0)
    assert contract.path_params == frozenset({placeholder})
    # Deny-by-default: neither read-back may send any query key at all.
    assert contract.safe_query_keys == frozenset()
    # Only leaves the packet proves may be retained from a read-back.
    assert contract.retained_response_fields == frozenset(
        {"name", "salePrice", "stockQuantity", "sellerManagementCode", "sellerManagerCode", "url"}
    )


@pytest.mark.parametrize(
    "change",
    [
        {"safe_query_keys": frozenset({"page"})},
        {"retained_response_fields": frozenset({"name", "detailContent"})},
        {"content_type": "application/json"},
        {"requires_bearer": False},
    ],
)
def test_the_safe_retention_profile_is_bound_to_the_mapping_fingerprint(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    # ADR-0014 §15: the profile is versioned with the mapping revision, so it cannot drift alone.
    before = mapping_fingerprint()
    changed = dataclasses.replace(ADOPTED[ORIGIN_READ], **change)  # type: ignore[arg-type]
    monkeypatch.setitem(registry.ADOPTED, ORIGIN_READ, changed)  # type: ignore[arg-type]
    assert mapping_fingerprint() != before


def test_the_profile_version_itself_is_in_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    before = mapping_fingerprint()
    monkeypatch.setattr(registry, "SAFE_RETENTION_PROFILE_VERSION", "smartstore-safe-retention/v2")
    assert mapping_fingerprint() != before


def test_em13_8_every_adopted_endpoint_has_a_success_predicate() -> None:
    for contract in ADOPTED.values():
        assert callable(contract.success_predicate)
        assert contract.predicate_revision


# ---------------------------------------------------------------- success predicates (EM §14.4)

VALID_TOKEN = {"access_token": "fixture-token", "expires_in": 10800, "token_type": "Bearer"}


@pytest.mark.parametrize("token_type", ["Bearer", "bearer", "BEARER", "bEaReR"])
def test_em14_4_a_bearer_token_passes_in_any_case(token_type: str) -> None:
    assert token_succeeded(200, {**VALID_TOKEN, "token_type": token_type})


def _without(body: dict[str, object], field: str) -> dict[str, object]:
    return {k: v for k, v in body.items() if k != field}


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (201, VALID_TOKEN),
        (204, VALID_TOKEN),
        (200, None),
        (200, ["access_token"]),
        (200, "access_token"),
        (200, _without(VALID_TOKEN, "access_token")),
        (200, {**VALID_TOKEN, "access_token": ""}),
        (200, {**VALID_TOKEN, "access_token": "   "}),
        (200, {**VALID_TOKEN, "access_token": 12345}),
        (200, _without(VALID_TOKEN, "expires_in")),
        (200, {**VALID_TOKEN, "expires_in": 0}),
        (200, {**VALID_TOKEN, "expires_in": -1}),
        (200, {**VALID_TOKEN, "expires_in": "10800"}),
        (200, {**VALID_TOKEN, "expires_in": 10800.0}),
        (200, {**VALID_TOKEN, "expires_in": True}),
        (200, _without(VALID_TOKEN, "token_type")),
        (200, {**VALID_TOKEN, "token_type": "mac"}),
        (200, {**VALID_TOKEN, "token_type": ""}),
        (200, {**VALID_TOKEN, "token_type": None}),
    ],
)
def test_em13_9_a_malformed_token_response_fails_closed(status: int, body: object) -> None:
    assert not token_succeeded(status, body)


@pytest.mark.parametrize(
    "body", [{"accountUid": "uid-1", "accountId": "account-1"}, {"accountUid": "uid-1"}]
)
def test_em14_8_a_non_empty_account_uid_passes(body: dict[str, object]) -> None:
    assert account_succeeded(200, body)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (201, {"accountUid": "uid-1"}),
        (200, None),
        (200, [{"accountUid": "uid-1"}]),
        (200, {"accountId": "account-1"}),  # accountId never substitutes for accountUid
        (200, {"accountUid": ""}),
        (200, {"accountUid": "  "}),
        (200, {"accountUid": 42}),
        (200, {"accountUid": None, "accountId": "account-1"}),
    ],
)
def test_em14_8_a_malformed_account_response_fails_closed(status: int, body: object) -> None:
    assert not account_succeeded(status, body)


# ---------------------------------------------------------------- mapping revision (§5)


def test_the_mapping_revision_is_bound_to_the_registry_fingerprint() -> None:
    # §5.3: a permission-relevant change without a revision bump fails here, in CI.
    assert SMARTSTORE_ENDPOINT_MAPPING_REVISION == "m5-image-upload-r1"
    # Superseded revisions stay resolvable, so stored evidence still names a known mapping.
    assert set(MAPPING_FINGERPRINTS) == {
        "m2-connect-r1",
        "m5-register-r1",
        "m5-image-upload-r1",
    }
    assert MAPPING_FINGERPRINTS[SMARTSTORE_ENDPOINT_MAPPING_REVISION] == mapping_fingerprint()
    assert RegistryMappingRevision().current_revision() == SMARTSTORE_ENDPOINT_MAPPING_REVISION


@pytest.mark.parametrize(
    "change",
    [
        {"required_groups": frozenset({"판매자정보", "상품"})},
        {"path": "/v2/seller/account"},
        {"method": Method.POST},
        {"mutating": True},
    ],
)
def test_a_permission_relevant_change_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    before = mapping_fingerprint()
    changed = dataclasses.replace(ADOPTED[ACCOUNT], **change)  # type: ignore[arg-type]
    monkeypatch.setitem(registry.ADOPTED, ACCOUNT, changed)  # type: ignore[arg-type]
    assert mapping_fingerprint() != before


def test_adopting_another_endpoint_changes_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    before = mapping_fingerprint()
    create = EndpointId.SMARTSTORE_PRODUCT_CREATE_V2
    adopted = dataclasses.replace(ADOPTED[ACCOUNT], endpoint_id=create)
    monkeypatch.setitem(registry.ADOPTED, create, adopted)  # type: ignore[arg-type]
    monkeypatch.setattr(registry, "NOT_ADOPTED", NOT_ADOPTED - {create})
    assert mapping_fingerprint() != before


def test_the_revision_is_code_never_parsed_from_documentation() -> None:
    # §5.2: no file is read to produce the revision or the fingerprint.
    source = inspect.getsource(registry)
    for reader in ("open(", "read_text", "read_bytes", "pathlib", "importlib.resources"):
        assert reader not in source
