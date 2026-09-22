# ADR-0014 — M5 SmartStore REGISTER contract: owner boundary, provider-listing units, derived preflight, immutable Snapshot, idempotent CREATE, UNKNOWN reconcile and read-back proof

Status: **ACCEPTED** 2026-09-19. This is PR-A of Issue #89 (kickoff `5740316498`, PR #90).
- GPT exact-head re-audit PASS review `5255222454` and the independent Claude AI exact-head cross-audit PASS `5740785909`, both on the audited head `c6d3426034333061932d3df1bc85d3c657a8e7e2`.
- It incorporates the architect addendum `5740352676` (rulings R1–R4) as binding decisions.
- It is amended for the PR #90 GPT review `5255157251` (HOLD): four contract blockers B1–B4 (see "Review amendments").
- It lands on main with the merge of PR #90.
- It is extended by the architect decision `5749504280` (PR-E review `5260076445`): §26 and invariants M5-25–M5-27 add the REGISTER execution-scope send brake, the one owner PR-E was missing, together with the migration that owner needs.
- It is extended by the architect decision `5751540323` (PR-F review `5261280389`): §27 and invariant M5-30 add the durable registration preparation — the operator's authored inputs, append-only, with the provenance of the Snapshot a revision froze — together with the migration that owner needs (`0018`).
- It is amended by the architect decisions `5765557497` and `5765663972`: §17.1 adopts `SMARTSTORE_PRODUCT_IMAGE_UPLOAD` alone, bounded, with no durable upload owner and no LIVE authority.

Apart from the two migrations §26 and §27 name, it authorizes no schema, migration, runtime code, UI, AI call, supplier request or marketplace call; the single endpoint adoption it authorizes is the bounded IMAGE UPLOAD of §17.1, which authorizes no real upload. **No real SmartStore request of any kind is authorized by it.** Each implementation PR (PR-B to PR-F) needs its own authorization, and a real CREATE needs a separate, explicit user authorization of a bounded scope.

Decision owner: Architect (ChatGPT). Sources:
- the Issue #89 body (the M5 umbrella) and the architect kickoff `5740316498`, which authorized PR-A only and listed the twenty decisions this ADR freezes;
- the architect addendum `5740352676`, which accepted four contract points (R1–R4) and the contract tests they require;
- `docs/architecture/CANONICAL-V3.1.md` §2.4–§2.7, §3, §6.5–§6.8, §8–§10, §11.2–§11.5 and §14 (the frozen registration model);
- ADR-0004 (registration automation guardrails), ADR-0008 (error taxonomy), ADR-0011 (raw read-back retention boundary), ADR-0012 (AI runtime) and ADR-0013 (the M4 contract and its §11 M4/M5 boundary);
- the SmartStore platform contracts: `docs/platforms/smartstore/ENDPOINT_MATRIX.md` §4, `ERRORS.md` §2.2–§3 and the M2 capability owner (`app/connect/marketplace/capability.py`);
- Issue #61 (detail composition), Issue #56 (Image Studio) and Issue #80 ruling `5738886070` (supplier resale-price advisory).

Recorded by: Claude Code. The number was confirmed free in `docs/adr/`, on `main` and in every open branch immediately before writing.
Date: 2026-09-19
Related:
- ADR-0011: this ADR states the REGISTER sanitizer and safe-query-key contract ADR-0011 §3 requires (§15);
- ADR-0013: M4 keeps every product-side owner. This ADR changes nothing in it.

---

## Context

M4 is accepted (`docs/acceptance/M4.md`): the canonical Product (the v3.1 `ProductGroup`), its Items, current source bindings, context-scoped `PricingSnapshot`s, derived image lineage with operator selection and exact-binary QA, and layered readiness exist and are proven offline. SmartStore CONNECT is accepted (M2). **When this ADR was written** every SmartStore product, image, category, attribute, option and notice endpoint was `NOT_ADOPTED` planning metadata (`ENDPOINT_MATRIX.md` §4), and `product_registration.write` was `UNVERIFIED`.

That starting state has since moved only where an amendment moved it: PR-D adopted the two product read-backs, §17.1 adopted the bounded image upload, and everything else — product CREATE, the duplicate-lookup search, the category, attribute, option and notice reads — remains `NOT_ADOPTED`. `product_registration.write` is still `UNVERIFIED`. The current adoption facts are `ENDPOINT_MATRIX.md` §4 and the adapter registry, never this paragraph.

M5 registers one canonical product to SmartStore and proves, by read-back, that the marketplace recorded exactly what ICBM sent. The dangerous failure is not a failed registration but a **second listing**: a replay, a restart, a retry after an ambiguous result, a changed Snapshot or a partial option set can each create one. This ADR freezes, before any schema, the contract that makes those impossible.

This ADR is a contract. It names entities, owners, states and invariants. It does not fix tables, columns, enum spellings, wire formats or algorithms except where it says so.

## Decision

### 1. Owner boundary

**M4 owns** (ADR-0013, unchanged): `SourceProduct` and its current source revision, the `ProductGroup` and its membership, `ListingComposition` and the Item, `SourceBinding`, `PricingSnapshot` per Item and pricing context, `SourceAsset`, `DerivedImageArtifact` with its derivation lineage, the image selection pointer, exact-binary QA, base readiness and per-context pricing readiness.

**M5 owns**:
- `MarketplaceListingDraft` and `DraftListingItem`, and the Draft invariants;
- category mapping and the marketplace taxonomy revision it names;
- the marketplace/account registration policy inputs;
- registration preflight (derived, §3);
- `RegistrationSnapshot` and `RegistrationItemSnapshot`;
- `RegistrationBatch`, `RegistrationIntent` and `RegistrationAttempt`;
- `MarketplaceRegistration` and `MarketplaceRegistrationItem`;
- `DuplicateOverride` and the marketplace/account duplicate lookup;
- the marketplace asset profile it requests, marketplace asset upload, provider asset identity and publication reference;
- REGISTER read-back evidence within ADR-0011 (§15).

**No second truth.** M5 copies or redefines no Product, Item, binding, price, image lineage or readiness. It consumes those owners and freezes **copies** of the exact values it sent into its own immutable Snapshot (§6). Only the M4 Pricing owner calculates a selling price; M5 never re-decides one, and no listing-level price exists.

### 2. Draft, listing shape and the provider-listing unit

**The Draft.**
- A Draft is scoped by `marketplace × account × draft_id` (v3.1 §8).
- Every `DraftListingItem` references an existing M4 Item. M5 creates no Item.
- **Within one Draft, `(group_id + composition_signature)` is unique** (v3.1 §3.1, §8.1).
- The listing shape is one of `SINGLE_LISTING_WITH_OPTIONS | SEPARATE_LISTINGS | SELECTED_OFFERS`.
- For `SINGLE_LISTING_WITH_OPTIONS`, every Item resolves to one SmartStore category, and the SmartStore adapter provides a **deterministic compatibility check**. AI never decides whether Items can be combined into one option listing.
- Price stays Item-owned: each Item carries its exact M4 `PricingSnapshot` for the target context. There is no listing-level price flattening.

