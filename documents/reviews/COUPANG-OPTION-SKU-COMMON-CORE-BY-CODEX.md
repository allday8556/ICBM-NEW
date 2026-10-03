# Coupang Option/SKU contract and common-core audit

> Audit identity: `track-c-coupang-option-sku/2026-10-03/v1`
>
> State: `RESEARCH_COMPLETE / COMMON_CORE_PROPOSAL_UNDER_REVIEW`
>
> Provider adoption: `NOT_ADOPTED`
>
> Runtime verification: `UNVERIFIED`
>
> Base: `origin/main@0875cd64354af6689af18bba62747278ea955bf1`
>
> Scope: Track C only. No Track A or Track B file was changed. No authenticated Coupang request or
> provider mutation was made.

This is a research packet and architecture proposal, not an accepted ADR. GitHub review and an
accepted ADR are required before the common-core changes in §J are implemented.

## Executive verdict

The current ICBM model **cannot yet represent one positive-option Atomic SKU once and project that
same identity to both SmartStore and Coupang**. Collection can preserve ordered option axes and
atomic source configurations, and REGISTER can carry option display values plus a stable
`registration_item_key`. The missing link is the canonical product layer: it materializes no
positive `SourceSKU`/Atomic SKU, and `ProductItem` is unique only by `ProductGroup +
composition_signature`. Two option combinations with the same seller composition therefore have
no distinct canonical Item identity and would also collide in `registration_item_key/v1`.

The problem is a common-core gap, not a reason to copy Coupang's schema into Product Core. The
recommended repair is a provider-neutral, source-proven Atomic SKU owner; reviewed marketplace
option-requirement revisions and projections remain in REGISTER; Coupang IDs remain provider
identities. No missing axis or value may be generated to satisfy Coupang.

Track B finding: **structural for a future optionful cross-market path, but not a blocker for the
current bounded SmartStore no-positive-option vertical**. Track B must not be changed by this
packet. If Track B expands to a positive-option listing before the common-core ADR is accepted,
that expansion is blocked.

## C0 — reconciliation and owner map

| Concern | Current owner | Finding |
| --- | --- | --- |
| Source observations | `ProductFactsRevision`, including `OptionsValue.axes/configurations` | Keeps exact ordered source evidence; no fabrication |
| Canonical product | `ProductGroup` | One product spine; reusable |
| Sellable unit | `ProductItem = ProductGroup + ListingComposition` | Does not identify an option combination |
| Current procurement | `SourceBinding` | Has no positive SourceSKU path in current implementation |
| Marketplace authoring | `RegistrationDraft` and authored `options[item_id][dimension]` | Display values only, not canonical option identity |
| Frozen outbound evidence | `RegistrationSnapshot` / `RegistrationItemSnapshot` | Reusable; outbound values and revisions are immutable |
| Pre-create item correlation | `registration_item_key/v1` | Reusable concept; input identity must be versioned and extended |
| Provider registration | `MarketplaceRegistration` / `MarketplaceRegistrationItem` | Reusable group/item link; provider identity multiplicity needs extension |
| Provider read-back | marketplace adapter | Correct owner; Coupang comparison contract is missing |
| Order/claim resolution | no implemented canonical owner found | Missing |

The frozen v3.1 architecture names `SourceSKU`, `required_option_mapping` and
`required_option_revision`, but the current accepted M4 scope explicitly excludes positive
option-axis/configuration materialization. Those frozen names are design context, not proof that a
current implementation exists.

## A. Official Coupang Option/SKU contract inventory

All locators below are official Coupang Developer Center pages, observed 2026-10-03. “Captured”
means documented here; it does not mean adopted or runtime-verified.

