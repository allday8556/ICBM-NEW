# Coupang catalog and products provider-zero contract

Status: implementation contract for `C-CAT+PRODUCT-1`; no endpoint in this document grants LIVE
authority. The sole provider evidence is the official [Coupang Developer Center API
reference](https://developers.coupang.com/en/api), re-opened per endpoint before adoption.

## Boundary

- `catalog.py` implements the six KR category endpoints and three KR brand endpoints.
- `products.py` implements all 22 Products endpoints for their documented KR/TW markets.
- Every request is authenticated by C-AUTH-1 and can be handed only to `FakeTransport`. There is no
  HTTP client, socket, egress grant, real credential or provider call.
- `SellerProductId`, `ProductId` and `VendorItemId` are separate identities. A vendor id always
  comes from the signing context; callers cannot select another vendor for vendor-scoped reads.
- A category recommendation remains provider suggestion evidence. It never adopts a category,
  creates a reviewed metadata revision, changes a current pointer or authorizes product creation.
- Category metadata preserves notices, attributes, units, required documents, certifications and
  allowed offer conditions without label matching or inferred values. Existing REGISTER metadata
  review/adoption remains the sole owner.
- Create and replace use an immutable canonical JSON document. Required fields and the documented
  item-level separation (`unitCount` is not inventory) are checked locally before the fake
  transport. Partial update has an explicit field allow-list.
- Product mutations are executable contract/fake paths only. DRY_RUN/provider-zero remains in
  force; no result is interpreted as LIVE adoption or send permission.
- Delete requires explicit caller evidence that every item is stopped and the product is not
  awaiting approval. Auto-option operations retain the documented `PROCESSING` state instead of
  treating it as a completed mutation.

## Official endpoint families

The closed endpoint paths and classifications remain pinned by
[`ENDPOINT_INVENTORY.md`](./ENDPOINT_INVENTORY.md). The detail sources used by this slice are:

### Categories and brands (9)

- [Category metadata](https://developers.coupang.com/en/api/categories/category-metadata-query)
- [Category recommendation](https://developers.coupang.com/en/api/categories/category-recommendation)
- [Auto-category agreement](https://developers.coupang.com/en/api/categories/confirmation-of-category-auto-matching-service-agreement)
- [Category validity](https://developers.coupang.com/en/api/categories/how-to-check-validity-of-a-category)
- [One category](https://developers.coupang.com/en/api/categories/how-to-get-categories)
- [Full category list](https://developers.coupang.com/en/api/categories/how-to-get-category-list)
- [Brand search](https://developers.coupang.com/en/api/brands/brand-search)
- [Enrolled brands](https://developers.coupang.com/en/api/brands/enrolled-brands-list)
- [Brand by id](https://developers.coupang.com/en/api/brands/get-brand-by-id)

### Products (22)

- [Seller auto-option on](https://developers.coupang.com/en/api/products/activate-auto-generated-option-total-product-unit)
- [Item auto-option on](https://developers.coupang.com/en/api/products/activate-auto-generated-option-option-product-unit)
- [Original price](https://developers.coupang.com/en/api/products/change-the-base-price-for-discount-rate-at-an-item-level)
- [Sale price](https://developers.coupang.com/en/api/products/changing-price-of-each-item-of-a-product)
- [Quantity](https://developers.coupang.com/en/api/products/changing-quantity-of-each-product-item)
- [Delete](https://developers.coupang.com/en/api/products/delete-a-product)
- [Seller auto-option off](https://developers.coupang.com/en/api/products/disable-auto-generated-option-total-product-unit)
- [Item auto-option off](https://developers.coupang.com/en/api/products/disable-auto-generated-option-option-product-unit)
- [Replace product](https://developers.coupang.com/en/api/products/modify-product)
- [Create product](https://developers.coupang.com/en/api/products/product-creation)
- [Paged list](https://developers.coupang.com/en/api/products/product-list-paging-query)
- [Time-frame list](https://developers.coupang.com/en/api/products/product-list-ranging-query)
- [Partial update](https://developers.coupang.com/en/api/products/product-modification-approval-not-required)
- [Status history](https://developers.coupang.com/en/api/products/query-a-history-of-product-status-updates)
- [Registration inflow status](https://developers.coupang.com/en/api/products/query-a-status-of-product-registration)
- [External SKU summary](https://developers.coupang.com/en/api/products/query-a-summary-of-product-info)
- [Item inventory/price/status](https://developers.coupang.com/en/api/products/query-quantitypricestatus-by-product-items)
- [Product read](https://developers.coupang.com/en/api/products/querying-product)
- [Partial product read](https://developers.coupang.com/en/api/products/querying-product-approval-not-required)
- [Approval request](https://developers.coupang.com/en/api/products/request-for-product-approval)
- [Resume sales](https://developers.coupang.com/en/api/products/resume-sales-of-each-item-of-a-product)
- [Stop sales](https://developers.coupang.com/en/api/products/suspend-sale-of-each-item-of-a-product)

## Validation tier

This slice is `PROVIDER_ZERO`: focused functional/negative tests, repository rules and applicable
lint/type checks, followed by normal required CI once the branch owns the merge lane. It does not
manufacture a HIGH_RISK DUAL audit because it adds no credential handling, real transport, LIVE
authority, external side effect or irreversible operation.
