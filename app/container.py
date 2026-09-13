"""Explicit composition root: every service is built here and nowhere else."""

import os
from collections.abc import Sequence
from dataclasses import dataclass

from app.audit.service import AuditLog
from app.collect.service import CollectService
from app.config import AppConfig
from app.connect.service import ConnectService
from app.core.clock import Clock, SystemClock
from app.core.egress import EGRESS
from app.core.secrets import SecretStore, build_secret_store
from app.db.database import Database
from app.db.migrate import head_revision
from app.jobs.diagnostic import FAILING_JOB
from app.jobs.policy import RetryPolicy
from app.jobs.registry import JobDefinition, JobRegistry
from app.jobs.runner import JobRunner
from app.jobs.service import JobService
from app.jobs.worker import JobWorker
from app.operate.service import OperateService
from app.products.service import ProductsService
from app.register.service import RegisterService
from app.review.service import ReviewService
from app.screens.service import ScreenService
from app.system.diagnostics import DiagnosticsService
from app.system.execution_mode import ExecutionModeService
from app.system.readiness import ReadinessService
from integrations.marketplaces.identity import MARKETPLACE_IDENTITIES


@dataclass
class Container:
    config: AppConfig
    clock: Clock
    db: Database
    audit: AuditLog
    job_registry: JobRegistry
    jobs: JobService
    runner: JobRunner
    worker: JobWorker
    secrets: SecretStore
    execution_mode: ExecutionModeService
    diagnostics: DiagnosticsService
    readiness: ReadinessService
    screens: ScreenService


def build_container(
    config: AppConfig,
    *,
    clock: Clock | None = None,
    secret_store: SecretStore | None = None,
    extra_jobs: Sequence[JobDefinition] = (),
) -> Container:
    clock = clock or SystemClock()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.database_url)
    audit = AuditLog(db, clock)

    registry = JobRegistry()
    registry.register(FAILING_JOB)
    for definition in extra_jobs:
        registry.register(definition)

    policy = RetryPolicy(
        max_attempts=config.job_max_attempts,
        base_delay_s=config.job_backoff_base_s,
        factor=config.job_backoff_factor,
        max_delay_s=config.job_backoff_max_s,
    )
    jobs = JobService(db, registry, clock, policy)
    runner = JobRunner(
        db,
        registry,
        clock,
        audit,
        default_policy=policy,
        worker_id=f"worker-{os.getpid()}",
        lease_s=config.job_lease_s,
    )
    worker = JobWorker(runner, poll_interval_s=config.job_poll_interval_s, clock=clock)
    jobs.set_worker_notifier(worker.notify)

    secrets = secret_store or build_secret_store(config.secret_backend)
    execution_mode = ExecutionModeService(config.execution_mode, audit)
    diagnostics = DiagnosticsService(
        enabled=config.diagnostics_enabled, db=db, jobs=jobs, audit=audit
    )
    readiness = ReadinessService(
        db=db,
        worker=worker,
        secrets=secrets,
        egress=EGRESS,
        execution_mode=execution_mode,
        clock=clock,
        head_revision=head_revision(),
    )

    products = ProductsService()
    screens = ScreenService(
        clock=clock,
        operator_name=config.operator_name,
        marketplaces=MARKETPLACE_IDENTITIES,
        connect=ConnectService(MARKETPLACE_IDENTITIES),
        collect=CollectService(jobs),
        products=products,
        register=RegisterService(products),
        operate=OperateService(),
        review=ReviewService(),
        execution_mode=execution_mode,
    )
    return Container(
        config=config,
        clock=clock,
        db=db,
        audit=audit,
        job_registry=registry,
        jobs=jobs,
        runner=runner,
        worker=worker,
        secrets=secrets,
        execution_mode=execution_mode,
        diagnostics=diagnostics,
        readiness=readiness,
        screens=screens,
    )
