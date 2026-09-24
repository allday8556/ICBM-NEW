# ADR-0018 — Gate 3: pre-LIVE safety and the bounded LIVE authorization contract

Status: **ACCEPTED** — decided by the architect kickoff `5821078540` (2026-09-24 19:47 UTC; 2026-09-25 KST). This is G3-0
of Gate 3 (Issue #89), on exact main `267d6a9eb20788819a8863f59a9c8f8e47700870` (post-merge CI
`36045605845`, 6/6), after the Gate 2 acceptance (`5818393660`, cross-audit `5818648647`).
- It records the kickoff's seven decisions (D1–D7) as contract, before any schema or runtime. The
  decisions were made by the kickoff. Where the kickoff asked the contract to define a rule, the
  rule is defined here and is open to the exact-head audit.
- **Its implementation authority becomes effective only after this exact contract PR is audited,
  independently cross-audited and merged**, and even then only slice by slice (§12).

**It authorizes nothing to run.** It authorizes no migration, model, service, route, execution-mode
change, LIVE grant, kill switch, backup or restore code, retention runtime, visual test runtime,
provider adapter, endpoint adoption, provider call, LIVE execution, canary, ComplianceGate owner, M6
or M6.5 work. `M0_DRY_RUN_ONLY` / `M0_LIVE_FORBIDDEN` stays the only execution policy at this main.

Decision owner: Architect (ChatGPT). Sources:
- the Gate 3 kickoff `5821078540` (D1–D7, the G3-0 scope and the expected later areas);
- `ROADMAP.md` §14.1 (the LIVE-authorization and ComplianceGate gaps) and §14.2 (the preconditions
  for the first LIVE write);
- `docs/ARCHITECTURE.md` §7 (ComplianceGate, RegistrationAttempt) and §13 (execution safety);
- `docs/acceptance/M5.md` §6 (derived canary readiness) and §9.1–§9.3 (the provider-evidence verdict,
  the owner gaps and the carried preconditions);
- the Gate 1 decisions D4 (ComplianceGate) and D5 (LIVE contract) that moved both to later pre-LIVE
  gates (ADR-0015 §4);
- the owners and rules this ADR leaves exactly as they are: the execution-mode owner
  (`app/system/execution_mode.py`), ADR-0014 §9–§11, §15, §17, §24 and §26, ADR-0011 and ADR-0016.

Recorded by: Claude Code. The number was confirmed free in `docs/adr/`, on `main` and in every
remote branch immediately before writing.
Date: 2026-09-24 UTC (2026-09-25 KST)
Related:
- ADR-0014 (SmartStore REGISTER): its CREATE, `UNKNOWN`, reconcile, read-back, sanitizer and
  execution-scope rules stay the only authority for what they govern. This ADR adds a layer in
  front of them and changes no sentence, invariant or ruling of ADR-0014.
- ADR-0011 (raw read-back retention): unchanged; §8 below only adds prerequisites.
- ADR-0016 (Gate 2): unchanged. A LIVE grant never creates, closes or resolves a ReviewItem.

---

## Context

M5 is still `PENDING`. Every M5 implementation PR, Gate 1 and Gate 2 are merged and accepted, yet
the first real SmartStore canary is impossible, for two independent reasons:

1. **No safety contract governs a real write.** The execution-mode owner refuses LIVE outright
   (`M0_DRY_RUN_ONLY`, `M0_LIVE_FORBIDDEN`). That is the deliberate safe state, and nothing yet says
   who may leave it, for what, for how long, how it is stopped, and what must be proven first
   (backup, retention, populated UI) — `ROADMAP.md` §14.1, §14.2; `docs/ARCHITECTURE.md` §13.
2. **The provider evidence is insufficient.** Product CREATE and the duplicate-lookup search stay
   `NOT_ADOPTED` after the official-evidence review closed `INSUFFICIENT` (Issue #89 `5768312853`,
   `5768347233`; `docs/acceptance/M5.md` §9.1).

Gate 3 closes the first as contract and keeps the second a hard, independent blocker. Closing both
is required before a canary; closing the first never substitutes for the second. M6 does not start
before M5 acceptance.

