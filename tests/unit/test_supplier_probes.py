"""The common proof procedure applied to KM통상's probe, on synthetic pages only."""

import inspect

import pytest

from app.connect.proof import ProtectedReadProof, ReadClassification, ReadOutcome, classify
from integrations.suppliers import kmretail
from integrations.suppliers.base import ProbeResponse, ProtectedReadProbe
from integrations.suppliers.registry import SUPPLIERS
from tests.suppliers import KMRETAIL_PAGES

PROBE = kmretail.DEFINITION.probe
R = ReadClassification


def _response(body: str, status: int = 200, location: str | None = None) -> ProbeResponse:
    return ProbeResponse(status=status, path=PROBE.target, location=location, body=body)


def test_the_unauthenticated_200_page_is_login_required_not_success() -> None:
    outcome = classify(PROBE, _response(KMRETAIL_PAGES.logged_out))
    assert outcome.classification is R.LOGIN_REQUIRED
    assert outcome.http_status == 200, "status 200 is exactly the case that proves nothing"
    assert {"state_logoff", "login_check_redirect"} <= set(outcome.signals)


def test_the_page_skeleton_alone_proves_nothing() -> None:
    skeleton = '<div class="xans-element- xans-myshop xans-myshop-asyncbankbook"></div>'
    assert classify(PROBE, _response(skeleton)).classification is R.UNRECOGNIZED


def test_the_signed_in_markers_prove_authentication() -> None:
    outcome = classify(PROBE, _response(KMRETAIL_PAGES.logged_in))
    assert outcome.classification is R.AUTHENTICATED
    assert set(outcome.signals) == {"state_logon", "logout_action"}


@pytest.mark.parametrize(
    "body",
    [
        '<div class="xans-layout-statelogon"></div>',  # logged-on module without logout action
        '<a href="/exec/front/Member/logout/">LOGOUT</a>',  # logout action without the module
    ],
)
def test_half_of_the_authenticated_evidence_is_not_enough(body: str) -> None:
    assert classify(PROBE, _response(body)).classification is R.UNRECOGNIZED


def test_a_redirect_to_the_login_page_is_login_required() -> None:
    outcome = classify(PROBE, _response("", status=302, location="/member/login.html"))
    assert (outcome.classification, outcome.signals) == (R.LOGIN_REQUIRED, ("redirect_to_login",))


def test_contradictory_evidence_is_unrecognized() -> None:
    both = KMRETAIL_PAGES.logged_in + KMRETAIL_PAGES.logged_out
    assert classify(PROBE, _response(both)).classification is R.UNRECOGNIZED


def test_signed_in_markers_on_a_denied_response_do_not_authenticate() -> None:
    outcome = classify(PROBE, _response(KMRETAIL_PAGES.logged_in, status=403))
    assert outcome.classification is R.LOGIN_REQUIRED


def test_a_broken_predicate_proves_nothing() -> None:
    def boom(_: ProbeResponse) -> tuple[bool, tuple[str, ...]]:
        raise RuntimeError("bug")

    probe = ProtectedReadProbe(
        target="/x", unauthenticated_expectation=boom, authenticated_predicate=boom
    )
    assert classify(probe, _response("")).classification is R.UNRECOGNIZED


@pytest.mark.parametrize(
    ("control", "authenticated", "proven"),
    [
        (R.LOGIN_REQUIRED, R.AUTHENTICATED, True),
        (R.AUTHENTICATED, R.AUTHENTICATED, False),  # target is public: proves nothing
        (R.LOGIN_REQUIRED, R.LOGIN_REQUIRED, False),
        (R.UNRECOGNIZED, R.AUTHENTICATED, False),
    ],
)
def test_the_proof_needs_both_halves_on_the_same_target(
    control: ReadClassification, authenticated: ReadClassification, proven: bool
) -> None:
    proof = ProtectedReadProof(
        target=PROBE.target,
        control=ReadOutcome(control, 200, ()),
        authenticated=ReadOutcome(authenticated, 200, ()),
    )
    assert proof.proven is proven


@pytest.mark.parametrize("definition", SUPPLIERS, ids=lambda d: d.profile.supplier_key)
def test_supplier_predicates_can_see_only_the_response_view(definition: object) -> None:
    # Object-capability check (Issue #7 comment 5653615136): a predicate receives one immutable
    # ProbeResponse and nothing it could use to make a request.
    probe = definition.probe  # type: ignore[attr-defined]
    for predicate in (probe.authenticated_predicate, probe.unauthenticated_expectation):
        parameters = list(inspect.signature(predicate).parameters.values())
        assert len(parameters) == 1
        assert parameters[0].annotation in (ProbeResponse, "ProbeResponse")


def test_kmretail_is_described_not_implemented() -> None:
    profile = kmretail.PROFILE
    assert (profile.supplier_key, profile.base_url, profile.auth_required) == (
        "kmretail",
        "https://kmretail.co.kr",
        True,
    )
    assert profile.egress_hosts == {"kmretail.co.kr", "login2.cafe24ssl.com"}
    assert PROBE.target == "/myshop/index.html"
    assert kmretail.DEFINITION.login.path == "/member/login.html"