**The provider-listing unit (ruling R3).** A Draft may hold many Items, but CREATE idempotency belongs to the **provider listing**: one listing SmartStore creates.
- **Before any execution, the Draft is resolved into provider-listing units. Each unit has exactly one immutable `RegistrationSnapshot` and one CREATE `RegistrationIntent`.**
- `SINGLE_LISTING_WITH_OPTIONS`: one unit holding N Items as N options (one Snapshot, one Intent).
- `SEPARATE_LISTINGS`: one unit per listing, so N Items give N Snapshots and N Intents.
- `SELECTED_OFFERS`: the provider-listing unit(s) of the selected Items are determined first; then one Snapshot and one Intent are created per unit.
- A Draft never resends a subset of its Items as a CREATE. A unit is sent, reconciled and verified as a whole (§11, §12).

### 3. Registration preflight is derived

- Preflight is **marketplace/account scoped** and **derived** from current canonical dependencies. **No stored `REGISTERABLE`**, `registerable = true` or equivalent truth exists.
- It combines:
  - M4 base readiness;
  - M4 pricing readiness under the target SmartStore/account pricing context;
  - the category and taxonomy revision;
  - required platform attributes, required options and product-information disclosure (§4);
  - shipping, returns and exchange policy or template references (§21);
  - the selected publication images, their QA and any prepared provider assets (§5);
  - the final selling price from the Item's `PricingSnapshot`;
  - prohibited and sold-out policy;
  - the duplicate lookup and any `DuplicateOverride` (§13);
  - the unresolved-UNKNOWN conflict check (§10);
  - SmartStore-specific required fields.
- States are `READY | REVIEW_REQUIRED | BLOCKED | DUPLICATE | STALE`. Within one target context the status is the highest of **`BLOCKED > DUPLICATE > STALE > REVIEW_REQUIRED > READY`**, and every reason is returned. Precedence never merges results across targets.
- **Its dependency fingerprint and rule version are explicit.** The fingerprint names every input above: the M4 readiness fingerprints, the pricing snapshot, the category, taxonomy and policy revisions, the selected artifacts and their QA, the prepared provider assets and the duplicate-lookup evidence.
- **Freshness is fail-closed.** A Snapshot is frozen only from a `READY` preflight whose fingerprint is current. Before a CREATE is sent, the preflight is evaluated again; if any dependency differs from the Snapshot's recorded fingerprint, that Snapshot is **not sent** and its unit is `STALE` until a new Snapshot is frozen from current truth.
- A check that cannot be performed, or whose evidence is inconclusive, is never `READY`.

**The pre-asset candidate gate (review `5255157251`, B2).** A marketplace asset upload is a protected marketplace mutation (§5). Nothing is uploaded for a provider-listing unit that could still be refused for any other reason:

```text
non-asset preflight candidate      mutation-free; every dependency except the provider asset identity
→ READY?                           otherwise no upload
→ marketplace asset preparation / upload      bound to the candidate dependency fingerprint
→ final preflight                  every dependency, the provider asset identity included
→ READY?                           otherwise no Snapshot
→ RegistrationSnapshot freeze
```

1. **Before any marketplace asset upload, every non-provider-asset preflight dependency of the provider-listing unit passes a mutation-free candidate evaluation.**
2. **If any non-asset result is `BLOCKED`, `DUPLICATE`, `STALE` or `REVIEW_REQUIRED`, no upload is permitted.**
3. **The only dependency allowed to be unresolved at asset-preparation time is the provider asset identity itself.**
4. **The asset preparation and upload are bound to that candidate's dependency fingerprint**, and the upload evidence records it.
5. After upload, the final preflight re-evaluates every dependency, the provider asset included, and must be `READY` before the Snapshot is frozen.
6. **If any dependency changed between the candidate and the final preflight, no CREATE follows from that upload.** The unit returns to a new candidate evaluation. An asset already uploaded stays marketplace-asset state only; it is reused only where §5 allows reuse and a final preflight proves it still corresponds.

The candidate is an evaluation, like every preflight. **No new readiness truth is stored for it.** When CREATE needs no provider-issued asset, the final preflight is the only evaluation.

```text
non-asset candidate status    marketplace asset upload
READY                         permitted, bound to the candidate fingerprint
REVIEW_REQUIRED               forbidden
STALE                         forbidden
DUPLICATE                     forbidden
BLOCKED                       forbidden
```

### 4. Category and product-information disclosure

- Platform and category required fields come from **reviewed SmartStore metadata** under an adopted endpoint contract and a recorded taxonomy revision. Nothing is guessed or hardcoded per product.
- **No exhaustive "fill every notice field" rule.** Fields the platform or category requires are satisfied with supported source or operator facts. Optional or non-required values are **never fabricated** into facts.
- A "refer to the detail page" representation may be used **only** where the adopted SmartStore policy officially permits it, and only by policy.
- No product-specific category or field hardcode exists. An operator-confirmed category or field value is a valid input; an AI ranking alone is not (§18, ADR-0004).

### 5. Marketplace publication assets (ruling R1)

**Marketplace-sized binaries remain in the M4 derived-image lineage.** No second image truth is created in M5.

```text
M5 target/profile requirement
→ M4 derived-image owner creates the immutable DerivedImageArtifact + lineage + QA
→ M5 uploads that exact artifact
→ M5 owns provider asset identity / publication reference / REGISTER evidence
```

- **M4 owns** the source and derived binary identity, the transformation lineage and the exact-artifact QA. A resize, a format normalization or any other marketplace-specific binary transformation is an M4 derivation.
- **M5 owns** the requested marketplace asset profile, the upload mutation, the provider asset identity and reference, the payload's publication choice and read-back verification.
- **M5 does not create a second derived-image lineage owner**, and never transforms a binary itself.
- **Before the final Snapshot.** When CREATE requires a provider-issued image identity, asset preparation and upload happen **before** the final `RegistrationSnapshot` is frozen, and **only after the non-asset preflight candidate is `READY` (§3)**. The Snapshot freezes both the **exact local artifact identity** and the **exact sanitized provider asset identity or reference** actually sent. Final preflight rechecks that the prepared provider asset still corresponds to the currently selected, QA-passed artifact.
- **A later transformation or upload never mutates a historical Snapshot.**
- **An upload is a marketplace mutation.** It falls under the same protected-write and audit boundary as CREATE (§24).
  - An upload success is not a listing success.
  - **An ambiguous upload outcome is never represented as a known provider asset identity.**
  - Where the provider offers a safe reuse or lookup identity, it is reconciled or reused rather than uploaded again blindly.
  - An orphan asset left by a later CREATE failure is marketplace-asset state. It is never Product truth and never a reason to mark a registration confirmed.