---

## Decision

### 1. Scope

This ADR is the contract for the **pre-LIVE safety stack**: the bounded LIVE grant (§3), the
protected-write brake (§4), canary eligibility without a ComplianceGate (§5), the provider-evidence
blocker (§6), and three proven prerequisites — backup and restore (§7), evidence retention (§8) and
populated visual/responsive acceptance (§9). §10 composes them into canary readiness.

It governs **marketplace mutations only**: a request that can create, change or remove state at a
marketplace (for SmartStore today: product CREATE and a product image upload; later any UPDATE,
DELETE, deactivate or shipment write). A provider **read** is not a mutation and is not governed
here; each read stays governed by its own adoption. A supplier order (발주) is outside this ADR and
stays a separately authorized protected action (`CLAUDE.md` §7.2).

### 2. The current execution policy stays authoritative

`M0_DRY_RUN_ONLY` / `M0_LIVE_FORBIDDEN` remains the execution policy until a later implementation
slice (§12) replaces it **under this contract**. Until then:
- every LIVE request is refused and audited exactly as today;
- `live_writes_permitted` stays `false`;
- no grant, brake state, backup proof, visual acceptance or user message can make it `true`.

When a slice replaces it, the global mode `DRY_RUN | LIVE` (`docs/ARCHITECTURE.md` §13) stays the
outer switch, and `DRY_RUN` stays the default and the state every restart without a valid grant
returns to. **The mode alone is never authority for a mutation**: in `LIVE`, a mutation still
needs every check of §4.3.

### 3. The bounded LIVE grant (D1)

A **LIVE grant** is the only authority for a marketplace mutation. It is a durable, audited,
server-owned record, created only by an explicit protected operator action that names every field
below. The server validates it and stores it; the UI only displays it.

**What a grant binds, and every field is exact:**

