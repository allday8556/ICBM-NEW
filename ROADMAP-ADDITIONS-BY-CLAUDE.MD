# ROADMAP — 추가 요청 사항 (BY CLAUDE)

> Companion document to `ROADMAP.md`.
> Status: **PROPOSAL / REVIEW NOTES — not an approved contract.**
> Nothing in this file is binding until the architect accepts it and it is merged into `ROADMAP.md` or `docs/ARCHITECTURE.md`.
> Scope: gaps that, if left unresolved, force a schema or contract rewrite later.

---

## 0. How to read this document

Each item is written as:

```
GAP          what is missing in the current roadmap
WHY          the concrete failure it causes
PROPOSAL     the contract / deliverable to add
INSERT INTO  where it belongs in ROADMAP.md
ACCEPTANCE   how we know it is done
```

Priority:

| Level | Meaning |
| ----- | ------- |
| **P0** | Must be decided **before M1 starts**. Deciding later means migrating the DB or rewriting contracts. |
| **P1** | Decide before the phase it belongs to. |
| **P2** | Operations / safety. Can land incrementally, but must exist before the first vertical is declared accepted. |
| **P3** | Documentation and process hygiene. |

---

## 1. Summary

| ID | Item | Priority | Target section in ROADMAP.md |
| -- | ---- | -------- | ---------------------------- |
| A1 | Fulfillment loop (발주 → 송장 → 배송추적) | P0 | §8 Phase 5 |
| A2 | Runtime stack decision | P0 | §3 Phase 0 |
| A3 | Job queue / scheduler | P0 | §3 Phase 0 |
| A4 | Idempotency & reconciliation on marketplace CREATE | P0 | §7 Phase 4 |
| A5 | FX (환율) policy | P0 | §6 Pricing contract |
| A6 | Image pipeline | P0 | §5 / §7 |
| A7 | `CLAUDE.md` repository rules file | P0 | §1.2 Roles |
| B1 | Pricing inputs: VAT, settlement fee, return shipping | P1 | §6 Pricing contract |
| B2 | Source-change detection (price/option/image drift) | P1 | §8 Phase 5 |
| B3 | Category mapping store | P1 | §7 Phase 4 |
| B4 | Compliance gate (인증 / 금지어 / 상품정보제공고시) | P1 | §7 Registration readiness |
| B5 | Multi-account support on registrations | P1 | §6 Canonical structure |
| B6 | `ProductFacts` vs `Product` boundary | P1 | §5 / §6 |
| B7 | Marketplace API rate limit & quota accounting | P1 | §2 architecture |
| B8 | Error taxonomy & platform error mapping | P1 | §3 Phase 0 |
| B9 | REVIEW_REQUIRED work queue | P1 | §5 Phase 2 |
| C1 | Dry-run / safe mode switch | P2 | §13 DoD |
| C2 | Crawl policy (rate, ban detection, session) | P2 | §4 Phase 1 |
| C3 | Evidence retention policy | P2 | §5 Phase 2 |
| C4 | Audit log for destructive/protected actions | P2 | §1.2 Roles |
| C5 | Backup & restore drill | P2 | §3 Phase 0 |
| C6 | Correlation ID across collect → register → operate | P2 | §3 Phase 0 |
| D1 | CI pipeline | P3 | §3 Phase 0 |
| D2 | ADR directory | P3 | §1.2 |
| D3 | Field-naming glossary | P3 | docs/ |
| D4 | Acceptance evidence format | P3 | §13 DoD |
| D5 | Performance targets | P3 | §13 DoD |
| D6 | Document hygiene (heading levels, version log) | P3 | ROADMAP.md |

---

# 2. P0 — must be settled before M1

## A1. Fulfillment loop is missing (발주 → 송장 → 배송추적)

