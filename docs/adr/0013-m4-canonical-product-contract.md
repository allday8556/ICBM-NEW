# ADR-0013 — M4 canonical product contract: one product identity, the current source revision, composition and Item, context-scoped pricing, layered readiness and image lineage

Status: **ACCEPTED** 2026-09-18. This is PR-A of Issue #80 (PR #81).
- GPT re-audit PASS `5245310563` and the independent Claude AI cross-audit PASS `5727333233`, both on head `bf2b699`.
- The rulings and blockers of review `5245152210` are incorporated.
- It lands on main with the merge of PR #81.

It authorizes no schema, migration, runtime code, UI, AI call, supplier request or marketplace call. Each implementation PR (PR-B to PR-F) needs its own authorization.

Clarified for product-level quantity offers by Issue #80 ruling `5738760913` (PR-Q, kickoff `5738854211`): §2 and §6 only. See "Clarification" below.

Decision owner: Architect (ChatGPT). Sources:
- the Issue #80 body (the M4 umbrella) and the architect kickoff `5726182664`, which authorized PR-A only;
- the architect's PR #81 review `5245152210`, which ruled the five former choices for review and set two blockers (see "Rulings");
- `docs/architecture/CANONICAL-V3.1.md` §2, §5.2, §6, §7.3, §8, §9, §10.1, §11.5, §12 and §14 (the frozen v3.1 identity model and schema);
- `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md` B1, B6 and §8;
- Issue #56 (Image Studio, planned), whose extension points this foundation must keep open;
- Issue #30 and ADR-0012 for the AI boundary.

Recorded by: Claude Code. The number was confirmed free in `docs/adr/` and in every open PR immediately before writing.
Date: 2026-09-18
Related:
- ADR-0009: where Canonical v3.1 lives;
- ADR-0010: `ProductFactsRevision` source truth and source assets. This ADR changes nothing in it;
- ADR-0011: the M5 read-back retention boundary;
- ADR-0012: the AI runtime contract.

---

## Context

