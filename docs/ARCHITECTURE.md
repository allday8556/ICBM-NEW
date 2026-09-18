# ICBM-NEW Architecture

Status: **CANONICAL — v1 foundation**

## 1. Product spine

ICBM-NEW is one connected commerce workflow:

```text
CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE
```

Supporting capabilities — AI, OCR, learning, pricing, compliance, jobs, audit, fulfillment, analytics — must attach to this spine. They are not separate top-level systems.

The AI runtime contract is `docs/adr/0012-ai-runtime-provider-contract.md` (Issue #8). It covers provider-neutral profiles, an optional local sidecar that ICBM never depends on, AI as a capability that never fails core readiness, and AI failure never blocking COLLECT, `ProductFactsRevision` or the canonical DB.

## 2. Runtime stack

```text
Python              3.12
Backend              FastAPI + Uvicorn
ORM / migrations     SQLAlchemy 2.x + Alembic
Database             SQLite WAL (local single-user v1)
Frontend             canonical standalone HTML/CSS/vanilla JS → ES modules
HTTP                 httpx
Browser automation   Playwright Chromium when required
Jobs                 durable DB-backed queue/scheduler; one worker owner initially
Secrets              OS-native secure credential store via keyring/Windows protection
Deployment            local Windows process; loopback-only by default
Tests                 pytest + contract/integration/E2E gates
CI                    GitHub Actions
```

No framework, DB engine, browser driver, secret store or queue may be swapped casually. A change requires an ADR.

ICBM-NEW keeps its local state under one canonical data root, `%USERPROFILE%\ICBM-NEW\data` (Issue #52 comment 5688854287):

- **Resolution.** Only `app/config.py` resolves the root, from `USERPROFILE` alone. There is no host detection and no AppData fallback, and resolution fails closed without an absolute `USERPROFILE`. `ICBM_DATA_DIR` names the data root explicitly, for tests and dedicated acceptance directories only.
- **Runtime.** `runtime/` belongs to the application and holds:
  - the one relational database, `runtime/icbm.db`;
  - the encrypted sessions;
  - the owner lock.
- **Reserved names.** `products/`, `api/`, `suppliers/`, `logs/` and `backups/` are reserved lowercase names for domain data.
- **Superseded path literals.** These runtime paths supersede the path literals of ADR-0006 (`.icbm-owner.lock`) and ADR-0007 (`sessions/`). The decisions of both ADRs are unchanged.
- **Scope.** This is the local desktop/CLI root only; a server deployment will have its own storage owner.

Exactly one ICBM process owns a data root at a time, through an OS lock on `<data root>/runtime/owner.lock` (`docs/adr/0006-single-data-directory-process-ownership.md`).

The supplier and marketplace logins live in the OS secret-store service `ICBM-NEW` (the same name as the data root), and CONNECT alone owns them together with the connection state and the sessions. COLLECT, and every harness that needs a session, borrows that owner through the canonical resolver and never defines a second one (Issue #52 comments 5687814715 and 5688150031).

## 3. Layering rule

Every functional path follows:

```text
UI action
→ application contract
→ domain/service owner
→ integration adapter
→ external read/write
→ read-back/reconcile
→ canonical state
→ UI refresh
```

Forbidden shortcuts:

- UI → marketplace/supplier directly
- UI-owned pricing/compliance/business truth
- supplier-specific rules inside global product services
- marketplace-specific payload fields leaking into ProductFacts
- AI writing ungrounded source facts

## 4. Core boundaries

### CONNECT
Owns supplier and marketplace connectivity only. Supplier CONNECT is supplier-generic: suppliers are site-knowledge definitions behind a policy-enforcing common transport, and READY is proven only by a protected read with its unauthenticated control (`docs/adr/0007-supplier-generic-connect.md`).

- supplier credentials/session/auth state
- marketplace accounts/API credentials/capabilities
- connection verification/read-back
- crawl/session policies

### COLLECT
Owns source evidence and source facts. Collection is supplier-generic (`docs/adr/0010-supplier-generic-collect-and-product-facts-revision.md`):
- every source read goes through a common collection gateway, separate from the CONNECT proof port;
- suppliers contribute pure collection definitions and parsers;
- COLLECT persists ProductFactsRevision source truth from M3, while the canonical Product arrives in M4.

- discover/open/extract
- raw evidence
- ProductFactsRevision creation
- stock evidence
- source image discovery/fetch/checksum
- ambiguity → REVIEW_REQUIRED

### PRODUCT DB
Owns canonical product identity and normalized accepted state.

- Product identity
- current accepted ProductFactsRevision
- atomic source SKU/option mapping
- enrichment proposals/accepted values
- pricing snapshots
- registration readiness inputs

### REGISTER
Owns platform conversion and listing creation.

- category mapping
- compliance gate
- image transformation/upload
- readiness
- RegistrationAttempt/idempotency
- CREATE/reconcile/read-back
- MarketplaceRegistration

### OPERATE
Owns everything after publication.

- listing state/read-back
- stock/sold-out sync
- order/claim/inquiry sync
- source drift handling
- fulfillment record: supplier order → tracking → marketplace shipment update
- settlement and actual-margin inputs

## 5. Canonical truth model

### ProductFactsRevision
Append-oriented source truth. Every successful recollection creates a new immutable revision, even when the source is unchanged; facts are never mutated in place. ADR-0010 defines:
- provenance: revision ID, extractor revision and fingerprint, collection run/correlation, `facts_status`;
- the product-scoped evidence model;
- fingerprint rules.

Minimum identity:

```text
supplier_key
source_product_id
source_url
captured_at
currency
source prices
supplier shipping
minimum_sale_price
quantity tiers
atomic SKU/options
images
brand/manufacturer/origin
notice facts
stock evidence
source fingerprint + field fingerprints
```

### Product
ICBM canonical identity. References the current accepted facts revision and downstream state.

### MarketplaceRegistration
Always includes:

```text
icbm_product_id
marketplace_key
account_id
marketplace_product_id
seller_product_code
published_state
last_readback_at
```

No downstream module creates a second product truth.

## 6. Pricing

Only Pricing owns selling-price calculation.

Canonical rule:

```text
if minimum_sale_price exists:
    final_sale_price = minimum_sale_price
    price_basis = MINIMUM_SALE_PRICE
else:
    final_sale_price = target_margin_price
    price_basis = TARGET_MARGIN
```

Never restore `max(target_margin_price, minimum_sale_price)`.

Preserve quantity-tier original totals; never manufacture source values by dividing or multiplying minimum prices.

Pricing snapshots must also support:

- source currency + FX snapshot when needed
- platform commission
- settlement/payment fees
- VAT policy
- advertising policy
- shipping policy
- coupon/discount burden
- return/exchange costs
- estimated vs actual settled margin

## 7. Registration safety

### ComplianceGate

```text
PASS | REVIEW_REQUIRED | BLOCKED
```

BLOCKED cannot be bypassed by automation. Resolution requires changed/verified evidence and an audit trail.

### RegistrationAttempt

CREATE is stateful and idempotent:

```text
PREPARED → SENT → CONFIRMED
                 ↘ UNKNOWN
                 ↘ FAILED
```

`UNKNOWN` is reconciled by marketplace read/search using a deterministic seller-side product code before any retry. Never blindly resend CREATE.

## 8. Jobs and sync

One durable Job contract is shared by collect/register/stock/order/inquiry/tracking operations.

Core fields:

```text
job_id
job_type
target_ref
state
attempt_count
next_attempt_at
correlation_id
last_error_class
last_error_code
created_at
started_at
finished_at
```

Core error classes — the aligned taxonomy of `docs/adr/0008-error-taxonomy-alignment.md`, which also records how it relates to `docs/architecture/CANONICAL-V3.1.md` §11.3:

```text
TRANSIENT
RATE_LIMITED       v3.1 RATE_LIMIT (same class, persisted spelling)
AUTH
VALIDATION
POLICY_BLOCKED     v3.1 POLICY (same class, persisted spelling)
NOT_FOUND          an ICBM-local addressed resource only; never a provider/wire classification
CONFLICT           added to the runtime enum by the ADR-0008 implementation stage
DUPLICATE          added to the runtime enum by the ADR-0008 implementation stage
REVIEW_REQUIRED    added to the runtime enum by the ADR-0008 implementation stage
FATAL              added to the runtime enum by the ADR-0008 implementation stage
UNKNOWN
```

A class is a cause. It is never a workflow state, retry permission or replay permission: `error_class=REVIEW_REQUIRED` is not `workflow_state=REVIEW_REQUIRED`. Only `TRANSIENT` and `RATE_LIMITED` are retried automatically (ADR-0004, ADR-0005). The taxonomy only grows: no member is removed or renamed while persisted rows can hold it.

UNKNOWN destructive/write outcomes are not automatically retried.

Job states, transitions and the per-attempt history are fixed by `docs/adr/0005-durable-job-state-and-attempt-history.md`.

## 9. Review queue

One ReviewItem model handles all human-required work:

```text
COLLECT_EVIDENCE
STOCK
SOURCE_CHANGE
COMPLIANCE
REGISTRATION_ERROR
FULFILLMENT
```

UI surfaces these items in the relevant existing screen plus dashboard counts. No separate top-level review application is required for v1.

## 10. Images

Published images must not depend on supplier hotlinks.

```text
source URL
→ fetch
→ checksum
→ validate
→ dedupe
→ transform if platform requires
→ upload
→ save marketplace image identity/read-back
```

Detail-page embedded images used in published content are rehosted as part of the same pipeline.

COLLECT covers only the first three steps for *source* assets (ADR-0010 §9):
- It keeps the original bytes unmodified, with role/order, dimensions, MIME type, size and SHA-256.
- It never transforms them.

Derived marketplace variants belong to the M4 image pipeline and to each marketplace adapter/readiness contract.

## 11. Source drift

Recollection compares facts fingerprints.

```text
price changed    → new pricing proposal
option removed   → REVIEW_REQUIRED
option added     → proposed addition
image changed    → image pipeline
notice/detail    → REVIEW_REQUIRED when materially changed
stock changed    → stock workflow
```

Never silently remove a live marketplace option because a supplier page changed.

## 12. Fulfillment inside OPERATE

First vertical: **manual supplier order, canonically recorded**.

```text
Marketplace order
→ Product + atomic SKU mapping
→ SupplierOrder record
→ manual supplier purchase/reference
→ tracking captured
→ marketplace shipment/tracking write
→ delivery read-back
→ settlement
```

Supplier ordering automation is a later adapter capability; the canonical record exists from v1.

## 13. Execution safety

Global external-write mode:

```text
DRY_RUN | LIVE
```

Default is DRY_RUN. Official marketplace sandbox/test accounts are adapter/environment configuration, not a third global mode.

Protected/destructive actions are audit logged.

## 14. Acceptance

A vertical is accepted only when the same real flow succeeds twice consecutively in fresh sessions:

```text
CONNECT
→ COLLECT
→ ProductFactsRevision
→ canonical Product
→ readiness/compliance
→ REGISTER
→ marketplace read-back
→ OPERATE sync
→ fulfillment record/tracking path when an order exists
```

Acceptance evidence belongs under `docs/acceptance/`, not in chat.

## 15. First vertical

```text
Supplier      KM통상 (supplier_key kmretail)
Marketplace   Naver SmartStore
Currency      KRW
Accounts      schema supports many; first run may use one
Fulfillment   manual-with-record
Writes        DRY_RUN until explicit LIVE verification
```

Expand only after the first vertical closes end-to-end.