```
GAP
Phase 5 ends at "the order knows its source product". There is no step that
actually purchases from the supplier and returns a tracking number to the
marketplace.

WHY
For a 위탁 / 구매대행 model this is the revenue loop. Without it Phase 5 is a
read-only dashboard: orders arrive, nothing ships, and the marketplace
penalises late 발송처리. Adding SupplierOrder after the schema is built means
migrating Order, Product, and Operational State at once.

PROPOSAL
Add a fifth stage to the product goal line:

    CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE → FULFILL

New entity:

    SupplierOrder
      supplier_order_id          (canonical, ICBM-owned)
      marketplace_order_id
      marketplace_order_item_id
      icbm_product_id
      source_option_sku
      supplier_key
      ordered_quantity
      purchase_price_snapshot
      fulfillment_state          NOT_ORDERED | ORDERING | ORDERED
                                 | AWAITING_TRACKING | SHIPPED
                                 | CANCELLED | FAILED | REVIEW_REQUIRED
      supplier_order_reference   (공급사 주문번호)
      courier_code
      tracking_number
      tracking_uploaded_at
      last_error

Flow:

    Marketplace order ingest
    → map to ICBM product + atomic SKU
    → supplier order placement (manual first, automated later)
    → supplier order reference recorded
    → tracking number acquired
    → marketplace 발송처리 / tracking upload
    → delivery state read-back
    → 구매확정 / settlement

INSERT INTO
§8 as a new 8.5, and §12 development sequence between M6 and FIRST VERTICAL
ACCEPTED. §0 product goal line updated.

ACCEPTANCE
One real marketplace order maps to exactly one SupplierOrder, a tracking
number is written back to the marketplace, and the state survives a restart.
Manual 발주 is acceptable for the first vertical; automation is Phase 6.
```

**Note:** decide explicitly whether 발주 is automated (supplier cart/API) or manual-with-record for the first vertical. Manual is the right call for M6 — but it must still be *recorded in the canonical DB*, not in a spreadsheet.

---

## A2. Runtime stack is not decided anywhere

```
GAP
No language, framework, DB engine, browser-automation tool, process model,
or deployment target is stated in ROADMAP.md.

WHY
The implementation role is delegated to Claude Code. An unstated stack is
re-decided on every session, which is exactly the drift this reset was meant
to eliminate. It also silently decides other things: SQLite forbids the
concurrent job model in A3; a single-user desktop app changes the secret
storage boundary in Phase 0.

PROPOSAL
Pin the following in docs/ARCHITECTURE.md §1, before any code:

    language / runtime         e.g. Python 3.12 / Node 22
    web or app framework
    database                   Postgres | SQLite  (+ migration tool)
    browser automation         Playwright | Puppeteer | requests-only
    job runner                 see A3
    process model              single-user local | single-tenant server
    deployment target          local desktop | VPS | container
    secret storage             OS keychain | age/sops file | KMS

INSERT INTO
§3 Phase 0 deliverables, as the first item.

ACCEPTANCE
docs/ARCHITECTURE.md §1 exists and a clean checkout runs the health endpoint
using exactly that stack.
```

---

## A3. No job queue / scheduler in the foundation

```
GAP
Phase 0 lists logging, config, DB, health, error contract — but no
asynchronous execution layer. Every real workload in this system is async:
collection, stock recheck, order polling, inquiry polling, bulk registration.

WHY
Retry, backoff, concurrency limits and failure visibility cannot be retrofitted
without touching every service. Retrofit cost grows linearly with the number
of adapters.

PROPOSAL
Add to Phase 0:

    Job
      job_id
      job_type            COLLECT | REGISTER | STOCK_SYNC | ORDER_SYNC
                          | INQUIRY_SYNC | TRACKING_UPLOAD
      target_ref          (icbm_product_id / order_id / supplier_key)
      state               QUEUED | RUNNING | SUCCEEDED | FAILED | DEAD
      attempt_count
      next_attempt_at
      correlation_id      (see C6)
      last_error_code     (see B8)
      created_at / finished_at

Policy to define:
  - retry schedule and max attempts per job_type
  - dead-letter handling and who reviews it
  - per-supplier and per-marketplace concurrency caps (ties to B7, C2)
  - schedule definitions for recurring syncs

INSERT INTO
§3 Phase 0 deliverables and §2 target architecture (a cross-cutting layer
beside the vertical flow).

ACCEPTANCE
A deliberately failing job retries on schedule, lands in dead-letter after the
cap, and is visible without reading raw log files.
```

---

## A4. Marketplace CREATE has no idempotency or reconciliation

