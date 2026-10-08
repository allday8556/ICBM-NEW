"""The structured enrichment result, owned by PRODUCT DB (ADR-0026 §5; AIF-3).

ARCHITECTURE §3: PRODUCT DB owns enrichment proposals. A result is a proposal row of its own and
is never written into a source fact or a product field (AIF-01).

**Request.** An enrichment request names one canonical product, its tasks and an optional target.
- With no AI provider it is refused before anything is written (AIF-08).
- A task whose current results were computed from exactly today's inputs is reused: no call,
  no job (Canonical §7.2).
- Every other task goes to one ``enrich.tasks`` job, never inline and never from COLLECT.

**Fingerprint.** It is ADR-0012 §6's, computed before the call over:
- the relevant facts: exactly the fields the task declares, by value. The revisions they were read
  from are recorded with the result but are not fingerprinted, so a new revision whose relevant
  fields are unchanged leaves the result fresh (dependency-scoped staleness, Issue #30);
- the prompt and policy versions;
- the requested identity;
- the task's schema version.

**Result keys.** A task's output names its result keys, for example the bundle task's
``product_name``, ``tags``, ``category`` and ``options``. Each key is recorded as its own state
(ADR-0026 §3.1), and a retry re-runs only what is not fresh.

**Staleness.** It is derived on read, never stored: a result is stale when today's inputs differ
from its own, with the reason naming what changed.

A task's definition (its result keys, fact dependencies and schema version) is code that lands
with its own stage. No production task is defined here, so every production request ends at the
provider check (no provider) or at ``AI_TASK_NOT_RUNNABLE``.
"""

import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.capabilities.ai.composer import ComposedRequest, PromptComposer
from app.capabilities.ai.execution import AIExecution, Execution
from app.capabilities.ai.provider import RequestedIdentity, not_configured
from app.capabilities.ai.registry import POLICY_BY_MARKETPLACE
from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.capabilities.jobs.registry import JobContext, JobDefinition
from app.capabilities.jobs.service import JobService
from app.platform.core.clock import Clock
from app.platform.core.errors import (
    ErrorClass,
    InputValidationError,
    NotFoundError,
    RateLimitedError,
    TransientError,
)
from app.platform.db.database import Database
from app.stages.connect.accounts import MarketplaceAccountStore
from app.stages.products.enrichment_models import ProductEnrichmentResult
from app.stages.products.store import ProductFoundationStore

ENRICH_JOB: Final = "enrich.tasks"
SINGLE_RESULT: Final = "result"

AI_TASK_NOT_RUNNABLE: Final = "AI_TASK_NOT_RUNNABLE"
AI_ENRICHMENT_REQUEST_INVALID: Final = "AI_ENRICHMENT_REQUEST_INVALID"
AI_TARGET_UNKNOWN: Final = "AI_TARGET_UNKNOWN"
AI_TARGET_HAS_NO_POLICY: Final = "AI_TARGET_HAS_NO_POLICY"
AI_OUTPUT_FIELD_MISSING: Final = "AI_OUTPUT_FIELD_MISSING"
AI_OUTPUT_SCHEMA_INVALID: Final = "AI_OUTPUT_SCHEMA_INVALID"
# The structured envelope every result object carries (ADR-0026 §5, Issue #30 refinement):
# its evidence, its confidence in 0..1 and whether it requires review.
ENVELOPE: Final = ("evidence", "confidence", "requires_review")
PRODUCTS_PRODUCT_UNKNOWN: Final = "PRODUCTS_PRODUCT_UNKNOWN"

# The parts of a fingerprint's inputs, in the order a stale reason names them.
REASONS: Final = ("facts", "prompt", "policy", "provider", "schema")


@dataclass(frozen=True)
class ResultSchema:
    """The output schema of one result object: the value fields it must carry, besides the
    envelope every result carries (``ENVELOPE``)."""

    required: tuple[str, ...]


@dataclass(frozen=True)
class TaskDefinition:
    """One runnable task: its registry task, its output schema by result key, the fact fields it
    depends on, and the version of its output schema.

    Each result key names one object of the output, recorded as its own state. The single key
    ``result`` means the whole output is the one result object."""

    task_key: str
    results: Mapping[str, ResultSchema]
    fact_fields: tuple[str, ...]
    schema_version: str

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self.results)


