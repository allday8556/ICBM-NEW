"""ADR-0036 O5b: retiring a sample from the analysis (§7, owner amendment 6160586049).

Offline throughout. Proven here:

- a sample with a structural problem blocks 「사용 시작」 until the operator retires it with a
  reason; then 3 other confirmed samples start the supplier, through the store's gate and the
  database's (migration 0062's trigger);
- a retired sample neither proves nor blocks, is never verified, is not read again, still counts
  toward the 5, and stays in the report with its reason, flags and values;
- a retirement needs a stated reason, a RECON site and a sample a trial of the supplier read; it
  happens once; the database refuses to update or delete it, or to retire an unread sample;
- the API carries the CSRF client header and the same refusals.
"""

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig, database_path
from app.container import Container
from app.main import create_app
from app.platform.core.errors import AppError
from app.stages.collect.onboarding.site import OnboardingState
from app.stages.collect.onboarding.store import (
    ONBOARDING_ACTIVATION_NOT_PROVEN,
    ONBOARDING_NOT_RECON,
    ONBOARDING_RETIREMENT_REASON_INVALID,
    ONBOARDING_SAMPLE_RETIRED,
    ONBOARDING_SAMPLE_UNKNOWN,
)
from app.stages.collect.onboarding.trials import ONBOARDING_SAMPLE_ALREADY_READ
from tests.conftest import LOCAL
from tests.support.jobs_support import FakeClock
from tests.support.onboarding_support import wait_until
from tests.support.onboarding_trial_support import (
    CAFE24_ORIGIN,
    OPERATOR,
    StubSessions,
    TrialShop,
    cafe24_page,
    cafe24_url,
    recon_site,
    run_trial,
    trial_container,
)

pytestmark = pytest.mark.integration

HEADERS = {"X-ICBM-Client": "icbm-web"}


@pytest.fixture
def shop() -> TrialShop:
    return TrialShop()


@pytest.fixture
def app(config: AppConfig, clock: FakeClock, shop: TrialShop) -> Iterator[Container]:
    with trial_container(config, clock, shop, StubSessions()) as built:
        yield built


def refused(code: str, call: object) -> AppError:
    assert callable(call)
    with pytest.raises(AppError) as caught:
        call()
    assert caught.value.code == code, caught.value.code
    return caught.value


def read(
    app: Container, clock: FakeClock, shop: TrialShop, key: str, number: int, **page: object
) -> str:
    url = cafe24_url(number)
    shop.serve(url, cafe24_page(number, **page))  # type: ignore[arg-type]
    run_trial(app, key, url)
    clock.advance(11)
    return app.onboarding.trials(key)[-1].sample_url_key


def confirm_all(app: Container, key: str) -> None:
    for sample in app.onboarding_analysis.view(key).samples:
        if sample.verdict is None and sample.verifiable:
            app.onboarding_analysis.verify(
                key,
                sample.trial_id,
                "CORRECT",
                shown_fingerprint=sample.facts_fingerprint,
                actor=OPERATOR,
            )