```
GAP
§7 pipeline goes "Payload builder → Marketplace CREATE → read-back". There is
no handling of the unknown-result case: request sent, response lost.

WHY
Retrying a lost CREATE produces duplicate live products on SmartStore. That
violates the roadmap's own Phase 5 acceptance ("no duplicate product identity
is created").

PROPOSAL
    RegistrationAttempt
      attempt_id
      icbm_product_id
      marketplace_key
      account_id                (see B5)
      idempotency_key           deterministic: hash(icbm_product_id,
                                marketplace_key, account_id, payload_version)
      state                     PREPARED | SENT | UNKNOWN | CONFIRMED | FAILED
      request_payload_hash
      marketplace_product_id
      error_code
      sent_at / confirmed_at

Rule:
  - state = UNKNOWN is never resolved by re-sending CREATE.
  - UNKNOWN is resolved only by a reconcile read (search the marketplace by
    seller product code / 판매자상품코드) before any retry is permitted.
  - the seller-side product code must be ICBM-generated and deterministic, so
    reconciliation is always possible.

INSERT INTO
§7 Phase 4 pipeline and Phase 4 acceptance.

ACCEPTANCE
Kill the process mid-CREATE. On restart, the system resolves the attempt to a
single marketplace product ID and does not create a second listing.
```

---

## A5. No FX (환율) policy, but 1688 and Rakuten are on the supplier list

```
GAP
§6 pricing contract inputs are purchase price, supplier shipping, platform fee,
ad cost, target margin, minimum_sale_price — all implicitly KRW.

WHY
Supplier order 6 (1688) is CNY and order 7 (Rakuten) is JPY. Without a snapshot
rule, the same product recomputes to a different price on every run and no one
can explain why. Currency also has to be preserved on the source fact, not
converted at collection time.

PROPOSAL
Source facts keep the original currency:

    ProductFacts.currency              CNY | JPY | KRW | ...
    ProductFacts.price_in_source_currency

Pricing records the conversion it used:

    Pricing.fx_rate
    Pricing.fx_base / fx_quote
    Pricing.fx_source            (which rate provider)
    Pricing.fx_captured_at
    Pricing.rounding_rule        (e.g. ceil to nearest 10 KRW)

Rules:
  - conversion happens in the pricing owner only, never in a collector or a
    UI component
  - a stored price never silently changes; recomputation is an explicit,
    logged event with a new fx snapshot
  - define the staleness threshold that triggers recomputation

INSERT INTO
§5 required facts (add currency), §6 pricing contract.

ACCEPTANCE
A 1688 product's stored KRW price can be fully explained from the stored fx
snapshot and rounding rule, and does not change on re-read.
```

---

## A6. Image pipeline is absent

```
GAP
§5 lists "representative images" and "detail images" as facts. There is no
processing, hosting, or upload path anywhere in Phase 4.

WHY
Image handling is the single most common cause of registration rejection, and
hotlinking supplier URLs means the listing breaks the day the supplier blocks
referrers or reorganises their CDN.

PROPOSAL
Add to Phase 4 (and partially Phase 2):

    Collect    record source image URLs + order + role (representative/detail)
    Fetch      download to ICBM-owned storage, store checksum
    Validate   format, dimensions, file size, count vs platform limits
    Transform  resize / re-encode / strip metadata as platform requires
    Dedupe     checksum-based, per product and across products
    Upload     marketplace image upload API
    Persist    marketplace image ID / URL against the registration

    ProductImage
      image_id
      icbm_product_id
      role              REPRESENTATIVE | ADDITIONAL | DETAIL
      sort_order
      source_url
      local_path / storage_key
      checksum
      width / height / bytes / format
      marketplace_uploads[]   (marketplace_key, remote_id, remote_url)

Also decide: detail-page HTML is stored as facts — are embedded images inside
it rehosted too, or left as supplier URLs? (Recommendation: rehost. Leaving
them creates the same breakage one layer down.)

INSERT INTO
§5 Phase 2 (fetch + checksum) and §7 Phase 4 (transform + upload).

ACCEPTANCE
A registered SmartStore product renders correctly after the supplier's
original image URLs are made unreachable.
```

---

## A7. Repository rules need their own file, not just a roadmap section

```
GAP
§1.1 and §1.3 define the strongest rules in the project ("no legacy code, no
#86 transplant"). They live in the middle of a 680-line roadmap.

WHY
The stated workflow is: ChatGPT architects, Claude Code implements. An
implementer that does not re-read §1.1 every session will eventually reach for
legacy code because it is convenient. The rule needs to be where the
implementer actually looks.

PROPOSAL
Create /CLAUDE.md at the repository root containing:
  - the no-legacy rule, stated in full and unconditionally
  - contract-first rule: UI → contract → service → adapter, never shortcut
  - the pinned stack from A2
  - commit / branch / PR conventions
  - the dry-run rule from C1: when real marketplace writes are permitted
  - what requires user approval before execution (destructive / protected)
  - where decisions are recorded (ADR, D2)

INSERT INTO
§1.2 Roles — GitHub as Source of Truth should name the files that carry the
rules.

ACCEPTANCE
The file exists, and a fresh implementation session can state the no-legacy
rule and the stack without being told in chat.
```

