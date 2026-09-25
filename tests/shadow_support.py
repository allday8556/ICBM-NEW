"""Synthetic support for the Adaptive shadow tests (ADR-0017 §10–§11; P3).

The fake shop of ``scripts.m3collect.fake_shop`` is the supplier, admitted by an injected gate. Its
EPR is an ordinary stored profile; it is made VALIDATED with a recorded PASS run for its exact
freshness (the validation checks themselves are P1/P2's and are proven there). Nothing here is a
real page, account, person or product, and nothing reaches a network.
"""

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.collect.adaptive.validation import ValidationRun, Verdict, freshness_tuple
from app.collect.adaptive_shadow.switch import bundle_key_of
from app.collect.adaptive_store.gate import build_supplier_gate
from app.collect.collection import COLLECT_PRODUCT_JOB, RegisteredCollection
from app.config import AppConfig
from app.container import Container, build_container
from app.core.ownership import acquire_data_dir
from app.jobs.registry import JobContext
from scripts.m3collect.fake_shop import (
    DETAIL_BYTES,
    DETAIL_URL,
    EXTRACTOR_FINGERPRINT,
    EXTRACTOR_REVISION,
    PRIMARY_BYTES,
    PRIMARY_URL,
    SUPPLIER_KEY,
    FakeGateway,
    StubSessions,
    collection,
    page,
)
from tests.adaptive_support import epr, template
from tests.support import FakeClock

OPERATOR = "operator:synthetic"
CORRELATION = "corr-shadow-p3"
INTERVAL = collection().profile.limits.same_product_interval_s


def registered() -> RegisteredCollection:
    return RegisteredCollection(
        collection=collection(),
        extractor_revision=EXTRACTOR_REVISION,
        extractor_fingerprint=EXTRACTOR_FINGERPRINT,
    )


def gateway() -> FakeGateway:
    return FakeGateway(
        documents=[page()], images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES}
    )


@contextlib.contextmanager
def container(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, *, admit: bool = True
) -> Iterator[Container]:
    """One application process over the data directory; each entry is a fresh process.

    ``admit`` admits the fake shop through an injected gate. Without it the container builds its
    production gate from its own registered CONNECT definitions and COLLECT envelopes.
    """
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            collection_gateway=shop,
            collection_sessions=StubSessions(),
            collections=(registered(),),
            adaptive_supplier_gate=build_supplier_gate({SUPPLIER_KEY}) if admit else None,
        )
        try:
            yield built
        finally:
            built.db.dispose()


def save_profile(app: Container, key: str = "plain", **overrides: Any) -> str:
    """A stored fake-shop EPR with one template of its own; returns its digest."""
    ptr = app.adaptive_profiles.save_template(
        template(key, choice=False, supplier=SUPPLIER_KEY),
        created_by=OPERATOR,
        correlation_id=CORRELATION,
    )
    return app.adaptive_profiles.save_draft(
        epr([ptr], supplier=SUPPLIER_KEY, **overrides),
        created_by=OPERATOR,
        correlation_id=CORRELATION,
    )


def validate(app: Container, epr_digest: str) -> tuple[str, ...]:
    """Record a PASS run for the EPR's exact current freshness; returns that freshness."""
    bundle = app.adaptive_profiles.load_bundle(epr_digest)
    freshness = freshness_tuple(bundle, [], None)
    app.adaptive_validation.record_run(
        bundle,
        ValidationRun(Verdict.PASS, (), freshness),
        [],
        recorded_by=OPERATOR,
        correlation_id=CORRELATION,
    )
    return freshness


def enabled_profile(app: Container, key: str = "plain", **overrides: Any) -> tuple[str, str]:
    """A VALIDATED fake-shop EPR with the shadow switch on for it: (epr digest, bundle key)."""
    digest = save_profile(app, key, **overrides)
    app.shadow_switch.enable(
        digest,
        validate(app, digest),
        actor=OPERATOR,
        reason="SYNTHETIC",
        correlation_id=CORRELATION,
    )
    return digest, bundle_key_of(digest)


def collect_once(app: Container, clock: FakeClock, product_url: str) -> str:
    """Submit one collection and run its job; returns the collection run id."""
    submitted = app.collection.submit(SUPPLIER_KEY, product_url)
    assert app.runner.run_next() is not None, "the collection job did not run"
    clock.advance(INTERVAL + 1)
    return submitted.collection_run_id


def job_context(app: Container, run_id: str, attempt_no: int) -> JobContext:
    run = app.collection.run(run_id)
    return JobContext(
        job_id=run.job_id,
        job_type=COLLECT_PRODUCT_JOB,
        attempt_no=attempt_no,
        max_attempts=3,
        correlation_id=run.correlation_id,
        target_ref=SUPPLIER_KEY,
        payload={},
    )


def raw(config: AppConfig) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(config.data_dir) / "runtime" / "icbm.db")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def rows(config: AppConfig, sql: str, *args: object) -> list[tuple[Any, ...]]:
    with contextlib.closing(raw(config)) as connection:
        return connection.execute(sql, args).fetchall()


def count(config: AppConfig, table: str) -> int:
    return int(rows(config, f"SELECT COUNT(*) FROM {table}")[0][0])


def canonical_snapshot(config: AppConfig) -> dict[str, Any]:
    """Everything canonical a collection leaves behind, without identifiers that differ by run."""
    return {
        "runs": rows(
            config,
            "SELECT outcome, facts_status, detail, source_product_id FROM collection_runs"
            " ORDER BY requested_at",
        ),
        "revisions": rows(
            config,
            "SELECT supplier_key, source_product_id, sequence, facts_status, source_fingerprint"
            " FROM product_facts_revisions ORDER BY sequence",
        ),
        "fields": rows(
            config,
            "SELECT field_key, status, value_json, field_fingerprint FROM product_facts_fields"
            " ORDER BY field_key",
        ),
        "jobs": rows(config, "SELECT job_type, state, attempt_count FROM jobs ORDER BY rowid"),
        "audit": rows(
            config, "SELECT event_type, action, outcome, reason_code FROM audit_events ORDER BY seq"
        ),
        "review_items": count(config, "review_items"),
        "pointers": rows(
            config,
            "SELECT sequence, reason FROM current_source_revision_moves ORDER BY sequence",
        ),
    }


def comparison_json(config: AppConfig, run_id: str) -> dict[str, Any]:
    (text,) = rows(
        config,
        "SELECT comparison_json FROM adaptive_shadow_records WHERE collection_run_id = ?",
        run_id,
    )[0]
    loaded: dict[str, Any] = json.loads(text)
    return loaded
