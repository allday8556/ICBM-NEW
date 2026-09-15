# ADR-0010 — Supplier-generic COLLECT and ProductFactsRevision source truth (M3)

Status: **PROPOSED** 2026-09-15 — contract PR-A of Issue #52. It becomes ACCEPTED only after the independent Claude cross-audit, the architect's review and the user's merge approval. The status line is then changed in this PR before merge (ADR-0008 precedent).
Decision owner: Architect (ChatGPT). Sources: Issue #52 body; architect addendum after the independent cross-audit `5672341510`; architect clarification on Coupang representative images `5672418057`.
Recorded by: Claude Code. The number was confirmed free in `docs/adr/` and in every open PR immediately before writing.
Date: 2026-09-15
Related: ADR-0004 (automation guardrails), ADR-0005 (job states; only TRANSIENT/RATE_LIMITED retry), ADR-0006 (one owner per data directory), ADR-0007 (supplier-generic CONNECT — this ADR is its COLLECT counterpart and changes nothing in it), ADR-0008 (error taxonomy)

---

## Context

M0, M1 and M2 are accepted. M3 is the first milestone that reads supplier *products*. It must prove **source truth independently of AI**: one real KM통상 product, read without supplier writes, becomes immutable source facts. The same stable identity is resolved across repeated collection and a restart.

Two constraints shape the design:

- the CONNECT boundary of ADR-0007 must stay intact, so a supplier package keeps holding site knowledge only;
- COLLECT must not create the canonical `Product`. That is M4.

`app/products/service.py` said that both `Product` and `ProductFactsRevision` arrive in M4. `ROADMAP.md` and `docs/ARCHITECTURE.md` say COLLECT creates `ProductFactsRevision` in M3. §1 resolves that conflict.

## Decision

### 1. Milestone boundary

```text
M3 = source-truth ProductFactsRevision persistence (+ collection observations, evidence, source assets)
M4 = canonical Product identity + current accepted revision link + enrichment/pricing/readiness
```

- M3 creates **no** canonical `Product` table, identity or link.
- M3 persists what COLLECT owns (`docs/ARCHITECTURE.md` §4):
  - immutable `ProductFactsRevision` records;
  - their product-scoped evidence;
  - their source-asset references;
  - the collection run that produced them.
- M4 later creates `Product` and chooses or links the current accepted revision.

### 2. Scope of M3

- **One operator-supplied KM통상 product URL at a time.**
- No category crawl, pagination crawl, bulk discovery or related-product traversal.
- Pipeline:

  ```text
  operator product URL
  → validate supplier/URL scope
  → ensure the KM통상 CONNECT session is usable
  → authenticated product-detail read
  → extract product-scoped evidence
  → resolve a stable source_product_id
  → normalize source facts without enrichment
  → fetch/checksum source images under the COLLECT policy
  → append an immutable ProductFactsRevision
  → DB/API read-back
  ```

