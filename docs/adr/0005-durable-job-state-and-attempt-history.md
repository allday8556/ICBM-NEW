# ADR-0005 — Durable Job states and attempt history

Status: **ACCEPTED**
Decision owner: Architect (ChatGPT) — ruling Q3 in PR #2 review `5190491898`
Recorded by: Claude Code, per Issue #3
Date accepted: 2026-09-13
Related: `docs/ARCHITECTURE.md` §8, `docs/adr/0002-job-worker-placement.md`, `docs/adr/0004-registration-automation-guardrails.md`

---

## Context

`docs/ARCHITECTURE.md` §8 defines one durable Job contract shared by collect/register/stock/order/inquiry/tracking work and lists its core fields, but not the state machine or how individual attempts are recorded. M0 had to fix both to demonstrate that a failing job retries on schedule and reaches dead-letter at its cap (Issue #1, acceptance item 3).

The earlier roadmap proposal used a single `FAILED` state, which cannot distinguish "failed, a retry is scheduled" from "failed for good". The M0 implementation chose explicit states and a separate history table; the architect accepted that choice when M0 was accepted.

## Decision

### Job states

```text
QUEUED | RUNNING | RETRY_SCHEDULED | SUCCEEDED | DEAD
```

| State | Meaning |
| --- | --- |
| `QUEUED` | Persisted and due at `next_attempt_at`; not yet attempted. |
| `RUNNING` | Claimed by the single worker (ADR-0002); `attempt_count` already incremented. |
| `RETRY_SCHEDULED` | The last attempt failed with a retryable class; the next attempt is due at `next_attempt_at`. |
| `SUCCEEDED` | Terminal. The last attempt completed. |
| `DEAD` | Terminal dead-letter. Automation has stopped; a human decides what happens next. |

Transitions:

```text
QUEUED           → RUNNING            claimed when due
RETRY_SCHEDULED  → RUNNING            claimed when next_attempt_at is due
RUNNING          → SUCCEEDED          attempt completed
RUNNING          → RETRY_SCHEDULED    error class TRANSIENT or RATE_LIMITED and attempt_count < max_attempts;
                                      next_attempt_at = failure time + backoff delay
RUNNING          → DEAD               any other error class, or the attempt cap is reached
RUNNING at startup (previous process stopped mid-attempt):
                 → RETRY_SCHEDULED    only if the job type is declared idempotent and attempts remain
                 → DEAD               otherwise, as UNKNOWN / INTERRUPTED_OUTCOME_UNKNOWN
```

`SUCCEEDED` and `DEAD` are terminal: no automatic transition leaves them. Every transition into `DEAD` is recorded as an `AuditEvent` of type `JOB_DEAD_LETTERED` (architect ruling Q2).

### Attempt history

`job_attempts` is an append-style history with exactly one row per `(job_id, attempt_no)`:

```text
JobAttempt:
job_id, attempt_no, scheduled_for, started_at, finished_at,
outcome (SUCCEEDED | FAILED | INTERRUPTED), error_class, error_code, error_message, retry_at
```

A row is written when the attempt starts and completed when it ends. Rows are never deleted (foreign key `ON DELETE RESTRICT`). The history is the evidence that retries ran on schedule: `scheduled_for` of attempt *n* minus `finished_at` of attempt *n − 1* is the planned backoff, and `started_at − scheduled_for` is the start lag.

### Retry rule (unchanged)

- Automatic retry only for `TRANSIENT` and `RATE_LIMITED` (ADR-0004).
- `UNKNOWN`, `VALIDATION`, `POLICY_BLOCKED`, `AUTH` and `NOT_FOUND` go to `DEAD` on their first failure. An `UNKNOWN` write outcome is reconciled, never blindly retried (`docs/ARCHITECTURE.md` §7–§8).
- Backoff is deterministic exponential with a cap and a per-job `max_attempts`. Jitter, if a marketplace rate limiter needs it (ARCHITECT_REVIEW B7), requires a superseding ADR.

## Consequences

- `jobs` carries the current state; `job_attempts` carries the history. Both are created by migration `0001_m0_foundation`.
- The worker's due query is `state IN (QUEUED, RETRY_SCHEDULED) AND next_attempt_at <= now`.
- Changing the state set, the transitions or the attempt semantics requires a superseding ADR. `tests/unit/test_repository_rules.py` checks the state and outcome lists in this ADR against `app/jobs/models.py`.

## References

- Issue #1 — M0 acceptance item 3
- PR #2 architect review `5190491898` — rulings Q2 and Q3
- `docs/acceptance/M0.md` §3.3 — schedule evidence
- `app/jobs/models.py`, `app/jobs/runner.py`
