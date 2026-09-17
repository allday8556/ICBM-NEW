"""The ``m3-accept-01`` campaign harness, offline (Issue #52 rulings 5711123764, 5711187191; PR #71
review 5233744115 and comment 5712246461).

Every pass here runs through the production ``ProductCollectionService``, the production job
runner and the production collection gateway contract, with the production M1 connection owner
handing out the session and proving it through the ledgered CONNECT gateway. Only the far end is
synthetic: the shop, its CONNECT transport and its bytes. No test here can reach a provider, and
none of them starts a REAL campaign.
"""

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from app.collect.collection import COLLECT_POLICY, RegisteredCollection, RunBudget
from app.config import AppConfig, database_path
from app.connect.sessions import SESSIONS_DIR_NAME
from app.container import build_container
from app.core.errors import TransientError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from integrations.suppliers.base import Credentials, ProbeResponse, RequestKind, SupplierDefinition
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
from scripts.m3accept.m1 import m1_session_problems
from scripts.m3accept.manifest import (
    M3_ACCEPT_01_BUDGET,
    MIB,
    RequestClass,
    approval_phrase,
    byte_facts,
)
from scripts.m3accept.prep import (
    ArmingRefused,
    arm,
    canonical_target,
    hard_zero_problems,
    profile_problems,
    real_environment,
    typed_approval_matches,
)
from scripts.m3collect import fake_shop
from tests.conftest import make_config
from tests.support import FakeClock

pytestmark = pytest.mark.integration

CODE_SHA = "a" * 40
PHASE_B = {"images": [{"bytes": 59019}, {"bytes": 555361}, {"bytes": 11147}]}
CONTROL, PROTECTED = RequestClass.CONNECT_CONTROL_READ, RequestClass.CONNECT_PROTECTED_READ


def shop_collection() -> SupplierCollection:
    """The synthetic shop, bounded exactly as the campaign's production profile is."""
    return fake_shop.collection(
        max_image_requests=30, max_image_bytes=2 * MIB, max_run_bytes=24 * MIB
    )


@dataclass
class WitnessConnect(fake_shop.FakeConnect):
    """The shop's CONNECT transport, which also witnesses the ledger at the moment of each send.

    ``expired`` makes every protected read come back signed out, as a session that has lapsed.
    """

    ledger: CampaignLedger | None = None
    pass_id: str = "A"
    witnessed: list[tuple[str, int]] = field(default_factory=list)
    expired: bool = False

    def fetch(
        self, definition: SupplierDefinition, *, kind: RequestKind, session: bytes | None
    ) -> ProbeResponse:
        if self.ledger is not None:
            request = {"CONTROL_READ": CONTROL, "PROTECTED_READ": PROTECTED}[kind.value]
            self.witnessed.append((kind.value, self.ledger.counts(self.pass_id)[request.value]))
        if self.expired and kind is RequestKind.PROTECTED_READ:
            self.fetches.append(kind.value)
            return ProbeResponse(
                status=200, path=definition.probe.target, location=None, body="state-logoff"
            )
        return super().fetch(definition, kind=kind, session=session)


@dataclass
class World:
    ledger: CampaignLedger
    env: Environment
    gateway: fake_shop.FakeGateway
    connect: WitnessConnect
    clock: FakeClock
    url: str = fake_shop.PRODUCT_URL


def campaign_data(template: Path, base: Path) -> AppConfig:
    """A dedicated campaign root, whose own ``data`` directory holds a migrated ICBM database."""
    data = base / "campaign" / "data"
    database = database_path(data)
    database.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, database)
    return make_config(data)


def environment(
    config: AppConfig,
    *,
    gateway: fake_shop.FakeGateway,
    connect: WitnessConnect,
    secrets: MemorySecretStore,
    clock: FakeClock,
    collection: SupplierCollection | None = None,
) -> Environment:
    collection = collection or shop_collection()
    return Environment(
        config=config,
        supplier_key=fake_shop.SUPPLIER_KEY,
        collection=collection,
        collection_transport=lambda: gateway,
        connect_transport=lambda: connect,
        secret_store=secrets,
        clock=clock,
        registered=(
            RegisteredCollection(
                collection=collection,
                extractor_revision=fake_shop.EXTRACTOR_REVISION,
                extractor_fingerprint=fake_shop.EXTRACTOR_FINGERPRINT,
            ),
        ),
        suppliers=(fake_shop.connect_definition(),),
    )


