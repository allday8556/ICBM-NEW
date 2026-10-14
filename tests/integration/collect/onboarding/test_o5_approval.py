"""ADR-0036 O5a: the operator verifies what each sample showed, 「사용 시작」 and 「비활성화」 (§7).

Offline throughout: the storefront is the scripted ``TrialShop``, sessions are stubbed and the
secret store lives in memory. Proven here:

- a verdict is recorded only for a current trial of a RECON site and only of the facts that trial
  showed; ``CORRECT`` only for a verifiable sample; a ``WRONG`` stands over any ``CORRECT``;
- 「사용 시작」 is offered (``can_activate``) and accepted only once 3 distinct samples are
  confirmed under the current words with no structural problem; it appends ACTIVE with the
  verifications as evidence, and the supplier is then offered for collection;
- a words change after approval returns the supplier to RECON, and the old verdicts prove nothing
  under the new words;
- 「비활성화」 appends DISABLED: the supplier is no longer offered for collection;
- the API carries the CSRF client header on every write and the same refusals.
"""

import json
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
    ONBOARDING_TRANSITION_REFUSED,
    ONBOARDING_TRIAL_NOT_CURRENT,
    ONBOARDING_TRIAL_UNKNOWN,
    ONBOARDING_VERIFICATION_INVALID,
    ONBOARDING_VERIFICATION_STALE,
)
from app.stages.collect.onboarding.trials import ONBOARDING_TRIAL_NOT_VERIFIABLE
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
OTHER = "0" * 64


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


def read_samples(
    app: Container, clock: FakeClock, shop: TrialShop, key: str, numbers: list[int]
) -> None:
    for number in numbers:
        shop.serve(cafe24_url(number), cafe24_page(number))
        run_trial(app, key, cafe24_url(number))
        clock.advance(11)


def confirm_all(app: Container, key: str) -> None:
    for sample in app.onboarding_analysis.view(key).samples:
        if sample.verdict is not None:
            continue
        app.onboarding_analysis.verify(
            key,
            sample.trial_id,
            "CORRECT",
            shown_fingerprint=sample.facts_fingerprint,
            actor=OPERATOR,
        )


