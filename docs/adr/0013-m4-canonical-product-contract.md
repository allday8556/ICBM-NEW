# ADR-0013 — M4 canonical product contract: one product identity, the accepted-revision pointer, composition and Item, pricing, derived readiness and image lineage

Status: **PROPOSED**. This is PR-A of Issue #80. It becomes binding only when this line reads ACCEPTED, and that needs three things: the GPT audit, the independent Claude AI cross-audit and the user's explicit merge authorization. It authorizes no schema, migration, runtime code, UI, AI call, supplier request or marketplace call.

Decision owner: Architect (ChatGPT). Sources:
- the Issue #80 body (the M4 umbrella) and the architect kickoff `5726182664`, which authorized PR-A only;
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

- **The persisted entity is `ProductGroup`**, as the frozen v3.1 schema names it (Choice for review E).
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
- **`SourceSKU` and `QuantityOffer`** are source facts that M4 reads from a revision (v3.1 §5.2).
  - Quantity tiers stay original `(quantity, total_price)` facts. They are never flattened into a unit price or multiplied into new totals.
  - Atomic source SKU identity is preserved. The same weight never merges a different count, grade or pack.
- **The M3 capability boundary carries forward unchanged** (`docs/acceptance/M3.md` §2): positive `CONFIRMED` option-axis/configuration support and positive quantity-tier values are not accepted. M4 fabricates no SKU and no offer. How a revision that states no options and no tiers presents its one sellable unit is Choice for review B.

### 3. The current accepted revision is a pointer, never an edit

**Where the pointer lives.** It is kept **per `SourceProduct`**. A group has no facts revision of its own in M4. Downstream reads a member's facts through that member's current pointer. v3.1 §10.1 reserves a separately named `group_facts_revision_*` for any future group-level facts.

**Its history.** The pointer history is append-only. Each decision records:
- the revision it points to and the one it replaced;
- the rule or actor that decided it, with the rule version;
- the reason, the time and the correlation.

Each decision is audited (`AuditEvent`).

**Invariants:**
- It points only to a revision of the **same** `(supplier_key, source_product_id)`, and only to one whose fingerprints are intact.
- Accepting a revision **does not** change its `facts_status`, any field status or any evidence. "Current" means "the source facts downstream uses now", not "confirmed". A `REVIEW_REQUIRED` revision may be current, and readiness then reads its field statuses (§8).
- A recollection creates a new revision (ADR-0010). It never updates a historical one.
- The pointer never moves to an older revision silently. Moving it backwards is an explicit, recorded decision.
- When the pointer moves, the difference between the old and new current revision is classified by the source-drift rules (`docs/ARCHITECTURE.md` §11). Every derived result whose dependency fingerprint includes the old revision becomes `STALE` (§8). User-locked values are kept, with a `SOURCE_DRIFT` mark, and are not overwritten (v3.1 §7.3, §7.7).

How the pointer advances when a new revision is recorded is Choice for review A.

### 4. Group membership and its revisions

**`GroupMember`** links a group to a `SourceProduct`, never to one revision. It carries:
- a status: `CANDIDATE`, `CONFIRMED` or `REJECTED`;
- `match_method`, `match_confidence` and `match_strategy_version`;
- `decided_by`, and when it was created and confirmed.

A `SourceProduct` is a `CONFIRMED` member of **at most one** group at a time.

**`GroupMembershipRevision`** is an immutable, numbered revision of a group's confirmed member set.
- A new one is created on every change to that set.
- Derived state names it in its dependency fingerprint, and a later `RegistrationItemSnapshot` names it as `group_membership_revision_id` (v3.1 §2.5).
- A change of a member's accepted-revision pointer is **not** a membership change and creates no membership revision.

**Materialization in M4.** An accepted `SourceProduct` with no confirmed group gets a new group with that one member. The first vertical has one supplier. Nothing in the contract makes one member per group an invariant.

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
  source_sku_id
  quantity_offer_id
  fulfillment_quantity
  valid_from / valid_to