@dataclass(frozen=True)
class Target:
    marketplace_key: str
    marketplace_account_id: str


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def subject_key(
    product_group_id: str, task_key: str, result_key: str, target: Target | None
) -> str:
    return "|".join(
        (
            product_group_id,
            task_key,
            result_key,
            target.marketplace_key if target else "",
            target.marketplace_account_id if target else "",
        )
    )


# ------------------------------------------------------------------ the contract


class TargetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marketplace_key: StrictStr = Field(min_length=1, max_length=40)
    marketplace_account_id: StrictStr = Field(min_length=1, max_length=40)


class EnrichmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: StrictStr = Field(min_length=1, max_length=64)
    tasks: list[StrictStr] = Field(min_length=1, max_length=16)
    target: TargetView | None = None


class TaskDecision(BaseModel):
    task_key: str
    decision: str  # REUSED | QUEUED
    input_fingerprint: str


class RequestView(BaseModel):
    product_group_id: str
    job_id: str | None
    tasks: list[TaskDecision]


class ResultView(BaseModel):
    task_key: str
    result_key: str
    target: TargetView | None
    sequence: int
    status: str
    value: dict[str, Any] | None
    evidence: Any
    confidence: float | None
    requires_review: bool | None
    error_class: str | None
    error_code: str | None
    input_fingerprint: str
    enrichment_schema_version: str
    provenance: dict[str, Any]
    recorded_at: datetime
    # Derived on read: today's inputs differ from the result's own, and what changed.
    stale: bool
    stale_reasons: list[str]


class ResultsView(BaseModel):
    product_group_id: str
    ai_configured: bool
    results: list[ResultView]


# ------------------------------------------------------------------ the store


