# CLAUDE.md — ICBM-NEW implementation rules

This file is mandatory context for every Claude Code implementation session.

## 1. Roles

```text
GitHub       = single durable work hub / Source of Truth
ChatGPT      = architecture, contracts, audit, review, acceptance
Claude Code  = implementation, tests, commits, PR updates
User         = product decisions and explicit protected/destructive approvals
```

Claude implements approved architecture. Claude does not redefine product architecture inside code.

## 2. Absolute no-legacy rule

`allday8556/ICBM-PROJECT` is legacy/archive only.

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

## 3. UI source rule

The approved standalone HTML prototype recorded in `docs/UI_SOURCE_OF_TRUTH.md` is the visual/product shell.

Use it for:

- visual hierarchy
- navigation
- responsive layout
- labels/status surfaces
- modal/drawer/empty-state interaction shapes

Do not treat its demo data or prototype JavaScript as runtime business truth.

## 4. Contract-first implementation

Every feature must follow:

```text
UI → application contract → service/domain owner → adapter → external system → read-back → canonical DB → UI
```

Forbidden:

- UI calling supplier/marketplace integrations directly
- duplicate pricing/category/stock truth in UI
- platform payload fields inside ProductFacts
- supplier selectors leaking into global product logic
- AI inventing facts
- silent fallbacks that convert UNKNOWN/REVIEW_REQUIRED into PASS

## 5. Canonical product spine

```text
CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE
```

Fulfillment is inside OPERATE. AI/OCR/learning/jobs/compliance/pricing are supporting owners, not parallel applications.

## 6. Pinned runtime stack

Use `docs/ARCHITECTURE.md` as canonical. Current v1 stack:

```text
Python 3.12
FastAPI + Uvicorn
SQLAlchemy 2.x + Alembic
SQLite WAL
httpx
Playwright Chromium when browser automation is required
DB-backed durable Job queue/scheduler
OS-native secret storage via keyring/Windows protection
pytest
GitHub Actions
```

Changing any major stack component requires an ADR approved by the architect before implementation.

## 7. Core immutable rules

### Pricing

```text
if minimum_sale_price exists:
    final_sale_price = minimum_sale_price
    price_basis = MINIMUM_SALE_PRICE
else:
    final_sale_price = target_margin_price
```

Never reintroduce `max(target_margin_price, minimum_sale_price)`.

Preserve source quantity tiers as `(quantity, total_price)` facts. Do not infer/flatten source prices.

### Options/SKU

Preserve atomic source SKU identity. Same weight does not justify flattening different count/grade/options.

### Stock

```text
BUY/CART active                         → ON_SALE
SOLD OUT + no active purchase path     → SOLD_OUT
mixed/insufficient evidence            → REVIEW_REQUIRED
```

### AI

AI may propose enrichment/validation. It cannot manufacture source facts or directly bypass review/compliance gates.

## 8. ProductFacts revision rule

Collection writes immutable/revisioned `ProductFactsRevision` records. Do not silently mutate old source facts in place.

`Product` points to the current accepted revision.

## 9. Marketplace CREATE rule

Real CREATE must use `RegistrationAttempt` and deterministic reconciliation.

If a request outcome is unknown:

- do not blindly retry CREATE
- reconcile by deterministic seller product code/read-back
- only retry after proving no marketplace product was created

## 10. Execution modes

Default external-write mode: **DRY_RUN**.

`LIVE` requires the intended verification scope to be explicit in GitHub. Marketplace delete/deactivate, bulk destructive changes, compliance resolution, credential mutation, and destructive DB migrations require user approval and audit evidence.

## 11. Branch / PR discipline

- Never implement directly from chat-only instructions if GitHub canonical docs differ.
- One focused feature/contract per branch where practical.
- PR description states: contract touched, schema touched, external writes touched, tests/evidence.
- No merge based only on unit/mock PASS when the roadmap requires E2E/read-back.
- Do not claim DONE without required acceptance evidence.

## 12. Decision records

Architecture changes go to `docs/adr/`.

Acceptance evidence goes to `docs/acceptance/`.

Canonical terminology belongs in `docs/GLOSSARY.md` once introduced.

## 13. First vertical

Do not horizontally expand before this closes:

```text
K홀세일 CONNECT
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

## 14. Current architecture authority

Read in this order before coding:

1. `ROADMAP.md`
2. `docs/ARCHITECTURE.md`
3. `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md`
4. `docs/UI_SOURCE_OF_TRUTH.md`
5. relevant ADRs

If documents conflict, stop implementation and request architect resolution in GitHub instead of guessing.
