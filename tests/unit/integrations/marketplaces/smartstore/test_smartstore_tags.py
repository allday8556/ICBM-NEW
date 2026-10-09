"""ADR-0028 T2: the two read-only SmartStore tag reads over the adopted caller.

The recommended-tag search keeps each distinct ``(text, code)``; the restricted-tag check answers
``True``/``False`` only for a tag answered exactly once, and ``None`` (not checked) otherwise. Both
are reads: no tag is ever sent (AIT-01).
"""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.platform.core.errors import AppError, PolicyBlockedError
from integrations.marketplaces.smartstore.caller import (
    SmartStoreCallError,
    SmartStoreEndpointCaller,
    TagRecommendRequest,
    TagRestrictedRequest,
)
from integrations.marketplaces.smartstore.registry import (
    EndpointId,
    resolve,
    tag_recommend_succeeded,
    tag_restricted_succeeded,
)
from integrations.marketplaces.smartstore.tags import SmartStoreTagSource


def _bearer() -> Any:
    return SimpleNamespace(access_token="token", credential_generation=3, session_generation=5)


def _source(handler: Any) -> SmartStoreTagSource:
    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(handler))
    return SmartStoreTagSource(caller, _bearer)


def test_both_endpoints_are_adopted_reads_of_the_product_group() -> None:
    for endpoint_id, path in (
        (EndpointId.SMARTSTORE_TAG_RECOMMEND, "/v2/tags/recommend-tags"),
        (EndpointId.SMARTSTORE_TAG_RESTRICTED, "/v2/tags/restricted-tags"),
    ):
        contract = resolve(endpoint_id)
        assert (contract.path, contract.mutating) == (path, False)
        assert contract.required_groups == frozenset({"상품"})


def test_the_recommendation_keeps_each_distinct_text_with_its_code() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {"code": 101, "text": "들기름", "extra": "dropped"},
                {"text": "생들기름"},
                {"code": 101, "text": "들기름"},
            ],
        )

    tags = _source(handler).recommend("생들기름 350ml")
    assert [(t.text, t.code) for t in tags] == [("들기름", 101), ("생들기름", None)]
    assert seen[0].method == "GET" and seen[0].url.path == "/external/v2/tags/recommend-tags"
    assert seen[0].url.params.get_list("keyword") == ["생들기름 350ml"]
    assert seen[0].headers["authorization"] == "Bearer token"


def test_an_empty_recommendation_is_no_tag() -> None:
    assert _source(lambda request: httpx.Response(200, json=[])).recommend("x") == ()


@pytest.mark.parametrize(
    "body",
    (
        {"text": "not an array"},
        [{"code": 1}],
        [{"code": True, "text": "bool code"}],
        [{"code": "1", "text": "string code"}],
        [{"text": "   "}],
        [{"text": f"t{n}"} for n in range(21)],
    ),
)
def test_an_undocumented_recommendation_is_refused(body: object) -> None:
    assert tag_recommend_succeeded(200, body) is False
    with pytest.raises(SmartStoreCallError):
        _source(lambda request: httpx.Response(200, json=body)).recommend("x")


def test_the_restricted_check_repeats_tags_and_answers_each_asked_tag_once() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {"tag": "들기름", "restricted": False},
                {"tag": "금지어", "restricted": True},
                {"tag": "두번", "restricted": False},
                {"tag": "두번", "restricted": True},
            ],
        )

    answers = _source(handler).check(("들기름", "금지어", "두번", "무응답"))
    assert answers == {"들기름": False, "금지어": True, "두번": None, "무응답": None}
    assert seen[0].url.params.get_list("tags") == ["들기름", "금지어", "두번", "무응답"]


def test_an_answer_for_a_tag_never_asked_proves_nothing() -> None:
    source = _source(
        lambda request: httpx.Response(200, json=[{"tag": "other", "restricted": False}])
    )
    with pytest.raises(PolicyBlockedError) as refused:
        source.check(("asked",))
    assert refused.value.code == "SMARTSTORE_TAG_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "body",
    ([{"tag": "a", "restricted": "false"}], [{"restricted": False}], {"tag": "a"}),
)
def test_an_undocumented_restricted_answer_is_refused(body: object) -> None:
    assert tag_restricted_succeeded(200, body) is False


@pytest.mark.parametrize(
    "request_",
    (
        TagRecommendRequest("token", 3, 5, ""),
        TagRecommendRequest("token", 3, 5, " padded "),
        TagRecommendRequest("token", 3, 5, "x" * 51),
        TagRestrictedRequest("token", 3, 5, ()),
        TagRestrictedRequest("token", 3, 5, tuple(f"t{n}" for n in range(11))),
        TagRestrictedRequest("token", 3, 5, ("dup", "dup")),
        TagRestrictedRequest("token", 3, 5, ("line\nbreak",)),
    ),
)
def test_a_request_outside_the_contract_never_reaches_the_wire(request_: Any) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=[])

    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(handler))
    endpoint = (
        EndpointId.SMARTSTORE_TAG_RECOMMEND
        if isinstance(request_, TagRecommendRequest)
        else EndpointId.SMARTSTORE_TAG_RESTRICTED
    )
    with pytest.raises(AppError):
        caller.call(endpoint, request_)
    assert sent == []


def test_no_committed_session_reads_nothing() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=[])

    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(handler))
    source = SmartStoreTagSource(caller, lambda: None)
    for read in (lambda: source.recommend("x"), lambda: source.check(("x",))):
        with pytest.raises(PolicyBlockedError) as refused:
            read()
        assert refused.value.code == "SMARTSTORE_SESSION_UNAVAILABLE"
    assert sent == []


def test_a_redirect_is_never_followed() -> None:
    source = _source(
        lambda request: httpx.Response(308, headers={"location": "https://elsewhere.invalid/"})
    )
    with pytest.raises(SmartStoreCallError) as refused:
        source.recommend("x")
    assert refused.value.code == "SMARTSTORE_UNEXPECTED_REDIRECT"
