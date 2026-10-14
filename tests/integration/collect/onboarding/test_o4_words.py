"""ADR-0036 O4: 「수집 규칙」 — the words of an onboarded site, its revisions and the preview.

Offline throughout: the storefront is the scripted ``TrialShop`` (U-PICK's real Cafe24 product
page moved onto the test host), sessions are stubbed and the secret store lives in memory.
Proven here:

- the view lists every template slot with the template's own words beside the site's words, the
  kind of each slot and the current words revision;
- a preview re-analyses the stored captures under the current and the proposed words: it shows
  the unread rows the new words read and the field statuses they change, and it reads nothing
  from the supplier, records no trial and saves nothing;
- a preview and a save refuse the same words (pattern characters, an unknown slot, too many
  words), and nothing is written by a refusal;
- a save appends the next words revision only from the revision the operator edited, refuses an
  identical one, leaves the samples stale for 「다시 분석」, and re-analysis then reads the new
  words without a supplier read;
- the API carries the CSRF client header on every write and the same refusals.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.platform.core.errors import AppError
from app.stages.collect.onboarding.store import (
    ONBOARDING_WORDS_INVALID,
    ONBOARDING_WORDS_MOVED,
    ONBOARDING_WORDS_SLOT_UNKNOWN,
    ONBOARDING_WORDS_UNCHANGED,
)
from tests.conftest import LOCAL
from tests.support.jobs_support import FakeClock
from tests.support.onboarding_support import wait_until
from tests.support.onboarding_trial_support import (
    CAFE24_ORIGIN,
    OPERATOR,
    StubSessions,
    TrialShop,
    cafe24_url,
    recon_site,
    run_trial,
    trial_container,
    upick_page,
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


def _analysed(app: Container, shop: TrialShop, *, worker: bool = False) -> str:
    """A RECON site with U-PICK 4954 read once. With the application's own job worker running
    (``worker``), the trial job is left to it and awaited, never run beside it."""
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(4954)
    shop.serve(url, upick_page("4954"))
    if not worker:
        run_trial(app, key, url)
        return key
    submitted = app.onboarding_analysis.submit(key, url, actor=OPERATOR)
    wait_until(lambda: app.jobs.get(submitted.job_id).state in ("SUCCEEDED", "DEAD"))
    assert app.jobs.get(submitted.job_id).state == "SUCCEEDED"
    return key


def test_the_view_lists_each_slot_with_the_templates_words_beside_the_sites(
    app: Container,
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    view = app.onboarding_words.view(key)
    assert view.words_seq == 0 and view.state == "RECON" and view.template == "cafe24"
    assert (view.words_per_slot, view.word_limit) == (12, 40)
    slots = {s.slot: s for s in view.slots}
    assert slots["price"].kind == "row" and "판매가" in slots["price"].defaults
    assert slots["channel_all_allowed"].kind == "phrase"
    assert slots["sold_out"].kind == "mark"
    assert all(s.site == [] for s in view.slots)
    app.onboarding.save_words(key, {"purchase_price": ["회원가"]}, expected_seq=0, actor=OPERATOR)
    saved = {s.slot: s for s in app.onboarding_words.view(key).slots}
    assert saved["purchase_price"].site == ["회원가"]


def test_a_preview_shows_what_new_words_read_and_reads_saves_records_nothing(
    app: Container, shop: TrialShop
) -> None:
    key = _analysed(app, shop)
    reads, trials = len(shop.requests), len(app.onboarding.trials(key))
    preview = app.onboarding_words.preview(key, {"purchase_price": ["회원가"]})
    assert preview.words_seq == 0 and preview.missing == 0
    (sample,) = preview.samples
    assert sample.sample_url == app.onboarding.trials(key)[0].sample_url_key
    assert "회원가" in sample.newly_read and "회원가" not in sample.newly_unread
    assert sample.unread_proposed == sample.unread_now - len(sample.newly_read) + len(
        sample.newly_unread
    )
    assert not sample.diverged
    # Nothing was read, recorded or saved.
    assert len(shop.requests) == reads
    assert len(app.onboarding.trials(key)) == trials
    assert app.onboarding.current(key).words_seq == 0
    # The current words previewed against themselves change nothing.
    same = app.onboarding_words.preview(key, {})
    assert same.samples[0].newly_read == [] and same.samples[0].changes == []


@pytest.mark.parametrize(
    ("words", "code"),
    [
        ({"price": ["<b>가격"]}, ONBOARDING_WORDS_INVALID),
        ({"price": ["판매가.*"]}, ONBOARDING_WORDS_INVALID),
        ({"price": [f"단어{i}" for i in range(13)]}, ONBOARDING_WORDS_INVALID),
        ({"price": [" 앞공백"]}, ONBOARDING_WORDS_INVALID),
        ({"no_such_slot": ["단어"]}, ONBOARDING_WORDS_SLOT_UNKNOWN),
    ],
)
def test_a_preview_and_a_save_refuse_the_same_words_and_write_nothing(
    app: Container, shop: TrialShop, words: dict[str, list[str]], code: str
) -> None:
    key = _analysed(app, shop)
    refused(code, lambda: app.onboarding_words.preview(key, words))
    refused(code, lambda: app.onboarding_words.save(key, words, expected_seq=0, actor=OPERATOR))
    assert app.onboarding.current(key).words_seq == 0


def test_a_save_appends_a_revision_from_the_edited_one_and_reanalysis_reads_it(
    app: Container, shop: TrialShop
) -> None:
    key = _analysed(app, shop)
    reads = len(shop.requests)
    saved = app.onboarding_words.save(
        key, {"purchase_price": ["회원가"]}, expected_seq=0, actor=OPERATOR
    )
    assert saved.words_seq == 1
    # From a stale revision, and as an identical revision: refused, nothing written.
    refused(
        ONBOARDING_WORDS_MOVED,
        lambda: app.onboarding_words.save(
            key, {"purchase_price": ["회원가", "도매가"]}, expected_seq=0, actor=OPERATOR
        ),
    )
    refused(
        ONBOARDING_WORDS_UNCHANGED,
        lambda: app.onboarding_words.save(
            key, {"purchase_price": ["회원가"]}, expected_seq=1, actor=OPERATOR
        ),
    )
    assert app.onboarding.current(key).words_seq == 1
    # The sample is stale under the new words until re-analysis reads its capture again.
    stale = app.onboarding_analysis.view(key)
    assert stale.samples == [] and [s.words_seq for s in stale.stale] == [0]
    result = app.onboarding_analysis.reanalyse(key, actor=OPERATOR)
    (sample,) = result.analysis.samples
    assert sample.words_seq == 1
    assert "회원가" not in {u.label for u in sample.unread_rows}
    assert len(shop.requests) == reads
    # Back to the template's own words: an empty revision is a revision too.
    assert app.onboarding_words.save(key, {}, expected_seq=1, actor=OPERATOR).words_seq == 2


@pytest.fixture
def api(config: AppConfig, shop: TrialShop) -> Iterator[TestClient]:
    application = create_app(
        config, collection_gateway=shop.gateway(), collection_sessions=StubSessions()
    )
    with TestClient(application, base_url=LOCAL) as client:
        yield client


def test_the_api_reads_previews_and_saves_words_with_the_client_header(
    api: TestClient, shop: TrialShop
) -> None:
    container = api.app.state.container  # type: ignore[attr-defined]
    key = _analysed(container, shop, worker=True)
    base = f"/api/v1/onboarding/suppliers/{key}/words"
    view = api.get(base)
    assert view.status_code == 200 and view.json()["words_seq"] == 0
    body = {"words": {"purchase_price": ["회원가"]}}
    # CSRF: every write needs the client header.
    assert api.post(f"{base}/preview", json=body).status_code == 403
    assert api.put(base, json={**body, "expected_seq": 0}).status_code == 403
    preview = api.post(f"{base}/preview", json=body, headers=HEADERS)
    assert preview.status_code == 200, preview.text
    assert "회원가" in preview.json()["samples"][0]["newly_read"]
    bad = api.post(f"{base}/preview", json={"words": {"price": ["<b>"]}}, headers=HEADERS)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == ONBOARDING_WORDS_INVALID
    assert "<b>" not in bad.text
    saved = api.put(base, json={**body, "expected_seq": 0}, headers=HEADERS)
    assert saved.status_code == 200 and saved.json()["words_seq"] == 1
    moved = api.put(base, json={**body, "expected_seq": 0}, headers=HEADERS)
    assert moved.status_code == 409 and moved.json()["error"]["code"] == ONBOARDING_WORDS_MOVED
    # Unknown request fields are refused outright.
    extra = api.put(base, json={**body, "expected_seq": 1, "hosts": ["x"]}, headers=HEADERS)
    assert extra.status_code == 422