- **No supplier hotlink is ever published.** Published content never depends on a supplier-hosted URL.
- `SourceAsset` stays immutable. M5 implements no Image Studio (Issue #56).

### 6. The immutable Snapshot: what was actually sent

A `RegistrationSnapshot` and its `RegistrationItemSnapshot[]` are **immutable** once frozen. They record what one provider-listing unit sends:

```text
RegistrationSnapshot               immutable, one per provider-listing unit
  registration_snapshot_id
  marketplace_key / account_id
  listing_shape
  draft_id / draft_revision
  listing identity                 the deterministic seller-side identity of this unit (§7)
  preflight rule version + dependency fingerprint
  category / taxonomy mapping revision
  platform policy and Settings revisions (shipping, returns, templates)
  detail composition revision (§19)
  sanitizer / safe-query-key profile version (§15)
  payload_hash                     SHA-256 of the sanitized canonical evidence representation (§15), never of wire bytes
  asset hashes                     the exact local artifact SHA-256s
  payload                          the exact outbound values, sanitized (§15)

RegistrationItemSnapshot           immutable
  registration_item_key            stable, created before the Snapshot is frozen
  group_id_at_registration
  group_membership_revision_id
  listing_composition_id
  source_product_facts_revision_id_at_registration
  pricing_snapshot_id_at_registration
  source_snapshot                  a copy of the source binding at registration, never a reference to it
  publication assets               exact local artifact identity + sanitized provider asset identity/reference
  outbound values                  final name, tags, attributes, option values and notice values sent
```

- **A historical Snapshot never follows later current-state changes**: not a Product, Item, binding, price, image, category or policy change.
- The mutable current Item and the immutable sent Snapshot are different owners.
- A Snapshot is never updated. A different payload is a new Snapshot.

### 7. Stable registration-item correspondence

- **`registration_item_key` exists before CREATE.** It is generated when the Snapshot is frozen and never changes.
- **Display labels are not identity.** An option name, a product name, a price or an order position is never a correspondence key.
- **Seller-controlled codes are deterministic from stable local identity, never from a mutable product name.** The listing identity and each `registration_item_key` derive from the provider-listing unit's stable identity and the Item key (`group_id + composition_signature`).
- **One listing identity never names two provider listings that may coexist.** An intentional duplicate (§13) or a re-registration (§14) gets a new listing identity unless the adopted SmartStore contract proves reuse legal and unambiguous.
- **The wire representation is deferred** to endpoint adoption (PR-D): which SmartStore field carries the listing identity and each `registration_item_key`. Before any real CREATE, that representation must be **proven to round-trip** through SmartStore read-back. If it cannot round-trip, the mapping contract is resolved first and no real CREATE happens.

### 8. Intent, idempotency and concurrency

```text
RegistrationIntent
  intent_id
  registration_batch_id
  registration_snapshot_id
  marketplace_key / account_id
  operation                        CREATE (UPDATE and DELETE are not authorized in M5's first vertical)
  idempotency_key                  deterministic from marketplace, account, operation and the exact Snapshot
  state                            PREPARED | SENT | CONFIRMED | UNKNOWN | FAILED
  marketplace_product_id           the provider identity once obtained or reconciled
```

- **One durable Intent per operation and exact Snapshot.** The same marketplace, account, operation and exact Snapshot always resolve to the same Intent and idempotency identity, across restarts.
- **Concurrent sends cannot compete.** Two concurrent SEND requests for one Intent never create two Intents or two in-flight Attempts. PR-B enforces this with database uniqueness and a service invariant, not by UI convention.
- **Attempts are append-only execution history** (§9).
- **A retry never creates a new Snapshot or a new Intent.** It is a new Attempt of the same Intent.
- A `RegistrationBatch` groups Intents for canary, audit and batch operations (v3.1 §10.5–§10.6). Its summary is derived (§12).

### 9. Attempts, error classes and automatic retry

```text
RegistrationAttempt                append-only
  attempt_id / intent_id / attempt_no
  request_payload_hash             SHA-256 of the sanitized canonical request representation (§15), never of wire bytes
  response status / sanitized response digest
  error_class / error_code
  remote_outcome                   APPLIED_PROVEN | NOT_APPLIED_PROVEN | UNKNOWN
  ambiguous_result                 = (remote_outcome == UNKNOWN), a projection, never written independently
  resolved_by                      READ_BACK | LOOKUP | USER: who recorded the resolution, never itself the evidence (§10)
  started_at / finished_at
```

- `error_class` (the cause) and `remote_outcome` (whether the mutation happened) are independent axes (`ERRORS.md` §2). `ambiguous_result` is derived from `remote_outcome` (`ERRORS.md` §3).
- **Automatic retry** is permitted only for `TRANSIENT` and `RATE_LIMITED` (ADR-0004, ADR-0008), **and only when `remote_outcome = NOT_APPLIED_PROVEN`**. A transient cause never implies a safe replay: `TRANSIENT` with `remote_outcome = UNKNOWN` is reconciled, never resent.
- `UNKNOWN`, `VALIDATION`, `POLICY_BLOCKED` and any write whose outcome cannot be proven are never retried automatically.
- A retry obeys the failure budget and rate limiter scoped to `marketplace × account × endpoint group` (v3.1 §11.2–§11.4). That scope's pause and its accepted release are owned by §26.

### 10. An UNKNOWN write outcome, and its conflict scope (ruling R2)

**Never resend blindly.** Once a CREATE may have been transmitted, and its outcome is not proven:

```text
UNKNOWN
→ deterministic reconcile / lookup / read-back (by the Snapshot's listing identity)
→ one of:
   CONFIRMED                               the listing exists and verifies (§11)
   NOT_APPLIED_PROVEN / remote absence proven
   REVIEW_REQUIRED, unresolved
```

- CREATE is retried only after `NOT_APPLIED_PROVEN` is established or remote absence is deterministically proven under the listing identity.
- **Unresolved ambiguity stays `UNKNOWN` with a `REVIEW_REQUIRED` workflow overlay. It is never silently `FAILED`.** `FAILED` requires `NOT_APPLIED_PROVEN`.

**Who may resolve an UNKNOWN (review `5255157251`, B3).** `resolved_by = USER` may record a workflow or operator decision, or acknowledge and accept evidence. It is never the evidence:
- **An operator assertion alone never establishes `NOT_APPLIED_PROVEN` or remote absence, and never releases an unresolved UNKNOWN conflict scope.**
- **Changing `UNKNOWN` to `NOT_APPLIED_PROVEN` or absent requires evidence that satisfies the adopted, operation-specific proof contract**: a read-back, a lookup by the listing identity, transmission-precluded evidence, or another explicitly reviewed machine or provider proof.
- **The operator may be the actor who records or accepts that evidence, but is not itself the evidence.** A resolution record always references the evidence it relies on.

```text
resolution evidence                                       may establish NOT_APPLIED_PROVEN / remote absence
provider read-back under the adopted contract             yes
provider lookup by the listing identity                   yes
transmission-precluded evidence (no transport handoff)    yes
another explicitly reviewed machine/provider proof        yes
operator assertion alone (resolved_by = USER)             no
```

**The conflict scope.** A changed Snapshot does not escape an unresolved, possibly applied CREATE:

```text
marketplace × account × overlapping target ProductGroup / listing identity
+ unresolved UNKNOWN
→ no new CREATE Intent
```

- **An unresolved UNKNOWN CREATE blocks every new CREATE Intent in its conflict scope, even for a new Snapshot.** A new Snapshot has a new idempotency identity, but idempotency-key uniqueness alone cannot prove the old UNKNOWN created no listing.
- **Overlap with any affected group is sufficient.** For a listing that contains several groups, a new CREATE covering any one of them is blocked.
- The groups are those the unresolved Snapshot names (`group_id_at_registration`). Where a group has since been merged or split, the groups its recorded lineage now maps to also overlap; an unclear successor counts as overlapping (fail-closed, v3.1 §6.5–§6.8).
- The same deterministic listing identity also overlaps.
- **A non-overlapping group in the same marketplace and account is not blocked** merely because another group's Intent is UNKNOWN.
- **Only after the old ambiguity is resolved as not applied or absent** may a new CREATE Intent for the same conflict scope be created.
- **A `DuplicateOverride` never releases an unresolved UNKNOWN.** An override permits an intentional duplicate listing; it proves nothing about an ambiguous one.
- **PR-B enforces this with a database and service invariant**, not by UI convention.

### 11. Read-back is the success proof

```text
CREATE 2xx  ≠  registration success
```

A registration succeeds only when **all** hold:
- the provider product identity is obtained or reconciled;
- read-back succeeds;
- **the returned listing is compared to the immutable `RegistrationSnapshot`**, never to the current editable Draft or the current Item;
- every critical field passes its comparison contract.

**Comparison classes** (initial; each is versioned with its normalizer):

| field | comparison |
| --- | --- |
| sale price | `EXACT`, per Item against `pricing_snapshot_id_at_registration` |
| item/option set | `EXACT` by `registration_item_key`: count and correspondence |
| category | `EXACT` |
| published state | `EXACT` against the explicitly expected state |
| name | `NORMALIZED_COMPARE` |
| tags | `SET_COMPARE` where the provider round-trips them |
| images | an explicit ordered/tolerance policy over the sent provider asset references |
| required attributes | an exact, policy-defined comparison |

- The read-back normalizer and the comparison contract carry versions, and each verification records them.
- **`MarketplaceRegistration` and `MarketplaceRegistrationItem` are the durable result after verification passes.** They are never created or updated before, and they are never the source of the comparison.
- Provider option order or option-name normalization never breaks correspondence: `registration_item_key` does.

**A subset read-back of `SINGLE_LISTING_WITH_OPTIONS` (ruling R3).** One SmartStore listing with N options is one CREATE Intent.

```text
provider listing exists
+ read-back Item set != RegistrationSnapshot Item set
→ the whole Intent is NOT CONFIRMED
→ no "successful Items + resend missing Items as CREATE"
```

- **A subset is a registration mismatch of the whole Intent, never a per-Item partial success.** The Intent is not `CONFIRMED`: its outcome is applied (`APPLIED_PROVEN`) and its verification is a mismatch under `REVIEW_REQUIRED`. It is never `FAILED`, because the listing exists.
- **The missing Items are never resent as a CREATE**, and no second listing is created for them.
- If the provider identity is known, it is preserved on the Intent together with the sanitized mismatch evidence. The listing is a live listing for duplicate preflight (§13).
- **Any repair is an explicit later UPDATE or reconcile operation with its own reviewed contract and Intent. It is not a CREATE retry.** No such operation is authorized by this ADR.

### 12. Partial success belongs to the provider-listing unit (ruling R3)

- **`SEPARATE_LISTINGS`.** The Draft is split, before execution, into one Snapshot and one Intent per provider listing. Then batch-level partial success is allowed:
  - **a `CONFIRMED` Intent stays `CONFIRMED` and is never resent**;
  - a `NOT_APPLIED_PROVEN` or safely retryable failure may retry **that listing** under the retry contract (§9);
  - an `UNKNOWN` stays blocked pending reconcile (§10);
  - **one listing's success or failure never rewrites a sibling Intent.**
- **`SELECTED_OFFERS`** follows the same rule: the provider-listing units come first, then one immutable Snapshot and Intent per unit.
- **`PARTIAL` is a derived summary.** A batch or Draft `PARTIAL` is computed from its child Intents' states for the UI and for batch reporting. **No authoritative batch or Draft state is stored that could disagree with its child Intents.**

### 13. Duplicate preflight and `DuplicateOverride`

- Duplicate detection is **not an ICBM-database lookup alone.** The target SmartStore account's preflight uses adopted provider lookup evidence: the deterministic seller product code, a valid barcode or GTIN, other official seller lookup keys, and a normalized name only as a weaker signal where appropriate.
- The ICBM side also counts: a live or unresolved listing ICBM knows (a verified `MarketplaceRegistration`, an applied Intent with a known provider identity, or an unresolved UNKNOWN in the conflict scope of §10).
- **A duplicate conflict is never auto-deleted or auto-overwritten.**
- **`DuplicateOverride`** is scoped to **`marketplace × account × group`** (optionally a composition, v3.1 §9.4). It requires an explicit operator decision, a reason and an audit record, and it can be revoked. It permits an intentional duplicate listing; it never releases an unresolved UNKNOWN (§10) or an unproven removal (§14).
- **No `allow_duplicate`** exists on the `ProductGroup` (ADR-0013 §4).
- The duplicate scope starts at `ACCOUNT` (v3.1 §9.4). DB identity and marketplace compliance policy are never mixed.

### 14. External removal and re-registration (ruling R4)

M5's first vertical adds **no automatic DELETE** merely to clean up a CREATE. When an operator removes a SmartStore listing outside ICBM:

1. **ICBM never deletes the `MarketplaceRegistration` row or its historical Snapshot, Attempt and read-back evidence.**
2. **An operator assertion alone does not prove deletion.**
3. An **explicit reconcile, read-back or lookup** is run for that known registration.
4. **If provider evidence proves the listing absent,** a terminal **external-absence** state ("externally removed") is recorded on the current registration lifecycle, with the observed time and a sanitized evidence reference. The exact enum and column are PR-B's. The historical `RegistrationSnapshot`, `RegistrationItemSnapshot` and Attempts stay immutable.
5. **If absence cannot be proven,** the registration stays `REVIEW_REQUIRED` and unresolved, and **duplicate protection is not freed.**

This explicit reconcile is M5 REGISTER lifecycle handling of a known registration. It applies to any provider listing ICBM knows it created: a verified registration, or an applied Intent whose provider identity is known. **Recurring detection of a later listing disappearance remains M6 OPERATE.**

**Re-registering the same group** is permitted only when **all** hold:
- the prior registration's remote absence is proven;
- no unresolved UNKNOWN remains in the same `marketplace × account × group` conflict scope;
- a **fresh duplicate preflight** finds no live conflicting SmartStore listing;
- a **new immutable `RegistrationSnapshot`** is created from current truth;
- a **new CREATE Intent** with a new idempotency identity is created.

- The historical removed registration stays queryable and is **never mistaken for an active duplicate**.
- **The old seller product code is not assumed reusable.** Reuse is allowed only if the adopted SmartStore contract proves it legal and lookup and reconcile stay unambiguous; otherwise a new deterministic listing identity is used (§7).
- Auto-delete and auto-overwrite remain forbidden.

### 15. Raw REGISTER evidence and the sanitizer contract (ADR-0011)

- **Purpose-bound.** Long-lived immutable or content-addressed raw evidence is allowed only for REGISTER: the CREATE attempt, the deterministic reconcile of an unknown CREATE, and the registration read-back (ADR-0011 §1). Image-upload evidence records the sanitized provider asset identity and digests; this ADR authorizes no other long-lived raw payload.
- **Sanitation happens before the artifact exists.** Before bytes are hashed, content-addressed or persisted, the sanitizer removes:
  - credentials, authorization headers and session material;
  - signed, expiring or tokenized URL material and any other credential-equivalent value (ADR-0010 §9, ADR-0011 §3);
  - unrelated private account data.

  **A later redacted view, masked render or restricted read path is not sufficient.**
- **Durable digests (review `5255157251`, B4).**
  - **Every durable payload or request digest (`payload_hash`, `request_payload_hash` and any evidence digest) is computed only from the sanitized canonical evidence representation**, and records the sanitizer and safe-query-key profile version.
  - **Authorization headers and session credentials are never part of a durable digest.**
  - Secret-bearing or tokenized material is removed **before durable hashing** as well as before persistence.
  - **If the provider requires such a value on the wire, it exists only transiently in memory for that call; its unsanitized bytes are neither persisted nor durably hashed.** A retry rebuilds the wire request from the Snapshot's business values and the credentials of the moment.
  - The Snapshot still freezes the exact business values and the safe provider identities needed for a retry and for read-back comparison, together with the sanitizer and profile version.
  - **If sanitation would remove a field required to prove the registration contract, the evidence is `REVIEW_REQUIRED`**; no forbidden raw artifact or hash is kept to fill the gap.
- **The safe-query-key contract.** Every adopted SmartStore endpoint whose response may be retained declares, with its adoption (PR-D), a **deny-by-default allow-list** of the URL query keys and response fields that may be kept. Everything not on it is removed before hashing. The profile is versioned together with the endpoint-mapping revision, and each artifact records the sanitizer version that produced it.
- **Fail-closed.** A payload the sanitizer cannot classify is not persisted raw. The evidence gap is recorded, and the dependent verification is `REVIEW_REQUIRED`.
- **No raw marketplace artifact is retained before this contract is implemented** for the endpoint concerned.
- **No automatic M6 inheritance** (ADR-0011 §2, §4): a recurring post-publication read of listing state, status, price or stock never inherits this allowance.

### 16. M2 capability convergence

- M5 reuses the accepted M2 `MarketplaceCapability` owner. **Registration success is not a parallel boolean.**
- **`product_registration.write` starts and stays `UNVERIFIED`** until real proof. It may become `READY` only after **all** of these:
  - the required M5 endpoints are `ADOPTED` under a current reviewed contract;
  - auth is `READY`;
  - operation-specific preflight passes;
  - the bounded real CREATE is explicitly authorized by the user;
  - any ambiguous outcome is reconciled;
  - the marketplace identity and read-back are obtained;
  - the read-back matches the immutable Snapshot under the adopted comparison contract;
  - durable sanitized evidence is retained.
- `write_scope = READY` never proves `write = READY`. `write_scope = MISSING` blocks the affected write with the existing workflow overlay. `write_scope = UNKNOWN` is not missing.
- **A successful real write never fabricates or backfills declared permission evidence.**
- `remote_outcome = UNKNOWN` forbids blind mutation replay.
- The transition is audited through the existing capability service. This ADR does not change `PRODUCT_WRITE_PROVABLE`; only the implementation PRs, under this contract, may.

### 17. SmartStore endpoint adoption comes later

- In PR-A, every product, image, category, attribute, option and notice endpoint **remains `NOT_ADOPTED`**. The registry resolves only the two M2 endpoints, and the endpoint-mapping revision is unchanged.
- **Enum presence and planned paths are not wire-contract proof.**
- Adoption (PR-D) requires: the current official Naver Commerce API documentation; the app mode and required permission groups; method, path, auth, content type, timeouts, redirect policy and success predicate; request and result types; provider error mapping; idempotency and read-back behaviour; a new endpoint-mapping revision and fingerprint in the same PR; and negative tests proving an unadopted or unknown endpoint fails locally.
- No SmartStore path is generalized outside the registry.

#### 17.1 IMAGE UPLOAD amendment (Issue #89 `5765557497`, `5765663972`)

`SMARTSTORE_PRODUCT_IMAGE_UPLOAD` alone is `ADOPTED` from the official Commerce API 2.89.0
contract. One exact artifact is one `POST /v1/product-images/upload` request with one
`imageFiles` multipart part. A successful `images[].url` may cross the existing `PreparedAsset`
boundary only when exactly one safe URL corresponds to that artifact and candidate fingerprint.

This amendment adds no migration, durable upload owner, cache or ledger. It authorizes no real
upload: execution remains `DRY_RUN` and provider-zero. There is no automatic retry. Any possibly
transmitted failure is `UPLOAD_UNKNOWN`, which is not `RegistrationIntent.UNKNOWN`. CREATE and
SEARCH remain `NOT_ADOPTED`; `product_registration.write` remains `UNVERIFIED`; LIVE, real canary
and M5 acceptance remain forbidden.

#### 17.2 CREATE and deterministic reconcile: the recorded evidence verdict

The architect's review of the official provider contract for the two remaining M5 endpoints closed
**`INSUFFICIENT`** (Issue #89 `5768247290` → `5768312853` → `5768347233`), on these blockers:

- no official CREATE idempotency or ambiguous-outcome replay-safety guarantee;
- a seller-code search may return similar, partial or exact matches;
- no official uniqueness guarantee for `sellerManagementCode`;
- no official freshness or read-after-write guarantee that would make a zero-result lookup an
  authoritative absence.

So the deterministic seller-side listing identity of §7 is exactly what §7 says it is: **ICBM's own
correlation identity, derived from stable local identity**. It is the key ICBM asks with; it is
never a provider uniqueness proof and it establishes nothing by itself. This verdict removes one
path only — **remote absence proven by a provider lookup** — because no lookup contract with the
needed semantics can be adopted, and an empty lookup result establishes no absence. It revokes
nothing else in §10: the resolution-evidence table there stays authoritative, including
transmission-precluded evidence and another explicitly reviewed machine or provider proof. A
possibly transmitted CREATE whose ambiguity no admissible evidence resolves stays `UNKNOWN` with its
conflict scope closed, and is never blindly replayed.

`SMARTSTORE_PRODUCT_CREATE_V2` and `SMARTSTORE_PRODUCT_SEARCH` therefore stay `NOT_ADOPTED` until
new official evidence resolves those blockers, and `product_registration.write` stays `UNVERIFIED`
(§16). This subsection records a verdict. It relaxes no rule of §7, §10 or §16.

### 18. AI is optional

- **M5 registers with no AI provider configured.** An operator-confirmed or manual category or field value completes the first vertical without AI.
- AI never fabricates `ProductFacts`, and AI output never substitutes for required source or operator evidence. A low-confidence category ranking is never auto-confirmed (ADR-0004).
- Where the accepted product contract permits the source name as a fallback final name, that fallback stays deterministic and never forces an AI call.
- Any AI runtime stays separately authorized (ADR-0012, Issue #30). PR-A implements no AI.

### 19. The detail-composition seam

- The Issue #61 boundary is kept: **product body → detail composition → marketplace payload**.
- The first vertical may be body-only. The payload builder never hardwires the body so that later top or bottom guidance requires replacing the registration owner.
- No Detail Guidance is implemented in PR-A or in M5's core merely to keep the seam open.

### 20. Supplier resale-price advisory is UI only

Issue #80 ruling `5738886070` stays binding. Complex supplier resale guidance is shown in the registration option-price UI as **`공급처 판매가 정책 참고`**:
- advisory only; it never rewrites an Item's selling price automatically;
- never canonical `PricingSnapshot` or procurement truth;
- the absence of a parsed advisory never blocks registration by itself;
- its provenance stays inspectable, and no supplier-site hardcode exists;
- any soft warning comparing an entered option price to it is a separate policy-assist decision.

### 21. Settings and policy ownership

- Account-scoped registration values (shipping template or policy, returns and exchanges, account defaults, the category taxonomy revision, the fee and policy context of pricing) belong to Settings and platform policy, **never to ProductFacts and never to a global product hardcode.**
- A concrete operator template id may be configured, but no account-specific id becomes a global rule.
- **The Snapshot freezes the exact resolved policy and template identities and revisions used.**

### 22. The registration UI

- The Registration Management surface displays **server-owned** state only: the selected Product and Items, listing shape, target account, category and required-field state, each Item's selling price and price basis from its `PricingSnapshot`, base, pricing and preflight status with every reason code, selected publication images and QA, the Snapshot identity, the Intent state (`PREPARED | SENT | CONFIRMED | UNKNOWN | FAILED`), the reconcile and read-back result, and the marketplace identity after verification.
- The UI never recomputes price or readiness, never turns `UNKNOWN` into `FAILED` for display, **never retries CREATE directly**, never invents category, notice or option compatibility, and **never shows a 2xx response as `CONFIRMED`** before server read-back verification.
- A retry or reconcile action appears only when the server contract allows it.
- After a reload, the screen reconstructs the same durable Draft, Intent, Attempt and Registration state from the server.

### 23. The M5 / M6 boundary

- **M5 read-back is registration and reconcile proof.** It covers the CREATE of a provider listing, the reconcile of an ambiguous CREATE, the verification against the Snapshot, and the explicit reconcile of a known registration (§14).
- **M5 claims no recurring operational ingest.** Recurring published-listing status, price, stock, order, inquiry and claim ingest, and **recurring detection of external deletion**, remain M6 OPERATE.

### 24. Execution safety

- The default external-write mode stays `DRY_RUN`. Merging code never authorizes a SmartStore write.
- No real marketplace mutation happens until the implementation PRs are audited, exact main is green, a bounded real M5 campaign is prepared, and **the user explicitly authorizes that write scope**.
- A required image upload is part of that protected write scope.
- A real M5 acceptance starts with a **single canary product**, never a bulk registration.
- Delete or deactivate cleanup is a separate protected write that requires its own explicit authorization and scope.

### 25. What this ADR does not decide

These are left to the PR that implements them, under this contract:
- tables, columns, enum spellings and migrations (PR-B), including the external-absence state (§14) and the mismatch verification state (§11);
- the preflight rule details and dependency-fingerprint encoding (PR-C);
- the wire representation of the listing identity and `registration_item_key`, the per-endpoint safe-query-key allow-lists, the read-back normalizer and comparison encodings, and endpoint adoption (PR-D);
- the execution job, failure budget and rate-limit values (PR-E), within the scope ownership §26 decides;
- the acceptance harness and the real canary campaign (PR-F).

### 26. The REGISTER execution-scope send brake (architect decision `5749504280`)

Entering a pause is derivable from durable attempt history; **leaving one is not**, because a release is a fact about an operator action or an authentication proof, and no attempt records that. PR-E therefore owns one minimal durable control, and only this one:

```text
RegistrationExecutionScope     one row per marketplace_key × marketplace_account_id × endpoint_group
  state                        ACTIVE | PAUSED
  pause_reason                 AUTH | POLICY | FAILURE_BUDGET, with its ErrorClass, its time and the policy version that judged it
  resume_generation/resumed_at the durable boundary the failure budget counts attempts after
  resumed_by / resume_reason   who accepted the release and why, as safe labels only
```

- **This owner is not capability truth.** CONNECT keeps account binding, authentication, permission and write scope, workflow overlays and contract freshness; REGISTER keeps Intents, Attempts, retry, reconcile, read-back, the failure budget and this brake. **Neither owner re-decides the other's state**, and no second queue, retry clock or authoritative status is introduced.
- **Entering `PAUSED`** happens only from durable execution evidence: `AUTH` with `NOT_APPLIED_PROVEN` engages `AUTH`; `POLICY_BLOCKED` with `NOT_APPLIED_PROVEN` engages `POLICY`; a versioned failure-budget breach engages `FAILURE_BUDGET`. It is idempotent for the same cause and it is audited. **An `UNKNOWN` outcome is governed by its Intent conflict scope (§10) and neither spends nor resets this budget.**
- **An `AUTH` pause may resume automatically, and only then**, on a CONNECT authentication proof **newer than the `paused_at` it answers**. REGISTER records that release in its own owner — actor `system`, the proof's own time, the next generation. The proof itself stays CONNECT's truth. **An `AUTH` pause is not an operator's to release**: an operator action is not an authentication proof, so the explicit resume answers `POLICY` and `FAILURE_BUDGET` only. The two authorities are disjoint, and neither can reach the other's cause.
- **A `POLICY` or `FAILURE_BUDGET` pause is never released by authentication.** Each requires an explicit audited REGISTER-scope resume after review or investigation. **A resume means "resume sending in this execution scope and re-evaluate current gates". It never claims that a remote mutation happened or that provider policy is factually absent.** The next send still runs the complete send-time gate (§3), and a repeated refusal pauses the scope again at a later boundary. **No time-only cooldown releases a scope.**
- **A budget the current policy has spent is recorded before the send is refused.** The budget is counted under the *current* versioned policy, so a lowered threshold, a policy revision or a restart can exhaust a scope that was never paused. The evaluation that refuses the send records what it found, as `FAILURE_BUDGET` with no measured class, so the scope an operator must resume exists instead of being a permanent stop with nothing to release.
- **A brake records only the class that caused it**: `AUTH` with an `AUTH` failure, `POLICY` with a `POLICY_BLOCKED` one, and `FAILURE_BUDGET` with neither — the class of the failure that spent the budget, or none when a policy revision alone did.
- **A scope's budget is counted from that scope's own history**: the attempts of the operation that sends to that endpoint group. M5 sends one operation, `CREATE`, so its execution owner refuses any other endpoint group rather than counting a history that is not its own; other groups' scope rows exist and stay independent.
- **The failure budget counts attempts only after that scope's latest accepted resume boundary.** Nothing else resets it: not a capability `updated_at`, not an authentication proof for a non-`AUTH` cause, not a permission refresh, a contract-freshness recording, a job, a batch or a Draft. An `APPLIED_PROVEN` attempt clears the derived consecutive count, and a scope that is already `PAUSED` still needs its own recorded release.
- **A resume never rewrites history.** It moves a window: no `RegistrationAttempt` is deleted, edited or re-classified. The audit records each pause and resume as history, and the `RegistrationExecutionScope` row stays the authoritative state — the audit log is never control truth.
- **The release is scoped exactly.** Releasing one marketplace × canonical account × endpoint group releases that scope and no other.

### 27. The durable registration preparation (architect decision `5751540323`)

A preflight is derived (§3), but the **inputs** it is derived from are an operator's own work: the category selection, the listing values and the detail composition of one provider-listing unit. They had no application-owned durable source — they survived only inside a queued `register.create` job's payload, which is execution and scheduler state. PR-F therefore owns one more durable control, and only this one:

```text
RegistrationPreparation            one preparation per provider-listing unit of a Draft
  revisions                        append-only authored revisions, each with its own sanitized fingerprint
  items                            the exact Item membership of that revision
Snapshot provenance                which exact revision, and which fingerprint, froze a Snapshot
```

- **It stores inputs only.** The Draft and Draft revision they were authored against, the Item membership, the category selection with its mapping, taxonomy and confirmation provenance, the listing values with their own provenance, and the detail composition. It stores **no** readiness, status or reason code, no Product, price, image or QA truth, no capability or auth truth, no provider duplicate outcome, no marketplace asset identity, no Snapshot, Intent, Attempt or Registration truth, and no retry or queue state. **No stored `REGISTERABLE` truth exists** (§3, M5-03), and no second unit identity exists beside the listing identity (§7).
- **Preflight stays derived**, from the durable preparation inputs, current owner truth and provider evidence where an adopted contract exists. A preparation is never a verdict.
- **Revisions are append-only and auditable.** Editing appends the next revision; an authored revision that has already frozen a Snapshot is never edited in place, so the Snapshot's provenance keeps its meaning.
- **A Snapshot proves which exact preparation revision and fingerprint produced it.** The link is its own row, so `registration_snapshots` stays immutable with its triggers intact and a Snapshot frozen before this owner existed stays valid with no provenance row. Authoring truth is never reconstructed by inverting `payload_json`.
- **The job payload stays an execution copy.** A `register.create` job may carry a frozen copy for crash-safe send-time revalidation, and it pins the same unit identity the Snapshot and Intent hold; it is never the authoring source. **No job is required to display or evaluate a preparation**, and changing a preparation later mutates no earlier Snapshot and no earlier job.
- **The operator surface owns no rule.** It creates, updates and reads a preparation, asks the preflight owner for the candidate evaluation with every reason code, and freezes a Snapshot and opens its Intent only through the owners that already decide READY and freshness (§3, §6, §8). IMAGE UPLOAD adoption adds no operator route or LIVE authority; CREATE and product search remain `NOT_ADOPTED` (§17, §24).

## Invariants

The binding invariants of this ADR, in one place. The contract tests pin this block.

```text
M5-01  M4 owns Product, Item, binding, PricingSnapshot, image lineage and readiness; M5 owns Draft, preflight, Snapshot, Intent, Attempt, Registration, DuplicateOverride and provider asset identity
M5-02  marketplace-sized binaries are M4 DerivedImageArtifacts with lineage and QA; M5 owns the upload and the provider asset identity only
M5-03  preflight is derived; no stored REGISTERABLE truth exists
M5-04  a provider-listing unit has exactly one immutable RegistrationSnapshot and one CREATE RegistrationIntent
M5-05  a historical Snapshot never follows later current-state changes
M5-06  registration_item_key exists before CREATE; display labels are not identity
M5-07  a retry never creates a new Snapshot or a new Intent
M5-08  an UNKNOWN CREATE is reconciled before any resend; no blind CREATE retry
M5-09  an unresolved UNKNOWN CREATE blocks every new CREATE Intent in its conflict scope, even for a new Snapshot
M5-10  a non-overlapping group in the same marketplace and account is not blocked by another group's UNKNOWN
M5-11  read-back is compared to the immutable Snapshot, never to the current Draft or Item
M5-12  a SINGLE_LISTING_WITH_OPTIONS subset read-back is a mismatch of the whole Intent; missing Items are never resent as CREATE
M5-13  SEPARATE_LISTINGS partial success is per listing Intent; a CONFIRMED sibling is never resent
M5-14  a batch or Draft PARTIAL is derived from child Intents, never stored as authoritative truth
M5-15  an operator assertion of external deletion alone never frees duplicate protection
M5-16  proven remote absence keeps the historical registration evidence and allows a fresh duplicate preflight and a new Snapshot and Intent
M5-17  recurring detection of external deletion is M6 OPERATE, not M5
M5-18  REGISTER raw evidence is sanitized before it is hashed or persisted
M5-19  no supplier hotlink is ever published
M5-20  product_registration.write stays UNVERIFIED until the bounded real CREATE is proven by read-back
M5-21  M5 registers with no AI provider configured
M5-22  no marketplace asset upload happens before a mutation-free non-asset preflight candidate is READY; the upload is bound to that candidate's fingerprint
M5-23  resolved_by = USER records or accepts evidence; an operator assertion alone never establishes NOT_APPLIED_PROVEN or remote absence and never releases an UNKNOWN conflict scope
M5-24  every durable payload or request digest hashes the sanitized canonical representation; secret-bearing wire bytes exist only transiently and are never persisted or durably hashed
M5-25  the REGISTER execution-scope brake is one row per marketplace, canonical account and endpoint group; it is REGISTER's own control and never capability truth
M5-26  an AUTH pause releases only on a CONNECT authentication proof newer than that pause and never on an operator action; a POLICY or FAILURE_BUDGET pause never releases on authentication and needs an explicit audited REGISTER resume
M5-27  the failure budget counts attempts only after the scope's latest accepted resume boundary, and a resume deletes, rewrites or re-classifies no RegistrationAttempt
M5-28  a scope's budget is counted from that scope's own operation history, and a budget the current policy has spent becomes a durable FAILURE_BUDGET pause before the send is refused
M5-29  a brake reason is recorded only with the measured class that caused it
M5-30  the registration preparation stores the operator's authored inputs only, append-only, and a Snapshot proves which exact revision froze it; a job payload is an execution copy and never the authoring source
```

## Rulings (Issue #89 addendum `5740352676`)

The architect accepted four contract points for this ADR. Each is folded into the decision above and recorded here.

- **R1. Marketplace-sized binaries remain in the M4 derived-image lineage** (§5, M5-02). M4 creates the immutable transformed artifact, its lineage and its QA; M5 requests the profile, uploads that exact artifact and owns the provider asset identity, the publication reference and REGISTER evidence. When CREATE needs a provider-issued image identity, upload precedes the final Snapshot, which freezes both the local artifact identity and the sanitized provider reference. Already present in Issue #89 §11 and kickoff item 5; restated here as a binding owner invariant.
- **R2. An unresolved UNKNOWN blocks a new CREATE Intent for the same conflict scope** (§10, M5-09, M5-10). A changed Snapshot does not escape it; overlap with any affected group is sufficient; a non-overlapping group is not blocked. Only a resolution as not applied or absent frees the scope. PR-B enforces it with a database and service invariant.
- **R3. Partial failure is defined at the provider-listing / Intent boundary** (§2, §11, §12, M5-04, M5-12–M5-14). A `SINGLE_LISTING_WITH_OPTIONS` subset read-back leaves the whole Intent not confirmed, and missing options are never resent as CREATE; any repair is a later reviewed UPDATE or reconcile. `SEPARATE_LISTINGS` and `SELECTED_OFFERS` split into one Snapshot and Intent per listing, and only there is partial success allowed. `PARTIAL` is derived.
- **R4. Manual or external delete preserves registration history; re-registration requires proven remote absence** (§14, M5-15–M5-17). No row or evidence is deleted; an assertion is not proof; proven absence records a terminal external-absence state; unproven absence keeps duplicate protection; re-registration needs proven absence, no unresolved UNKNOWN, a fresh duplicate preflight, a new Snapshot and a new Intent. Recurring disappearance detection stays M6.

## Review amendments (PR #90 GPT review `5255157251`)

The review held PR-A on four contract blockers, each folded into the decision above:
- **B1. One image owner in the canonical documents.** `docs/ARCHITECTURE.md` no longer lists "image transformation/upload" under REGISTER. REGISTER owns marketplace publication-asset requirements, upload and read-back; binary transformation itself remains the M4 derived-image owner (§5, R1). ARCHITECTURE §10 says the same.
- **B2. The pre-asset candidate gate** (§3, §5, M5-22). No marketplace asset is uploaded before a mutation-free non-asset preflight candidate is `READY`; a `BLOCKED`, `DUPLICATE`, `STALE` or `REVIEW_REQUIRED` candidate never uploads. The upload is bound to the candidate fingerprint, the final preflight re-evaluates everything, and a dependency change in between allows no CREATE.
- **B3. `resolved_by = USER` is never evidence** (§9, §10, M5-23). An operator assertion alone never establishes `NOT_APPLIED_PROVEN` or remote absence and never releases an UNKNOWN conflict scope; the outcome needs machine or provider proof under the adopted proof contract.
- **B4. Sanitation before durable hashing** (§6, §9, §15, M5-24). `payload_hash`, `request_payload_hash` and every durable digest hash the sanitized canonical representation; secret-bearing wire bytes exist only transiently and are never persisted or durably hashed.

The eight fail-closed readings submitted in Issue #89 comment `5740555092` were accepted by the review as written; reading 7 (sanitized upload evidence) is completed by B4.

## Consequences

- **PR-B to PR-F build on this contract** (Issue #89 §20): registration foundation and migrations, preflight and the Snapshot builder, SmartStore endpoint adoption with a typed adapter and read-back normalizer, idempotent execution and reconcile, then the acceptance harness and the bounded real canary campaign.
- **Repository contract tests added with this ADR** (`tests/unit/test_m5_register_contract.py`) pin, before any M5 schema exists:
  - the M5 SmartStore endpoints remain `NOT_ADOPTED`, resolve locally to a refusal, and the endpoint-mapping revision and fingerprint are unchanged;
  - no migration beyond `0015_m4_quantity_offers` and no registration table or column exists;
  - `RegisterService` stays unimplemented and returns 0;
  - no REGISTER module reaches a provider transport, an HTTP client or a SmartStore caller;
  - no stored registerable or readiness truth exists;
  - `product_registration.write` stays `UNVERIFIED` and `PRODUCT_WRITE_PROVABLE` stays `False`;
  - this ADR's invariants block (M5-01 to M5-24) and the decision text of R1–R4, including the addendum's nine required rules, and the absence of contradicting phrasings;
  - the review amendments: the canonical REGISTER owner list claims no image transformation (B1); only a `READY` non-asset candidate permits an upload (B2); an operator assertion alone never establishes a remote outcome (B3); every durable digest field hashes the sanitized representation (B4);
  - `docs/acceptance/M5.md` stays `PENDING` and names the bounded acceptance;
  - `docs/ARCHITECTURE.md` and `ROADMAP.md` reference this ADR.
- **The status documents** (CLAUDE.md §11, `ROADMAP.md` §14, README) name Issue #89 and this ADR as the M5 track. M5 stays CURRENT.

## References

- Issue #89 (body; kickoff `5740316498`; addendum `5740352676`)
- `docs/architecture/CANONICAL-V3.1.md` §2.4–§2.7, §3, §3.1, §6.5–§6.8, §8, §8.1, §9.1–§9.4, §10.1–§10.6, §11.2–§11.5, §14
- `ROADMAP.md` §7, §12–§14; `docs/ARCHITECTURE.md` §4, §5, §7, §13; CLAUDE.md §6.5, §7
- ADR-0004, ADR-0008, ADR-0010 §9, ADR-0011, ADR-0012, ADR-0013 (§4, §8, §9, §11)
- `docs/platforms/smartstore/ENDPOINT_MATRIX.md` §4; `ERRORS.md` §2.2–§3; `app/connect/marketplace/capability.py` (W1, write status)
- Issue #56 (Image Studio); Issue #61 (detail composition); Issue #80 ruling `5738886070` (resale advisory)
- `docs/acceptance/M4.md` (the accepted M4 foundation); `docs/acceptance/M5.md` (the M5 acceptance plan)