```

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

### 7. `PricingSnapshot`: one owner, one rule, immutable

**Only the Pricing owner calculates a selling price.** No UI, no adapter and no other service re-decides one.

**What a snapshot is:**
- It is **immutable**, and it belongs to one Item: the price is an Item attribute, never a listing attribute (v3.1 §8).
- It is computed from one current binding, the bound source's current accepted revision, and one pricing policy version.
- Any change of input produces a **new** snapshot. A snapshot is never updated.
- A later `RegistrationItemSnapshot.pricing_snapshot_id_at_registration` references the exact snapshot used.

At contract level it records:

```text
PricingSnapshot            immutable
  pricing_snapshot_id
  item key                 (group identifier + composition_signature)
  source_binding_id
  source_product_facts_revision_id
  purchase_cost            from the bound QuantityOffer's own total; never a derived unit price
  supplier_shipping
  platform_fee             with its fee-table version
  minimum_sale_price       exactly as the source states it for the bound offer, or none
  target_margin_price
  final_sale_price
  price_basis              MINIMUM_SALE_PRICE | TARGET_MARGIN
  expected_net_margin
  price_guard              OK | LOSS | BELOW_MIN_MARGIN
  pricing_policy_version
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

**The registration guard (Issue #80 §7).** These are policy inputs, versioned by `pricing_policy_version`, and never source facts:
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
- No `minimum_sale_price × quantity` and no per-unit source price is ever manufactured. When a composition's quantity has no minimum the source states, while the unit does, the case is Choice for review C.

### 8. Readiness is derived, never a stored truth

**The vocabulary** (v3.1 §3, §9.3):

```text
READY
REVIEW_REQUIRED
BLOCKED
DUPLICATE
STALE
```

**How it is evaluated.** Readiness is a **server-side evaluation of current canonical state**. At M4 it is evaluated per product-side Item.
- It returns one status and **every** applicable reason code. When several apply, the status is the highest in the order `BLOCKED > DUPLICATE > STALE > REVIEW_REQUIRED > READY` (Choice for review D).
- It carries its rule version.
- If an evaluation is cached, it is keyed by its dependency fingerprint and rule version. It stays recomputable and is never authoritative.
- **No independent `REGISTERABLE = true`** or equivalent row exists as a source of truth.
- The UI displays the server's result and never re-decides it.

**What it reads at M4:**
- the current accepted revision's field statuses: a CORE field that is `REVIEW_REQUIRED` gives `REVIEW_REQUIRED`;
- stock evidence: `SOLD_OUT` gives `BLOCKED`, and mixed evidence gives `REVIEW_REQUIRED`;
- the current `PricingSnapshot`: a guard of `LOSS` or `BELOW_MIN_MARGIN` gives `BLOCKED`;
- open review items for the Item or its group, including an unresolved successor;
- the image state of §9;
- product-side Item-key duplicates after a merge, which give `DUPLICATE`.

**`STALE`.**
- It applies when a derived input was computed against a dependency that is no longer current:
  - the accepted-revision pointer;
  - the membership revision;
  - the binding;
  - the composition;
  - a policy, rule or prompt version;
  - the image selection or its QA.
- It is not permanent: re-evaluation against current inputs clears it.

**The M3 capability boundary.** `ABSENT` options or tiers, read as absent from the page, are not in themselves a readiness failure. `REVIEW_REQUIRED` fields are.

**What M5 owns.** Marketplace- and account-scoped checks are M5 preflight:
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
- **Facts staleness, fail-closed.** An artifact or QA validated against a facts revision other than the current accepted one is `STALE` for readiness until it is re-validated (Issue #56 §9).
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
| `SourceProduct` identity and the accepted-revision pointer | `MarketplaceListingDraft`, `DraftListingItem` and the Draft invariants |
| `ProductGroup` (the canonical product), `GroupMember`, `GroupMembershipRevision`, `GroupChangeEvent` | category mapping, FINAL-GATE, marketplace preflight |
| `ListingComposition` and the product-side `Item` | `RegistrationSnapshot`, `RegistrationItemSnapshot` (incl. `source_snapshot`) |
| `SourceBinding` and the Item's current binding for pricing | `RegistrationIntent`, `RegistrationAttempt`, `MarketplaceRegistration(Item)` |
| `PricingSnapshot` | `DuplicateOverride` and the marketplace/account `DUPLICATE` lookup |
| product-side readiness evaluation | marketplace image upload and marketplace asset identity |
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

## Choices for review

- **A. How the accepted-revision pointer advances.**
  - This ADR's choice: a newly `RECORDED` revision of the same source identity, with intact fingerprints, becomes current by a versioned default rule (`decided_by` = that rule). The drift classification of §3 then stales the dependents.
  - The alternative: an explicit operator acceptance for every advance. It is slower, and it keeps downstream on older facts until someone acts.
- **B. One sellable unit when a revision states no options and no tiers.**
  - The case: the accepted M3 product states no options and no tiers.
  - This ADR's choice: read its own `prices`, `shipping` and `minimum_sale_price` fields as **one implicit `SourceSKU` with one quantity-1 `QuantityOffer`**. That is a reading of what the page offers, not a fabricated SKU.
  - A revision whose options or tiers are `REVIEW_REQUIRED` yields no implicit unit. Its pricing is `REVIEW_REQUIRED`.
- **C. A minimum for a multi-unit composition.**
  - The case: the source states a `minimum_sale_price` for one unit but not for the bound offer's quantity.
  - This ADR's choice: the Pricing owner derives nothing (no `× quantity`), and the Item's pricing is `REVIEW_REQUIRED`, never silently treated as "no minimum".
- **D. The readiness precedence** `BLOCKED > DUPLICATE > STALE > REVIEW_REQUIRED > READY`, with every reason still reported.
- **E. The persisted name.** The entity is persisted as `ProductGroup`, following the frozen v3.1 schema, and `Product` is its name in prose and UI. The alternative is the name `Product` with v3.1's semantics. Either way there is one entity and one identifier (§1).

## Consequences

- **PR-B to PR-F build on this contract** (Issue #80 §12): models and migrations, materialization and read-back, pricing and readiness, derived image lineage, then the M4 acceptance harness on a fresh dedicated data root. No preserved M3 campaign, and not the `m3-accept-04` runtime, is used as a migration shortcut.
- **Repository contract tests added with this ADR** (`tests/unit/test_m4_product_contract.py`) pin, before any schema exists:
  - the canonical pricing rule reads the same in CLAUDE.md, `docs/ARCHITECTURE.md` and this ADR;
  - no production code calls `max()` over a target-margin price and a minimum sale price;
  - the schema holds no second product root (no `products` table beside the group) and no `primary_source_id` or `allow_duplicate` column;
  - this ADR is referenced from `docs/ARCHITECTURE.md` and `ROADMAP.md`.
- **The status documents** (CLAUDE.md §11, `ROADMAP.md` §14, README) name Issue #80 and this ADR as the M4 track. M4 stays CURRENT.

## References

- Issue #80 (body; kickoff `5726182664`)
- `docs/architecture/CANONICAL-V3.1.md` §2.2–§2.7, §3.1, §5.2, §6.1–§6.8, §7.3, §7.7, §8, §8.1–§8.2, §9.3–§9.4, §10.1, §11.5, §12–§12.2, §14
- `ROADMAP.md` §6; `docs/ARCHITECTURE.md` §4–§6, §10–§11; CLAUDE.md §5.1, §6
- `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md` B1, B6, §8
- ADR-0009, ADR-0010 (§1, §9), ADR-0011, ADR-0012 (§9, §14)
- Issue #30 (and refinement `5661813529`); Issue #56 (§1–§13)
- `docs/acceptance/M3.md` §2 (capability boundary)
