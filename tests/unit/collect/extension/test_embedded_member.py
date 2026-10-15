"""The embedded-data member: its reviewed policy entry, and the server's own re-check (ADR-0040 §3).

The browser projects one inline script's data argument onto the policy's allowlist; the server
trusts none of that. Here: the policy entry is validated and digested, a member is refused for any
path outside the allowlist (the member's receive address above all, OC-05), for any wrong type and
over its bounds, and the security gate reads every string in it.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.stages.collect.extension.capture import CaptureEnvelope
from app.stages.collect.extension.embedded import (
    EMBEDDED_OVER_BOUND,
    EMBEDDED_PATH_NOT_ALLOWED,
    EMBEDDED_TYPE_MISMATCH,
    MAX_PROJECTION_BYTES,
    EmbeddedDataMember,
    projection_problem,
    strings_of,
)
from app.stages.collect.extension.gate import embedded_data_gate
from app.stages.collect.extension.policy import (
    CapturePolicyRefused,
    CapturePolicySource,
    EmbeddedDataRule,
    canonical_bytes,
    parse_policy,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SUPPLIERS = REPO_ROOT / "integrations" / "suppliers"
POLICY_FILE = SUPPLIERS / "ali1688" / "browser_capture_policy.json"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "suppliers" / "ali1688" / "990041659965.embedded.json"


def _document() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(POLICY_FILE.read_text("utf-8"))
    return loaded


def _rule() -> EmbeddedDataRule:
    rule = CapturePolicySource(SUPPLIERS).load("ali1688").embedded_data
    assert rule is not None
    return rule


def _projection() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_text("utf-8"))
    return loaded


def _data(projection: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = projection["result"]["data"]
    return data


def _parse(document: dict[str, Any]) -> Any:
    return parse_policy(canonical_bytes(document), "ali1688")


# ---------------------------------------------------------------- the policy entry


def test_only_the_1688_policy_declares_embedded_data_and_it_is_digested() -> None:
    source = CapturePolicySource(SUPPLIERS)
    policy = source.load("ali1688")
    assert policy.embedded_data is not None
    assert policy.embedded_data.prefix == 'window.contextPath = "/default";\nwindow.context=('
    assert policy.embedded_data.argument_marker == "})(window.contextPath,"
    assert policy.embedded_data.terminator == ");"
    assert policy.embedded_data.bare_integer_keys is True
    paths = {entry.path for entry in policy.embedded_data.allowlist}
    assert "result.data.shippingServices.fields.freightInfo.totalCost" in paths
    # ADR-0040 §3.2, OC-09: the description's URL is read in the browser from the parsed
    # argument (the description entry's url_path) and is never projected, so it never leaves
    # the browser and a member that carries it is refused.
    assert not any(path.startswith("result.data.description") for path in paths)
    assert policy.description is not None
    assert policy.description.url_path == "result.data.description.fields.detailUrl"
    assert "result.data.Root.fields.dataJson.skuModel.skuInfoMap.*.price" in paths
    # Nothing of the member's address, identity or account, and no hash-shaped SKU spec id.
    for path in paths:
        lowered = path.lower()
        for word in ("receive", "recieve", "address", "member", "nick", "login", "specid"):
            assert word not in lowered, path
    assert source.load("kmretail").embedded_data is None
    # The entry is part of the digest: changing it changes the policy's identity.
    changed = _document()
    changed["embedded_data"]["max_depth"] = 39
    assert _parse(changed).digest != policy.digest


@pytest.mark.parametrize(
    "change",
    [
        lambda e: e["allowlist"].append(
            {
                "path": "result.data.shippingServices.fields.freightInfo.recieveAddress",
                "type": "string",
            }
        ),
        lambda e: e["allowlist"].append(
            {
                "path": "result.data.shippingServices.fields.freightInfo.receiveAddressId",
                "type": "integer",
            }
        ),
        lambda e: e["allowlist"].append(
            {"path": "result.data.frontSellerMemberModel.id", "type": "string"}
        ),
        lambda e: e["allowlist"].append({"path": "result.global.userId", "type": "string"}),
        lambda e: e["allowlist"].append({"path": "result.data.x", "type": "object"}),
        lambda e: e["allowlist"].append({"path": "result.data.a-b", "type": "string"}),
        lambda e: e["allowlist"].append(
            {"path": "result.data.mainPrice.fields.unit", "type": "string"}
        ),
        # A leaf that is also a container.
        lambda e: e["allowlist"].append({"path": "result.data.mainPrice.fields", "type": "string"}),
        # A segment written once as an array and once as a value.
        lambda e: e["allowlist"].append(
            {
                "path": "result.data.mainPrice.fields.priceModel.currentPrices.price",
                "type": "string",
            }
        ),
        # A map with named keys beside its wildcard.
        lambda e: e["allowlist"].append(
            {
                "path": "result.data.Root.fields.dataJson.skuModel.skuInfoMap.top.price",
                "type": "string",
            }
        ),
        lambda e: e.update(extra=True),
        lambda e: e.pop("terminator"),
        lambda e: e.update(bare_integer_keys="yes"),
        lambda e: e.update(max_bytes=10 * 1024 * 1024),
        lambda e: e.update(max_depth=0),
        lambda e: e.update(prefix=""),
        lambda e: e.update(allowlist=[]),
    ],
)
def test_an_embedded_entry_that_is_not_exactly_reviewed_refuses_the_policy(change: Any) -> None:
    document = _document()
    change(document["embedded_data"])
    with pytest.raises(CapturePolicyRefused) as refused:
        _parse(document)
    assert refused.value.code in {"CAPTURE_POLICY_INVALID", "CAPTURE_POLICY_BOUND_INVALID"}


def test_a_policy_without_the_entry_is_unchanged() -> None:
    document = _document()
    del document["embedded_data"]
    del document["description"]
    parsed = _parse(document)
    assert parsed.embedded_data is None and parsed.description is None
    # A description entry needs the embedded data it reads its URL from.
    alone = _document()
    del alone["embedded_data"]
    with pytest.raises(CapturePolicyRefused):
        _parse(alone)


@pytest.mark.parametrize(
    "change",
    [
        # OC-09: the URL is never projected, so its path may not be on the allowlist, nor be
        # a prefix of an entry, nor sit under one.
        lambda d: d["embedded_data"]["allowlist"].append(
            {"path": "result.data.description.fields.detailUrl", "type": "string"}
        ),
        lambda d: d["description"].update(url_path="result.data.mainPrice.fields.unit"),
        lambda d: d["description"].update(url_path="result.data.mainPrice"),
        lambda d: d["description"].update(
            url_path="result.data.mainPrice.fields.priceModel.currentPrices"
        ),
        lambda d: d["description"].update(
            url_path="result.data.Root.fields.dataJson.skuModel.skuInfoMap.anyKey.price"
        ),
        lambda d: d["description"].update(url_path="result.data"),
        lambda d: d["description"].update(url_path="result.global.detailUrl"),
        lambda d: d["description"].update(url_path="result.data.description.*.detailUrl"),
        lambda d: d["description"].update(url_path="result.data.description[].detailUrl"),
        lambda d: d["description"].update(url_path=7),
    ],
)
def test_a_description_url_path_that_could_be_projected_refuses_the_policy(
    change: Any,
) -> None:
    document = _document()
    change(document)
    with pytest.raises(CapturePolicyRefused) as refused:
        _parse(document)
    assert refused.value.code == "CAPTURE_POLICY_INVALID"


# ---------------------------------------------------------------- the server's re-check


def test_the_browsers_projection_is_exactly_allowlisted() -> None:
    assert projection_problem(_rule(), _projection()) is None


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        # OC-05: the member's receive address is never on the allowlist.
        (
            lambda d: d["shippingServices"]["fields"]["freightInfo"].update(
                recieveAddress="某省某市某路 1 号"
            ),
            EMBEDDED_PATH_NOT_ALLOWED,
        ),
        (
            lambda d: d["shippingServices"]["fields"]["freightInfo"].update(receiveAddressId=123),
            EMBEDDED_PATH_NOT_ALLOWED,
        ),
        (lambda d: d.update(userRights={"isLogin": True}), EMBEDDED_PATH_NOT_ALLOWED),
        (
            lambda d: next(
                iter(d["Root"]["fields"]["dataJson"]["skuModel"]["skuInfoMap"].values())
            ).update(specId="93ccf20e4942667efa75ecc9dcc9b421"),
            EMBEDDED_PATH_NOT_ALLOWED,
        ),
        (
            lambda d: d["mainPrice"]["fields"]["priceModel"]["currentPrices"][0].update(price=9.5),
            EMBEDDED_TYPE_MISMATCH,
        ),
        (
            lambda d: d["mainPrice"]["fields"]["priceModel"]["currentPrices"][0].update(
                beginAmount="1"
            ),
            EMBEDDED_TYPE_MISMATCH,
        ),
        (lambda d: d["gallery"]["fields"].update(offerId=True), EMBEDDED_TYPE_MISMATCH),
        (lambda d: d["gallery"]["fields"].update(offerId=None), EMBEDDED_TYPE_MISMATCH),
        (
            lambda d: d["mainPrice"]["fields"]["priceModel"].update(currentPrices={"price": "1"}),
            EMBEDDED_TYPE_MISMATCH,
        ),
        (lambda d: d["mainPrice"].update(fields=["unit"]), EMBEDDED_TYPE_MISMATCH),
        (
            lambda d: d["mainPrice"]["fields"].update(unit="x" * 5000),
            EMBEDDED_TYPE_MISMATCH,
        ),
        (
            lambda d: d.update(description={"fields": {"label": "详情描述"}}),
            EMBEDDED_PATH_NOT_ALLOWED,
        ),
        # OC-09: the description's URL is a browser-only read; a member that carries it,
        # plain or with a credential or a secret query, is refused.
        (
            lambda d: d.update(
                description={
                    "fields": {"detailUrl": "https://itemcdn.tmall.com/1688offer/icoss5072039755"}
                }
            ),
            EMBEDDED_PATH_NOT_ALLOWED,
        ),
        (
            lambda d: d.update(
                description={
                    "fields": {
                        "detailUrl": "https://user:pw@itemcdn.tmall.com/1688offer/x?token=abc"
                    }
                }
            ),
            EMBEDDED_PATH_NOT_ALLOWED,
        ),
    ],
)
def test_anything_outside_the_allowlist_refuses_the_member(change: Any, problem: str) -> None:
    projection = copy.deepcopy(_projection())
    change(_data(projection))
    assert projection_problem(_rule(), projection) == problem


def test_a_member_over_its_bounds_is_refused() -> None:
    projection = copy.deepcopy(_projection())
    skus = _data(projection)["Root"]["fields"]["dataJson"]["skuModel"]["skuInfoMap"]
    template = next(iter(skus.values()))
    for index in range(5000):
        skus[f"sku-{index}"] = dict(template)
    assert projection_problem(_rule(), projection) == EMBEDDED_OVER_BOUND
    huge = copy.deepcopy(_projection())
    _data(huge)["mainPrice"]["fields"]["unit"] = "个" * (MAX_PROJECTION_BYTES // 2)
    assert projection_problem(_rule(), huge) == EMBEDDED_OVER_BOUND


# ---------------------------------------------------------------- the security gate


def test_the_gate_reads_every_string_and_quotes_only_the_allowlist_path() -> None:
    clean = embedded_data_gate(strings_of(_rule(), _projection()))
    assert clean.blocking == () and clean.notes == ()
    projection = copy.deepcopy(_projection())
    skus = _data(projection)["Root"]["fields"]["dataJson"]["skuModel"]["skuInfoMap"]
    secret_key = "session=abcdefgh12345678"
    skus[secret_key] = skus.pop(next(iter(skus)))
    _data(projection)["shippingServices"]["fields"]["freightInfo"]["location"] = (
        "联系 010-1234-5678"
    )
    _data(projection)["mainPrice"]["fields"]["unit"] = "access_token=abcdef0123456789"
    gate = embedded_data_gate(strings_of(_rule(), projection))
    assert set(gate.blocking) == {
        "SECRET_VALUE@embedded:result.data.Root.fields.dataJson.skuModel.skuInfoMap.*",
        "SECRET_VALUE@embedded:result.data.mainPrice.fields.unit",
    }
    assert gate.notes == (
        "CONTACT@embedded:result.data.shippingServices.fields.freightInfo.location",
    )
    for finding in (*gate.blocking, *gate.notes):
        assert "abcdef" not in finding and "1234" not in finding


# ---------------------------------------------------------------- the envelope member


def test_the_member_is_a_projection_or_a_refusal_and_nothing_else() -> None:
    assert EmbeddedDataMember(projection={"result": {}}).refused is None
    assert EmbeddedDataMember(refused="EMBEDDED_NOT_JSON").projection is None
    for invalid in (
        {},
        {"projection": {"a": 1}, "refused": "EMBEDDED_NOT_JSON"},
        {"refused": "SOMETHING_ELSE"},
        {"projection": {"a": 1}, "script": "window.x=1"},
    ):
        with pytest.raises(ValidationError):
            EmbeddedDataMember.model_validate(invalid)
    envelope = {
        "supplier_key": "ali1688",
        "policy": {"revision": "r", "digest": "d"},
        "transport": {
            "url": "https://detail.1688.com/offer/1.html",
            "navigation_name": "https://detail.1688.com/offer/1.html",
            "response_status": 200,
            "redirect_count": 0,
            "content_type": "text/html",
            "character_set": "UTF-8",
        },
        "html": "<!doctype html><html><head></head><body></body></html>",
        "embedded_data": {"refused": "EMBEDDED_SCRIPT_NOT_EXACTLY_ONE"},
    }
    parsed = CaptureEnvelope.model_validate(envelope)
    assert parsed.embedded_data is not None
    assert "projection" not in repr(parsed) or "result" not in repr(parsed)
    with pytest.raises(ValidationError):
        CaptureEnvelope.model_validate({**envelope, "script_text": "window.context=1"})