---

# 3. P1 — contract reinforcements

## B1. Pricing inputs are incomplete

```
GAP
Missing from §6 inputs: VAT (부가세), settlement/card fee distinct from
platform commission, return & exchange shipping cost, coupon/discount burden,
free-shipping threshold, 도서산간 surcharge policy.

WHY
§10 analytics promises "net margin". With the current inputs the computed
margin cannot reconcile against the marketplace settlement statement, so the
number is decorative.

PROPOSAL
Extend the pricing input list, and add settlement data as a named source in
§10 analytics:

    Settlement
      marketplace_key / account_id
      settlement_period
      marketplace_order_id
      gross_amount / commission / fees / adjustments / net_payout

Rule: reported margin is *estimated* until a settlement row exists, then it is
*actual*. The UI must distinguish the two.

INSERT INTO §6 pricing contract, §10 analytics.
```

## B2. No source-change detection

```
GAP
Phase 5 syncs stock. Nothing detects that the supplier changed the price, an
option, the images, or the detail content after registration.

WHY
A disappeared option that is still live on the marketplace becomes an order
that cannot be fulfilled — a claim, not a bug report.

PROPOSAL
    ProductFacts.source_fingerprint      hash over the normalized fact set
    ProductFacts.fingerprint_parts       per-field hashes (price/options/
                                         images/detail/notice)

On recollection: diff → classify → route.

    price change      → recompute pricing → propose marketplace update
    option removed    → REVIEW_REQUIRED (never auto-delete a live option)
    option added      → propose addition
    image change      → re-run image pipeline
    detail change     → REVIEW_REQUIRED

INSERT INTO §8 Phase 5 as 8.2b, sharing the stock-recheck job.
```

## B3. Category mapping has no store

```
GAP
§7 mentions AI category ranking, but there is no persisted mapping.

WHY
Non-deterministic and repeatedly paid-for. Also unreviewable: a wrong category
silently repeats across every product from that supplier category.

PROPOSAL
    CategoryMapping
      supplier_key / source_category_path
      marketplace_key / marketplace_category_id
      marketplace_category_tree_version
      confidence
      decided_by        AI | USER | RULE
      decided_at

AI proposes candidates; the mapping table decides. Tree-version change
invalidates mappings for re-review rather than failing silently.

INSERT INTO §7 Phase 4, between enrichment and readiness.
```

## B4. Compliance needs to be its own gate

```
GAP
§7 readiness includes "prohibited/sold-out policy" as one line among ten.

WHY
The supplier list includes 건강산 (health products). 건강기능식품, 의약외품,
의료기기, KC 인증 대상, 식품 표시사항 each carry registration restrictions and
legal exposure, not merely a rejected API call. This is a different class of
failure from "missing image".

PROPOSAL
Split a ComplianceGate service out of readiness:

    category → required certifications (KC / 식약처 신고번호 / 인증번호)
    금지어 / 과장광고 표현 screening on name, tags, detail
    상품정보제공고시 field set per category per marketplace
    seller-level eligibility (판매 자격 / 영업신고 required?)
    result: PASS | BLOCKED(reason) | REVIEW_REQUIRED

BLOCKED must be unbypassable by automation; only the user can override, and the
override is audit-logged (C4).

INSERT INTO §7 registration readiness — as a separate preceding gate.
```

## B5. Single vs multiple marketplace accounts

```
GAP
Nothing states whether there is one SmartStore account or several.

WHY
account_id on MarketplaceRegistration, RegistrationAttempt, Order, and
Settlement is a schema-level decision. Adding it later is a migration across
the most-written tables.

PROPOSAL
Add account_id from the start, even if exactly one account exists in the
first vertical. Cost now: one column. Cost later: a migration plus every
adapter call site.

INSERT INTO §6 canonical structure.
```

## B6. ProductFacts / Product boundary is ambiguous

```
GAP
Phase 2 saves ProductFacts. Phase 3 creates the canonical Product. It is not
stated whether these are two tables from the start or whether facts are
promoted into Product.

WHY
M3 (collect) precedes M4 (canonical DB) in §12, so M3 has to write somewhere.
Whatever M3 writes is the de-facto schema.

PROPOSAL
State explicitly: ProductFacts is a separate, append-oriented table keyed by
(supplier_key, source_product_id); Product references the current facts row.
Facts are never mutated in place — recollection writes a new revision, which
also gives B2 its diff for free.

INSERT INTO §5 and §6, plus §12 M3/M4 note.
```

