# M2 acceptance harness and evidence contract

Author: Claude Code · Status: **UNDER REVIEW** (Issue #46 §2 harness PR; architect audit pending)

This directory holds the evidence contract for the M2 SmartStore CONNECT acceptance campaign, and
the runbook of the harness that produces the evidence (`scripts/m2_acceptance.py`,
`scripts/m2harness/`). The acceptance plan itself is [`docs/acceptance/M2.md`](../M2.md). Nothing
here changes it, and nothing here changes product runtime behavior.

| File | Purpose |
| --- | --- |
| `m2-campaign-evidence.schema.json` | The only shape campaign evidence may take. Every object is closed, every string is an enum, a fixed pattern or bounded text, and no field exists for a secret or a raw account identity. `scripts/m2harness/evidence.py` enforces it. |
| `M2-EVIDENCE-TEMPLATE.md` | The closeout record (M2.md §7) that the evidence PR fills in. |

Campaign evidence files are committed here only by the evidence/closeout PR (Issue #46 §5). The
harness PR commits none, and **no real SmartStore request was made for it**.

## 1. What the harness guarantees, and where

| Guarantee (Issue #46 §2; comment 5669037896) | Mechanism | Proven by |
| --- | --- | --- |
| Default and CI runs cannot reach SmartStore | The default command is `dry`: a fake provider stands behind the real caller. The live inner transport exists only in an application process of a **REAL** ledger, and its factory refuses under `CI`, `GITHUB_ACTIONS`, `PYTEST_CURRENT_TEST` or a loaded pytest, both when it is created and when it builds. | `test_m2_harness_transport.py`, `test_m2_harness_gates.py::test_a_real_campaign_process_never_builds_the_live_transport_under_ci_or_tests`, `test_m2_harness_static.py` |
| No second SmartStore client; nothing bypasses the registry or egress ownership | The harness only passes a transport to the real `SmartStoreEndpointCaller`. The caller still resolves the adopted endpoint, composes the request, opens the egress grant and applies the timeouts, the success predicate and NO_FOLLOW. The gate recognizes only the registry's exact scheme, host, port, method and path. | `test_m2_harness_static.py`, `test_a_forbidden_target_never_reaches_a_transport`, `test_an_unadopted_endpoint_fails_in_the_caller_before_the_gate` |
| Durable reservation **before** the transport handoff | `BudgetedTransport` reserves in the campaign ledger (SQLite, `synchronous=FULL`) before the inner transport is even built. The response only completes the reservation. | `test_the_caller_request_is_reserved_before_the_transport_receives_it`, `test_a_request_is_durably_reserved_before_its_response_exists` |
| Hard caps: token 8, seller 6, anything else 0; over-cap → `BUDGET_EXHAUSTED`, nothing sent | Checked in the reservation transaction, and again by SQLite triggers. | `test_each_cap_refuses_…`, `test_every_other_endpoint_has_a_cap_of_zero`, `test_an_over_cap_request_never_reaches_the_transport`, `test_the_seller_cap_refuses_the_seventh_read_before_send` |
| Nothing exploratory | A request is accepted only as an unused step of the open phase's frozen plan (M2.md §6.1). | `test_only_the_open_phases_frozen_plan_can_be_sent` |
| Counts survive a crash; nothing resets them | Append-only tables and triggers. A ledger is never recreated over an existing one, and a campaign ID is registered once per machine (its OS keyring scope). | `test_the_budget_survives_a_process_that_dies_right_after_reserving`, `test_spent_budget_and_history_cannot_be_rewritten`, `test_a_ledger_with_a_dropped_guard_is_refused` |
| Crash sub-cap 2; no T4c | `crash_attempts` holds only attempts 1 and 2 (a CHECK constraint). T4b opens only after a T4a miss, and each attempt is exactly one token request. | `test_t4b_follows_only_a_t4a_miss_and_there_is_never_a_t4c`, `test_a_second_miss_exhausts_the_crash_sub_budget_and_there_is_no_t4c` |
| **T1 approval STOP is a code gate** | `preflight` ends at `AWAITING_REAL_PROVIDER_APPROVAL` with token 0 / seller 0 / other 0, and never continues. Only `Ledger.begin_real_run(approval)` leaves that state: it needs approval material written by `approve` at an interactive terminal (never under CI or tests), for the same campaign, SHA and preflight, and still the ledger's latest event. It is consumed atomically. A reservation outside `RUNNING` is refused. | `test_a_passed_preflight_without_approval_sends_nothing`, `test_nothing_is_accepted_before_an_approved_run`, `test_a_real_run_consumes_exactly_the_current_approval`, `test_m2_harness_gates.py` |
| Evidence rejects secrets | A closed schema, plus a writer guard over every encoding the artifact scanner knows. A refused document is not written. | `test_m2_harness_evidence.py` |
| The crash marker cannot be fabricated | The child writes it only in place of `_commit`, after the ledger shows its own completed HTTP 200 token response. It is authenticated with a nonce passed on stdin and never stored. The parent also requires exit code 86, a data directory with no committed session, and zero candidate hits on disk. | `test_m2_harness_crash.py` |

## 2. Runbook (only after this PR is merged and audited)

Do this only after this PR is merged with the user's approval, the independent cross-audit and
the architect audit have passed, and, for step 6, the user has given an explicit go-ahead.

0. Check out the approved `main` SHA with nothing changed or untracked (`git status` is empty).
   Pick a campaign directory **outside** the repository and outside every ordinary ICBM data
   directory, and a new campaign ID such as `m2-campaign-01`.
1. `python scripts/m2_acceptance.py init --campaign-dir <DIR> --campaign-id <ID>`
   This creates the ledger (token 0/8, seller 0/6), two fresh migrated data directories
   (`data/baseline`, `data/crash`) and the campaign's scoped OS keyring entries.
2. `python scripts/m2_acceptance.py serve --campaign-dir <DIR>`
   Open the printed loopback URL and, on the baseline data directory:
   * save the SmartStore application credentials;
   * record the reviewed contract freshness `CURRENT`;
   * record the A0 attestation of the API groups as shown in Commerce API Center.

   No phase is open, so any SmartStore request (for example pressing 연결) is refused before
   send and recorded. Never run `icbm serve` on a campaign directory.
3. Set `ICBM_SMARTSTORE_RENEWAL_MARGIN_S=600`, then run
   `python scripts/m2_acceptance.py preflight --campaign-dir <DIR> --campaign-id <ID>`.
   The preflight checks:
   * CI 5/5 green at HEAD, read with `gh`;
   * the regression floor: pytest, ruff, mypy, and M0 acceptance with `--visual`;
   * a complete DRY rehearsal at this SHA;
   * baseline readiness: credentials saved, freshness `CURRENT`, auth not `READY`, A0 shown as ◐ and still the same after a restart, zero egress;
   * the budget at 0/0/0.

   When everything passes, it stops at **`AWAITING_REAL_PROVIDER_APPROVAL`**.
4. Post the preflight summary (`preflight/status.json`) to Issue #46 and **wait for the user's
   explicit go-ahead**.
5. `python scripts/m2_acceptance.py approve --campaign-dir <DIR> --campaign-id <ID> --approved-sha <SHA>`
   Type `APPROVE <ID> <sha12>` at the prompt. The approval is valid for 2 hours and is consumed
   by one run.
6. `python scripts/m2_acceptance.py real --campaign-dir <DIR> --campaign-id <ID> --approved-sha <SHA> --acknowledge <ID> --real-provider`
   Every gate is printed. The run then goes T1/A1 → the operator types `BIND <last 4>` → A1b →
   A2 after a restart → recheck → T4a (T4b only after typing `SPEND T4B`) → T5/A3 → closeout.
   The observed account is shown on this terminal only. Do not tee it into a file.
7. On any stop, nothing is retried. `status` shows the ledger. Resuming repeats an
   already-planned operation, so it needs the architect to authorize that bounded recovery, and a
   new `approve`. The budget already spent stays spent.
8. For closeout, copy `evidence/m2-campaign-evidence.json` into this directory in the
   evidence PR, and fill in `M2-EVIDENCE-TEMPLATE.md`.

`python scripts/m2_acceptance.py` with no arguments runs the DRY rehearsal into a temporary
directory. Every automated test uses DRY campaigns.

## 3. What evidence records, and never records

Recorded (all sanitized):

* the campaign ID and mode;
* the exact HEAD, the approved SHA and the tree state;
* times;
* per-request ledger rows: step label, endpoint ID, HTTP status, latency, outcome;
* the caller's own sanitized `smartstore.request` lines: result class, transmission phase, remote outcome, provider trace marker as the caller sanitized it, credential and session generation;
* committed-session metadata: token type, `expires_in`, generations;
* HMAC fingerprints of the application and the account, under a per-campaign key kept in the campaign's OS keyring scope;
* identity match results;
* the crash marker and verdicts;
* scan counts, reconciliation, regression results, and one row per slot.

Never recorded:

* the client secret or the full client id;
* a bearer token, any hash of one, or an Authorization header;
* a raw `accountUid` or `accountId`, or any PII.

The observed account is shown to the operator on the terminal and exists only in memory.

## 4. Known limits, for the audit

* **Operator discipline outside the harness.** `icbm serve` on a campaign directory would use the
  product's unscoped keyring and the default, ungated caller. The scoped keyring means that app
  would not see the campaign's credentials. Reconciliation also flags any call that the ledger did
  not reserve: it checks every caller log line of both data directories against the ledger.
* **The T4a candidate token** is scanned for only inside the crash child, at the boundary, and
  only as counts. Whether T5 returned the same token as T4a is not recorded, because that would
  need token-derived material from the crashed process. `expires_in` is recorded for both.
* **`APPROVAL_MAX_AGE` = 2 hours** is a harness choice, open to review.
* **Contingency calls** are reachable only through a new approved invocation. The harness never
  retries automatically, because PR-A's retry budgets are policy-pending (ERRORS §25 Q6).
