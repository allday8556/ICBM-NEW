# ICBM-NEW

Local single-user commerce operations application:
`CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE`.

Read [`CLAUDE.md`](CLAUDE.md), [`ROADMAP.md`](ROADMAP.md) and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) before changing anything.
The approved UI source is recorded in [`docs/UI_SOURCE_OF_TRUTH.md`](docs/UI_SOURCE_OF_TRUTH.md).

## Status

- **M0** — fresh v29 UI shell + Phase 0 foundation: **ACCEPTED** 2026-09-13 ([`docs/acceptance/M0.md`](docs/acceptance/M0.md)).
- **Issue #4** — one ICBM process per data directory: **ACCEPTED** and merged ([ADR-0006](docs/adr/0006-single-data-directory-process-ownership.md)).
- **M1** — KM통상 CONNECT only: **ACCEPTED** 2026-09-13 (Issue #7, [ADR-0007](docs/adr/0007-supplier-generic-connect.md), [`docs/acceptance/M1.md`](docs/acceptance/M1.md)).
- **M2** — SmartStore CONNECT: **ACCEPTED** 2026-09-15 (Issue #46, closeout PR #51, [`docs/acceptance/M2.md`](docs/acceptance/M2.md)).
- **Current milestone:** M3 — KM통상 one-product COLLECT → ProductFactsRevision, with zero AI/OCR calls ([`ROADMAP.md`](ROADMAP.md) §12, Issue #52, [ADR-0010](docs/adr/0010-supplier-generic-collect-and-product-facts-revision.md)).

External calls are limited to CONNECT, and happen only when the operator runs a connection action (no call at startup):
- **KM통상:** the application authenticates and reads the protected 마이쇼핑 page, through the common, allowlisted supplier transport.
- **SmartStore:** it uses only the two adopted endpoints (token and seller account), through the registry-gated caller.

It collects no products yet and makes **zero external business writes**. Every screen without a live contract renders the empty state reported by its application contract.

## Quick start (Windows, Python 3.12)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -c constraints.txt -e ".[dev]"
.\.venv\Scripts\icbm db upgrade      # creates %LOCALAPPDATA%\ICBM-NEW\icbm.db (SQLite WAL) at head
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

### Data directory and connection owner

ICBM-NEW decides its data directory itself, so the operator never names one. `app/config.py` is
the only resolver (`default_data_dir`). The data directory is the per-user application data root:
`%LOCALAPPDATA%\ICBM-NEW` on Windows, and `$XDG_DATA_HOME/ICBM-NEW` elsewhere. It is named after
the OS secret-store service `ICBM-NEW`, which holds the supplier and marketplace logins, and it
does not depend on the checkout, the working directory or the shell. `ICBM_DATA_DIR` is an
explicit override, for tests and dedicated acceptance directories only.

CONNECT (M1/M2) is the only owner of the logins, the connection state and the sessions. Every
ICBM process, and every harness that borrows a connection (the M3 reconnaissance), resolves this
same owner, so a login saved once in ICBM is the one reused everywhere. Nothing may define a
second owner, login store or session store. The M3 reconnaissance binds the owner at its
preflight and refuses a run against any other. Repository rules in
`tests/unit/test_repository_rules.py` keep this fixed.

### One process per data directory

`icbm serve` and `icbm db upgrade` take an exclusive OS lock on `<data directory>/.icbm-owner.lock`
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
