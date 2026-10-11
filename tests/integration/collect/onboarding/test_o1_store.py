"""ADR-0036 O1: the supplier onboarding owner, its migration and the running registry.

Everything is offline. The onboarded storefronts are invented names that nothing resolves or
fetches; no gateway is ever asked to send a request, and the extension capture is handed to the
service directly. Proven here:

- ``create`` writes a ``DRAFT`` identity and its bootstrap storefront host, after the OB-01 guard;
- the state log takes the allowed edges only, in the store and in the database, and ``ACTIVE``
  only when 3 distinct verified samples under the current words prove it (§7 D2, OB-04);
- a words revision passes the site-word rules and the template's own slots, numbered with an
  optimistic sequence, and a change to an ``ACTIVE`` site returns it to ``RECON`` (OB-04);
- every onboarding table refuses UPDATE and DELETE, and the downgrade fails closed;
- an onboarded site binds through the cafe24 and godomall templates with the identity
  ``<template revision>+<key>-w<seq>`` (§4), and the running registry rebinds it without a
  restart: CONNECT lists it and readiness names it at once;
- OB-10: COLLECT submit, the extension capture, its recording and the list queue refuse a site
  that is not ``ACTIVE``, and accept it once it is;
- an onboarded site's egress hosts are exactly its confirmed, non-disabled hosts (with its
  template's own platform hosts), and ``DISABLED`` withdraws them all;
- the repository sites keep their identities and their own ``RECON`` rule (OB-07).
"""

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command

from app.config import AppConfig, database_path
from app.container import Container, _registered
from app.platform.core.errors import AppError, InputValidationError, NotFoundError
from app.platform.db.database import create_sqlite_engine
from app.platform.db.migrate import alembic_config, current_revision, upgrade_to_head
from app.stages.collect.collection import SUPPLIER_NOT_ACTIVE
from app.stages.collect.extension.capture import CaptureEnvelope
from app.stages.collect.onboarding.catalog import SupplierCatalog
from app.stages.collect.onboarding.hosts import (
    ONBOARDING_HOST_REFUSED,
    ONBOARDING_ORIGIN_REFUSED,
    onboarding_host,
    onboarding_origin,
)
from app.stages.collect.onboarding.site import HostRole, OnboardingState, onboarded_site
from app.stages.collect.onboarding.store import (
    ACTIVATION_SAMPLES,
    CORE_FIELDS,
    ONBOARDING_ACTIVATION_NOT_PROVEN,
    ONBOARDING_HOST_TAKEN,
    ONBOARDING_HOST_UNOBSERVED,
    ONBOARDING_SUPPLIER_DISABLED,
    ONBOARDING_TRANSITION_REFUSED,
    ONBOARDING_WORDS_INVALID,
    ONBOARDING_WORDS_MOVED,
    ONBOARDING_WORDS_SLOT_UNKNOWN,
    ONBOARDING_WORDS_UNCHANGED,
    OnboardingStore,
)
from integrations.suppliers.collection import DocumentView, ReadKind
from integrations.suppliers.registry import COLLECTIONS, SITES
from integrations.suppliers.sites import TEMPLATES, bind_site, bind_sites
from tests.support.jobs_support import FakeClock

pytestmark = pytest.mark.integration

OPERATOR = "operator"
CAFE24_ORIGIN = "https://shop.onboard-sample.co.kr"
CAFE24_HOST = "shop.onboard-sample.co.kr"
CAFE24_KEY = "site-shop-onboard-sample-co-kr"
GODOMALL_ORIGIN = "https://www.godo-sample.com"
GODOMALL_KEY = "site-godo-sample-com"
CAFE24_PRODUCT = f"{CAFE24_ORIGIN}/product/sample/123/"
TABLES = (
    "onboarded_suppliers",
    "onboarding_transitions",
    "onboarding_trials",
    "onboarding_hosts",
    "onboarding_word_revisions",
    "onboarding_policy_reads",
    "onboarding_verifications",
)
WORDS = {"purchase_price": ["공급가"], "list_price": ["권장소비자가"]}
BEFORE = "0059_detail_guidance_placement"
MIGRATIONS = Path(__file__).resolve().parents[4] / "app/platform/db/migrations/versions"
HEAD = "0060_supplier_onboarding"


@contextlib.contextmanager
def raw(config: AppConfig) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database_path(config.data_dir))
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def count(config: AppConfig, table: str) -> int:
    with raw(config) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def cafe24(container: Container) -> str:
    return container.onboarding.create(
        name="온보드 샘플", origin=CAFE24_ORIGIN, template="cafe24", actor=OPERATOR
    ).supplier_key


ALL_CORE_READ = dict.fromkeys(CORE_FIELDS, "CONFIRMED")
FACTS = "c" * 64