def test_three_confirmed_samples_start_the_supplier_with_the_verdicts_as_evidence(
    app: Container, config: AppConfig, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    read_samples(app, clock, shop, key, [101, 102])
    confirm_all(app, key)
    view = app.onboarding_analysis.view(key)
    assert [s.verdict for s in view.samples] == ["CORRECT", "CORRECT"]
    assert view.activation["proven_samples"] == 2 and not view.can_activate
    refused(
        ONBOARDING_ACTIVATION_NOT_PROVEN,
        lambda: app.onboarding_analysis.activate(key, actor=OPERATOR),
    )
    read_samples(app, clock, shop, key, [103])
    assert not app.onboarding_analysis.view(key).can_activate
    confirm_all(app, key)
    ready = app.onboarding_analysis.view(key)
    assert ready.can_activate and ready.activation["proven_samples"] == 3
    started = app.onboarding_analysis.activate(key, actor=OPERATOR)
    assert started.state == "ACTIVE" and not started.can_activate
    assert app.onboarding.current(key).state is OnboardingState.ACTIVE
    # The ACTIVE row names one verification per proven sample as its evidence.
    connection = sqlite3.connect(database_path(config.data_dir))
    try:
        (evidence,) = connection.execute(
            "SELECT evidence_json FROM onboarding_transitions WHERE supplier_key = ?"
            " AND to_state = 'ACTIVE'",
            (key,),
        ).fetchone()
    finally:
        connection.close()
    verification_ids = {v.verification_id for v in app.onboarding.verifications(key)}
    assert len(verification_ids) == 3 and set(json.loads(evidence)) == verification_ids
    assert len(json.loads(evidence)) == 3
    entry = app.supplier_catalog.onboarded(key)
    assert entry is not None and entry.offered


def test_a_verdict_names_exactly_what_a_current_trial_showed(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    read_samples(app, clock, shop, key, [201])
    (sample,) = app.onboarding_analysis.view(key).samples
    verify = app.onboarding_analysis.verify
    refused(
        ONBOARDING_VERIFICATION_INVALID,
        lambda: verify(
            key,
            sample.trial_id,
            "MAYBE",
            shown_fingerprint=sample.facts_fingerprint,
            actor=OPERATOR,
        ),
    )
    refused(
        ONBOARDING_VERIFICATION_INVALID,
        lambda: verify(key, sample.trial_id, "CORRECT", shown_fingerprint="abc", actor=OPERATOR),
    )
    refused(
        ONBOARDING_VERIFICATION_STALE,
        lambda: verify(key, sample.trial_id, "CORRECT", shown_fingerprint=OTHER, actor=OPERATOR),
    )
    refused(
        ONBOARDING_TRIAL_UNKNOWN,
        lambda: verify(key, "no-such-trial", "CORRECT", shown_fingerprint=OTHER, actor=OPERATOR),
    )
    assert app.onboarding.verifications(key) == ()
    # Under new words the old trial is not current until re-analysis records a new one.
    app.onboarding.save_words(key, {"brand": ["상표"]}, expected_seq=0, actor=OPERATOR)
    refused(
        ONBOARDING_TRIAL_NOT_CURRENT,
        lambda: verify(
            key,
            sample.trial_id,
            "CORRECT",
            shown_fingerprint=sample.facts_fingerprint,
            actor=OPERATOR,
        ),
    )


def test_a_sample_that_is_not_verifiable_is_never_confirmed_and_a_wrong_stands(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    # Two product modules: a structural problem, so the sample is not verifiable.
    shop.serve(cafe24_url(301), cafe24_page(301, duplicate_module=True))
    run_trial(app, key, cafe24_url(301))
    clock.advance(11)
    (bad,) = app.onboarding_analysis.view(key).samples
    assert not bad.verifiable
    refused(
        ONBOARDING_TRIAL_NOT_VERIFIABLE,
        lambda: app.onboarding_analysis.verify(
            key, bad.trial_id, "CORRECT", shown_fingerprint=bad.facts_fingerprint, actor=OPERATOR
        ),
    )
    # A WRONG verdict is taken for any current trial, and stands over a CORRECT one.
    read_samples(app, clock, shop, key, [302])
    good = next(s for s in app.onboarding_analysis.view(key).samples if s.verifiable)
    for verdict in ("CORRECT", "WRONG", "CORRECT"):
        app.onboarding_analysis.verify(
            key, good.trial_id, verdict, shown_fingerprint=good.facts_fingerprint, actor=OPERATOR
        )
    again = next(
        s for s in app.onboarding_analysis.view(key).samples if s.trial_id == good.trial_id
    )
    assert again.verdict == "WRONG"
    assert app.onboarding.activation(key).refuted == 1


def test_a_words_change_after_approval_returns_to_recon_and_old_verdicts_prove_nothing(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    read_samples(app, clock, shop, key, [401, 402, 403])
    confirm_all(app, key)
    app.onboarding_analysis.activate(key, actor=OPERATOR)
    # An ACTIVE supplier takes no verdict.
    sample = app.onboarding_analysis.view(key).samples[0]
    refused(
        ONBOARDING_NOT_RECON,
        lambda: app.onboarding_analysis.verify(
            key,
            sample.trial_id,
            "CORRECT",
            shown_fingerprint=sample.facts_fingerprint,
            actor=OPERATOR,
        ),
    )
    app.onboarding_words.save(key, {"brand": ["상표"]}, expected_seq=0, actor=OPERATOR)
    assert app.onboarding.current(key).state is OnboardingState.RECON
    app.onboarding_analysis.reanalyse(key, actor=OPERATOR)
    view = app.onboarding_analysis.view(key)
    assert view.activation["proven_samples"] == 0 and not view.can_activate
    assert all(s.verdict is None for s in view.samples)
    confirm_all(app, key)
    assert app.onboarding_analysis.view(key).can_activate


def test_disabling_withdraws_the_supplier_from_collection(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    read_samples(app, clock, shop, key, [501, 502, 503])
    confirm_all(app, key)
    app.onboarding_analysis.activate(key, actor=OPERATOR)
    entry = app.supplier_catalog.onboarded(key)
    assert entry is not None and entry.offered
    app.onboarding_service.disable(key, actor=OPERATOR)
    assert app.onboarding.current(key).state is OnboardingState.DISABLED
    entry = app.supplier_catalog.onboarded(key)
    assert entry is None or not entry.offered
    # A disabled supplier is never started again from the analysis screen.
    refused(
        ONBOARDING_TRANSITION_REFUSED,
        lambda: app.onboarding.transition(key, OnboardingState.ACTIVE, actor=OPERATOR),
    )


@pytest.fixture
def api(config: AppConfig, shop: TrialShop) -> Iterator[TestClient]:
    application = create_app(
        config, collection_gateway=shop.gateway(), collection_sessions=StubSessions()
    )
    with TestClient(application, base_url=LOCAL) as client:
        yield client


def test_the_api_verifies_and_starts_with_the_client_header(
    api: TestClient, shop: TrialShop
) -> None:
    """The routes' wiring and headers on one sample; the app's own clock paces real reads, so
    starting with three confirmed samples is proven on the service above."""
    container = api.app.state.container  # type: ignore[attr-defined]
    key = recon_site(container, CAFE24_ORIGIN, "cafe24")
    shop.serve(cafe24_url(601), cafe24_page(601))
    submitted = container.onboarding_analysis.submit(key, cafe24_url(601), actor=OPERATOR)
    wait_until(lambda: container.jobs.get(submitted.job_id).state in ("SUCCEEDED", "DEAD"))
    assert container.jobs.get(submitted.job_id).state == "SUCCEEDED"
    base = f"/api/v1/onboarding/suppliers/{key}"
    (sample,) = api.get(f"{base}/trials").json()["samples"]
    url = f"{base}/trials/{sample['trial_id']}/verification"
    body = {"verdict": "CORRECT", "shown_fingerprint": sample["facts_fingerprint"]}
    # CSRF: every write needs the client header.
    assert api.post(url, json=body).status_code == 403
    assert api.post(f"{base}/activate").status_code == 403
    done = api.post(url, json=body, headers=HEADERS)
    assert done.status_code == 200, done.text
    assert done.json()["samples"][0]["verdict"] == "CORRECT"
    stale = api.post(url, json={**body, "shown_fingerprint": OTHER}, headers=HEADERS)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == ONBOARDING_VERIFICATION_STALE
    extra = api.post(url, json={**body, "trial_id": "x"}, headers=HEADERS)
    assert extra.status_code == 422
    not_yet = api.post(f"{base}/activate", headers=HEADERS)
    assert not_yet.status_code == 409
    assert not_yet.json()["error"]["code"] == ONBOARDING_ACTIVATION_NOT_PROVEN
    assert container.onboarding.current(key).state is OnboardingState.RECON
