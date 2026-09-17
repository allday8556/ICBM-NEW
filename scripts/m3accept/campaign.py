"""Running ``m3-accept-01``: two fresh-session passes and a closeout (ruling 5711123764 §3, §5).

A pass is one invocation, in one fresh application process, on the campaign's own data directory:

1. PASS-B first asks the durable pacing store whether the product may be read yet. If not, it
   returns ``WAITING_FOR_PACING`` with the seconds that remain — nothing is reserved, nothing is
   submitted, and nothing loops. The operator runs the pass again when the time has passed.
2. The pass is entered in the ledger with a session nonce of its own; PASS-B refuses the nonce
   PASS-A ran under.
3. A fresh application is composed on the campaign data directory. Its collection gateway and its
   CONNECT gateway are the production ones, each behind the campaign ledger; its session comes from
   the M1 connection owner, reached only if the campaign budgeted that owner's session proof.
4. The operator's one URL is submitted to the **production** ``ProductCollectionService``, and the
   production ``JobRunner`` runs the durable ``collect.product`` job once. If the job owner
   schedules
   a retry, the pass reports ``IN_PROGRESS`` and the next invocation resumes that same run —
   the harness never retries anything itself.
5. When the run is terminal, the pass is judged from durable state alone — the run row, the
   revision as the read-back returns it, and the ledger's own reservations and refusals.

Nothing here parses a document, classifies an image, hashes a byte or appends a revision.
"""

import json
import secrets
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.collect.collection import (
    CollectionGateway,
    RegisteredCollection,
    SessionProvider,
    pacing_key,
)
from app.collect.contracts import RevisionView
from app.collect.facts import FactsStatus, FieldLevel, FieldStatus, ImageIssue, ImageRole
from app.collect.models import CollectionOutcome
from app.collect.runs import CollectionRunRecord, CollectionRunStore
from app.config import AppConfig
from app.container import Container, build_container
from app.core.clock import Clock
from app.core.errors import AppError
from app.core.ownership import acquire_data_dir
from app.core.secrets import SecretStore
from app.jobs.models import JobState
from integrations.suppliers.base import SupplierDefinition, SupplierGateway
from integrations.suppliers.collection import SupplierCollection
from scripts.m3accept.gateways import (
    CampaignSessions,
    LedgeredCollectionGateway,
    LedgeredConnectGateway,
)
from scripts.m3accept.ledger import CampaignLedger, State, Submission
from scripts.m3accept.m1 import m1_session_problems
from scripts.m3accept.manifest import (
    HARD_ZERO,
    CampaignBudget,
    RequestClass,
)

# The capability boundary of docs/acceptance/M3.md §2, restated into every closeout unchanged.
CAPABILITY_BOUNDARY = (
    "Positive CONFIRMED support for option axes/configurations is NOT evidence-backed and NOT "
    "accepted; final M3 acceptance must not claim it.",
    "Positive CONFIRMED support for quantity-tier values/source totals is NOT evidence-backed and "
    "NOT accepted; final M3 acceptance must not claim it.",
)
_CAMPAIGN_STOP_CODES = ("M3_ACCEPT_",)


@dataclass
class _LateOwner:
    """The M1 connection owner, known only once the fresh application exists."""

    owner: SessionProvider | None = None

    def collection_session(self, supplier_key: str, *, operator_initiated: bool = False) -> bytes:
        if self.owner is None:
            raise RuntimeError("the M1 owner is bound after the application is composed")
        return self.owner.collection_session(supplier_key, operator_initiated=operator_initiated)


@dataclass
class Environment:
    """What a fresh session is composed of. REAL builds the production transports; tests fake them.

    A *fresh session* is a fresh application composition — its own ownership lease, database
    engine, job runner and service graph — on the campaign's one dedicated data directory, loading
    the M1 connection owner that directory already holds. It is never a new supplier login.
    """

    config: AppConfig
    supplier_key: str
    collection: SupplierCollection
    collection_transport: Callable[[], CollectionGateway]
    connect_transport: Callable[[], SupplierGateway]
    secret_store: SecretStore | None = None
    clock: Clock | None = None
    registered: Sequence[RegisteredCollection] | None = None
    suppliers: Sequence[SupplierDefinition] | None = None


@dataclass(frozen=True)
class PassOutcome:
    status: str  # WAITING_FOR_PACING | IN_PROGRESS | ACCEPTED | HOLD | STOPPED
    pass_id: str
    seconds_remaining: float = 0.0
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    verdict: str  # ACCEPTED | HOLD | STOPPED
    reasons: tuple[str, ...]
    observations: tuple[str, ...]


