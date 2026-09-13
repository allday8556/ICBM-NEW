from app.jobs.service import JobService

COLLECT_JOB_PREFIX = "collect."


class CollectService:
    """COLLECT stage owner. Collection runs as durable ``collect.*`` jobs; M0 registers none."""

    def __init__(self, jobs: JobService) -> None:
        self._jobs = jobs

    def collection_job_count(self) -> int:
        return self._jobs.count(job_type_prefix=COLLECT_JOB_PREFIX)
