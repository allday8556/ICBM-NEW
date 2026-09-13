# ADR-0001 — Runtime stack

Status: **ACCEPTED**
Author: Architect (ChatGPT), based on Claude draft
Date accepted: 2026-09-13
Supersedes: none
Source draft: `docs/review/ADR-0001-runtime-stack-DRAFT-BY-CLAUDE.md`

---

## Context

ICBM-NEW needs one pinned v1 runtime stack so implementation does not re-decide language, framework, database, browser automation, deployment model, or queue design from session to session.

The application is intentionally a **single-user local Windows application with a local web UI** for v1. A move to multi-user/server-hosted operation is a separate architecture decision.

## Decision

| Concern | Decision |
| ------- | -------- |
| Language / runtime | Python 3.12 |
| Backend | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2.x |
| Migrations | Alembic |
| Database | SQLite in WAL mode |
| Frontend | Approved standalone HTML/CSS/vanilla JS shell, split into ES modules |
| HTTP client | httpx |
| Browser automation | Playwright (Chromium) only where supplier interaction requires a browser |
| Jobs | Durable DB-backed Job table with one scheduler/worker owner for background jobs |
| Secrets | OS-native credential storage via `keyring` / Windows credential protection |
| Deployment | Local Windows process, loopback-only by default |
| Tests | pytest with contract / integration / E2E gates |
| CI | GitHub Actions — lint, type check, tests, migration check |

No major component in this table may be substituted without a superseding ADR.

Worker placement is decided separately by `ADR-0002`.

## SQLite constraints

SQLite is accepted because v1 is local and single-user. Therefore:

- WAL mode is mandatory.
- Transactions must stay short.
- Background jobs have exactly one worker owner.
- All DB writes go through the common repository / unit-of-work boundary rather than feature-specific direct connections.
- Feature-specific embedded databases are forbidden.
- SQLite-specific behavior must not leak into Product, Pricing, Registration, or Operation contracts.
- If the process model becomes multi-user or server-hosted, a DB-engine ADR is required before migration.

## Backup rule

Do **not** treat an arbitrary copy of a live `.db` file as a valid backup while WAL writes may be active.

Accepted backup paths are:

1. SQLite online backup API, or
2. controlled application shutdown / checkpoint followed by a verified copy.

A restore drill must prove the resulting backup can start the application and read canonical state before the first LIVE marketplace write.

## Consequences

### Accepted benefits

- Minimal local operational footprint.
- No Redis/Postgres service required for v1.
- Python can own backend, jobs, and supplier automation.
- The approved standalone UI can be reproduced rather than rewritten in a frontend framework.

### Accepted costs

- SQLite write concurrency is a design constraint.
- Playwright work must run through background jobs, not block request handlers.
- Remote/multi-user access is not a configuration toggle; it requires a new deployment/database decision.

## Rejected alternatives for v1

- **Postgres** — appropriate for a multi-user server, unnecessary operational cost for the local single-user v1.
- **Redis/RQ/Celery** — unnecessary service complexity for the first vertical.
- **Frontend framework rewrite** — would replace the approved UI source rather than reproduce it.

## References

- `docs/ARCHITECTURE.md`
- `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md`
- `CLAUDE.md`
- `docs/review/ADR-0001-runtime-stack-DRAFT-BY-CLAUDE.md`
