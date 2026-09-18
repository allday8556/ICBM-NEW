# CLAUDE.md — ICBM-NEW implementation rules

Operating rules for ICBM-NEW. Read this before doing any work in this repository.

This file is the enforcement layer for `ROADMAP.md` and `docs/ARCHITECTURE.md`.
The roadmap says what to build; architecture defines the approved contracts and stack;
this file defines how implementation work is performed and recorded.

If documents conflict, stop and request architect resolution in GitHub instead of guessing.

---

## 1. Roles

```text
GitHub       = single durable work hub / Source of Truth
ChatGPT      = architecture, contracts, audit, review, acceptance decisions
Claude Code  = implementation, tests, commits, PR updates, implementation drafts
User         = product decisions and explicit protected/destructive approvals
```

Claude implements approved architecture. Claude does not redefine product architecture inside code.
Chat is not the durable work log. Anything that must survive a session belongs in this repository.

### 1.1 Repository exchange protocol

ChatGPT and Claude do not rely on direct AI-to-AI conversation. GitHub is the exchange channel.

| Location | Primary author | Purpose | Expected response |
| --- | --- | --- | --- |
| `docs/adr/NNNN-*.md` | Architect | Binding architecture/product decision | Implement against it |
| `docs/review/*-BY-CLAUDE.md` | Claude | Proposal or implementation question | Architect review / ADR / issue decision |
| `docs/acceptance/*.md` | Claude | Acceptance evidence | Architect accept / reject |
| GitHub Issues | Either | Open question, one topic per issue | Commented resolution |
| Pull requests | Claude | Implementation for review | Review comments / acceptance |

Rules:

- Proposal/review documents written by an AI must identify their author and status: `PROPOSAL`, `UNDER REVIEW`, `ACCEPTED`, or `SUPERSEDED`.
- Canonical documents such as `ROADMAP.md` and `docs/ARCHITECTURE.md` do not need author suffixes once accepted.
- A contract/architecture decision becomes binding only when reflected in an ADR or canonical document.
- If an implementation question changes architecture, Claude must stop implementation and open a GitHub issue for architect resolution.

---

## 2. Absolute no-legacy rule

**No legacy code, owner, DB schema, patch chain, test harness, marketplace ID, runtime behavior, or previously wired UI functionality is inherited from `allday8556/ICBM-PROJECT` or from the #86 UI rebuild.**

This is unconditional. It is not relaxed because copying appears faster or because a legacy component already works.

Do not copy or transplant:

- old Python/JS owners
- old DB/schema/migrations
- old Collector runtime/extension code
- old API handlers/contracts
- old patch/hotfix chains
- old tests as implementation truth
- old marketplace IDs/runtime state
- #86 functional implementation

Legacy may be inspected only when the architect explicitly authorizes a narrow reference case in GitHub. Default: **do not inspect and do not reuse**.

---

## 3. UI source rule

The approved standalone HTML prototype recorded in `docs/UI_SOURCE_OF_TRUTH.md` is the visual/product shell.

Use it for:

- visual hierarchy
- navigation
- responsive layout
- labels/status surfaces
- modal/drawer/empty-state interaction shapes

Do not treat its demo data, mock counts, or prototype JavaScript as runtime business truth.

---

## 4. Pinned runtime stack

The authoritative stack is `docs/ARCHITECTURE.md` §2. Current v1:

| Item | Decision |
| --- | --- |
| Language / runtime | Python 3.12 |
| Web / app framework | FastAPI + Uvicorn |
| Database | SQLite WAL — local single-user v1 |
| Migration tool | Alembic |
| ORM | SQLAlchemy 2.x |
| HTTP | httpx |
| Browser automation | Playwright Chromium when required |
| Job runner | Durable DB-backed queue/scheduler; one worker owner initially |
| Process model | Local single-user Windows application, loopback-only by default; one ICBM process per data directory (ADR-0006) |
| Secret storage | OS-native secure credential storage via keyring / Windows protection |
| Test framework | pytest + contract/integration/E2E gates |
| CI | GitHub Actions |

No major stack component may be changed casually. A change requires an architect-approved ADR before implementation.

---

## 5. Architectural rules

### 5.1 Direction of dependency

```text
Approved standalone HTML UI
    ↓
NEW application contract
    ↓
NEW service/domain owner
    ↓
NEW adapter/integration
    ↓
external system + read-back
    ↓
NEW canonical DB state
    ↓
UI state
```