M3 is accepted. COLLECT persists immutable `ProductFactsRevision` records per `(supplier_key, source_product_id)`, their evidence, and content-addressed `SourceAsset` bytes. There is no canonical downstream product yet. M4 is CURRENT: canonical Product DB, image pipeline, and pricing and readiness foundations (Issue #80).

The canonical documents name the downstream product in two vocabularies:
- `ROADMAP.md` §6, `docs/ARCHITECTURE.md` §4–§5, CLAUDE.md §5.1 and ARCHITECT_REVIEW B6 speak of a **`Product`**, "the canonical ICBM product ID", that references the current accepted facts revision. `docs/ARCHITECTURE.md` §5 `MarketplaceRegistration` carries an `icbm_product_id`.
- Canonical v3.1 (frozen) speaks of a **`ProductGroup`**: the intrinsic, same-sellable identity across supplier members (§2.2). Its frozen schema (§14) lists `ProductGroup`, `GroupMember`, `GroupMembershipRevision` and `GroupChangeEvent`, and no separate `Product`. Its Item layer keys everything downstream on `current_group_id + composition_signature` (§2.4, §2.5).

Read side by side, these can suggest two canonical identities: a `Product` and a `ProductGroup`. Issue #80 §4 requires the relationship to be decided **before** any schema, with exactly one downstream canonical ID. This ADR decides it, and fixes the other boundaries PR-B to PR-F build on.

This ADR is a contract. It names entities and invariants. It does not fix tables, columns, migrations or algorithms except where it says so.

## Decision

### 1. One canonical product identity: the `Product` **is** the `ProductGroup`

**The decision.** The canonical ICBM product of ROADMAP, ARCHITECTURE, CLAUDE.md and ARCHITECT_REVIEW B6 **is** the `ProductGroup` of Canonical v3.1. They are two names for one entity, with one identifier.

| where it is written | what it is called there |
| --- | --- |
| ROADMAP, ARCHITECTURE, CLAUDE.md, UI (통합DB) | `Product`, "the canonical ICBM product ID", `icbm_product_id` |
| Canonical v3.1 | `ProductGroup`, `group_id`, `current_group_id`, `group_id_at_registration` |

- **The persisted entity is `ProductGroup`**, as the frozen v3.1 schema names it (ruling E). UI and prose may call it `Product`.
- **No second product entity exists.** There is no separate `Product` row, table or identifier above, below or beside it.
- **The downstream key.** Pricing, readiness, drafts, registration, orders, stock, inquiries and analytics resolve to that one identifier. Per sellable Item they also use the composition (§5).
- **`icbm_product_id` in `docs/ARCHITECTURE.md` §5 is this identifier.** Under the v3.1 Item layer, M5 records it per registration Item: `current_group_id` on the live item, and `group_id_at_registration` in the immutable snapshot.

**The five required properties (Issue #80 §4), and how they hold:**
1. Exactly one downstream canonical ID: the group identifier. Nothing downstream keys on a source product or a revision instead.
2. Source revisions stay immutable and separately addressable. `ProductFactsRevision` is untouched, and a group references source products, never by copying their facts (§2, §3).
3. A later cross-supplier merge or split rewrites no historical source truth. It changes membership and current pointers only, and records its lineage (§4).
4. Item identity stays `group identifier + composition_signature` (§5).
5. No second screen or service owns product truth. There is no parallel `Product` owner to drift from the group.

**Alternatives rejected:**
- *A `Product` row above a 1:1 `ProductGroup`.* Two identifiers for one thing. Every downstream reference would have to pick one, and they could diverge.
- *A `Product` per source product, grouped under a `ProductGroup`.* A downstream record would key either on the source-level product or on the group. A cross-supplier merge would then re-point pricing and registration. This is the "two competing canonical identities" Issue #80 forbids.
- *A `Product` equal to the source product, with no grouping.* This breaks the v3.1 cross-supplier model and makes "one supplier forever" an invariant.

**Stability.**
- A group identifier is created once, never reused and never rewritten.
- A membership change keeps the identifier and adds a `GroupMembershipRevision` (§4).
- A MERGE or SPLIT is an explicit, recorded `GroupChangeEvent` naming predecessors and successors:
  - immutable references, such as a later `group_id_at_registration`, keep their original identifier;
  - current references move to a successor;
  - an unclear successor is never chosen automatically. It is `REVIEW_REQUIRED` (v3.1 §6.5).
- A `ProductFamily`, if ever added, is a search and UI convenience only, never a registration identity (v3.1 §2.2).

### 2. The source side: `SourceProduct`, its revisions, and what M4 may read from them

- **`SourceProduct`** is the supplier's product, identified by `(supplier_key, source_product_id)`. This is the self-duplicate identity: at most one per pair (v3.1 §6.1).
- **Its facts are its `ProductFactsRevision` history**, exactly as ADR-0010 defines it: immutable, sequenced per identity, fingerprinted. M4 never updates, deletes or re-derives a revision.
- **`SourceSKU` and `QuantityOffer`** are source facts. M4 reads them from a revision only where the source states them (v3.1 §5.2).
  - Quantity tiers stay original `(quantity, total_price)` facts. They are never flattened into a unit price or multiplied into new totals.
  - Atomic source SKU identity is preserved. The same weight never merges a different count, grade or pack.
  - **Product-level offers (ruling `5738760913`, PR-Q).** A revision may state `options = ABSENT` and `quantity_tiers = CONFIRMED`. Then each confirmed tier is one immutable, revision-scoped `QuantityOffer` of the source product itself, with its exact original total.
    - `options = ABSENT` proves that no source SKU or configuration was stated, so such an offer has **no** `SourceSKU` reference.
    - No synthetic "base SKU" is ever created to give it one.
    - A `SourceSKU` reference is required only for a genuinely SKU-scoped offer, one tied to a CONFIRMED atomic source configuration. PR-Q materializes none.
    - This reads what a revision states. It widens no COLLECT capability.
- **The M3 capability boundary carries forward unchanged** (`docs/acceptance/M3.md` §2): positive `CONFIRMED` option-axis/configuration support and positive quantity-tier values are not accepted.
- **ABSENT is never turned into a source entity.** A revision that validly reads `options = ABSENT` and `quantity_tiers = ABSENT` states no SKU and no tier. M4 creates and persists **no** `SourceSKU`, supplier SKU identifier or `QuantityOffer` for it. The sellable unit such a product needs is represented on the product side only: a default single-unit composition and Item (§5) with a base-product binding (§6). Missing capability is not a licence to guess (M3.md §2.4).

### 3. The current source revision is a pointer, never an edit

**What it is, and what it is not.**
- The pointer names **the source revision downstream uses now**: the `current_source_revision` of a `SourceProduct`.
- It is **not** an "accepted" or "confirmed" revision, and nothing may call it that: a current revision may be `REVIEW_REQUIRED`.
- Any later persisted name keeps this distinction. Its fact ambiguity is carried by readiness (§8), never by the pointer.
- The phrase "current accepted facts revision" in `docs/ARCHITECTURE.md` (before this ADR), in ARCHITECT_REVIEW B6 and in ADR-0010 §1 names this pointer.

**Where the pointer lives.** It is kept **per `SourceProduct`**.
- A group has no facts revision of its own in M4, so downstream reads a member's facts through that member's current source revision.
- v3.1 §10.1 reserves a separately named `group_facts_revision_*` for any future group-level facts.

**How it advances (ruling A).** It advances **automatically** to the newest eligible revision, with no operator confirmation per collection. A revision is eligible only when all of these hold:
- it belongs to the **same stable source identity** `(supplier_key, source_product_id)`;
- it is durably **`RECORDED`**, meaning its collection run's outcome is `RECORDED`;
- its fingerprints are structurally **intact**.

An unresolved identity never advances the pointer: such a run records no revision at all (ADR-0010).

**Invariants:**
- Advancing the pointer **does not** change the revision's `facts_status`, any field status or any evidence.
- A `REVIEW_REQUIRED` revision may be current. No operator acceptance is required merely because of that status.
- A recollection creates a new revision (ADR-0010). It never updates a historical one.
- The pointer never moves to an older revision silently. Moving it backwards is an explicit, recorded decision.
- The history is append-only. Each move records:
  - the revision it points to and the one it replaced;
  - the rule and rule version, or the actor, that moved it;
  - the reason, the time and the correlation.

  Each move is audited (`AuditEvent`).

> **Amendment note (ADR-0017 §5.3, §5.4, §7.4).** Read "the same `extractor_revision`" below as
> "equal `comparability_key`". For every revision without profile provenance the key is
> `("CODE", extractor_revision)`, so every existing revision compares exactly as before. An
> `ExtractionProfileRevision` becoming `ACTIVE` moves no pointer; only a later recorded eligible
> revision does, and that move is `EXTRACTOR_CHANGED`.

**Source drift vs extractor change (ADR-0010 §6).** Fingerprints are drift evidence only between revisions with the **same `extractor_revision`**.
- **Same extractor.** When the pointer moves, the old and new current revisions are compared. The difference is classified by the source-drift rules (`docs/ARCHITECTURE.md` §11). Every derived result whose dependency fingerprint includes the old revision becomes `STALE` (§8). User-locked values are kept, with a `SOURCE_DRIFT` mark, and are not overwritten (v3.1 §7.3, §7.7).
- **Different extractor.**
  - A fingerprint difference or equality proves **neither** drift nor its absence.
  - The move is recorded with the explicit reason `EXTRACTOR_CHANGED`, and every dependent derived result is invalidated for re-evaluation under that reason. It is never treated as ordinary source drift.
  - A drift classification is not inferred across the change.

### 4. Group membership and its revisions

**`GroupMember`** links a group to a `SourceProduct`, never to one revision. It carries:
- a status: `CANDIDATE`, `CONFIRMED` or `REJECTED`;
- `match_method`, `match_confidence` and `match_strategy_version`;
- `decided_by`, and when it was created and confirmed.

A `SourceProduct` is a `CONFIRMED` member of **at most one** group at a time.

**`GroupMembershipRevision`** is an immutable, numbered revision of a group's confirmed member set.
- A new one is created on every change to that set.
- Derived state names it in its dependency fingerprint, and a later `RegistrationItemSnapshot` names it as `group_membership_revision_id` (v3.1 §2.5).
- A move of a member's current source revision is **not** a membership change and creates no membership revision.

**Materialization in M4.** A `SourceProduct` that has a current source revision but no confirmed group gets a new group with that one member. The first vertical has one supplier. Nothing in the contract makes one member per group an invariant.

**Matching (v3.1 §6.2–§6.4):**
- **Auto-confirmation** happens only on a valid GTIN, or on manufacturer (brand) + MPN, with no conflict in capacity, specification or manufacturer pack. Such a conflict is a **rejection**, not a weak signal.
- **Candidates only:** pHash, normalized-name similarity, brand, model name and option structure produce `CANDIDATE`s and a review item. They never merge.
- **Reversibility:** a merge must be reversible; a rejected candidate returns to an independent group.
- **Not on the group:** it carries no `primary_source_id` (v3.1 §12.1), no `allow_duplicate` and no marketplace duplicate policy. Intentional duplicates are a marketplace/account-scoped `DuplicateOverride` in M5 (v3.1 §9.4).

**MERGE and SPLIT.**
- Each one records a `GroupChangeEvent` with its type, predecessor identifiers, successor identifiers, the membership revision, who decided it and when (v3.1 §6.8).
- It follows the Item-level rules of v3.1 §6.5–§6.7: no automatic deletion or deactivation, and the survivor is chosen by the operator.
- M4 does not have to automate cross-supplier merging, but any merge or split it performs obeys this section.

### 5. `ListingComposition` and the product-side `Item`

**`ListingComposition`** is the seller-chosen sellable multiplicity. It is not a supplier quantity tier and not intrinsic product identity (v3.1 §2.3).

```text
ListingComposition        immutable
  composition_id
  quantity
  unit_amount / unit_code
  pack_count
  units_per_pack
  total_amount
  composition_signature
```

**`composition_signature`:**
- It is the hash of a **canonical normalized structure**, never display text: `2x500ml` and `500ml 2개` are one signature.
- Its normalization is deterministic and versioned. The signature algorithm version is recorded.
- It is unique among compositions: two compositions with one signature never coexist.
- Count, grade and manufacturer-pack differences stay distinct signatures even at equal total weight.

**Immutability.** A composition is never updated. A different structure is a new composition, so a past snapshot's meaning never changes.

**Source facts stay source facts.** A `QuantityOffer` is never rewritten as a composition, and a composition never writes a source fact. Which offer fulfils which composition is a binding (§6).

**The default single-unit composition (ruling B).** Some products validly prove "no options" and "no quantity tiers": their current source revision reads `options = ABSENT` and `quantity_tiers = ABSENT`, as M3.md §2.1 records for the product of the accepted M3 campaign.
- For such a product M4 may materialize a **product-side default single-unit composition**: quantity 1 of the base product as the source sells it. It also materializes the default Item on it.
- Unit fields no source fact states stay **unknown**, and "unknown" is part of the canonical signature. No capacity is guessed.
- This is a product-side representation only. It creates **no** `SourceSKU`, supplier SKU identifier or `QuantityOffer`.
- If option- or tier-looking evidence is `REVIEW_REQUIRED`, **no default unit is inferred**. The affected Item and its pricing stay `REVIEW_REQUIRED`.

**The product-side `Item`:**
- It is the sellable unit: `group identifier + composition_signature`. That pair is its identity and it is unique (v3.1 §2.4).
- M4 creates this product-side foundation. Pricing snapshots and readiness attach to it.
- M5 drafts and registrations refer to it: `DraftListingItem`, `RegistrationItemSnapshot`, `MarketplaceRegistrationItem`.
- **After a MERGE**, two Items can come to share one key. That is a `DUPLICATE` conflict for the operator to resolve (v3.1 §6.6–§6.7), never an automatic deletion.
- **The Draft invariant stays M5's to enforce:** one draft never holds two Items with one key (v3.1 §3.1, §8.1).

### 6. The current source binding and the historical source snapshot are different owners

**`SourceBinding`** is where an Item is **currently** procured (v3.1 §12.1):

```text
SourceBinding
  source_binding_id
  group_member_id
  binding_kind             SOURCE_OFFER | BASE_PRODUCT
  source_sku_id            a SKU-scoped SOURCE_OFFER only; none for a product-level offer or BASE_PRODUCT
  quantity_offer_id        every SOURCE_OFFER, exactly one; none for BASE_PRODUCT
  fulfillment_quantity
  provenance               which source revision and which fields justify the binding
  valid_from / valid_to
```

**The two binding kinds:**
- **`SOURCE_OFFER`** always binds one exact, source-stated `QuantityOffer` (v3.1 §12.1).
  - Its `fulfillment_quantity` is that offer's quantity.
  - It names the offer's `SourceSKU` only when the offer is SKU-scoped. A product-level offer has none (ruling `5738760913`).
- **`BASE_PRODUCT`** binds the source product itself, when its current source revision validly states no options and no tiers (§2, §5). It references no SKU or offer identity, because none exists. Its provenance names the revision and the explicit base product price, `shipping` and `minimum_sale_price` fields it relies on.

A `BASE_PRODUCT` binding fulfils only the default single-unit composition with `fulfillment_quantity = 1`. Anything more would be composed fulfillment, which is off by default (below).

**What M4 owns:** `SourceBinding` records, and the product-side Item's current binding that Pricing reads.

**How a binding changes:**
- A binding changes by closing the current validity window and opening a new one. It is append-only and audited.
- A binding change is operational state, not a facts change (v3.1 §7.3). It stales the dependent pricing and readiness (§7, §8).

**What stays out of M4:**
- **Automatic substitution** of the source after a stock or price change belongs to OPERATE (v3.1 §12). It passes the `SourceCompatibilityGate` in order (v3.1 §12.2):
  - the composition can be fulfilled;
  - origin, components, quantity, expiry policy and brand labelling match;
  - cost, shipping, `minimum_sale_price` and margin work;
  - the source is sellable and in stock.

  `allow_composed_fulfillment = false` is the default, and a broken margin is `REVIEW_REQUIRED`, never an automatic switch.
- **The historical source snapshot** (`RegistrationItemSnapshot.source_snapshot`) is an immutable **copy** taken at registration and owned by M5. It never references a mutable binding in place of a copy.
- **How a `MarketplaceRegistrationItem.current_source_binding_id` is chosen** is M5's decision.

### 7. `PricingSnapshot`: one owner, one rule, immutable, per Item **and pricing context**

**Only the Pricing owner calculates a selling price.** No UI, no adapter and no other service re-decides one.

**Price varies by context, not only by Item (blocker 1).**
- v3.1 §8's "the price belongs to the Item" means that price is never flattened above the Item/SKU layer.
- It does **not** mean one global price per Item. The platform fee, and with it the target-margin price, the expected margin and the guard outcome, differ by marketplace, by account and by policy.
- One Item can hold a SmartStore, a Coupang and an 11st price at the same time, each with its own margin and guard.

**The pricing context.** It is an explicit part of every snapshot:

```text
PricingContext
  marketplace_key          the target the price is for
  account_id               when the fee or policy can differ by account; otherwise explicitly none
  fee_table_version        the versioned platform-fee inputs
  pricing_policy_version   target margin, guards, rounding, other policy costs
```

**What a snapshot is:**
- It is **immutable**, and it belongs to **one Item under one pricing context**: the price is an Item attribute, never a listing attribute.
- It is computed from four things:
  - one current binding;
  - the bound source's current source revision;
  - that context's fee table;
  - that context's pricing policy version.
- Any change of input produces a **new** snapshot. A snapshot is never updated.
- **Many snapshots per Item.** One Item can have many current and historical snapshots, at most one current per `(Item, pricing context)`.
- **No universal price is assumed.** No SmartStore assumption is built into the Product DB.
- **A platform fee always has its context.** No snapshot carries a platform fee without its pricing context.
- A later `RegistrationItemSnapshot.pricing_snapshot_id_at_registration` references the exact **context-specific** snapshot used.

At contract level it records:

```text
PricingSnapshot            immutable
  pricing_snapshot_id
  item key                 (group identifier + composition_signature)
  pricing_context          marketplace_key, account_id (or none), fee_table_version, pricing_policy_version
  source_binding_id
  source_product_facts_revision_id
  purchase_cost            SOURCE_OFFER: the bound QuantityOffer's own total;
                           BASE_PRODUCT: the revision's explicit base product price; never a derived unit price
  supplier_shipping
  platform_fee             from the context's fee table
  minimum_sale_price       exactly as the source states it for the bound offer or base product, or none
  target_margin_price
  final_sale_price
  price_basis              MINIMUM_SALE_PRICE | TARGET_MARGIN
  expected_net_margin
  price_guard              OK | LOSS | BELOW_MIN_MARGIN
  fx_rate / fx_source / fx_captured_at    non-KRW sources only
  input fingerprint
```

**The canonical rule** (CLAUDE.md §6.1, `docs/ARCHITECTURE.md` §6):

```text
if minimum_sale_price exists:
    final_sale_price = minimum_sale_price
    price_basis = MINIMUM_SALE_PRICE
else:
    final_sale_price = target_margin_price
    price_basis = TARGET_MARGIN
```

Never restore `max(target_margin_price, minimum_sale_price)`.

**The registration guard (Issue #80 §7).** These are policy inputs, versioned by the context's `pricing_policy_version`, and never source facts. Each is evaluated per snapshot, so per context:
- **Unrestricted price.** When no `minimum_sale_price` applies, `target_margin_price` is set for a target net margin of **35%**.
- **Loss guard.** If `purchase_cost + supplier_shipping + platform_fee >= final_sale_price`, then `price_guard = LOSS`: not registerable.
- **Margin guard.** If `expected_net_margin < 10%`, then `price_guard = BELOW_MIN_MARGIN`: not registerable.
- **Both guards also apply under `MINIMUM_SALE_PRICE`.** A supplier's minimum can itself be a loss.

**How the margin is computed:**

```text
expected_net_margin = (final_sale_price − purchase_cost − supplier_shipping − platform_fee − other policy costs)
                      / final_sale_price
```

"Other policy costs" are the ones `docs/ARCHITECTURE.md` §6 lists: advertising, payment and settlement fees, VAT policy, coupon burden, returns. Each enters only when the policy version defines it.

**Not decided here:**
- the rounding of `target_margin_price` in whole KRW;
- the platform fee tables.

Both are policy-version content decided in PR-D, with deterministic tests.

**What the snapshot holds, and what it never does:**
- It holds an **estimated** margin. The actual settled margin is a different state, owned by OPERATE (ARCHITECT_REVIEW B1).
- No `minimum_sale_price × quantity` and no per-unit source price is ever manufactured.
- **Ruling C.** Sometimes the source states a minimum for one unit but none for the bound quantity of a multi-unit composition. Then nothing is derived, and that Item's pricing is `REVIEW_REQUIRED` in every context.

### 8. Readiness is derived, never a stored truth

**The vocabulary** (v3.1 §3, §9.3):

```text
READY
REVIEW_REQUIRED
BLOCKED
DUPLICATE
STALE
```

**How it is evaluated.** Readiness is a **server-side evaluation of current canonical state**, in three separate layers (blocker 1). **No single readiness result is claimed for an Item across pricing contexts:** a pricing guard can be `READY` for one marketplace or account and `BLOCKED` for another.

| layer | scope | owner | reads |
| --- | --- | --- | --- |
| **Base readiness** | one Item, context-free | M4 | see below |
| **Pricing readiness** | one Item under one pricing context | M4 | see below |
| **Registration preflight** | a draft or registration target | M5 | base readiness + pricing readiness for the target's context + the marketplace/account requirements below |

**Base readiness reads:**
- **Source facts.** The current source revision's field statuses. A CORE field that is `REVIEW_REQUIRED` gives `REVIEW_REQUIRED`.
- **Stock evidence.** `SOLD_OUT` gives `BLOCKED`; mixed evidence gives `REVIEW_REQUIRED`.
- **Binding and composition.**
  - The Item needs a valid current binding for its composition.
  - A default unit that could not be inferred gives `REVIEW_REQUIRED` (§5).
- **Image integrity.** The image state of §9.
- **Unresolved group or Item conflicts.**
  - Open review items for the Item or its group, including an unresolved successor.
  - Product-side Item-key duplicates after a merge give `DUPLICATE`.

**Pricing readiness reads** the current `PricingSnapshot` for that Item **and that context**:
- a guard of `LOSS` or `BELOW_MIN_MARGIN` gives `BLOCKED`;
- pricing `REVIEW_REQUIRED` (ruling C, §5) gives `REVIEW_REQUIRED`;
- a missing or superseded snapshot gives `STALE`.

**Rules for every layer:**
- It returns one status and **every** applicable reason code.
- **Ruling D.** Within **one evaluation context**, the status is the highest of `BLOCKED > DUPLICATE > STALE > REVIEW_REQUIRED > READY`. Precedence never merges results across contexts.
- It carries its rule version.
- **Caching.**
  - A cached evaluation is keyed by its dependency fingerprint and rule version.
  - Pricing readiness, and anything that consumes a context-specific snapshot, includes the **pricing context** in that fingerprint.
  - A cached evaluation stays recomputable and is never authoritative.
- **No independent `REGISTERABLE = true`** or equivalent row exists as a source of truth.
- The UI displays the server's result and never re-decides it.

**`STALE`.**
- It applies when a derived input was computed against a dependency that is no longer current:
  - the current source revision, including an `EXTRACTOR_CHANGED` move (§3);
  - the membership revision;
  - the binding;
  - the composition;
  - the pricing context's fee table or policy version;
  - a rule or prompt version;
  - the image selection or its QA.
- It is not permanent: re-evaluation against current inputs clears it.

**The M3 capability boundary.** `ABSENT` options or tiers, read as absent from the page, are not in themselves a readiness failure. `REVIEW_REQUIRED` fields are.

**What M5 owns.** Registration preflight combines the two M4 layers with the marketplace- and account-scoped checks:
- category and category-required notices;
- required options;
- platform policy;
- the `SINGLE_LISTING_WITH_OPTIONS` compatibility check;
- the marketplace/account `DUPLICATE` lookup and `DuplicateOverride`.

M4 leaves those inputs open rather than guessing them.

### 9. Source assets stay immutable; derived images have their own lineage

**Source assets.** A `SourceAsset` (ADR-0010 §9) is never modified, overwritten, re-encoded or re-created by M4. It is content-addressed, with its bytes, SHA-256, MIME, dimensions, role and order. It stays the only source-image truth; Issue #56 creates no second one.

**A derived image is its own immutable artifact:**

```text
DerivedImageArtifact       immutable, content-addressed apart from source assets
  artifact_sha256
  inputs                   source asset SHA-256(s) and/or parent artifact SHA-256(s)
  transformation spec + transformation version (+ policy version)
  provenance               per operation: capability, execution class, input/output identity, executed_at
  produced_at
```

- **Chains are allowed:** source → derived → derived → marketplace variant. A derivation is never fixed to one step (Issue #56 §2.A).
- **Only a completed operation yields a durable artifact.** An intermediate or failed recipe leaves no registration candidate (Issue #56 §6).
- **Selection is its own pointer.**
  - Which artifact or source image an Item uses now is a **current selection pointer**, separate from any `parent` link. A parent link never serves as the current-state owner (Issue #56 §5).
  - Using, excluding or editing an image is an operator decision. The system may detect, warn and recommend, but it never deletes or alters a source image.
- **QA belongs to the exact artifact:** `artifact_sha256 + qa_rule_version → verdict (+ findings)`.
  - It is never inherited by another artifact, even one from the same logical edit (Issue #56 §8).
  - Readiness reads the QA of the exact artifacts selected.
- **Facts staleness, fail-closed.** An artifact or QA validated against a facts revision other than the current source revision is `STALE` for readiness until it is re-validated (Issue #56 §9).
- **Image edits have their own owner.** The Image Studio's edit lineage (`ImageEditRevision`) is a different domain owner from product enrichment (Issue #56 §4). This foundation is what it consumes.
- **pHash.** A perceptual hash may be stored beside an asset's hash to generate grouping candidates (v3.1 §6.2). It is never an auto-merge signal.
- **Out of M4:** marketplace upload, marketplace asset identity, and publication that no longer depends on supplier hotlinks are M5. No transformation or upload to a marketplace is authorized by M4.

### 10. The AI and enrichment boundary

- **M4 works with no AI provider configured.** ADR-0012 §9 and §14 apply: AI is a capability, and its failure blocks no COLLECT, `ProductFactsRevision` or canonical DB.
- **AI never creates or rewrites a source fact.** Low-confidence enrichment never becomes accepted source truth.
- **Locks and staleness:**
  - user-locked values are protected;
  - a group change stales enrichment results, never user decisions (v3.1 §7.3).
- **Prompts** come only from the persisted Settings/`PromptTemplate` store of Issue #30. No adapter owns a hidden business prompt.
- **No AI in the first M4 PRs.** Enrichment revisions and any provider call are separately authorized, not part of PR-A to PR-F unless an authorization says so.

### 11. The M4 / M5 boundary

| M4 owns (PRODUCT DB) | M5 owns (REGISTER) |
| --- | --- |
| `SourceProduct` identity and its current source revision pointer | `MarketplaceListingDraft`, `DraftListingItem` and the Draft invariants |
| `ProductGroup` (the canonical product), `GroupMember`, `GroupMembershipRevision`, `GroupChangeEvent` | category mapping, FINAL-GATE, registration preflight |
| `ListingComposition` (incl. the default single-unit composition) and the product-side `Item` | `RegistrationSnapshot`, `RegistrationItemSnapshot` (incl. `source_snapshot`) |
| `SourceBinding` (`SOURCE_OFFER` / `BASE_PRODUCT`) and the Item's current binding for pricing | `RegistrationIntent`, `RegistrationAttempt`, `MarketplaceRegistration(Item)` |
| `PricingSnapshot` per Item and `PricingContext` | `DuplicateOverride` and the marketplace/account `DUPLICATE` lookup |
| base readiness and per-context pricing readiness | marketplace image upload and marketplace asset identity |
| `DerivedImageArtifact`, the image selection pointer and artifact-bound QA | publication without supplier hotlinks |

M4 creates no marketplace listing and makes no marketplace call. Automatic source substitution and settlement are OPERATE (M6).

### 12. What this ADR does not decide

These are left to the PR that implements them, under this contract:
- table and column names beyond the entities named here, and migrations;
- the exact composition normalization and signature encoding;
- fee tables, rounding and the policy costs of a pricing policy version;
- readiness rule details beyond §8;
- image transformation specs;
- enrichment tasks.

## Rulings (PR #81 review `5245152210`)

The first draft raised five choices for review. The architect ruled all five and set two blockers. All of them are folded into the decision above and recorded here.

- **A. Current source revision: automatic advance accepted, renamed, extractor rule added.**
  - The pointer advances automatically to the newest eligible revision: same stable source identity, durably `RECORDED`, fingerprints intact. An unresolved identity never advances it.
  - It is named the *current source revision*, never "accepted", because it may point to a `REVIEW_REQUIRED` revision.
  - Across an `extractor_revision` change, fingerprints prove neither drift nor its absence. The move carries `EXTRACTOR_CHANGED` and invalidates dependents for re-evaluation (§3).
- **B. No source entity from ABSENT: the first draft's proposal is rejected and replaced.**
  - That draft read a product with ABSENT options and tiers as a source-side SKU and offer. Now M4 creates no `SourceSKU`, supplier SKU identifier or `QuantityOffer` for it.
  - It materializes only a product-side default single-unit composition and Item (§5), with a `BASE_PRODUCT` binding (§6) that pricing reads from the revision's explicit base product price, shipping and minimum fields.
  - `REVIEW_REQUIRED` option or tier evidence gives no default unit.
- **C. Accepted.** A multi-unit composition with no source-stated minimum for its quantity derives nothing, and its pricing is `REVIEW_REQUIRED` (§7).
- **D. Accepted within one evaluation context.** The precedence is `BLOCKED > DUPLICATE > STALE > REVIEW_REQUIRED > READY`, with every reason reported, and it never merges results across pricing contexts (§8).
- **E. Accepted.** The entity is persisted as `ProductGroup`, and `Product` is used in prose and UI (§1).
- **Blocker 1: pricing context.**
  - `PricingSnapshot` is per Item **and** per explicit `PricingContext` (marketplace, account when fees or policy differ, fee table version, pricing policy version), and one Item can have several current snapshots across contexts (§7).
  - Readiness splits into base readiness, per-context pricing readiness and M5 registration preflight (§8).
- **Blocker 2: no fabricated source truth.** Resolved by ruling B.

## Clarification (Issue #80 ruling `5738760913`, PR-Q)

The architect's product-level quantity-offer ruling, under the quantity-priced rule `5737762202`, clarifies §2 and §6. The text above now carries it:
- a product-level `QuantityOffer` may have no `SourceSKU` when the revision states `options = ABSENT`;
- a `SOURCE_OFFER` binding always requires an exact `QuantityOffer`;
- a `SourceSKU` reference is required only for a genuinely SKU-scoped offer;
- no synthetic "base SKU" is ever fabricated to satisfy a reference.

Ruling C (§7) already covers the minimum sale price. A generic `minimum_sale_price` names no offer. It is never applied to every quantity and never multiplied by one, so a `SOURCE_OFFER` it would affect is `REVIEW_REQUIRED`. Nothing else in this ADR changes.

## Consequences

- **PR-B to PR-F build on this contract** (Issue #80 §12): models and migrations, materialization and read-back, pricing and readiness, derived image lineage, then the M4 acceptance harness on a fresh dedicated data root. No preserved M3 campaign, and not the `m3-accept-04` runtime, is used as a migration shortcut.
- **Repository contract tests added with this ADR** (`tests/unit/test_m4_product_contract.py`) pin, before any schema exists:
  - the canonical pricing rule reads the same in CLAUDE.md, `docs/ARCHITECTURE.md` and this ADR;
  - no production code calls `max()` over a target-margin price and a minimum sale price;
  - the schema holds no second product root (no `products` table beside the group) and no `primary_source_id` or `allow_duplicate` column;
  - no table carries a platform fee without a pricing context, and this ADR's `PricingSnapshot` contract never lists `platform_fee` without `pricing_context`;
  - this ADR never reintroduces a source-side SKU or offer built from ABSENT options or tiers, and it keeps the `BASE_PRODUCT` binding;
  - this ADR keeps readiness layered and claims no single readiness for an Item across pricing contexts;
  - the pointer is never called an "accepted" revision in this ADR's decision;
  - this ADR is referenced from `docs/ARCHITECTURE.md` and `ROADMAP.md`.
- **The status documents** (CLAUDE.md §11, `ROADMAP.md` §14, README) name Issue #80 and this ADR as the M4 track. M4 stays CURRENT.

## References

- Issue #80 (body; kickoff `5726182664`); PR #81 architect review `5245152210` (rulings A–E, blockers 1–2)
- `docs/architecture/CANONICAL-V3.1.md` §2.2–§2.7, §3.1, §5.2, §6.1–§6.8, §7.3, §7.7, §8, §8.1–§8.2, §9.3–§9.4, §10.1, §11.5, §12–§12.2, §14
- `ROADMAP.md` §6; `docs/ARCHITECTURE.md` §4–§6, §10–§11; CLAUDE.md §5.1, §6
- `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md` B1, B6, §8
- ADR-0009, ADR-0010 (§1, §6 fingerprint/extractor boundary, §9), ADR-0011, ADR-0012 (§9, §14)
- Issue #30 (and refinement `5661813529`); Issue #56 (§1–§13)
- `docs/acceptance/M3.md` §2 (capability boundary)