| field | rule |
| --- | --- |
| `marketplace_key` | one marketplace |
| `marketplace_account_id` | one canonical account (GLOSSARY §1), bound at grant time |
| operation / endpoint group | the exact operations and adopted endpoint groups it permits (e.g. `CREATE` + the image upload that unit needs); never "any" |
| provider-listing unit / Intent scope | the exact `RegistrationIntent` (and its Snapshot's listing identity) where the operation has one |
| mutation budget | a finite maximum number of mutation attempts, at least 1; each attempt is counted when it is started, and never refunded, whatever its outcome — an `UNKNOWN` included |
| approval | the approving user's identity, the GitHub authorization reference the approval answers, and the audit correlation identity |
| window | a finite `not_before` and `expires_at`; no open-ended grant exists, and the maximum window length is a server-owned bound the implementing slice fixes and pins |
| state | `ACTIVE` until it becomes `EXPIRED`, `REVOKED` or `EXHAUSTED`; each of those is terminal |

**Rules:**
- **Deny by default.** No active grant that matches the mutation exactly — marketplace, account,
  operation, endpoint group, unit/Intent, within its window, with budget left — means the mutation
  is refused **before any transmission**, and the refusal is audited.
- **No wildcard and no widening.** A grant never matches another account, marketplace, operation,
  endpoint group or unit. A reload, restart, retry, job re-run or new batch can neither widen a grant
  nor recreate an expired, revoked or exhausted one. A new scope needs a new grant.
- **Terminal states are terminal.** `EXPIRED`, `REVOKED` and `EXHAUSTED` never return to `ACTIVE`.
  Revocation is an explicit audited action and takes effect for every mutation not yet started.
- **A grant decides nothing else.** It never changes endpoint-adoption state, CONNECT capability or
  write-scope truth, registration readiness or preflight, ComplianceGate state, provider truth,
  `product_registration.write` status or ReviewItem state.
- **A grant never authorizes a blind CREATE replay.** An `UNKNOWN` outcome stays governed by
  ADR-0014 §10: it is reconciled only with admissible evidence, it keeps its conflict scope closed,
  and no grant, budget or approval turns it into `FAILED` or `NOT_APPLIED_PROVEN`.
- **UI text and checkboxes are never authority.** Only the server-owned grant is. A confirmation
  typed in the UI is an input to the protected action that creates the grant, never the grant.
- **Every grant transition is audited** in the same unit of work: created, activated, consumed
  (per attempt), expired, revoked, exhausted — with actor, time and correlation identity.

### 4. The protected-write brake — the kill switch (D4)

#### 4.1 The brake

One **server-owned protected-write brake** stops every new marketplace mutation at once,
independently of any UI state and of any grant.
- **`ENGAGED` or `RELEASED`**, durable, and read by the server on every mutation check.
- **Fail closed:** a brake state that is absent, unreadable or inconsistent is treated as `ENGAGED`.
- **Engaging** is always allowed to an operator and to the system (for example on a crash-recovery
  path), takes effect for every mutation not yet started, and is audited with actor, time and
  correlation identity.
- **Releasing** requires a new explicit authorization and is audited. **Releasing the brake does not
  resurrect an expired, revoked or exhausted grant**, and does not widen any grant.
- **It survives restart.** A restart never releases it.

#### 4.2 What the brake never does

- It never deletes, edits or re-classifies an Intent, Attempt, read-back or evidence record.
- **It never rewrites an `UNKNOWN`** to `FAILED` or `NOT_APPLIED_PROVEN`, and never frees a conflict
  scope.
- An attempt already handed to the transport before the brake engaged completes as the one
  attempt it is: its outcome is recorded under ADR-0014 §9–§10, never discarded.

#### 4.3 The whole safety stack

A marketplace mutation may start only when **every** layer allows it, checked at send time in the
same unit of work that starts the attempt:

1. the execution mode is `LIVE` (§2);
2. the protected-write brake is `RELEASED` (§4.1);
3. an `ACTIVE` grant matches exactly, with budget left (§3);
4. the endpoint group is adopted, and the capability and write scope allow it (unchanged owners);
5. the REGISTER execution-scope send brake for that scope is `ACTIVE` (ADR-0014 §26 — **unchanged
   and not weakened**; the two brakes are independent, and releasing either releases nothing of the
   other);
6. the complete send-time gate of ADR-0014 §3 passes, with no unresolved conflict (§10).

Any failing layer refuses the mutation before transmission. No layer re-decides another's truth.

### 5. ComplianceGate stays a separate later owner; the first canary is non-regulated (D2)

- **No production ComplianceGate is implemented by Gate 3**, and the grant contains no compliance
  logic. `docs/ARCHITECTURE.md` §7 is unchanged: regulated categories may not be claimed as
  automatically registrable.
- **Canary eligibility.** The first bounded canary may use only a product whose existing,
  operator-reviewed category metadata (ADR-0015 §3) demonstrably places it outside every regulated
  category in scope — for example 건강기능식품, KC certification, 식약처 notices and prohibited
  wording. **This is an eligibility restriction, never a `COMPLIANCE PASS` verdict**: it is recorded
  as the canary's own eligibility evidence and is read by no other owner.
- **If the system cannot prove that the chosen canary is outside the regulated set, the canary stays
  `BLOCKED`.** An operator assertion is not that proof.
- A production ComplianceGate owner is still required before any regulated-category automation, under
  its own contract and authorization.

### 6. Provider evidence is an independent hard blocker (D3)

- `SMARTSTORE_PRODUCT_CREATE_V2` stays **`NOT_ADOPTED`**.
- `SMARTSTORE_PRODUCT_SEARCH` stays **`NOT_ADOPTED`**.
- The official-evidence verdict stays **`INSUFFICIENT`** (Issue #89 `5768312853` / `5768347233`;
  `docs/acceptance/M5.md` §9.1).
- **No LIVE grant, brake release, backup proof, retention proof, visual acceptance or user approval
  can override that verdict.** The canary stays **`BLOCKED`** until new official provider evidence
  independently supports the CREATE and reconcile safety contract of ADR-0014, and the endpoints
  concerned are separately adopted.
- In particular, **an ICBM seller-side code and a zero-result search remain insufficient proof of
  remote absence** (ADR-0014 §7, §17.2).

### 7. Backup and restore: a proven drill, not a declaration (D5)

Before any first LIVE write, a **backup and restore drill** is performed and recorded:
- a backup is taken from the canonical data root, consistent with SQLite WAL (a copy of the live
  database file alone is not a backup);
- it is restored into a **separate fresh root** — never over the active data root;
- the restored root proves that the schema is at the expected Alembic head and that the database is
  readable and passes its integrity check;
- it proves that **the complete canary-critical chain that exists at drill time** survives, each
  element compared by identity **and** state with the source root, so a restore that loses or
  changes one fails:
  - the product side: the source revision, the Product and Item, the canonical account, and the
    target-policy and category-metadata revisions;
  - the preparation side: the Draft and the preparation revision;
  - **the REGISTER chain** (review `5821787401`):
    - the canary unit's immutable `RegistrationSnapshot` — its listing identity, payload hash,
      preflight fingerprint and item snapshots;
    - its `RegistrationIntent` — the intent identity, the **idempotency key**, and its current
      `state`, remote outcome and verification state;
    - every `RegistrationAttempt` of that Intent, as history;
    - the REGISTER **execution-scope brake** state (ADR-0014 §26) for that marketplace × account ×
      endpoint group — its state, pause cause and resume generation, or its proven absence, which
      is an `ACTIVE` scope;
    - when they exist: an unresolved conflict scope, a duplicate override, and a registration and
      its verification;
- **an element that does not exist yet at the drill point is recorded as absent, never created for
  the drill**: before a freeze there is no Snapshot, before an Intent there is no Attempt. The drill
  writes nothing to the active root and fabricates nothing in either root;
- the drill's sanitized evidence (identities, states, counts, digests, versions, times, and every
  element recorded as absent) is recorded; no credential, secret or raw payload enters it.