def trial(
    config: AppConfig,
    key: str,
    hosts: tuple[str, ...] = (),
    *,
    sample: str = CAFE24_PRODUCT,
    words_seq: int = 0,
    statuses: dict[str, str] | None = None,
    structural: list[str] | None = None,
    template_revision: str | None = None,
) -> str:
    """A trial row as O3's writer will append it. This is a test fixture: O1 has no trial
    writer, and the store stays the only production writer."""
    trial_id = str(uuid.uuid4())
    with raw(config) as connection:
        connection.execute(
            "INSERT INTO onboarding_trials (trial_id, supplier_key, sample_url_key,"
            " template_revision, words_seq, capture_sha256, facts_fingerprint, field_status_json,"
            " unread_rows_json, observed_hosts_json, structural_json, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)",
            (
                trial_id,
                key,
                sample,
                template_revision or TEMPLATES["cafe24"].revision,
                words_seq,
                "a" * 64,
                FACTS,
                json.dumps(ALL_CORE_READ if statuses is None else statuses),
                json.dumps([{"host": h, "role": "image"} for h in hosts]),
                json.dumps(structural or []),
                OPERATOR,
                "2026-10-12 00:00:00",
            ),
        )
    return trial_id


def verify(
    config: AppConfig, key: str, trial_id: str, verdict: str = "CORRECT", shown: str = FACTS
) -> str:
    """An operator verification as O5's writer will append it (a test fixture, as above)."""
    verification_id = str(uuid.uuid4())
    with raw(config) as connection:
        connection.execute(
            "INSERT INTO onboarding_verifications (verification_id, trial_id, supplier_key,"
            " shown_fingerprint, verdict, verified_by, verified_at)"
            " VALUES (?, ?, ?, ?, ?, 'operator', '2026-10-12 00:00:00')",
            (verification_id, trial_id, key, shown, verdict),
        )
    return verification_id


def prove(config: AppConfig, key: str, words_seq: int, samples: int = 3) -> list[str]:
    """``samples`` distinct verified samples under ``words_seq``; their verification ids."""
    return [
        verify(config, key, trial(config, key, sample=f"{CAFE24_PRODUCT}?n={n}", words_seq=seq))
        for n, seq in ((n, words_seq) for n in range(samples))
    ]


def activate(container: Container, config: AppConfig, key: str) -> None:
    record = container.onboarding.current(key)
    if record.state is OnboardingState.DRAFT:
        container.onboarding.transition(key, OnboardingState.RECON, actor=OPERATOR)
    prove(config, key, record.words_seq)
    container.onboarding.transition(key, OnboardingState.ACTIVE, actor=OPERATOR)


def refused(code: str, call: object) -> AppError:
    assert callable(call)
    with pytest.raises(AppError) as caught:
        call()
    assert caught.value.code == code, caught.value.code
    return caught.value


# ---------------------------------------------------------------- create and the host guard


def test_create_is_a_draft_with_its_bootstrap_storefront_host(
    container: Container, config: AppConfig
) -> None:
    record = container.onboarding.create(
        name="온보드 샘플",
        origin="https://Shop.Onboard-Sample.CO.KR/",
        template="cafe24",
        actor=OPERATOR,
    )
    assert record.supplier_key == CAFE24_KEY
    assert record.origin == CAFE24_ORIGIN and record.storefront_host == CAFE24_HOST
    assert record.state is OnboardingState.DRAFT and record.state_seq == 1
    assert record.words is None and record.words_seq == 0
    assert [(h.host, h.role, h.observed_trial_id) for h in record.hosts] == [
        (CAFE24_HOST, HostRole.STOREFRONT, None)
    ]
    assert container.onboarding.all_current() == (record,)
    with raw(config) as connection:
        events = connection.execute(
            "SELECT action, target_ref, before_json, after_json, details_json FROM audit_events"
            " WHERE event_type = 'SUPPLIER_ONBOARDING_RECORDED'"
        ).fetchall()
    assert [e[:2] for e in events] == [("ONBOARDING_SUPPLIER_CREATED", f"supplier:{CAFE24_KEY}")]
    # Identifiers and hosts only: never the operator's name for it.
    assert "온보드" not in json.dumps(events, ensure_ascii=False)


