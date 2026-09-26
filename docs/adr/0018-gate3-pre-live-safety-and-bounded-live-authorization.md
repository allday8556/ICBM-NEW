# ADR-0018 — Gate 3: pre-LIVE safety and the bounded LIVE authorization contract

Status: **ACCEPTED** — decided by the architect kickoff `5821078540` (2026-09-24 19:47 UTC; 2026-09-25 KST). This is G3-0
of Gate 3 (Issue #89), on exact main `267d6a9eb20788819a8863f59a9c8f8e47700870` (post-merge CI
`36045605845`, 6/6), after the Gate 2 acceptance (`5818393660`, cross-audit `5818648647`).
- It records the kickoff's seven decisions (D1–D7) as contract, before any schema or runtime. The
  decisions were made by the kickoff. Where the kickoff asked the contract to define a rule, the
  rule is defined here and is open to the exact-head audit.
- Amended before merge by the reviews `5821787401` (the restore drill proves the REGISTER chain)
  and `5822405880` (the canary has two mutation stages, ASSET and CREATE, each with its own
  grant binding, restore proof and send-time readiness — §3.1, §7, §10), and `5823321537` (a durable
  ASSET upload-attempt owner is a prerequisite of any upload; the ADR-0014 §26 scope brake stays
  CREATE-only — §3.4, §4.3, §7, §10), and `5823765435` (the upload replay fence is keyed by the
  provider mutation itself, never narrowed by local provenance — §3.2, §3.4, §7, §10), and
  `5824235764` (a `derivation_id` or a local artifact kind is provenance only, and `APPLIED_PROVEN`
  is fenced by the same key — §3.4), and `5825163444` (the key is the conservative wire boundary:
  marketplace, canonical account, wire method, host and path, and the outbound content digest; a
  file name, MIME or type metadata and any local contract or adoption label are provenance only —
  §3.4, Consequences).
- **Its implementation authority becomes effective only after this exact contract PR is audited,
  independently cross-audited and merged**, and even then only slice by slice (§12).
- Amended after merge by the architect decision `5845062336` (design `5845034124`, after the
  area 4 closeout `5844770185`):
  - §6.1 records the revised safety strategy of ADR-0014 §28 — never resend while the outcome is
    unknown, positive-only reconcile, durable ambiguity isolation — and its explicit
    residual-risk acceptance gate;
  - §9, §10, §11, G3-14 and the new G3-30 and G3-31 follow it.
  - **The provider-evidence verdict stays `INSUFFICIENT`, and nothing is adopted or authorized
    to run.**

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
  execution-scope rules — as amended by its own §28 (`5845062336`) — stay the only authority for
  what they govern. This ADR adds a layer in front of them and changes no sentence, invariant or
  ruling of ADR-0014.
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
populated visual/responsive acceptance (§9). §10 composes them into a readiness for **each
mutation stage**, enforced at send time.

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

#### 3.1 The two mutation stages of one canary

A canary makes its marketplace mutations in the order ADR-0014 §5 fixes, and never in another:

```text
candidate preflight READY
→ ASSET stage:  upload the exact selected, QA-passed artifacts (image upload)
→ final preflight READY
→ freeze the RegistrationSnapshot and open its PREPARED RegistrationIntent
→ CREATE stage: send that Intent (product CREATE)
```

The **ASSET** stage happens before any Snapshot or Intent exists; the **CREATE** stage happens only
after the freeze. Each stage is its own mutation identity with its own grant (§3.2), its own restore
proof (§7) and its own send-time readiness (§10). **A grant, proof or readiness of one stage never
covers, widens into or automatically authorizes the other.** A unit that needs no provider asset has
no ASSET stage; the CREATE stage is then the first mutation, and nothing of it is relaxed.

#### 3.2 What a grant binds, and every field is exact

**Every grant names one stage and one exact unit of that stage. No unit-less grant and no
wildcard exists.**