A document saying that backups exist is not a drill. **A canary without a recorded drill on the
current schema head stays `BLOCKED`.**

### 8. Evidence retention: end to end, fail closed (D6)

Before canary authorization, what is retained, for how long and when it may be removed is fixed as
follows. It relaxes nothing of ADR-0014 §15, ADR-0011 or the Gate 2 evidence rules.
- **What is retained.**
  - The durable REGISTER rows — Draft, Snapshot, Batch, Intent, Attempt, registration, scope — are
    append-only history and are never deleted.
  - The sanitized evidence of every canary mutation and verification: the sanitized canonical
    request representation and its digest, the sanitized provider response or error class, the
    sanitized read-back comparison evidence, the reconcile evidence of an `UNKNOWN`, and the
    sanitized provider asset identity of an image upload — each recording its sanitizer and
    safe-query-key profile version (ADR-0014 §15).
  - The grant and brake history (§3, §4) and the audit trail.
- **Sanitation before hash or persist** stays the rule (ADR-0014 §15): no credential, token,
  cookie, session material, raw private payload or unsafe or signed URL is ever hashed or stored.
  A value the sanitizer cannot classify is not persisted, and the dependent verification becomes
  `REVIEW_REQUIRED`.
- **Retention duration and deletion boundary.**
  - **No automatic deletion** of canary-scope REGISTER evidence is authorized before M5 acceptance
    is decided. A later deletion policy needs its own decision.
  - **Evidence tied to an unresolved condition is never discarded**, silently or by any policy,
    while that condition is unresolved. That covers an `UNKNOWN` Intent, a read-back `MISMATCH`,
    an open review item that references it, and an open conflict scope.
  - Any later deletion is itself audited, and it removes only sanitized raw artifacts, never
    durable rows.
- **Fail closed.** If the retention path cannot store the evidence a mutation requires, the mutation
  does not start, and the canary stays `BLOCKED`.

### 9. Populated visual and responsive acceptance (D7)

The first canary may not be authorized from functional Playwright wiring tests alone. Before it:
- the **populated first-vertical state** is exercised through the relevant screens: 수집관리 (a
  recorded collection), 통합DB (the Product and Item, image and review state), 등록관리 (target,
  Draft, preparation, candidate preflight, REGISTER review items, execution-scope and
  protected-write state, grant state), the dashboard and 품절 review counts, and Settings (the
  target policy and category metadata);