def establish_m1(env: Environment, connect: WitnessConnect, *, auto_connect: bool = False) -> None:
    """The ordinary CONNECT operation an operator runs before arming — never the campaign."""
    with acquire_data_dir(env.config.data_dir, app_version="m1") as lease:
        built = build_container(
            env.config,
            ownership=lease,
            clock=env.clock,
            secret_store=env.secret_store,
            supplier_gateway=connect,
            suppliers=env.suppliers or (),
            collection_gateway=fake_shop.FakeGateway(),
            collections=env.registered,
        )
        try:
            built.connect.save_credentials(
                fake_shop.SUPPLIER_KEY,
                username=fake_shop.MEMBER_ID,
                password=fake_shop.MEMBER_PASSWORD,
                actor="operator",
            )
            built.connect.verify(fake_shop.SUPPLIER_KEY, trigger="operator_test", allow_login=True)
            if auto_connect:
                built.connect.set_auto_connect(
                    fake_shop.SUPPLIER_KEY, enabled=True, actor="operator"
                )
            assert m1_session_problems(built, fake_shop.SUPPLIER_KEY) == []
        finally:
            built.db.dispose()


def make_world(
    template: Path,
    base: Path,
    *,
    product_evidence: int = 13,
    images: dict[str, object] | None = None,
    mode: str = "DRY",
    auto_connect: bool = False,
) -> World:
    clock = FakeClock()
    config = campaign_data(template, base)
    gateway = fake_shop.FakeGateway(
        documents=[fake_shop.page(product_evidence=product_evidence)], images=images or {}
    )
    connect = WitnessConnect()
    env = environment(
        config, gateway=gateway, connect=connect, secrets=MemorySecretStore(), clock=clock
    )
    establish_m1(env, connect, auto_connect=auto_connect)
    ledger = CampaignLedger.create(base / "campaign" / "campaign.sqlite3")
    arm(
        ledger,
        env=env,
        product_url=fake_shop.PRODUCT_URL,
        phase_b_findings=PHASE_B,
        mode=mode,
        code_sha=CODE_SHA,
    )
    connect.ledger = ledger
    return World(ledger=ledger, env=env, gateway=gateway, connect=connect, clock=clock)


# ---------------------------------------------------------------- the armed contract


def test_the_armed_manifest_and_its_ceilings_can_never_change(
    migrated_template: Path, tmp_path: Path
) -> None:
    world = make_world(migrated_template, tmp_path)
    manifest = world.ledger.manifest()
    assert manifest is not None
    assert manifest["campaign_id"] == "m3-accept-01" and manifest["code_sha"] == CODE_SHA
    ceilings = manifest["budget"]["ceilings"]
    assert ceilings["IMAGE_REQUEST"] == {"per_pass": 13, "campaign": 26}
    assert ceilings["CONNECT_CONTROL_READ"] == {"per_pass": 2, "campaign": 4}
    assert ceilings["CONNECT_PROTECTED_READ"] == {"per_pass": 2, "campaign": 4}
    assert ceilings["CONNECT_AUTHENTICATE"] == {"per_pass": 0, "campaign": 0}

    with closing(sqlite3.connect(world.ledger.path)) as db:
        for statement in (
            "UPDATE manifest SET code_sha = 'b'",
            "DELETE FROM manifest",
            "UPDATE ceilings SET per_pass = 99 WHERE class = 'IMAGE_REQUEST'",
            "UPDATE ceilings SET per_pass = 1 WHERE class = 'CONNECT_AUTHENTICATE'",
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
            env=world.env,
            product_url=fake_shop.PRODUCT_URL,
            phase_b_findings=PHASE_B,
            mode="DRY",
            code_sha="c" * 40,
        )
    assert world.ledger.manifest() == manifest