## B7. No marketplace API rate limit / quota accounting

```
GAP
Adapters call marketplace APIs with no central limiter.

WHY
SmartStore, Coupang and 11st each enforce call limits. Bulk registration
(§12, after the first vertical) will hit them immediately, and a throttled or
suspended API key blocks the whole pipeline.

PROPOSAL
One rate-limit/quota component shared by all marketplace adapters:
per-key limits, 429 backoff, quota consumption counters, and a circuit breaker
that pauses the relevant job types rather than failing them individually.

INSERT INTO §2 architecture, as a cross-cutting layer beside the job runner.
```

## B8. Error taxonomy is named but not defined

```
GAP
Phase 0 lists "error contract" with no content.

WHY
Retry policy (A3), circuit breaking (B7) and the review queue (B9) all need to
know whether an error is retryable, permanent, or needs a human.

PROPOSAL
    ErrorClass  TRANSIENT | RATE_LIMITED | AUTH | VALIDATION
                | POLICY_BLOCKED | NOT_FOUND | UNKNOWN

Each adapter maps platform-specific codes into this set; the core never
inspects raw platform codes. UNKNOWN is always treated as
"do not retry destructive operations, escalate to review".

INSERT INTO §3 Phase 0.
```

## B9. REVIEW_REQUIRED has no home

```
GAP
REVIEW_REQUIRED appears in §5 (evidence), §5 (sold-out), and implicitly
elsewhere, but no queue, owner, or screen is defined.

WHY
A state that nothing surfaces is equivalent to silently dropped work.

PROPOSAL
    ReviewItem
      review_id
      kind              COLLECT_EVIDENCE | STOCK | SOURCE_CHANGE
                        | COMPLIANCE | REGISTRATION_ERROR | FULFILLMENT
      icbm_product_id / order_id
      opened_at / resolved_at / resolved_by
      resolution

Surfaced in the Collection Management and Sold-out Confirmation screens per
§11, with a global count on the Dashboard.

INSERT INTO §5 Phase 2 acceptance and §11 UI mapping.
```

---

# 4. P2 — operations & safety

## C1. Dry-run / safe mode

§13 DoD requires real read/write against external systems. That is correct for
proving the architecture, and dangerous during iteration. Add a global
execution mode — `DRY_RUN | SANDBOX | LIVE` — where every marketplace-writing
adapter method asserts the mode before acting, plus a documented test seller
account policy. A destructive call in the wrong mode should fail loudly rather
than be quietly skipped.

## C2. Crawl policy

Phase 1 covers login and session, not politeness or survival. Define per
supplier: minimum request interval, max concurrency, user-agent policy,
ban/challenge detection, backoff on detection, and a re-login loop guard
(repeated failed logins must stop, not spin — a locked supplier account halts
the entire project). Also: what happens when the supplier's DOM changes —
collection-profile versioning plus a breakage alert, not a silent empty result.

## C3. Evidence retention

Storing raw HTML snapshots and original images is right for auditability and
grows fast. Define retention: how long full snapshots are kept, what is
compressed, what is reduced to a checksum, and the storage ceiling that
triggers pruning.

## C4. Audit log for protected actions

§1.2 says the user owns "protected/destructive approvals" but no mechanism
records them. Add an append-only audit log: actor, action, target, before/after,
timestamp, correlation_id. Minimum coverage: marketplace delete/deactivate,
compliance override (B4), bulk operations, credential changes, DB destructive
migrations.

## C5. Backup & restore drill

The canonical DB is described as the spine of the application. The Phase 0
deliverable should be a *tested restore*, not a backup script: documented
procedure, one rehearsed restore into a scratch environment, recorded RPO/RTO.

## C6. Correlation ID

Phase 0 lists structured logging but nothing that ties a single product's
journey together. Issue a correlation_id at the entry point of every flow and
propagate it through collect → facts → pricing → registration attempt →
marketplace call → operate sync → job records. Without it, debugging the first
vertical means hand-joining timestamps.

---

# 5. P3 — documentation & process

## D1. CI
GitHub is declared the Source of Truth, but Phase 0 has no CI. Add GitHub
Actions running: lint, format check, type check, unit tests, migration check.
A PR that cannot pass CI is not an acceptance candidate.

