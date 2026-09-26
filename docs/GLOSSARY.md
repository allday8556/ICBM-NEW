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

**Review work** (ADR-0016). A `REVIEW_REQUIRED` condition stays the truth of the owner that derives
it. The review owner only indexes it:

| name | meaning |
| --- | --- |
| `ReviewItem` | a durable **index** of human work over one owner-derived condition; never a source of that owner's truth |
| condition key | the server-computed digest of kind × producer × canonical scope × subject × owner reason code |
| review key | the condition key × the exact owner source identity (revision, fingerprint or evidence identity); one row per review key |
| `OPEN` / `RESOLVED` / `SUPERSEDED` | the ReviewItem lifecycle, decided by reconciliation against current owner truth; `SUPERSEDED` means the same condition moved to a new source identity |
| `NOT_WIRED` | a review kind with no fully reconciled producer; its count is unknown, never zero |
| current coverage | a `WIRED` kind whose full reconciliation succeeded in the current process run and has no known indexing failure unrecovered; only then is its count authoritative |
| `CURRENT` / `NOT_CURRENT` (a review count) | whether a kind's open count is authoritative: `CURRENT` only when **every** producer that can emit the kind is wired and its coverage current, and the owner has not moved since the watermark's pass; `NOT_CURRENT` when all are wired and one is not current. Only `CURRENT` carries a count (G2-C) |

## 3a. Collection profiles and extraction identity

Three different things are called a "profile". They are never one another (ADR-0017 §4):

| name | what it is | owner |
| --- | --- | --- |
| `SupplierProfile` | the **CONNECT** profile of a supplier: key, display name, base URL, auth flag, egress hosts, request policy. Not an extraction profile | ADR-0007 |
| `CollectionProfile` | the **COLLECT access envelope**: product path form, policy paths, explicit image hosts, safe query keys, frozen limits, transport. Repository-reviewed, never widened at run time, never profile data | ADR-0010 §3, §9 |
| `ExtractionProfileRevision` (EPR) | an immutable, content-addressed revision of one supplier's **interpretation** — identity rule, vocabularies, image-role rules, hook bindings — pinning a closed set of PTRs by digest; validated and activated as one bundle | ADR-0017 §3 |
| `PageTemplateRevision` (PTR) | an immutable, content-addressed revision of **one page shape** of a supplier: its signature and per-field locator rules; never activated alone | ADR-0017 §3 |

| name | what it is | owner |
| --- | --- | --- |
| `CORE` / `COVERAGE` | the two field levels of `FIELD_REGISTRY` (`FieldLevel`). Every non-CORE field is a `COVERAGE` field. ADR-0010 §7's historical label "Source coverage" / "source-coverage" means `COVERAGE`; no new contract uses it | ADR-0010 §7, ADR-0017 §4 |
| `comparability_key` | the tagged semantic tuple that decides drift comparability: `("CODE", extractor_revision)` for a revision without profile provenance, derived on read and never stored or backfilled; `("ADAPTIVE", extractor_revision, profile_schema_version, extraction_profile_digest)` otherwise | ADR-0017 §5.3; ADR-0013 §3 as amended |
| `extraction_semantics_id` | the stored, collision-resistant digest of an Adaptive revision's semantic tuple; an integrity and indexing aid, never the comparability decision by itself | ADR-0017 §5.2 |
| `HOOK_REVISION` / `HOOK_FINGERPRINT` | a supplier hook manifest's semantic revision (bound by the EPR) and implementation fingerprint (provenance and validation freshness only, never semantic identity) | ADR-0017 §6.2 |
| promotion key | `(hook_point, target)` of a hook binding; the G6 counting unit, and when two or more suppliers share it, an architect promotion decision is required | ADR-0017 §6.3, §6.4 |
| `ValidationSample` | an operator-verified, sanitized, product-scoped structured snapshot replayed by profile validation, cut by an independent capture owner and never by the profile it validates; keeps product controls and admissible embedded data as parsed literals, excludes non-authoritative regions; never a whole authenticated page. A sample with a digest-only (truncated) block is `SAMPLE_TRUNCATED` and never supports a `PASS` | ADR-0017 §7.3, V8 |
| `shadow_enabled_for_run` | a run's shadow decision, frozen at its first product-read reservation; the shadow step and the Phase C denominator both read only it | ADR-0017 §10.1 |
| `VALIDATED` (a profile) | derived, never stored: a `PASS` validation run exists for the exact freshness tuple, implementation fingerprints included | ADR-0017 §7.1 |

## 4. Adoption and execution words

