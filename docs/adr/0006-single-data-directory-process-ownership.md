# ADR-0006 — Single data-directory process ownership

Status: **ACCEPTED**
Decision owner: Architect (ChatGPT) — Issue #4 directive `5653140744`, clarifications `5653161585` and `5653170835`
Recorded by: Claude Code, per Issue #4 (number confirmed free in `docs/adr/` immediately before writing)
Date accepted: 2026-09-13
Related: `docs/adr/0001-runtime-stack.md` (SQLite WAL, one worker owner), `docs/adr/0002-job-worker-placement.md` (in-process worker), `docs/adr/0005-durable-job-state-and-attempt-history.md`, `docs/acceptance/M0.md` limitation L3

---

## Context

M0's SQLite write coordinator and its single JobWorker are process-scoped (ADR-0002). Nothing stopped two ICBM processes from opening the same data directory, and then “one worker owner” and serialised writes no longer held globally: two processes could compete for due jobs, migrate a schema under a running server, or — once CONNECT/COLLECT jobs exist — act on suppliers twice. This was accepted M0 limitation L3 and is the M1 entry gate.

The concern is multiple ICBM application processes, not browser child processes.

## Decision

### Ownership primitive

- The lock file is `<ICBM_DATA_DIR>/.icbm-owner.lock`. The owner opens it and keeps the handle open for its whole lifetime.
- **Windows:** `msvcrt.locking(LK_NBLCK)` on one byte at offset 1 MiB. Windows byte-range locks are mandatory, so the locked byte lies past the metadata that contenders read.
- **POSIX:** `fcntl.flock(LOCK_EX | LOCK_NB)`.
- **Identity is the OS lock on that open file handle.** No ownership key is derived from a path string or a normalised path cache. Every spelling that reaches the same directory — relative, `..`, symlink, junction — opens the same file and contends for the same lock.
- Acquisition never blocks and never takes over. A held lock means `DATA_DIR_IN_USE`.
- **Fail closed.** Without an OS locking primitive the command refuses to run (`DATA_DIR_LOCK_UNSUPPORTED`); an unexpected locking error also refuses (`DATA_DIR_LOCK_FAILED`). There is no best-effort multi-process mode.
- **Crash release.** Process death — graceful or forced — releases the OS lock. The lock file stays behind and is never deleted by ICBM (on POSIX, deleting a locked file would let a second process lock a fresh inode); recovery never requires deleting it. After acquiring, the owner verifies that its handle is still the file at the path. Windows releases a terminated process's handles asynchronously, so recovery checks may poll for up to about 5 s.
- Lock handles are non-inheritable (PEP 446), so child processes such as future Playwright browsers never keep ownership alive.
- **Metadata is diagnostic only.** After acquiring the lock, the owner writes JSON `pid`, `started_at`, `hostname`, `resolved_data_dir`, `app_version`, `lock`. Contenders may display it; stale metadata after a crash is expected and never consulted to decide ownership.

### Scope — default command policy

Every ICBM command acquires the lock **by default**, before any per-data-directory side effect (log file, database engine, migration, worker). Only commands explicitly classified as read-only may skip it. The classification lives in `app/cli.py` (`OWNING_COMMANDS`, `READ_ONLY_COMMANDS`); `tests/unit/test_repository_rules.py` fails if any CLI command is unclassified or if the read-only rows below differ from `READ_ONLY_COMMANDS`.

| Command / entry point | Policy |
| --- | --- |
| `icbm serve` | owner — acquired before logging, database or worker; held for the process lifetime |
| `icbm db upgrade` | owner — held for the whole migration; contention prints “Stop the ICBM server using this data directory, then retry the database upgrade.” |
| `icbm db current` | read-only exception — SQLite opened with `mode=ro`, no pragma, never creates a database, takes no lock (SQLite may create its own `-wal`/`-shm` coordination files, which hold no application state) |
| application factory `create_app` without an injected lease | owner — so `uvicorn --factory` or any other launcher cannot bypass the lock |
| Alembic `env.py` without an injected lease | owner — so the `alembic` CLI cannot migrate under a running server |
| future restore / repair / write tools | owner — must use `app.core.ownership.acquire_data_dir` |

Adding another read-only exception requires changing this ADR.

External read-only SQLite observers, such as the acceptance evidence reads with `sqlite3`, are not ICBM owners and are not blocked: the lock guards `.icbm-owner.lock`, not `icbm.db`.

### Contention contract

- `reason_code = DATA_DIR_IN_USE`; CLI exit status **3**. Other ownership failures exit with status **4**.
- stderr names the resolved data directory, shows the lock metadata as “diagnostic only”, and gives an operator hint.
- A contending `icbm serve` exits before configuring the log file, opening the database or starting a worker.

### Readiness

Readiness includes `data_dir_owner`. It is PASS only while this process holds an active lease whose handle is still the lock file at its path, and FAIL if ownership was never acquired or has been released.

## Consequences

- Exactly one ICBM process owns a data directory at a time, so ADR-0002's single worker and the process-scoped write coordinator hold for that directory as a whole. SQLite WAL and Job/JobAttempt semantics (ADR-0005) are unchanged.
- Operators stop the server before `icbm db upgrade`.
- Evidence is required from real processes on Windows and Ubuntu: second-owner fail-fast, `db upgrade` contention, graceful and forced-kill reacquisition.
- Moving the worker to a separate process (ADR-0002 reconsideration) must define how ownership is shared or transferred, in a superseding ADR.

## References

- Issue #4 and its architect comments `5653140744`, `5653161585`, `5653170835`
- `app/core/ownership.py`, `app/cli.py`, `app/main.py`, `app/db/migrations/env.py`, `app/system/readiness.py`
- `tests/unit/test_ownership.py`, `tests/integration/test_ownership_app.py`, `tests/integration/test_ownership_processes.py`
