# ICBM-NEW Glossary

Status: **CANONICAL**. Authorized by `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md` §5 (D3, ACCEPTED)
and created by the Gate 0 canonical sync at main `260dea4d7f54561d40ced291f99025c7c41f0ed1`.

This file fixes the **name** of a field or a concept, so two documents cannot drift into two names
for one thing, and one name cannot come to mean two things. It decides nothing new: every entry
points at the contract that owns it. Where this file and an ADR disagree, the ADR is right and this
file is a defect to be fixed.

It is not complete. It holds the names that have already been confused at least once.

---

## 1. Accounts and identity

| name | what it is | owner |
| --- | --- | --- |
| `marketplace_account_id` | **the canonical ICBM identity of one marketplace account.** Every registration, capability, execution-scope and target-policy row is scoped by it, and it is the required spelling for every account-scoped owner and every new account identity or foreign-key field. | `docs/adr/0014-…readback.md` §2, `docs/adr/0015-…metadata.md` §2, `app/connect/account_models.py` |
| `account_id` (M4 pricing) | **a pricing-context discriminator, not an account-identity owner.** The pre-existing M4 `PricingContextInput.account_id` and its persisted `pricing_snapshots.account_id` say whether a price context varies by account: for a registration target it holds the canonical `marketplace_account_id`, or `None` when the fee and policy are explicitly account-invariant (ADR-0013 §7, ADR-0015 §2). It is a frozen local spelling of the M4 contract and is not renamed. | ADR-0013 §7, `app/products/pricing.py` |
| `account_id` (SmartStore wire) | **a provider request field.** In the SmartStore token contract it is the field an own-store (`type=SELF`) application must not send (`docs/platforms/smartstore/ENDPOINT_MATRIX.md` §Provenance). It is never an ICBM identity. | SmartStore AUTH contract |

**No new generic `account_id` identity field.** The M4 pricing discriminator is not a precedent: a
new owner, column or contract field that identifies a marketplace account is named
`marketplace_account_id`.
| `accountUid`, `accountId` | provider-side identifiers a SmartStore account is known by upstream. They are recorded as provider facts and mapped to the canonical account; they never replace it. | `docs/platforms/smartstore/ACCOUNT_IDENTITY.md` |
| `icbm_product_id` | the canonical product identifier — the Canonical v3.1 `ProductGroup` identifier. There is no second product root. | ADR-0013 §1 |

A registration row therefore carries `marketplace_key + marketplace_account_id`, and reaches the
canonical product **through its registered Items**, not through a copied `icbm_product_id` column
(`docs/ARCHITECTURE.md` §5).

The frozen `docs/architecture/CANONICAL-V3.1.md` writes `account_id` in its own model text. That
document is frozen history and is never edited to match this one: read its `account_id` as the
canonical account identifier the implementation spells `marketplace_account_id`.

## 2. Listing identity and seller codes

| name | what it is | owner |
| --- | --- | --- |
| **listing identity** (`listing_identity`) | the deterministic identity of **one provider-listing unit**, derived from stable local identity. It is ICBM's own correlation identity for that unit. | ADR-0014 §7 |
| `seller_product_code` | the listing identity **as sent** on a registration. The same value, recorded on the durable result. | ADR-0014 §7, `app/register/models.py` |
| `sellerManagementCode` | the **provider wire field** a seller-controlled code is carried in. It is the provider's field name, not an ICBM concept, and the provider guarantees it no uniqueness. | SmartStore product contract |
| `registration_item_key` | the stable correspondence key of one Item inside a listing, created before CREATE and proven to round-trip. Display labels are never identity. | ADR-0014 §6, invariant M5-06 |

**None of these is a provider uniqueness proof.** A deterministic code is the key ICBM asks with;
it does not make a lookup deterministic, and a lookup that returns nothing proves no remote absence
(ADR-0014 §17.2). What may settle an ambiguous outcome is fixed by ADR-0014 §10, not by this file:
a provider read-back or a provider lookup under an adopted contract, transmission-precluded
evidence, or another explicitly reviewed machine or provider proof — never a seller code alone and
never an operator's word.

