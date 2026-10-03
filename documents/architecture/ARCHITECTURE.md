# ICBM-NEW Architecture

Status: **CANONICAL — v1 foundation**

## 1. Product spine

ICBM-NEW is one connected commerce workflow:

```text
CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE
```

Supporting capabilities — AI, OCR, learning, pricing, compliance, jobs, audit, fulfillment, analytics — must attach to this spine. They are not separate top-level systems.

The AI runtime contract is `documents/decisions/adr/0012-ai-runtime-provider-contract.md` (Issue #8). It covers provider-neutral profiles, an optional local sidecar that ICBM never depends on, AI as a capability that never fails core readiness, and AI failure never blocking COLLECT, `ProductFactsRevision` or the canonical DB.

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

Exactly one ICBM process owns a data root at a time, through an OS lock on `<data root>/runtime/owner.lock` (`documents/decisions/adr/0006-single-data-directory-process-ownership.md`).

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
Owns supplier and marketplace connectivity only. Supplier CONNECT is supplier-generic: suppliers are site-knowledge definitions behind a policy-enforcing common transport, and READY is proven only by a protected read with its unauthenticated control (`documents/decisions/adr/0007-supplier-generic-connect.md`).

- supplier credentials/session/auth state
- marketplace accounts/API credentials/capabilities
- connection verification/read-back
- crawl/session policies

### COLLECT
Owns source evidence and source facts. Collection is supplier-generic (`documents/decisions/adr/0010-supplier-generic-collect-and-product-facts-revision.md`):
- a document arrives by one of two acquisition transports (`documents/decisions/adr/0019-extension-primary-collection-transport.md`):
  - **Implementation status.** `EXTENSION`-primary is the accepted ADR-0019 **target contract**. **E2 is implemented: one click, recorded** (ADR-0019 §10; `documents/acceptance/adaptive/EXTENSION-E2.md`, `ACCEPTED`), on top of E1 (rulings `5906290729` and `5906712259`, owner amendment `5907095955`; `documents/acceptance/adaptive/EXTENSION-E1.md`). An accepted capture opens a canonical run, and **the supplier's canonical extractor is its revision writer**: the run ends `RECORDED` with its revision, `NO_REVISION` when the identity is unresolved, or `FAILED`. Both transports write a revision through the one pipeline after capture. **E3 is implemented: the list queue** (ADR-0019 §8.1; `documents/acceptance/adaptive/EXTENSION-E3.md`, `ACCEPTED`): the side panel finds a loaded list page's visible product URLs, and every queue read is issued by the server, durably, before it happens. E3's real acceptance passed under the user's grant on exact main `091133b6`. Every later slice (ADR-0019 §10) is a separate slice, one at a time.
  - **`EXTENSION`** is primary in the target contract. The operator's own Chrome, through a first-party MV3 extension, captures the current product page on a click, and later a list page's products through a bounded queue. The product scope is cut first under an independent `BrowserCapturePolicy`, and the page is sent to a paired loopback ingest.
  - **`DIRECT_URL`** is the fallback, submitted from Collection Management.
- both transports converge into one `DocumentView`, one collection-run lifecycle and one server-owned pipeline.
  - The extension is transport only.
  - The existing KM extractor stays the only revision writer, and Adaptive stays shadow.
  - Transport is provenance, never a drift input.
- Collection Management is kept. It shows the run, status and review history of both transports and owns the direct-URL submission.
- the direct-URL read goes through a common collection gateway, separate from the CONNECT proof port;
- suppliers contribute pure collection definitions and parsers;
- COLLECT persists ProductFactsRevision source truth from M3, while the canonical Product arrives in M4.

The extension transport as implemented by E1 and E2:
- **Owners.** The client is `ui/extension/`: MV3, plain ES modules, no build step, host permissions for the reviewed supplier host and the loopback only, Chrome 114 or later (the side panel is its only surface), and the pairing is the only thing it stores. The server owner is `app/stages/collect/extension/`. The extension is transport and capture UX; it writes no database row, revision, pointer or asset. The server owner writes none either: the revision, its source assets and the shadow record are the collection owner's, through the stores a direct run uses.
- **Surface.** Exactly these routes, on their own router: the capture routes `GET /api/v1/collect/extension/capture-policies/{supplier_key}` and `POST /api/v1/collect/extension/captures`, and the list-queue routes `GET /api/v1/collect/extension/queue-policies/{supplier_key}`, `POST /api/v1/collect/extension/queues`, `GET /api/v1/collect/extension/queues/{queue_id}`, `POST …/queues/{queue_id}/next`, `POST …/queues/{queue_id}/cancel` and `POST …/queues/{queue_id}/release`. The application gains no CORS: only these paths answer a preflight, and only for the paired extension's origin. The DIRECT_URL submit is unchanged.
- **List queue (ADR-0019 §8.1, E3).**
  - **Owner.** `app/stages/collect/extension/queue.py`, with migration 0035 (`extension_queues`, `extension_queue_items`).
  - **Discovery.** It judges each discovered product URL with `check_target` and the §6.1 secret rules, and deduplicates by product. A refused link is counted, never stored or logged.
  - **Bounds.** It opens a queue only inside the supplier's declared `QueueLimits` and the operator's own size and interval. A missing bound refuses; nothing is defaulted.
  - **Issued reads.** It answers each `next` with a wait, one issued read or done. An issue is written before the answer, with only its single-use ticket's SHA-256.
  - **Pacing.** The same-product interval counts server reads, issued queue reads and extension captures.
  - **Ticketed capture.** A ticketed capture claims its item for exactly that URL, once and in time, in the unchanged ingest's own write unit.
  - **Going on.** The queue goes on past an expired read, a refused queue capture or a `FAILED` run; the item keeps how it ended and is never reissued. A read the extension could not capture is given back at once (`…/queues/{queue_id}/release`), spent the same way, so the queue does not wait for the issue lifetime.
  - **Serialization.** A single click waits while a queue read of the supplier is out.
  - **Client.** `ui/extension/lib/discover.js` reads the operator's loaded list page and returns only links that fully match the supplier's reviewed product path form, as scheme, host and path.
    - The service worker asks for the next read and waits as ICBM says.
    - It navigates the operator's own tab to the one URL ICBM issued, and captures it with the unchanged cut.
    - It sends the capture with its ticket through the unchanged ingest.
    - The side panel shows each item's own state beside its run's own outcome.
- **Sender.** Loopback, `X-ICBM-Client`, the pinned extension origin when an `Origin` is present, and the pairing are all required; pairing replaces none. The pairing secret lives in the OS keyring only. Each request carries the extension identity, pairing id and generation, a timestamp, a nonce, the body digest and an HMAC over that tuple. The replay cache is bounded and in memory.
- **Policy.** One `BrowserCapturePolicy` per supplier, at `integrations/suppliers/<supplier>/browser_capture_policy.json`. The extension holds no copy: it reads the canonical bytes before every cut, and every capture names the revision and digest the server recomputes.
- **Order of one ingest.** Authentication, then the ceilings (512 KiB, 20 000 elements, 40 image references, one capture at a time, 5 s between two), then the browser-observed transport evidence, the target check and the policy. Only then are a job and a canonical run opened. A refusal before that point creates no run.
- **Handoff.** The capture waits in a bounded in-process buffer for a non-idempotent, single-attempt job. It is never written to the database, a job payload, the filesystem or a log. This depends on the in-process worker (ADR-0002 Option A).
- **Browser sanitize (ADR-0019 §6, AC-12).** In E1 the browser's sanitize step is the cut itself: `ui/extension/lib/capture.js` serializes only the policy's allowed tags and attributes of the product scope, leaves out comments, scripts, form values and every excluded region, and refuses a capture over a bound.
- **Processing.** The server's own structural check (excluded tags and regions, attributes, the frame, declarations, nesting depth), then the security gate over exactly what arrived (ADR-0019 §6.1): one pass over every element, attribute and text refuses only a secret or the signed-in member's own account and identity, and everything else — a business contact, a member price, an odd image reference — goes on as it arrived and is only noted, because the pipeline goes on with the capture as it arrived and never with a sanitized copy. Then the same `DocumentView`, built from browser-observed values only, nothing defaulted.
- **One pipeline after capture (ADR-0019 §2, E2).** The collection owner records that `DocumentView` exactly as it records a direct one: the supplier's identity and role rules, the server's own policed image fetch, the canonical extractor as the revision writer, then the frozen shadow decision. The run's shadow and capture decisions are frozen when its job starts, as a direct run's are. An attempt that appended its revision and died before settling is finished from that revision; the capture is never processed twice.
- **Provenance.** `transport_kind`, `capture_policy_revision` and `capture_policy_digest` on the run and the revision (migration 0034): additive, nullable, never backfilled, never an identity input.
- **No server product read** is made for an extension run: its budget allows none and it reserves none. The server does fetch the product's images itself, through the collection gateway and under the supplier's image limits (ADR-0019 §7); the browser relays no image bytes.

The Adaptive Collector contract is `documents/decisions/adr/0017-adaptive-collector-profile-extraction-and-shadow-validation.md` (Issue #110). It authorizes no implementation by itself. Three production slices exist: the offline core (P1, `app/stages/collect/adaptive/engine/`), the profile and validation persistence owner (P2, `app/stages/collect/adaptive/store/`, migration 0024) and the shadow foundation (P3, `app/stages/collect/adaptive/shadow/`, migration 0025). P2 stores immutable EPR/PTR revisions with their DRAFT lint findings, the append-only lifecycle log, supplier-bound local-only validation samples and validation runs; `VALIDATED` is derived, never stored. P3 adds the append-only per-supplier shadow switch (the only way into `SHADOW`), freezes each run's shadow decision and exact bundle on the canonical run record at its first product-read reservation, runs the one-fetch shadow comparison after the canonical commit in its own write unit, and keeps the raw shadow records (90 days and 5,000 per supplier, whichever first), the evidence ledger and the evidence windows. Phase C stage C0 (`app/stages/collect/adaptive/phase_c_capture/`, migration 0027, and the stopped-app harness `automation/adaptive/phase_c/phase_c.py`) adds an in-memory ValidationSample capture seam, off by default and frozen per run at its first reservation, and the only operator path for the Phase C actions; the evidence record is `documents/acceptance/adaptive/ADAPTIVE-PHASE-C.md` (NOT ACCEPTED — C1 INCOMPLETE / STOPPED; the record owns this status). C1 PREP-0 (migration 0028) adds the durable accounting of every actual send of a Phase-C-accounted collection, reserved before transmission, with CONNECT authentication a hard zero; ordinary collections are unchanged. C1 PREP-1 adds the reviewed operator path for profile persistence. The server-transport C1 campaign `phase-c-kmretail-01` is **INCOMPLETE / STOPPED and preserved** (Issue #110 `5844538783`) with no Adaptive verdict, and its C2–C4 plan is paused and superseded for execution by ADR-0019. The Adaptive work is kept as the extraction and validation core behind the extension-primary transport. **No supplier's switch is on, so no run is shadowed or captured and no window is open**: each later stage needs its own authorization. There is no Adaptive `ProductFactsRevision` write and no `ACTIVE`, each of which needs its own authorized slice:
- it is a second implementation of the same parser seam: a generic engine interprets an immutable, digested `ExtractionProfileRevision` bundle, and the gateway, budget, image fetch, revision store and job owners stay as above;
- the access envelope (`CollectionProfile`) is never profile data and is never widened at run time;
- a shadow comparison reads the same one fetch, writes nothing canonical and makes zero AI/OCR calls; the canonical extractor stays the only revision writer until a separate cutover ADR.

- discover/open/extract
- raw evidence
- ProductFactsRevision creation
- stock evidence
- source image discovery/fetch/checksum
- ambiguity → REVIEW_REQUIRED

### PRODUCT DB
Owns canonical product identity and normalized accepted state.

- Product identity
- current source ProductFactsRevision per member source product
- atomic source SKU/option mapping
- enrichment proposals/accepted values
- pricing snapshots
- registration readiness inputs

### REGISTER
Owns platform conversion and listing creation.

- category mapping
- compliance gate
- marketplace publication-asset requirements, upload and read-back (binary transformation itself remains the M4 derived-image owner)
- readiness
- RegistrationAttempt/idempotency
- CREATE/reconcile/read-back
- MarketplaceRegistration

The M5 REGISTER contract is `documents/decisions/adr/0014-smartstore-register-idempotency-readback.md` (Issue #89):
- M4 keeps every product-side owner. Marketplace-sized binaries stay M4 derived artifacts; M5 owns the upload and the provider asset identity.
- Each provider-listing unit has one immutable `RegistrationSnapshot` and one CREATE `RegistrationIntent`. Read-back is compared to that Snapshot, never to current state.
- An unresolved `UNKNOWN` CREATE is never resent. It ends only on positive reconcile (an exact candidate read back by its provider number carrying the same code, then the Snapshot comparison), on a read-back by an already known provider identity, or on later machine proof of non-application (ADR-0014 §28.2–§28.3) — never on a seller-side code alone or a zero-result lookup (§7, §17.2). Until then it blocks every new CREATE Intent in its marketplace × account × group conflict scope.
- **The adopted provider surface is still narrow.** At this main the two SmartStore product read-backs, the bounded image upload, product CREATE and the product search — for positive-only reconcile only, never duplicate absence — are `ADOPTED`; `product_registration.write` stays `UNVERIFIED`, and execution stays `DRY_RUN`, so no listing has been created and no provider has been read. The state and what still blocks a bounded canary are recorded in `documents/acceptance/milestones/M5.md` §9 and `documents/contracts/platforms/smartstore/ENDPOINT_MATRIX.md` §4.

#### Registration authoring and AI boundary

Issue #127 records the sequencing clarification for the Registration Management redesign.

The operator surface may have three depths — a list for batch-oriented work, a quick-review panel,
and a full one-product editor — but **screen depth does not create new truth owners**. The full editor
must read/write through the existing Product, image, Item/Pricing, readiness and REGISTER owners. It
must not introduce a parallel "edited product" database that copies ProductFacts or marketplace
state.

The first-vertical authoring path is deterministic/manual and remains fully usable with every AI
capability unavailable. This restates repository-canonical M5 requirements: ADR-0014 §18 says M5
registers with no AI provider configured, and `documents/acceptance/milestones/M5.md` §2 keeps AI outside M5
acceptance. It is not a new AI availability requirement introduced here.

Before that vertical is accepted, the approved/prototype UX may reserve an AI control's final
position, but the production runtime must not render that control at all until an authoritative
server capability/owner exists. A disabled placeholder with no authoritative reason is not a valid
runtime state. If no server owner can state why the capability is unavailable, the client must not
invent or hardcode that reason. The UI must not manufacture a result, call an unadopted platform
endpoint, or create an interim client-owned enrichment store merely to make the control active.

When registration AI is implemented after the first vertical, it reuses the Canonical v3.1 §7 and
Issue #30 contracts:
- tasks remain independent (`recommended_name`, `recommended_tags`, category validation,
  required-option mapping and fact review), with platform-specific name/tag projections where the
  canonical contract already defines them;
- runtime prompt composition remains persisted `ROLE + PlatformPolicy + PROMPT task`, through the
  ADR-0012 provider-neutral AI port;
- `AI_UNREVIEWED` stays visible but is not itself a registration blocker;
- final user-approved values use the canonical `field × marketplace × account` lock boundary, and
  optimistic-concurrency mismatches are skipped rather than overwritten;
- the storage scope of an AI recommendation/cache is decided by its implementation contract and is
  **not** inferred from the final-value lock scope;
- Product-information notice AI may validate/normalize/flag supported facts but never invents a
  missing legal/source fact;
- a platform search/tag/metadata signal enters only through an adopted platform contract and the
  appropriate adapter. SearchSignalAdapter is not a bypass around the endpoint registry. For an
  endpoint not yet represented in the matrix, the order is candidate registration → official
  evidence review → adoption only if sufficient → adapter implementation;
- efficacy/functionality/target expressions reach final tags only when the deterministic evidence
  and platform-policy requirements of Canonical v3.1 §7.8 are satisfied. AI is not the final policy
  owner.

**AI_INITIAL timing stays unresolved until the AI implementation contract.** Canonical v3.1 §7.6
defines target-scoped name/tag projections and also says the first collection auto-applies an
initial recommendation, while Gate 1 can establish the RegistrationTargetSet later. This
clarification does not amend the frozen canonical text. The first SmartStore AI slice therefore
covers recommendation generation/presentation only; automatic application as `AI_INITIAL` remains
deferred until that timing contract names the first eligible target/enrichment event. Until then,
an absent `final_name` uses the canonical `original_name` fallback and no collection-time
platform projection is fabricated.

Seasonal-keyword expiry is a later tag-enrichment concern. It is modeled separately from ordinary
facts/prompt/policy `STALE`; a locked final value is never silently deleted because time passed.
Bulk AI and bulk registration are later orchestration over accepted single-product paths, not a
separate truth system.

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
- provenance: revision ID, extractor revision and fingerprint, collection run/correlation, `facts_status`; a profile-interpreted revision also carries its profile provenance (ADR-0017 §5), and no existing revision is backfilled;
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
ICBM canonical identity. References each member source product's current source facts revision, and downstream state.

The M4 product contract is `documents/decisions/adr/0013-m4-canonical-product-contract.md` (Issue #80):
- The canonical `Product` **is** the Canonical v3.1 `ProductGroup`: one entity and one identifier, with no second product root. `icbm_product_id` is the name this document uses for that identifier (`documents/architecture/GLOSSARY.md`).
- Each member source product keeps its own current source revision pointer. That pointer is never an "accepted" or confirmed revision: it may point to a `REVIEW_REQUIRED` revision, and readiness carries that ambiguity.
- A `PricingSnapshot` is per Item **and** per explicit pricing context (marketplace, account where it matters, fee and policy versions). Readiness is layered: base readiness, per-context pricing readiness, then M5 registration preflight.
- Sellable Items are `group identifier + composition_signature`.

### MarketplaceRegistration
Always includes:

```text
marketplace_key
marketplace_account_id
marketplace_product_id
seller_product_code
published_state
last_readback_at
```

`marketplace_account_id` is the canonical spelling of the account identity in every registration row and every new account-scoped owner. `account_id` is not an account identity: in M4 pricing it is the pre-existing pricing-context discriminator (the account, or `None` when account-invariant), and in the SmartStore token contract it is a provider request field (`documents/architecture/GLOSSARY.md` §1). `seller_product_code` holds the listing identity as sent (ADR-0014 §7).

The canonical product is reached **through the registered Items**: each `MarketplaceRegistrationItem` carries its `registration_item_key`, the frozen Item snapshot it registered and the group that Item belonged to. The registration row holds no second `icbm_product_id` copy of that relation.

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

**Today the implemented schema is KRW-only.** `product_facts_revisions.currency` is constrained to `KRW`, and the pricing owner computes in whole KRW with one rounding rule. That is sufficient for the first vertical (KM통상 → SmartStore, §15). A second-currency supplier — 1688, Rakuten or any other — first needs a currency and FX-snapshot extension of the source and pricing schema, decided in an ADR. **No such extension is authorized now**, and horizontal supplier expansion cannot start before it exists (`documents/roadmap/ROADMAP.md` §14).

## 7. Registration safety

### ComplianceGate

```text
PASS | REVIEW_REQUIRED | BLOCKED
```

BLOCKED cannot be bypassed by automation. Resolution requires changed/verified evidence and an audit trail.

**No production ComplianceGate owner exists yet.** The states above are the contract; no service decides them at this main, and the REGISTER preflight carries no compliance verdict of its own. Until that owner is implemented and accepted, regulated goods — 건강기능식품, KC certification, 식약처 notices, prohibited wording and every other regulated category — must not be claimed as automatically registrable, and **the first bounded canary uses a non-regulated product**: one whose reviewed category metadata proves it outside every regulated category — an eligibility restriction recorded for the canary, never a `COMPLIANCE PASS` (ADR-0018 §5). Gate 3 implements no ComplianceGate.

### RegistrationAttempt

CREATE is stateful and idempotent:

```text
PREPARED → SENT → CONFIRMED
                 ↘ UNKNOWN
                 ↘ FAILED
```

`UNKNOWN` is reconciled before any retry. Never blindly resend CREATE.

What may settle an `UNKNOWN` is fixed by ADR-0014 §10, as ADR-0014 §28 narrows it, and that stays authoritative: `NOT_APPLIED_PROVEN` or remote absence needs evidence that satisfies the adopted, operation-specific proof contract — a provider read-back under the adopted contract, transmission-precluded evidence (no transport handoff), or another explicitly reviewed machine or provider proof. **A provider lookup by the listing identity is positive evidence only** (ADR-0014 §28.2, M5-31): for SmartStore it may only make a product's presence provable, by a read-back of that exact candidate carrying the same ICBM `sellerManagementCode`, and it never establishes `NOT_APPLIED_PROVEN` or absence — a zero, several or absent result proves neither. An operator assertion is never the evidence.

The deterministic seller-side product code is **ICBM's own correlation identity** for a provider-listing unit (ADR-0014 §7). It is not a provider uniqueness guarantee, and it does not by itself make a lookup deterministic. **Remote absence via a provider lookup is what the current evidence does not support**: no lookup contract with proven key, uniqueness and completeness semantics is adopted, and **a lookup that returns nothing is not proof of absence** (ADR-0014 §17.2). So a CREATE that may have been transmitted, and whose ambiguity no admissible evidence resolves, stays `UNKNOWN` under its `REVIEW_REQUIRED` workflow overlay, keeps its conflict scope closed, and is never blindly replayed; the current evidence verdict is recorded in `documents/acceptance/milestones/M5.md` §9.

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

Core error classes — the aligned taxonomy of `documents/decisions/adr/0008-error-taxonomy-alignment.md`, which also records how it relates to `documents/architecture/frozen/CANONICAL-V3.1.md` §11.3:

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

Job states, transitions and the per-attempt history are fixed by `documents/decisions/adr/0005-durable-job-state-and-attempt-history.md`.

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

**The ReviewItem owner exists** (G2-A: `app/capabilities/review/owner.py`, migration 0021) **with the COLLECT / M3 producer** (G2-B: `app/capabilities/review/collect_producer.py`, coverage watermark migration 0022). COLLECT's `REVIEW_REQUIRED` conditions on each source identity's current revision are indexed, reconciled at startup and periodically, and shown on the COLLECT screen's run focus with the coverage verdict. **G2-C** adds the M4 base-readiness producer (`app/capabilities/review/products_producer.py`), the REGISTER execution producer (`app/capabilities/review/register_producer.py`: an `UNKNOWN` Intent, a read-back `MISMATCH`, a PAUSED execution scope) and the REGISTER preparation producer (`app/capabilities/review/preflight_producer.py`: each current durable preparation's candidate preflight, derived by the existing preflight owner and anchored on the preparation revision), each reconciled at startup and periodically like COLLECT. Each owner screen lists its own scope's items with its producers' coverage (`GET /api/v1/review/items`, `app/capabilities/review/scopes.py`): 수집관리 a source product, 통합DB a Product or Item, 등록관리 an account, Draft, Intent or preparation. Only those exact canonical scope shapes are read; an incomplete or mixed shape is refused (`REVIEW_SCOPE_UNSUPPORTED`), since a partial scope would reach across scopes (ADR-0016 §8). Every watermark stores the owner truth token its pass was fenced on (migration 0023), and coverage compares it with the owner's token on every read, so an owner that moved since is not current at once. `ReviewService.open_counts()` counts durable OPEN rows per kind; a kind is `CURRENT` (the count is authoritative) only when **every** producer that can emit it is wired and current, `NOT_CURRENT` when one is wired but not current, and `NOT_WIRED` when one does not exist or never completed a full pass (`app/capabilities/review/counts.py`). Neither of the last two carries a count. STOCK is therefore never authoritative on COLLECT's coverage alone, and the dashboard and 품절 are never EMPTY on a count that is not authoritative.

The owner's contract is `documents/decisions/adr/0016-gate2-human-review-path-and-review-item-owner.md` (Gate 2). A ReviewItem is a durable index of human work over a condition a production owner already derives, never a source of that owner's truth:
- it is deduplicated by server-computed condition and review keys;
- a changed owner revision supersedes the old item, and reconciliation alone decides whether an item is open;
- a human resolution changes no owner fact and leaves the item open while the owner still derives the condition;
- a kind without a fully reconciled producer is reported as `NOT_WIRED`, never as zero;
- missed indexing is recovered by a full reconciliation at process startup and periodically while running, never only by the next event for that scope, and a count is authoritative only while that coverage is current.

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

> **Amendment note (B-DETAIL; owner decision 2026-10-04).** The detail body is composed from the
> selected detail images and plain operator text; only the trusted REGISTER renderer writes the
> provider `detailContent`, from uploaded provider asset identities, never from a source URL or an
> operator-authored string (ADR-0014 §19 amendment note). Decided by the owner on 2026-10-04; it takes effect with the B-DETAIL implementation slice (design draft `documents/reviews/B-DETAIL-detail-composition.md`), and until then the rule above holds unchanged.

COLLECT covers only the first three steps for *source* assets (ADR-0010 §9):
- It keeps the original bytes unmodified, with role/order, dimensions, MIME type, size and SHA-256.
- It never transforms them.

Derived marketplace variants (the transformed binary, its lineage and its QA) belong to the M4 derived-image owner. The marketplace adapter supplies only the target profile requirement; REGISTER uploads that exact artifact and owns the provider asset identity and read-back (ADR-0014 §5).

## 11. Source drift

Recollection compares facts fingerprints, only between revisions with equal `comparability_key` (ADR-0013 §3 as amended by ADR-0017 §5.3; for every revision without profile provenance that is the same `extractor_revision`).

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

**DRY_RUN is the boot default and the state every process starts in** (ROADMAP §14 item 5, the ADR-0018 §2 amendment note). `LIVE` exists only as one bounded, audited window held in the running process's memory: it opens only inside an approved bounded LIVE mutation scope — while a live grant exists, and never past it — for a requested duration of at most four hours, asks for no approval or evidence identity of its own (the scope carries the user's approval, and its GitHub evidence is the agent's bookkeeping, ADR-0022 §3), lapses by itself, is never widened while open, closes on a `DRY_RUN` request and never survives a restart. A `LIVE` request that names no window is denied with `M0_LIVE_FORBIDDEN`, and every request is audited. While no window is open the stack's execution-mode layer refuses every mutation (`M0_DRY_RUN_ONLY`). **The mode alone is never authority for a mutation.** Before any bounded real canary, the authorization contract that replaces it — who may enter LIVE, for which marketplace, account, endpoint group and time-bounded scope, on what recorded approval, and how it returns to DRY_RUN — must be decided and accepted. **That contract is ADR-0018 (Gate 3, G3-0). Its owners exist provider-zero since Gate 3 area 1 — the grant, the brake, the ASSET upload-attempt owner and the send-time stack, integrated deny-by-default into the CREATE and ASSET paths — and the stack's execution-mode layer refuses every mutation while `M0_DRY_RUN_ONLY` holds; Gate 3 area 2 adds the restore-drill and evidence-retention proofs as durable owners, and area 3 the reviewed populated visual acceptance record, bound to the exact accepted commit and running code (proofs, never permission)**: a bounded, audited, server-owned LIVE grant is the only authority for a marketplace mutation, a durable fail-closed protected-write brake stops every new mutation, and a mutation starts only when every layer of that safety stack allows it — beside, never instead of, the ADR-0014 §26 execution-scope brake. A canary has two mutation stages, the image upload (ASSET, before any Snapshot) and CREATE (after the freeze), each with its own exact grant, restore proof and send-time readiness. ROADMAP §14 item 5 is the implementation slice that replaces `M0_DRY_RUN_ONLY` under it: inside an open window the execution-mode layer allows, and every other layer still decides. CREATE and the positive-only reconcile search are adopted, which satisfies only the CREATE stage's endpoint-adoption layer; a canary stays `BLOCKED` on every other prerequisite (ADR-0018 §6, §10). CREATE's adoption is an endpoint contract in code only (`documents/contracts/platforms/smartstore/ENDPOINT_MATRIX.md` §4.1.1), never a session, a provider call or LIVE authority: the CREATE seam reads the CONNECT owner's committed bearer only while a proven current committed session exists (ROADMAP §14 item 4), no provider-listing unit becomes sendable by adoption, and the ADR-0018 §10 stack — `M0_DRY_RUN_ONLY` outside a LIVE window — and the residual-risk gate G3-30 still refuse every mutation.

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

Acceptance evidence belongs under `documents/acceptance/`, not in chat.

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
