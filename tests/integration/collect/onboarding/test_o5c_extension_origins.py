"""ADR-0036 O5c: the extension asks Chrome for an onboarded origin on approval (§8, D3, OB-08).

Offline throughout: the trials read the scripted ``TrialShop``, sessions are stubbed, the secret
store lives in memory and the extension is a signed request of the test. Proven here:

- the onboarded-origins read lists exactly the ``ACTIVE``, offered onboarded suppliers, each with
  its exact storefront origin, key and name; a ``DRAFT``, ``RECON`` or ``DISABLED`` one is absent,
  and so is every repository supplier, whose host is the manifest's own;
- a supplier disabled after approval leaves the list at once, which is what the extension removes
  its granted origin against;
- the read needs everything every extension route needs (the client header, the pinned origin,
  the pairing signature and no body), and only the paired origin's preflight is answered.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.stages.collect.extension.pairing import SIGNATURE_HEADER, PairingRecord
from app.stages.collect.onboarding.site import OnboardingState
from integrations.suppliers.registry import SITES
from tests.conftest import LOCAL
from tests.support.extension_support import ORIGIN, pair, signed
from tests.support.jobs_support import FakeClock
from tests.support.onboarding_trial_support import (
    CAFE24_HOST,
    CAFE24_ORIGIN,
    OPERATOR,
    StubSessions,
    TrialShop,
    active_cafe24_site,
    recon_site,
    trial_container,
)

pytestmark = pytest.mark.integration

ORIGINS = "/api/v1/collect/extension/onboarded-origins"
SUPPLIERS = "/api/v1/collect/extension/suppliers"
ACTIVE_NAME = "셀프등록 상점"


@dataclass(frozen=True)
class Sites:
    active: str
    recon: str
    draft: str
    disabled: str


@pytest.fixture
def shop() -> TrialShop:
    return TrialShop()


@pytest.fixture
def sites(config: AppConfig, clock: FakeClock, shop: TrialShop) -> Sites:
    """One onboarded supplier in each state, written through the owner as O2–O5a do. The approval
    is proven on the service with the test clock; the application below then binds them at start."""
    with trial_container(config, clock, shop, StubSessions()) as app:
        active = active_cafe24_site(app, clock, shop, name=ACTIVE_NAME)
        recon = recon_site(app, "https://recon.trial-sample.co.kr", "cafe24", name="분석 중")
        draft = app.onboarding.create(
            name="초안",
            origin="https://draft.trial-sample.co.kr",
            template="cafe24",
            actor=OPERATOR,
        ).supplier_key
        disabled = app.onboarding.create(
            name="중지", origin="https://off.trial-sample.co.kr", template="cafe24", actor=OPERATOR
        ).supplier_key
        app.onboarding.transition(disabled, OnboardingState.DISABLED, actor=OPERATOR)
        return Sites(active=active, recon=recon, draft=draft, disabled=disabled)


@pytest.fixture
def api(config: AppConfig, shop: TrialShop, sites: Sites) -> Iterator[TestClient]:
    application = create_app(
        config, collection_gateway=shop.gateway(), collection_sessions=StubSessions()
    )
    with TestClient(application, base_url=LOCAL) as client:
        yield client


def _container(client: TestClient) -> Container:
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    return container


def _read(client: TestClient, record: PairingRecord, path: str = ORIGINS) -> Any:
    response = client.get(path, headers=signed(record, method="GET", path=path))
    assert response.status_code == 200, response.text
    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
    return response.json()


def _code(response: Any) -> str:
    code: str = response.json()["error"]["code"]
    return code


def test_only_active_onboarded_origins_are_listed(api: TestClient, sites: Sites) -> None:
    record = pair(_container(api))
    assert _read(api, record) == {
        "origins": [
            {"supplier_key": sites.active, "display_name": ACTIVE_NAME, "origin": CAFE24_ORIGIN}
        ]
    }
    # The listed origin is exactly the host the extension recognises the supplier by, so a granted
    # origin and a recognised page are the same thing.
    assert _read(api, record, SUPPLIERS)["suppliers"][CAFE24_HOST] == sites.active
    listed = {entry["supplier_key"] for entry in _read(api, record)["origins"]}
    for absent in (sites.recon, sites.draft, sites.disabled, *SITES, "kmretail"):
        assert absent not in listed, absent
    catalog = _container(api).supplier_catalog
    assert {o.supplier_key for o in catalog.active_origins()} == {sites.active}


def test_a_disabled_supplier_leaves_the_list_at_once(api: TestClient, sites: Sites) -> None:
    container = _container(api)
    record = pair(container)
    assert [entry["origin"] for entry in _read(api, record)["origins"]] == [CAFE24_ORIGIN]
    container.onboarding_service.disable(sites.active, actor=OPERATOR)
    # No restart: the catalog rebinds, and the extension removes the origin it holds against this.
    assert _read(api, record) == {"origins": []}
    assert CAFE24_HOST not in _read(api, record, SUPPLIERS)["suppliers"]


def test_the_read_needs_everything_every_extension_route_needs(
    api: TestClient, sites: Sites
) -> None:
    # Not paired yet: nothing is answered, and nothing names an origin.
    unpaired = api.get(ORIGINS, headers={"X-ICBM-Client": "x", "Origin": ORIGIN})
    assert unpaired.status_code in (401, 403)
    assert "Access-Control-Allow-Origin" not in unpaired.headers
    record = pair(_container(api))

    def get(**signing: Any) -> Any:
        return api.get(ORIGINS, headers=signed(record, method="GET", path=ORIGINS, **signing))

    assert _code(get(client_header=None)) == "CLIENT_HEADER_REQUIRED"
    assert _code(get(origin="https://kmretail.co.kr")) == "EXTENSION_ORIGIN_MISMATCH"
    assert _code(get(overrides={SIGNATURE_HEADER: "0" * 64})) == "EXTENSION_SIGNATURE_INVALID"
    assert _code(get(body=b"x")) == "EXTENSION_BODY_DIGEST_MISMATCH"
    # A request signed for another extension path does not open this one.
    assert (
        _code(api.get(ORIGINS, headers=signed(record, method="GET", path=SUPPLIERS)))
        == "EXTENSION_SIGNATURE_INVALID"
    )
    for refused in (get(client_header=None), get(origin="https://kmretail.co.kr")):
        assert "Access-Control-Allow-Origin" not in refused.headers

    def preflight(origin: str, method: str = "GET") -> Any:
        return api.options(
            ORIGINS, headers={"Origin": origin, "Access-Control-Request-Method": method}
        )

    allowed = preflight(ORIGIN)
    assert allowed.status_code == 204
    assert allowed.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert allowed.headers["Access-Control-Allow-Methods"] == "GET"
    assert "Access-Control-Allow-Credentials" not in allowed.headers
    for origin, method in (
        (CAFE24_ORIGIN, "GET"),
        ("chrome-extension://" + "p" * 32, "GET"),
        (ORIGIN, "POST"),
    ):
        refused = preflight(origin, method)
        assert refused.status_code == 403
        assert "Access-Control-Allow-Origin" not in refused.headers
