# Issue #4 — Single data-directory process ownership: evidence

- Status: **SUBMITTED — awaiting architect review.** This change is not accepted yet. Merging to `main` needs the user's explicit approval (architect comment 5653170835).
- Issue: #4. Architect requirements are in the issue body and in comments 5653140744, 5653161585 and 5653170835.
- Decision record: [`docs/adr/0006-single-data-directory-process-ownership.md`](../adr/0006-single-data-directory-process-ownership.md)
- Evidence commit: `46cc5bcd1d06b91e4c27888e4a84d9ed8cc4840e` on branch `fix/infra-data-dir-single-owner`, based on `main` at `2821bfa`. The branch's later commits add only this document.
- Scope: no M1 / K홀세일 code, no schema change and no external write.

## Runs

| Run | Environment | Result |
| --- | --- | --- |
| [CI run 34757147112](https://github.com/allday8556/ICBM-NEW/actions/runs/34757147112) | GitHub Actions, evidence commit | **success**: all 5 jobs |
| ↳ [Tests (ubuntu-latest)](https://github.com/allday8556/ICBM-NEW/actions/runs/34757147112/job/103723370122) | Linux, CPython 3.12.14 | 128 passed, no skips |
| ↳ [Tests (windows-latest)](https://github.com/allday8556/ICBM-NEW/actions/runs/34757147112/job/103723370329) | Windows, CPython 3.12.10 | 128 passed, no skips |
| ↳ [Lint, format, type check](https://github.com/allday8556/ICBM-NEW/actions/runs/34757147112/job/103723370226) | ruff, ruff format, mypy strict | success |
| ↳ [Migration check](https://github.com/allday8556/ICBM-NEW/actions/runs/34757147112/job/103723370246) | Alembic | success |
| ↳ [M0 acceptance (Windows, clean checkout)](https://github.com/allday8556/ICBM-NEW/actions/runs/34757147112/job/103723512802) | `scripts/m0_acceptance.py` | **PASS (24/24 checks)** |
| Local clean clone | Windows 11 Pro 10.0.26200, CPython 3.13.15; worktree clean at the evidence commit | `pytest -v`: 128 passed; `scripts/m0_acceptance.py --visual`: **PASS (24/24 checks)**, run id `20260913T122829Z` |

Each pytest run shows the same 2 warnings. Both are third-party deprecation notices raised when the Starlette/FastAPI test client is imported (the `httpx` test-client transport and the `anyio.abc.BlockingPortal` alias). Neither comes from ICBM code.

## Requirement → evidence

Every test below passed on both CI runners and in the local clean clone. None was skipped. On Ubuntu the `link` alias case uses a symlink; on Windows it uses a directory junction.

| Issue #4 requirement | Proven by |
| --- | --- |
| The lock is an exclusive OS lock inside the data directory. Ownership is the OS lock on the open handle, not a path string. | `tests/unit/test_ownership.py::test_a_second_owner_is_refused_even_within_one_process`<br>`test_every_spelling_of_the_same_directory_contends_for_one_lock[relative\|dotdot\|link]` |
| A second owner fails fast with `DATA_DIR_IN_USE` when it is a real second process. | `tests/integration/test_ownership_processes.py::test_second_process_cannot_own_and_graceful_stop_hands_over`<br>The contender runs `icbm serve` against `<dir>/logs/..`. It exits with code 3, its stderr starts with `DATA_DIR_IN_USE` and names the owner's pid, and it never listens on its own port. |
| `icbm db upgrade` conflicts with a running server and prints the operator hint. | The same process test (exit 3 plus the hint).<br>`tests/integration/test_ownership_app.py::test_db_upgrade_refuses_an_owned_directory` (exit 3, hint, nothing created). |
| `db current` is the only read-only exception. External `sqlite3` read-only inspection stays allowed. | The same process test: while the server runs, `icbm db current` prints the head revision, and `sqlite3` reports `journal_mode=wal`.<br>`test_ownership_app.py::test_db_current_is_read_only_and_needs_no_ownership` (creates no database and no data directory). |
| CLI default policy: any command that can change state takes the lock. | `tests/unit/test_repository_rules.py::test_every_cli_command_is_classified_for_data_dir_ownership` (every argparse leaf command is either owning or read-only).<br>`test_ownership_adr_lists_exactly_the_read_only_commands` (ADR-0006 and the code agree). |
| Other launchers cannot bypass the lock. | `test_ownership_app.py::test_app_factory_refuses_an_owned_directory_before_touching_it`<br>`test_an_injected_lease_must_cover_the_configured_directory`<br>`test_migrations_run_only_under_ownership` |
| Readiness reports `data_dir_owner`. | `test_ownership_app.py::test_readiness_passes_only_while_ownership_is_held`<br>`tests/integration/test_api.py::test_health_and_readiness_pass`<br>Both process tests assert `data_dir_owner=PASS` for the live owner. |
| A graceful stop releases the lock, and a successor can acquire it. | Process test 1: only the owner's pid logs `app.stopped`, and the successor reaches readiness PASS with its own pid in the metadata.<br>`test_ownership_app.py::test_app_owns_the_directory_while_running_and_releases_it_on_stop` |
| A forced kill releases the lock (bounded poll of 5 s or less). The lock file is never deleted and stale metadata is harmless. | `test_ownership_processes.py::test_forced_termination_releases_ownership_and_jobs_resume`: TerminateProcess or SIGKILL of the serving interpreter. The victim's metadata stays in place. The lock becomes acquirable within 5 s, and the successor takes over.<br>`test_ownership.py::test_stale_metadata_never_blocks_acquisition`<br>`test_release_allows_reacquisition_and_keeps_the_lock_file` |
| WAL and Job regression. | The forced-kill test kills the victim while its job is `RETRY_SCHEDULED`. Under the successor the job resumes and dead-letters with attempts `[1, 2, 3]`, and the database is still in WAL mode. The existing job-runner and migration suites are unchanged and pass. |
| The lock fails closed where no lock primitive exists. | `test_ownership.py::test_a_platform_without_a_lock_primitive_fails_closed` (`DATA_DIR_LOCK_UNSUPPORTED`, and nothing is created) |
| The README milestone is updated. | `test_repository_rules.py::test_readme_does_not_present_m0_as_the_current_milestone` |
| M0 acceptance still passes 24/24. | CI job "M0 acceptance (Windows, clean checkout)" and the local clean clone, both **24/24** |

## How the real-process tests synchronise

- Every wait is on an explicit signal: readiness reported over HTTP, process exit, or the OS lock becoming acquirable. None uses a fixed sleep.
- Recovery after a forced kill is a bounded poll of 5 s or less on lock acquisition, because Windows releases a dead process's handles asynchronously.
- The server's own pid comes from `GET /api/health` → `pid`. On Windows, a virtualenv's `python.exe` is a launcher, so `Popen.pid` may not be the interpreter that holds the lock. The forced kill targets that interpreter directly.
- Every JSON log line now carries `pid`. The first process test checks that nothing but the owner wrote to the log while the contenders ran.

## `scripts/m0_acceptance.py`

The script is unchanged and needs no change:

- Its direct `sqlite3` inspection opens `icbm.db`, while the ownership lock guards `.icbm-owner.lock`, so the two never conflict.
- It runs `icbm db upgrade` only while no server owns the directory.
- Each restart begins only after the previous owner has stopped.

## Reproduce

```bash
python -m pytest -v tests/unit/test_ownership.py tests/integration/test_ownership_app.py tests/integration/test_ownership_processes.py
```

```bash
python scripts/m0_acceptance.py --visual
```