| name | meaning |
| --- | --- |
| `ADOPTED` / `NOT_ADOPTED` | whether ICBM has frozen an endpoint's operational contract. `NOT_ADOPTED` means no network call at all, even when the provider documents the endpoint. Documented by the provider is not adopted by ICBM (`ENDPOINT_MATRIX.md` §1, §2). |
| `UNVERIFIED` (a capability) | the capability has not been proven by a real operation. `product_registration.write` is `UNVERIFIED` until a bounded real CREATE is proven by read-back (ADR-0014 §16). |
| `DRY_RUN` / `LIVE` | the global external-write mode. `LIVE` is currently refused outright by the execution-mode owner (`M0_DRY_RUN_ONLY`). Its authorization contract is ADR-0018, whose owners exist provider-zero (Gate 3 area 1) while LIVE stays refused; the mode alone is never authority for a mutation (`docs/ARCHITECTURE.md` §13). |
| LIVE grant | the only authority for a marketplace mutation (ADR-0018 §3): durable, audited and server-owned, for one mutation stage and one exact unit of it — ASSET (preparation revision, candidate fingerprint, artifact set, asset profile) or CREATE (Snapshot, Intent, idempotency key) — one marketplace and canonical account, a finite window and a finite mutation budget; never unit-less or a wildcard. `EXPIRED`, `REVOKED` and `EXHAUSTED` are terminal. It decides no readiness, adoption, compliance or provider truth. |
| protected-write brake | the server-owned kill switch (ADR-0018 §4): `ENGAGED` or `RELEASED`, durable, fail-closed (unreadable means `ENGAGED`); it stops every new mutation and never rewrites history or an `UNKNOWN`. Distinct from the ADR-0014 §26 execution-scope send brake. |
| `ASSET_MUTATION_READY` / `CREATE_MUTATION_READY` | the derived readiness of one canary mutation stage (ADR-0018 §10), a mandatory send-time layer: before an upload, and after the freeze before a CREATE; never permission |
| ASSET upload attempt | one durable, server-owned record of one image upload (ADR-0018 §3.4), required before any upload: started before transmission in the same atomic unit that consumes the ASSET grant budget, terminal once as `APPLIED_PROVEN`, `NOT_APPLIED_PROVEN` or `UPLOAD_UNKNOWN`; an unfinished one after a crash is `UPLOAD_UNKNOWN`, and only `APPLIED_PROVEN` yields a known provider asset. Its provenance (grant, preparation revision, candidate, profile, `derivation_id`, local artifact kind, multipart file name, MIME or type metadata, contract or adoption label) is separate from its **replay-conflict key**, the conservative wire boundary — exactly the marketplace, canonical account, wire method, host and path, and the outbound content digest — and a started, `UPLOAD_UNKNOWN` or `APPLIED_PROVEN` attempt blocks every fresh upload with that key whatever the provenance. The owner exists provider-zero (`asset_upload_attempts`, Gate 3 area 1); no sender is wired. |
| restore drill / restore proof | ADR-0018 §7: a WAL-consistent backup of the canonical database restored into a separate fresh root, with integrity, the Alembic head and the restored schema itself (every table, index, trigger and view equal to what the shipped migrations build at that head) proven, and every element of one stage's chain compared by identity and state. A PASSED drill is a restore proof for exactly its stage and target digest at its schema head; it is stale once that state moves. Owned by `restore_drills` (Gate 3 area 2). |
| visual acceptance record | ADR-0018 §9: a reviewed, PASSED populated visual and responsive acceptance report — every required surface at every required viewport, no server-owned state hidden, truncated, covered or clipped, no external request — recorded append-only in `visual_acceptances` (Gate 3 area 3) only by `icbm live record-visual-acceptance`, at the report's exact commit, with the reviewer and the GitHub comment that accepted it. `VISUAL_ACCEPTANCE_RECORDED` holds only while such a record matches the running code digest and schema head. |
| running code digest | Gate 3 area 3: the SHA-256 identity of everything the application executes and serves — the `app` and `integrations` packages, the served UI directory and the dependency pins — computed by the running process itself (`app/core/code_identity.py`). Documents and evidence never move it; any code change does. The git commit is recorded beside it as provenance. |
| evidence-retention proof | ADR-0018 §8: a recorded check that every canary-scope durable table keeps a delete guard that refuses every delete, that every trigger on those tables (the forward-only triggers included) is exactly what the shipped migrations build (semantics, not names), and that no deleting job exists; `EVIDENCE_RETENTION_READY` holds only while the live checks still match a PASSED proof (`retention_proofs`, Gate 3 area 2). |
| canary eligibility (non-regulated) | the first canary's own proof that its reviewed category metadata places it outside every regulated category (ADR-0018 §5); never a `COMPLIANCE PASS` |
| `PENDING` (an acceptance record) | the milestone is not accepted. Merged PRs, green CI and offline runs never change it; only an architect-accepted acceptance run on the exact merged main SHA does. |