## 3. Not-knowing: `UNKNOWN`, `REVIEW_REQUIRED`, `FAILED`

These three are confused most often. They are not degrees of the same thing.

| name | axis | meaning |
| --- | --- | --- |
| `UNKNOWN` | **remote outcome** (`RemoteOutcome`: `APPLIED_PROVEN` \| `NOT_APPLIED_PROVEN` \| `UNKNOWN`) | the external mutation may or may not have happened; ICBM has no proof either way. It forbids a blind replay, and an unresolved UNKNOWN CREATE keeps its conflict scope closed. It is settled only by the evidence ADR-0014 §10 admits, never by an operator's word. A `RegistrationAttempt` and a `RegistrationIntent` both carry this axis; it has no `FAILED` value. |
| `UNKNOWN` | **Intent state** (`IntentState`) | the Intent's CREATE outcome is not proven; it must be reconciled, never resent. Constrained to `remote_outcome = UNKNOWN`. |
| `UNKNOWN` | **error class** (`ErrorClass`) | the cause of a failure could not be classified. A cause, never a workflow state and never a replay permission. |
| `REVIEW_REQUIRED` | **workflow state / review work** | a human must decide. It is the fail-closed landing place for ambiguous source evidence, a stale or conflicting dependency, and an unresolved `UNKNOWN`. `error_class = REVIEW_REQUIRED` is **not** `workflow_state = REVIEW_REQUIRED` (`docs/ARCHITECTURE.md` §8). |
| `FAILED` | **Intent state** (`IntentState`: `PREPARED` \| `SENT` \| `CONFIRMED` \| `UNKNOWN` \| `FAILED`) | the CREATE is **proven not applied**. The database constrains `state = FAILED` to `remote_outcome = NOT_APPLIED_PROVEN`, so an ambiguous outcome can never be recorded as `FAILED`; a retry is a new attempt of the same Intent (ADR-0014 §8, §10). |

Two other axes use similar words and decide none of the above: a **job attempt**
(`SUCCEEDED` \| `FAILED` \| `INTERRUPTED`) and a **job state** (`QUEUED`, `RUNNING`,
`RETRY_SCHEDULED`, `SUCCEEDED`, `DEAD`), both ADR-0005. A failed job attempt or a dead job never
classifies a remote outcome and never makes an Intent `FAILED`.

**The Korean UI labels — `재확인필요`, `검토 필요`, `확인 필요` and their siblings — are display text
for one of the server-owned states above.** They are never a new backend truth, never a fourth
state, and the UI never computes one: it renders what the owner decided (`docs/ARCHITECTURE.md`
§3, `CLAUDE.md` §5.1).

## 4. Adoption and execution words

| name | meaning |
| --- | --- |
| `ADOPTED` / `NOT_ADOPTED` | whether ICBM has frozen an endpoint's operational contract. `NOT_ADOPTED` means no network call at all, even when the provider documents the endpoint. Documented by the provider is not adopted by ICBM (`ENDPOINT_MATRIX.md` §1, §2). |
| `UNVERIFIED` (a capability) | the capability has not been proven by a real operation. `product_registration.write` is `UNVERIFIED` until a bounded real CREATE is proven by read-back (ADR-0014 §16). |
| `DRY_RUN` / `LIVE` | the global external-write mode. `LIVE` is currently refused outright by the execution-mode owner (`M0_DRY_RUN_ONLY`), and needs its own authorization contract before any bounded canary (`docs/ARCHITECTURE.md` §13). |
| `PENDING` (an acceptance record) | the milestone is not accepted. Merged PRs, green CI and offline runs never change it; only an architect-accepted acceptance run on the exact merged main SHA does. |
