# M2 SmartStore CONNECT — acceptance evidence record (closeout)

Status: **ACCEPTED**. This is the M2 closeout record (Issue #46 §5). It was accepted through the independent cross-audit, the architect's full-diff/CI review and the user's explicit approval of the closeout merge (PR #51). It is filled in from `M2-EVIDENCE-TEMPLATE.md`. Every value comes from the committed evidence files below, and those files are never edited by hand.

| File | What it is |
| --- | --- |
| `m2-campaign-02.json` | The campaign's sanitized evidence, byte-for-byte as the harness wrote it (schema `m2-campaign-evidence/v1`). SHA-256 `cb48a2e57f4aeec8ee80343509fc0e1a1302d060a416679e2f02a63bdfa5438f` |
| `m2-campaign-02-preflight.json` | The campaign's zero-provider preflight status file, which is the A0 slot's `evidence_ref`. SHA-256 `4e2f5a1202ac64c183b2a7bdf315943d5fbf86cc58568e631fd94047a426aaa1`, the digest the ledger recorded in its `PREFLIGHT_PASSED` event |

The absolute path of the campaign directory, raw account identifiers, bearer or token-derived values, the client secret, the full client id and Authorization headers appear in none of these files.

## Provenance (M2.md §7)

```text
Acceptance evidence commit:   the merge commit of closeout PR #51 (a commit cannot name itself)
Runtime code last changed at: 494ac4eac098b26bf3581919e3587fe760311cb2 (product runtime: app/, integrations/)
Evidence produced at:         011e6fdcf2398e7ddae2db2f2e1a7a660a53789f (approved SHA = HEAD; tree clean)
494ac4e..011e6fd:             docs (#47), acceptance harness with its tests and docs (#49, #50), mypy config;
                              no app/ or integrations/ change
011e6fd..closeout:            docs/evidence only (closeout PR #51)
Campaign ID:                  m2-campaign-02 (REAL)
Observed range:               2026-09-14T22:22:39.876Z – 22:31:30.741Z
```

## Campaign closeout

| Endpoint | Hard cap | Used |
| --- | ---: | ---: |
| `SMARTSTORE_AUTH_TOKEN` | 8 | 3 |
| `SMARTSTORE_SELLER_ACCOUNT` | 6 | 4 |
| any other endpoint | 0 | 0 |

- **Crash-attempt sub-budget:** 1 of 2. T4a was `BOUNDARY_EXERCISED`, T4b was not used, and there is no T4c.
- **Requests blocked before send by the budget gate:** none.
- **`campaign_outcome`:** `COMPLETED`.
- **Transmission:** every request was reserved in the ledger before it was sent, and every one returned HTTP 200. No provider trace marker was returned on any response.
- **Artifact scan (counts only), 0 hits:**
  * local artifacts: 202 files, scanned for the client secret, the client id and both committed bearers;
  * evidence: the same values plus both observed account identifiers;
  * the committed form was scanned again in closeout PR #51, also 0 hits.
- **Ledger vs caller log:** an exact match: token 3/3, seller 4/4, refusals 0/0.
- **Regression floor at the recheck and at the final stage:** pytest, ruff check, ruff format, mypy and M0 acceptance `--visual` all passed. M0 scored 24/24 both times.
- **Evidence schema:** valid. It was re-validated on the committed file.
- **`MEASURED_CONTRACT_REVIEW_REQUIRED`:** none is open from this campaign.

| Step | Phase | Endpoint | HTTP | Latency (ledger / caller) | What it proved |
| --- | --- | --- | --- | --- | --- |
| T1 | BASELINE_CONNECT | `SMARTSTORE_AUTH_TOKEN` | 200 | 409.1 / 419.4 ms | Bearer, `expires_in` 6757. It became current only as committed session generation 1. |
| A1 | BASELINE_CONNECT | `SMARTSTORE_SELLER_ACCOUNT` | 200 | 353.4 / 361.7 ms | The account was observed **unbound** (NOT_BOUND) and shown only on the operator's terminal. |
| A1b | BASELINE_BIND | `SMARTSTORE_SELLER_ACCOUNT` | 200 | 353.1 / 360.9 ms | The operator typed the exact `BIND` line. The bind-time fresh read matched, and the binding was committed: bound, auth READY. |
| A2 | BASELINE_RESTART | `SMARTSTORE_SELLER_ACCOUNT` | 200 | 439.3 / 448.5 ms | After the restart, persisted READY was **not trusted** (NOT_READY before the read). READY returned only after the fresh protected read, and the committed session was reused with no token request. |
| T4a | CRASH_T4A | `SMARTSTORE_AUTH_TOKEN` | 200 | 399.5 / 408.0 ms | A fresh crash data directory. The child died at the post-response, pre-commit boundary. |
| T5 | CRASH_RECOVERY | `SMARTSTORE_AUTH_TOKEN` | 200 | 360.7 / 369.7 ms | After the restart, nothing was trusted. The token was committed as crash-directory session generation 1. |
| A3 | CRASH_RECOVERY | `SMARTSTORE_SELLER_ACCOUNT` | 200 | 357.6 / 366.0 ms | The crash data directory stayed unbound and not READY. |

**FIRST-TOKEN-CRASH.** The parent accepted the boundary because every check agreed:

- the exit code was 86;
- the nonce-authenticated `CRASH_BOUNDARY_REACHED` marker was present, naming reservation seq 5 and the same pid as the ledger's completed HTTP 200 token row;
- the crash directory held no committed session: no session file, session generation 0, no commit audit row;
- the candidate scan at the boundary read 13 files with 0 hits.

After the restart, T5 and A3 both returned 200. The **A3 acceptance-only identity comparison was MATCH (`SAME_ACCOUNT`)**: A3's non-reversible fingerprint equals the baseline bound-account fingerprint that A1/A1b established and A2 revalidated, under the same application fingerprint and credential generation 1. The crash data directory was never bound, and nothing there was promoted to READY.

## Slots (one row each)

| `evidence_id` | `evidence_kind` | `status` | `required_for_m2_acceptance` | `observed_at` (UTC, 2026-09-14) | `application_fingerprint` | `credential_generation` | `session_generation` | `endpoint_ids` | `request_count` | `evidence_ref` | `result_summary` | `contract_impact` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `SMARTSTORE-R0-TOKEN` | PROVIDER_MEASURED | **PASS** | true | 22:22:41.337 – 22:22:41.752 | `fc40be8bae9e…` | 1 | 1 | `SMARTSTORE_AUTH_TOKEN` | token 1 | `m2-campaign-02.json` requests(T1), sessions(baseline) | SELF token issued (Bearer, `expires_in` 6757); current only once committed as generation 1 | NONE |
| `SMARTSTORE-R0-SELLER-ACCOUNT` | PROVIDER_MEASURED | **PASS** | true | 22:22:41.792 – 22:22:58.037 | `fc40be8bae9e…` | 1 | 1 | `SMARTSTORE_SELLER_ACCOUNT` | seller 3 | `m2-campaign-02.json` requests(A1, A1b, A2), identity | A1 observed the account unbound; A1b bound the operator-confirmed account on a fresh read; A2 proved it after a restart; one account fingerprint throughout | NONE |
| `SMARTSTORE-R0-FIRST-TOKEN-CRASH` | PROVIDER_MEASURED | **PASS** | true | 22:27:13.220 – 22:27:15.891 | `fc40be8bae9e…` | 1 | 1 (crash dir) | both | token 2, seller 1 | `m2-campaign-02.json` crash, identity | boundary exercised on T4a; T5/A3 recovered after the restart; A3 MATCH; crash dir unbound | NONE |
| `SMARTSTORE-A0-PERMISSION` | OPERATOR_ATTESTED | **PASS** (limited strength) | true | preflight 22:18:18.518 | `fc40be8bae9e…` | 1 | — | none | none | `m2-campaign-02-preflight.json` | operator-attested groups persisted across a restart as LIMITED (◐), never machine-verified; 0 provider requests | NONE |
| `SMARTSTORE-R0-TOKEN-REISSUE-WINDOW` | PROVIDER_MEASURED | DEFERRED_LONG_HORIZON | false | — | — | — | — | none | none | M2.md §5.3 | separate long-horizon session; the token-lifetime observation below supports it but does not complete it | NONE |
| `SMARTSTORE-R0-APP-REAUTH` | PROVIDER_MEASURED | BLOCKED_BY_TIME | false | — | — | — | — | none | none | M2.md §5.5 | not practically observable; never manufactured | NONE |

The full 64-hex fingerprints are in `m2-campaign-02.json`. They are HMACs under a per-campaign key that stays in the campaign's OS credential store and is never committed.

## Identity (no raw identity is ever recorded)

- **Baseline account fingerprint** (A1 = A1b = A2): `6a971e099875fb4bbde842e0a2fe1a8e691f0783ed3c076cd38ddc487e72d495`.
- **A3 on the crash data directory:** MATCH (`SAME_ACCOUNT`), under the same application fingerprint and credential generation.
- **Crash data directory:** unbound (no `provider_account_uid`), auth not READY, one committed session (generation 1).

## Supporting observation: token lifetime

This is a supporting observation. It is not a contradiction of any contract, and it does not complete `SMARTSTORE-R0-TOKEN-REISSUE-WINDOW`.

| Token response | Observed at (UTC, 2026-09-14) | `expires_in` | Computed expiry |
| --- | --- | ---: | --- |
| `m2-campaign-01` T1 (history only) | 21:15:19.353 | 10799 | 00:15:18.353 |
| `m2-campaign-02` T1 | 22:22:41.756 | 6757 | 00:15:18.756 |
| `m2-campaign-02` T4a (uncommitted candidate, from the crash marker) | 22:27:13.651 | 6485 | ≈ 00:15:18.6 |
| `m2-campaign-02` T5 | 22:27:15.515 | 6483 | 00:15:18.515 |

What was measured:
- every response arrived while more than 30 minutes of lifetime remained;
- `expires_in` decreased from one response to the next;
- the computed expiries fall at essentially one absolute instant.

This is consistent with the NAVER P0 rule, now stated in `AUTH.md` §7: for the same resource, an existing token is returned while 30 minutes or more remain, and a new token may be issued inside the less-than-30-minute window. The harness records no token-derived value, so **byte-identity of the returned material was not measured and is not claimed**. The less-than-30-minute side, old-token validity and the persistence-policy review remain pending in `AUTH.md` §24.3.

## History: `m2-campaign-01` (terminal, NOT_ACCEPTED)

- **How it ended:** in `STOPPED_OPERATOR_DECLINED` after T1 and A1, both HTTP 200. At the A1b confirmation the operator typed only the four-character suffix, and the harness then treated any non-matching input as a decline. PR #50 fixed that behavior. A1b was never sent, and nothing was bound.
- **Real calls:** token 1, seller 1. Its A1 fingerprint and partial slot results are **not** used for any slot in this record. The sanitized record is Issue #46 comment 5670974832.
- **Cumulative real calls across both campaigns at closeout:** token 4, seller 5.

## Known limitation (display-only, non-gating)

A preflight gate that passes can still print its failure hint as its detail. In `m2-campaign-02-preflight.json`, `campaign_registered` reads `no matching registration` and `dedicated_campaign_dir` has a null detail, even though both have `result: PASS`. The `result` field is authoritative. This is display-only and was not changed for the closeout.

## M2 CONNECT final truth (M2.md §11)

```text
auth               = READY (baseline data directory; committed binding + fresh matching proof)
write_scope        = READY with OPERATOR_ATTESTED / LIMITED evidence (◐, never ●)
write              = UNVERIFIED
contract_freshness = CURRENT (operator-reviewed, not NAVER-measured)

SMARTSTORE-R0-TOKEN-REISSUE-WINDOW = DEFERRED_LONG_HORIZON
SMARTSTORE-R0-APP-REAUTH           = BLOCKED_BY_TIME
APPLICATION_REAUTH_REQUIRED automatic detection = DISABLED
AUTH.md verified_at = null
PERMISSIONS_SCOPES.md verified_at = null
```

## Final summary (M2.md §7)

```text
Required R0: 3/3
A0:          1/1
Long-horizon R0:
  TOKEN-REISSUE-WINDOW = DEFERRED_LONG_HORIZON (non-gating)
  APP-REAUTH           = BLOCKED_BY_TIME (non-gating)
Contract reviews opened from measurements: 0
```
