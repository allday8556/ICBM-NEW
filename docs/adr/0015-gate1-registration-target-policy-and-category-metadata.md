# ADR-0015 — Gate 1: the durable registration target policy, the operator-reviewed category metadata, and the Gate 1 acceptance boundary

Status: **ACCEPTED** — decided by the architect kickoff `5784108069` (2026-09-22 UTC). This is G1-0
of Gate 1 (Issue #89, on the preparation proposal `5783937360`), on exact main
`644af5b51c69e0e338b247fbc3f313f52152d87f`.
- It records the architect decisions D1–D5 of that kickoff as contract, before any schema. The
  decisions were made by the kickoff; this document does not re-open them.
- **Its implementation authority becomes effective only after this exact contract PR is audited,
  independently cross-audited and merged**, and even then only slice by slice (§5).

**It authorizes no migration, model, service, route, UI, test for new behaviour, provider adapter,
endpoint adoption or provider call.** Each implementation slice it names (G1-A to G1-D) needs its
own authorization in GitHub, and any migration is authorized only with the slice that needs it.

Decision owner: Architect (ChatGPT). Sources:
- the Gate 1 kickoff `5784108069` (decisions D1–D5, the slice order and the exit boundary) and the
  preparation proposal `5783937360` it answers;
- the Gate 0 record: `ROADMAP.md` §14.1–§14.3 and `docs/acceptance/M5.md` §9, closed by `5783937028`;
- ADR-0014 §3 (derived preflight), §4 (category and disclosure), §10 and §17.2 (the reconcile rule
  and the evidence verdict, unchanged here), §18 (AI is optional), §21 (Settings and policy
  ownership), §22 (the registration UI) and §24 (execution safety);
- ADR-0013 §7 (the explicit M4 pricing context) and ADR-0011 (retention of sanitized evidence);
- the existing contracts this ADR gives durable owners to: `TargetPolicy`,
  `RegistrationPolicySource`, `CategoryMetadata` and `RegistrationMetadataSource` in
  `app/register/policy.py`.

Recorded by: Claude Code. The number was confirmed free in `docs/adr/`, on `main` and in every
remote branch immediately before writing.
Date: 2026-09-23
Related:
- ADR-0014: this ADR adds owners behind two of its read contracts. It changes no sentence,
  invariant or ruling of ADR-0014.
- ADR-0013: M4 keeps the pricing owner. This ADR only says which context the target policy hands it.

---

## Context

At `644af5b5` the REGISTER preflight is complete as a derivation but has nothing durable to derive
from on the target side. Production wiring binds an empty `StaticRegistrationPolicy()` and an empty
`StaticRegistrationMetadata()`, so every evaluation stops at `REGISTER_TARGET_POLICY_MISSING` or
`CATEGORY_METADATA_MISSING`. ADR-0014 §21 already assigns the account-scoped registration values to
"Settings and platform policy", but no Settings persistence owner exists. The category metadata was
expected to come from provider endpoints, which PR-D left `NOT_ADOPTED` for want of evidence.

The operator path around those owners is also missing: no Draft is created from a chosen product, no
price is computed and pinned for a target, the product DB has no list or detail, and the COLLECT
screen does not submit. Gate 1 closes that path **locally and provider-zero**. It does not approach
a real write.

## Decision

### 1. What Gate 1 is

- Gate 1 makes the REGISTER segment reachable by an operator, end to end, **with zero provider
  writes**: from a collection started in the UI to a server-derived preflight that no longer stops
  at the two local owner gaps.
- The kickoff's dispositions are binding:

| decision | disposition |
| --- | --- |
| D1 target policy | a new durable, revisioned owner (§2); its migration is authorized only with G1-A |
| D2 category metadata | a new durable, operator-reviewed owner (§3); **no endpoint adoption** in Gate 1 |
| D3 `ReviewItem` | **out of Gate 1**; Gate 2 (the human review path) |
| D4 `ComplianceGate` | **out of Gate 1**; a later pre-LIVE gate |
| D5 bounded LIVE authorization | **out of Gate 1**; a later dedicated gate |

### 2. The durable registration target policy (D1)

**Scope.** One policy per `marketplace_key × marketplace_account_id` — the canonical account of
`docs/GLOSSARY.md` §1.

**Revisions.**
- A policy is a sequence of **append-only, immutable revisions**. The current policy points at
  exactly one revision.
- **The server creates every revision**: its `policy_revision` identity, its fingerprint over the
  canonical sanitized content, and its audit record. **A client never supplies or invents a
  `policy_revision`.**
- A Settings edit appends a revision. It never overwrites or deletes an earlier one, and history
  survives a restart unchanged.
- A revision that would not materialize a valid `TargetPolicy` is refused whole: there is no partial
  revision and no default filled in by the server.

**What a revision owns** — the target and platform policy inputs ADR-0014 §21 already assigns, and
nothing else:
- the category taxonomy revision;
- the M4 pricing context inputs and their fee-table and pricing-policy version references;
- the account-scoped template identities (shipping, returns and exchanges, and the other templates a
  category may require);
- the sanitizer profile version;
- the asset policy;
- the duplicate-proof policy and its lookup-key kinds;
- the server-owned authoring revision references `TargetPolicy` carries
  (`category_mapping_revision`, `detail_composition_revision`).

**What it never owns.** No Product or ProductFacts, no `PricingSnapshot` and no price, no readiness
or reason code, no `RegistrationSnapshot`, Intent, Attempt or Registration, no capability, auth or
permission truth, no provider truth, and no category metadata (§3).

**The pricing context it hands to M4.** The revision's pricing context is exactly an M4
`PricingContextInput` (ADR-0013 §7). Its `marketplace_key` is the policy's marketplace, and its
`account_id` field is either the policy's `marketplace_account_id` or `None`, which states
explicitly that the fee and policy do not vary by account; the existing preflight rule
`TARGET_PRICING_CONTEXT_INVALID` keeps refusing anything else. **M5 never prices**: the M4 pricing
owner prices under this context, and only its `PricingSnapshot` identities are pinned.

**How it is read.**
- Production binds a durable `RegistrationPolicySource` that materializes the existing `TargetPolicy`
  contract from the current revision. `StaticRegistrationPolicy` stays a test and offline fixture
  only.
- The preflight keeps reading the policy on every evaluation, so a revision appended after a
  preflight changes the next evaluation's dependency fingerprint, and a Snapshot built from the
  older evaluation is refused as stale (ADR-0014 §3).
- **A frozen Snapshot keeps the exact policy revision it froze** (ADR-0014 §21). A later revision
  never changes an earlier Snapshot, Intent or job payload.

**The Settings surface (G1-A).**
- Settings may edit **only this explicitly supported policy surface**. The rest of the Settings
  screen does not become editable by implication.
- Whether a Settings field is editable is **server-owned and scope-accurate**, and the screen
  renders it. The inherited `M0 · 읽기 전용 — 설정 저장 계약이 아직 연결되지 않았습니다` label is
  replaced by that server-owned state in G1-A, never by a client decision.
- The write path is an application contract into this owner; the UI computes no revision, no
  fingerprint and no validity.

### 3. The operator-reviewed category metadata (D2)

**Scope.** One metadata owner keyed by **`marketplace_key × taxonomy_revision × category_id`**,
able to materialize the existing `CategoryMetadata` contract. `marketplace_key` is part of the key:
the durable source never serves one marketplace's metadata for another, however G1-B shapes the
read path.

**Revisions and the current revision.**
- Revisions are append-only and immutable, like §2.
- **For each key the owner holds an explicit current-revision selection** that names exactly one
  revision. "Current" is that selection, never "the newest reviewed one" and never "the latest
  row" inferred at read time.
- Changing the selection and the history is durable and restart-stable. How it is recorded
  transactionally is a G1-B implementation detail within this rule.

Each revision carries:
- a provenance and evidence reference — **which reviewed evidence the rules came from**, as a
  sanitized reference, never a retained raw provider payload (ADR-0011);
- whether it is reviewed and, when it is, who reviewed it; and when it was recorded;
- its `metadata_revision` identity and fingerprint, created by the server;
- whether the category is a leaf and whether it is registrable;
- the required attribute rules and the notice type with its field rules, each with its
  `missing_status` and length limit;
- whether "상세페이지 참조" is allowed, per field;
- the option rule;
- the templates the category requires;
- every other field the existing `CategoryMetadata` contract carries.

**What "operator-reviewed" means.** The operator **accepts reviewed evidence**; it is **not
permission to guess a provider requirement**.
- A rule the evidence does not prove cannot be promoted into a reviewed revision.
- A category whose required rules cannot be proven stays unreviewed, and its preflight fails closed.
- An AI suggestion is never reviewed metadata and never satisfies a rule (ADR-0014 §4, §18).

**Fail-closed use.**
- **The preflight reads the current revision** — the selection above — on every evaluation, and
  nothing else.
- **Only a reviewed current revision may satisfy the preflight.** No current revision answers
  `CATEGORY_METADATA_MISSING`. A current revision with `reviewed = false` answers
  `CATEGORY_METADATA_UNREVIEWED`, exactly as the preflight already does, and **the preflight never
  falls back to an older reviewed revision** of the same key.
- Every evaluation and every Snapshot freezes **the exact metadata revision it evaluated**. A change
  of the current selection changes the next evaluation's dependency fingerprint, so an older
  candidate is stale; a frozen Snapshot keeps the revision it froze.
- The taxonomy revision is the one the account's target policy names (§2); metadata of another
  taxonomy revision never stands in for it.

**No second category truth.** If official provider category, attribute, standard-option or notice
endpoints are later evidenced and adopted — separately, under their own review — they may only
**feed new revisions into this same owner**. They never create a second category truth.
`StaticRegistrationMetadata` stays a test and offline fixture only; the production binding changes
only in the separately authorized G1-B.

**No endpoint adoption in Gate 1.** `SMARTSTORE_CATEGORY_LIST`, `SMARTSTORE_CATEGORY_READ`,
`SMARTSTORE_PRODUCT_ATTRIBUTE_LIST`, `SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES`,
`SMARTSTORE_STANDARD_OPTIONS`, `SMARTSTORE_NOTICE_TYPES` and `SMARTSTORE_NOTICE_TYPE_READ` stay
`NOT_ADOPTED`, and Gate 1 makes no provider call to them.

### 4. What stays out of Gate 1 (D3–D5)

- **`ReviewItem` (D3).** Its persistence and producers belong to Gate 2. Gate 1 creates **no second
  review queue**, and a zero dashboard review count is never evidence that no review work exists
  (`docs/ARCHITECTURE.md` §9). Every Gate 1 `REVIEW_REQUIRED` result stays visible on the screen
  that derives it.
- **`ComplianceGate` (D4).** It stays a pre-LIVE blocker. **Gate 1 never claims a compliance PASS.**
  Gate 1 fixtures and acceptance use a clearly non-regulated product only to exercise the path; that
  choice is not a compliance verdict, and the first real canary stays forbidden until the
  compliance decision is closed.
- **Bounded LIVE authorization (D5).** `M0_DRY_RUN_ONLY` and `M0_LIVE_FORBIDDEN` stay exactly as
  they are. Gate 1 performs **zero marketplace mutations**.

### 5. The Gate 1 slices

After this ADR is audited, cross-audited and merged, the slices are authorized **one at a time,
never in parallel**, each as its own PR with its own exact-head audit:

| slice | content |
| --- | --- |
| G1-A | the durable target-policy owner (§2), its Settings write path, and the scope-accurate, server-owned Settings editability and label |
| G1-B | the durable operator-reviewed category-metadata owner (§3) and its reviewed write and read path, provider-zero |
| G1-C | the product DB list, search, pagination, detail and registration-target selection |
| G1-E | the COLLECT UI submit, wired to the existing `POST /api/v1/collect/collections` |
| G1-D | the Draft command path: selected Product → the account's target policy → M4 `PricingService.price` → the persisted `PricingSnapshot` identities → `create_draft` / `add_draft_item` |

- G1-A and G1-B come first. G1-C and G1-E may be swapped after them. G1-D comes last, after the
  owners it needs and a selectable product path exist.
- **G1-D adds no second price owner**: it calls the M4 pricing owner and pins what that owner
  recorded, through the existing registration store writers.

### 6. The Gate 1 acceptance boundary

Gate 1 closes when, **from a fresh session and with zero provider writes**, this operator path
works and returns the same durable state after a reload and after a restart:

```text
COLLECT UI
→ one product collected
→ Product DB list / detail
→ choose the registration target
→ the durable account target policy
→ reviewed category metadata
→ M4 pricing
→ PricingSnapshot pin
→ RegistrationDraft
→ preparation
→ server-derived preflight
→ reload / restart returns the same durable state
```

- For the reviewed canary setup, Gate 1 closes the two **local** preflight blockers
  `REGISTER_TARGET_POLICY_MISSING` and `CATEGORY_METADATA_MISSING`.
- **It does not promise that only provider-side blockers remain.** `ReviewItem`, `ComplianceGate`,
  bounded LIVE authorization and the CREATE and lookup evidence blockers of ADR-0014 §17.2 stay
  explicitly open for later gates.
- Its evidence is recorded in GitHub against the exact main SHA it ran on. **Gate 1 evidence is not
  M5 acceptance**, and `docs/acceptance/M5.md` stays `PENDING`.

### 7. What this ADR does not decide

- table names, columns, enum spellings, route paths, payload shapes or screen layout — each slice
  decides them within this boundary and its own review;
- the `ReviewItem`, `ComplianceGate` and LIVE authorization contracts;
- any provider endpoint adoption, including the category and notice reads;
- anything about CREATE, SEARCH, the reconcile rule or the image upload, which stay exactly as
  ADR-0014 states them.

## Invariants

```text
G1-01  a target policy is scoped by marketplace_key × marketplace_account_id; its revisions are append-only, server-created, fingerprinted and audited, and the current policy points at exactly one immutable revision
G1-02  a client never supplies or invents a policy_revision or a metadata_revision
G1-03  a target policy revision owns only the ADR-0014 §21 inputs; it holds no Product, price, PricingSnapshot, readiness, Snapshot, Intent, Attempt, capability or provider truth
G1-04  the target policy's pricing context is an M4 PricingContextInput for its own marketplace, with account_id equal to its marketplace_account_id or None; M5 never prices
G1-05  category metadata is keyed by marketplace_key × taxonomy_revision × category_id; its revisions are append-only and immutable, and each key has an explicit, durable current-revision selection
G1-06  the preflight reads only the current revision; none is CATEGORY_METADATA_MISSING, an unreviewed one is CATEGORY_METADATA_UNREVIEWED, and it never falls back to an older reviewed revision
G1-07  operator review accepts reviewed evidence; a rule the evidence does not prove is never promoted, and an AI suggestion is never reviewed metadata
G1-08  a later adopted provider category source feeds new revisions into the same owner; there is never a second category truth
G1-09  every evaluation and every frozen Snapshot records the exact policy and metadata revisions it evaluated; a later revision or selection changes only later evaluations
G1-10  StaticRegistrationPolicy and StaticRegistrationMetadata are test and offline fixtures only
G1-11  Gate 1 adopts no endpoint, makes no provider call and performs zero marketplace mutations; M0_DRY_RUN_ONLY is unchanged
G1-12  Gate 1 creates no second review queue, claims no compliance PASS, and does not accept M5
```

## Consequences

- The two local preflight blockers get owners without inventing provider truth: the policy is the
  operator's configuration, and the metadata is reviewed evidence with its provenance.
- Two migrations are expected, one with G1-A and one with G1-B, each authorized with its slice.
- The category metadata can later move to provider evidence without a second truth or a migration of
  meaning: only the source of new revisions changes.
- Gate 1 leaves M5 exactly as blocked as ADR-0014 §17.2 left it; it removes local gaps only.

## References

- Issue #89: Gate 1 kickoff `5784108069`, preparation `5783937360`, Gate 0 closeout `5783937028`.
- `docs/adr/0014-smartstore-register-idempotency-readback.md` §3, §4, §10, §17.2, §18, §21, §22, §24.
- `docs/adr/0013-m4-canonical-product-contract.md` §7.
- `docs/adr/0011-marketplace-readback-retention-boundary.md`.
- `app/register/policy.py` (`TargetPolicy`, `RegistrationPolicySource`, `CategoryMetadata`,
  `RegistrationMetadataSource`).
- `ROADMAP.md` §14, `docs/acceptance/M5.md` §9, `docs/GLOSSARY.md`.