- at the **accepted viewport set**: at least the established landscape 1920×1080 and portrait
  1080×1920 of the approved prototype comparison (`scripts/visual_check.py`); the implementing slice
  may add sizes and records the final set;
- it verifies that server-owned statuses and reason codes render and that **no blocker is hidden**
  — no truncation, overlap or horizontal scroll that removes a reason, a `NOT_WIRED` or
  `NOT_CURRENT` state, a refusal or a protected-write state;
- its evidence (screenshots and checks per screen and viewport, the code SHA and the viewport set)
  is recorded under `docs/acceptance/`.

This is UI acceptance only. It authorizes no provider mutation.

### 10. Canary readiness

The derived, read-only canary readiness (`docs/acceptance/M5.md` §6) stays `BLOCKED` until **every**
requirement holds, each proven from its own durable evidence, never asserted:

| requirement | owner / evidence |
| --- | --- |
| `CREATE_ADOPTED`, `RECONCILE_PATH_ADOPTED` (and `IMAGE_UPLOAD_ADOPTED` when needed) | endpoint adoption — **not met** (§6) |
| `LIVE_GRANT_ACTIVE` | an `ACTIVE` grant matching the canary unit exactly (§3) |
| `PROTECTED_WRITE_BRAKE_RELEASED` | the brake (§4) |
| `CANARY_NON_REGULATED` | the canary eligibility evidence (§5) |
| `BACKUP_RESTORE_PROVEN` | the recorded drill on the current schema head (§7) |
| `EVIDENCE_RETENTION_READY` | the retention path of §8 |
| `VISUAL_ACCEPTANCE_RECORDED` | the recorded populated acceptance at the accepted SHA (§9) |
| the existing requirements | account binding, auth, write scope, a prepared Intent, no unresolved conflict, the execution-scope brake, one unit only (M5.md §6, unchanged) |

Readiness is derived, read-only and authorizes nothing: even `READY` is not permission. A real
write stays a separate, explicitly user-authorized, single-product canary (ADR-0014 §24).

### 11. Unchanged

- M5 stays **`PENDING`**; `docs/acceptance/M5.md` records no acceptance run.
- `SMARTSTORE_PRODUCT_CREATE_V2` and `SMARTSTORE_PRODUCT_SEARCH` stay **`NOT_ADOPTED`**; no
  category, attribute, standard-option or notice endpoint is adopted.
- `product_registration.write` stays **`UNVERIFIED`**.
- Execution stays **`DRY_RUN` / `M0_DRY_RUN_ONLY`**; LIVE stays forbidden.
- The canary stays **`BLOCKED`**; marketplace mutations stay **0**.
- ComplianceGate has no owner; M6 and M6.5 are not started.
- ADR-0014 §9–§11, §15, §17, §24 and §26, ADR-0011, ADR-0015 and ADR-0016 are unchanged.

### 12. The Gate 3 slices

After this ADR is audited, cross-audited and merged, later pre-LIVE slices are authorized **one at
a time**, never in parallel. Their exact boundaries are decided after this contract lands. The
expected areas, none authorized by this ADR:

| area | content |
| --- | --- |
| 1 | the bounded grant and the protected-write brake as durable owners, integrated deny-by-default into execution; still provider-zero |
| 2 | the backup/restore drill and the evidence-retention proof |
| 3 | the populated visual/responsive acceptance |
| 4 | a provider-evidence re-review, and any endpoint adoption **only if** new official evidence resolves §6 |
| 5 | one explicitly user-authorized, non-regulated SmartStore canary, only after every prerequisite is green |

### 13. What this ADR does not decide

- table names, columns, enum spellings, route paths, payload shapes and screen layout for the grant,
  the brake, the drill record or the eligibility record — each slice decides them within this
  boundary and its own review;
- the maximum grant window length and the exact budget values, beyond "finite" and "at least 1";
- the backup mechanism and file format, beyond WAL-consistency and restore into a separate root;
- a deletion policy after M5 acceptance (§8);
- the final viewport set beyond the two established sizes (§9);
- the ComplianceGate contract, CREATE/SEARCH adoption, and anything M6 or M6.5.

