"""ADR-0018 §3.4, G3-28: the ASSET replay-conflict key is exactly the wire boundary.

Pure: no database, no provider. The key takes four fields and nothing else, and the server-owned
host rule resolves every spelling of the provider host to one canonical host or refuses it.
"""

import inspect

import pytest

from app.live.model import (
    REPLAY_KEY_UNDETERMINABLE,
    WireEndpoint,
    WireHostPolicy,
    WireIdentityError,
    content_digest,
    normalize_host,
    normalize_path,
    replay_key,
)

CANONICAL = "api.commerce.naver.com"
PATH = "/external/v1/product-images/upload"
SHA = content_digest(b"the exact outbound bytes")
POLICY = WireHostPolicy(
    {"smartstore": CANONICAL}, {"smartstore": {"edge.alias.example": CANONICAL}}
)


def key(**overrides: str) -> str:
    endpoint = POLICY.endpoint(
        "smartstore",
        method=overrides.get("method", "POST"),
        host=overrides.get("host", CANONICAL),
        path=overrides.get("path", PATH),
    )
    return replay_key(
        marketplace_key=overrides.get("marketplace", "smartstore"),
        marketplace_account_id=overrides.get("account", "acct-1"),
        endpoint=endpoint,
        content_sha256=overrides.get("sha", SHA),
    ).digest


def test_the_key_takes_exactly_the_four_wire_boundary_fields() -> None:
    parameters = list(inspect.signature(replay_key).parameters)
    assert parameters == [
        "marketplace_key",
        "marketplace_account_id",
        "endpoint",
        "content_sha256",
    ]
    assert list(WireEndpoint.__dataclass_fields__) == ["method", "host", "path"]


@pytest.mark.parametrize(
    "host",
    [
        "API.COMMERCE.NAVER.COM",
        "api.commerce.naver.com.",
        "api.commerce.naver.com:443",
        "https://api.commerce.naver.com/anything",
        "user:pw@api.commerce.naver.com",
        "edge.alias.example",
    ],
)
def test_every_spelling_of_the_provider_host_is_one_key(host: str) -> None:
    assert key(host=host) == key()


@pytest.mark.parametrize(
    "path", ["/external//v1/product-images/upload", "/external/v1/product-images/upload/"]
)
def test_every_spelling_of_the_same_path_is_one_key(path: str) -> None:
    assert key(path=path) == key()


def test_the_method_is_case_normalized() -> None:
    assert key(method="post") == key()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("marketplace", "coupang"),
        ("account", "acct-2"),
        ("method", "PUT"),
        ("path", "/external/v2/product-images/upload"),
        ("sha", content_digest(b"other bytes")),
    ],
)
def test_each_boundary_field_is_its_own_scope(field: str, value: str) -> None:
    assert key(**{field: value}) != key()


@pytest.mark.parametrize(
    "host",
    [
        "proxy.internal.example",
        "127.0.0.1",
        "[::1]",
        "api.commerce.naver.com:8443",
        "localhost",
        "api..commerce.naver.com",
    ],
)
def test_an_unproven_host_is_refused_never_a_new_key(host: str) -> None:
    with pytest.raises(WireIdentityError) as refused:
        key(host=host)
    assert refused.value.code == REPLAY_KEY_UNDETERMINABLE


@pytest.mark.parametrize(
    "path", ["relative/path", "/a/../b", "/a/./b", "/a?x=1", "/a#f", "/a%2Fb", "/"]
)
def test_an_unsafe_path_is_refused(path: str) -> None:
    with pytest.raises(WireIdentityError):
        normalize_path(path)


def test_a_get_is_not_an_upload_and_a_bad_digest_is_not_a_key() -> None:
    with pytest.raises(WireIdentityError):
        key(method="GET")
    with pytest.raises(WireIdentityError):
        key(sha="not-a-digest")


def test_an_alias_must_name_its_canonical_host() -> None:
    with pytest.raises(ValueError):
        WireHostPolicy({"smartstore": CANONICAL}, {"smartstore": {"a.example": "b.example"}})
    assert normalize_host("Api.Commerce.Naver.Com.") == CANONICAL
