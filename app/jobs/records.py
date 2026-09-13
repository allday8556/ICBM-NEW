from datetime import datetime

from pydantic import BaseModel, ConfigDict


class JobAttemptRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt_no: int
    scheduled_for: datetime
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    error_class: str | None
    error_code: str | None
    error_message: str | None
    retry_at: datetime | None


class JobRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    job_type: str
    target_ref: str | None
    state: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime | None
    correlation_id: str
    last_error_class: str | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime
    attempts: list[JobAttemptRecord] = []
