# ICBM-NEW

Local single-user commerce operations application:
`CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE`.

Read [`CLAUDE.md`](CLAUDE.md), [`ROADMAP.md`](ROADMAP.md) and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) before changing anything.
The approved UI source is recorded in [`docs/UI_SOURCE_OF_TRUTH.md`](docs/UI_SOURCE_OF_TRUTH.md).

## Status

- **M0** — fresh v29 UI shell + Phase 0 foundation: **ACCEPTED** 2026-09-13 ([`docs/acceptance/M0.md`](docs/acceptance/M0.md)).
- **Issue #4** — one ICBM process per data directory: **ACCEPTED** and merged ([ADR-0006](docs/adr/0006-single-data-directory-process-ownership.md)).
- **Current milestone:** M1 — K홀세일 CONNECT only (Issue #7, [ADR-0007](docs/adr/0007-supplier-generic-connect.md)), in progress; not accepted yet.
- **Next:** M2 — SmartStore CONNECT ([`ROADMAP.md`](ROADMAP.md) §12).

The application performs **zero supplier/marketplace calls and zero external writes**; every screen renders the empty state reported by its application contract.

## Quick start (Windows, Python 3.12)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -c constraints.txt -e ".[dev]"
.\.venv\Scripts\icbm db upgrade      # creates var\icbm.db (SQLite WAL) at schema head
.\.venv\Scripts\icbm serve           # http://127.0.0.1:8790
```

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | liveness |
| `GET /api/ready` | deterministic readiness (data-directory owner, DB, WAL, schema head, worker, secret store, DRY_RUN, egress) |
| `GET /api/v1/shell`, `GET /api/v1/screens/{screen}` | UI application contracts |
| `POST /api/v1/system/execution-mode` | protected action (audited; LIVE is always denied for now) |
| `POST /api/v1/diagnostics/failing-job` | retry/dead-letter demonstration (`ICBM_DIAGNOSTICS=1` only) |

Configuration is read from `ICBM_*` environment variables (`app/config.py`). The server binds
loopback only and refuses `ICBM_EXECUTION_MODE=LIVE`.

### One process per data directory

`icbm serve` and `icbm db upgrade` take an exclusive OS lock on `<ICBM_DATA_DIR>/.icbm-owner.lock`
([ADR-0006](docs/adr/0006-single-data-directory-process-ownership.md)). A second owner exits with
status 3 and `DATA_DIR_IN_USE`; stop the server before running `icbm db upgrade`. `icbm db current`
is read-only and takes no lock. After a crash the next start recovers on its own — never delete the
lock file.

## Checks

```powershell
.\.venv\Scripts\ruff check . ; .\.venv\Scripts\ruff format --check .
.\.venv\Scripts\mypy
.\.venv\Scripts\pytest
.\.venv\Scripts\python scripts\m0_acceptance.py   # end-to-end M0 acceptance run
```