class EnrichmentStore:
    """The only production writer of ``product_enrichment_results``."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def current(self, session: Session, subject: str) -> ProductEnrichmentResult | None:
        return session.scalars(
            select(ProductEnrichmentResult)
            .where(ProductEnrichmentResult.subject_key == subject)
            .order_by(ProductEnrichmentResult.sequence.desc())
            .limit(1)
        ).one_or_none()

    def currents(self, product_group_id: str) -> list[ProductEnrichmentResult]:
        with self._db.read() as session:
            newest = (
                select(
                    ProductEnrichmentResult.subject_key,
                    func.max(ProductEnrichmentResult.sequence).label("sequence"),
                )
                .where(ProductEnrichmentResult.product_group_id == product_group_id)
                .group_by(ProductEnrichmentResult.subject_key)
                .subquery()
            )
            return list(
                session.scalars(
                    select(ProductEnrichmentResult)
                    .join(
                        newest,
                        (ProductEnrichmentResult.subject_key == newest.c.subject_key)
                        & (ProductEnrichmentResult.sequence == newest.c.sequence),
                    )
                    .order_by(ProductEnrichmentResult.subject_key)
                ).all()
            )

    def record(self, session: Session, row: ProductEnrichmentResult) -> ProductEnrichmentResult:
        row.sequence = 1 + int(
            session.scalar(
                select(func.coalesce(func.max(ProductEnrichmentResult.sequence), 0)).where(
                    ProductEnrichmentResult.subject_key == row.subject_key
                )
            )
            or 0
        )
        session.add(row)
        session.flush()
        return row


# ------------------------------------------------------------------ the owner


@dataclass(frozen=True)
class _Inputs:
    composed: ComposedRequest
    inputs: Mapping[str, Any]
    fingerprint: str


class EnrichmentService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        jobs: JobService,
        products: ProductFoundationStore,
        accounts: MarketplaceAccountStore,
        composer: PromptComposer,
        execution: AIExecution,
        tasks: Sequence[TaskDefinition] = (),
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._jobs = jobs
        self._products = products
        self._accounts = accounts
        self._composer = composer
        self._execution = execution
        self._store = EnrichmentStore(db)
        self._tasks = {task.task_key: task for task in tasks}

    def job_definition(self) -> JobDefinition:
        return JobDefinition(
            job_type=ENRICH_JOB,
            handler=self._run,
            description="Run a product's enrichment tasks through the AI provider port",
            # A provider call can cost money, so an interrupted attempt is never re-run blindly.
            idempotent=False,
        )

    # -------------------------------------------------------------- request

    def request(
        self, product_group_id: str, request: EnrichmentRequest, *, correlation_id: str
    ) -> RequestView:
        identity = self._execution.identity()
        if identity is None:
            raise not_configured()
        self._require_product(product_group_id)
        if len(set(request.tasks)) != len(request.tasks):
            raise InputValidationError(
                AI_ENRICHMENT_REQUEST_INVALID, "a task is named more than once"
            )
        tasks = [self._task(key) for key in request.tasks]
        target = self._target(request.target)
        decisions: list[TaskDecision] = []
        queued: list[str] = []
        with self._db.read() as session:
            for task in tasks:
                inputs = self._inputs(task, product_group_id, target, identity)
                fresh = all(
                    self._fresh(session, product_group_id, task, key, target, inputs.fingerprint)
                    for key in task.keys
                )
                decisions.append(
                    TaskDecision(
                        task_key=task.task_key,
                        decision="REUSED" if fresh else "QUEUED",
                        input_fingerprint=inputs.fingerprint,
                    )
                )
                if not fresh:
                    queued.append(task.task_key)
        job_id = None
        if queued:
            job = self._jobs.enqueue(
                ENRICH_JOB,
                payload={
                    "product_group_id": product_group_id,
                    "tasks": queued,
                    "target": None if target is None else asdict(target),
                    "actor": request.actor,
                },
                target_ref=f"product:{product_group_id}",
            )
            job_id = job.job_id
        return RequestView(product_group_id=product_group_id, job_id=job_id, tasks=decisions)

    # -------------------------------------------------------------- the job

    def _run(self, ctx: JobContext) -> None:
        identity = self._execution.identity()
        if identity is None:
            raise not_configured()
        product_group_id = str(ctx.payload["product_group_id"])
        raw_target = ctx.payload.get("target")
        target = None if raw_target is None else Target(**raw_target)
        self._require_product(product_group_id)
        retry: Execution | None = None
        for task_key in ctx.payload["tasks"]:
            task = self._task(str(task_key))
            inputs = self._inputs(task, product_group_id, target, identity)
            with self._db.read() as session:
                # A key this job already recorded, OK or FAILED, is settled: a retry of the job
                # (only ever caused by a TRANSIENT or RATE_LIMITED task) never calls it again
                # (ADR-0012 §8).
                needed = [
                    key
                    for key in task.keys
                    if not self._fresh(
                        session, product_group_id, task, key, target, inputs.fingerprint
                    )
                    and not self._settled_by(session, product_group_id, task, key, target, ctx)
                ]
            if not needed:
                continue
            execution = self._execution.run(inputs.composed, correlation_id=ctx.correlation_id)
            if execution.retryable and ctx.attempt_no < ctx.max_attempts:
                # Nothing is recorded for an attempt that will be retried; the tasks already
                # recorded are fresh and are skipped next time.
                retry = execution
                continue
            self._record(ctx, product_group_id, task, needed, target, inputs, execution)
        if retry is not None:
            cls = (
                RateLimitedError
                if retry.outcome.error_class is ErrorClass.RATE_LIMITED
                else TransientError
            )
            raise cls(
                retry.outcome.error_code or "AI_PROVIDER_RETRY", "the AI provider asked to retry"
            )

    def _record(
        self,
        ctx: JobContext,
        product_group_id: str,
        task: TaskDefinition,
        keys: Sequence[str],
        target: Target | None,
        inputs: _Inputs,
        execution: Execution,
    ) -> None:
        outcome = execution.outcome
        provenance = asdict(outcome.provenance)
        now = self._clock.now()
        statuses: dict[str, str] = {}
        with self._db.write() as session:
            for key in keys:
                row = ProductEnrichmentResult(
                    result_id=str(uuid.uuid4()),
                    subject_key=subject_key(product_group_id, task.task_key, key, target),
                    product_group_id=product_group_id,
                    task_key=task.task_key,
                    result_key=key,
                    marketplace_key=target.marketplace_key if target else None,
                    marketplace_account_id=target.marketplace_account_id if target else None,
                    input_fingerprint=inputs.fingerprint,
                    inputs_json=_canonical(inputs.inputs),
                    enrichment_schema_version=task.schema_version,
                    provenance_json=_canonical(provenance),
                    job_id=ctx.job_id,
                    correlation_id=ctx.correlation_id,
                    recorded_at=now,
                    **_status_fields(task, key, outcome.ok, outcome.value, outcome),
                )
                self._store.record(session, row)
                statuses[key] = row.status
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.AI_ENRICHMENT_RESULT_RECORDED,
                    action="AI_ENRICHMENT_RESULT_RECORDED",
                    actor="system:enrich",
                    outcome=AuditOutcome.RECORDED,
                    target_ref=product_group_id,
                    details={
                        "task_key": task.task_key,
                        "results": statuses,
                        "input_fingerprint": inputs.fingerprint,
                        "requested_model": provenance["requested_model"],
                        "actual_model": provenance["actual_model"],
                        "billing_mode": provenance["billing_mode"],
                        "error_code": outcome.error_code,
                        "requested_by": ctx.payload.get("actor"),
                    },
                    correlation_id=ctx.correlation_id,
                ),
                session=session,
            )

    # -------------------------------------------------------------- read

    def results(self, product_group_id: str) -> ResultsView:
        self._require_product(product_group_id)
        identity = self._execution.identity()
        views = []
        inputs_cache: dict[tuple[str, Target | None], _Inputs | None] = {}
        for row in self._store.currents(product_group_id):
            target = (
                Target(row.marketplace_key, row.marketplace_account_id)
                if row.marketplace_key and row.marketplace_account_id
                else None
            )
            task = self._tasks.get(row.task_key)
            cache_key = (row.task_key, target)
            if cache_key not in inputs_cache:
                inputs_cache[cache_key] = (
                    None
                    if task is None or identity is None
                    else self._inputs(task, product_group_id, target, identity)
                )
            now = inputs_cache[cache_key]
            reasons = _stale_reasons(json.loads(row.inputs_json), now, task is None)
            views.append(
                ResultView(
                    task_key=row.task_key,
                    result_key=row.result_key,
                    target=None if target is None else TargetView(**asdict(target)),
                    sequence=row.sequence,
                    status=row.status,
                    value=None if row.value_json is None else json.loads(row.value_json),
                    evidence=None if row.evidence_json is None else json.loads(row.evidence_json),
                    confidence=row.confidence,
                    requires_review=row.requires_review,
                    error_class=row.error_class,
                    error_code=row.error_code,
                    input_fingerprint=row.input_fingerprint,
                    enrichment_schema_version=row.enrichment_schema_version,
                    provenance=json.loads(row.provenance_json),
                    recorded_at=row.recorded_at,
                    stale=bool(reasons),
                    stale_reasons=reasons,
                )
            )
        return ResultsView(
            product_group_id=product_group_id, ai_configured=identity is not None, results=views
        )

    def current_result(
        self,
        product_group_id: str,
        task_key: str,
        result_key: str,
        marketplace_key: str,
        marketplace_account_id: str,
    ) -> ResultView | None:
        """The current result of one subject for one target, with its derived staleness, or
        ``None``. A target-free result serves any target; a targeted one serves only its own
        (ADR-0026 §7)."""
        target = Target(marketplace_key, marketplace_account_id)
        for view in self.results(product_group_id).results:
            if (view.task_key, view.result_key) != (task_key, result_key):
                continue
            own = None if view.target is None else Target(**view.target.model_dump())
            if own is None or own == target:
                return view
        return None

    # -------------------------------------------------------------- inputs

    def _inputs(
        self,
        task: TaskDefinition,
        product_group_id: str,
        target: Target | None,
        identity: RequestedIdentity,
    ) -> _Inputs:
        read = list(self._facts(product_group_id, task.fact_fields))
        facts = [{"member": m["member"], "fields": m["fields"]} for m in read]
        composed = self._composer.compose(
            task.task_key,
            {
                "product_group_id": product_group_id,
                "target": None if target is None else asdict(target),
                "facts": facts,
            },
            policy_key=None if target is None else POLICY_BY_MARKETPLACE[target.marketplace_key],
            context={} if target is None else asdict(target),
        )
        inputs = {
            "facts": facts,
            "prompt_version": dict(composed.prompt_version),
            "policy_version": dict(composed.policy_version),
            "provider": asdict(identity),
            "schema": task.schema_version,
        }
        # Recorded with the result for tracing, outside the fingerprint and the stale reasons.
        revisions = {m["member"]: m["revision_id"] for m in read}
        return _Inputs(
            composed=composed,
            inputs={**inputs, "fact_revisions": revisions},
            fingerprint=_digest(inputs),
        )

    def _facts(self, product_group_id: str, fields: Sequence[str]) -> Iterator[dict[str, Any]]:
        """Exactly the declared fields of every confirmed member's current revision."""
        with self._products.reading() as unit:
            product = unit.readback(product_group_id)
            if product is None:
                raise _unknown()
            for member in product.members:
                revision = member.current_source_revision_id
                readings = {} if revision is None else unit.source_fields(revision, fields)
                yield {
                    "member": member.source_product_uid,
                    "revision_id": revision,
                    "fields": {
                        key: {
                            "status": readings[key].status.value,
                            "value": _value_json(readings[key].value),
                        }
                        for key in sorted(readings)
                    },
                }

    def _fresh(
        self,
        session: Session,
        product_group_id: str,
        task: TaskDefinition,
        key: str,
        target: Target | None,
        fingerprint: str,
    ) -> bool:
        current = self._store.current(
            session, subject_key(product_group_id, task.task_key, key, target)
        )
        return (
            current is not None
            and current.status == "OK"
            and current.input_fingerprint == fingerprint
        )

    def _settled_by(
        self,
        session: Session,
        product_group_id: str,
        task: TaskDefinition,
        key: str,
        target: Target | None,
        ctx: JobContext,
    ) -> bool:
        current = self._store.current(
            session, subject_key(product_group_id, task.task_key, key, target)
        )
        return current is not None and current.job_id == ctx.job_id

    def _task(self, key: str) -> TaskDefinition:
        task = self._tasks.get(key)
        if task is None:
            raise InputValidationError(
                AI_TASK_NOT_RUNNABLE,
                f"{key} has no runnable definition yet; it lands with its own stage",
                details={"task": key},
            )
        return task

    def _target(self, view: TargetView | None) -> Target | None:
        if view is None:
            return None
        if view.marketplace_key not in POLICY_BY_MARKETPLACE:
            raise InputValidationError(
                AI_TARGET_HAS_NO_POLICY,
                f"{view.marketplace_key} has no platform policy in the prompt registry",
            )
        account = self._accounts.account(view.marketplace_account_id)
        if account is None or account.marketplace_key != view.marketplace_key:
            raise NotFoundError(
                AI_TARGET_UNKNOWN, "the target is not a canonical account of that marketplace"
            )
        return Target(view.marketplace_key, view.marketplace_account_id)

    def _require_product(self, product_group_id: str) -> None:
        with self._products.reading() as unit:
            if unit.readback(product_group_id) is None:
                raise _unknown()