@contextmanager
def fresh_session(
    env: Environment, ledger: CampaignLedger, pass_id: str
) -> Iterator[tuple[Container, str]]:
    """Compose one fresh application on the campaign data directory, behind the ledger."""
    owner = _LateOwner()
    sessions = CampaignSessions(owner)
    kwargs: dict[str, Any] = {
        "collection_gateway": LedgeredCollectionGateway(
            env.collection_transport(), ledger, pass_id
        ),
        "supplier_gateway": LedgeredConnectGateway(env.connect_transport(), ledger, pass_id),
        "collection_sessions": sessions,
    }
    if env.secret_store is not None:
        kwargs["secret_store"] = env.secret_store
    if env.clock is not None:
        kwargs["clock"] = env.clock
    if env.registered is not None:
        kwargs["collections"] = env.registered
    if env.suppliers is not None:
        kwargs["suppliers"] = env.suppliers
    nonce = secrets.token_hex(16)
    with acquire_data_dir(env.config.data_dir, app_version=f"m3-accept-{nonce[:8]}") as lease:
        container = build_container(env.config, ownership=lease, **kwargs)
        owner.owner = container.connect
        try:
            yield container, nonce
        finally:
            container.db.dispose()


def seconds_until_eligible(env: Environment, container: Container, product_url: str) -> float:
    """What the durable pacing store says, asked read-only. Nothing is reserved."""
    runs = CollectionRunStore(container.db, container.clock)
    return runs.seconds_until_readable(
        pacing_key(env.collection, product_url),
        interval_s=env.collection.profile.limits.same_product_interval_s,
    )


def run_pass(
    ledger: CampaignLedger, env: Environment, pass_id: str, product_url: str
) -> PassOutcome:
    """Advance one pass by exactly one step in a fresh session. Never loops, never retries."""
    state = ledger.state()
    if state in (State.COMPLETED, State.HOLD, State.STOPPED, State.CLOSEOUT_READY):
        raise RuntimeError(f"the campaign is {state.value}; no pass runs")
    budget = _budget_from(ledger)

    with fresh_session(env, ledger, pass_id) as (container, nonce):
        running = state.value == f"PASS_{pass_id}_RUNNING"
        if not running:
            # Pre-pass PREP, local only: the M1 owner must still hold a usable stored session.
            # Whether it is live is the budgeted proof inside the pass, not this check.
            if problems := m1_session_problems(container, env.supplier_key):
                ledger.stop("M1_SESSION_NOT_LOADABLE")
                return PassOutcome("STOPPED", pass_id, detail={"reasons": problems})
            if pass_id == "B":
                remaining = seconds_until_eligible(env, container, product_url)
                if remaining > 0:
                    # An explicit state, not a loop: nothing reserved, nothing submitted.
                    return PassOutcome("WAITING_FOR_PACING", pass_id, seconds_remaining=remaining)
            ledger.begin_pass(pass_id, session_nonce=nonce)

        submission = ledger.submission(pass_id)
        if submission is None:
            try:
                submitted = container.collection.submit(env.supplier_key, product_url)
            except AppError as refused:
                if refused.code == "COLLECT_SAME_PRODUCT_TOO_SOON":
                    remaining = seconds_until_eligible(env, container, product_url)
                    return PassOutcome("WAITING_FOR_PACING", pass_id, seconds_remaining=remaining)
                raise
            submission = Submission(
                pass_id=pass_id,
                session_nonce=nonce,
                collection_run_id=submitted.collection_run_id,
                job_id=submitted.job_id,
                correlation_id=submitted.correlation_id,
            )
            ledger.record_submission(submission)

        # The production job owner runs the job once. A scheduled retry is its business.
        container.runner.run_next()
        job = container.jobs.get(submission.job_id)
        if job.state not in (JobState.SUCCEEDED, JobState.DEAD):
            return PassOutcome(
                "IN_PROGRESS",
                pass_id,
                detail={
                    "job_state": str(job.state),
                    "next_attempt_at": None
                    if job.next_attempt_at is None
                    else job.next_attempt_at.isoformat(),
                    "last_error_code": job.last_error_code,
                },
            )

        run = container.collection.run(submission.collection_run_id)
        revision = (
            None if run.revision_id is None else container.source_truth.revision(run.revision_id)
        )
        verdict = classify_pass(
            run=run,
            revision=revision,
            counts=ledger.counts(pass_id),
            refusals=[r for r in ledger.refusals() if r["pass"] == pass_id],
            budget=budget,
        )
        detail = {
            "collection_run_id": run.collection_run_id,
            "job_id": run.job_id,
            "correlation_id": run.correlation_id,
            "outcome": run.outcome.value,
            "revision_id": run.revision_id,
            "facts_status": None if run.facts_status is None else run.facts_status.value,
            "source_product_id": None if revision is None else revision.source_product_id,
            "run_detail": run.detail,
            "reasons": list(verdict.reasons),
            "observations": list(verdict.observations),
            "requests": ledger.counts(pass_id),
            "references": _reference_summary(revision),
        }
        ledger.finish_pass(pass_id, verdict=verdict.verdict, detail=detail)
        return PassOutcome(verdict.verdict, pass_id, detail=detail)


