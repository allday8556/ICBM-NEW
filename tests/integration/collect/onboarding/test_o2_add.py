"""ADR-0036 O2: 「+ 공급처 추가」 — guard, platform detection, policy reads, CONNECT and RECON.

Everything is offline. The storefronts are scripted (``tests.support.onboarding_support``): the
real bootstrap reader runs over a mock transport and a fixed resolver, and the supplier gateway is
the fake CONNECT gateway. Proven here:

- detection reads the home page once and each template's login page once, through one grant for
  the entered host only, and classifies cafe24 / godomall / unsupported from markers only;
- a name that resolves to a loopback or private address is refused before any HTTP read;
- the policy reads are robots.txt and the template's terms page (two, within the cap of 3), shown
  with their warnings, stored acknowledged with the draft, and never a refusal;
- the API refuses guarded, repository and duplicate origins before any read, refuses a save
  without the acknowledgement or with another platform than the one shown, and keeps a draft
  out of ``RECON`` until the login is stored and CONNECT's test passed; ``DISABLED`` unbinds it;
- the login goes only through CONNECT's credential route into the secret store: never into an
  onboarding table, the audit log, the database file, a response or a log line (OB-05);
- the repository suppliers are unchanged (OB-07).
"""

import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig, database_path
from app.container import Container
from app.main import create_app
from app.platform.core.errors import AppError
from app.stages.collect.onboarding.detect import UNSUPPORTED, detect, read_policies
from app.stages.collect.onboarding.service import (
    ONBOARDING_DETECTION_MISMATCH,
    ONBOARDING_POLICY_NOT_ACKNOWLEDGED,
    ONBOARDING_RECON_NOT_READY,
    ONBOARDING_TEMPLATE_NEEDED,
)
from app.stages.collect.onboarding.store import (
    ONBOARDING_HOST_TAKEN,
    ONBOARDING_POLICY_READS_INVALID,
    PolicyReadInput,
)
from integrations.suppliers.base import PublicReadFailure
from integrations.suppliers.registry import SITES
from integrations.suppliers.sites import TEMPLATES
from tests.conftest import LOCAL
from tests.support.fake_suppliers import KMRETAIL_PAGES, PASSWORD, USERNAME, FakeGateway
from tests.support.jobs_support import TEST_JOBS
from tests.support.onboarding_support import (
    ROBOTS_ALL,
    ROBOTS_PRODUCTS,
    Storefront,
    cafe24_site,
    godomall_site,
    plain_site,
    recorded_grants,
    wait_until,
)

pytestmark = pytest.mark.integration

HEADERS = {"X-ICBM-Client": "icbm-web"}
CAFE24_HOST = "shop.onboard-sample.co.kr"
CAFE24_ORIGIN = f"https://{CAFE24_HOST}"
CAFE24_KEY = "site-shop-onboard-sample-co-kr"
GODOMALL_HOST = "www.godo-sample.com"
PLAIN_HOST = "plain-sample.co.kr"
DETECTION_READS = ("/", "/member/login.html", "/member/login.php")
TABLES = (
    "onboarded_suppliers",
    "onboarding_transitions",
    "onboarding_hosts",
    "onboarding_policy_reads",
)


@pytest.fixture
def storefront() -> Storefront:
    return Storefront(
        sites={
            CAFE24_HOST: cafe24_site(),
            GODOMALL_HOST: godomall_site(),
            PLAIN_HOST: plain_site(),
        }
    )


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway(KMRETAIL_PAGES)


@pytest.fixture
def api(config: AppConfig, storefront: Storefront, gateway: FakeGateway) -> Iterator[TestClient]:
    app = create_app(
        config,
        extra_jobs=TEST_JOBS,
        onboarding_reader=storefront.reader(),
        supplier_gateway=gateway,
    )
    with TestClient(app, base_url=LOCAL) as client:
        yield client


def rows(config: AppConfig, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database_path(config.data_dir))
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def count(config: AppConfig, table: str) -> int:
    return int(rows(config, f"SELECT COUNT(*) FROM {table}")[0][0])  # type: ignore[call-overload]


