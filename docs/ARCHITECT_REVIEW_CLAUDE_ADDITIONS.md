# Architect Review — ROADMAP-ADDITIONS-BY-CLAUDE

Status: **APPROVED WITH MODIFICATIONS**
Architect: ChatGPT
Implementation role: Claude Code
Repository: `allday8556/ICBM-NEW`

This review decides which additions become part of the ICBM-NEW design. The companion file `ROADMAP-ADDITIONS-BY-CLAUDE.MD` remains a proposal/history document; this review is the architecture decision.

## 1. Non-negotiable top-level shape

The user-defined product flow remains:

```text
CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE
```

Do not add new peer top-level systems. Supporting concerns live inside or across these stages.

`FULFILL` is required, but it is **not** a sixth top-level stage. It is an OPERATE subflow:

```text
OPERATE
├─ sales/read-back sync
├─ stock/sold-out
├─ orders/claims/inquiries
└─ fulfillment: supplier order → tracking → marketplace shipment update
```

The reason is architectural: the user explicitly wants one simple business spine. Fulfillment is what happens after an order enters OPERATE, not a separate application.

## 2. P0 decisions

| ID | Decision | Architect ruling |
|---|---|---|
| A1 Fulfillment | **ACCEPT / MODIFY** | Keep under OPERATE. First vertical uses **manual supplier ordering with canonical recording**. Supplier-cart/API automation comes later. |
| A2 Runtime stack | **ACCEPT** | Pin stack in `docs/ARCHITECTURE.md` before implementation. |
| A3 Durable jobs | **ACCEPT** | A DB-backed durable job layer is foundation work. Do not build separate queues per feature. |
| A4 CREATE idempotency/reconcile | **ACCEPT** | Required before first real marketplace CREATE. UNKNOWN result must reconcile before retry. |
| A5 FX | **ACCEPT / SPLIT** | Currency fields exist from day one. FX provider/staleness policy is decided before first non-KRW supplier, not before K홀세일 M1. |
| A6 Image pipeline | **ACCEPT** | Source URL alone is insufficient. Fetch/checksum/validate/rehost/upload/read-back are one image pipeline. Detail embedded images are rehosted for published content. |
| A7 `CLAUDE.md` | **ACCEPT** | Root implementation rules file is mandatory. |

## 3. P1 decisions

| ID | Decision | Architect ruling |
|---|---|---|
| B1 Pricing/settlement inputs | **ACCEPT** | Estimated margin and actual settled margin must be different states. |
| B2 Source drift | **ACCEPT** | Recollection writes a new facts revision and produces a typed diff. No silent option/image/detail replacement. |
| B3 Category mapping store | **ACCEPT** | AI proposes; persisted mapping decides. Tree version invalidates stale mappings. |
| B4 Compliance gate | **ACCEPT / HARDEN** | `PASS / REVIEW_REQUIRED / BLOCKED`. Automation can never bypass BLOCKED. User resolution requires evidence/change of facts, not a blind override. |
| B5 Multi-account schema | **ACCEPT** | Include `account_id` from the first schema even when only one account is connected. |
| B6 ProductFacts vs Product | **ACCEPT** | `ProductFacts` is immutable/revisioned source truth; `Product` references the current accepted facts revision. |
| B7 Rate/quota limiter | **ACCEPT** | Central limiter per marketplace/account; adapters do not implement isolated retry storms. |
| B8 Error taxonomy | **ACCEPT** | Core error class is canonical; platform-specific codes are adapter details. |
| B9 REVIEW_REQUIRED queue | **ACCEPT / MODIFY UI** | One canonical ReviewItem model. Surface it inside the relevant existing screens + dashboard count; no new top-level menu initially. |

## 4. P2 decisions

- **C1 Safe execution — ACCEPT / MODIFY:** global execution mode is `DRY_RUN | LIVE`. A marketplace's official sandbox/test capability is an adapter/environment property, not a third universal mode.
- **C2 Crawl policy — ACCEPT:** per supplier limits, challenge/ban detection, backoff, login loop guard, profile version/break alert.
- **C3 Evidence retention — ACCEPT:** define retention/storage ceiling before broad collection rollout.
- **C4 Audit log — ACCEPT:** append-only audit for protected/destructive actions and approvals.
- **C5 Backup/restore — ACCEPT:** a restore drill is required, not only a backup script.
- **C6 Correlation ID — ACCEPT:** issue at flow entry and propagate through jobs, facts, registration, marketplace calls, fulfillment and operation sync.