| Capability | Method and path | Contract relevant to Track C | Coverage |
| --- | --- | --- | --- |
| Full category hierarchy | `GET /v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories` | Recursive category code/name/status tree; statuses `ACTIVE`, `READY`, `DISABLED` | Captured |
| One category depth | `GET .../display-categories/{displayCategoryCode}` | `0` returns depth 1; response contains one lower depth | Captured |
| Leaf validity | `GET .../display-categories/{displayCategoryCode}/status` | Only a leaf is accepted; returns boolean; category renewal is documented twice yearly | Captured |
| Category metadata | `GET .../category-related-metas/display-category-codes/{displayCategoryCode}` | `isAllowSingleItem`, attributes, notices, documents, certifications, allowed conditions | Captured; live all-category values not captured |
| Product CREATE | `POST /v2/providers/seller_api/apis/api/v1/marketplace/seller-products` | `items[]` are vendor-item option units; max 200; response returns `sellerProductId` only | Captured |
| Product modify | `PUT /v2/providers/seller_api/apis/api/v1/marketplace/seller-products` | Existing option update needs `sellerProductItemId` and `vendorItemId`; add/delete rules vary by approval history | Captured for identity use |
| Product read | `GET .../seller-products/{sellerProductId}` | Returns item-level `sellerProductItemId`, `vendorItemId`, `externalVendorSku`, attributes and prices | Captured |
| Seller-code summary | `GET .../seller-products/external-vendor-sku-codes/{externalVendorSkuCode}` | Wait at least one minute after CREATE; returns an array of seller products | Captured; uniqueness/absence semantics not documented |
| Item state | `GET .../vendor-items/{vendorItemId}/inventories` | `amountInStock`, `salePrice`, `onSale` per option | Captured |
| Item price | `PUT .../vendor-items/{vendorItemId}/prices/{price}` | Post-approval item price; 10-won input unit | Captured, mutation not adopted |
| Item stock | `PUT .../vendor-items/{vendorItemId}/quantities/{quantity}` | Post-approval item inventory | Captured, mutation not adopted |
| Orders | `GET /v2/providers/openapi/apis/api/v5/vendors/{vendorId}/ordersheets` | Each order item includes `vendorItemId`; `externalVendorSkuCode` is optional | Captured |
| Returns/cancellations read | `GET /v2/providers/openapi/apis/api/v6/vendors/{vendorId}/returnRequests` | Return items are identified by `vendorItemId` and include `sellerProductId` | Captured |
| Seller cancellation | `POST /v2/providers/openapi/apis/api/v5/vendors/{vendorId}/orders/{orderId}/cancel` | Mutation addresses `vendorItemIds[]` with receipt counts | Captured for identity only; not adopted |
| Exchanges read | `GET /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/exchangeRequests` | Primary exchange rows use order/target item IDs; nested delivery/return rows expose `vendorItemId` | Captured |

Official sources:

- Category list: https://developers.coupang.com/en/api/categories/how-to-get-category-list
- Category detail: https://developers.coupang.com/en/api/categories/how-to-get-categories
- Category validity: https://developers.coupang.com/en/api/categories/how-to-check-validity-of-a-category
- Category metadata: https://developers.coupang.com/en/api/categories/category-metadata-query
- Korean listing guide: https://developers.coupang.com/ko/getting-started/guide-to-creating-product-listings
- Product creation: https://developers.coupang.com/en/api/products/product-creation
- Product query: https://developers.coupang.com/en/api/products/querying-product
- Product modify: https://developers.coupang.com/en/api/products/modify-product
- External seller-code query: https://developers.coupang.com/en/api/products/query-a-summary-of-product-info
- Item state/price/stock: https://developers.coupang.com/en/api/products/query-quantitypricestatus-by-product-items , https://developers.coupang.com/en/api/products/changing-price-of-each-item-of-a-product , https://developers.coupang.com/en/api/products/changing-quantity-of-each-product-item
- Orders: https://developers.coupang.com/en/api/shipments/po-list-query-by-minute
- Returns: https://developers.coupang.com/en/api/returns/return-cancellation-request-list-query
- Cancellation: https://developers.coupang.com/en/api/shipments/cancel-an-order
- Exchanges: https://developers.coupang.com/en/api/exchanges/query-a-list-of-exchange-requests
- Identifier glossary: https://developers.coupang.com/en/getting-started/coupang-open-api

Current policy overlays:

- Free/open purchase options were disallowed for the target categories from 2024-10-10; only
  category-metadata purchase options may be registered:
  https://developers.coupang.com/en/notices/notice-on-changes-to-all-category-purchase-options-sepember-9-2024
- Mandatory option enforcement was strengthened from 2026-02-02; CREATE can fail or exposure can
  be restricted, and list-query violation types include `MOTA_V2` and `ATTR`:
  https://developers.coupang.com/en/notices/notice-mandatory-purchase-option-entry-and-api-specification-changes-effective-f