def detect_api(api: TestClient, origin: str, template: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {"origin": origin}
    if template is not None:
        body["template"] = template
    response = api.post("/api/v1/onboarding/detect", json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def create_api(api: TestClient, detection: dict[str, object], **overrides: object) -> object:
    body = {
        "name": "온보드 샘플",
        "origin": detection["origin"],
        "template": detection["policy_template"],
        "detection_id": detection["detection_id"],
        "acknowledged": True,
        **overrides,
    }
    return api.post("/api/v1/onboarding/suppliers", json=body, headers=HEADERS)


def added(api: TestClient) -> dict[str, object]:
    response = create_api(api, detect_api(api, CAFE24_ORIGIN))
    assert response.status_code == 201, response.text  # type: ignore[attr-defined]
    return response.json()  # type: ignore[attr-defined,no-any-return]


def error_code(response: object) -> str:
    return str(response.json()["error"]["code"])  # type: ignore[attr-defined]


def connection_test(api: TestClient, key: str) -> dict[str, object]:
    queued = api.post(f"/api/v1/connect/suppliers/{key}/test", headers=HEADERS)
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    result: dict[str, object] = {}

    def done() -> bool:
        result.update(api.get(f"/api/v1/system/jobs/{job_id}", headers=HEADERS).json())
        return result.get("state") in ("SUCCEEDED", "DEAD")

    wait_until(done)
    return result


# ---------------------------------------------------------------- detection (§4 step 2)


@pytest.mark.parametrize(
    ("host", "platform", "markers"),
    [
        (
            CAFE24_HOST,
            "cafe24",
            {"cafe24_asset_host", "cafe24_module", "cafe24_login_form", "cafe24_login_module"},
        ),
        (
            GODOMALL_HOST,
            "godomall",
            {"godomall_skin", "godomall_storage_host", "godomall_login_form"},
        ),
    ],
)
def test_detection_reads_home_and_each_login_page_once_on_the_entered_host_only(
    storefront: Storefront,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    platform: str,
    markers: set[str],
) -> None:
    with recorded_grants(monkeypatch) as egress:
        found = detect(storefront.reader(), host, TEMPLATES)
    assert found.platform == platform and found.reason is None
    assert markers <= set(found.marker_names)
    assert found.reads == 3
    assert storefront.paths(host) == list(DETECTION_READS)
    assert {h for _, h, _ in storefront.requests} == {host}
    assert egress.grants == [(f"onboarding:{host}", frozenset({host}))]
    assert all(seen == frozenset({host}) for seen in storefront.grant_hosts_seen)
    assert storefront.resolved == [host]


def test_a_storefront_without_template_markers_is_unsupported(storefront: Storefront) -> None:
    found = detect(storefront.reader(), PLAIN_HOST, TEMPLATES)
    assert found.platform == UNSUPPORTED and found.reason is not None
    assert found.reason.value == "NO_MARKERS" and found.marker_names == ()
    assert storefront.paths(PLAIN_HOST) == list(DETECTION_READS)


def test_both_templates_matching_is_ambiguous_and_unsupported(storefront: Storefront) -> None:
    pages = cafe24_site()
    pages["/"] = (200, pages["/"][1] + godomall_site()["/"][1], {})
    pages["/member/login.php"] = godomall_site()["/member/login.php"]
    storefront.sites["both.onboard-sample.co.kr"] = pages
    found = detect(storefront.reader(), "both.onboard-sample.co.kr", TEMPLATES)
    assert found.platform == UNSUPPORTED and found.reason is not None
    assert found.reason.value == "AMBIGUOUS"


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fd00::1"])
def test_a_name_resolving_to_a_non_public_address_is_never_read(
    storefront: Storefront, address: str
) -> None:
    storefront.addresses[CAFE24_HOST] = ("93.184.216.34", address)
    reads = storefront.reader().read(CAFE24_HOST, ("/", "/robots.txt"))
    assert [r.failure for r in reads] == [PublicReadFailure.ADDRESS_REFUSED] * 2
    assert storefront.requests == []
    found = detect(storefront.reader(), CAFE24_HOST, TEMPLATES)
    assert found.platform == UNSUPPORTED and found.reason is not None
    assert found.reason.value == "HOME_UNREAD"


def test_a_redirecting_home_page_suggests_its_target(
    api: TestClient, storefront: Storefront
) -> None:
    storefront.sites["onboard-sample.co.kr"] = {
        "/": (301, "", {"location": f"{CAFE24_ORIGIN}/"}),
    }
    detection = detect_api(api, "https://onboard-sample.co.kr")
    assert detection["detected"] == UNSUPPORTED and detection["reason"] == "HOME_REDIRECT"
    assert detection["suggested_origin"] == CAFE24_ORIGIN
    assert detection["policy"] == [] and detection["policy_reads"] == 0


# ---------------------------------------------------------------- policy reads (§4 step 3)


def test_the_policy_reads_are_robots_and_the_template_terms_and_warn_only(
    storefront: Storefront,
) -> None:
    storefront.sites[CAFE24_HOST] = cafe24_site(ROBOTS_PRODUCTS)
    rules = TEMPLATES["cafe24"].detection
    assert rules is not None
    robots, terms = read_policies(storefront.reader(), CAFE24_HOST, rules)
    assert storefront.paths(CAFE24_HOST) == ["/robots.txt", "/member/agreement.html"]
    assert robots.status == 200 and robots.robots is not None
    assert robots.robots.disallows_products and not robots.robots.disallows_all
    assert robots.robots.crawl_delay == "10"
    assert [w.value for w in robots.warnings] == ["ROBOTS_DISALLOWS_PRODUCTS"]
    assert terms.status == 200 and terms.warnings == () and terms.content_sha256 is not None
    storefront.sites[CAFE24_HOST] = cafe24_site(ROBOTS_ALL)
    robots, _ = read_policies(storefront.reader(), CAFE24_HOST, rules)
    assert [w.value for w in robots.warnings] == ["ROBOTS_DISALLOWS_ALL"]
    storefront.sites[CAFE24_HOST] = cafe24_site(None)
    robots, _ = read_policies(storefront.reader(), CAFE24_HOST, rules)
    assert robots.status == 404 and [w.value for w in robots.warnings] == ["ROBOTS_ABSENT"]


def test_a_robots_disallow_is_shown_acknowledged_and_saved_never_refused(
    api: TestClient, storefront: Storefront, config: AppConfig
) -> None:
    storefront.sites[CAFE24_HOST] = cafe24_site(ROBOTS_PRODUCTS)
    detection = detect_api(api, CAFE24_ORIGIN)
    assert detection["detected"] == "cafe24" and detection["policy_template"] == "cafe24"
    assert detection["reads"] == 3 and detection["policy_reads"] == 2
    robots = detection["policy"][0]  # type: ignore[index]
    assert robots["document"] == "ROBOTS" and robots["warnings"] == ["ROBOTS_DISALLOWS_PRODUCTS"]
    assert robots["robots"]["lines"][1] == "Disallow: /product/"
    assert robots["url"] == f"{CAFE24_ORIGIN}/robots.txt"
    # Detection writes nothing.
    assert all(count(config, table) == 0 for table in TABLES)
    # Five reads in all: home, two login pages, robots.txt and the terms page.
    assert len(storefront.paths(CAFE24_HOST)) == 5

    refused = create_api(api, detection, acknowledged=False)
    assert refused.status_code == 422 and error_code(refused) == ONBOARDING_POLICY_NOT_ACKNOWLEDGED  # type: ignore[attr-defined]
    assert count(config, "onboarded_suppliers") == 0

    created = create_api(api, detection)
    assert created.status_code == 201, created.text  # type: ignore[attr-defined]
    record = created.json()  # type: ignore[attr-defined]
    assert record["supplier_key"] == CAFE24_KEY and record["state"] == "DRAFT"
    assert record["template"] == "cafe24"
    assert sorted((r["document"], r["http_status"]) for r in record["policy_reads"]) == [
        ("ROBOTS", 200),
        ("TERMS", 200),
    ]
    assert all(r["acknowledged_by"] for r in record["policy_reads"])
    assert record["recon"] == {
        "platform_confirmed": True,
        "policy_acknowledged": True,
        "credentials_stored": False,
        "connection_verified": False,
    }
    # No further read: saving reuses what the operator was shown.
    assert len(storefront.paths(CAFE24_HOST)) == 5
    stored = rows(config, "SELECT document, path, http_status FROM onboarding_policy_reads")
    assert sorted(stored) == [
        ("ROBOTS", "/robots.txt", 200),
        ("TERMS", "/member/agreement.html", 200),
    ]
    (details,) = rows(
        config,
        "SELECT details_json FROM audit_events WHERE action = 'ONBOARDING_SUPPLIER_CREATED'",
    )[0:1]
    audited = json.loads(str(details[0]))
    assert audited["detected"] == "cafe24" and audited["confirmed"] == "cafe24"
    assert "cafe24_login_form" in audited["markers"]
    # Names, statuses and paths only: never the policy text or the operator's name for the site.
    text = json.dumps(audited, ensure_ascii=False)
    assert "Disallow" not in text and "이용약관" not in text and "온보드" not in text


def test_the_store_keeps_at_most_three_policy_reads(container: Container) -> None:
    read = PolicyReadInput("ROBOTS", "/robots.txt", 200, None, datetime(2026, 10, 12, tzinfo=UTC))
    with pytest.raises(AppError) as caught:
        container.onboarding.create(
            name="샘플",
            origin=CAFE24_ORIGIN,
            template="cafe24",
            actor="operator",
            policy_reads=[read] * 4,
        )
    assert caught.value.code == ONBOARDING_POLICY_READS_INVALID
    assert container.onboarding.all_current() == ()


# ---------------------------------------------------------------- the API's refusals


@pytest.mark.parametrize(
    "origin",
    [
        "http://shop.onboard-sample.co.kr",
        "https://192.168.0.10",
        "https://localhost",
        "https://shop.onboard-sample.co.kr:8443",
        "https://shop.onboard-sample.co.kr/shop",
        "https://*.onboard-sample.co.kr",
    ],
)
def test_a_guarded_origin_is_refused_before_any_read(
    api: TestClient, storefront: Storefront, origin: str
) -> None:
    response = api.post("/api/v1/onboarding/detect", json={"origin": origin}, headers=HEADERS)
    assert response.status_code == 422, response.text
    assert origin not in response.text
    assert storefront.requests == [] and storefront.resolved == []


def test_a_repository_storefront_and_a_duplicate_are_refused_before_any_read(
    api: TestClient, storefront: Storefront
) -> None:
    for site in SITES.values():
        response = api.post(
            "/api/v1/onboarding/detect",
            json={"origin": f"https://{site.config.storefront_host}"},
            headers=HEADERS,
        )
        assert response.status_code == 409 and error_code(response) == ONBOARDING_HOST_TAKEN
    assert storefront.requests == []
    added(api)
    before = len(storefront.requests)
    response = api.post(
        "/api/v1/onboarding/detect", json={"origin": CAFE24_ORIGIN}, headers=HEADERS
    )
    assert response.status_code == 409 and error_code(response) == ONBOARDING_HOST_TAKEN
    assert response.json()["error"]["details"] == {"supplier_key": CAFE24_KEY}
    assert len(storefront.requests) == before


def test_an_unknown_or_mismatched_detection_is_refused(api: TestClient, config: AppConfig) -> None:
    detection = detect_api(api, CAFE24_ORIGIN)
    unknown = create_api(api, detection, detection_id="0" * 32)
    assert unknown.status_code == 404  # type: ignore[attr-defined]
    other = create_api(api, detection, origin=f"https://{GODOMALL_HOST}")
    assert error_code(other) == ONBOARDING_DETECTION_MISMATCH
    platform = create_api(api, detection, template="godomall")
    assert error_code(platform) == ONBOARDING_DETECTION_MISMATCH
    unknown_template = create_api(api, detection, template="shopify")
    assert unknown_template.status_code == 422  # type: ignore[attr-defined]
    assert count(config, "onboarded_suppliers") == 0


def test_an_unsupported_site_stops_unless_the_operator_chooses_a_template(
    api: TestClient, storefront: Storefront, config: AppConfig
) -> None:
    detection = detect_api(api, f"https://{PLAIN_HOST}")
    assert detection["detected"] == UNSUPPORTED and detection["policy_template"] is None
    assert detection["policy"] == []
    assert storefront.paths(PLAIN_HOST) == list(DETECTION_READS)
    refused = create_api(api, detection, template="cafe24")
    assert error_code(refused) == ONBOARDING_TEMPLATE_NEEDED
    assert count(config, "onboarded_suppliers") == 0
    # The operator's own choice: only the policy reads of that template are made.
    chosen = detect_api(api, f"https://{PLAIN_HOST}", template="godomall")
    assert chosen["chosen"] == "godomall" and chosen["policy_template"] == "godomall"
    assert chosen["detected"] == UNSUPPORTED and chosen["policy_reads"] == 2
    # The earlier detection is reused: no home or login page is read again.
    assert chosen["reads"] == 0
    assert storefront.paths(PLAIN_HOST)[3:] == ["/robots.txt", "/service/agreement.php"]
    created = create_api(api, chosen)
    assert created.status_code == 201, created.text  # type: ignore[attr-defined]
    assert created.json()["template"] == "godomall"  # type: ignore[attr-defined]
    (details,) = rows(
        config,
        "SELECT details_json FROM audit_events WHERE action = 'ONBOARDING_SUPPLIER_CREATED'",
    )
    assert json.loads(str(details[0]))["detected"] == UNSUPPORTED


# ---------------------------------------------------------------- CONNECT and RECON (§4 4–5)


def test_recon_waits_for_the_login_and_a_passed_connection_test(api: TestClient) -> None:
    record = added(api)
    key = str(record["supplier_key"])
    # The new key is known to CONNECT at once (the registry rebinds it).
    listed = {s["supplier_key"] for s in api.get("/api/v1/connect/suppliers").json()}
    assert key in listed

    early = api.post(f"/api/v1/onboarding/suppliers/{key}/recon", headers=HEADERS)
    assert early.status_code == 409 and error_code(early) == ONBOARDING_RECON_NOT_READY
    assert early.json()["error"]["details"] == {
        "platform_confirmed": True,
        "policy_acknowledged": True,
        "credentials_stored": False,
        "connection_verified": False,
    }
    saved = api.put(
        f"/api/v1/connect/suppliers/{key}/credentials",
        json={"username": USERNAME, "password": PASSWORD},
        headers=HEADERS,
    )
    assert saved.status_code == 200, saved.text
    assert PASSWORD not in saved.text
    still = api.post(f"/api/v1/onboarding/suppliers/{key}/recon", headers=HEADERS)
    assert error_code(still) == ONBOARDING_RECON_NOT_READY
    assert still.json()["error"]["details"]["credentials_stored"] is True
    assert still.json()["error"]["details"]["connection_verified"] is False

    job = connection_test(api, key)
    assert job["state"] == "SUCCEEDED", job
    view = api.get(f"/api/v1/onboarding/suppliers/{key}").json()
    assert view["state"] == "DRAFT" and view["connection"]["state"] == "READY"
    recon = api.post(f"/api/v1/onboarding/suppliers/{key}/recon", headers=HEADERS)
    assert recon.status_code == 200, recon.text
    assert recon.json()["state"] == "RECON"
    again = api.post(f"/api/v1/onboarding/suppliers/{key}/recon", headers=HEADERS)
    assert again.status_code == 409
    # OB-10: an onboarded RECON site is still not collectable.
    container = api.app.state.container  # type: ignore[attr-defined]
    assert container.supplier_catalog.collectable(key) is False
    assert [r["state"] for r in api.get("/api/v1/onboarding/suppliers").json()] == ["RECON"]


def test_a_failed_connection_test_keeps_the_draft(api: TestClient, gateway: FakeGateway) -> None:
    key = str(added(api)["supplier_key"])
    api.put(
        f"/api/v1/connect/suppliers/{key}/credentials",
        json={"username": USERNAME, "password": "wrong-password"},
        headers=HEADERS,
    )
    job = connection_test(api, key)
    assert job["state"] == "DEAD"
    refused = api.post(f"/api/v1/onboarding/suppliers/{key}/recon", headers=HEADERS)
    assert error_code(refused) == ONBOARDING_RECON_NOT_READY
    assert api.get(f"/api/v1/onboarding/suppliers/{key}").json()["state"] == "DRAFT"


def test_disable_unbinds_the_supplier(api: TestClient) -> None:
    key = str(added(api)["supplier_key"])
    disabled = api.post(f"/api/v1/onboarding/suppliers/{key}/disable", headers=HEADERS)
    assert disabled.status_code == 200 and disabled.json()["state"] == "DISABLED"
    assert disabled.json()["connection"] is None
    listed = {s["supplier_key"] for s in api.get("/api/v1/connect/suppliers").json()}
    assert key not in listed
    assert (
        api.post(f"/api/v1/onboarding/suppliers/{key}/disable", headers=HEADERS).status_code == 409
    )
    refused = api.post(f"/api/v1/onboarding/suppliers/{key}/recon", headers=HEADERS)
    assert refused.status_code == 409


def test_every_onboarding_write_needs_the_client_header(api: TestClient) -> None:
    detection = detect_api(api, CAFE24_ORIGIN)
    response = api.post(
        "/api/v1/onboarding/suppliers",
        json={
            "name": "x",
            "origin": CAFE24_ORIGIN,
            "template": "cafe24",
            "detection_id": detection["detection_id"],
            "acknowledged": True,
        },
    )
    assert response.status_code == 403
    assert api.post("/api/v1/onboarding/detect", json={"origin": CAFE24_ORIGIN}).status_code == 403


# ---------------------------------------------------------------- credentials (OB-05)


def test_the_login_reaches_only_the_secret_store(
    api: TestClient, config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    key = str(added(api)["supplier_key"])
    saved = api.put(
        f"/api/v1/connect/suppliers/{key}/credentials",
        json={"username": USERNAME, "password": PASSWORD},
        headers=HEADERS,
    )
    assert saved.status_code == 200
    assert connection_test(api, key)["state"] == "SUCCEEDED"
    responses = [
        api.get(f"/api/v1/onboarding/suppliers/{key}").text,
        api.get("/api/v1/onboarding/suppliers").text,
        api.post(f"/api/v1/onboarding/suppliers/{key}/recon", headers=HEADERS).text,
        api.get(f"/api/v1/connect/suppliers/{key}/credentials").text,
    ]
    assert not any(PASSWORD in text for text in responses)
    container = api.app.state.container  # type: ignore[attr-defined]
    stored = container.connect._credentials.load(key)
    assert stored is not None and stored.password == PASSWORD
    for table in (*TABLES, "audit_events"):
        dumped = json.dumps(rows(config, f"SELECT * FROM {table}"), ensure_ascii=False, default=str)
        assert PASSWORD not in dumped and USERNAME not in dumped, table
    database = database_path(config.data_dir).read_bytes()
    assert PASSWORD.encode() not in database and USERNAME.encode() not in database
    logged = "\n".join(
        f"{r.getMessage()} {json.dumps(r.__dict__, default=str, ensure_ascii=False)}"
        for r in caplog.records
    )
    assert PASSWORD not in logged and USERNAME not in logged


# ---------------------------------------------------------------- repository sites (OB-07)


def test_the_repository_suppliers_are_unchanged(api: TestClient) -> None:
    before = {s["supplier_key"]: s for s in api.get("/api/v1/connect/suppliers").json()}
    key = str(added(api)["supplier_key"])
    after = {s["supplier_key"]: s for s in api.get("/api/v1/connect/suppliers").json()}
    assert set(after) == set(before) | {key}
    assert all(after[k] == before[k] for k in before)
    container = api.app.state.container  # type: ignore[attr-defined]
    for site_key, site in SITES.items():
        assert container.supplier_catalog.definitions[site_key] is site.definition
    assert api.get("/api/v1/onboarding/suppliers").json()[0]["supplier_key"] == key


# ---------------------------------------------------------------- reader and robots details


def test_the_reader_caps_each_read_and_never_follows_a_redirect(storefront: Storefront) -> None:
    from integrations.suppliers.transport.gateway import PolicedPublicReader
    from integrations.suppliers.transport.pacing import RequestPacer

    storefront.sites[CAFE24_HOST]["/moved"] = (
        302,
        "",
        {"location": "https://elsewhere.example.net/x"},
    )
    reader = PolicedPublicReader(
        pacer=RequestPacer(sleep=lambda _: None),
        http_transport=storefront.reader()._http_transport,
        resolve=storefront.resolve,
        max_bytes=10,
    )
    home, moved = reader.read(CAFE24_HOST, ("/", "/moved"))
    assert home.status == 200 and home.truncated and len(home.content) == 10
    assert moved.status == 302 and moved.location_host == "elsewhere.example.net"
    assert moved.location_path == "/x"
    assert {h for _, h, _ in storefront.requests} == {CAFE24_HOST}
    with pytest.raises(ValueError):
        reader.read(CAFE24_HOST, ("/search?q=1",))


@pytest.mark.parametrize(
    ("robots", "products", "everything"),
    [
        ("User-agent: *\nDisallow: /product/\nAllow: /product/\n", False, False),
        ("User-agent: Googlebot\nDisallow: /\n", False, False),
        ("User-agent: *\nDisallow: /*.html$\n", False, False),
        ("User-agent: *\nDisallow: /prod\n", True, False),
        ("User-agent: a\nUser-agent: *\nDisallow: /\n", True, True),
        ("", False, False),
    ],
)
def test_robots_rules_are_judged_for_the_star_group_by_longest_match(
    robots: str, products: bool, everything: bool
) -> None:
    from app.stages.collect.onboarding.detect import summarise_robots

    summary = summarise_robots(robots, "/product/")
    assert (summary.disallows_products, summary.disallows_all) == (products, everything)
