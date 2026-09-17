"""The ``m3-accept-01`` campaign harness, offline (Issue #52 rulings 5711123764, 5711187191).

Every pass here runs through the production ``ProductCollectionService``, the production job
runner and the production collection gateway contract, with the M1 connection owner handing out
the session. Only the far end is synthetic: the shop, its CONNECT transport and its bytes. No test
here can reach a provider, and none of them starts a REAL campaign.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest

from app.collect.collection import COLLECT_POLICY, RegisteredCollection, RunBudget
from app.config import AppConfig
from app.container import build_container
from app.core.errors import TransientError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from integrations.suppliers.base import Credentials, RequestKind
from integrations.suppliers.collection import DocumentView, ReadKind, SupplierCollection
from integrations.suppliers.transport.collection import PolicedCollectionGateway
from scripts.m3accept.campaign import Environment, closeout, run_pass
from scripts.m3accept.gateways import (
    LedgeredCollectionGateway,
    LedgeredConnectGateway,
    UnreservedRequest,
)
from scripts.m3accept.ledger import (
    CampaignBudgetRefused,
    CampaignLedger,
    LedgerError,
    State,
    Submission,
)
from scripts.m3accept.manifest import (
    M3_ACCEPT_01_BUDGET,
    MIB,
    CampaignBudget,
    Ceiling,
    RequestClass,
    approval_phrase,
    byte_facts,
)
from scripts.m3accept.prep import (
    ArmingRefused,
    arm,
    canonical_target,
    hard_zero_problems,
    m1_session_problems,
    profile_problems,
    real_environment,
    typed_approval_matches,
)
from scripts.m3collect import fake_shop
from tests.support import FakeClock

pytestmark = pytest.mark.integration

CODE_SHA = "a" * 40
PHASE_B = {"images": [{"bytes": 59019}, {"bytes": 555361}, {"bytes": 11147}]}


def budget_with(**connect: Ceiling) -> CampaignBudget:
    """The frozen campaign budget, with only the CONNECT proof ceilings changed for a test."""
    ceilings = dict(M3_ACCEPT_01_BUDGET.ceilings)
    ceilings.update({RequestClass(name): value for name, value in connect.items()})
    return CampaignBudget(
        ceilings=MappingProxyType(ceilings),
        per_image_bytes=M3_ACCEPT_01_BUDGET.per_image_bytes,
        new_image_bytes_per_pass=M3_ACCEPT_01_BUDGET.new_image_bytes_per_pass,
        same_product_interval_s=M3_ACCEPT_01_BUDGET.same_product_interval_s,
        product_evidence_baseline=M3_ACCEPT_01_BUDGET.product_evidence_baseline,
    )


# The shape a session-proof budget would need if it were approved: one proof per product-read
# attempt, so a job retry inside a pass is not stopped by its own session check. NOT the frozen
# budget — that one allows no proof at all.
PROOF_BUDGET = budget_with(
    CONNECT_CONTROL_READ=Ceiling(per_pass=2, campaign=4),
    CONNECT_PROTECTED_READ=Ceiling(per_pass=2, campaign=4),
)


def shop_collection() -> SupplierCollection:
    """The synthetic shop, bounded exactly as the campaign's production profile is."""
    return fake_shop.collection(
        max_image_requests=30, max_image_bytes=2 * MIB, max_run_bytes=24 * MIB
    )


@dataclass
class World:
    ledger: CampaignLedger
    env: Environment
    gateway: fake_shop.FakeGateway
    connect: fake_shop.FakeConnect
    clock: FakeClock
    url: str = fake_shop.PRODUCT_URL


