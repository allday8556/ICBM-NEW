# ADR-0002 — Job worker placement for v1

Status: **ACCEPTED**
Author: Architect (ChatGPT)
Date accepted: 2026-09-13
Supersedes: none
Related: `docs/adr/0001-runtime-stack.md`

---

## Context

ADR-0001 pins SQLite WAL and one durable background-job worker owner, but the Claude draft correctly identified that the worker location must be decided before M0 job-layer implementation.

Three options were considered:

- A — worker runs as a background task inside the FastAPI process
- B — separate worker process shares the SQLite database
- C — separate worker process owns every durable write and the API communicates through IPC

## Decision

Choose **Option A for ICBM-NEW v1**:

> One FastAPI/Uvicorn application process hosts one durable background worker task. Background jobs are not executed inside request handlers. All application DB writes use the same repository/unit-of-work layer and a process-scoped write coordinator so SQLite writes are serialized intentionally.

This is a deliberate v1 decision, not an accidental default.

## Why A instead of C

Option C provides strong structural isolation, but for a local single-user v1 it introduces a second process plus IPC solely to enforce a constraint that can be enforced more simply inside one process.

It also creates an awkward durability question for enqueueing: if the API cannot write the Job row directly, it must send an IPC command to the worker and wait for the worker to persist it. That extra protocol is not justified before the first vertical.

Option A keeps M0 small while preserving the architectural boundary needed to move the worker later.

## Required implementation boundaries

Choosing A does **not** allow job code to become FastAPI route code.

M0 must implement these separations:

```text
HTTP/UI route
→ application service
→ JobService.enqueue(...)
→ durable Job row
→ JobRunner / Worker
→ domain service / adapter
→ repository/unit-of-work
```

Rules:

1. request handlers never perform Playwright collection, marketplace registration, stock polling, order polling, or tracking upload inline;
2. exactly one background worker consumes durable jobs;
3. worker task exceptions are caught at the job boundary and cannot terminate the web application loop;
4. retry/backoff/dead-letter state is persisted in the Job table;
5. all DB write transactions remain short;
6. a process-scoped write coordinator serializes write sections that could otherwise contend;
7. long CPU-bound work must be offloaded from the event loop if introduced;
8. Playwright/browser processes may be child processes, but the ICBM job owner remains the single in-process worker.

## Migration boundary

The `JobRunner` and repository/unit-of-work interfaces must not depend on being in the same process. Business services receive contracts, not event-loop objects or FastAPI globals.

If production evidence later shows that browser workloads or reliability require process isolation, a superseding ADR may move the worker to a separate process without changing Product/Pricing/Register/Operate contracts.

Triggers for reconsideration include:

- UI/API responsiveness is materially degraded by background workload;
- worker failures repeatedly destabilize the API process;
- parallel supplier collection is required beyond the safe single-worker model;
- the application becomes multi-user/server-hosted;
- SQLite is replaced by a server database.

## M0 acceptance implication

M0 must demonstrate, not assert:

- enqueue a durable test job;
- worker consumes it outside the request path;
- deliberate failure retries according to policy;
- final failure reaches DEAD/dead-letter state;
- API remains responsive during the test;
- restart preserves the queued/dead-letter state.

## References

- `docs/review/ADR-0001-runtime-stack-DRAFT-BY-CLAUDE.md`
- `docs/adr/0001-runtime-stack.md`
- `docs/ARCHITECTURE.md`