## D2. ADR directory
§9 says contract changes require architecture review first. Those decisions
need a location: `docs/adr/NNNN-title.md` with context, decision, consequences.
This is also what prevents a re-litigated decision from quietly reversing
(e.g. the `max(target margin, minimum_sale_price)` rule §6 explicitly forbids).

## D3. Field-naming glossary
`최소판매가` and `minimum_sale_price` already coexist across the roadmap. Fix one
canonical name per concept in `docs/GLOSSARY.md`, with the Korean UI label
mapped to it. Contract drift starts as vocabulary drift.

## D4. Acceptance evidence format
§13 requires "2 consecutive complete PASS". Define what proves it: a committed
run report under `docs/acceptance/` containing the correlation IDs, the
marketplace product ID, the read-back payload, timestamps, and the fresh-session
condition. Chat is explicitly not the durable log (§1.2) — acceptance evidence
must obey the same rule.

## D5. Performance targets
Add numeric targets so "it works" is measurable: single-product collection
time, registration time, stock sync throughput per supplier, order sync
latency, and the bulk-registration rate ceiling implied by B7.

## D6. Document hygiene
- Heading levels break at §2 (`## 0`, `## 1`, then `# 2`), which fragments the
  outline.
- Add a version and change log to ROADMAP.md itself, since it is the contract
  everything else references.
- §12 uses M0–M6 while §3–§9 use Phase 0–7; add an explicit mapping table
  between the two numbering schemes.

---

# 6. Suggested patches to ROADMAP.md

| Section | Change |
| ------- | ------ |
| §0 Product goal | Extend the flow line with `→ FULFILL` (A1) |
| §1.2 Roles | Name `CLAUDE.md`, `docs/adr/`, `docs/acceptance/` as rule/decision/evidence locations (A7, C4, D2, D4) |
| §2 Architecture | Add cross-cutting layers: job runner, rate limiter, audit log, correlation ID (A3, B7, C4, C6) |
| §3 Phase 0 | Add stack decision, job runner, error taxonomy, CI, backup drill, correlation ID (A2, A3, B8, C5, C6, D1) |
| §4 Phase 1 | Add crawl policy and re-login loop guard (C2) |
| §5 Phase 2 | Add `currency`, image fetch + checksum, fingerprint, review queue, retention (A5, A6, B2, B6, B9, C3) |
| §6 Phase 3 | Add FX snapshot fields, VAT/fee/return-cost inputs, `account_id`, facts-revision rule (A5, B1, B5, B6) |
| §7 Phase 4 | Add `RegistrationAttempt` + reconcile rule, category mapping store, compliance gate, image upload (A4, A6, B3, B4) |
| §8 Phase 5 | Add 8.2b source-change detection and 8.5 fulfillment loop (A1, B2) |
| §9 Phase 6 | Note that bulk registration is gated on B7 and D5 |
| §10 Phase 7 | Add settlement as the source of actual (vs estimated) margin (B1) |
| §11 UI mapping | Add review-queue surfaces and a fulfillment view (A1, B9) |
| §12 Sequence | Insert M6.5 FULFILL before FIRST VERTICAL ACCEPTED; add Phase↔M mapping (A1, D6) |
| §13 DoD | Add execution-mode rule, evidence format, performance targets (C1, D4, D5) |

---

# 7. Open questions for the architect / user

1. **Fulfillment**: is 발주 automated at the supplier for the first vertical, or manual-with-record? (Recommendation: manual, but recorded canonically.)
2. **Deployment**: single-user local application, or a server the user connects to? This determines the secret storage boundary and the DB choice.
3. **Accounts**: one SmartStore account, or several now or later?
4. **Images**: rehost detail-page embedded images, or keep supplier URLs? (Recommendation: rehost.)
5. **FX source**: which rate provider, and what staleness threshold triggers price recomputation?
6. **Compliance scope**: is 건강기능식품 in scope for the first vertical, or deliberately excluded until B4 exists?
7. **Live writes**: which environment and which seller account is permitted to perform real marketplace CREATE during development?
8. **Retention**: what is the acceptable storage ceiling for evidence snapshots and images?

---

# 8. Change log

| Date | Author | Change |
| ---- | ------ | ------ |
| 2026-09-13 | Claude | Initial review of ROADMAP.md (2 commits, 682 lines). P0–P3 items A1–D6. |