@pytest.mark.parametrize(
    ("origin", "code", "reason"),
    [
        ("http://shop.onboard-sample.co.kr", ONBOARDING_ORIGIN_REFUSED, "scheme"),
        ("https://192.168.0.10", ONBOARDING_HOST_REFUSED, "ip_literal"),
        ("https://10.1.2.3", ONBOARDING_HOST_REFUSED, "ip_literal"),
        ("https://169.254.169.254", ONBOARDING_HOST_REFUSED, "ip_literal"),
        ("https://127.1", ONBOARDING_HOST_REFUSED, "ip_literal"),
        ("https://[::1]", ONBOARDING_HOST_REFUSED, "ip_literal"),
        ("https://[fe80::1]", ONBOARDING_HOST_REFUSED, "ip_literal"),
        ("https://localhost", ONBOARDING_HOST_REFUSED, "form"),
        ("https://shop.localhost", ONBOARDING_HOST_REFUSED, "special_use"),
        ("https://printer.local", ONBOARDING_HOST_REFUSED, "special_use"),
        ("https://shop.onboard-sample.co.kr:8443", ONBOARDING_HOST_REFUSED, "port"),
        ("https://shop.onboard-sample.co.kr/shop", ONBOARDING_HOST_REFUSED, "path"),
        ("https://shop.onboard-sample.co.kr/?a=1", ONBOARDING_HOST_REFUSED, "query"),
        ("https://*.onboard-sample.co.kr", ONBOARDING_HOST_REFUSED, "wildcard"),
        ("https://me@shop.onboard-sample.co.kr", ONBOARDING_HOST_REFUSED, "userinfo"),
        ("https://shop_1.onboard-sample.co.kr", ONBOARDING_HOST_REFUSED, "form"),
    ],
)
def test_the_host_guard_refuses_anything_but_an_exact_public_https_host(
    container: Container, config: AppConfig, origin: str, code: str, reason: str
) -> None:
    error = refused(
        code,
        lambda: container.onboarding.create(
            name="거부", origin=origin, template="cafe24", actor=OPERATOR
        ),
    )
    assert error.details.get("reason") == reason
    assert origin not in json.dumps(error.details)
    assert all(count(config, table) == 0 for table in TABLES)


def test_the_host_guard_normalises_case_and_idna() -> None:
    assert onboarding_origin("https://SHOP.Onboard-Sample.co.KR") == (CAFE24_ORIGIN, CAFE24_HOST)
    origin, host = onboarding_origin("https://한국상점.kr/")
    assert host.startswith("xn--") and host.endswith(".kr") and origin == f"https://{host}"
    assert onboarding_host("IMG.Onboard-CDN.com") == "img.onboard-cdn.com"
    for value in ("10.0.0.1", "::1", "*.cdn.com", "cdn.com.", "cdn", "a..b.com", " cdn.com"):
        with pytest.raises(InputValidationError):
            onboarding_host(value)


def test_a_repository_storefront_or_a_second_onboarding_of_one_origin_is_refused(
    container: Container,
) -> None:
    refused(
        ONBOARDING_HOST_TAKEN,
        lambda: container.onboarding.create(
            name="유픽", origin="https://upickb2b.com", template="cafe24", actor=OPERATOR
        ),
    )
    cafe24(container)
    refused(ONBOARDING_HOST_TAKEN, lambda: cafe24(container))
    refused(
        "ONBOARDING_TEMPLATE_UNKNOWN",
        lambda: container.onboarding.create(
            name="메이크샵", origin="https://make.onboard.com", template="makeshop", actor=OPERATOR
        ),
    )


# ---------------------------------------------------------------- the state log


def test_the_state_log_takes_the_allowed_edges_only(
    container: Container, config: AppConfig
) -> None:
    key = cafe24(container)
    store = container.onboarding
    prove(config, key, 0)
    refused(ONBOARDING_TRANSITION_REFUSED, lambda: store.transition(key, "ACTIVE", actor=OPERATOR))
    assert store.transition(key, OnboardingState.RECON, actor=OPERATOR).state_seq == 2
    refused(ONBOARDING_TRANSITION_REFUSED, lambda: store.transition(key, "DRAFT", actor=OPERATOR))
    refused(ONBOARDING_TRANSITION_REFUSED, lambda: store.transition(key, "RECON", actor=OPERATOR))
    active = store.transition(key, "ACTIVE", actor=OPERATOR, reason="승인", evidence=["v1"])
    assert active.state is OnboardingState.ACTIVE
    assert store.transition(key, "RECON", actor=OPERATOR).state is OnboardingState.RECON
    disabled = store.transition(key, "DISABLED", actor=OPERATOR, reason="중단")
    assert (disabled.state, disabled.state_seq) == (OnboardingState.DISABLED, 5)
    for state in OnboardingState:
        refused(ONBOARDING_TRANSITION_REFUSED, lambda s=state: store.transition(key, s, actor="x"))
    refused(
        ONBOARDING_SUPPLIER_DISABLED,
        lambda: store.save_words(key, WORDS, expected_seq=0, actor="x"),
    )
    with pytest.raises(NotFoundError):
        store.transition("site-unknown", "RECON", actor=OPERATOR)
    refused(
        "ONBOARDING_EVIDENCE_INVALID",
        lambda: store.transition(
            cafe24_second(container), "RECON", actor=OPERATOR, evidence=["not an id!"]
        ),
    )


def cafe24_second(container: Container) -> str:
    return container.onboarding.create(
        name="둘째", origin="https://second.onboard-sample.co.kr", template="cafe24", actor=OPERATOR
    ).supplier_key