- Business rules live in services/contracts, never duplicated in the UI.
- The UI displays server-owned state. It does not re-decide pricing, readiness, compliance, or stock.
- The canonical ICBM product ID is the spine. Collection, registration, orders, stock, inquiries, analytics, and fulfillment resolve back to it.
- No downstream screen owns a second copy of product truth.

### 5.2 Canonical product spine

```text
CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE
```

Fulfillment is inside OPERATE. AI, OCR, learning, pricing, compliance, jobs, audit, and analytics are supporting capabilities, not parallel top-level systems.

### 5.3 Adapter rule

Core product logic stays platform-neutral. A supplier or marketplace implements the approved adapter contract.

Site-specific extraction belongs inside that supplier adapter/profile. Marketplace payload specifics belong inside that marketplace adapter.

**A new adapter may not change canonical Product / Pricing / Operation contracts merely to accommodate one site.** Contract changes require architecture review first.

### 5.4 Facts vs enrichment

Raw source facts and evidence stay separate from AI enrichment.

AI may validate or propose enrichment, but it never fabricates source facts. Ambiguous evidence becomes `REVIEW_REQUIRED`, never a confident guess.

---

## 6. Immutable domain rules

### 6.1 Pricing

```text
if minimum_sale_price exists:
    final_sale_price = minimum_sale_price
    price_basis = MINIMUM_SALE_PRICE
else:
    final_sale_price = target_margin_price
    price_basis = TARGET_MARGIN
```

Never reintroduce `max(target_margin_price, minimum_sale_price)`.

Preserve supplier quantity tiers as original `(quantity, total_price)` facts. Do not infer or flatten source totals.

### 6.2 Options / SKU

Preserve atomic source SKU identity. Same weight does not justify flattening different count, grade, or option identity.

### 6.3 Stock

```text
BUY/CART active                     → ON_SALE
SOLD OUT + no active purchase path → SOLD_OUT
mixed/insufficient evidence        → REVIEW_REQUIRED
```

### 6.4 ProductFacts revisions

Collection writes append-oriented/revisioned `ProductFactsRevision` records. Never silently mutate historical source facts in place.

`Product` resolves each member source product through that member's current source revision; this pointer is not an acceptance or confirmation status.

### 6.5 Marketplace CREATE

Real CREATE uses `RegistrationAttempt` and deterministic reconciliation.

If a CREATE result is unknown:

- do not blindly retry CREATE
- reconcile using the deterministic seller product code / marketplace read-back
- only retry after proving no marketplace product was created

---

## 7. Execution safety

### 7.1 Execution mode

Global external-write mode is:

```text
DRY_RUN | LIVE
```

Default during development is `DRY_RUN`.

Marketplace sandbox/test accounts, where available, are environment/account configuration — not a third global execution mode.

`LIVE` requires the intended verification scope to be explicit in GitHub and user approval when the action is protected/destructive.

### 7.2 Requires user approval before execution

- any destructive marketplace action: delete/deactivate or equivalent
- any real supplier order (발주)
- bulk live writes or bulk destructive operations
- destructive migrations, table/row drops, irreversible rewrites
- compliance gate override
- credential changes that replace/delete active credentials
- deleting branches or force-pushing

For first-time real marketplace CREATE/UPDATE verification, use the explicit LIVE acceptance scope defined in GitHub and obtain user approval before the run.

Approval is per action/scope and does not automatically generalize to future actions.

### 7.3 Never do

- commit credentials, tokens, cookies, or session state
- commit real customer/order personal data in tests or fixtures
- spin repeated failed supplier logins
- retry an unknown-result marketplace CREATE without reconciliation
- bypass the compliance gate
- turn `UNKNOWN` or `REVIEW_REQUIRED` into PASS through a silent fallback

---

## 8. Git conventions

### 8.1 Branches

```text
main                         always releasable / green
feat/<area>-<short-desc>
fix/<area>-<short-desc>
docs/<short-desc>
chore/<short-desc>
```

`<area>` is one of:

```text
connect | collect | products | register | operate | ui | infra
```

Fulfillment work uses the `operate` area because fulfillment belongs inside OPERATE.

### 8.2 Commits

Use conventional-commit style and one logical change per commit.

Examples:

```text
feat(collect): add KM통상 detail fact extraction
fix(register): reconcile unknown create before retry
docs(adr): record database choice
```