- Brand, GTIN/model number and mandatory purchase options began becoming mandatory across all
  categories from 2026-08-01:
  https://developers.coupang.com/en/notices/product-information-policy-update-mandatory-brand-gtinmodel-number-and-purchase-
- The published baseline rate was reduced from 10 to 5 requests/second on 2026-03-17:
  https://developers.coupang.com/en/notices/optimization-and-adjustment-of-open-api-rate-limiteffective-march-17th2026

Where an older API page still says an open attribute is possible, the later explicit policy notice
is the governing captured evidence for new integration work. No open purchase option is proposed.

## B. Category required-option/attribute matrix

### Classification that can be proven

An attribute is a purchase option when `exposed=EXPOSED`; `exposed=NONE` is a search attribute.
The Korean guide and official FAQ define these required cases:

1. `required=MANDATORY`, `groupNumber=NONE`, `exposed=EXPOSED`: the axis itself is required.
2. `required=MANDATORY`, `groupNumber=1`, `exposed=EXPOSED`: choose one attribute from the group.

The generic metadata reference says group values may be `NONE`, `1`, or `2` and that one attribute
is selected among attributes with the same non-`NONE` group number. It does not independently say
that every group `2` is required. The English listing guide currently says `OPTIONAL` for condition
2 while the Korean guide and FAQ say `MANDATORY`. This is a real official-document contradiction.
The inventory must therefore store raw fields and source/revision, and must not derive a broader
rule for `OPTIONAL` or group `2` without live metadata plus reviewed clarification.

### Required machine-readable columns

```text
taxonomy_observed_at
category_code / category_path / category_status / leaf_valid
metadata_fingerprint / metadata_observed_at
is_allow_single_item
attribute_type_name
role                         PURCHASE_OPTION | SEARCH_ATTRIBUTE
required                     MANDATORY | OPTIONAL
group_number                 NONE | 1 | 2 | provider-new-value
data_type
input_type                   INPUT | SELECT | provider-new-value | NOT_CAPTURED
input_values[]
basic_unit / usable_units[]
classification               REQUIRED_AXIS | REQUIRED_AXIS_CHOICE | OPTIONAL_AXIS | SEARCH_ATTRIBUTE | AMBIGUOUS
classification_evidence
```

`documents/reviews/coupang-option-sku-category-matrix-v1.json` contains this schema and the
official-guide sample rows. It is deliberately not labelled as a current full inventory.

### Captured official-guide samples, not an exhaustive matrix

| Category | Attribute | Raw role | Raw requirement/group | Classification |
| --- | --- | --- | --- | --- |
| `27742` 비타민 | 개당 캡슐/정 | `EXPOSED` | `MANDATORY`, group `1` | one required group-1 choice |
| `27742` 비타민 | 개당 중량 | `EXPOSED` | `MANDATORY`, group `1` | one required group-1 choice |
| `27742` 비타민 | 개당 용량 | `EXPOSED` | `MANDATORY`, group `1` | one required group-1 choice |
| `27742` 비타민 | 수량 | `EXPOSED` | `MANDATORY`, `NONE` | required axis |
| `62676` 휴대폰 악세서리 | 색상 | `EXPOSED` | `MANDATORY`, `NONE` | required axis |
| `62676` 휴대폰 악세서리 | 수량 | `EXPOSED` | `MANDATORY`, `NONE` | required axis |

### Full-inventory feasibility and safe plan

Full inventory is mechanically feasible, but was not executed because it requires authenticated
provider reads and endpoint adoption:

1. Read the full hierarchy once and retain exact raw status/path evidence.
2. Select leaf candidates; confirm validity only when needed, since Coupang documents twice-yearly
   renewal rather than a per-request requirement.
3. Read metadata once per ACTIVE valid leaf. Persist raw sanitized content, observation time and
   content digest before deriving rows.
4. Target at most 1–2 requests/second, sequential with jitter, below the current 5 rps baseline;
   checkpoint every leaf and resume rather than restart.
5. On `429` or transient `5xx`, honor documented response guidance when present, otherwise apply
   bounded exponential backoff. Do not parallel-fan-out.
6. Treat every new raw enum, missing field, changed group rule, or document contradiction as
   `REVIEW_REQUIRED`; never coerce it into an existing value.
7. Refresh on category/policy notice or explicit operator request. A periodic TTL is not specified
   by Coupang and remains an ICBM adoption decision.