def test_the_database_refuses_a_state_edge_the_store_would_refuse(
    container: Container, config: AppConfig
) -> None:
    key = cafe24(container)

    def insert(seq: int, before: str | None, after: str) -> None:
        with raw(config) as connection:
            connection.execute(
                "INSERT INTO onboarding_transitions VALUES (?, ?, ?, ?, ?, 'x', '', '[]',"
                " '2026-10-12 00:00:00')",
                (str(uuid.uuid4()), key, seq, before, after),
            )

    with pytest.raises(sqlite3.IntegrityError, match="allowed edge"):
        insert(2, "DRAFT", "DRAFT")
    with pytest.raises(sqlite3.IntegrityError, match="leaves the state"):
        insert(2, "RECON", "DISABLED")
    with pytest.raises(sqlite3.IntegrityError, match=r"follows|leaves"):
        insert(3, "DRAFT", "RECON")
    insert(2, "DRAFT", "RECON")
    assert container.onboarding.current(key).state is OnboardingState.RECON


# ---------------------------------------------------------------- words


def test_words_are_plain_words_in_the_template_slots_with_an_optimistic_sequence(
    container: Container, config: AppConfig
) -> None:
    key = cafe24(container)
    store = container.onboarding
    refused(
        ONBOARDING_WORDS_SLOT_UNKNOWN,
        lambda: store.save_words(key, {"price_regex": ["공급가"]}, expected_seq=0, actor="x"),
    )
    for bad in ({"price": ["<b>공급가</b>"]}, {"price": ["공급.*가"]}, {"price": []}, ["가"]):
        refused(
            ONBOARDING_WORDS_INVALID,
            lambda b=bad: store.save_words(key, b, expected_seq=0, actor="x"),  # type: ignore[arg-type]
        )
    # ADR-0032 §2: one label names one price role at most.
    refused(
        ONBOARDING_WORDS_INVALID,
        lambda: store.save_words(
            key, {"purchase_price": ["가격"], "list_price": ["가격"]}, expected_seq=0, actor="x"
        ),
    )
    first = store.save_words(key, WORDS, expected_seq=0, actor=OPERATOR)
    assert first.words is not None and first.words_seq == 1
    assert dict(first.words.words) == {
        "list_price": ("권장소비자가",),
        "purchase_price": ("공급가",),
    }
    refused(ONBOARDING_WORDS_MOVED, lambda: store.save_words(key, WORDS, expected_seq=0, actor="x"))
    refused(
        ONBOARDING_WORDS_UNCHANGED, lambda: store.save_words(key, WORDS, expected_seq=1, actor="x")
    )
    second = store.save_words(key, {**WORDS, "price": ["도매가"]}, expected_seq=1, actor=OPERATOR)
    assert second.words_seq == 2 and second.state is OnboardingState.DRAFT
    assert count(config, "onboarding_word_revisions") == 2
    with raw(config) as connection:
        audited = connection.execute(
            "SELECT before_json, after_json FROM audit_events"
            " WHERE action = 'ONBOARDING_WORDS_SAVED'"
        ).fetchall()
    assert len(audited) == 2
    assert "공급가" not in json.dumps(audited, ensure_ascii=False), "no word reaches the audit log"


def test_a_words_change_returns_an_active_site_to_recon(
    container: Container, config: AppConfig
) -> None:
    key = cafe24(container)
    activate(container, config, key)
    changed = container.onboarding.save_words(key, WORDS, expected_seq=0, actor=OPERATOR)
    assert changed.state is OnboardingState.RECON and changed.state_seq == 4
    assert container.supplier_catalog.collectable(key) is False
    # The samples verified under words 0 prove nothing about words 1 (OB-04).
    proof = container.onboarding.activation(key)
    assert (proof.words_seq, proof.trials, proof.proven) == (1, 0, False)
    refused(
        ONBOARDING_ACTIVATION_NOT_PROVEN,
        lambda: container.onboarding.transition(key, "ACTIVE", actor=OPERATOR),
    )
    activate(container, config, key)
    assert container.onboarding.current(key).state is OnboardingState.ACTIVE


# ---------------------------------------------------------------- the ACTIVE gate (§7, OB-04)


def recon(container: Container) -> str:
    key = cafe24(container)
    container.onboarding.transition(key, OnboardingState.RECON, actor=OPERATOR)
    return key


def not_proven(container: Container, key: str) -> dict[str, object]:
    error = refused(
        ONBOARDING_ACTIVATION_NOT_PROVEN,
        lambda: container.onboarding.transition(key, "ACTIVE", actor=OPERATOR),
    )
    assert container.onboarding.current(key).state is OnboardingState.RECON
    assert container.supplier_catalog.collectable(key) is False
    return error.details