| field | ASSET grant | CREATE grant |
| --- | --- | --- |
| `marketplace_key`, `marketplace_account_id` | one marketplace and one canonical account (GLOSSARY §1), bound at grant time | the same |
| operation / endpoint group | the adopted image-upload endpoint group only | the product CREATE endpoint group only |
| exact unit | the exact **preparation revision**, the exact **candidate fingerprint** of its `READY` candidate preflight, the exact **selected artifact set** (each artifact's kind, SHA-256 and derivation identity) and the requested **marketplace asset profile** | the exact **`RegistrationSnapshot`** and its listing identity, and the exact **`RegistrationIntent`** with its **idempotency key** |
| mutation budget | a finite maximum number of mutation attempts, at least 1; each attempt is counted when it is started, and never refunded, whatever its outcome — an `UPLOAD_UNKNOWN` included | the same — an Intent `UNKNOWN` included |
| approval | the approving user's identity, the GitHub authorization reference the approval answers, and the audit correlation identity | the same, recorded separately for this stage |
| window | a finite `not_before` and `expires_at`; no open-ended grant exists, and the maximum window length is a server-owned bound the implementing slice fixes and pins | the same |
| state | `ACTIVE` until it becomes `EXPIRED`, `REVOKED` or `EXHAUSTED`; each of those is terminal | the same |

An ASSET grant stops matching as soon as its bound preparation revision, candidate fingerprint,
artifact set or asset profile no longer is the current one; a CREATE grant stops matching when its
Snapshot is superseded or its Intent leaves `PREPARED` other than through its own attempt.

**A grant's exact unit is authorization provenance, never a replay boundary.** It narrows what a
grant may authorize; it never narrows which earlier upload blocks a new one. That is the ASSET
replay-conflict key of §3.4, which no grant, preparation revision or candidate fingerprint enters.

#### 3.3 Rules

- **Deny by default.** No active grant of the mutation's stage that matches it exactly —
  marketplace, account, endpoint group and the stage's exact unit (§3.2), within its window, with
  budget left — means the mutation is refused **before any transmission**, and the refusal is
  audited.
- **No wildcard and no widening.** A grant never matches another account, marketplace, stage,
  endpoint group or unit. An ASSET grant never authorizes a CREATE, and a CREATE grant never an
  upload. A reload, restart, retry, job re-run or new batch can neither widen a grant
  nor recreate an expired, revoked or exhausted one. A new scope needs a new grant.
- **Terminal states are terminal.** `EXPIRED`, `REVOKED` and `EXHAUSTED` never return to `ACTIVE`.
  Revocation is an explicit audited action and takes effect for every mutation not yet started.
- **A grant decides nothing else.** It never changes endpoint-adoption state, CONNECT capability or
  write-scope truth, registration readiness or preflight, ComplianceGate state, provider truth,
  `product_registration.write` status or ReviewItem state.
- **A grant never authorizes a blind replay** — of a CREATE or of an upload. An Intent `UNKNOWN`
  stays governed by ADR-0014 §10: it is reconciled only with admissible evidence, it keeps its
  conflict scope closed, and no grant, budget or approval turns it into `FAILED` or
  `NOT_APPLIED_PROVEN`. An `UPLOAD_UNKNOWN` (ADR-0014 §17.1, distinct from the Intent `UNKNOWN`)
  is never retried automatically, is never treated as a known provider asset identity, and is
  never re-uploaded blindly; its evidence is kept while it is unresolved (§8), in the durable
  ASSET upload-attempt owner (§3.4).
- **A grant and a restore proof authorize only the state they were issued for.** After a CREATE
  attempt proven `NOT_APPLIED_PROVEN`, any retry ADR-0014 permits for the same durable Intent and
  idempotency key needs a **new CREATE grant and a fresh restore proof and readiness**; an
  `UNKNOWN` still forbids any resend.
- **UI text and checkboxes are never authority.** Only the server-owned grant is. A confirmation
  typed in the UI is an input to the protected action that creates the grant, never the grant.
  **Raw confirmation prose an operator enters is never persisted, hashed or logged**: the durable
  grant keeps only the approved safe identities, the approver's identity and the authorization
  reference.
- **Every grant transition is audited** in the same unit of work: created, activated, consumed
  (per attempt), expired, revoked, exhausted — with actor, time and correlation identity.

#### 3.4 The durable ASSET upload-attempt owner — a prerequisite of any upload

ADR-0014 §17.1 records the state its amendment left: the bounded image upload is adopted with **no
durable upload owner, cache or ledger**, and a possibly transmitted failure is `UPLOAD_UNKNOWN`. A
`PreparedAsset` is only an input to the final preflight, and a `RegistrationAttempt` belongs to an
Intent that does not exist before the freeze. Without a durable owner, **nothing durably records that
an upload may already have been transmitted**, and every fence of the ASSET stage — no blind
re-upload, no unresolved `UPLOAD_UNKNOWN`, retained upload evidence — would rest on the absence of a
record. **Therefore `ASSET_MUTATION_READY` is necessarily `BLOCKED` whenever that owner is absent.**

**One server-owned, durable ASSET upload-attempt owner** is required before `ASSET_MUTATION_READY`
can ever be `READY`. The separately authorized Gate 3 area 1 slice (§12) created it, provider-zero
(migration `0026_g3_live_authority`, `app/live/store.py`, `app/live/assets.py`). Its presence is not
readiness and authorizes no upload (§10). This ADR freezes its semantics; its tables and columns are
that slice's:
- **Provenance.** One durable attempt identity is bound, for audit, to the exact ASSET grant, the
  marketplace and canonical account, the exact preparation revision, the candidate fingerprint, the
  local artifact tuple (the local source-or-derived artifact kind, SHA-256 and `derivation_id`),
  the asset profile, the upload endpoint group and its local adoption or contract label, the
  multipart file name and MIME or type metadata actually sent, an optional sanitized canonical
  wire-request digest, and an attempt number and correlation identity. **Provenance records why
  and under what an attempt was made; it never decides which attempts block another** (reviews
  `5823765435`, `5824235764`, `5825163444`).
- **Replay-conflict key — the conservative wire boundary.** Separately, every attempt carries an
  **ASSET replay-conflict key** that identifies the provider mutation by the widest boundary no
  local choice can split. For the adopted SmartStore image upload (ADR-0014 §17.1: one
  `POST /v1/product-images/upload` with one `imageFiles` multipart part) it is **exactly**:
  - the marketplace;
  - the canonical account;
  - the normalized wire endpoint identity: HTTP method, provider host and path;
  - the exact outbound content digest of the uploaded binary.

  The path includes a version segment only when that segment is actually in the path. **Nothing
  else keys a replay scope. Provenance only, never a key field, and never narrowing the scope**: the
  multipart file name, MIME or type metadata, the local source-or-derived artifact kind, the
  `derivation_id`, the candidate fingerprint, preparation revision, grant, Draft revision, listing
  text, category, policy state, any local profile label, a local endpoint-mapping revision, a
  provider-document version label and any ICBM adoption or contract label. **Even when such a
  value is serialized on the wire, it never makes a new replay key** for the same account, wire
  endpoint and content digest: the same bytes sent under another file name or MIME type are the
  same scope, and a changed ICBM contract or adoption label with an unchanged method, host and path
  never opens a new one. A local identity used to derive a serialized file name or type is no
  exception. So two derivations, or a source and a derived artifact, with the same outbound bytes
  are **one** replay-conflict scope — M4 lets another recipe or execution producing equal bytes be
  another derivation sharing one artifact (`app/products/images.py`). The candidate fingerprint in
  particular spans many non-asset dependencies (listing, category, pricing, policy, capability,
  duplicate evidence); editing any of them leaves the same upload in the same scope.

  **Ambiguity is resolved by the wider scope, never by inventing another key.** A later
  implementation may persist a sanitized canonical wire-request digest as evidence and provenance,
  never as a key that narrows this fence. **If the wire endpoint identity or the outbound content
  digest cannot be determined, the ASSET stage stays `BLOCKED`.**
- **Start before transmission, atomically with the budget.** Before any provider transmission, the
  attempt is durably recorded as **started in the same atomic unit of work that consumes the ASSET
  grant's budget**. If that commit fails, **nothing is transmitted**.
- **Terminal exactly once.** After the provider call, the attempt is terminalized once, as
  `APPLIED_PROVEN`, `NOT_APPLIED_PROVEN` or **`UPLOAD_UNKNOWN`**, with its sanitized evidence (§8).
- **Crash and restart fail closed.** A started attempt that is not terminal after a crash or
  restart is treated as unresolved — `UPLOAD_UNKNOWN` — unless admissible evidence proves that
  transmission was precluded. A restart never erases this fence.
- **Only `APPLIED_PROVEN` yields a known provider asset.** It is the only attempt state from which a
  known `PreparedAsset.provider_asset_ref` may come; nothing else — an `UPLOAD_UNKNOWN`, a started
  attempt, an operator entry — may supply one.
- **No record is not proof.** Missing, unreadable or stale upload-attempt truth is never proof that
  no unresolved upload exists; it keeps the ASSET stage `BLOCKED`.
- **Replay fence, over the whole replay-conflict scope.** A started or unresolved `UPLOAD_UNKNOWN`
  attempt **anywhere in a replay-conflict key's scope** blocks every new upload with that key —
  **across a new grant, a new preparation revision, a new candidate fingerprint, another
  derivation or local artifact kind of the same bytes, another file name or MIME or type
  metadata, a local profile or contract/adoption label change, a restart, a job re-run or a new
  batch** — until separately admissible reconciliation or reuse evidence resolves it
  (ADR-0014 §5). Changing local provenance never erases an unresolved
  remote-mutation ambiguity.
  - A `NOT_APPLIED_PROVEN` attempt may clear that ambiguity for a retry, and the retry still needs
    a new or current matching ASSET grant, a current `ASSET_MUTATION_READY` and a fresh ASSET
    restore proof.
  - **The same key governs `APPLIED_PROVEN`.** An `APPLIED_PROVEN` attempt in a replay-conflict
    scope **keeps a fresh upload with that key blocked**: the same content to the same account and
    wire endpoint is **never re-sent merely because the file name, MIME or type metadata,
    derivation, local artifact kind, candidate, preparation, grant, profile or local contract label
    changed**. Its provider asset identity may be reused or rebound only through a separately
    adopted reuse/rebind path; nothing here adopts one, so until one is adopted a fresh upload in
    that scope stays blocked. This is deliberately over-conservative (see Consequences).

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
3. an `ACTIVE` grant **of the mutation's stage** matches its exact unit, with budget left (§3);
4. the stage's mutation readiness is `READY` — `ASSET_MUTATION_READY` before an upload,
   `CREATE_MUTATION_READY` before a CREATE (§10) — which enforces §5, §7, §8 and §9 for that stage,
   and for the ASSET stage the durable upload-attempt owner (§3.4);
5. the endpoint group is adopted, and the capability and write scope allow it (unchanged owners);
6. **for the CREATE stage only**, the REGISTER execution-scope send brake for that scope is
   `ACTIVE` (ADR-0014 §26 — **CREATE-only, unchanged and not weakened**; the two brakes are
   independent, and releasing either releases nothing of the other). **The ASSET stage has no §26
   scope owner and does not pretend one exists**: it is bounded by the global brake, its exact
   grant and finite budget, the durable upload-attempt owner (§3.4) and `ASSET_MUTATION_READY`; an
   ASSET failure-budget brake, if one is wanted, needs its own authorization;
7. for an upload, the attempt is durably started together with its budget consumption (§3.4)
   and the candidate preflight passes; for a CREATE, the final preflight and the ADR-0014 §3
   send-time gate pass — in each case with no unresolved conflict.

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
  can override that verdict.** The area 4 re-review confirmed it (`5844652548`, closeout
  `5844770185`). It holds for idempotent replay and for remote-absence proof, and it is never
  turned into either.
- In particular, **an ICBM seller-side code and a zero-result search remain insufficient proof of
  remote absence** (ADR-0014 §7, §17.2).
- The canary stays **`BLOCKED`**. Under the strategy of §6.1, it can leave `BLOCKED` only when all
  of these hold:
  - CREATE and the positive-only reconcile path are separately adopted under ADR-0014 §28;
  - every other prerequisite of this ADR is green;
  - the residual-risk acceptance of §6.1 is recorded.

#### 6.1 The revised safety strategy (architect decision `5845062336`)

```text
before    prove remote absence before retry
now       never resend while the outcome is unknown
          + positive-only reconcile
          + durable ambiguity isolation                    (ADR-0014 §28)
```

- **This is a deliberate change of the canary's safety strategy, not a rewording.** It weakens no
  refusal:
  - an `UNKNOWN` is never resent and keeps its conflict scope closed (G3-07);
  - no grant is issued for it;
  - no lookup, code or approval proves absence (G3-15).
- **Why the verdict no longer has to be overturned:** under this strategy no path resends on an
  unknown outcome, and no path needs remote absence. An `UNKNOWN` ends only on positive evidence of
  presence followed by Snapshot verification, or on later machine proof of non-application
  (ADR-0014 §28.2, §28.3).
- **What the endpoints still need, each in its own separately authorized adoption slice:**
  - CREATE, under the official wire contract: request, response, and an error classification that
    separates a definitive rejection from an ambiguous outcome;
  - SEARCH, limited to positive reconcile: exact request, response and pagination contract.
- **The residual risk, which must be accepted explicitly:**

  > a product may be live in SmartStore after an ambiguous CREATE while ICBM has not yet recovered
  > the provider identity, leaving that listing temporarily outside normal confirmed price/stock
  > monitoring.

  **Opening any real canary under this contract requires a separate, explicit user and architect
  acceptance of this residual risk**, recorded in GitHub, in addition to every other Gate-3
  prerequisite. The acceptance authorizes nothing by itself. It is one more precondition, and no
  grant, proof or readiness implies it.

### 7. Backup and restore: a proven drill, not a declaration (D5)

A restore proof is **stage-bound and freshness-bound**. The mutation it gates needs its own proof,
taken for that exact target and state; one stage's proof never gates the other.

**Every drill:**
- a backup is taken from the canonical data root, consistent with SQLite WAL (a copy of the live
  database file alone is not a backup);
- it is restored into a **separate fresh root** — never over the active data root;
- the restored root proves that the schema is at the expected Alembic head and that the database is
  readable and passes its integrity check;
- every element below is compared with the source root **by identity and state**, so a restore that
  loses or changes one fails;
- **only state that legitimately cannot yet exist at that stage may be absent**, and it is recorded
  as absent, **never created for the drill**; the drill writes nothing to the active root and
  fabricates nothing in either root;
- its sanitized evidence (identities, states, counts, digests, versions, times, every element
  recorded as absent, and the **target state digest** below) is recorded; no credential, secret or
  raw payload enters it.

**The ASSET restore proof** — before an upload — proves the exact pre-upload chain:
- the product side: the source revision, the Product and Item, the canonical account, and the
  target-policy and category-metadata revisions;
- the preparation side: the Draft and the exact preparation revision, and the candidate preflight
  fingerprint the ASSET grant binds;
- the exact selected artifact set (kind, SHA-256, derivation identity) with its QA, and the asset
  profile;
- **the durable upload-attempt and replay state over the whole replay-conflict scope** (§3.4) of
  every selected artifact: every attempt with that replay-conflict key, with its state, **whatever
  grant, preparation revision, candidate fingerprint or local profile it was started under** — or
  the owner's own readable record that none exists, never the mere absence of rows; a proof that
  inspects only the current candidate's or profile's attempts proves nothing; **no ADR-0014 §26
  scope row is part of an ASSET proof**;
- recorded as absent, because they cannot exist yet: the `RegistrationSnapshot`, the
  `RegistrationIntent`, its Attempts and any registration.

**The CREATE restore proof** — after the freeze and before a CREATE, taken **after** the freeze —
proves the exact post-freeze chain:
- the product and preparation sides as above, and the prepared provider asset identities the
  Snapshot froze;
- **the REGISTER chain** (review `5821787401`):
  - the canary unit's immutable `RegistrationSnapshot` — its listing identity, payload hash,
    preflight fingerprint and item snapshots;
  - its `RegistrationIntent` in `PREPARED` — the intent identity, the **idempotency key**, and its
    state, remote outcome and verification state;
  - the REGISTER **execution-scope brake** state (ADR-0014 §26) for the CREATE endpoint group;
  - when they exist: an unresolved conflict scope and a duplicate override;
- **the Snapshot and the Intent may never be recorded as absent** in a CREATE restore proof: a
  proof without them does not gate a CREATE. Only what cannot yet exist before the CREATE — its
  Attempts and the registration — may be recorded as absent. **A pre-freeze proof is never accepted
  as a CREATE restore proof.**

**Freshness.** Each proof records a **target state digest** over exactly what it proved for its
stage: the preparation revision, candidate fingerprint, artifact set, asset profile and the
upload-attempt state of the whole replay-conflict scope for ASSET; the Snapshot, the Intent's identity and state, the idempotency key
and the scope state for CREATE. A proof gates a mutation **only while that digest equals the
current state**: if the bound candidate, preparation, artifact set, upload-attempt state,
Snapshot, Intent state or CREATE scope state changes,
the proof is **stale** and gates nothing until a new drill proves the new state.

A document saying that backups exist is not a drill. **A stage without its own current restore
proof stays `BLOCKED`.**

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
  - The grant and brake history (§3, §4) and the audit trail — never the raw confirmation prose
    an operator typed (§3.3).
- **Sanitation before hash or persist** stays the rule (ADR-0014 §15): no credential, token,
  cookie, session material, raw private payload or unsafe or signed URL is ever hashed or stored.
  A value the sanitizer cannot classify is not persisted, and the dependent verification becomes
  `REVIEW_REQUIRED`.
- **Retention duration and deletion boundary.**
  - **No automatic deletion** of canary-scope REGISTER evidence is authorized before M5 acceptance
    is decided. A later deletion policy needs its own decision.
  - **Evidence tied to an unresolved condition is never discarded**, silently or by any policy,
    while that condition is unresolved. That covers an `UNKNOWN` Intent, an **`UPLOAD_UNKNOWN`**
    (ADR-0014 §17.1), a read-back `MISMATCH`, an open review item that references it, and an
    open conflict scope. An `UPLOAD_UNKNOWN` is never recorded as a known provider asset
    identity, and its evidence is what any later reconcile or reuse must rest on.
  - Any later deletion is itself audited, and it removes only sanitized raw artifacts, never
    durable rows.
- **Fail closed.** If the retention path cannot store the evidence a mutation requires, the mutation
  does not start, and the canary stays `BLOCKED`.

### 9. Populated visual and responsive acceptance (D7)

The first canary may not be authorized from functional Playwright wiring tests alone. Before its
**first** mutation — the ASSET stage when the unit needs one — and again for the CREATE stage when
the accepted code SHA changed in between:
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

**The registration read state (ADR-0014 §28.5)** — the lower-right registration status card and its
detail panel — joins the required surfaces and state selectors of this acceptance, or their
successor, when it is implemented. It is proven inside the harness, never re-recorded outside it.
Any merged commit is a new accepted code SHA, so an earlier visual acceptance never covers it.

### 10. Mutation-stage readiness

Each stage has its own derived, read-only readiness, and **it is a mandatory layer of the send-time
safety stack (§4.3)**: a mutation of a stage cannot start unless that stage's readiness is `READY`
at send time. Each requirement is proven from its own durable evidence, never asserted.

| requirement | `ASSET_MUTATION_READY` (before an upload) | `CREATE_MUTATION_READY` (before a CREATE) |
| --- | --- | --- |
| endpoint adoption (§6) | `IMAGE_UPLOAD_ADOPTED` | `CREATE_ADOPTED` and `RECONCILE_PATH_ADOPTED`, the latter positive-only (ADR-0014 §28) — **not met** |
| residual-risk acceptance (§6.1) | the canary cannot open without it | an explicit, recorded user and architect acceptance — **not recorded** |
| grant (§3) | an `ACTIVE` ASSET grant matching the exact preparation revision, candidate fingerprint, artifact set and asset profile | an `ACTIVE` CREATE grant matching the exact Snapshot, Intent and idempotency key |
| protected-write brake (§4) | `RELEASED` | `RELEASED` |
| canary eligibility (§5) | `CANARY_NON_REGULATED` | `CANARY_NON_REGULATED` |
| restore proof (§7) | a current ASSET restore proof for that exact target | a current CREATE restore proof for that exact target, taken after the freeze |
| evidence retention (§8) | `EVIDENCE_RETENTION_READY` | `EVIDENCE_RETENTION_READY` |
| visual acceptance (§9) | `VISUAL_ACCEPTANCE_RECORDED` at the accepted SHA | `VISUAL_ACCEPTANCE_RECORDED` at the accepted SHA |
| durable attempt owner (§3.4) | the ASSET upload-attempt owner present, readable, current and able to persist the required sanitized evidence; **no started or unresolved `UPLOAD_UNKNOWN` attempt in the replay-conflict scope** (§3.4) of any selected artifact, whatever grant, preparation revision, candidate fingerprint or local profile it was started under, proven from that owner and never from row absence | — (CREATE attempts are ADR-0014's `RegistrationAttempt`) |
| the stage's own gate | candidate preflight `READY` | final preflight `READY`; a `PREPARED` Intent; no unresolved conflict or `UNKNOWN` |
| the existing requirements | account binding, auth, write scope, one unit only — **no ADR-0014 §26 scope row** | the same, and the ADR-0014 §26 execution-scope brake `ACTIVE` (M5.md §6) |

- **The ASSET stage never depends on a `PREPARED` Intent**, which cannot exist before its upload; and
  no requirement is circular.
- **`ASSET_MUTATION_READY` is `BLOCKED` at this main.** The durable upload-attempt owner of §3.4
  exists (area 1), so it is not what blocks: the execution policy is still `M0_DRY_RUN_ONLY`, no
  ASSET sender is wired, and canary eligibility (§5) has no owner, each of which refuses on its own.
  It also stays `BLOCKED` whenever the §3.4 owner is absent, unreadable, stale or unable to persist
  the required evidence.
- **The ASSET readiness queries the whole replay-conflict scope**, never only the attempts of the
  current candidate, preparation, grant or profile; a readiness that does is not
  `ASSET_MUTATION_READY`.
- The overall canary readiness (`docs/acceptance/M5.md` §6) only **summarizes** the two stages. It is
  derived, read-only and authorizes nothing: even `READY` is not permission. A real write stays a
  separate, explicitly user-authorized, single-product canary (ADR-0014 §24), and while CREATE and
  SEARCH are `NOT_ADOPTED` the CREATE stage — and so the canary — stays `BLOCKED`.

### 11. Unchanged

- M5 stays **`PENDING`**; `docs/acceptance/M5.md` records no acceptance run.
- `SMARTSTORE_PRODUCT_CREATE_V2` and `SMARTSTORE_PRODUCT_SEARCH` stay **`NOT_ADOPTED`**; no
  category, attribute, standard-option or notice endpoint is adopted.
- `product_registration.write` stays **`UNVERIFIED`**.
- Execution stays **`DRY_RUN` / `M0_DRY_RUN_ONLY`**; LIVE stays forbidden.
- The canary stays **`BLOCKED`**; marketplace mutations stay **0**.
- ComplianceGate has no owner; M6 and M6.5 are not started.
- ADR-0014 §9, §15, §24 and §26, ADR-0011, ADR-0015 and ADR-0016 are unchanged. ADR-0014 §10,
  §11, §17.2 and §22 are narrowed only by ADR-0014 §28 (`5845062336`).

### 12. The Gate 3 slices

After this ADR is audited, cross-audited and merged, later pre-LIVE slices are authorized **one at
a time**, never in parallel. Their exact boundaries are decided after this contract lands. The
expected areas, none authorized by this ADR:

| area | content |
| --- | --- |
| 1 | the bounded grant, the protected-write brake and the durable ASSET upload-attempt owner (§3.4) as durable owners, integrated deny-by-default into execution; still provider-zero |
| 2 | the backup/restore drill and the evidence-retention proof |
| 3 | the populated visual/responsive acceptance |
| 4 | a provider-evidence re-review; it closed confirming `INSUFFICIENT` (`5844770185`). Adoption is not part of it and never waits for the verdict to be overturned: CREATE and the positive-only reconcile path each need their own separately authorized adoption slice (§6.1, ADR-0014 §17.2, §28) |
| 5 | one explicitly user-authorized, non-regulated SmartStore canary, only after every prerequisite is green |

### 13. What this ADR does not decide

- table names, columns, enum spellings, route paths, payload shapes and screen layout for the grant,
  the brake, the ASSET upload-attempt owner, the drill record or the eligibility record — each slice decides them within this
  boundary and its own review;
- the maximum grant window length and the exact budget values, beyond "finite" and "at least 1";
- the backup mechanism and file format, beyond WAL-consistency and restore into a separate root,
  and the encoding of the target state digest (§7);
- a deletion policy after M5 acceptance (§8);
- the final viewport set beyond the two established sizes (§9);
- the ComplianceGate contract, CREATE/SEARCH adoption, and anything M6 or M6.5.

---

## Invariants

```text
G3-01  M0_DRY_RUN_ONLY / M0_LIVE_FORBIDDEN stays the only execution policy until a slice replaces it under this contract; nothing in G3-0 permits a LIVE write
G3-02  a marketplace mutation needs an ACTIVE LIVE grant of its stage that matches marketplace, account, endpoint group and the stage's exact unit, within its window, with budget left; otherwise it is refused before any transmission
G3-03  a grant is durable, audited and server-owned; UI text and checkboxes are never authority
G3-04  a grant binds a finite not_before/expires_at window and a finite mutation budget of at least 1; an attempt consumes budget when started and is never refunded, an UNKNOWN included
G3-05  EXPIRED, REVOKED and EXHAUSTED are terminal; no reload, restart, retry or brake release widens a grant or recreates one
G3-06  a grant never changes endpoint adoption, capability or write scope, readiness, ComplianceGate state, provider truth, product_registration.write or ReviewItem state
G3-07  a grant never authorizes a blind CREATE or upload replay; an UNKNOWN stays governed by ADR-0014 §10 and keeps its conflict scope closed
G3-08  the protected-write brake is server-owned, durable and fail-closed: absent or unreadable means ENGAGED, and a restart never releases it
G3-09  an engaged brake stops every mutation not yet started; it never deletes or rewrites history and never rewrites an UNKNOWN
G3-10  releasing the brake needs a new explicit audited authorization and never resurrects an expired, revoked or exhausted grant
G3-11  every layer of the safety stack must allow a mutation at send time; the ADR-0014 §26 execution-scope brake stays CREATE-only, unchanged and not weakened, and gates the CREATE stage only
G3-12  Gate 3 implements no ComplianceGate and puts no compliance logic in a grant
G3-13  the first canary uses only a product proven outside every regulated category by its reviewed category metadata; that proof is eligibility, never a COMPLIANCE PASS, and without it the canary stays BLOCKED
G3-14  CREATE and SEARCH stay NOT_ADOPTED until separately authorized adoption slices under ADR-0014 §28, and the provider-evidence verdict stays INSUFFICIENT for idempotent replay and remote-absence proof; no grant, brake, backup, retention, visual acceptance or approval overrides it or turns it into such a proof
G3-15  an ICBM seller-side code and a zero-result search are never proof of remote absence
G3-16  each mutation stage needs its own current restore proof into a separate fresh root on the current schema head, bound to a target state digest and stale once that state changes; a CREATE restore proof is taken after the freeze and proves, by identity and state, the RegistrationSnapshot, the RegistrationIntent with its idempotency key and state, and the execution-scope brake state, which it may never record as absent; a pre-freeze proof never gates a CREATE; an ASSET restore proof proves the durable upload-attempt state over the whole replay-conflict scope and never an ADR-0014 §26 row; only state that cannot yet exist is recorded as absent, never created; a declaration is not a drill
G3-17  canary evidence is sanitized before hash or persist, durable REGISTER rows are never deleted, no canary evidence is deleted before M5 acceptance, and evidence tied to an unresolved condition is never discarded
G3-18  a canary needs a recorded populated visual and responsive acceptance at the accepted viewport set in which no server-owned blocker is hidden
G3-19  ASSET_MUTATION_READY before an upload and CREATE_MUTATION_READY before a CREATE are mandatory send-time layers requiring the stage's grant, brake, eligibility, restore proof, retention and visual acceptance; the ASSET stage never depends on a PREPARED Intent; readiness is derived, read-only and never permission to write
G3-20  M5 stays PENDING, product_registration.write stays UNVERIFIED, the canary stays BLOCKED and M6/M6.5 stay unstarted until their own decisions
G3-21  every grant names one stage and one exact unit: an ASSET grant binds the preparation revision, candidate fingerprint, selected artifact set and asset profile, a CREATE grant the Snapshot, Intent and idempotency key; no unit-less or wildcard grant exists, and one stage never widens into the other
G3-22  an UPLOAD_UNKNOWN is never retried automatically, never treated as a known provider asset identity and never re-uploaded blindly, and its evidence is kept while it is unresolved
G3-23  raw confirmation prose an operator enters is never persisted, hashed or logged; a grant stores only safe identities, the approver and the authorization reference
G3-24  no upload is transmitted unless a durable ASSET upload-attempt owner has recorded the attempt as started in the same atomic unit that consumes the ASSET grant budget; each attempt is terminalized exactly once as APPLIED_PROVEN, NOT_APPLIED_PROVEN or UPLOAD_UNKNOWN
G3-25  a started attempt not terminal after a crash or restart is UPLOAD_UNKNOWN unless admissible evidence proves transmission was precluded; missing or unreadable attempt truth is never proof that no unresolved upload exists; only APPLIED_PROVEN yields a known provider asset identity
G3-26  ASSET_MUTATION_READY requires that durable owner and no started or unresolved UPLOAD_UNKNOWN attempt in the replay-conflict scope of any selected artifact, and is BLOCKED while the owner does not exist; the ASSET stage never depends on an ADR-0014 §26 scope row
G3-27  a CREATE grant and restore proof authorize only the state they were issued for; after a NOT_APPLIED_PROVEN attempt any permitted retry needs a new CREATE grant and a fresh restore proof and readiness, and an UNKNOWN still forbids any resend
G3-28  attempt provenance and the ASSET replay-conflict key are separate: the key is exactly the marketplace, the canonical account, the normalized wire endpoint identity (HTTP method, provider host and path) and the exact outbound content digest; the multipart file name, MIME or type metadata, local artifact kind, derivation_id, candidate fingerprint, preparation revision, grant, Draft revision, listing, category, policy state, local profile label, local endpoint-mapping revision, provider-document version label and any ICBM adoption or contract label are provenance only and never enter or narrow it, even when serialized on the wire; ambiguity takes the wider scope; an undeterminable wire endpoint identity or content digest keeps the ASSET stage BLOCKED
G3-29  a started or unresolved UPLOAD_UNKNOWN anywhere in a replay-conflict scope blocks every new upload with that key across a new grant, preparation revision, candidate fingerprint, derivation, local artifact kind, file name, MIME or type metadata, local profile or contract/adoption label change, restart or batch; ASSET restore proofs and ASSET_MUTATION_READY inspect the whole scope, never only the current candidate's attempts; only NOT_APPLIED_PROVEN clears it for a retry, which still needs a matching grant, readiness and a fresh restore proof; an APPLIED_PROVEN in that scope keeps a fresh upload with the same key blocked, whatever file name, MIME or type metadata, candidate, derivation, local artifact kind, profile or contract label asks, until a separately adopted reuse/rebind path exists
G3-30  the canary's CREATE safety strategy is never resend while the outcome is unknown, positive-only reconcile and durable ambiguity isolation (ADR-0014 §28); opening any real canary also requires a recorded explicit user and architect acceptance of the residual risk that a listing may be live while its provider identity is unrecovered
G3-31  the registration read state and its status card and detail panel are surfaces of the visual acceptance contract when implemented; a merged commit is a new accepted code SHA and never inherits an earlier visual acceptance
```

## Consequences

- The LIVE-authorization gap (`ROADMAP.md` §14.1) and the §14.2 preconditions now have a contract.
  Areas 1–3 implemented its provider-zero owners — the grant, the protected-write brake, the ASSET
  upload-attempt owner and the send-time stack (migration `0026`), the restore drill and the
  evidence-retention proof (`0029`) and the visual acceptance (`0030`) — and none of them is
  permission. The execution-mode owner still refuses LIVE.
- The M5 canary gains two mutation stages, each with its own grant, restore proof and send-time
  readiness (§3.1, §7, §10). Neither stage can be `READY` at this main: the execution policy is
  `M0_DRY_RUN_ONLY`, no ASSET sender is wired, canary eligibility has no owner, and CREATE/SEARCH
  adoption and the residual-risk acceptance (§6.1) are missing independently, so the canary is
  `BLOCKED` for several independent reasons at once.
- A later slice that implements a grant or the brake adds a migration under its own authorization.
- **The ASSET replay key is deliberately over-conservative** (§3.4): an `APPLIED_PROVEN` upload of
  the same content to the same account and wire endpoint blocks every fresh upload of it. That is
  a known liveness limitation — an asset already applied cannot be uploaded again after candidate
  drift or for another listing — accepted for Gate 3 safety. Lifting it needs a later, separately
  adopted reuse/rebind contract; it is **never** a reason to narrow the replay key.

## References

- Gate 3 kickoff: Issue #89 `5821078540`.
- Provider-evidence verdict: Issue #89 `5768247290` → `5768312853` → `5768347233`.
- Gate 2 acceptance: `5818393660`, cross-audit `5818648647`; Gate 1 acceptance: `5804516180`.
- `ROADMAP.md` §14; `docs/ARCHITECTURE.md` §7, §13; `docs/acceptance/M5.md` §6, §9; `docs/GLOSSARY.md` §4.
- ADR-0011, ADR-0014, ADR-0015, ADR-0016.