def _unknown() -> NotFoundError:
    return NotFoundError(PRODUCTS_PRODUCT_UNKNOWN, "no canonical product has that id")


def _value_json(value: object) -> Any:
    dump = getattr(value, "model_dump", None)
    return None if dump is None else dump(mode="json")


def _status_fields(
    task: TaskDefinition, key: str, ok: bool, value: dict[str, Any] | None, outcome: Any
) -> dict[str, Any]:
    failed = {
        "status": "FAILED",
        "value_json": None,
        "evidence_json": None,
        "confidence": None,
        "requires_review": None,
    }
    if not ok or value is None:
        return {
            **failed,
            "error_class": (outcome.error_class or ErrorClass.UNKNOWN).value,
            "error_code": outcome.error_code or "AI_PROVIDER_FAILED",
        }
    part = value if task.keys == (SINGLE_RESULT,) else value.get(key)
    if not isinstance(part, dict):
        return {
            **failed,
            "error_class": ErrorClass.VALIDATION.value,
            "error_code": AI_OUTPUT_FIELD_MISSING,
        }
    if not _conforms(part, task.results[key]):
        return {
            **failed,
            "error_class": ErrorClass.VALIDATION.value,
            "error_code": AI_OUTPUT_SCHEMA_INVALID,
        }
    return {
        "status": "OK",
        "value_json": _canonical({k: v for k, v in part.items() if k not in ENVELOPE}),
        "evidence_json": _canonical(part["evidence"]),
        "confidence": float(part["confidence"]),
        "requires_review": part["requires_review"],
        "error_class": None,
        "error_code": None,
    }


def _conforms(part: Mapping[str, Any], schema: ResultSchema) -> bool:
    """The object carries the structured envelope and every value field its schema requires: an
    object that does not is a failed result, never an OK one."""
    confidence = part.get("confidence")
    return (
        isinstance(part.get("evidence"), list)
        and isinstance(confidence, int | float)
        and not isinstance(confidence, bool)
        and 0 <= confidence <= 1
        and isinstance(part.get("requires_review"), bool)
        and all(part.get(field) is not None for field in schema.required)
    )


def _stale_reasons(stored: Mapping[str, Any], now: _Inputs | None, task_gone: bool) -> list[str]:
    if task_gone:
        return ["schema"]
    if now is None:
        return ["provider"]
    current = now.inputs
    return [
        reason
        for reason, part in zip(
            REASONS,
            ("facts", "prompt_version", "policy_version", "provider", "schema"),
            strict=True,
        )
        if stored.get(part) != current.get(part)
    ]