- **Not owned by M3:**
  - canonical `Product`;
  - AI title/tag/category/option enrichment;
  - pricing calculation;
  - SmartStore registration/readiness;
  - marketplace calls;
  - supplier ordering or purchasing;
  - UI foundation redesign (#48 is M4);
  - broad collector learning or auto-selector training.

### 3. Port and adapter boundary

COLLECT gets its **own** supplier-generic port, separate from the CONNECT proof port:

```text
CollectService / durable collect.* job                 app/collect/
        ↓
SupplierCollectionGateway                              common policy/transport/session owner
        ↓
<supplier> collection definition + parser              pure site knowledge (integrations/suppliers/<key>/)
        ↓
ProductFactsRevision persistence                       app/collect/
```

- **The common gateway owns everything that acts:**
  - authenticated session use, through the accepted M1 connection owner and its bounded recovery;
  - host and path allowlisting;
  - request pacing and timeouts;
  - HTTP or browser execution, when the collection profile approves it;
  - request attribution and audit;
  - the request budget (§4);
  - source-image fetch and checksum (§9).

  It lives in the common supplier transport (`integrations/suppliers/transport/`). Its raw-client and egress-grant ownership is added to the repository rules as a reviewed entry, in the PR that introduces it.
- **The supplier contributes only immutable site knowledge:**
  - a collection profile (URL scope, approved transport, collection hosts, limits);
  - pure, deterministic parser functions over an immutable document view.

  The document view is status, final path, content type and body. `body` is used in memory only; it is never logged, persisted whole or returned. This mirrors `ProbeResponse` in ADR-0007.
- **A supplier package receives no** `httpx` client, Playwright page or context, logger, DB session, egress handle or callback. The existing rule `test_supplier_packages_hold_site_knowledge_only` keeps applying to every module the collection definition adds.
- **The CONNECT `SupplierGateway` is not widened.** Its `fetch()` stays "read `definition.probe.target`" and its `login()` stays one form submission. COLLECT never passes a URL through it. A repository rule pins that port's shape (`test_connect_gateway_port_is_not_widened_for_collect`).
- **No redirect is followed automatically** (`test_no_production_client_follows_redirects`). A redirect is evidence:
  - to the login page → `AUTH`;
  - to anything outside the approved product path → `VALIDATION`, with no revision.
- **The operator URL is validated before any request:**
  - `https`;
  - host equal to the supplier's storefront host;
  - path matching the product-path form proven by reconnaissance (§5);
  - no credentials or fragments;
  - query parameters only as the profile allows.

  A rejected URL makes zero supplier requests.

### 4. Request policy and budgets (addendum A)

- **Operator-supplied one-product scope only** (§2).
- **Pacing:** the supplier `RequestPolicy` applies unchanged. For KM통상 that is one request at a time, at least 2.0 s between request starts, and a 20 s timeout.
- **Same-product interval:** there is also a **minimum interval between real reads of the same `source_product_id`**. The collection profile fixes it; proposal in Q2.
- **Bounded real-provider request budget.** Separate budgets cover reconnaissance, debugging and acceptance:
  - they are counted **durably before send** (the M2 ledger precedent);
  - they are refused with zero bytes sent once exhausted;
  - job retries count against the same budget.
- **Caps frozen before the real campaign.** The product-read and image-read caps are frozen in `docs/acceptance/M3.md` and are never left open-ended.
- **No-default rule (M2 precedent).** A missing cap or limit is a refusal before any network I/O, never a code default.
- **Supplier policy review.** `robots.txt`, the terms of use and any automation policy are reviewed during reconnaissance. The findings are recorded in the sanitized reconnaissance record.
- **No bypass of an explicit technical or contractual restriction.** This covers:
  - no CAPTCHA or bot-check solving;
  - no rate-limit evasion;
  - no reuse of another account;
  - no access outside the member's own entitlement.
- **No hidden parser-debug loop.** Parser development runs on sanitized fixtures. Real reads happen only through the budgeted gateway, only in an approved step.

### 5. Source reconnaissance before freezing selectors and hosts

Before the final KM통상 parser and profile are frozen (PR-C), one bounded reconnaissance runs against the chosen real test product.

**Conditions:**
- the user's explicit go-ahead for the product and the budget (Q1);
- the dedicated acceptance data directory;
- the same budgeted gateway rules.

**Recorded in GitHub, sanitized only:**
- the observed canonical product URL/path form;
- the stable product-identity evidence, and the exact rule used for `source_product_id`;
- whether authenticated HTTP HTML is sufficient or browser rendering is actually required;
- where each fact's evidence lives: price, shipping, minimum sale price, options, stock, images and notice facts;
- the exact image/CDN hosts that must be allowlisted;
- every field that exists only as image evidence;
- the `robots.txt`, terms and automation-policy review (§4).

**Rules:**
- legacy ICBM-PROJECT collector code is not a source of truth, and is not inspected (CLAUDE.md §2);
- product name or content hash is **never** an acceptable fallback identity;
- if a stable source identity cannot be proven, stop for architecture review. Never invent one;
- no generic wildcard egress host to make assets "just work".

### 6. ProductFactsRevision contract

`ProductFactsRevision` is **append-only source truth**.

- **Every successful collection creates a new immutable revision, even when the page is unchanged.**
- Historical revisions, their evidence and their image references are never updated in place. The database enforces this, as it does for `audit_events`.

**Minimum identity and provenance:**

```text
revision_id
supplier_key            (kmretail)
source_product_id
source_url
captured_at
currency                (KRW)
extractor_revision      semantic extraction identity (§12)
extractor_fingerprint   implementation identity (§12)
source_fingerprint
field_fingerprints
collection_run_id / correlation_id
facts_status            CONFIRMED | REVIEW_REQUIRED
```

**Repeated unchanged collection:**

```text
revision 1 ─ source_product_id X ─ fingerprint F
revision 2 ─ source_product_id X ─ fingerprint F
restart
revision 3 ─ source_product_id X ─ fingerprint F
```

**Identity:**
- The stable identity is `(supplier_key, source_product_id)`.
- A changed name, price or image never manufactures a new source identity.
- An unresolved or contradictory identity produces **no revision** (§11).

**Fingerprints** are deterministic and content-only:
- `field_fingerprint` = SHA-256 over a canonical serialization of `(field_key, status, normalized value, ordered evidence digests)`.
- `source_fingerprint` = SHA-256 over the ordered `(field_key, field_fingerprint)` pairs and the ordered image references `(role, order, sha256)`.
- Neither includes `captured_at`, run or correlation IDs, session material or page-volatile tokens. An unchanged source therefore yields equal fingerprints.
- Fingerprints are compared as drift evidence only between revisions with the same `extractor_revision`. Across an extractor change, the change of extractor explains a changed fingerprint.

**Committed evidence** never carries plain digests of business values, such as a SHA-256 of a wholesale price, which would be trivially reversible. Repository evidence uses counts, presence and statuses, plus keyed per-campaign fingerprints (the M2 HMAC precedent). The raw values stay in the local database.

### 7. Facts: two levels (addendum D)

`ROADMAP.md` §5 and `docs/ARCHITECTURE.md` §5 define the COLLECT source-truth contract. M3 keeps all of it in the schema and the parser/evidence model, and splits only what **acceptance** requires.

| Level | Facts | Acceptance |
| --- | --- | --- |
| **Core** | stable source identity + URL; original name; price facts; options / atomic configuration facts; source images; stock evidence | must be proven on the real product |
| **Source coverage** | shipping policy + fee; `minimum_sale_price`; brand; manufacturer; origin; product-information notice facts/evidence; other product-scoped detail facts of the canonical contract | each must report `CONFIRMED`, `ABSENT` or `REVIEW_REQUIRED`; a legitimately missing field or image-only notice does not fail M3 |

No OCR or AI may turn a missing or ambiguous source field into a confident fact.

**Price:**
- Money is integer whole KRW. There is no float money.
- The source label and evidence used for each source price are preserved.
- `minimum_sale_price` is **only** an explicitly observed supplier value. When it is not present, it is stored as `null` with `ABSENT` evidence and is never derived.
- Quantity tiers are the original `(quantity, total_price)` pairs. A unit price is never produced by dividing, and `minimum_sale_price × quantity` is never manufactured.

**Shipping:**
- Both the structured interpretation and the exact source policy evidence are kept.
- A conditional or unknown policy is never flattened into a guessed fixed fee.

**Options / atomic configurations:**
- Source option axes and value labels are kept in source order, with every count, grade and weight distinction. Nothing is flattened because weights match.
- An explicit supplier SKU ID is nullable. If the site exposes none, none is fabricated.
- ICBM may derive a deterministic atomic configuration key from `source_product_id` and the exact ordered source selections. It is labelled as ICBM-derived and never presented as a supplier SKU ID.
- Option-level sold-out evidence is kept wherever it is observable.
- A product whose page proves it has no option control has options `CONFIRMED` with zero axes, not `ABSENT`.

**Revision status:** the rule is proposed in Q3.

### 8. Evidence model

Evidence is separate from normalized facts, and **product-scoped**. The authenticated page can hold member data, so it is never persisted blindly.

Each evidence entry holds:

```text
field_key
source_kind       DOM_TEXT | ATTRIBUTE | EMBEDDED_JSON | JSON_LD | CONTROL_STATE | URL | PRODUCT_HTML_FRAGMENT | IMAGE
locator           stable locator / semantic selector
observed          product-scoped value or sanitized fragment (bounded size)
normalized        normalization result
digest            evidence digest (feeds the field fingerprint)
status            CONFIRMED | ABSENT | REVIEW_REQUIRED
```

**Forbidden** in evidence, audit and logs:
- cookies, authorization headers and session values;
- member IDs;
- unrelated account or profile data;
- page-wide private HTML.

If product-information notice facts exist **only inside an image**:
- the image evidence and its checksum are stored;
- the text facts are `REVIEW_REQUIRED`;
- **no OCR or AI fills them in during M3.**

### 9. Source images (addendum B and clarification `5672418057`)

**Pipeline:**

```text
source image reference
→ approved-host fetch (collection gateway)
→ validate status / content type / size bounds
→ SHA-256 checksum
→ content-addressed local source asset under the data directory
→ ProductFactsRevision image reference preserving role and order
```

**Metadata kept for every source asset or reference:**
- role (representative or detail) and source order;
- source URL and its evidence;
- original width and height;
- MIME type, verified against the bytes rather than trusted from the header;
- byte size;
- SHA-256.

**Handling rules:**
- The original bytes are preserved exactly. M3 **never overwrites, resizes, re-encodes or watermarks** a source asset, and never uploads or publishes one.
- Storage may dedupe by checksum, but every ordered reference and role is kept.
- A collected supplier URL never becomes a published image contract; `docs/ARCHITECTURE.md` §10 forbids hotlinks.
- There is no `data:`, `blob:` or `file:` URL escape and no arbitrary third-party fetch.

**Hosts:** asset hosts are the ones reconnaissance justifies, allowlisted explicitly. There is no wildcard.

**Limits, frozen after reconnaissance.** A missing value means refusal:
- maximum image references per revision;
- maximum bytes per image;
- maximum total new image bytes and image requests per collection run;
- the explicit asset-host allowlist.

**Reuse between revisions:**
- The same URL is **not** evidence of the same bytes.
- `ETag` / `Last-Modified` validators are used when the origin provides them. A validated `304 Not Modified` may reuse the existing content-addressed blob and checksum for the new revision's ordered reference.
- Without a trustworthy validator or supplier-specific immutability proof, the image is refetched under the budget and the checksum recomputed.
- Checksum equality is never claimed without observing or revalidating the bytes.

**Failures:**
- A bad host, content type, oversize response or budget exhaustion fails closed for that image, with no partial blob.
- The image facts become `REVIEW_REQUIRED`.
- The host allowlist is never widened at run time.

**Marketplace image rules are out of M3.** Derived marketplace variants belong to the M4 image-pipeline foundation and to each marketplace adapter/readiness contract. Those record the source checksum, the transformation spec or policy version, and the output checksum, and they never mutate the source asset.
- Coupang's `REPRESENTATION` image is square, JPG or PNG, 500×500 to 5000×5000 px and at most 3 MB. That is a provider-documented rule for the future Coupang adapter, not for M3 and not for SmartStore.
- For SmartStore, 1000×1000 is recommended. A 500×500 minimum is not treated as a SmartStore invariant until the current official rule or a measured provider error verifies it.
- Cropping is never a default fallback.
- Dimension compliance is never presented as source quality.

### 10. Stock / sold-out

```text
BUY/CART active                     → ON_SALE
SOLD OUT + no active purchase path → SOLD_OUT
mixed / insufficient evidence      → REVIEW_REQUIRED
```

- Hidden DOM, review badges, description text, stale badges or disabled/irrelevant elements alone are not authoritative.
- The deciding control evidence is recorded.
- Availability is never proven by adding to cart or by any other supplier write.

### 11. Jobs, retry and audit

- Collection runs through the durable Job owner as `collect.*` jobs (ADR-0005).
- **Only `TRANSIENT` and `RATE_LIMITED` retry automatically.** Every retry is a real read and counts against the budget (§4).
- **`AUTH`** delegates to the bounded M1 connection recovery: `SingleFlightAuth`, one authentication per operation, and the loop guard. COLLECT has **no login loop of its own**.
- **`VALIDATION`, `REVIEW_REQUIRED` and parser ambiguity** are never blindly retried.
- **An unresolved stable source identity produces no ProductFactsRevision.** The run ends with `COLLECT_REVIEW_REQUIRED` and a `COLLECT_EVIDENCE` review item.
- **Field-level ambiguity** may produce a revision whose `facts_status` is `REVIEW_REQUIRED`. Process completion (the job) and facts readiness (the revision) are separate concepts.
- **Audit family.** Names may be normalized during implementation, but they cover at least: `COLLECT_REQUESTED`, `COLLECT_STARTED`, `COLLECT_SOURCE_FETCHED`, `PRODUCT_FACTS_REVISION_CREATED`, `COLLECT_REVIEW_REQUIRED` and `COLLECT_FAILED`.
- **Audit and log payloads** come only from the allowlist builder `app.core.safe_payload`. They never contain full HTML, cookies, auth or session values, or private account data.

### 12. Extraction identity (addendum C)

A manual string with no guard is insufficient. The **supplier collection definition** owns its extraction identity, and every revision persists both parts of it.

- **`extractor_revision`: the semantic identity**, for example `kmretail-collect-r1`.
  - It covers selectors, identity rules, normalization, stock judgment and field interpretation, including the profile's hosts and limits.
  - **Any semantic change advances it in the same PR.**
- **`extractor_fingerprint`: the implementation identity.**
  - It is SHA-256 over the LF-normalized source of the definition, profile and parser modules, in a fixed file order.
  - It is pinned next to `extractor_revision`, so the exact extractor that produced a revision can be reproduced from the repository.

Mechanical guards (implemented with the parser, PR-C):

1. **Pin check.** A repository test recomputes the fingerprint. Any edit to a pinned file fails until the pin is updated, so no change goes unnoticed.
2. **Golden outputs per revision.** For every sanitized fixture, the expected normalized facts, statuses and fingerprints are recorded under the `extractor_revision` that produces them. A semantic change alters an output, which fails the test until the revision advances and new goldens are recorded. A semantic change without an identity change is therefore a failing test for every covered behaviour.
3. **Clean-tree campaigns.** The acceptance harness runs only at an exact commit with a clean tree (the M2 P0 precedent), so a local uncommitted edit cannot produce accepted revisions under a stale pin.

### 13. AI, OCR and marketplace isolation — hard M3 gate

M3 acceptance proves:

```text
AI provider calls           = 0
OCR / vision inference calls = 0
SmartStore/marketplace calls = 0
supplier writes             = 0
```

- COLLECT and ProductFactsRevision work with every AI capability disabled or unavailable.
- Issue #8 starts only after M3 is accepted.
- A repository rule, added in this PR, forbids the source-truth path (`app/collect/`, `integrations/suppliers/`) from importing:
  - AI-provider, OCR or vision libraries;
  - any `ai` package;
  - marketplace integrations and the marketplace CONNECT code.

### 14. Minimal application contract

A typed application/service contract to:

1. request one KM통상 product collection by URL;
2. return a durable job ID and correlation ID;
3. read back the collection result, with revision ID and status;
4. read back a ProductFactsRevision with its evidence and image references.

- No M4 UI foundations.
- The existing Collection Management shell may expose only the minimum wiring the accepted contract needs; otherwise acceptance exercises the API/service directly.
- No broad CSS, token or layout cleanup in M3.

### 15. Acceptance outline

`docs/acceptance/M3.md` and its harness arrive with PR-D. They are dry/fake by default, and **CI never contacts KM통상**: the live transport factory refuses under CI or pytest, as in M2.

- Before the **first real product read**, STOP and obtain the user's explicit go-ahead for the chosen product and scope.
- The recommended real sequence runs on a dedicated acceptance data directory:
  - **P0:** exact main SHA, clean tree, CI green.
  - **P1:** M0 regression 24/24, plus the M1/M2 contract regressions.
  - **P2:** AI/OCR/marketplace egress hard-zero preflight.
  - **P3:** KM통상 CONNECT proof / session usable.
  - **C1, C2, restart, C3:** revisions R1, R2 and R3.
  - Then DB/API read-back with source-asset checksum verification, and a secret/PII/evidence scan.
- The assertions are those of Issue #52 §12.
- Closeout evidence is sanitized (§6): repository evidence is not a business-data dump.

### 16. Tests and negative controls

The tests cover at least the Issue #52 §13 list. Fixtures are synthetic or sanitized only; authenticated real KM통상 HTML containing member data is never committed. The list:

- identity missing or ambiguous → no revision;
- same identity with a changed product name → the same identity;
- repeated collection → a new immutable revision, with no historical mutation;
- one fact changed → field and source fingerprints change deterministically;
- `minimum_sale_price` absent → `null`, not derived;
- quantity-tier totals preserved exactly;
- atomic options keep count, grade and weight distinctions;
- an active BUY/CART beats irrelevant SOLD OUT text;
- SOLD OUT with no active purchase → `SOLD_OUT`;
- mixed stock evidence → `REVIEW_REQUIRED`;
- image-only notice → evidence kept, text fact `REVIEW_REQUIRED`, no OCR;
- image with a bad host, content type or size → fail closed / review;
- no forbidden endpoint or host escapes the collection gateway;
- CI and tests cannot construct the real supplier transport;
- no AI or marketplace transport on the M3 path;
- migration upgrade and downgrade, and the M0/M1/M2 regressions, stay green.

### 17. Delivery

| PR | Scope |
| --- | --- |
| **PR-A** (this) | contract, canonical status sync, this ADR, repository rules; docs and rules only, no provider call |
| **PR-B** | source-truth model and migrations: revision, evidence and source-asset persistence, repositories, fake tests |
| **PR-C** | supplier-generic collection gateway and the KM통상 deterministic parser/profile, frozen only after the reconnaissance findings are reviewed |
| **PR-D** | durable `collect.*` job, application/API read-back, acceptance harness and negative controls; still no CI real call |
| **Campaign + closeout** | only after the architect audit, the independent Claude cross-audit and the user's explicit go-ahead for the real scope |

No PR is merged without the user's explicit approval. A contract change and its implementation never share a PR.

## Open questions for the architect (proposals)

1. **Reconnaissance budget (§5).**
   - **Proposal:** product-detail reads ≤ 4; image requests ≤ 30; public policy reads (`robots.txt`, terms) ≤ 3; logins only under the M1 policy (≤ 1 expected, the session is reused).
   - The reads are counted durably before send, and the user's go-ahead comes before the first read.
2. **Same-product minimum interval (§4).**
   - **Proposal:** 60 s between real reads of the same `source_product_id`, on top of the 2.0 s supplier spacing. C1 → C2 → restart → C3 fits comfortably.
3. **`facts_status` aggregation (§7).**
   - **Proposal:** `CONFIRMED` iff every core field is `CONFIRMED` and no field anywhere is `REVIEW_REQUIRED`; otherwise `REVIEW_REQUIRED`.
   - `ABSENT` is allowed only for source-coverage fields. A core field that is `ABSENT` makes the revision `REVIEW_REQUIRED`.
4. **Image dimensions (§9).**
   - Reading width and height needs either an in-house header parser for the formats the reconnaissance observes, or a new pinned dependency (Pillow), which needs the user's approval under CLAUDE.md §4.
   - **Proposal:** an in-house header parser, where an unknown format means `REVIEW_REQUIRED`.
5. **Where the sanitized reconnaissance record lives (§5).**
   - **Proposal:** a comment on Issue #52 for the review, plus `docs/suppliers/kmretail/COLLECT_RECONNAISSANCE.md` committed with PR-C.
6. **Runtime `app.MILESTONE`.**
   - It still reads `"M1"`. It feeds `/api/health`, the screen `meta.milestone` and the UI footer, and `tests/integration/test_api.py` pins it.
   - This PR is docs and rules only, so it leaves the value alone.
   - **Proposal:** the value names the milestone under development. The first M3 code PR (PR-B) sets it to `"M3"`, and the active-milestone rule then also checks it.
7. **Evidence fragment bound (§8).**
   - **Proposal:** a sanitized `PRODUCT_HTML_FRAGMENT` is at most 4 KiB per entry, and larger evidence is referenced by digest only.

## Consequences

- A second supplier's collection is a new collection definition, not a core change, just as CONNECT works under ADR-0007.
- The source-truth path cannot silently depend on AI, OCR or a marketplace. Repository rules fail such an import.
- Every revision is reproducible: an unchanged source yields equal fingerprints, and an extractor change is distinguishable from source drift.
- The real supplier sees a bounded, paced, budgeted number of reads, and never an unattended debug loop.
- M4 inherits a stable, immutable source-truth history. It chooses the current accepted revision instead of repairing mutated facts.

## References

- Issue #52 and comments `5672341510`, `5672418057`
- `ROADMAP.md` §5, §12; `docs/ARCHITECTURE.md` §4, §5, §8, §10, §11
- ADR-0004, ADR-0005, ADR-0006, ADR-0007, ADR-0008
- `integrations/suppliers/base.py`, `integrations/suppliers/transport/`, `integrations/suppliers/kmretail/`, `app/collect/service.py`
- `tests/unit/test_repository_rules.py`
