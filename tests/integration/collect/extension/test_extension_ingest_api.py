"""The extension ingest over the real application (ADR-0019 E1; specification 5907009512 §3).

Every test serves the real app with the real KM통상 collection definition and the reviewed KM
capture policy. The captures are synthetic, nothing can reach a network, and the supplier's host
appears only as text.
"""

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container, _server_final_scan
from app.stages.collect.extension.pairing import (
    BODY_DIGEST_HEADER,
    NONCE_HEADER,
    SIGNATURE_HEADER,
    PairingRecord,
)
from app.stages.collect.extension.policy import MAX_HTML_BYTES, MAX_IMAGE_REFS, MAX_NODES
from tests.support.extension_support import (
    BODY,
    CAPTURES,
    EXTENSION_ID,
    HEAD,
    KM_POLICY,
    ORIGIN,
    POLICY,
    PRODUCT_URL,
    RUNS,
    SUPPLIER,
    envelope,
    frame,
    pair,
    policy_reference,
    post_capture,
    signed,
    table_counts,
    transport,
    untouched,
    wait_for_outcome,
)

pytestmark = pytest.mark.integration


def _container(client: TestClient) -> Container:
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    return container


@pytest.fixture
def paired(client: TestClient) -> Iterator[PairingRecord]:
    yield pair(_container(client))


def _runs(client: TestClient) -> list[dict[str, Any]]:
    listed: list[dict[str, Any]] = client.get(RUNS).json()["runs"]
    return listed


def _code(response: Any) -> str:
    code: str = response.json()["error"]["code"]
    return code


# ---------------------------------------------------------------- the policy read (H-2)