def make_world(
    config: AppConfig,
    tmp_path: Path,
    *,
    budget: CampaignBudget = PROOF_BUDGET,
    product_evidence: int = 13,
    images: dict[str, object] | None = None,
    establish_m1: bool = True,
    mode: str = "DRY",
) -> World:
    clock = FakeClock()
    secrets = MemorySecretStore()
    collection = shop_collection()
    gateway = fake_shop.FakeGateway(
        documents=[fake_shop.page(product_evidence=product_evidence)], images=images or {}
    )
    connect = fake_shop.FakeConnect()
    registered = RegisteredCollection(
        collection=collection,
        extractor_revision=fake_shop.EXTRACTOR_REVISION,
        extractor_fingerprint=fake_shop.EXTRACTOR_FINGERPRINT,
    )
    env = Environment(
        config=config,
        supplier_key=fake_shop.SUPPLIER_KEY,
        collection=collection,
        collection_transport=lambda: gateway,
        connect_transport=lambda: connect,
        secret_store=secrets,
        clock=clock,
        registered=(registered,),
        suppliers=(fake_shop.connect_definition(),),
    )
    if establish_m1:
        # The accepted M1 connection, established before the campaign exists — the only login.
        with acquire_data_dir(config.data_dir, app_version="m1") as lease:
            built = build_container(
                config,
                ownership=lease,
                clock=clock,
                secret_store=secrets,
                supplier_gateway=connect,
                suppliers=(fake_shop.connect_definition(),),
                collection_gateway=gateway,
                collections=(registered,),
            )
            try:
                built.connect.save_credentials(
                    fake_shop.SUPPLIER_KEY,
                    username=fake_shop.MEMBER_ID,
                    password=fake_shop.MEMBER_PASSWORD,
                    actor="operator",
                )
                built.connect.verify(
                    fake_shop.SUPPLIER_KEY, trigger="operator_test", allow_login=True
                )
                assert not m1_session_problems(built, fake_shop.SUPPLIER_KEY)
            finally:
                built.db.dispose()
    ledger = CampaignLedger.create(tmp_path / "campaign" / "campaign.sqlite3")
    arm(
        ledger,
        collection=collection,
        product_url=fake_shop.PRODUCT_URL,
        phase_b_findings=PHASE_B,
        mode=mode,
        code_sha=CODE_SHA,
        budget=budget,
    )
    return World(ledger=ledger, env=env, gateway=gateway, connect=connect, clock=clock)


# ---------------------------------------------------------------- the armed contract