def classify_pass(
    *,
    run: CollectionRunRecord,
    revision: RevisionView | None,
    counts: Mapping[str, int],
    refusals: Sequence[Mapping[str, Any]],
    budget: CampaignBudget,
) -> Verdict:
    """Judge a finished pass from durable state alone (ruling 5711123764 §5, 5711187191 §2–§3)."""
    reasons: list[str] = []
    observations: list[str] = []

    if run.outcome is not CollectionOutcome.RECORDED:
        detail = run.detail or ""
        # A run the campaign itself refused is a stop, whatever code the production owner then
        # surfaced for it: the M1 owner, for one, reports a refused login as its own auth failure.
        stopped = bool(refusals) or any(detail.startswith(code) for code in _CAMPAIGN_STOP_CODES)
        if run.outcome is CollectionOutcome.NO_REVISION:
            reasons.append("IDENTITY_UNRESOLVED")
        else:
            reasons.append(f"RUN_{run.outcome.value}:{detail}")
        return Verdict("STOPPED" if stopped else "HOLD", tuple(reasons), ())

    if revision is None or run.revision_id != revision.revision_id:
        return Verdict("HOLD", ("REVISION_NOT_READ_BACK",), ())
    if not revision.source_product_id:
        reasons.append("IDENTITY_UNRESOLVED")
    if not revision.fingerprints_intact:
        reasons.append("FINGERPRINTS_NOT_INTACT")

    core_under_review = [
        field.key
        for field in revision.fields
        if field.level is FieldLevel.CORE and field.status is FieldStatus.REVIEW_REQUIRED
    ]
    if core_under_review:
        reasons.append("CORE_FACT_AMBIGUOUS:" + ",".join(sorted(core_under_review)))

    references = revision.images
    baseline = budget.product_evidence_baseline
    image_ceiling_hit = any(
        r["class"] == RequestClass.IMAGE_REQUEST.value
        and r["reason"] in ("PASS_CEILING", "CAMPAIGN_CEILING")
        for r in refusals
    )
    if len(references) > baseline or image_ceiling_hit:
        # More product evidence than the frozen baseline: the reference past it was never fetched,
        # and the budget is not widened to reach it.
        reasons.append(f"DRIFT_OVER_BASELINE:{len(references)}>{baseline}")
    elif len(references) < baseline:
        observations.append(f"SOURCE_DRIFT_UNDER_BASELINE:{len(references)}<{baseline}")
        confirmed_roles = {r.role for r in references if r.status is FieldStatus.CONFIRMED}
        missing = [
            role.value
            for role in (ImageRole.REPRESENTATIVE, ImageRole.DETAIL)
            if role not in confirmed_roles
        ]
        if missing:
            reasons.append("REQUIRED_EVIDENCE_MISSING:" + ",".join(missing))

    exhausted = [r for r in references if r.issue is ImageIssue.BUDGET_EXHAUSTED]
    if exhausted and not image_ceiling_hit:
        # The production path refused a body that would cross the pass's byte total. The revision
        # is a safe incomplete one; the pass is not an acceptance.
        reasons.append(f"BYTE_CAP_REACHED:{len(exhausted)}")
    unresolved = [
        r
        for r in references
        if r.status is FieldStatus.REVIEW_REQUIRED and r.issue is not ImageIssue.BUDGET_EXHAUSTED
    ]
    if unresolved:
        reasons.append(
            "IMAGE_REFERENCE_UNRESOLVED:"
            + ",".join(sorted({str(r.issue.value if r.issue else "-") for r in unresolved}))
        )
    for request in RequestClass:
        # The ledger already refuses anything over the armed ceilings. This asks the same question
        # of the durable counts a second time, so a pass is never accepted on trust.
        if counts.get(request.value, 0) > budget.ceiling(request).per_pass:
            reasons.append(f"OVER_CEILING:{request.value}")

    return Verdict("HOLD" if reasons else "ACCEPTED", tuple(reasons), tuple(observations))