---

## Invariants

```text
G3-01  M0_DRY_RUN_ONLY / M0_LIVE_FORBIDDEN stays the only execution policy until a slice replaces it under this contract; nothing in G3-0 permits a LIVE write
G3-02  a marketplace mutation needs an ACTIVE LIVE grant that matches marketplace, account, operation, endpoint group and unit exactly, within its window, with budget left; otherwise it is refused before any transmission
G3-03  a grant is durable, audited and server-owned; UI text and checkboxes are never authority
G3-04  a grant binds a finite not_before/expires_at window and a finite mutation budget of at least 1; an attempt consumes budget when started and is never refunded, an UNKNOWN included
G3-05  EXPIRED, REVOKED and EXHAUSTED are terminal; no reload, restart, retry or brake release widens a grant or recreates one
G3-06  a grant never changes endpoint adoption, capability or write scope, readiness, ComplianceGate state, provider truth, product_registration.write or ReviewItem state
G3-07  a grant never authorizes a blind CREATE replay; an UNKNOWN stays governed by ADR-0014 §10 and keeps its conflict scope closed
G3-08  the protected-write brake is server-owned, durable and fail-closed: absent or unreadable means ENGAGED, and a restart never releases it
G3-09  an engaged brake stops every mutation not yet started; it never deletes or rewrites history and never rewrites an UNKNOWN
G3-10  releasing the brake needs a new explicit audited authorization and never resurrects an expired, revoked or exhausted grant
G3-11  every layer of the safety stack must allow a mutation at send time; the ADR-0014 §26 execution-scope brake is unchanged and not weakened
G3-12  Gate 3 implements no ComplianceGate and puts no compliance logic in a grant
G3-13  the first canary uses only a product proven outside every regulated category by its reviewed category metadata; that proof is eligibility, never a COMPLIANCE PASS, and without it the canary stays BLOCKED
G3-14  CREATE and SEARCH stay NOT_ADOPTED and the provider-evidence verdict stays INSUFFICIENT; no grant, brake, backup, retention, visual acceptance or approval overrides it
G3-15  an ICBM seller-side code and a zero-result search are never proof of remote absence
G3-16  a canary needs a recorded backup and restore drill into a separate fresh root on the current schema head that proves, by identity and state, the complete canary-critical chain existing at drill time, including the RegistrationSnapshot, the RegistrationIntent with its idempotency key and state, and the execution-scope brake state; an element not yet existing is recorded as absent, never created; a declaration is not a drill
G3-17  canary evidence is sanitized before hash or persist, durable REGISTER rows are never deleted, no canary evidence is deleted before M5 acceptance, and evidence tied to an unresolved condition is never discarded
G3-18  a canary needs a recorded populated visual and responsive acceptance at the accepted viewport set in which no server-owned blocker is hidden
G3-19  canary readiness is derived and read-only, stays BLOCKED until every requirement holds, and is never permission to write
G3-20  M5 stays PENDING, product_registration.write stays UNVERIFIED, the canary stays BLOCKED and M6/M6.5 stay unstarted until their own decisions
```

## Consequences

- The LIVE-authorization gap (`ROADMAP.md` §14.1) and the §14.2 preconditions now have a contract,
  but **no implementation**. The execution-mode owner still refuses LIVE.
- The M5 canary readiness gains named requirements (§10). Every one of them is missing at this main,
  and CREATE/SEARCH adoption stays missing independently, so the canary is `BLOCKED` for several
  independent reasons at once.
- A later slice that implements a grant or the brake adds a migration under its own authorization.

## References

- Gate 3 kickoff: Issue #89 `5821078540`.
- Provider-evidence verdict: Issue #89 `5768247290` → `5768312853` → `5768347233`.
- Gate 2 acceptance: `5818393660`, cross-audit `5818648647`; Gate 1 acceptance: `5804516180`.
- `ROADMAP.md` §14; `docs/ARCHITECTURE.md` §7, §13; `docs/acceptance/M5.md` §6, §9; `docs/GLOSSARY.md` §4.
- ADR-0011, ADR-0014, ADR-0015, ADR-0016.