## 5. P3 decisions

D1 CI, D2 ADR, D3 glossary, D4 acceptance evidence, D5 performance targets and D6 document hygiene are all **ACCEPTED**.

They are not separate projects. They are the minimum project discipline needed so a new Claude session cannot silently redefine the architecture.

## 6. Runtime stack — architect decision

ICBM-NEW v1 is a **single-user local Windows application** with a local web UI.

```text
Runtime            Python 3.12
Backend            FastAPI + Uvicorn
Data layer          SQLAlchemy 2.x + Alembic
Database            SQLite WAL for v1 local single-user runtime
Frontend            approved HTML/CSS/vanilla JS shell, split into ES modules during implementation
HTTP client          httpx
Supplier browser     Playwright Chromium when browser interaction is required
Job execution        durable DB-backed Job table + one scheduler/worker owner initially
Secrets              OS-native credential store via Python keyring / Windows credential protection
Deployment           local Windows process, loopback-only service by default
Testing              pytest + contract/integration tests
CI                   GitHub Actions: lint/type/test/migration checks
```

### SQLite constraint

SQLite is accepted because this is a local single-user v1, not a multi-tenant server. The design must use:

- WAL mode
- short transactions
- a single durable DB-write owner for background jobs
- no feature-specific embedded databases
- repository/service boundaries that do not leak SQLite-specific behavior into business contracts

If the product later becomes multi-user/server-hosted, a DB-engine ADR is required before migration. Do not pre-build a distributed stack now.

## 7. First-vertical operating choices

- Supplier: **K홀세일**
- Marketplace: **Naver SmartStore**
- Fulfillment: **manual supplier order + canonical SupplierOrder record**, then tracking read/write where the platform permits
- Marketplace writes: DRY_RUN by default; LIVE only for the explicitly approved first-vertical verification
- Accounts: schema supports many; first test may use one account
- Currency: KRW first vertical; original currency always stored
- Images: ICBM-owned copy/checksum; published images must not depend on supplier hotlinks
- Compliance: first vertical must pass ComplianceGate before CREATE

## 8. Canonical entities that must exist before downstream features grow

```text
SupplierConnection
MarketplaceAccount
ProductFactsRevision
Product
ProductOption / SourceSKU
ProductImage
PricingSnapshot
CategoryMapping
ReviewItem
RegistrationAttempt
MarketplaceRegistration
Order / OrderItem
SupplierOrder
Settlement
Job
AuditEvent
```

This list defines contracts, not necessarily one-table-per-name implementation. Claude must propose schema/migrations for review before coding deep feature behavior.

## 9. Implementation order after UI shell is fixed

```text
M0  Foundation + stack + migrations + jobs + errors + audit/correlation
M1  K홀세일 CONNECT
M2  SmartStore CONNECT
M3  K홀세일 one-product COLLECT → ProductFactsRevision
M4  canonical Product DB + image pipeline + pricing/readiness foundations
M5  SmartStore REGISTER with idempotency + reconcile + read-back
M6  OPERATE read-back + stock + order ingest
M6.5 fulfillment record + tracking flow
FIRST VERTICAL: two consecutive complete passes in fresh sessions
```

Only then expand suppliers/platforms.

## 10. Explicitly rejected interpretations

- Do not turn Fulfillment, AI, OCR, Learning, Compliance, Pricing or Jobs into independent top-level products.
- Do not import legacy owners from `ICBM-PROJECT` to satisfy these contracts.
- Do not copy #86 functional implementation. The approved standalone HTML prototype is the UI source.
- Do not implement every future feature before the first vertical. Add schema/contract seams now; activate capability when its phase arrives.
- Do not equate mock/unit success with vertical acceptance.

## 11. Status

The Claude additions are now **architecturally reviewed**. Implementation may use the accepted rulings above, together with `ROADMAP.md`, `docs/ARCHITECTURE.md`, and root `CLAUDE.md`.