def test_a_structural_sample_blocks_until_retired_then_three_others_start_the_site(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    bad = read(app, clock, shop, key, 801, duplicate_module=True)
    for number in (802, 803, 804):
        read(app, clock, shop, key, number)
    confirm_all(app, key)
    blocked = app.onboarding_analysis.view(key)
    assert blocked.activation["proven_samples"] == 3 and blocked.activation["structural_open"] == 1
    assert not blocked.can_activate
    refused(
        ONBOARDING_ACTIVATION_NOT_PROVEN,
        lambda: app.onboarding_analysis.activate(key, actor=OPERATOR),
    )
    retired = app.onboarding_analysis.retire(
        key, bad, "중복 상품 모듈이 있는 한 상품", actor=OPERATOR
    )
    shown = next(s for s in retired.samples if s.sample_url == bad)
    assert shown.retired_reason == "중복 상품 모듈이 있는 한 상품" and not shown.verifiable
    assert shown.structural and shown.summary is not None  # still shown, with its flags
    assert retired.activation["structural_open"] == 0 and retired.can_activate
    assert app.onboarding_analysis.activate(key, actor=OPERATOR).state == "ACTIVE"
    assert app.onboarding.current(key).state is OnboardingState.ACTIVE


def test_a_retired_sample_never_proves_is_never_verified_or_read_again(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    samples = [read(app, clock, shop, key, n) for n in (811, 812, 813)]
    confirm_all(app, key)
    app.onboarding_analysis.retire(key, samples[0], "대표 상품이 아님", actor=OPERATOR)
    view = app.onboarding_analysis.view(key)
    # Two proven samples remain: a retired sample proves nothing.
    assert view.activation["proven_samples"] == 2 and not view.can_activate
    assert view.samples_taken == 3
    retired = next(s for s in view.samples if s.sample_url == samples[0])
    refused(
        ONBOARDING_SAMPLE_RETIRED,
        lambda: app.onboarding_analysis.verify(
            key,
            retired.trial_id,
            "WRONG",
            shown_fingerprint=retired.facts_fingerprint,
            actor=OPERATOR,
        ),
    )
    clock.advance(600)
    refused(
        ONBOARDING_SAMPLE_ALREADY_READ,
        lambda: app.onboarding_analysis.submit(key, cafe24_url(811), actor=OPERATOR),
    )
    refused(
        ONBOARDING_SAMPLE_RETIRED,
        lambda: app.onboarding_analysis.retire(key, samples[0], "다시", actor=OPERATOR),
    )


@pytest.mark.parametrize("reason", ["", "   ", "가" * 201, "줄\n바꿈"])
def test_a_retirement_states_its_reason(
    app: Container, clock: FakeClock, shop: TrialShop, reason: str
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    sample = read(app, clock, shop, key, 821)
    refused(
        ONBOARDING_RETIREMENT_REASON_INVALID,
        lambda: app.onboarding_analysis.retire(key, sample, reason, actor=OPERATOR),
    )
    assert app.onboarding.retirements(key) == ()


def test_only_a_read_sample_of_a_recon_site_is_retired(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    refused(
        ONBOARDING_SAMPLE_UNKNOWN,
        lambda: app.onboarding.retire_sample(key, cafe24_url(899), "읽지 않음", actor=OPERATOR),
    )
    samples = [read(app, clock, shop, key, n) for n in (831, 832, 833)]
    confirm_all(app, key)
    app.onboarding_analysis.activate(key, actor=OPERATOR)
    refused(
        ONBOARDING_NOT_RECON,
        lambda: app.onboarding.retire_sample(key, samples[0], "사용 중", actor=OPERATOR),
    )


def test_the_database_keeps_a_retirement_and_refuses_an_unread_sample(
    app: Container, config: AppConfig, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    sample = read(app, clock, shop, key, 841)
    app.onboarding.retire_sample(key, sample, "확인용", actor=OPERATOR)
    connection = sqlite3.connect(database_path(config.data_dir))
    try:
        for statement in (
            "UPDATE onboarding_sample_retirements SET reason = 'x'",
            "DELETE FROM onboarding_sample_retirements",
            "INSERT INTO onboarding_sample_retirements VALUES"
            f" ('r-unread', '{key}', 'https://nowhere/', '사유', 'operator',"
            " '2026-10-14 00:00:00')",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(statement)
        assert connection.execute(
            "SELECT COUNT(*) FROM onboarding_sample_retirements"
        ).fetchone() == (1,)
    finally:
        connection.close()


@pytest.fixture
def api(config: AppConfig, shop: TrialShop) -> Iterator[TestClient]:
    application = create_app(
        config, collection_gateway=shop.gateway(), collection_sessions=StubSessions()
    )
    with TestClient(application, base_url=LOCAL) as client:
        yield client


def test_the_api_retires_a_sample_with_the_client_header(api: TestClient, shop: TrialShop) -> None:
    container = api.app.state.container  # type: ignore[attr-defined]
    key = recon_site(container, CAFE24_ORIGIN, "cafe24")
    shop.serve(cafe24_url(851), cafe24_page(851))
    job_id = container.onboarding_analysis.submit(key, cafe24_url(851), actor=OPERATOR).job_id
    wait_until(lambda: container.jobs.get(job_id).state in ("SUCCEEDED", "DEAD"))
    (sample,) = api.get(f"/api/v1/onboarding/suppliers/{key}/trials").json()["samples"]
    url = f"/api/v1/onboarding/suppliers/{key}/samples/retire"
    body = {"sample_url": sample["sample_url"], "reason": "확인용"}
    assert api.post(url, json=body).status_code == 403
    done = api.post(url, json=body, headers=HEADERS)
    assert done.status_code == 200, done.text
    assert done.json()["samples"][0]["retired_reason"] == "확인용"
    again = api.post(url, json=body, headers=HEADERS)
    assert again.status_code == 409 and again.json()["error"]["code"] == ONBOARDING_SAMPLE_RETIRED
    long = api.post(url, json={**body, "reason": "가" * 201}, headers=HEADERS)
    assert long.status_code == 422
