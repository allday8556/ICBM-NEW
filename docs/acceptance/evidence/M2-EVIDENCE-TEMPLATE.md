# M2 SmartStore CONNECT — acceptance evidence record (template)

Status: **TEMPLATE**. The evidence/closeout PR (Issue #46 §5) copies this file, fills in every
field from the campaign's `m2-campaign-evidence.json`, and never edits that JSON by hand.

## Provenance (M2.md §7)

```text
Acceptance evidence commit:   <sha>
Runtime code last changed at: <sha>   (494ac4eac098b26bf3581919e3587fe760311cb2 unless a later reviewed runtime fix)
<runtime>..<evidence>:        <expected docs/evidence-only delta>
<evidence>..HEAD:             <closeout-only delta, if any>
Campaign ID:                  <m2-…>
Approved SHA (git.head):      <sha>
Evidence file:                docs/acceptance/evidence/<campaign-id>.json  (SHA-256 <…>)
Observed range:               <started_at> – <finished_at>
```

## Campaign closeout

| Endpoint | Hard cap | Used |
| --- | ---: | ---: |
| `SMARTSTORE_AUTH_TOKEN` | 8 | `<budget.used>` |
| `SMARTSTORE_SELLER_ACCOUNT` | 6 | `<budget.used>` |
| any other endpoint | 0 | 0 |

- Crash-attempt sub-budget: `<crash_attempts_used>` of 2 (T4a `<verdict>`, T4b `<verdict or not used>`)
- Requests blocked before send by the budget gate: `<blocked_before_send, or none>`
- `campaign_outcome`: `<COMPLETED | BUDGET_EXHAUSTED | STOPPED_FOR_CONTRACT_REVIEW | …>`
- Artifact scan (local / evidence): `<hits> / <hits>`
- Ledger vs caller log: `<reconcile.match>`
- Regression floor at recheck and at final: `<results>` (M0 `<n>/<n>`)

## Slots (one row each)

| `evidence_id` | `evidence_kind` | `status` | `required_for_m2_acceptance` | `observed_at` | `application_fingerprint` | `credential_generation` | `session_generation` | `endpoint_ids` | `request_count` | `evidence_ref` | `result_summary` | `contract_impact` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `SMARTSTORE-R0-TOKEN` | PROVIDER_MEASURED | | true | | | | | | | | | |
| `SMARTSTORE-R0-SELLER-ACCOUNT` | PROVIDER_MEASURED | | true | | | | | | | | | |
| `SMARTSTORE-R0-FIRST-TOKEN-CRASH` | PROVIDER_MEASURED | | true | | | | | | | | | |
| `SMARTSTORE-A0-PERMISSION` | OPERATOR_ATTESTED | | true | | | | | [] | {} | | | |
| `SMARTSTORE-R0-TOKEN-REISSUE-WINDOW` | PROVIDER_MEASURED | DEFERRED_LONG_HORIZON | false | | | | | [] | {} | M2.md#5.3 | | NONE |
| `SMARTSTORE-R0-APP-REAUTH` | PROVIDER_MEASURED | BLOCKED_BY_TIME | false | | | | | [] | {} | M2.md#5.5 | | NONE |

The harness proposes each `status`. A slot is accepted only after the architect and the
independent cross-audit verify it and the user explicitly approves the closeout (Issue #46 §4 G.22).

## Identity (no raw identity is ever recorded)

- Baseline account fingerprint (A1 = A1b = A2): `<fingerprint>`
- A3 comparison on the crash data directory: `<MATCH | MISMATCH | NOT_COMPARABLE>` (`<reason>`)
- Crash data directory left unbound, auth not READY there: `<crash.recovery.crash_dir_bound = false>`

## Final summary (M2.md §7)

```text
Required R0: <n>/3
A0:          <n>/1
Long-horizon R0:
  TOKEN-REISSUE-WINDOW = DEFERRED_LONG_HORIZON (non-gating) or PASS
  APP-REAUTH           = BLOCKED_BY_TIME (non-gating) or PASS
Contract reviews opened from measurements: <n>
```
