<!-- Moved verbatim from CLAUDE.md §4 (Pinned runtime stack) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §4` references resolve here (PATH_MIGRATION_MAP). -->

## 4. Pinned runtime stack

The authoritative stack is `documents/architecture/ARCHITECTURE.md` §2. Current v1:

| Item | Decision |
| --- | --- |
| Language / runtime | Python 3.12 |
| Web / app framework | FastAPI + Uvicorn |
| Database | SQLite WAL — local single-user v1 |
| Migration tool | Alembic |
| ORM | SQLAlchemy 2.x |
| HTTP | httpx |
| Browser automation | Playwright Chromium when required |
| Job runner | Durable DB-backed queue/scheduler; one worker owner initially |
| Process model | Local single-user Windows application, loopback-only by default; one ICBM process per data directory (ADR-0006) |
| Secret storage | OS-native secure credential storage via keyring / Windows protection |
| Test framework | pytest + contract/integration/E2E gates |
| CI | GitHub Actions |

Replacing a canonical stack component or changing the product deployment/runtime model is a product
decision: it requires explicit user approval and an accepted ADR before implementation. Compatible
dependency updates, implementation libraries and internal code organization inside the pinned stack
are implementation decisions under §14; record them in the PR, without creating an ADR unless they
also change the canonical stack or externally visible product behaviour.