Current full-matrix coverage: `NOT_CAPTURED`. The sample is `OFFICIAL_GUIDE_SAMPLE`, not runtime
evidence and not safe for category-wide registration.

## C. Option axis, value and combination limits

| Contract | Consequence |
| --- | --- |
| CREATE `items[]` is the vendor item option list, max 200 | Preflight blocks 201+ projected Atomic SKUs; never silently truncates |
| Each `itemName` must be unique, max 150 characters | Name is display/management text, not identity |
| Attributes are category-predefined; name max 25, value max 30 including unit | Projection validates exact metadata revision and wire lengths |
| At least one attribute value is required | Empty required option projection is blocked |
| An exposed attribute cannot be added when all item values are identical | Constant mandatory-axis interaction is insufficiently specified; do not invent a workaround |
| `inputType`, `inputValues`, `dataType`, `basicUnit`, `usableUnits` can constrain values | Preserve typed value plus exact provider rendering; do not store only a concatenated label |
| `groupNumber` is an OR-group of attribute types | It is not an option-axis ordinal and must not become one |
| Metadata attributes may change before approval; after approval they may be added but not deleted/modified | Snapshot the metadata revision; updates need a separate reviewed workflow |
| `isAllowSingleItem` is category metadata | Single-item capability is category evidence, not a global default |
| `bundleType=AB` disallows options and becomes immutable | AB bundle is a separate compatibility rule, not an Atomic SKU model |
| Free/open purchase options are disallowed by the current policy notice | No free-axis fallback when a canonical axis cannot map |

ICBM must enumerate only source-proven atomic configurations. It must not create the Cartesian
product of axis values merely because axes list those values. For the example `color × quantity`,
four Atomic SKUs exist only when the source evidence proves all four configurations.

## D. SKU price and stock contract

- At CREATE, `salePrice` and `maximumBuyCount` belong to each `items[]` option. `maximumBuyCount`
  is inventory (maximum 99,999); `unitCount` is solely for displayed unit-price calculation and
  must never be read as stock.
- After approval, item price and inventory mutate by immutable `vendorItemId`. Price accepts a
  minimum input unit of 10 won. Those mutations are outside this research scope.
- `GET .../vendor-items/{vendorItemId}/inventories` reads item-level `amountInStock`, `salePrice`
  and `onSale`.
- Therefore Product Core continues to own canonical Item pricing/procurement facts; the Coupang
  adapter projects and reads item-level values. Coupang price or stock must not become a second
  Atomic SKU truth.

## E. Registration identity map

| ICBM identity | Proposed Coupang projection/read | Stability | Owner |
| --- | --- | --- | --- |
| provider-listing unit / `listing_identity` | local only; do not overload a provider product ID | immutable locally | REGISTER core |
| `MarketplaceRegistration.marketplace_product_id` | `sellerProductId` | immutable group-level provider ID | registration |
| `registration_item_key` | `items[].externalVendorSku` | seller-controlled; provider length/charset bound is `NOT_CAPTURED` | snapshot + adapter projection |
| `MarketplaceRegistrationItem.marketplace_option_id` | `vendorItemId` | immutable lowest-level provider option ID | registration item |
| typed secondary item identity | `sellerProductItemId` | needed to modify option values | provider identity alias/evidence |
| typed exposure identities | `itemId`, `productId` | `productId` may change on merge/split | adapter read evidence, never canonical identity |

CREATE success returns `sellerProductId`, not item IDs. The item mapping is completed by product
read-back. A temporarily saved item has `vendorItemId=null`; that state cannot prove the final
operational item identity. The provider does not document an `externalVendorSku` length/charset
limit on the captured pages, so using the 37-character `rik1-...` value verbatim remains blocked
until that wire constraint is captured or a deterministic projection is accepted.

## F. Read-back identity map

1. Read by the returned `sellerProductId`.
2. Require exactly the expected set of seller-controlled item codes, after an accepted
   `externalVendorSku` projection exists.
3. Map each code to exactly one `RegistrationItemSnapshot`; duplicate/missing/unexpected codes are
   a whole-listing mismatch.
4. Persist `vendorItemId` as the primary operational option identity only after it is non-null and
   the item correspondence passes.