Do not mix broad refactoring with unrelated behavior changes in the same commit.

### 8.3 Pull requests

Every PR states:

1. what changed
2. which contract it implements or modifies
3. whether schema changed
4. whether external writes are involved
5. how it was verified — commands and observed evidence
6. what it explicitly does not do

A PR that changes an approved contract links the ADR authorizing it. No ADR, no contract change.

No merge based only on unit/mock PASS when the roadmap requires real E2E/read-back evidence.

---

## 9. Definition of Done

A feature is not done because a function exists or a test is green.

1. UI action reaches the intended service.
2. Service uses the canonical contract.
3. Integration performs the real read/write where permitted.
4. Result is read back from the external system when applicable.
5. Canonical DB reflects the read-back.
6. UI reflects canonical state after reload.
7. The same flow succeeds again in a fresh session.
8. No unrelated flow regresses.

Acceptance evidence belongs in `docs/acceptance/`, not chat. It records correlation IDs, external IDs, timestamps, read-back evidence, and the fresh-session condition.

---

## 10. Working style expected of Claude

- Read the canonical documents before starting; do not ask the user to re-explain rules already written here.
- Implement the current milestone only. Do not horizontally expand before the first vertical closes.
- When the roadmap or architecture is ambiguous, do not pick a convenient interpretation and proceed.
- Raise blockers early and plainly.
- Report what was actually verified separately from what was assumed.
- Never describe an untested path as working.
- Prefer deleting a wrong abstraction over wrapping it.

---

## 11. Current milestone

```text
M0 — fresh UI shell + Phase 0 foundation   ACCEPTED 2026-09-13 (Issue #1, PR #2, docs/acceptance/M0.md)
M1 — KM통상 CONNECT only                   ACCEPTED 2026-09-13 (Issue #7, PR #9, ADR-0007, docs/acceptance/M1.md)
M2 — SmartStore CONNECT                    ACCEPTED 2026-09-15 (Issue #46, PR #51, docs/acceptance/M2.md)
M3 — KM통상 one-product COLLECT             ACCEPTED 2026-09-18 (Issue #52, ADR-0010, docs/acceptance/M3.md; bounded by its §2)
M4 — canonical Product DB                  CURRENT (Issue #80; PR-A contract ADR-0013 → PR-B → PR-C → PR-D → PR-E → PR-F; each PR separately authorized)
```

The current approved visual source is the prototype recorded in `docs/UI_SOURCE_OF_TRUTH.md`. Do not hard-code a prototype file name in this file or treat an older prototype as current.

The accepted milestone sequence is `ROADMAP.md` §12. Implement one milestone at a time, and do not begin horizontal supplier/marketplace expansion before the first vertical (§12 below) closes.

---

## 12. First vertical

Do not horizontally expand before this closes:

```text
KM통상 CONNECT
→ 1 real product COLLECT
→ ProductFactsRevision
→ canonical Product DB
→ SmartStore readiness/compliance
→ idempotent SmartStore REGISTER
→ marketplace read-back
→ OPERATE sync
→ fulfillment record/tracking path when applicable
```

Required: two consecutive complete passes in fresh sessions.

---

## 13. Canonical file index and read order

Read in this order before coding (the order Issue #1 mandated for M0):

1. `CLAUDE.md` — implementation/process rules (this file)
2. `ROADMAP.md` — product/phase plan and milestone sequence
3. `docs/ARCHITECTURE.md` — canonical contracts and stack
4. relevant `docs/adr/` decisions
5. `docs/UI_SOURCE_OF_TRUTH.md` — which prototype is the approved visual shell, with its fingerprint
6. the current milestone's GitHub issue and its acceptance criteria
7. `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md` when context on reviewed proposals is needed

Additional locations:

| Path | Purpose |
| --- | --- |
| `ROADMAP-ADDITIONS-BY-CLAUDE.md` | Claude review proposal; not binding by itself |
| `docs/review/` | Claude drafts/proposals awaiting architecture review |
| `docs/acceptance/` | Durable acceptance evidence |
| `docs/GLOSSARY.md` | Canonical field and concept names (accepted in ARCHITECT_REVIEW D3; not created yet) |
| `ui/prototypes/` | Standalone UI prototypes; only `docs/UI_SOURCE_OF_TRUTH.md` names the current one |

If canonical documents conflict, stop implementation and request architect resolution in GitHub rather than guessing.