def test_the_armed_manifest_and_its_ceilings_can_never_change(
    config: AppConfig, tmp_path: Path
) -> None:
    world = make_world(config, tmp_path, establish_m1=False)
    manifest = world.ledger.manifest()
    assert manifest is not None
    assert manifest["campaign_id"] == "m3-accept-01" and manifest["code_sha"] == CODE_SHA
    assert manifest["budget"]["ceilings"]["IMAGE_REQUEST"] == {"per_pass": 13, "campaign": 26}

    with closing(sqlite3.connect(world.ledger.path)) as db:
        for statement in (
            "UPDATE manifest SET code_sha = 'b'",
            "DELETE FROM manifest",
            "UPDATE ceilings SET per_pass = 99 WHERE class = 'IMAGE_REQUEST'",
            "DELETE FROM ceilings",
            "INSERT INTO ceilings VALUES ('SOMETHING_NEW', 9, 9)",
            "DELETE FROM events",
            "UPDATE events SET state = 'COMPLETED'",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                db.execute(statement)
    with pytest.raises(LedgerError):
        arm(
            world.ledger,
            collection=world.env.collection,
            product_url=fake_shop.PRODUCT_URL,
            phase_b_findings=PHASE_B,
            mode="DRY",
            code_sha="c" * 40,
            budget=PROOF_BUDGET,
        )
    assert world.ledger.manifest() == manifest


def test_arming_refuses_a_budget_that_is_not_the_production_profile(
    config: AppConfig, tmp_path: Path
) -> None:
    from integrations.suppliers.kmretail.collection import COLLECTION as KM

    # The frozen budget and KM's production profile agree; the campaign changes neither.
    assert profile_problems(KM, M3_ACCEPT_01_BUDGET) == []
    narrower = fake_shop.collection(max_image_requests=30)  # 1 MiB images, 4 MiB per run
    assert len(profile_problems(narrower, M3_ACCEPT_01_BUDGET)) == 2

    ledger = CampaignLedger.create(tmp_path / "refused.sqlite3")
    with pytest.raises(ArmingRefused):
        arm(
            ledger,
            collection=narrower,
            product_url=fake_shop.PRODUCT_URL,
            phase_b_findings=PHASE_B,
            mode="DRY",
            code_sha=CODE_SHA,
        )
    assert ledger.state() is State.INITIALIZED

    # A target is bound only through the production rule, and only as a digest.
    km_url = "https://kmretail.co.kr/product/%EC%83%81%ED%92%88/355/category/23/display/1/"
    assert canonical_target(KM, km_url).startswith("https://kmretail.co.kr/product/")
    with pytest.raises(ArmingRefused):
        canonical_target(KM, f"{km_url}?utm_source=mail")


def test_byte_facts_claim_only_what_reconnaissance_proved() -> None:
    facts = byte_facts(PHASE_B, baseline=13)
    assert facts.sampled == 3 and facts.unknown_references == 10
    assert facts.known_total == 59019 + 555361 + 11147
    assert facts.sufficiency_claimed is False, "24 MiB is not claimed to suffice for all 13"


def test_the_approval_is_the_operator_s_exact_words_for_the_exact_sha(
    config: AppConfig, tmp_path: Path
) -> None:
    phrase = approval_phrase(CODE_SHA)
    assert phrase == f"APPROVE m3-accept-01 {CODE_SHA[:12]} TWO-PASS-REAL"
    assert typed_approval_matches(phrase, CODE_SHA)
    for typed in ("approve m3-accept-01", phrase.replace("REAL", "DRY"), "yes"):
        assert not typed_approval_matches(typed, CODE_SHA)

    real = make_world(config, tmp_path, establish_m1=False, mode="REAL")
    with pytest.raises(LedgerError):
        real.ledger.begin_pass("A", session_nonce="n")  # not approved
    with pytest.raises(LedgerError):
        real.ledger.approve("b" * 40)  # a different SHA
    real.ledger.approve(CODE_SHA)
    assert real.ledger.state() is State.APPROVED

    dry = CampaignLedger.create(tmp_path / "dry.sqlite3")
    arm(
        dry,
        collection=shop_collection(),
        product_url=fake_shop.PRODUCT_URL,
        phase_b_findings=PHASE_B,
        mode="DRY",
        code_sha=CODE_SHA,
    )
    with pytest.raises(LedgerError):
        dry.approve(CODE_SHA)


# ---------------------------------------------------------------- pre-send reservations


def test_nothing_is_reserved_outside_a_running_pass(config: AppConfig, tmp_path: Path) -> None:
    world = make_world(config, tmp_path, establish_m1=False)
    with pytest.raises(CampaignBudgetRefused):
        world.ledger.reserve("A", RequestClass.PRODUCT_READ, world.url)
    assert world.ledger.counts()["PRODUCT_READ"] == 0
    assert world.ledger.refusals()[-1]["reason"] == "PASS_NOT_RUNNING"


def test_a_transport_that_sends_without_reserving_stops_the_campaign(
    config: AppConfig, tmp_path: Path
) -> None:
    world = make_world(config, tmp_path, establish_m1=False)
    world.ledger.begin_pass("A", session_nonce="n1")

    class Rogue:
        def read_document(self, *args: object, **kwargs: object) -> DocumentView:
            return fake_shop.document(fake_shop.page())  # never reserved

    ledgered = LedgeredCollectionGateway(Rogue(), world.ledger, "A")  # type: ignore[arg-type]
    with pytest.raises(UnreservedRequest):
        ledgered.read_document(
            world.env.collection.profile,
            world.url,
            kind=ReadKind.PRODUCT_READ,
            budget=RunBudget(max_product_reads=1, max_image_requests=1),
        )


def test_the_production_gateway_sends_nothing_the_campaign_refused(
    config: AppConfig, tmp_path: Path
) -> None:
    # The real policed transport, with a mock network: a refused reservation sends no byte, and an
    # allowed one is already durable in the ledger at the moment the request goes out.
    world = make_world(config, tmp_path, establish_m1=False)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(world.ledger.counts("A")["PRODUCT_READ"])
        return httpx.Response(200, headers={"content-type": "text/html"}, text=fake_shop.page())

    transport = PolicedCollectionGateway(http_transport=httpx.MockTransport(handler))
    ledgered = LedgeredCollectionGateway(transport, world.ledger, "A")

    def read() -> DocumentView:
        return ledgered.read_document(
            world.env.collection.profile,
            world.url,
            kind=ReadKind.PRODUCT_READ,
            budget=RunBudget(max_product_reads=5, max_image_requests=1),
        )

    with pytest.raises(CampaignBudgetRefused):
        read()  # the pass is not running
    assert seen == [], "a refused reservation sent nothing"

    world.ledger.begin_pass("A", session_nonce="n1")
    read()
    assert seen == [1], "the reservation was committed before the request left"
    read()
    with pytest.raises(CampaignBudgetRefused):
        read()  # a third product read in one pass
    assert seen == [1, 2], "the refused third read never reached the network"


def test_ceilings_hold_per_pass_and_across_the_campaign(config: AppConfig, tmp_path: Path) -> None:
    world = make_world(config, tmp_path, establish_m1=False)
    ledger = world.ledger

    def spend(pass_id: str, request: RequestClass, times: int) -> None:
        for n in range(times):
            ledger.reserve(pass_id, request, f"{request.value}-{pass_id}-{n}")

    ledger.begin_pass("A", session_nonce="a")
    spend("A", RequestClass.PRODUCT_READ, 2)
    with pytest.raises(CampaignBudgetRefused):
        ledger.reserve("A", RequestClass.PRODUCT_READ, "third")
    spend("A", RequestClass.IMAGE_REQUEST, 13)
    with pytest.raises(CampaignBudgetRefused):
        ledger.reserve("A", RequestClass.IMAGE_REQUEST, "fourteenth")
    ledger.record_submission(Submission("A", "a", "run-a", "job-a", "cid-a"))
    ledger.finish_pass("A", verdict="ACCEPTED", detail={})

    ledger.begin_pass("B", session_nonce="b")
    spend("B", RequestClass.PRODUCT_READ, 2)
    with pytest.raises(CampaignBudgetRefused):
        ledger.reserve("B", RequestClass.PRODUCT_READ, "fifth")  # product #5 of the campaign
    spend("B", RequestClass.IMAGE_REQUEST, 13)
    with pytest.raises(CampaignBudgetRefused):
        ledger.reserve("B", RequestClass.IMAGE_REQUEST, "twenty-seventh")

    assert ledger.counts() | {} == {
        "PRODUCT_READ": 4,
        "IMAGE_REQUEST": 26,
        "POLICY_READ": 0,
        "CONNECT_CONTROL_READ": 0,
        "CONNECT_PROTECTED_READ": 0,
        "CONNECT_AUTHENTICATE": 0,
    }
    reasons = [r["reason"] for r in ledger.refusals()]
    assert reasons == ["PASS_CEILING"] * 4


def test_classes_this_campaign_does_not_budget_are_refused(
    config: AppConfig, tmp_path: Path
) -> None:
    world = make_world(config, tmp_path, budget=M3_ACCEPT_01_BUDGET, establish_m1=False)
    world.ledger.begin_pass("A", session_nonce="a")
    for request in (
        RequestClass.POLICY_READ,
        RequestClass.CONNECT_CONTROL_READ,
        RequestClass.CONNECT_PROTECTED_READ,
        RequestClass.CONNECT_AUTHENTICATE,
    ):
        with pytest.raises(CampaignBudgetRefused):
            world.ledger.reserve("A", request, "anything")

    connect = LedgeredConnectGateway(fake_shop.FakeConnect(), world.ledger, "A")
    with pytest.raises(Exception, match="never logs in"):
        connect.login(fake_shop.connect_definition(), Credentials("x", "y"))
    with pytest.raises(CampaignBudgetRefused):
        connect.fetch(fake_shop.connect_definition(), kind=RequestKind.PROTECTED_READ, session=b"x")
    assert sum(world.ledger.counts().values()) == 0
    # AI, OCR and marketplace code is not reachable from anything a campaign runs.
    assert hard_zero_problems() == []


def test_a_restart_preserves_the_ledger_and_what_remains_of_the_budget(
    config: AppConfig, tmp_path: Path
) -> None:
    world = make_world(config, tmp_path, establish_m1=False)
    world.ledger.begin_pass("A", session_nonce="a")
    for n in range(12):
        world.ledger.reserve("A", RequestClass.IMAGE_REQUEST, f"image-{n}")

    reopened = CampaignLedger(world.ledger.path)  # a new process, the same file
    assert reopened.state() is State.PASS_A_RUNNING
    assert reopened.counts("A")["IMAGE_REQUEST"] == 12
    reopened.reserve("A", RequestClass.IMAGE_REQUEST, "image-12")
    with pytest.raises(CampaignBudgetRefused):
        reopened.reserve("A", RequestClass.IMAGE_REQUEST, "image-13")


# ---------------------------------------------------------------- the M1 connection owner


def test_the_frozen_budget_stops_a_pass_before_the_m1_owner_is_asked(
    config: AppConfig, tmp_path: Path
) -> None:
    # The M1 owner proves a stored session with two requests before handing it out, and the frozen
    # budget allows none. The pass stops before the owner runs, so its state is left untouched.
    world = make_world(config, tmp_path, budget=M3_ACCEPT_01_BUDGET)
    fetches_before, logins_before = list(world.connect.fetches), world.connect.logins

    outcome = run_pass(world.ledger, world.env, "A", world.url)

    assert outcome.status == "STOPPED"
    assert outcome.detail["run_detail"] == "M3_ACCEPT_SESSION_PROOF_NOT_BUDGETED"
    assert world.connect.fetches == fetches_before, "no session proof was sent"
    assert world.connect.logins == logins_before, "no login was attempted"
    assert world.gateway.document_reads == 0, "no product read was sent"
    assert sum(world.ledger.counts().values()) == 0
    assert world.ledger.state() is State.STOPPED
    with acquire_data_dir(config.data_dir, app_version="check") as lease:
        built = build_container(
            config,
            ownership=lease,
            secret_store=world.env.secret_store,
            clock=world.clock,
            supplier_gateway=world.connect,
            suppliers=world.env.suppliers or (),
            collection_gateway=world.gateway,
            collections=world.env.registered,
        )
        try:
            assert m1_session_problems(built, fake_shop.SUPPLIER_KEY) == []
        finally:
            built.db.dispose()


def test_the_m1_owner_hands_out_its_session_and_the_campaign_never_logs_in(
    config: AppConfig, tmp_path: Path
) -> None:
    world = make_world(config, tmp_path)
    logins_before = world.connect.logins
    assert logins_before == 1, "the accepted M1 login happened before the campaign"

    outcome = run_pass(world.ledger, world.env, "A", world.url)

    assert outcome.status == "ACCEPTED", outcome.detail["reasons"]
    assert world.connect.logins == logins_before, "the campaign never logs in"
    counts = world.ledger.counts("A")
    assert counts["CONNECT_CONTROL_READ"] == 1 and counts["CONNECT_PROTECTED_READ"] == 1
    assert counts["CONNECT_AUTHENTICATE"] == 0
    assert world.gateway.sessions == [world.gateway.sessions[0]]
    assert world.gateway.sessions[0] is not None, "the product read carried the M1 session"


# ---------------------------------------------------------------- two fresh-session passes


def test_two_fresh_session_passes_record_two_revisions_of_one_product(
    config: AppConfig, tmp_path: Path
) -> None:
    world = make_world(config, tmp_path)

    first = run_pass(world.ledger, world.env, "A", world.url)
    assert first.status == "ACCEPTED", first.detail["reasons"]
    assert world.ledger.state() is State.WAITING_FOR_PACING

    # Too soon: an explicit wait, not a loop. Nothing is reserved, submitted or sent.
    reads, counts = world.gateway.document_reads, world.ledger.counts()
    waiting = run_pass(world.ledger, world.env, "B", world.url)
    assert waiting.status == "WAITING_FOR_PACING" and waiting.seconds_remaining > 0
    assert world.ledger.counts() == counts, "waiting spent no reservation"
    assert world.gateway.document_reads == reads, "waiting sent nothing"
    assert world.ledger.submission("B") is None
    assert world.ledger.state() is State.WAITING_FOR_PACING

    world.clock.advance(M3_ACCEPT_01_BUDGET.same_product_interval_s + 1)
    second = run_pass(world.ledger, world.env, "B", world.url)
    assert second.status == "ACCEPTED", second.detail["reasons"]
    assert world.ledger.state() is State.CLOSEOUT_READY

    a, b = world.ledger.submission("A"), world.ledger.submission("B")
    assert a is not None and b is not None
    assert a.session_nonce != b.session_nonce, "each pass ran in its own fresh session"

    report = closeout(world.ledger, world.env)
    assert report["problems"] == []
    assert report["revisions_distinct"] and report["same_source_identity"]
    assert report["history_revisions"] == 2
    assert report["requests"]["PRODUCT_READ"] == 2
    assert report["requests"]["IMAGE_REQUEST"] == 26
    assert report["requests"]["CONNECT_AUTHENTICATE"] == 0
    assert report["hard_zero"] == {"AI": 0, "OCR": 0, "MARKETPLACE": 0, "SUPPLIER_WRITE": 0}
    assert len(report["capability_boundary"]) == 2
    assert fake_shop.PRODUCT_URL not in str(report), "the closeout carries no raw target URL"

    # Everything above is read back from durable state by a process that did none of it.
    reopened = CampaignLedger(world.ledger.path)
    assert reopened.state() is State.COMPLETED
    assert reopened.counts() == report["requests"]
    assert reopened.result("A")["detail"]["collection_run_id"] == a.collection_run_id
    assert reopened.result("B")["detail"]["collection_run_id"] == b.collection_run_id
    revisions = {reopened.result(p)["detail"]["revision_id"] for p in ("A", "B")}
    assert len(revisions) == 2 and None not in revisions


def test_pass_b_never_runs_in_the_session_pass_a_used(config: AppConfig, tmp_path: Path) -> None:
    world = make_world(config, tmp_path, establish_m1=False)
    world.ledger.begin_pass("A", session_nonce="same")
    world.ledger.record_submission(Submission("A", "same", "run", "job", "cid"))
    world.ledger.finish_pass("A", verdict="ACCEPTED", detail={})
    with pytest.raises(LedgerError, match="fresh session"):
        world.ledger.begin_pass("B", session_nonce="same")
    world.ledger.begin_pass("B", session_nonce="another")


def test_a_job_retry_is_owned_by_the_job_and_spends_the_same_budget(
    config: AppConfig, tmp_path: Path
) -> None:
    world = make_world(config, tmp_path)

    class FlakyOnce(fake_shop.FakeGateway):
        failed: bool = False

        def read_document(self, profile, url, *, kind, budget, session=None):  # type: ignore[no-untyped-def]
            if not self.failed:
                budget.reserve(kind, url)  # the request is reserved and sent, then fails
                self.failed = True
                self.document_reads += 1
                raise TransientError("PROVIDER_BUSY", "try again")
            return super().read_document(profile, url, kind=kind, budget=budget, session=session)

    flaky = FlakyOnce(documents=[fake_shop.page(product_evidence=13)])
    world.env.collection_transport = lambda: flaky

    started = run_pass(world.ledger, world.env, "A", world.url)
    assert started.status == "IN_PROGRESS", "the job owner scheduled the retry, not the harness"
    assert world.ledger.counts("A")["PRODUCT_READ"] == 1

    world.clock.advance(COLLECT_POLICY.base_delay_s)
    finished = run_pass(world.ledger, world.env, "A", world.url)
    assert finished.status == "ACCEPTED", finished.detail["reasons"]
    assert world.ledger.counts("A")["PRODUCT_READ"] == 2, "the retry spent the same budget"
    assert flaky.document_reads == 2


# ---------------------------------------------------------------- reference drift and byte caps


@pytest.mark.parametrize("references", [12, 13, 14])
def test_product_evidence_drift_around_the_frozen_baseline(
    config: AppConfig, tmp_path: Path, references: int
) -> None:
    world = make_world(config, tmp_path, product_evidence=references)

    outcome = run_pass(world.ledger, world.env, "A", world.url)
    image_requests = world.ledger.counts("A")["IMAGE_REQUEST"]

    if references == 12:
        # Fewer than recon saw: recorded as drift, nothing invented, nothing extra requested.
        assert outcome.status == "ACCEPTED", outcome.detail["reasons"]
        assert outcome.detail["observations"] == ["SOURCE_DRIFT_UNDER_BASELINE:12<13"]
        assert image_requests == 12 and len(world.gateway.image_reads) == 12
    elif references == 13:
        assert outcome.status == "ACCEPTED", outcome.detail["reasons"]
        assert outcome.detail["observations"] == []
        assert image_requests == 13
    else:
        # More than the frozen baseline: reference 14 is never fetched and the cap is not raised.
        assert outcome.status == "HOLD"
        assert any(r.startswith("DRIFT_OVER_BASELINE") for r in outcome.detail["reasons"])
        assert image_requests == 13 and len(world.gateway.image_reads) == 13
        assert world.ledger.ceiling(RequestClass.IMAGE_REQUEST) == (13, 26)
        assert world.ledger.refusals()[-1]["reason"] == "PASS_CEILING"
        assert outcome.detail["references"]["issues"] == {"BUDGET_EXHAUSTED": 1}
        assert world.ledger.state() is State.HOLD
    assert outcome.detail["references"]["count"] == references


def test_thirteen_legal_images_that_cross_24_mib_hold_the_pass(
    config: AppConfig, tmp_path: Path
) -> None:
    # Every image fits the 2 MiB bound on its own; together they do not fit 24 MiB. The production
    # path refuses the body that would cross the total, and the campaign calls the pass HOLD.
    big = fake_shop.png(64, 64) + b"\x00" * (19 * MIB // 10)
    assert len(big) <= 2 * MIB
    urls = [fake_shop.PRIMARY_URL] + [fake_shop.detail_url(i) for i in range(12)]
    world = make_world(config, tmp_path, images=dict.fromkeys(urls, big))

    outcome = run_pass(world.ledger, world.env, "A", world.url)

    assert outcome.status == "HOLD"
    assert "BYTE_CAP_REACHED:1" in outcome.detail["reasons"]
    references = outcome.detail["references"]
    assert references["count"] == 13
    assert references["issues"] == {"BUDGET_EXHAUSTED": 1}
    assert references["stored_bytes"] <= 24 * MIB, "no byte past the pass total was stored"
    assert outcome.detail["revision_id"] is not None, "the safe incomplete revision still exists"
    assert world.ledger.ceiling(RequestClass.IMAGE_REQUEST) == (13, 26), "nothing was widened"


# ---------------------------------------------------------------- CI


def test_a_real_campaign_environment_cannot_exist_under_a_test_run(config: AppConfig) -> None:
    from integrations.suppliers.kmretail.collection import COLLECTION as KM

    registered = RegisteredCollection(
        collection=KM, extractor_revision=KM.roles.identity, extractor_fingerprint="d" * 64
    )
    with pytest.raises(RuntimeError, match="never exists under"):
        real_environment(config, collection=KM, registered=registered)


@pytest.fixture(autouse=True)
def _no_live_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Belt and braces: nothing in this module may build the live CONNECT transport either."""
    import integrations.suppliers.transport.gateway as gateway

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test built the live CONNECT transport")

    monkeypatch.setattr(gateway.PolicedSupplierGateway, "__init__", refuse)
    yield


def test_the_campaign_commands_that_can_send_refuse_a_test_run(tmp_path: Path) -> None:
    from scripts import m3_accept

    for argv in (
        ["approve", "--root", str(tmp_path)],
        [
            "run-pass",
            "--root",
            str(tmp_path),
            "--pass",
            "A",
            "--product-url",
            fake_shop.PRODUCT_URL,
        ],
        ["closeout", "--root", str(tmp_path)],
    ):
        with pytest.raises(SystemExit, match="never runs under"):
            m3_accept.main(argv)