5. Retain `sellerProductItemId` as a typed provider identity because option-value modification
   needs it; do not overwrite `vendorItemId` with it.
6. Compare sent purchase-option values under a Coupang-specific normalizer. Product query can add
   recommended/auto-extracted attributes and can return recommended attributes with empty values,
   so full raw-array equality is not a valid generic comparison.
7. A CREATE envelope with `code=SUCCESS` may still carry MOTA details and `errorItems`; success
   status alone must not confirm a registration or exposure.

The external-seller-code summary endpoint returns a list and requires at least one minute after
CREATE. No uniqueness guarantee, complete freshness guarantee, or zero-result absence proof was
captured. It may support a positive candidate search, but never proves uniqueness or non-creation.

## G. Order and claim identity map

Resolution is identity-first and names are diagnostics only:

```text
order item
  if externalVendorSkuCode is present:
      resolve exact seller-code projection -> one MarketplaceRegistrationItem
      if vendorItemId is also present, require it to match the persisted provider identity
  else:
      resolve vendorItemId -> exactly one MarketplaceRegistrationItem
  conflict, duplicate or no match:
      REVIEW_REQUIRED; never fall back to product/option name
  success:
      MarketplaceRegistrationItem -> Item -> AtomicSKU -> current source binding
```

| Flow | Provider identity | Canonical resolution rule |
| --- | --- | --- |
| Order | required `vendorItemId`; optional `externalVendorSkuCode`; `sellerProductId` also returned | seller code when present plus vendor ID cross-check; otherwise vendor ID |
| Return/cancellation read | `returnItems[].vendorItemId`, `sellerProductId` | vendor ID maps to the persisted registration item |
| Seller cancellation mutation | `vendorItemIds[]` plus receipt counts | out of scope; if later adopted, issue only from a resolved order line |
| Exchange | original `orderItemId`/`targetItemId`; nested return/delivery item rows contain `vendorItemId` | correlate through the immutable original order line; nested vendor ID is additional evidence |

The current repository has no implemented canonical order-line identity/resolution owner. That is a
later OPERATE slice, but its contract must be defined before any order ingestion is called
complete.

## H. Current ICBM gap analysis

| Requested concept | Verdict | Evidence and required action |
| --- | --- | --- |
| `ProductGroup` | `KEEP` | Correct one-product canonical spine |
| `Item` | `CONFLICT` | Identity is only group + composition; extend to include Atomic SKU without fabricating a base SKU |
| source options | `KEEP + EXTEND` | Collection preserves axes/configurations; add accepted materialization and source binding |
| atomic SKU | `MISSING` | Frozen design names it; current DB intentionally materializes none |
| quantity structure | `KEEP` | `ListingComposition` and exact `QuantityOffer` separation is correct |
| `SINGLE_LISTING_WITH_OPTIONS` | `KEEP + EXTEND` | Unit semantics are valid; compatibility must read canonical Atomic SKUs and provider requirements |
| `SEPARATE_LISTINGS` | `KEEP` | Valid projection choice; not a substitute for missing SKU identity |
| `required_option_mapping` | `MISSING` | Frozen design only; current authored option text is not a reviewed mapping |
| `required_option_revision` | `EXTEND` | Category metadata revision exists, but its option policy stores only supported/count/dimensions |
| `registration_item_key` | `CONFLICT` | v1 input omits Atomic SKU and collides for equal compositions |
| `MarketplaceRegistrationItem` | `KEEP + EXTEND` | Correct correspondence owner; needs typed multiple provider identities |
| `marketplace_option_id` | `KEEP + EXTEND` | Suitable for Coupang `vendorItemId`; insufficient for `sellerProductItemId` and volatile exposure IDs |
| `RegistrationDraft` | `KEEP + EXTEND` | Correct workflow owner; options must reference canonical axes/values rather than author a second truth |
| Snapshot / ItemSnapshot | `KEEP + EXTEND` | Correct immutable evidence; freeze Atomic SKU ID, mapping revision and typed rendering |
| read-back | `KEEP + EXTEND` | Correct adapter boundary; add Coupang normalizer/comparison |
| order identity | `MISSING` | Add order-line resolution contract keyed by persisted provider identities |

## I. Can NAVER and Coupang share one Atomic SKU?

Current implementation: **No, not for a source with positive option configurations.** It can only
exercise the bounded no-positive-option path and manually authored registration option display
values. Those values do not establish canonical SKU identity.

