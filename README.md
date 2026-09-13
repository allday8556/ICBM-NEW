# ICBM-NEW

Local single-user commerce operations application:
`CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE`.

Read [`CLAUDE.md`](CLAUDE.md), [`ROADMAP.md`](ROADMAP.md) and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) before changing anything.
The approved UI source is recorded in [`docs/UI_SOURCE_OF_TRUTH.md`](docs/UI_SOURCE_OF_TRUTH.md).

## Current milestone: M0 — UI shell + foundation

M0 performs **zero supplier/marketplace calls and zero external writes**. Every screen renders
the empty state reported by its application contract. Evidence: [`docs/acceptance/M0.md`](docs/acceptance/M0.md).

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
| `GET /api/ready` | deterministic readiness (DB, WAL, schema head, worker, secret store, DRY_RUN, egress) |
| `GET /api/v1/shell`, `GET /api/v1/screens/{screen}` | UI application contracts |
| `POST /api/v1/system/execution-mode` | protected action (audited; always denied in M0) |
| `POST /api/v1/diagnostics/failing-job` | M0 retry/dead-letter demonstration (`ICBM_DIAGNOSTICS=1` only) |

Configuration is read from `ICBM_*` environment variables (`app/config.py`). The server binds
loopback only and refuses `ICBM_EXECUTION_MODE=LIVE` during M0.

## Checks

```powershell
.\.venv\Scripts\ruff check . ; .\.venv\Scripts\ruff format --check .
.\.venv\Scripts\mypy
.\.venv\Scripts\pytest
.\.venv\Scripts\python scripts\m0_acceptance.py   # end-to-end M0 acceptance run
```
