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
- **M3** — KM통상 one-product COLLECT → ProductFactsRevision, with zero AI/OCR calls: **ACCEPTED** 2026-09-18, bounded by its §2 capability boundary (Issue #52, [ADR-0010](docs/adr/0010-supplier-generic-collect-and-product-facts-revision.md), [`docs/acceptance/M3.md`](docs/acceptance/M3.md)).
- **M4** — canonical Product DB + image pipeline + pricing/readiness foundations: **ACCEPTED** 2026-09-19 on the final offline acceptance run at exact main `57a6676`, bounded by its §2 (Issue #80, [ADR-0013](docs/adr/0013-m4-canonical-product-contract.md), [`docs/acceptance/M4.md`](docs/acceptance/M4.md)).
- **Current milestone:** M5 — SmartStore REGISTER idempotency/reconcile/read-back ([`ROADMAP.md`](ROADMAP.md) §12). This is a status pointer only: no M5 behaviour is implemented or authorized yet.

External calls happen only when the operator starts an action (no call at startup), and only for these accepted flows:
- **KM통상 CONNECT:** the application authenticates and reads the protected 마이쇼핑 page, through the common, allowlisted supplier transport.
- **KM통상 one-product COLLECT (M3):** for one operator-supplied product URL at a time, it reads that product page and the page's own product images through the same policed transport, under the collection request controls. It stores them as an immutable `ProductFactsRevision` with its source assets, which can be read back unchanged.
- **SmartStore CONNECT:** it uses only the two adopted endpoints (token and seller account), through the registry-gated caller.

Collection is one product per operator action. There is no listing, category or bulk collection and no crawl.

A recorded collection materializes the canonical `Product` (M4): its current source revision, its Items and their source bindings, which can be read through `GET /api/v1/products/{product_group_id}`. Pricing snapshots, derived image lineage with operator selection and exact-binary QA, and derived readiness are server-owned. The offline acceptance run proved them ([`docs/acceptance/M4.md`](docs/acceptance/M4.md)). Nothing is registered to a marketplace yet (M5). Positive `CONFIRMED` option-axis/configuration support and quantity-tier values/source totals are not claimed ([`docs/acceptance/M3.md`](docs/acceptance/M3.md) §2). The application makes **zero external business writes**. Every screen without a live contract renders the empty state reported by its application contract.

## Quick start (Windows, Python 3.12)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -c constraints.txt -e ".[dev]"
.\.venv\Scripts\icbm db upgrade      # creates %USERPROFILE%\ICBM-NEW\data\runtime\icbm.db at head
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

ICBM-NEW decides its data root itself, so the operator never names one. `app/config.py` is the
only resolver (`default_data_dir`). The local data root is `%USERPROFILE%\ICBM-NEW\data`, taken
from `USERPROFILE` alone:
- The user profile is not redirected for packaged (MSIX) processes, so the desktop app, a
  terminal and a launcher all reach the same physical directory.
- Without an absolute `USERPROFILE`, resolution fails closed.
- `ICBM_DATA_DIR` names the data root explicitly, for tests and dedicated acceptance directories
  only.

This is the local desktop/CLI root. A server deployment will have its own storage owner (Issue
#52 comment 5688854287).

| Under the data root | Contents |
| --- | --- |
| `runtime\icbm.db` | the one canonical relational database |
| `runtime\sessions\`, `runtime\marketplace_sessions\` | encrypted sessions (keys in the OS secret store) |
| `runtime\owner.lock` | the single-owner lock (ADR-0006) |
| `logs\` | application logs |
| `source-assets\` | COLLECT source assets (ADR-0010), left in place until a separate migration |
| `products\`, `api\`, `suppliers\`, `backups\` | reserved names for domain data, not created yet |

No login, API secret, session cookie or auth header is ever stored in plain text under the data
root.

CONNECT (M1/M2) is the only owner of the logins, the connection state and the sessions. Every
ICBM process, and every harness that borrows a connection (the M3 reconnaissance), resolves this
same owner, so a login saved once in ICBM is the one reused everywhere. Nothing may define a
second owner, login store or session store. The M3 reconnaissance binds the owner at its
preflight and refuses a run against any other. Repository rules in
`tests/unit/test_repository_rules.py` keep this fixed.

### One process per data directory

`icbm serve` and `icbm db upgrade` take an exclusive OS lock on `<data root>/runtime/owner.lock`
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