def test_arming_refuses_a_budget_that_is_not_the_production_profile(
    migrated_template: Path, tmp_path: Path
) -> None:
    from integrations.suppliers.kmretail.collection import COLLECTION as KM

    # The frozen budget and KM's production profile agree; the campaign changes neither.
    assert profile_problems(KM, M3_ACCEPT_01_BUDGET) == []
    narrower = fake_shop.collection(max_image_requests=30)  # 1 MiB images, 4 MiB per run
    assert len(profile_problems(narrower, M3_ACCEPT_01_BUDGET)) == 2

    config = campaign_data(migrated_template, tmp_path)
    connect = WitnessConnect()
    env = environment(
        config,
        gateway=fake_shop.FakeGateway(),
        connect=connect,
        secrets=MemorySecretStore(),
        clock=FakeClock(),
        collection=narrower,
    )
    establish_m1(env, connect)
    ledger = CampaignLedger.create(tmp_path / "campaign" / "campaign.sqlite3")
    with pytest.raises(ArmingRefused):
        arm(
            ledger,
            env=env,
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
    migrated_template: Path, tmp_path: Path
) -> None:
    phrase = approval_phrase(CODE_SHA)
    assert phrase == f"APPROVE m3-accept-01 {CODE_SHA[:12]} TWO-PASS-REAL"
    assert typed_approval_matches(phrase, CODE_SHA)
    for typed in ("approve m3-accept-01", phrase.replace("REAL", "DRY"), "yes"):
        assert not typed_approval_matches(typed, CODE_SHA)

    real = make_world(migrated_template, tmp_path / "real", mode="REAL")
    with pytest.raises(LedgerError):
        real.ledger.begin_pass("A", session_nonce="n")  # not approved
    with pytest.raises(LedgerError):
        real.ledger.approve("b" * 40)  # a different SHA
    real.ledger.approve(CODE_SHA)
    assert real.ledger.state() is State.APPROVED

    dry = make_world(migrated_template, tmp_path / "dry")
    with pytest.raises(LedgerError):
        dry.ledger.approve(CODE_SHA)


# ---------------------------------------------------------------- the local M1 prerequisite