def test_the_policy_read_serves_exactly_the_canonical_bytes(
    client: TestClient, paired: PairingRecord
) -> None:
    response = client.get(POLICY, headers=signed(paired, method="GET", path=POLICY))
    assert response.status_code == 200
    reference = policy_reference()
    assert hashlib.sha256(response.content).hexdigest() == reference["digest"]
    assert response.headers["X-ICBM-Capture-Policy-Digest"] == reference["digest"]
    assert response.headers["X-ICBM-Capture-Policy-Revision"] == reference["revision"]
    # The one canonical serialization of the repository file, and topology only.
    assert json.loads(response.content) == json.loads(KM_POLICY.read_text("utf-8"))
    assert response.content == json.dumps(
        json.loads(KM_POLICY.read_text("utf-8")),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    # Only the paired origin is named, and only on this response.
    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert response.headers["Cache-Control"] == "no-store"


def test_the_policy_read_needs_everything_the_ingest_needs(
    client: TestClient, paired: PairingRecord
) -> None:
    def get(**signing: Any) -> Any:
        return client.get(POLICY, headers=signed(paired, method="GET", path=POLICY, **signing))

    assert _code(get(client_header=None)) == "CLIENT_HEADER_REQUIRED"
    assert _code(get(origin="https://kmretail.co.kr")) == "EXTENSION_ORIGIN_MISMATCH"
    assert _code(get(overrides={SIGNATURE_HEADER: "0" * 64})) == "EXTENSION_SIGNATURE_INVALID"
    assert (
        _code(client.get(POLICY, headers={"X-ICBM-Client": "x"})) == "EXTENSION_IDENTITY_MISMATCH"
    )
    # A read signed for a body is not a policy read.
    assert _code(get(body=b"x")) == "EXTENSION_BODY_DIGEST_MISMATCH"
    # A request signed for the ingest path does not open the policy.
    assert (
        _code(client.get(POLICY, headers=signed(paired, method="POST", path=CAPTURES)))
        == "EXTENSION_SIGNATURE_INVALID"
    )
    unknown = "/api/v1/collect/extension/capture-policies/othershop"
    assert (
        _code(client.get(unknown, headers=signed(paired, method="GET", path=unknown)))
        == "COLLECT_SUPPLIER_UNKNOWN"
    )
    _container(client).extension_pairing.revoke()
    assert _code(get()) == "EXTENSION_NOT_PAIRED"


# ---------------------------------------------------------------- an accepted capture


def test_an_accepted_capture_opens_and_settles_one_canonical_run(
    client: TestClient, config: AppConfig, paired: PairingRecord
) -> None:
    before = table_counts(config)
    response = post_capture(client, paired)
    assert response.status_code == 202
    accepted = response.json()
    # A transport state and an identity to read back: never a result.
    assert accepted["state"] == "ACCEPTED" and set(accepted) == {
        "collection_run_id",
        "job_id",
        "correlation_id",
        "state",
    }
    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
    run = wait_for_outcome(client, accepted["collection_run_id"])
    reference = policy_reference()
    assert (run["outcome"], run["detail"]) == ("NO_REVISION", "EXTENSION_COMPARE_ONLY")
    assert (run["revision_id"], run["facts_status"]) == (None, None)
    assert (run["supplier_key"], run["source_url"]) == (SUPPLIER, PRODUCT_URL)
    assert (
        run["transport_kind"],
        run["capture_policy_revision"],
        run["capture_policy_digest"],
    ) == ("EXTENSION", reference["revision"], reference["digest"])
    # Exactly the one authorized run, and its one job.
    after = table_counts(config)
    assert after["collection_runs"] == before["collection_runs"] + 1
    assert after["jobs"] == before["jobs"] + 1
    assert [r["collection_run_id"] for r in _runs(client)] == [accepted["collection_run_id"]]
    # Zero write: no revision, field, evidence, image reference, source asset, Product or
    # Adaptive row. Every table outside the run's own is exactly as it was.
    assert untouched(before, after) == {}
    for table in (
        "product_facts_revisions",
        "product_facts_fields",
        "product_facts_evidence",
        "product_facts_image_refs",
        "source_assets",
        "adaptive_shadow_records",
        "adaptive_capture_candidates",
        "adaptive_validation_samples",
    ):
        assert after[table] == 0, table
    # The extension run reserved no server product read and froze no shadow or capture decision.
    record = _container(client).collection.run(accepted["collection_run_id"])
    assert (record.product_read_at, record.frozen, record.source_product_id) == (None, None, "9001")


def test_an_unresolved_identity_is_the_same_answer_it_is_on_the_direct_path(
    client: TestClient, paired: PairingRecord
) -> None:
    # A capture that states no product number: NO_REVISION with the parser's own reason.
    response = post_capture(client, paired, envelope(frame(head="")))
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("NO_REVISION", "the page declares no product number")


def test_private_material_inside_the_scope_still_refuses(
    client: TestClient, config: AppConfig, paired: PairingRecord
) -> None:
    # ADR-0019 §6, AC-13 (the C1 regression pair, second half): the server's own final scan
    # refuses residual private material wherever it sits, the product scope included.
    before = table_counts(config)
    private = BODY + '<div id="extra"><p>문의 010-0000-0000</p></div>'
    response = post_capture(client, paired, envelope(frame(body=private)))
    assert response.status_code == 202
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("FAILED", "EXTENSION_FINAL_SCAN_REFUSED")
    assert (run["revision_id"], run["transport_kind"]) == (None, "EXTENSION")
    assert untouched(before, table_counts(config)) == {}


IN_SCOPE = '<div class="xans-product-action">'


@pytest.mark.parametrize(
    "addition",
    [
        # GPT audit 5365019650 B-1: a private region with no phone, e-mail or token in it. The
        # sanitizer takes it out and its final scan then finds nothing, so only the gate sees it.
        '<div class="member-benefit"><p>회원 전용 안내</p></div>',
        '<div id="mypage-summary"><p>등급 안내</p></div>',
        # What the sanitizer removes rather than excludes: a query on a URL it would keep.
        '<img src="/web/product/extra/synthetic-9001.jpg?v=20260930">',
    ],
    ids=["private-class-region", "private-id-region", "url-query"],
)
def test_a_capture_the_sanitizer_would_have_to_clean_fails_the_run(
    client: TestClient, config: AppConfig, paired: PairingRecord, addition: str
) -> None:
    # Owner amendment 5909645067 §1: the pipeline goes on with the capture as it arrived, so a
    # capture the capture owner's sanitizer had to take anything private or secret out of never
    # reaches the extractor. It sits inside the product scope, where the browser cut kept it.
    assert BODY.count(IN_SCOPE) == 1
    before = table_counts(config)
    response = post_capture(
        client, paired, envelope(frame(body=BODY.replace(IN_SCOPE, addition + IN_SCOPE)))
    )
    assert response.status_code == 202
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("FAILED", "EXTENSION_FINAL_SCAN_REFUSED")
    assert (run["revision_id"], run["transport_kind"]) == (None, "EXTENSION")
    assert untouched(before, table_counts(config)) == {}


def test_the_final_gate_names_kinds_and_boundaries_and_never_a_value() -> None:
    gate = _server_final_scan
    assert gate(frame()) == ()
    private = gate(frame(body=BODY + '<div class="member-benefit"><p>회원 전용 안내</p></div>'))
    assert private == ("SANITIZER_EXCLUDED:PRIVATE@div#.member-benefit",)
    removed = gate(frame(body=BODY + '<img src="/a.jpg?session=synthetic-value">'))
    assert removed == ("IMAGE_REFERENCE_QUERY:src@img#.",)
    residual = gate(frame(body=BODY + "<p>문의 010-0000-0000</p>"))
    assert len(residual) == 1 and residual[0].startswith("residual secret or private material")
    handler = gate(frame(body=BODY + '<p onclick="synthetic-value">x</p>'))
    assert handler == ("SANITIZER_REMOVED:EVENT_HANDLER:onclick@p#.",)
    for findings in (private, removed, residual, handler):
        assert all(
            "회원" not in f and "synthetic-value" not in f and "010-" not in f for f in findings
        )


def test_the_gate_scans_inside_every_region_the_sanitizer_sets_aside() -> None:
    # The sanitizer drops a navigation or non-authoritative region whole and looks no further,
    # but the extractor receives the whole capture. So the gate opens each one and scans it too.
    gate = _server_final_scan
    # Clean regions of those kinds are not findings.
    assert gate(frame(body=BODY + '<div class="banner"><p>안내</p></div><nav>메뉴</nav>')) == ()
    # What sits inside them is judged exactly like the rest of the capture.
    for region in (
        '<div class="banner"><p>a@synthetic.invalid</p></div>',
        "<footer><p>문의 010-0000-0000</p></footer>",
        '<div class="related"><div class="recent"><p>b@synthetic.invalid</p></div></div>',
    ):
        findings = gate(frame(body=BODY + region))
        assert len(findings) == 1, region
        assert findings[0].startswith("residual secret or private material"), region
        assert "synthetic.invalid" not in findings[0] and "010-" not in findings[0]
    # A private region nested inside one is found as well.
    nested = gate(
        frame(body=BODY + '<div class="related"><div class="member-note"><p>안내</p></div></div>')
    )
    assert nested == ("SANITIZER_EXCLUDED:PRIVATE@div#.member-note",)


def test_an_image_reference_is_judged_as_a_locator_not_as_a_secret() -> None:
    # Suppliers name uploaded images with long hashes. A reference is a plain locator or a finding;
    # a generic secret pattern never decides it.
    gate = _server_final_scan
    hashed = "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d"
    for clean in (
        f'<img src="//kmretail.co.kr/web/product/big/202409/{hashed}.jpg">',
        f'<img ec-data-src="/web/upload/{hashed}{hashed}.png" data-original="/web/{hashed}.jpg">',
        '<img srcset=" /a.jpg 1x, /b.jpg 2x">',
        '<img srcset="/a.jpg 480w,/b.jpg 800w" src="https://kmretail.co.kr/c.jpg">',
    ):
        assert gate(frame(body=BODY + clean)) == (), clean
    for bad, kind in (
        ('<img src="/a.jpg?v=1">', "QUERY"),
        ('<img src="/a.jpg#frag">', "FRAGMENT"),
        ('<img src="https://user:pw@kmretail.co.kr/a.jpg">', "CREDENTIALS"),
        ('<img src="data:image/png;base64,AAAA">', "SCHEME"),
        ('<img src="javascript:void(0)">', "SCHEME"),
        ('<img src="/a b.jpg">', "NOT_A_LOCATOR"),
        ('<img srcset="/a.jpg 1x, /b.jpg wide">', "NOT_A_LOCATOR"),
        ('<img src="/eyJhbGciOiJIUzI1NiJ9abc.jpg">', "TOKEN_SHAPED"),
        ('<img data-src="/a.jpg?token=synthetic-value">', "QUERY"),
    ):
        findings = gate(frame(body=BODY + bad))
        assert len(findings) == 1 and findings[0].startswith(f"IMAGE_REFERENCE_{kind}:"), bad
        assert "synthetic-value" not in findings[0] and "pw@" not in findings[0]
    # The mask is for image references only: the same value anywhere else is still the scan's.
    assert gate(frame(body=BODY + f'<p class="{hashed}">x</p>')) != ()


def test_a_hash_named_image_is_accepted_end_to_end(
    client: TestClient, config: AppConfig, paired: PairingRecord
) -> None:
    hashed = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
    body = BODY.replace("synthetic-9001.jpg", f"{hashed}.jpg")
    assert body != BODY
    response = post_capture(client, paired, envelope(frame(body=body)))
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("NO_REVISION", "EXTENSION_COMPARE_ONLY")


@pytest.mark.parametrize(
    ("html", "violation"),
    [
        (frame(body=BODY + "<![CDATA[<p>a@synthetic.invalid</p>]]>"), "DECLARATION"),
        (
            "<!doctype html>" + frame().removeprefix("<!doctype html>") + "<!doctype html>",
            "DECLARATION",
        ),
        (frame().replace("<!doctype html>", "<!DOCTYPE svg>"), "DECLARATION"),
        (frame(body=BODY + "<div>" * 120 + "</div>" * 120), "DEPTH_EXCEEDED"),
        (
            frame(body=BODY + '<div class="xans-product-review"><p>후기</p></div>'),
            "REGION_EXCLUDED:div",
        ),
    ],
    ids=["cdata", "second-doctype", "other-doctype", "depth", "excluded-region"],
)
def test_the_structure_check_refuses_what_the_extension_never_writes(
    client: TestClient, config: AppConfig, paired: PairingRecord, html: str, violation: str
) -> None:
    from app.stages.collect.extension.capture import policy_violations

    policy = _container(client).extension_capture.policy(SUPPLIER)
    assert violation in policy_violations(html, policy)
    before = table_counts(config)
    response = post_capture(client, paired, envelope(html))
    assert response.status_code == 202
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("FAILED", "EXTENSION_CAPTURE_POLICY_VIOLATION")
    assert untouched(before, table_counts(config)) == {}


def test_an_extension_request_from_a_non_loopback_host_is_refused_before_anything(
    client: TestClient, config: AppConfig, paired: PairingRecord
) -> None:
    # The loopback host is required together with everything else (TrustedHostMiddleware): a
    # correctly signed request that names another host opens no run and consumes no nonce.
    before = table_counts(config)
    for host in ("icbm.example.invalid", "192.168.0.10:8790", "localhost.evil.invalid"):
        response = post_capture(client, paired, envelope(), overrides={"host": host})
        assert response.status_code == 400, host
        policy = client.get(
            POLICY, headers=signed(paired, method="GET", path=POLICY, overrides={"host": host})
        )
        assert policy.status_code == 400, host
    assert table_counts(config) == before
    assert _container(client).collection.recent_runs() == ()


def test_a_capture_the_policy_does_not_allow_fails_the_run(
    client: TestClient, paired: PairingRecord
) -> None:
    beyond = frame(head=HEAD + '<meta property="og:title" content="합성 샘플 상품">')
    response = post_capture(client, paired, envelope(beyond))
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("FAILED", "EXTENSION_CAPTURE_POLICY_VIOLATION")


# ---------------------------------------------------------------- refusals open no run


def _oversize() -> str:
    return frame(body=BODY + "<p>" + "가" * (MAX_HTML_BYTES // 3 + 1) + "</p>")


REFUSALS: list[tuple[str, dict[str, Any], dict[str, Any], int, str]] = [
    # (name, envelope changes, signing changes, HTTP status, code)
    ("no client header", {}, {"client_header": None}, 403, "CLIENT_HEADER_REQUIRED"),
    ("another origin", {}, {"origin": "https://kmretail.co.kr"}, 401, "EXTENSION_ORIGIN_MISMATCH"),
    (
        "another extension",
        {},
        {"origin": "chrome-extension://" + "p" * 32},
        401,
        "EXTENSION_ORIGIN_MISMATCH",
    ),
    (
        "forged signature",
        {},
        {"overrides": {SIGNATURE_HEADER: "0" * 64}},
        401,
        "EXTENSION_SIGNATURE_INVALID",
    ),
    ("stale timestamp", {}, {"now": 1_000_000.0}, 401, "EXTENSION_TIMESTAMP_STALE"),
    (
        "another body",
        {},
        {"overrides": {BODY_DIGEST_HEADER: "0" * 64}},
        401,
        "EXTENSION_SIGNATURE_INVALID",
    ),
    ("bytes ceiling", {"html": _oversize()}, {}, 403, "EXTENSION_CEILING_HTML_BYTES"),
    (
        "node ceiling",
        {
            "html": frame(
                body='<div class="xans-product-detail">' + "<p></p>" * MAX_NODES + "</div>"
            )
        },
        {},
        403,
        "EXTENSION_CEILING_NODES",
    ),
    (
        "image ceiling",
        {
            "html": frame(
                body='<div class="xans-product-detail">'
                + '<img src="/a.jpg">' * (MAX_IMAGE_REFS + 1)
                + "</div>"
            )
        },
        {},
        403,
        "EXTENSION_CEILING_IMAGE_REFS",
    ),
    (
        "status not observed",
        {"transport": transport(response_status=0)},
        {},
        422,
        "EXTENSION_EVIDENCE_STATUS",
    ),
    (
        "status not 200",
        {"transport": transport(response_status=404)},
        {},
        422,
        "EXTENSION_EVIDENCE_STATUS",
    ),
    (
        "redirected",
        {"transport": transport(redirect_count=1)},
        {},
        422,
        "EXTENSION_EVIDENCE_REDIRECT",
    ),
    (
        "location is not the navigated URL",
        {"transport": transport(navigation_name="https://kmretail.co.kr/product/other/1/")},
        {},
        422,
        "EXTENSION_EVIDENCE_URL",
    ),
    (
        "not html",
        {"transport": transport(content_type="application/json")},
        {},
        422,
        "EXTENSION_EVIDENCE_CONTENT_TYPE",
    ),
    (
        "no charset",
        {"transport": transport(character_set="")},
        {},
        422,
        "EXTENSION_EVIDENCE_CHARSET",
    ),
    (
        "another host",
        {
            "transport": transport(
                url="https://www.kmretail.co.kr/product/a/9001/",
                navigation_name="https://www.kmretail.co.kr/product/a/9001/",
            )
        },
        {},
        422,
        "EXTENSION_TARGET_REFUSED",
    ),
    (
        "a listing path",
        {
            "transport": transport(
                url="https://kmretail.co.kr/category/23/",
                navigation_name="https://kmretail.co.kr/category/23/",
            )
        },
        {},
        422,
        "EXTENSION_TARGET_REFUSED",
    ),
    (
        "a query",
        {
            "transport": transport(
                url=PRODUCT_URL + "?member=1", navigation_name=PRODUCT_URL + "?member=1"
            )
        },
        {},
        422,
        "EXTENSION_TARGET_REFUSED",
    ),
    (
        "http",
        {
            "transport": transport(
                url="http://kmretail.co.kr/product/a/9001/",
                navigation_name="http://kmretail.co.kr/product/a/9001/",
            )
        },
        {},
        422,
        "EXTENSION_TARGET_REFUSED",
    ),
    (
        "another policy digest",
        {"policy": {**policy_reference(), "digest": "0" * 64}},
        {},
        403,
        "CAPTURE_POLICY_MISMATCH",
    ),
    (
        "another policy revision",
        {"policy": {**policy_reference(), "revision": "kmretail-capture-0"}},
        {},
        403,
        "CAPTURE_POLICY_MISMATCH",
    ),
    ("an unknown supplier", {"supplier_key": "othershop"}, {}, 404, "COLLECT_SUPPLIER_UNKNOWN"),
    ("cookies in the envelope", {"cookies": "a=b"}, {}, 422, "EXTENSION_PAYLOAD_INVALID"),
    (
        "request headers in the envelope",
        {"request_headers": {"Authorization": "x"}},
        {},
        422,
        "EXTENSION_PAYLOAD_INVALID",
    ),
    (
        "storage in the envelope",
        {"local_storage": {"k": "v"}},
        {},
        422,
        "EXTENSION_PAYLOAD_INVALID",
    ),
    ("image bytes in the envelope", {"image_bytes": "AAAA"}, {}, 422, "EXTENSION_PAYLOAD_INVALID"),
]


@pytest.mark.parametrize(
    ("changes", "signing", "status", "code"),
    [pytest.param(*case[1:], id=case[0]) for case in REFUSALS],
)
def test_a_refused_ingest_opens_no_run_and_writes_nothing(
    client: TestClient,
    config: AppConfig,
    paired: PairingRecord,
    changes: dict[str, Any],
    signing: dict[str, Any],
    status: int,
    code: str,
) -> None:
    before = table_counts(config)
    response = post_capture(client, paired, envelope(**changes), **signing)
    assert (response.status_code, _code(response)) == (status, code)
    # Refused whole: no run, no job, nothing buffered, and no database row of any kind.
    after = table_counts(config)
    assert after == before
    assert _runs(client) == []
    assert len(_container(client).extension_capture._buffer) == 0
    # A refusal names a code; it never echoes what was sent.
    assert "xans-product-detail" not in response.text and paired.secret not in response.text


def test_an_unpaired_application_accepts_nothing(client: TestClient, config: AppConfig) -> None:
    record = pair(_container(client))
    _container(client).extension_pairing.revoke()
    before = table_counts(config)
    response = post_capture(client, record)
    assert (response.status_code, _code(response)) == (401, "EXTENSION_NOT_PAIRED")
    assert table_counts(config) == before


def test_a_replayed_request_is_refused_and_opens_no_second_run(
    client: TestClient, paired: PairingRecord
) -> None:
    first = post_capture(client, paired, nonce="n" * 32)
    assert first.status_code == 202
    wait_for_outcome(client, first.json()["collection_run_id"])
    replayed = post_capture(client, paired, nonce="n" * 32)
    assert (replayed.status_code, _code(replayed)) == (401, "EXTENSION_NONCE_REPLAYED")
    assert len(_runs(client)) == 1


def test_an_unauthenticated_oversize_request_is_refused_as_unauthenticated(
    client: TestClient, config: AppConfig, paired: PairingRecord
) -> None:
    # Ceilings come after authentication: what an unauthenticated sender carries is never measured.
    before = table_counts(config)
    huge = b"x" * (4 * MAX_HTML_BYTES)
    forged = signed(
        paired, method="POST", path=CAPTURES, body=huge, overrides={SIGNATURE_HEADER: "0" * 64}
    )
    response = client.post(CAPTURES, content=huge, headers=forged)
    assert (response.status_code, _code(response)) == (401, "EXTENSION_SIGNATURE_INVALID")
    # The same request from the paired extension is refused by the request ceiling, with no run.
    genuine = client.post(
        CAPTURES, content=huge, headers=signed(paired, method="POST", path=CAPTURES, body=huge)
    )
    assert (genuine.status_code, _code(genuine)) == (403, "EXTENSION_CEILING_REQUEST_BYTES")
    assert table_counts(config) == before


def test_a_body_other_than_the_signed_one_is_refused(
    client: TestClient, config: AppConfig, paired: PairingRecord
) -> None:
    before = table_counts(config)
    signed_body = json.dumps(envelope()).encode()
    sent = json.dumps(envelope(frame(body=BODY + "<p>swapped</p>"))).encode()
    response = client.post(
        CAPTURES,
        content=sent,
        headers=signed(paired, method="POST", path=CAPTURES, body=signed_body),
    )
    assert (response.status_code, _code(response)) == (401, "EXTENSION_BODY_DIGEST_MISMATCH")
    assert table_counts(config) == before


def test_one_capture_at_a_time_and_the_minimum_interval(
    client: TestClient, paired: PairingRecord
) -> None:
    # Ruling 5906290729 B-4: the second capture inside the minimum interval is refused whole.
    first = post_capture(client, paired)
    assert first.status_code == 202
    second = post_capture(client, paired)
    assert second.status_code == 429
    assert _code(second) in {"EXTENSION_CEILING_CONCURRENCY", "EXTENSION_CEILING_INTERVAL"}
    wait_for_outcome(client, first.json()["collection_run_id"])
    third = post_capture(client, paired)
    assert (third.status_code, _code(third)) == (429, "EXTENSION_CEILING_INTERVAL")
    assert len(_runs(client)) == 1


# ---------------------------------------------------------------- no CORS widening (B-3)


def test_only_the_two_extension_paths_answer_a_preflight_for_the_paired_origin(
    client: TestClient, paired: PairingRecord
) -> None:
    def preflight(path: str, origin: str, method: str) -> Any:
        return client.options(
            path, headers={"Origin": origin, "Access-Control-Request-Method": method}
        )

    for path, method in ((CAPTURES, "POST"), (POLICY, "GET")):
        allowed = preflight(path, ORIGIN, method)
        assert allowed.status_code == 204
        assert allowed.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert allowed.headers["Access-Control-Allow-Methods"] == method
        # Never a wildcard, never credentials.
        assert "Access-Control-Allow-Credentials" not in allowed.headers
        for origin in ("https://kmretail.co.kr", "chrome-extension://" + "p" * 32, "null"):
            refused = preflight(path, origin, method)
            assert refused.status_code == 403
            assert "Access-Control-Allow-Origin" not in refused.headers
        other = preflight(path, ORIGIN, "DELETE")
        assert other.status_code == 403
    # Every other route is exactly as it was: no preflight answer and no CORS header at all.
    for path in (
        RUNS,
        "/api/v1/collect/revisions/x",
        "/api/v1/screens/collect",
        "/api/v1/system/health",
    ):
        answer = preflight(path, ORIGIN, "POST")
        assert answer.status_code in (404, 405), path
        assert "Access-Control-Allow-Origin" not in answer.headers, path
    listed = client.get(RUNS, headers={"Origin": ORIGIN})
    assert "Access-Control-Allow-Origin" not in listed.headers
    submit = client.post(
        RUNS,
        json={"supplier_key": SUPPLIER, "product_url": PRODUCT_URL + "?x=1"},
        headers={"Origin": ORIGIN, "X-ICBM-Client": "pytest"},
    )
    assert "Access-Control-Allow-Origin" not in submit.headers


def test_an_unpaired_application_answers_no_preflight(client: TestClient) -> None:
    response = client.options(
        CAPTURES, headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"}
    )
    assert response.status_code == 403 and "Access-Control-Allow-Origin" not in response.headers


def test_the_pairing_never_appears_in_a_response(client: TestClient, paired: PairingRecord) -> None:
    for response in (
        client.get(POLICY, headers=signed(paired, method="GET", path=POLICY)),
        post_capture(client, paired),
        post_capture(client, paired, overrides={NONCE_HEADER: "n" * 32}),
        client.get(RUNS),
    ):
        assert paired.secret not in response.text
        assert EXTENSION_ID not in response.text or response.status_code == 204