Target after §J is accepted: **Yes.** One Product Core record such as `sku-red-2` owns selections
`color=red`, `quantity=2`. A SmartStore registration item and a Coupang vendor item both point to
that same Item/Atomic SKU. Their provider IDs and rendered labels differ, but an order from either
resolves back to `sku-red-2`. No marketplace owns a second SKU truth.

## J. Proposed common-core changes

### J1. Product Core owners

```text
ProductOptionAxis
  axis_id, product_group_id, semantic_key, ordinal

ProductOptionValue
  value_id, axis_id, typed canonical value, unit semantics, display evidence

AtomicSKU
  atomic_sku_id, product_group_id, selection_signature, created_at

AtomicSKUSelection
  atomic_sku_id, axis_id, value_id

AtomicSKUSourceBinding
  atomic_sku_id, group_member_id, source_revision_id
  source_configuration_key, supplier_sku_id?, valid_from, valid_to, provenance
```

- Materialize only a `CONFIRMED` source configuration. Axis lists alone never create the Cartesian
  product.
- Preserve count, grade, pack, weight and unit semantics; equal totals never merge structurally
  different configurations.
- `supplier_sku_id` remains optional and source-owned. The deterministic
  `source_configuration_key` is not presented as a supplier ID.
- A no-option product keeps the existing base-product path and has no synthetic Atomic SKU.

### J2. Product Item identity

Extend ProductItem to one sellable composition of either a proven Atomic SKU or the existing
no-option base product:

```text
ProductItem
  item_id
  product_group_id
  atomic_sku_id?             null only for the proven no-option base path
  listing_composition_id
  item_identity_signature    versioned
```

Use separate uniqueness constraints for atomic and base paths. `item_identity_signature/v2`
includes the Atomic SKU identity when present. Do not reinterpret or rewrite existing v1 Items.

### J3. Marketplace requirement revision and projection

Extend the reviewed, append-only category metadata content with provider-neutral option-rule rows:

```text
MarketplaceOptionRequirementRevision
  marketplace/category/taxonomy revision
  single_item_allowed
  max_items / max_dimensions when documented
  rules[]: provider_key, role, requiredness, choice_group,
           data_type, input_type, allowed_values, basic_unit, usable_units

RequiredOptionProjection
  metadata_revision_id
  atomic_sku_id
  canonical_axis/value ids
  provider rule key and exact rendered value
  decision provenance / review state
```

Purchase options, search attributes and product-information notices remain distinct projections.
They may reference the same canonical fact but never share an owner. A missing required axis/value
is `REVIEW_REQUIRED`, not an authored guess.

### J4. Registration identity versioning

- Keep existing `registration_item_key/v1` immutable for existing snapshots.
- Define v2 from `listing_identity + item_identity_signature/v2` (or the immutable `item_id` if the
  ADR selects that narrower contract). Never derive it from labels, position or price.
- Freeze key version, Atomic SKU ID, option mapping revision and exact provider rendering in the
  ItemSnapshot.
- Add a provider-neutral typed alias collection for secondary/volatile provider identities. Keep
  the one primary operational ID in `marketplace_option_id`; for Coupang that is `vendorItemId`.

### J5. Order resolution owner

Add an immutable order-line provider identity record and a resolution record naming exactly one
`MarketplaceRegistrationItem`/Item/Atomic SKU, its evidence, resolver version and review state.
Conflicting seller code and provider option ID is never auto-resolved.

### J6. ADR and migration boundary

An accepted common-core ADR is required. Its minimum decisions are Atomic SKU ownership and
lifecycle, Item identity v2, registration key coexistence, category option-requirement schema,
typed provider identities, drift behavior, and order-line resolution. Migrations must be additive;
existing no-option Items, snapshots and keys remain readable and unchanged.

## K. Coupang adapter implementation slices

No slice below authorizes a provider call.

1. **CP-0 evidence/adoption:** resolve the official English/Korean group-rule contradiction,
   capture external SKU wire limits and read-after-write behavior, register read-only endpoints.
2. **CP-1 category inventory:** adopted read-only hierarchy/validity/metadata client, conservative
   pacing, checkpointing, raw sanitized evidence and revision diff.
3. **CP-2 requirement mapper:** metadata rows to reviewed provider-neutral requirements; no product
   projection yet.