def test_active_is_entered_with_three_verified_samples_named_as_evidence(
    container: Container, config: AppConfig
) -> None:
    key = recon(container)
    assert ACTIVATION_SAMPLES == 3
    assert set(CORE_FIELDS) == {"original_name", "prices", "options", "images", "stock"}
    # The database gate freezes the same list with the migration.
    migration = (MIGRATIONS / "0060_supplier_onboarding.py").read_text("utf-8")
    frozen = ", ".join(f'"{field}"' for field in CORE_FIELDS)
    assert f"CORE_FIELDS = ({frozen})" in migration
    verifications = prove(config, key, 0)
    proof = container.onboarding.activation(key)
    assert proof.proven and len(proof.proven_samples) == 3
    assert sorted(proof.evidence) == sorted(verifications)
    container.onboarding.transition(key, "ACTIVE", actor=OPERATOR, evidence=["ui-click"])
    with raw(config) as connection:
        evidence = connection.execute(
            "SELECT evidence_json FROM onboarding_transitions WHERE to_state = 'ACTIVE'"
        ).fetchone()[0]
    assert json.loads(evidence)[0] == "ui-click"
    assert sorted(json.loads(evidence)[1:]) == sorted(verifications)


def test_fewer_than_three_distinct_samples_do_not_prove_active(
    container: Container, config: AppConfig
) -> None:
    key = recon(container)
    prove(config, key, 0, samples=2)
    # A second verified trial of a sample already proven is not a third sample.
    verify(config, key, trial(config, key, sample=f"{CAFE24_PRODUCT}?n=0"))
    details = not_proven(container, key)
    assert details["proven_samples"] == 2 and details["trials"] == 3
    assert details["required_samples"] == 3


def test_trials_under_an_old_words_revision_do_not_prove_active(
    container: Container, config: AppConfig
) -> None:
    key = recon(container)
    prove(config, key, 0)
    container.onboarding.save_words(key, WORDS, expected_seq=0, actor=OPERATOR)
    details = not_proven(container, key)
    assert (details["words_seq"], details["trials"], details["proven_samples"]) == (1, 0, 0)
    # Nor do trials of another template revision under the current words.
    for n in range(3):
        sample = f"{CAFE24_PRODUCT}?old={n}"
        stale = trial(config, key, sample=sample, words_seq=1, template_revision="cafe24-0")
        verify(config, key, stale)
    assert not_proven(container, key)["proven_samples"] == 0


def test_an_unread_core_field_does_not_prove_a_sample(
    container: Container, config: AppConfig
) -> None:
    key = recon(container)
    prove(config, key, 0, samples=2)
    for status in ("REVIEW_REQUIRED", "ABSENT", None):
        statuses = dict(ALL_CORE_READ)
        if status is None:
            del statuses["stock"]
        else:
            statuses["prices"] = status
        unread = trial(config, key, sample=f"{CAFE24_PRODUCT}?s={status}", statuses=statuses)
        verify(config, key, unread)
    details = not_proven(container, key)
    assert (details["proven_samples"], details["core_unread"]) == (2, 3)


def test_a_wrong_or_mismatched_verification_does_not_prove_a_sample(
    container: Container, config: AppConfig
) -> None:
    key = recon(container)
    prove(config, key, 0, samples=2)
    refuted = trial(config, key, sample=f"{CAFE24_PRODUCT}?n=wrong")
    verify(config, key, refuted, "CORRECT")
    verify(config, key, refuted, "WRONG")
    mismatched = trial(config, key, sample=f"{CAFE24_PRODUCT}?n=other")
    verify(config, key, mismatched, "CORRECT", shown="d" * 64)
    unverified = trial(config, key, sample=f"{CAFE24_PRODUCT}?n=none")
    details = not_proven(container, key)
    assert details["proven_samples"] == 2 and details["refuted"] == 1
    assert details["unverified"] == 2
    assert unverified


def test_an_open_structural_problem_blocks_active(container: Container, config: AppConfig) -> None:
    key = recon(container)
    prove(config, key, 0)
    flagged = trial(config, key, sample=f"{CAFE24_PRODUCT}?n=s", structural=["price_block"])
    verify(config, key, flagged)
    details = not_proven(container, key)
    assert (details["proven_samples"], details["structural_open"]) == (3, 1)


def test_the_database_refuses_an_unproven_active(container: Container, config: AppConfig) -> None:
    key = recon(container)

    def insert() -> None:
        with raw(config) as connection:
            connection.execute(
                "INSERT INTO onboarding_transitions VALUES (?, ?, 3, 'RECON', 'ACTIVE', 'x', '',"
                " '[]', '2026-10-12 00:00:00')",
                (str(uuid.uuid4()), key),
            )

    prove(config, key, 0, samples=2)
    with pytest.raises(sqlite3.IntegrityError, match="only when proven"):
        insert()
    refuted = trial(config, key, sample=f"{CAFE24_PRODUCT}?n=wrong")
    verify(config, key, refuted, "CORRECT")
    verify(config, key, refuted, "WRONG")
    with pytest.raises(sqlite3.IntegrityError, match="only when proven"):
        insert()
    third = trial(config, key, sample=f"{CAFE24_PRODUCT}?n=third")
    verify(config, key, third)
    flagged = trial(config, key, sample=f"{CAFE24_PRODUCT}?n=s", structural=["script_rendered"])
    with pytest.raises(sqlite3.IntegrityError, match="only when proven"):
        insert()
    assert flagged
    container.onboarding.save_words(key, WORDS, expected_seq=0, actor=OPERATOR)
    prove(config, key, 1)
    insert()


