"""M0 diagnostic job used to demonstrate retry/backoff/dead-letter (Issue #1, deliverable 8)."""

import logging

from app.core.errors import TransientError
from app.jobs.registry import JobContext, JobDefinition

logger = logging.getLogger("icbm.jobs.diagnostic")

FAILING_JOB_TYPE = "m0.diagnostic_always_fail"


def _always_fail(ctx: JobContext) -> None:
    logger.info(
        "diagnostic.failing_job.executing",
        extra={
            "job_id": ctx.job_id,
            "attempt_no": ctx.attempt_no,
            "max_attempts": ctx.max_attempts,
        },
    )
    raise TransientError(
        "M0_DIAGNOSTIC_FORCED_FAILURE",
        "M0 diagnostic job fails on every attempt to exercise retry, backoff and dead-letter.",
    )


FAILING_JOB = JobDefinition(
    job_type=FAILING_JOB_TYPE,
    handler=_always_fail,
    description="Always raises TRANSIENT; reaches dead-letter at the configured attempt cap.",
    idempotent=True,
)