def test_arming_refuses_a_campaign_whose_data_directory_holds_no_m1_connection(
    migrated_template: Path, tmp_path: Path
) -> None:
    # PR #71 review 5233744115 Q2: the M1 connection must already be established in <root>/data.
    # Without it, arming refuses before a manifest or a ceiling exists, so nothing can be approved,
    # and the check itself sends nothing.
    config = campaign_data(migrated_template, tmp_path)
    connect = WitnessConnect()
    env = environment(
        config,
        gateway=fake_shop.FakeGateway(),
        connect=connect,
        secrets=MemorySecretStore(),
        clock=FakeClock(),
    )
    ledger = CampaignLedger.create(tmp_path / "campaign" / "campaign.sqlite3")

    def attempt() -> None:
        arm(
            ledger,
            env=env,
            product_url=fake_shop.PRODUCT_URL,
            phase_b_findings=PHASE_B,
            mode="REAL",
            code_sha=CODE_SHA,
        )

    with pytest.raises(ArmingRefused, match="ordinary CONNECT operation first"):
        attempt()
    assert ledger.state() is State.INITIALIZED
    assert ledger.manifest() is None
    with closing(sqlite3.connect(ledger.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM ceilings").fetchone() == (0,)
    with pytest.raises(LedgerError):
        ledger.approve(CODE_SHA)
    assert connect.fetches == [] and connect.logins == 0, "arming sent no request at all"

    # The operator establishes M1 there — outside the campaign — and arming then succeeds, still
    # without a single request of its own.
    establish_m1(env, connect)
    fetches, logins = list(connect.fetches), connect.logins
    attempt()
    assert ledger.state() is State.ARMED
    assert connect.fetches == fetches and connect.logins == logins


def test_the_arm_check_cannot_send_even_if_the_check_itself_tried(
    migrated_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The local check is composed with transports that refuse every request. Were the check ever
    # to reach for the owner's live verification, that request would stop at the transport.
    import scripts.m3accept.prep as prep
    from app.connect.service import OPERATOR_TEST
    from scripts.m3accept.m1 import LocalCheckOnly

    config = campaign_data(migrated_template, tmp_path)
    connect = WitnessConnect()
    env = environment(
        config,
        gateway=fake_shop.FakeGateway(),
        connect=connect,
        secrets=MemorySecretStore(),
        clock=FakeClock(),
    )
    establish_m1(env, connect)
    sent = len(connect.fetches)

    def reaching(container, supplier_key):  # type: ignore[no-untyped-def]
        container.connect.verify(supplier_key, trigger=OPERATOR_TEST, allow_login=False)
        return []

    monkeypatch.setattr(prep, "m1_session_problems", reaching)
    ledger = CampaignLedger.create(tmp_path / "campaign" / "campaign.sqlite3")
    with pytest.raises(LocalCheckOnly):
        arm(
            ledger,
            env=env,
            product_url=fake_shop.PRODUCT_URL,
            phase_b_findings=PHASE_B,
            mode="DRY",
            code_sha=CODE_SHA,
        )
    assert len(connect.fetches) == sent, "the would-be proof read never reached the transport"
    assert ledger.manifest() is None


def test_arming_accepts_no_data_directory_but_the_campaign_s_own(
    migrated_template: Path, tmp_path: Path
) -> None:
    # An ordinary data directory that does hold a usable M1 connection is not a substitute.
    elsewhere = make_config(tmp_path / "ordinary")
    database = database_path(elsewhere.data_dir)
    database.parent.mkdir(parents=True)
    shutil.copyfile(migrated_template, database)
    connect = WitnessConnect()
    env = environment(
        elsewhere,
        gateway=fake_shop.FakeGateway(),
        connect=connect,
        secrets=MemorySecretStore(),
        clock=FakeClock(),
    )
    establish_m1(env, connect)
    ledger = CampaignLedger.create(tmp_path / "campaign" / "campaign.sqlite3")
    with pytest.raises(ArmingRefused, match="not this campaign's own"):
        arm(
            ledger,
            env=env,
            product_url=fake_shop.PRODUCT_URL,
            phase_b_findings=PHASE_B,
            mode="DRY",
            code_sha=CODE_SHA,
        )
    assert ledger.manifest() is None


# ---------------------------------------------------------------- pre-send reservations


def test_nothing_is_reserved_outside_a_running_pass(
    migrated_template: Path, tmp_path: Path
) -> None:
    world = make_world(migrated_template, tmp_path)
    with pytest.raises(CampaignBudgetRefused):
        world.ledger.reserve("A", RequestClass.PRODUCT_READ, world.url)
    assert world.ledger.counts()["PRODUCT_READ"] == 0
    assert world.ledger.refusals()[-1]["reason"] == "PASS_NOT_RUNNING"


def test_a_transport_that_sends_without_reserving_stops_the_campaign(
    migrated_template: Path, tmp_path: Path
) -> None:
    world = make_world(migrated_template, tmp_path)
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
    migrated_template: Path, tmp_path: Path
) -> None:
    # The real policed transport, with a mock network: a refused reservation sends no byte, and an
    # allowed one is already durable in the ledger at the moment the request goes out.
    world = make_world(migrated_template, tmp_path)
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


def test_ceilings_hold_per_pass_and_across_the_campaign(
    migrated_template: Path, tmp_path: Path
) -> None:
    world = make_world(migrated_template, tmp_path)
    ledger = world.ledger

    def spend(pass_id: str, request: RequestClass, times: int) -> None:
        for n in range(times):
            ledger.reserve(pass_id, request, f"{request.value}-{pass_id}-{n}")

    def refused(pass_id: str, request: RequestClass) -> None:
        with pytest.raises(CampaignBudgetRefused):
            ledger.reserve(pass_id, request, "over")

    ledger.begin_pass("A", session_nonce="a")
    for request, per_pass in (
        (RequestClass.PRODUCT_READ, 2),
        (RequestClass.IMAGE_REQUEST, 13),
        (CONTROL, 2),
        (PROTECTED, 2),
    ):
        spend("A", request, per_pass)
        refused("A", request)
    refused("A", RequestClass.CONNECT_AUTHENTICATE)
    ledger.record_submission(Submission("A", "a", "run-a", "job-a", "cid-a"))
    ledger.finish_pass("A", verdict="ACCEPTED", detail={})

    ledger.begin_pass("B", session_nonce="b")
    for request, per_pass in (
        (RequestClass.PRODUCT_READ, 2),  # the refusal after this is product read #5
        (RequestClass.IMAGE_REQUEST, 13),  # ... image request #27
        (CONTROL, 2),  # ... control read #5
        (PROTECTED, 2),  # ... protected read #5
    ):
        spend("B", request, per_pass)
        refused("B", request)

    assert ledger.counts() == {
        "PRODUCT_READ": 4,
        "IMAGE_REQUEST": 26,
        "POLICY_READ": 0,
        "CONNECT_CONTROL_READ": 4,
        "CONNECT_PROTECTED_READ": 4,
        "CONNECT_AUTHENTICATE": 0,
    }


def test_classes_this_campaign_does_not_budget_are_refused(
    migrated_template: Path, tmp_path: Path
) -> None:
    world = make_world(migrated_template, tmp_path)
    world.ledger.begin_pass("A", session_nonce="a")
    for request in (RequestClass.POLICY_READ, RequestClass.CONNECT_AUTHENTICATE):
        with pytest.raises(CampaignBudgetRefused):
            world.ledger.reserve("A", request, "anything")

    connect = LedgeredConnectGateway(world.connect, world.ledger, "A")
    logins = world.connect.logins
    with pytest.raises(Exception, match="never logs in"):
        connect.login(fake_shop.connect_definition(), Credentials("x", "y"))
    assert world.connect.logins == logins, "a login never reaches the transport"
    assert world.ledger.counts()["CONNECT_AUTHENTICATE"] == 0
    assert world.ledger.refusals()[-1]["reason"] == "LOGIN_NOT_IN_CAMPAIGN"
    # AI, OCR and marketplace code is not reachable from anything a campaign runs.
    assert hard_zero_problems() == []


def test_a_restart_preserves_the_ledger_and_what_remains_of_the_budget(
    migrated_template: Path, tmp_path: Path
) -> None:
    world = make_world(migrated_template, tmp_path)
    world.ledger.begin_pass("A", session_nonce="a")
    for n in range(12):
        world.ledger.reserve("A", RequestClass.IMAGE_REQUEST, f"image-{n}")

    reopened = CampaignLedger(world.ledger.path)  # a new process, the same file
    assert reopened.state() is State.PASS_A_RUNNING
    assert reopened.counts("A")["IMAGE_REQUEST"] == 12
    reopened.reserve("A", RequestClass.IMAGE_REQUEST, "image-12")
    with pytest.raises(CampaignBudgetRefused):
        reopened.reserve("A", RequestClass.IMAGE_REQUEST, "image-13")


# ---------------------------------------------------------------- the production M1 owner


def test_a_pass_spends_one_proof_pair_each_reserved_before_it_is_sent(
    migrated_template: Path, tmp_path: Path
) -> None:
    # The production M1 owner proves the stored session through the ledgered CONNECT gateway. The
    # ledger records what was actually sent — 1 control + 1 protected read — and each reservation
    # was already committed when its request reached the transport.
    world = make_world(migrated_template, tmp_path)
    fetches_before, logins_before = len(world.connect.fetches), world.connect.logins

    outcome = run_pass(world.ledger, world.env, "A", world.url)

    assert outcome.status == "ACCEPTED", outcome.detail["reasons"]
    counts = world.ledger.counts("A")
    assert (counts["CONNECT_CONTROL_READ"], counts["CONNECT_PROTECTED_READ"]) == (1, 1)
    assert counts["CONNECT_AUTHENTICATE"] == 0
    assert world.connect.fetches[fetches_before:] == ["CONTROL_READ", "PROTECTED_READ"]
    assert world.connect.witnessed == [("CONTROL_READ", 1), ("PROTECTED_READ", 1)]
    assert world.connect.logins == logins_before, "the campaign never logs in"
    assert world.gateway.sessions and world.gateway.sessions[0] is not None


def test_proof_reads_follow_job_retries_and_a_third_never_reaches_the_transport(
    migrated_template: Path, tmp_path: Path
) -> None:
    # Two product attempts fail transiently. Each attempt asks the M1 owner for the session, so
    # each spends a proof pair: 2 + 2 is the ceiling. The job policy allows a third attempt; its
    # control read is refused by the ledger before send, and the transport never sees it.
    world = make_world(migrated_template, tmp_path)

    class FlakyTwice(fake_shop.FakeGateway):
        failures: int = 2

        def read_document(self, profile, url, *, kind, budget, session=None):  # type: ignore[no-untyped-def]
            if self.failures:
                budget.reserve(kind, url)  # reserved and sent, then the provider fails
                self.failures -= 1
                self.document_reads += 1
                raise TransientError("PROVIDER_BUSY", "try again")
            return super().read_document(profile, url, kind=kind, budget=budget, session=session)

    flaky = FlakyTwice(documents=[fake_shop.page(product_evidence=13)])
    world.env.collection_transport = lambda: flaky
    sent_before = len(world.connect.fetches)

    first = run_pass(world.ledger, world.env, "A", world.url)
    assert first.status == "IN_PROGRESS"
    world.clock.advance(COLLECT_POLICY.delay_after(1).total_seconds())
    second = run_pass(world.ledger, world.env, "A", world.url)
    assert second.status == "IN_PROGRESS"
    counts = world.ledger.counts("A")
    assert (counts["CONNECT_CONTROL_READ"], counts["CONNECT_PROTECTED_READ"]) == (2, 2)
    assert counts["PRODUCT_READ"] == 2

    world.clock.advance(COLLECT_POLICY.delay_after(2).total_seconds())
    third = run_pass(world.ledger, world.env, "A", world.url)

    assert third.status == "STOPPED"
    assert len(world.connect.fetches) - sent_before == 4, "the third proof read was never sent"
    counts = world.ledger.counts("A")
    assert (counts["CONNECT_CONTROL_READ"], counts["CONNECT_PROTECTED_READ"]) == (2, 2)
    assert counts["PRODUCT_READ"] == 2 and flaky.document_reads == 2
    assert {"pass": "A", "class": "CONNECT_CONTROL_READ", "reason": "PASS_CEILING"}.items() <= (
        world.ledger.refusals()[-1].items()
    )


def test_an_expired_session_stops_the_campaign_and_no_login_reaches_the_transport(
    migrated_template: Path, tmp_path: Path
) -> None:
    # The proof shows the stored session has lapsed, and auto-connect would let the owner log in.
    # The ledgered gateway refuses that login before send: no login, no recovery budget, a stop.
    world = make_world(migrated_template, tmp_path, auto_connect=True)
    world.connect.expired = True
    logins_before = world.connect.logins

    outcome = run_pass(world.ledger, world.env, "A", world.url)

    assert outcome.status == "STOPPED", (
        outcome.detail.get("run_detail"),
        outcome.detail["reasons"],
    )
    assert world.connect.logins == logins_before, "the login never reached the transport"
    counts = world.ledger.counts("A")
    assert counts["CONNECT_AUTHENTICATE"] == 0
    assert (counts["CONNECT_CONTROL_READ"], counts["CONNECT_PROTECTED_READ"]) == (1, 1)
    assert counts["PRODUCT_READ"] == 0 and world.gateway.document_reads == 0
    assert world.ledger.refusals()[-1]["reason"] == "LOGIN_NOT_IN_CAMPAIGN"
    # The production owner records the refused login as its own authentication failure, so the
    # connection is left unusable rather than recovered — a later pass stops at its local PREP.
    with acquire_data_dir(world.env.config.data_dir, app_version="check") as lease:
        built = build_container(
            world.env.config,
            ownership=lease,
            clock=world.clock,
            secret_store=world.env.secret_store,
            supplier_gateway=world.connect,
            suppliers=world.env.suppliers or (),
            collection_gateway=world.gateway,
            collections=world.env.registered,
        )
        try:
            assert m1_session_problems(built, fake_shop.SUPPLIER_KEY) != []
        finally:
            built.db.dispose()


def test_pre_pass_prep_stops_when_the_local_m1_session_is_gone(
    migrated_template: Path, tmp_path: Path
) -> None:
    # Without this local check the production owner would still be stopped — by the ledgered
    # gateway refusing its login — but only after it had tried to log in and marked its own
    # connection failed. The check stops the pass before any of that: no pass, no job, no login
    # attempt, and the M1 connection's state left exactly as the operator left it.
    world = make_world(migrated_template, tmp_path)
    shutil.rmtree(world.env.config.runtime_dir / SESSIONS_DIR_NAME)
    sent, logins = len(world.connect.fetches), world.connect.logins

    outcome = run_pass(world.ledger, world.env, "A", world.url)

    assert outcome.status == "STOPPED"
    assert world.ledger.state() is State.STOPPED
    assert world.ledger.events()[-1]["detail"] == {"reason": "M1_SESSION_NOT_LOADABLE"}
    assert world.ledger.submission("A") is None, "no pass was begun and no job was submitted"
    assert world.ledger.refusals() == [], "no login was even attempted"
    assert sum(world.ledger.counts().values()) == 0, "nothing was reserved"
    assert len(world.connect.fetches) == sent and world.connect.logins == logins


# ---------------------------------------------------------------- two fresh-session passes


def test_two_fresh_session_passes_record_two_revisions_of_one_product(
    migrated_template: Path, tmp_path: Path
) -> None:
    world = make_world(migrated_template, tmp_path)

    first = run_pass(world.ledger, world.env, "A", world.url)
    assert first.status == "ACCEPTED", first.detail["reasons"]
    assert world.ledger.state() is State.WAITING_FOR_PACING

    # Too soon: an explicit wait, not a loop. Nothing is reserved, submitted or sent.
    reads, counts, sent = (
        world.gateway.document_reads,
        world.ledger.counts(),
        len(world.connect.fetches),
    )
    waiting = run_pass(world.ledger, world.env, "B", world.url)
    assert waiting.status == "WAITING_FOR_PACING" and waiting.seconds_remaining > 0
    assert world.ledger.counts() == counts, "waiting spent no reservation"
    assert world.gateway.document_reads == reads, "waiting sent no product read"
    assert len(world.connect.fetches) == sent, "waiting sent no session proof"
    assert world.ledger.submission("B") is None
    assert world.ledger.state() is State.WAITING_FOR_PACING

    world.clock.advance(M3_ACCEPT_01_BUDGET.same_product_interval_s + 1)
    world.connect.pass_id = "B"
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
    assert report["requests"] == {
        "PRODUCT_READ": 2,
        "IMAGE_REQUEST": 26,
        "POLICY_READ": 0,
        "CONNECT_CONTROL_READ": 2,
        "CONNECT_PROTECTED_READ": 2,
        "CONNECT_AUTHENTICATE": 0,
    }
    assert report["hard_zero"] == {"AI": 0, "OCR": 0, "MARKETPLACE": 0, "SUPPLIER_WRITE": 0}
    assert len(report["capability_boundary"]) == 2
    assert fake_shop.PRODUCT_URL not in str(report), "the closeout carries no raw target URL"

    # Everything above is read back from durable state by a process that did none of it.
    reopened = CampaignLedger(world.ledger.path)
    assert reopened.state() is State.COMPLETED
    assert reopened.counts() == report["requests"]
    result_a, result_b = reopened.result("A"), reopened.result("B")
    assert result_a is not None and result_b is not None
    assert result_a["detail"]["collection_run_id"] == a.collection_run_id
    assert result_b["detail"]["collection_run_id"] == b.collection_run_id
    revisions = {result_a["detail"]["revision_id"], result_b["detail"]["revision_id"]}
    assert len(revisions) == 2 and None not in revisions


def test_pass_b_never_runs_in_the_session_pass_a_used(
    migrated_template: Path, tmp_path: Path
) -> None:
    world = make_world(migrated_template, tmp_path)
    world.ledger.begin_pass("A", session_nonce="same")
    world.ledger.record_submission(Submission("A", "same", "run", "job", "cid"))
    world.ledger.finish_pass("A", verdict="ACCEPTED", detail={})
    with pytest.raises(LedgerError, match="fresh session"):
        world.ledger.begin_pass("B", session_nonce="same")
    world.ledger.begin_pass("B", session_nonce="another")


def test_a_job_retry_is_owned_by_the_job_and_spends_the_same_budget(
    migrated_template: Path, tmp_path: Path
) -> None:
    world = make_world(migrated_template, tmp_path)

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
    counts = world.ledger.counts("A")
    assert counts["PRODUCT_READ"] == 2, "the retry spent the same budget"
    assert (counts["CONNECT_CONTROL_READ"], counts["CONNECT_PROTECTED_READ"]) == (2, 2)
    assert flaky.document_reads == 2


# ---------------------------------------------------------------- reference drift and byte caps


@pytest.mark.parametrize("references", [12, 13, 14])
def test_product_evidence_drift_around_the_frozen_baseline(
    migrated_template: Path, tmp_path: Path, references: int
) -> None:
    world = make_world(migrated_template, tmp_path, product_evidence=references)

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
    migrated_template: Path, tmp_path: Path
) -> None:
    # Every image fits the 2 MiB bound on its own; together they do not fit 24 MiB. The production
    # path refuses the body that would cross the total, and the campaign calls the pass HOLD.
    big = fake_shop.png(64, 64) + b"\x00" * (19 * MIB // 10)
    assert len(big) <= 2 * MIB
    urls = [fake_shop.PRIMARY_URL] + [fake_shop.detail_url(i) for i in range(12)]
    world = make_world(migrated_template, tmp_path, images=dict.fromkeys(urls, big))

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


@pytest.fixture(autouse=True)
def _no_live_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Belt and braces: nothing in this module may build the live CONNECT transport either."""
    import integrations.suppliers.transport.gateway as gateway

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test built the live CONNECT transport")

    monkeypatch.setattr(gateway.PolicedSupplierGateway, "__init__", refuse)
    yield