# ---------------------------------------------------------------- immutability and migration


def test_every_onboarding_table_refuses_update_and_delete(
    container: Container, config: AppConfig
) -> None:
    key = cafe24(container)
    container.onboarding.save_words(key, WORDS, expected_seq=0, actor=OPERATOR)
    trial_id = trial(config, key, ("img.onboard-cdn.com",))
    with raw(config) as connection:
        connection.execute(
            "INSERT INTO onboarding_policy_reads VALUES (?, ?, 'ROBOTS', '/robots.txt', 200, NULL,"
            " '2026-10-12 00:00:00', 'operator', '2026-10-12 00:00:01')",
            (str(uuid.uuid4()), key),
        )
        connection.execute(
            "INSERT INTO onboarding_verifications VALUES (?, ?, ?, ?, 'CORRECT', 'operator',"
            " '2026-10-12 00:00:00')",
            (str(uuid.uuid4()), trial_id, key, "b" * 64),
        )
    for table in TABLES:
        assert count(config, table) >= 1, table
        with raw(config) as connection:
            with pytest.raises(sqlite3.IntegrityError, match="never updated"):
                connection.execute(f"UPDATE {table} SET rowid = rowid")
            with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
                connection.execute(f"DELETE FROM {table}")


def test_a_verification_or_host_names_a_trial_of_its_own_supplier(
    container: Container, config: AppConfig
) -> None:
    first = cafe24(container)
    other = cafe24_second(container)
    trial_id = trial(config, first, ("img.onboard-cdn.com",))
    with raw(config) as connection, pytest.raises(sqlite3.IntegrityError, match="own supplier"):
        connection.execute(
            "INSERT INTO onboarding_verifications VALUES (?, ?, ?, ?, 'CORRECT', 'operator',"
            " '2026-10-12 00:00:00')",
            (str(uuid.uuid4()), trial_id, other, "b" * 64),
        )
    refused(
        ONBOARDING_HOST_UNOBSERVED,
        lambda: container.onboarding.confirm_host(
            other, "img.onboard-cdn.com", "image", observed_trial_id=trial_id, actor=OPERATOR
        ),
    )


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_0060_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = _url(database)
    upgrade_to_head(url)
    command.downgrade(alembic_config(url), BEFORE)
    with contextlib.closing(sqlite3.connect(database)) as connection:
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert not names & set(TABLES)
    assert not any(name.startswith("trg_onboard") for name in names)
    command.upgrade(alembic_config(url), "head")
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO onboarded_suppliers VALUES ('site-held-shop', '보존', 'https://held.shop.kr',"
            " 'cafe24', 'operator', '2026-10-12 00:00:00')"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), BEFORE)
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == HEAD
    finally:
        engine.dispose()
    with contextlib.closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM onboarded_suppliers").fetchone() == (1,)


def test_the_identity_form_is_checked_by_the_database(
    container: Container, config: AppConfig
) -> None:
    for key in ("kmretail", "site-", "site-Upper", "site-a_b", "upick"):
        with raw(config) as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO onboarded_suppliers VALUES (?, '이름', 'https://form.shop.kr',"
                " 'cafe24', 'operator', '2026-10-12 00:00:00')",
                (key,),
            )


# ---------------------------------------------------------------- binding and identity


@pytest.mark.parametrize(
    ("template", "origin", "key", "slot"),
    [
        ("cafe24", CAFE24_ORIGIN, CAFE24_KEY, "purchase_price"),
        ("godomall", GODOMALL_ORIGIN, GODOMALL_KEY, "purchase_price"),
    ],
)
def test_an_onboarded_site_binds_through_its_template_with_a_words_identity(
    container: Container, template: str, origin: str, key: str, slot: str
) -> None:
    store = container.onboarding
    record = store.create(name="샘플", origin=origin, template=template, actor=OPERATOR)
    assert record.supplier_key == key
    chosen = TEMPLATES[template]
    bound, problems = bind_site(onboarded_site(record, chosen), chosen)
    assert problems == () and bound is not None
    assert bound.extractor_revision == f"{chosen.revision}+{key}-w0"
    after = store.save_words(key, {slot: ["회원공급가"]}, expected_seq=0, actor=OPERATOR)
    bound_after, _ = bind_site(onboarded_site(after, chosen), chosen)
    assert bound_after is not None
    assert bound_after.extractor_revision == f"{chosen.revision}+{key}-w1"
    assert bound_after.extractor_fingerprint != bound.extractor_fingerprint
    assert bound_after.collection.roles.identity == bound_after.extractor_revision
    # The running registry holds exactly what the binder produced.
    registered = container.supplier_catalog.collections[key]
    assert registered.extractor_revision == bound_after.extractor_revision
    assert registered.extractor_fingerprint == bound_after.extractor_fingerprint
    assert bound_after.config.label_overrides == {slot: ("회원공급가",)}
    assert dict(bound_after.config.region_overrides) == {} and dict(bound_after.config.limits) == {}


