# ADR-0001 — Runtime stack

Status: **DRAFT — awaiting architect acceptance**
Author of draft: Claude
Decision owner: Architect (ChatGPT)
Date drafted: 2026-09-13
Place at: `docs/review/` until accepted, then move to `docs/adr/0001-runtime-stack.md` with status `ACCEPTED`

---

## Context

The stack for ICBM-NEW v1 was decided in `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md`
§6 and is currently written out in three places:

- `docs/ARCHITECTURE.md` §2 (declared authoritative by `CLAUDE.md` §4)
- `CLAUDE.md` §4
- `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md` §6

Three copies of one decision will eventually diverge, because only one of them
gets edited when something changes. `CLAUDE.md` §4 and `docs/ARCHITECTURE.md` §2
both state that changing the stack requires an ADR — but no ADR exists, so there
is nothing for a change to be recorded against.

This ADR records the decision once so the other three become references rather
than independent sources.

## Decision

ICBM-NEW v1 is a **single-user local Windows application with a local web UI**.

| Concern | Decision |
| ------- | -------- |
| Language / runtime | Python 3.12 |
| Backend | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2.x |
| Migrations | Alembic |
| Database | SQLite in WAL mode |
| Frontend | Approved standalone HTML/CSS/vanilla JS shell, split into ES modules |
| HTTP client | httpx |
| Browser automation | Playwright (Chromium) — only where supplier interaction requires a browser |
| Jobs | Durable DB-backed Job table with a single scheduler/worker owner |
| Secrets | OS-native credential store via `keyring` / Windows credential protection |
| Deployment | Local Windows process, loopback-only by default |
| Tests | pytest, with contract / integration / E2E gates |
| CI | GitHub Actions — lint, type check, tests, migration check |

No component in this table may be substituted without a superseding ADR.

## Constraints that follow from SQLite

SQLite is accepted because v1 is local and single-user, not multi-tenant. The
implementation must therefore:

- run in WAL mode
- keep transactions short
- maintain **one durable DB-write owner** for background work
- avoid feature-specific embedded databases
- keep SQLite-specific behavior behind repository/service boundaries so business
  contracts never depend on it

If the product later becomes multi-user or server-hosted, a DB-engine ADR is
required before migration. A distributed stack is not pre-built now.

## Consequences

**Accepted:**

- Fast local startup, no external services to operate, trivially backed up by
  copying a file (which makes the C5 restore drill cheap to rehearse).
- One language across backend, jobs, and supplier automation.

**Costs:**

- SQLite write concurrency is the binding constraint on the job layer. Every
  design decision about workers has to respect the single-writer rule.
- Playwright pulls in a browser runtime and meaningful per-run latency; supplier
  collection cannot be treated as a fast in-request operation.
- Loopback-only deployment means any future remote access is a new decision, not
  a configuration change.

**Rejected alternatives:**

- Postgres — correct for a multi-user server, unjustified operational cost for a
  single-user local v1. Revisit only if the process model changes.
- A dedicated queue (Redis/RQ/Celery) — adds a service to operate for a workload
  that one durable table and one worker can carry at this scale.
- A frontend framework — the approved prototype is plain HTML/CSS/JS; adopting a
  framework would mean rewriting the visual source of truth rather than
  reproducing it.

## Open question requiring an architect decision

The stack states "one worker owner initially" but does not say **where that
worker runs.** This affects the M0 job-layer design directly, so it should be
settled before M0 code, not during it.

**Option A — worker as a background task inside the FastAPI process**
Simplest to start and to ship as one executable. Risk: a long Playwright run
occupies the same process that serves the UI, and any worker crash takes the API
with it.

**Option B — worker as a separate process against the same SQLite file**
Isolates long supplier work from the UI. Requires disciplined WAL usage and a
clear single-writer arrangement; two processes writing is exactly what the
constraint above forbids, so the write path must be owned by one of them.

**Option C — separate worker process, worker owns all writes**
The API reads and enqueues; the worker performs every durable write. Cleanest fit
with the single-writer constraint and with the DRY_RUN/LIVE boundary, since all
external writes then originate from one place. Costs an IPC/queue-polling
mechanism and makes local startup two processes instead of one.

**Draft recommendation: Option C**, on the grounds that the single-writer rule is
already a stated constraint and Option C is the only one that enforces it
structurally rather than by convention. Option A is the pragmatic alternative if
M0 speed matters more than that guarantee — but if A is chosen, the switch to C
later will touch every write path, so it should be chosen deliberately rather
than by default.

This recommendation is **not** a decision. The architect decides; the outcome
becomes ADR-0002.

## References

- `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md` §6
- `docs/ARCHITECTURE.md` §2, §8
- `CLAUDE.md` §4