4. **CP-3 projection/preflight:** Atomic SKUs to Coupang `items[]`; enforce required axes, values,
   units, 200-item limit, unique names/codes, AB exclusion and no open-option fallback. Dry-run only.
5. **CP-4 read-back:** product query normalizer, exact seller-code item-set comparison, typed ID
   capture and MOTA/error-item handling.
6. **CP-5 order resolver:** ordersheet read-only ingestion and identity-first Atomic SKU resolution.
7. **CP-6 claim resolver:** return/exchange read-only ingestion correlated to immutable order lines.
8. **CP-7 mutation adoption:** CREATE, approval, price, stock or cancellation only under separate
   evidence, safety, authorization and bounded LIVE work. This packet grants none.

## L. Contract-test matrix

| Area | Required test |
| --- | --- |
| Source evidence | Axes without configurations create zero Atomic SKUs |
| Source evidence | Only explicit configurations materialize; missing Cartesian combinations stay missing |
| Identity | Same selections in different axis order cannot silently alias; canonical order is revisioned |
| Identity | Different count/grade/pack remain different even at equal weight/total |
| No-option compatibility | Existing ABSENT-option base Item gains no synthetic Atomic SKU |
| Item v2 | Two Atomic SKUs with the same composition remain distinct Items |
| Key migration | v1 snapshots/keys remain unchanged; new v2 keys include Atomic SKU identity |
| Requirement metadata | Purchase/search/notice roles remain separate |
| Requirement metadata | Unknown enums and the MANDATORY/OPTIONAL group contradiction become REVIEW_REQUIRED |
| Drift | A moved current metadata revision stales preflight; a frozen Snapshot keeps its old revision |
| Projection | Missing required canonical axis/value blocks; no default or AI fabrication |
| Projection | 201 items block; 200 pass if every other rule passes |
| Projection | Duplicate external SKU, duplicate itemName and invalid unit/value block |
| CREATE parse | Response returns only sellerProductId; no item ID is invented |
| CREATE parse | `SUCCESS` with MOTA details/errorItems does not confirm exposure |
| Read-back | `vendorItemId=null` in saved state does not complete operational identity |
| Read-back | Expected seller-code set must match exactly; provider-added recommended attrs are normalized separately |
| Reconcile | External-SKU query returning N candidates is not uniqueness or absence proof |
| Orders | seller code + vendor ID agree -> one Atomic SKU; disagreement -> REVIEW_REQUIRED |
| Orders | absent optional seller code resolves only by unique persisted vendor ID |
| Claims | return vendor ID resolves through registration item; exchange resolves through original order line |
| Safety | Inventory research never calls mutation endpoints and respects the local adoption gate |
| Regression | SmartStore projection/read-back and current bounded canary tests remain unchanged |

## M. Track B structural blocker verdict

```text
COMMON_CORE_STRUCTURAL_FINDING = true
TRACK_B_CURRENT_BOUNDED_CANARY_BLOCKER = false
TRACK_B_POSITIVE_OPTION_EXPANSION_BLOCKER = true
```

Reason: current Track B can continue within its accepted no-positive-option M4/M5 boundary. The
finding becomes blocking only if it claims that manually authored option display text is a shared
canonical Atomic SKU, or attempts a positive-option listing before the common-core ADR and Product
Core materialization are accepted. Track C makes no Track B change and requests no main freeze.

## Residual unknowns before implementation

- `externalVendorSku` length, charset, account-scoped uniqueness and reuse behavior.
- Authoritative resolution of the English `OPTIONAL` versus Korean/FAQ `MANDATORY` group rule,
  and whether group `2` has the same required-choice semantics.
- The exact current live attribute schema for every ACTIVE leaf and whether `inputType` and
  `inputValues` are always returned despite their omission from the current English field table.
- Required exposed attributes whose value is constant across all items versus the CREATE rule that
  forbids adding an exposed attribute when all values are identical.
- Read-after-write timing for product query and when `vendorItemId` becomes non-null after approval.
- Whether seller code is preserved exactly in every relevant order/claim path and every product
  lifecycle transition.
- Idempotency/replay guarantees for CREATE and mutation endpoints.

Every unknown is fail-closed. None is filled by SmartStore behavior, examples, AI inference or a
category-specific hard-coded fallback.