def test_the_running_registry_rebinds_a_supplier_without_a_restart(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    connect = container.connect
    assert CAFE24_KEY not in {s.supplier_key for s in connect.supplier_connections()}
    key = cafe24(container)
    listed = {s.supplier_key: s for s in connect.supplier_connections()}
    assert listed[key].display_name == "온보드 샘플" and listed[key].base_url == CAFE24_ORIGIN
    assert f"supplier:{key}" in {c.key for c in connect.capabilities()}
    assert container.supplier_names[key] == "온보드 샘플"
    first = container.supplier_catalog.collections[key]
    assert first.extractor_revision.endswith(f"+{key}-w0")
    container.onboarding.save_words(key, WORDS, expected_seq=0, actor=OPERATOR)
    second = container.supplier_catalog.collections[key]
    assert second.extractor_revision.endswith(f"+{key}-w1")
    assert second.extractor_fingerprint != first.extractor_fingerprint
    # A fresh registry over the same database binds the same supplier at start (§4).
    restarted = SupplierCatalog(
        definitions=(),
        collections=(),
        capture_policies={},
        repository_active=lambda _key: None,
        store=OnboardingStore(container.db, clock, container.audit),
    )
    assert restarted.collections[key].extractor_fingerprint == second.extractor_fingerprint
    assert restarted.definitions[key].profile.base_url == CAFE24_ORIGIN


# ---------------------------------------------------------------- OB-10


def capture(container: Container, key: str) -> CaptureEnvelope:
    policy = container.extension_capture.policy(key)
    html = (
        "<!doctype html><html><head>"
        '<meta property="product:productId" content="123"></head>'
        '<body><div class="xans-product-detail"><table><tr><th>판매가</th><td>1,000원</td></tr>'
        "</table></div></body></html>"
    )
    return CaptureEnvelope.model_validate(
        {
            "supplier_key": key,
            "policy": {"revision": policy.revision, "digest": policy.digest},
            "transport": {
                "url": CAFE24_PRODUCT,
                "navigation_name": CAFE24_PRODUCT,
                "response_status": 200,
                "redirect_count": 0,
                "content_type": "text/html",
                "character_set": "UTF-8",
            },
            "html": html,
        }
    )


def test_collection_and_capture_refuse_a_site_until_it_is_active(
    container: Container, config: AppConfig
) -> None:
    key = cafe24(container)
    catalog = container.supplier_catalog
    for state in (None, OnboardingState.RECON):
        if state is not None:
            container.onboarding.transition(key, state, actor=OPERATOR)
        assert catalog.collectable(key) is False and catalog.site_active(key) is False
        refused(SUPPLIER_NOT_ACTIVE, lambda: container.collection.submit(key, CAFE24_PRODUCT))
        refused(SUPPLIER_NOT_ACTIVE, lambda: container.extension_capture.policy(key))
        refused(
            SUPPLIER_NOT_ACTIVE,
            lambda: container.extension_queues.discovery_policy(key),
        )
        assert key not in container.collection.supplier_keys()
        assert CAFE24_HOST not in container.extension_queues.supplier_hosts()
        # CONNECT still tests it: a site is connected before it is collected (§4 step 4).
        assert container.connect.supplier_connection(key).supplier_key == key
    assert count(config, "collection_runs") == 0 and count(config, "jobs") == 0

    activate(container, config, key)
    assert catalog.collectable(key) is True and catalog.site_active(key) is True
    assert key in container.collection.supplier_keys()
    assert container.extension_queues.supplier_hosts()[CAFE24_HOST] == key
    submitted = container.collection.submit(key, CAFE24_PRODUCT)
    assert container.collection.run(submitted.collection_run_id).supplier_key == key
    accepted = container.extension_capture.ingest(capture(container, key))
    record = container.collection.run(accepted.collection_run_id)
    assert record.supplier_key == key

    # A words change returns the site to RECON: the recording of an accepted capture refuses it
    # before any revision is written (OB-04, OB-10).
    container.onboarding.save_words(key, WORDS, expected_seq=0, actor=OPERATOR)
    document = DocumentView(
        kind=ReadKind.PRODUCT_READ,
        status=200,
        path="/product/sample/123/",
        location=None,
        content_type="text/html",
        body="<html></html>",
    )
    refused(
        SUPPLIER_NOT_ACTIVE,
        lambda: container.collection.record_captured_document(
            record, document, captured_at=datetime(2026, 10, 12, tzinfo=UTC)
        ),
    )
    refused(SUPPLIER_NOT_ACTIVE, lambda: container.collection.submit(key, CAFE24_PRODUCT))
    assert count(config, "product_facts_revisions") == 0


# ---------------------------------------------------------------- hosts and egress


def test_egress_is_exactly_the_confirmed_hosts_and_disabled_withdraws_them(
    container: Container, config: AppConfig
) -> None:
    key = cafe24(container)
    catalog = container.supplier_catalog
    template_hosts = TEMPLATES["cafe24"].platform_egress_hosts
    profile = catalog.definitions[key].profile
    assert profile.egress_hosts == {CAFE24_HOST} | template_hosts
    assert catalog.collections[key].collection.profile.hosts == {CAFE24_HOST}

    refused(
        ONBOARDING_HOST_UNOBSERVED,
        lambda: container.onboarding.confirm_host(
            key, "img.onboard-cdn.com", "image", observed_trial_id=None, actor=OPERATOR
        ),
    )
    trial_id = trial(config, key, ("img.onboard-cdn.com",))
    refused(
        ONBOARDING_HOST_UNOBSERVED,
        lambda: container.onboarding.confirm_host(
            key, "other.onboard-cdn.com", "image", observed_trial_id=trial_id, actor=OPERATOR
        ),
    )
    refused(
        ONBOARDING_HOST_REFUSED,
        lambda: container.onboarding.confirm_host(
            key, "10.0.0.8", "image", observed_trial_id=trial_id, actor=OPERATOR
        ),
    )
    container.onboarding.confirm_host(
        key, "IMG.onboard-cdn.com", HostRole.IMAGE, observed_trial_id=trial_id, actor=OPERATOR
    )
    collection = catalog.collections[key].collection.profile
    assert collection.image_hosts == {"img.onboard-cdn.com"}
    assert collection.hosts == {CAFE24_HOST, "img.onboard-cdn.com"}
    container.onboarding.disable_host(key, "img.onboard-cdn.com", "image", actor=OPERATOR)
    assert catalog.collections[key].collection.profile.hosts == {CAFE24_HOST}
    with pytest.raises(NotFoundError):
        container.onboarding.disable_host(key, "img.onboard-cdn.com", "image", actor=OPERATOR)

    container.onboarding.transition(key, OnboardingState.DISABLED, actor=OPERATOR)
    assert key not in catalog.definitions and key not in catalog.collections
    assert key not in catalog.capture_policies
    assert catalog.site_active(key) is False and catalog.collectable(key) is False
    refused("SUPPLIER_UNKNOWN", lambda: container.connect.supplier_connection(key))
    refused("COLLECT_SUPPLIER_UNKNOWN", lambda: container.collection.submit(key, CAFE24_PRODUCT))
    assert f"supplier:{key}" not in {c.key for c in container.connect.capabilities()}


def test_a_disabled_bootstrap_storefront_unbinds_the_site(container: Container) -> None:
    key = cafe24(container)
    container.onboarding.disable_host(key, CAFE24_HOST, "storefront", actor=OPERATOR)
    entry = container.supplier_catalog.onboarded(key)
    assert entry is not None and entry.bound is None and entry.problems
    assert key not in container.supplier_catalog.definitions
    container.onboarding.confirm_host(
        key, CAFE24_HOST, "storefront", observed_trial_id=None, actor=OPERATOR
    )
    assert key in container.supplier_catalog.definitions


# ---------------------------------------------------------------- repository sites (OB-07)


def test_repository_sites_keep_their_identities_and_their_recon_rule(
    container: Container,
) -> None:
    catalog = container.supplier_catalog
    sites, _ = bind_sites()
    assert {s.config.supplier_key: s.extractor_revision for s in sites} == {
        "ggsan": f"{TEMPLATES['godomall'].revision}+ggsan-3",
        "upick": f"{TEMPLATES['cafe24'].revision}+upick-3",
    }
    expected = {r.supplier_key: r for r in _registered(COLLECTIONS)}
    assert set(expected) == {"kmretail", "upick", "ggsan"}
    for key, registered in expected.items():
        assert catalog.collections[key] == registered, key
        assert catalog.collectable(key) is True
    for key, site in SITES.items():
        assert catalog.site_active(key) is site.config.active
        assert catalog.collections[key].extractor_fingerprint == site.extractor_fingerprint
    assert catalog.site_active("kmretail") is None
    assert catalog.repository_keys == {"kmretail", "upick", "ggsan"}

    # A repository site in RECON still collects (ADR-0030 §7); only PT-06 refuses it.
    recon = SupplierCatalog(
        definitions=[catalog.definitions["upick"]],
        collections=[catalog.collections["upick"]],
        capture_policies={},
        repository_active=lambda key: False if key == "upick" else None,
    )
    assert recon.collectable("upick") is True and recon.site_active("upick") is False
