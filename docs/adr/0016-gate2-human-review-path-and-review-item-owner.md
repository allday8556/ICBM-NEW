# ADR-0016 — Gate 2: the human review path and the durable ReviewItem owner

Status: **ACCEPTED** — decided by the architect kickoff `5804605624` (2026-09-24 UTC). This is G2-0
of Gate 2 (Issue #89), on exact main `48129070c7a9d2c38302bd94fb6dcae4217fa202`, after the Gate 1
acceptance `5804516180`.
- It records the eight contract decisions of that kickoff as contract, before any schema. The
  decisions were made by the kickoff. Where the kickoff asked the contract to pick one deterministic
  rule (§4, §5), the rule is picked here and is open to the exact-head audit.
- **Its implementation authority becomes effective only after this exact contract PR is audited,
  independently cross-audited and merged**, and even then only slice by slice (§11).

**It authorizes no migration, model, service, route, producer, dashboard counter behaviour, UI
action, test for new behaviour, ComplianceGate, endpoint adoption or provider call.** Each slice it
names (G2-A to G2-C) needs its own authorization in GitHub. Any migration is authorized only with
the slice that needs it.

Decision owner: Architect (ChatGPT). Sources:
- the Gate 2 kickoff `5804605624` (decisions 1–8, the G2-0 scope and the likely slice order);
- the Gate 1 acceptance `5804516180` and ADR-0015 §4, which moved `ReviewItem` (D3) to Gate 2;
- `docs/ARCHITECTURE.md` §9 (one ReviewItem model, surfaced in the existing screens and dashboard
  counts, no separate top-level review application in v1) and the gap it records;
- `ROADMAP.md` §14.1 and `docs/acceptance/M5.md` §9.2, which record the missing owner;
- `docs/GLOSSARY.md` §3 (`REVIEW_REQUIRED` is a workflow state, not an error class);
- the existing contract this ADR gives a durable owner to: `ReviewKind` and
  `ReviewService.open_counts()` in `app/review/service.py`, consumed by `ScreensService.dashboard()`
  and `ScreensService.soldout()` (`app/screens/service.py`);
- the evidence and retention rules this ADR reuses unchanged: ADR-0010 §9 (persisted source text is
  sanitized and bounded), ADR-0011 (retention of sanitized evidence) and ADR-0014 §15 / M5-24
  (sanitize before hashing or persisting).

Recorded by: Claude Code. The number was confirmed free in `docs/adr/`, on `main` and in every
remote branch immediately before writing.
Date: 2026-09-24
Related:
- ADR-0010, ADR-0013, ADR-0014 and ADR-0015: their owners stay the only sources of their truth. This
  ADR changes no sentence, invariant or ruling of any of them.
- `docs/ARCHITECTURE.md` §7 (ComplianceGate): unchanged, and **not** implemented by the `COMPLIANCE`
  review kind.

---

## Context

At `48129070` every owner that needs a human already says so in its own truth: a
`ProductFactsRevision` field, image reference or availability is `REVIEW_REQUIRED` (COLLECT, M3); an
M4 readiness or pricing evaluation carries `REVIEW_REQUIRED` reasons; an M5 preflight, Intent or
Attempt carries its own. Each is visible only on the screen that derives it.

What does not exist is the place a human finds that work. `ReviewService.open_counts()` returns zero
for every `ReviewKind` because nothing is stored, and the dashboard and the 품절 screen render those
zeros. `docs/ARCHITECTURE.md` §9 already says that a zero there proves nothing. Gate 2 closes that
gap with one durable owner of **human work**, and it must do so without creating a second copy of
any truth the owners above already hold.

## Decision

### 1. What Gate 2 is

- Gate 2 gives the human review path a durable owner and connects it to review conditions that
  **already exist** in production owners. It adds no new review reason, no verdict and no provider
  contact.
- The kickoff's decisions are binding:

| kickoff decision | disposition in this ADR |
| --- | --- |
| 1 one durable owner, no second domain truth | §2 |
| 2 lifecycle and idempotency | §3 identity, §4 lifecycle |
| 3 resolution never overrides the owner | §5 |
| 4 producer boundary | §6 |
| 5 counts and UI semantics | §7 |
| 6 scope, account and identity safety | §8 |
| 7 audit and persistence | §9 |
| 8 safety boundaries unchanged | §10 |

### 2. The ReviewItem owner: an index of human work, never a truth

**What a ReviewItem is.** A durable record that a canonical owner currently derives a condition
that needs a human. It is a **human-work index over an owner-derived condition**, and nothing more.

**What it never is.** It is never a source of:
- ProductFacts;
- Product membership;
- a price or `PricingSnapshot`;
- a readiness, preflight or registration verdict;
- a compliance verdict;
- stock truth;
- fulfillment truth;
- provider truth.

No owner reads a ReviewItem to make any decision. The dependency runs one way only: the review owner
reads owners through their existing read contracts, and **no owner imports or reads the review
owner**.

**The kinds are closed.** `ReviewKind` stays exactly:
- `COLLECT_EVIDENCE`
- `STOCK`
- `SOURCE_CHANGE`
- `COMPLIANCE`
- `REGISTRATION_ERROR`
- `FULFILLMENT`

A new kind needs an amendment of this ADR.

**What every ReviewItem references.** Every ReviewItem references, from its producing owner only:
- the **kind**;
- the **producer**: which owner derivation opened it;
- the **canonical scope** that owner uses (§8);
- the exact **subject** the owner names, such as a field key, an image reference, an Item or a
  provider-listing unit;
- the owner's own **reason code**;
- the exact **source identity** that caused the condition: the owner revision, fingerprint or
  evidence identity the owner evaluated.

It copies no value the owner holds beyond these identifiers.

### 3. Identity and deduplication

A ReviewItem is identified by two deterministic keys, both computed by the server from the
references above and never supplied by a client:

```text
condition key  = kind × producer × canonical scope × subject × owner reason code
review key     = condition key × source identity
```

- **One row per review key.** Re-observing the same condition at the same source identity finds
  that row: after a reload, a restart, a repeated producer evaluation or a retried job alike. It
  never adds another.
- **At most one `OPEN` item per condition key.** The same condition at a *new* source identity is a
  different review key, handled by §4, never folded into the old row.
- Both keys are the SHA-256 of a canonical, sanitized representation of their references. The same
  inputs always give the same key.

### 4. Lifecycle

**States.** Three states, every transition audited (§9) and restart-stable:

| state | meaning |
| --- | --- |
| `OPEN` | the owner derives this condition **now**, at this review key's source identity |
| `RESOLVED` | the owner no longer derives it at this source identity, and no successor replaced it |
| `SUPERSEDED` | the owner now derives the **same condition key at a new source identity**; the successor item holds it |

**Reconciliation decides the state, and only reconciliation.** A producer's reconciliation derives,
from the owner's current truth, the set of conditions in a scope. It then applies these rules:

| owner now derives … | existing row for that review key | result |
| --- | --- | --- |
| the condition at source identity *S* | none | a new `OPEN` item for *S* |
| the condition at *S* | `OPEN` | nothing changes |
| the condition at *S* | `RESOLVED` or `SUPERSEDED` | the same row is **reopened**: `OPEN`, generation + 1 |
| the same condition key at a new identity *S′* | `OPEN` item for *S* | the *S* item becomes `SUPERSEDED`, naming the new `OPEN` item for *S′* as its successor |
| nothing for that condition key | `OPEN` item | it becomes `RESOLVED` with basis `OWNER_CONDITION_CLEARED`, by the system actor |

**The rule for a changed owner revision or fingerprint is supersede.** The old item keeps its
source identity and evidence references unchanged. A new item opens for the new identity. Nothing
is rewritten or merged.

**When reconciliation runs.** It is idempotent and scoped, and it runs:
- after the owner's own unit of work that changed the scope commits;
- on an explicit re-evaluation;
- as a full pass when a producer is first wired (§7);
- inside every human resolution (§5).

Two further rules apply:
- A failure to index **never** rolls back, delays or changes the owner's write. Missed indexing is
  recovered by the next reconciliation of that scope, which is durable and repeatable.
- Reload, restart and repeated evaluation never multiply `OPEN` items. The two key rules of §3 make
  that a storage invariant, not a convention.

### 5. A human resolution never overrides the owner

**What a human resolution records.** A human resolves an item through the review owner. The
resolution records:
- the **actor**;
- the **time**;
- the **correlation identity**;
- a **disposition** from a bounded, server-owned set;
- an optional **note**, sanitized and bounded (§9);
- an optional **evidence reference**. The reference is only an identifier the owners already hold,
  such as a newer `ProductFactsRevision` or a preparation revision.

**It changes no owner fact.** A resolution never:
- edits a field status;
- moves a pointer;
- confirms a member;
- changes a price;
- marks anything `READY` or `PASS`.

A human who changes owner truth does so through **that owner's own command**, on its own screen,
under its own rules and audit.

**The resolution is reconciled in the same unit of work.** The owner is re-derived for that exact
review key:
- **If the owner no longer derives the condition,** the item becomes `RESOLVED`, carrying the human
  actor and disposition.
- **If the owner still derives the condition,** the item **stays `OPEN`**. The human resolution is
  still recorded and audited as history, and the response says the condition persists, with the
  owner's reason code. Review work therefore never leaves the open set while its owner condition is
  unchanged. A resolution can never make an owner `PASS` or `READY`, and it can never lower a count
  that the owner's truth still supports.

**Free text is never evidence.** No note, disposition or operator assertion is ever:
- provider evidence;
- `NOT_APPLIED_PROVEN`;
- remote-absence proof;
- source-fact confirmation;
- a compliance verdict.

ADR-0014 §9, §10 and M5-23 stay exactly as they are.

### 6. The producer boundary

- **Existing conditions only.** A producer indexes a condition only if a production owner already
  derives it, and only by referencing that owner's reason code, subject and source identity. It
  never:
  - re-implements a readiness, preflight, stock or pricing rule;
  - copies an owner's logic;
  - invents a reason to populate the queue.
- **One condition, one kind.** Each owner condition maps to exactly one `ReviewKind`. The mapping
  is a reviewed table in the slice that wires the producer.
- **No double indexing across layers.** A condition that is a projection of another owner's
  condition is not indexed twice. For example, M4's `SOURCE_CORE_FIELD_REVIEW_REQUIRED` projects a
  COLLECT field status.
- **The first mandatory producer is COLLECT / M3.** The `REVIEW_REQUIRED` source truth of a
  `ProductFactsRevision` must be indexed: its fields, image references and availability, as that
  owner states them.
- **M4 and REGISTER conditions** that already exist may be indexed only by reference. Examples are
  image QA `REVIEW_REQUIRED`, pricing-binding reasons, preparation and preflight reasons, and an
  `UNKNOWN` Intent under its `REVIEW_REQUIRED` overlay. A derived-only condition, such as a
  preflight reason, is indexed against the durable source identity it was derived from, such as the
  preparation revision. It is never indexed against a stored verdict, because none exists
  (ADR-0014 M5-03).
- **`COMPLIANCE` does not implement ComplianceGate.** It has no producer in Gate 2 and stays
  `NOT_WIRED`. ComplianceGate remains a later pre-LIVE gate (`docs/ARCHITECTURE.md` §7).
- **`FULFILLMENT` authorizes no M6 behaviour.** It has no producer in Gate 2 and stays
  `NOT_WIRED`.
- **`SOURCE_CHANGE`** stays `NOT_WIRED` unless a slice names an existing production owner condition
  for it. `docs/ARCHITECTURE.md` §11 source drift has no production owner today.

### 7. Counts, `NOT_WIRED`, and the screens

**Each kind is `WIRED` or `NOT_WIRED`.** A kind is `WIRED` only when both of these hold:
- a producer for it is implemented;
- that producer's **first full reconciliation** over existing owner truth is durably recorded.

Until then the kind is `NOT_WIRED`. A producer that indexes only new writes would undercount
conditions that already existed, and that undercount must never read as coverage.

**Counts come from durable rows.** `open_counts()` derives each `WIRED` kind's count from durable
`OPEN` ReviewItems, and never returns a hard-coded zero.
- For a `NOT_WIRED` kind, the server says `NOT_WIRED` explicitly. It never says `0`.
- The screens render that difference.
- A dashboard "empty" or a `NO_STOCK_REVIEW_ITEMS` verdict may rest on review counts only when the
  kind is `WIRED` and its count is zero.

**Where review work is shown.** The relevant existing screens show their own ReviewItems, and the
dashboard aggregates the counts. **No new top-level review application is added in v1**
(`docs/ARCHITECTURE.md` §9).

**The server owns every state.** Korean labels such as 검토 필요 and 확인 필요 are display text for
server-owned states (`docs/GLOSSARY.md` §3). Every lifecycle state, reason, disposition and
`WIRED` verdict is the server's.

### 8. Scope, account and identity safety

**Scope uses the owner's own identifiers.** A ReviewItem carries the canonical scope its owner
already uses, and invents no cross-domain identifier:

| producer | scope identifiers |
| --- | --- |
| COLLECT | `supplier_key`, `source_product_id`, and the `ProductFactsRevision` as source identity |
| M4 | `product_group_id` and `item_id`, and the owner revision it evaluated |
| REGISTER | `marketplace_key`, `marketplace_account_id`, `draft_id`, the provider-listing unit, and the preparation revision or Intent |

**An item never crosses scopes.** An item of one scope never renders or resolves as work of
another: Product A as Product B, or account A as account B. The rules that enforce this:
- A resolution names the item and the **scope and generation the client was shown**.
- A mismatch is refused, and nothing is written.
- A stale response is refused. So is a repeated click after the item moved, or a second resolution
  of an already resolved generation. A resolution is idempotent per item generation, and a retry
  records no second transition.

### 9. Audit and persistence

**Every transition is audited.** Opening, resolution, supersede and reopen each write an audit event
in the same unit of work as the transition.
- Each event carries the actor: the system actor for reconciliation, or the operator.
- Each event carries the correlation identity.
- Before and after hold identifiers, states and keys only.

**Durable rows are the only truth for the queue.** They survive restart. No browser storage and no
page state is authoritative.

**Transitions are kept as history.** They are append-only. A row's identity references never change
after it is written: its kind, producer, scope, subject, reason code, source identity and keys.

**A ReviewItem stores references only.** It stores no raw provider or page payload, no response
body, no credential, token, cookie or session material, no external URL, and no unsanitized
evidence. Only identifiers, reason codes, digests and sanitized bounded text are permitted by the
existing rules:
- ADR-0010 §9, persisted source text;
- ADR-0011, retention;
- ADR-0014 §15 and M5-24, sanitize before hashing or persisting.

A value that cannot be classified as safe is not persisted.

**Notes are display text only.** A note is bounded, sanitized with the same fail-closed rule as
persisted source text, and never parsed back into truth.

**Nothing verdict-like is stored.** The review owner stores no readiness, verdict or status copied
from an owner. Its own lifecycle state is the only state it holds.

### 10. Safety boundaries: unchanged

- M5 stays `PENDING`.
- `SMARTSTORE_PRODUCT_CREATE_V2`, `SMARTSTORE_PRODUCT_SEARCH` and the category, attribute,
  standard-option and notice endpoints stay `NOT_ADOPTED`.
- `product_registration.write` stays `UNVERIFIED`.
- Execution stays `DRY_RUN` / `M0_DRY_RUN_ONLY`, and the canary stays `BLOCKED`.
- Gate 2 contains:
  - no ComplianceGate;
  - no LIVE authorization;
  - no provider call or provider mutation;
  - no M6 or fulfillment implementation;
  - no second marketplace or supplier.

### 11. The Gate 2 slices

After this ADR is audited, cross-audited and merged, the slices are authorized **one at a time**,
each as its own PR with its own exact-head audit:

| slice | content |
| --- | --- |
| G2-A | the durable ReviewItem owner and its migration: identity and deduplication (§3), the audited lifecycle (§4) and resolution (§5); no producer |
| G2-B | the COLLECT / M3 producer and its review path on the relevant existing screens |
| G2-C | indexing of the current M4 and REGISTER conditions; `open_counts()` from durable rows, and the dashboard and 품절 counts with `NOT_WIRED` semantics (§7) |
| closeout | exact-main Gate 2 closeout from a fresh session |

### 12. What this ADR does not decide

- table names, columns, enum spellings (including the disposition set), route paths, payload shapes
  or screen layout: each slice decides them within this boundary and its own review;
- the kind mapping of each M4 and REGISTER condition, which is reviewed with G2-C (§6);
- the ComplianceGate, LIVE authorization and M6 contracts;
- anything about CREATE, SEARCH, reconcile or image upload, which stay exactly as ADR-0014 states
  them.

## Invariants

```text
G2-01  a ReviewItem is a human-work index over an owner-derived condition; it is never a source of facts, membership, price, readiness, compliance, stock, fulfillment or provider truth
G2-02  no owner reads a ReviewItem to decide anything; the review owner reads owners, never the reverse
G2-03  ReviewKind is closed: COLLECT_EVIDENCE, STOCK, SOURCE_CHANGE, COMPLIANCE, REGISTRATION_ERROR, FULFILLMENT
G2-04  every ReviewItem references its kind, producer, canonical owner scope, subject, owner reason code and exact source identity
G2-05  the condition key and the review key are server-computed digests; one row per review key and at most one OPEN item per condition key
G2-06  reload, restart, a retried job and repeated producer evaluation never multiply OPEN items
G2-07  a changed owner revision or fingerprint supersedes the old item and opens a new one; nothing is rewritten or merged
G2-08  reconciliation alone decides OPEN, RESOLVED and SUPERSEDED from current owner truth; a reappearing review key reopens its own row
G2-09  a failure to index never rolls back, delays or changes an owner write; the next reconciliation of that scope recovers it
G2-10  a human resolution records actor, time, disposition, note and evidence reference, changes no owner fact, and leaves the item OPEN while the owner still derives the condition
G2-11  no note, disposition or operator assertion is ever provider evidence, NOT_APPLIED_PROVEN, remote-absence proof, a source-fact confirmation or a compliance verdict
G2-12  producers index only conditions production owners already derive, each in exactly one kind, without copying owner logic or inventing reasons
G2-13  COMPLIANCE and FULFILLMENT have no Gate 2 producer and stay NOT_WIRED; COMPLIANCE never implements ComplianceGate
G2-14  a kind is WIRED only after its producer's first full reconciliation is durably recorded; a NOT_WIRED kind is never reported as zero
G2-15  a ReviewItem of one scope never renders or resolves as another's; a stale, mismatched or repeated resolution writes nothing
G2-16  every transition is audited with actor and correlation identity; a row's identity references are immutable and its transitions append-only
G2-17  a ReviewItem stores references, reason codes, digests and sanitized bounded text only; no raw payload, credential, external URL or verdict copied from an owner
G2-18  Gate 2 adds no ComplianceGate, LIVE authorization, provider call, endpoint adoption, M6 behaviour or expansion; M5 stays PENDING
```

## Consequences

- **The screens gain a real source for review counts without a second truth.** Every count is
  traceable to a durable item, and every item to the exact owner revision that caused it.
- **G2-A is expected to add the one migration Gate 2 needs.** Several repository pins will then move
  with it:
  - `M5_HEAD` in `tests/unit/test_m5_register_contract.py`;
  - the table tuple and head in `tests/integration/test_migrations.py`;
  - the table set in `tests/unit/test_repository_rules.py`.

  Column names must also stay clear of the stored-truth guard in the M5 contract tests: no
  `ready`, `readiness`, `preflight_status` or `summary` column.
- **G2-C changes what an empty screen means.** Today the dashboard and 품절 contracts treat the
  placeholder zeros as empty (`NO_CONNECTIONS`, `NO_STOCK_REVIEW_ITEMS`, pinned by
  `tests/integration/test_api.py`). After G2-C those verdicts rest on review counts only for a
  `WIRED` kind (§7). That test is updated with G2-C, not before.
- **Gate 2 leaves M5 exactly as blocked as Gate 1 left it.** It removes the review-queue gap only.

## References

- Issue #89: Gate 2 kickoff `5804605624`; Gate 1 acceptance `5804516180`; ADR-0015 §4 (D3).
- `docs/ARCHITECTURE.md` §7, §9, §11; `docs/GLOSSARY.md` §3.
- `ROADMAP.md` §14.1; `docs/acceptance/M5.md` §9.2.
- ADR-0010 §9, ADR-0011, ADR-0013, ADR-0014 §9, §10, §15, M5-03, M5-23, M5-24, ADR-0015.
- `app/review/service.py`, `app/screens/service.py`, `app/screens/contracts.py`.