def closeout(ledger: CampaignLedger, env: Environment) -> dict[str, Any]:
    """Compare the two passes from durable read-back and complete the campaign."""
    if ledger.state() is not State.CLOSEOUT_READY:
        raise RuntimeError("closeout needs both passes accepted")
    a, b = ledger.result("A"), ledger.result("B")
    assert a is not None and b is not None
    with fresh_session(env, ledger, "B") as (container, _):
        runs = {
            p: container.collection.run(r["detail"]["collection_run_id"])
            for p, r in (("A", a), ("B", b))
        }
        revisions = {
            p: container.source_truth.revision(run.revision_id)
            for p, run in runs.items()
            if run.revision_id is not None
        }
        history = container.source_truth.history(
            env.supplier_key, revisions["A"].source_product_id
        ).revisions
    problems = []
    if revisions["A"].revision_id == revisions["B"].revision_id:
        problems.append("R1_EQUALS_R2")
    if revisions["A"].source_product_id != revisions["B"].source_product_id:
        problems.append("IDENTITY_CHANGED_BETWEEN_PASSES")
    known = {r.revision_id for r in history}
    if not {revisions["A"].revision_id, revisions["B"].revision_id} <= known:
        problems.append("HISTORY_MISSING_A_REVISION")
    manifest = ledger.manifest() or {}
    report: dict[str, Any] = {
        "campaign_id": manifest.get("campaign_id"),
        "code_sha": manifest.get("code_sha"),
        "manifest_digest": _manifest_digest(ledger),
        "target_digest": manifest.get("target_digest"),
        "passes": {p: r["detail"] for p, r in (("A", a), ("B", b))},
        "revisions_distinct": revisions["A"].revision_id != revisions["B"].revision_id,
        "same_source_identity": revisions["A"].source_product_id
        == revisions["B"].source_product_id,
        "source_fingerprint_equal": revisions["A"].source_fingerprint
        == revisions["B"].source_fingerprint,
        "history_revisions": len(history),
        "requests": ledger.counts(),
        "refusals": ledger.refusals(),
        "hard_zero": dict.fromkeys(HARD_ZERO, 0),
        "capability_boundary": list(CAPABILITY_BOUNDARY),
        "problems": problems,
    }
    if problems:
        ledger.stop("CLOSEOUT_" + "_".join(problems))
        return report
    digest = _report_digest(report)
    ledger.complete(digest)
    report["report_digest"] = digest
    return report


# ---------------------------------------------------------------- helpers


def _reference_summary(revision: RevisionView | None) -> dict[str, Any]:
    if revision is None:
        return {"count": 0}
    roles: dict[str, int] = {}
    issues: dict[str, int] = {}
    for reference in revision.images:
        roles[reference.role.value] = roles.get(reference.role.value, 0) + 1
        if reference.issue is not None:
            issues[reference.issue.value] = issues.get(reference.issue.value, 0) + 1
    return {
        "count": len(revision.images),
        "roles": roles,
        "issues": issues,
        "order": [f"{r.role.value}:{r.ordinal}" for r in revision.images],
        "stored_bytes": sum(r.asset.byte_size for r in revision.images if r.asset is not None),
        "facts_status": revision.facts_status.value,
        "confirmed": revision.facts_status is FactsStatus.CONFIRMED,
    }


def _budget_from(ledger: CampaignLedger) -> CampaignBudget:
    from types import MappingProxyType

    from scripts.m3accept.manifest import Ceiling

    manifest = ledger.manifest()
    if manifest is None:
        raise RuntimeError("the campaign is not armed")
    body = manifest["budget"]
    return CampaignBudget(
        ceilings=MappingProxyType(
            {
                RequestClass(name): Ceiling(value["per_pass"], value["campaign"])
                for name, value in body["ceilings"].items()
            }
        ),
        per_image_bytes=body["per_image_bytes"],
        new_image_bytes_per_pass=body["new_image_bytes_per_pass"],
        same_product_interval_s=body["same_product_interval_s"],
        product_evidence_baseline=body["product_evidence_baseline"],
    )


def _manifest_digest(ledger: CampaignLedger) -> str | None:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(ledger.path)) as db:
        row = db.execute("SELECT manifest_digest FROM manifest").fetchone()
    return None if row is None else str(row[0])


def _report_digest(report: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(report, sort_keys=True, indent=2, default=str), encoding="utf-8")
