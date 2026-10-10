# Coupang Open API Endpoint Inventory

## Status

| Field | Value |
| --- | --- |
| Provider | Coupang Open API |
| Inventory status | `DETAIL_PAGE_VERIFIED_CLASSIFICATION` |
| Runtime adoption | `NONE` — this inventory grants no network or LIVE authority |
| Primary source | `P0-INDEX` + `P0-DETAIL` — official Coupang Developer Center index and endpoint pages |
| Retrieved at | `2026-10-10` |
| Indexed endpoints | `102` |
| Current Coupang transport | `NOT_IMPLEMENTED` |

This document is a complete first-pass inventory of the endpoints exposed by the official
Coupang Developer Center API index on the retrieval date. It is a planning and gap-analysis
artifact, not an endpoint allow-list.

Core rules:

`officially documented != adopted by ICBM`

`IMPLEMENT != callable`

`inventory classification != LIVE authority`

No row below authorizes credentials, HMAC signing, a provider request, a mutation, or production
use. Each endpoint still needs its own reviewed contract, safe retention profile, error semantics,
tests, and — for mutations — idempotency/read-back/reconciliation and an explicit execution grant.

## Classification

| State | Meaning |
| --- | --- |
| `IMPLEMENT` | In product scope and implementable from public official documentation. Build contract/model/adapter/fake/negative tests first; adoption and LIVE authority remain separate. |
| `CONTRACT_ONLY` | Official reference material without a callable endpoint, or data that belongs in a versioned local contract rather than a network caller. |
| `ACCOUNT_GATED` | Technically modelable, but real use depends on a special Coupang program, commercial contract, seller enrollment, or entitlement. Build the local blocked contract surface without enabling calls. |
| `OUT_OF_PRODUCT_SCOPE` | Explicitly limited to a market or workflow ICBM does not currently operate. Record it, but do not build a caller in the current Korea seller scope. |

## Source ledger

| Source ID | Authority | URL | Observation |
| --- | --- | --- | --- |
| `P0-INDEX` | `PROVIDER_NORMATIVE / OFFICIAL_DOC` | https://developers.coupang.com/en/api | API family counts and the 102-row method/path/title/family catalogue |
| `P0-HOME` | `PROVIDER_NORMATIVE / OFFICIAL_DOC` | https://developers.coupang.com/en | Portal capability summary, HMAC/IP-allowlist onboarding, and a displayed total of 103 endpoints |
| `P0-START` | `PROVIDER_NORMATIVE / OFFICIAL_DOC` | https://developers.coupang.com/en/getting-started/coupang-open-api | Seller prerequisites and sellerProductId/productId/vendorItemId/externalVendorSku identity semantics |
| `P0-DETAIL` | `PROVIDER_NORMATIVE / OFFICIAL_DOC` | linked from every endpoint title below | Endpoint-specific path, market statement, request/response surface, and documented gates |

Each endpoint title below links to its official detail page. `Market` uses the detail page's
buyer-market statement; `INDEX KR/TW` means the detail page omits that statement and the official
index is the only market evidence. `Effect` is closed to `READ`, `RECOMMENDATION_READ`,
`DOCUMENT_READ`, `MUTATION`, or `REFERENCE`; HTTP method alone does not turn a POST search,
recommendation, or document-generation request into a marketplace mutation.

The portal home currently displays `103` endpoints while `P0-INDEX` says `102 total` and its
family counts sum to 102. This inventory follows the enumerable 102-row API index and records the
one-count discrepancy as upstream drift to re-check, never as permission to invent a missing row.

## Family summary

| Family | Official count | `IMPLEMENT` | `CONTRACT_ONLY` | `ACCOUNT_GATED` | `OUT_OF_PRODUCT_SCOPE` |
| --- | ---: | ---: | ---: | ---: | ---: |
| Products | 22 | 22 | 0 | 0 | 0 |
| Categories | 6 | 6 | 0 | 0 | 0 |
| Brands | 3 | 3 | 0 | 0 | 0 |
| Shipments & Orders | 15 | 12 | 1 | 0 | 2 |
| Returns | 7 | 7 | 0 | 0 | 0 |
| Exchanges | 4 | 4 | 0 | 0 | 0 |
| Promotions & Coupons | 21 | 3 | 0 | 18 | 0 |
| Logistics Info | 8 | 7 | 1 | 0 | 0 |
| Customer Service | 6 | 6 | 0 | 0 | 0 |
| Settlement | 2 | 2 | 0 | 0 | 0 |
| CGF / CGF lite | 1 | 0 | 0 | 1 | 0 |
| Rocket Growth | 7 | 0 | 0 | 7 | 0 |
| **Total** | **102** | **72** | **2** | **26** | **2** |

## Endpoint matrix

All paths are relative to `https://api-gateway.coupang.com`. Every row is sourced from
`P0-INDEX`; endpoint-specific request/response contracts must cite the corresponding official
detail page before implementation.

### Products — 22

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | `POST` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller/auto-generated/opt-in` | [Activate auto-generated option (total product unit)](https://developers.coupang.com/en/api/products/activate-auto-generated-option-total-product-unit) | `KR` | `MUTATION` | `IMPLEMENT` |
| 2 | `POST` | `/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/auto-generated/opt-in` | [Activate auto-generated option (option product unit)](https://developers.coupang.com/en/api/products/activate-auto-generated-option-option-product-unit) | `KR` | `MUTATION` | `IMPLEMENT` |
| 3 | `PUT` | `/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/original-prices/{originalPrice}` | [Change base price for discount rate at item level](https://developers.coupang.com/en/api/products/change-the-base-price-for-discount-rate-at-an-item-level) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 4 | `PUT` | `/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/prices/{price}` | [Change price of each product item](https://developers.coupang.com/en/api/products/changing-price-of-each-item-of-a-product) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 5 | `PUT` | `/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/quantities/{quantity}` | [Change quantity of each product item](https://developers.coupang.com/en/api/products/changing-quantity-of-each-product-item) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 6 | `DELETE` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{sellerProductId}` | [Delete a product](https://developers.coupang.com/en/api/products/delete-a-product) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 7 | `POST` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller/auto-generated/opt-out` | [Disable auto-generated option (total product unit)](https://developers.coupang.com/en/api/products/disable-auto-generated-option-total-product-unit) | `KR` | `MUTATION` | `IMPLEMENT` |
| 8 | `POST` | `/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/auto-generated/opt-out` | [Disable auto-generated option (option product unit)](https://developers.coupang.com/en/api/products/disable-auto-generated-option-option-product-unit) | `KR` | `MUTATION` | `IMPLEMENT` |
| 9 | `PUT` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products` | [Modify product](https://developers.coupang.com/en/api/products/modify-product) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 10 | `POST` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products` | [Product creation](https://developers.coupang.com/en/api/products/product-creation) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 11 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products` | [Product list paging query](https://developers.coupang.com/en/api/products/product-list-paging-query) | `KR/TW` | `READ` | `IMPLEMENT` |
| 12 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/time-frame` | [Product list ranging query](https://developers.coupang.com/en/api/products/product-list-ranging-query) | `KR/TW` | `READ` | `IMPLEMENT` |
| 13 | `PUT` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{sellerProductId}/partial` | [Product modification (approval not required)](https://developers.coupang.com/en/api/products/product-modification-approval-not-required) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 14 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{sellerProductId}/histories` | [Query product status-update history](https://developers.coupang.com/en/api/products/query-a-history-of-product-status-updates) | `KR/TW` | `READ` | `IMPLEMENT` |
| 15 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/inflow-status` | [Query product-registration status](https://developers.coupang.com/en/api/products/query-a-status-of-product-registration) | `KR/TW` | `READ` | `IMPLEMENT` |
| 16 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/external-vendor-sku-codes/{externalVendorSkuCode}` | [Query product summary by external vendor SKU](https://developers.coupang.com/en/api/products/query-a-summary-of-product-info) | `KR/TW` | `READ` | `IMPLEMENT` |
| 17 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/inventories` | [Query quantity, price, and status by item](https://developers.coupang.com/en/api/products/query-quantitypricestatus-by-product-items) | `KR/TW` | `READ` | `IMPLEMENT` |
| 18 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{sellerProductId}` | [Query product](https://developers.coupang.com/en/api/products/querying-product) | `KR/TW` | `READ` | `IMPLEMENT` |
| 19 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{sellerProductId}/partial` | [Query product (approval not required)](https://developers.coupang.com/en/api/products/querying-product-approval-not-required) | `KR/TW` | `READ` | `IMPLEMENT` |
| 20 | `PUT` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{sellerProductId}/approvals` | [Request product approval](https://developers.coupang.com/en/api/products/request-for-product-approval) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 21 | `PUT` | `/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/sales/resume` | [Resume item sales](https://developers.coupang.com/en/api/products/resume-sales-of-each-item-of-a-product) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 22 | `PUT` | `/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/sales/stop` | [Suspend item sales](https://developers.coupang.com/en/api/products/suspend-sale-of-each-item-of-a-product) | `KR/TW` | `MUTATION` | `IMPLEMENT` |

### Categories — 6

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 23 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/meta/category-related-metas/display-category-codes/{displayCategoryCode}` | [Category metadata query](https://developers.coupang.com/en/api/categories/category-metadata-query) | `KR` | `READ` | `IMPLEMENT` |
| 24 | `POST` | `/v2/providers/openapi/apis/api/v1/categorization/predict` | [Category recommendation](https://developers.coupang.com/en/api/categories/category-recommendation) | `KR` | `RECOMMENDATION_READ` | `IMPLEMENT` |
| 25 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/vendors/{vendorId}/check-auto-category-agreed` | [Check category auto-matching agreement](https://developers.coupang.com/en/api/categories/confirmation-of-category-auto-matching-service-agreement) | `KR` | `READ` | `IMPLEMENT` |
| 26 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories/{displayCategoryCode}/status` | [Check category validity](https://developers.coupang.com/en/api/categories/how-to-check-validity-of-a-category) | `KR` | `READ` | `IMPLEMENT` |
| 27 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories/{displayCategoryCode}` | [Get one category](https://developers.coupang.com/en/api/categories/how-to-get-categories) | `KR` | `READ` | `IMPLEMENT` |
| 28 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories` | [Get category list](https://developers.coupang.com/en/api/categories/how-to-get-category-list) | `KR` | `READ` | `IMPLEMENT` |

### Brands — 3

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 29 | `POST` | `/v2/providers/seller_api/apis/api/v1/marketplace/brands/search` | [Brand search](https://developers.coupang.com/en/api/brands/brand-search) | `KR` | `READ` | `IMPLEMENT` |
| 30 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/brands/enrolled` | [Enrolled brands list](https://developers.coupang.com/en/api/brands/enrolled-brands-list) | `KR` | `READ` | `IMPLEMENT` |
| 31 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/brands/{brandId}` | [Get brand by ID](https://developers.coupang.com/en/api/brands/get-brand-by-id) | `KR` | `READ` | `IMPLEMENT` |

### Shipments & Orders — 15

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 32 | `POST` | `/v2/providers/openapi/apis/api/v1/vendors/{vendorId}/orders/directIntegration/arrangeShipment` | [Taiwan-only direct-courier shipment arrangement](https://developers.coupang.com/en/api/shipments/for-taiwan-onlyarrange-shipment-for-directly-integrated-courier) | `TW` | `MUTATION` | `OUT_OF_PRODUCT_SCOPE` |
| 33 | `POST` | `/v2/providers/openapi/apis/api/v1/vendors/{vendorId}/orders/directIntegration/downloadInvoices` | [Taiwan-only direct-courier invoice download](https://developers.coupang.com/en/api/shipments/for-taiwan-onlydownload-invoice-for-direct-integrated-courier) | `TW` | `DOCUMENT_READ` | `OUT_OF_PRODUCT_SCOPE` |
| 34 | `POST` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/orders/{orderId}/cancel` | [Cancel an order](https://developers.coupang.com/en/api/shipments/cancel-an-order) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 35 | `PATCH` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/ordersheets/acknowledgement` | [Change status to product in preparation](https://developers.coupang.com/en/api/shipments/changing-the-status-to-product-in-preparation) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 36 | `POST` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/completeLongTermUndelivery` | [Complete long-past promised delivery](https://developers.coupang.com/en/api/shipments/process-orders-long-past-their-promised-delivery-date-as-delivery-completed) | `KR` | `MUTATION` | `IMPLEMENT` |
| 37 | `PATCH` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/returnRequests/{receiptId}/completedShipment` | [Process already released](https://developers.coupang.com/en/api/shipments/processing-already-released) | `KR` | `MUTATION` | `IMPLEMENT` |
| 38 | `PATCH` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/returnRequests/{receiptId}/stoppedShipment` | [Process release stop](https://developers.coupang.com/en/api/shipments/processing-release-stop) | `KR` | `MUTATION` | `IMPLEMENT` |
| 39 | `GET` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/ordersheets` | [PO list query by minute](https://developers.coupang.com/en/api/shipments/po-list-query-by-minute) | `KR/TW` | `READ` | `IMPLEMENT` |
| 40 | `GET` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/ordersheets` | [PO list query by day with paging](https://developers.coupang.com/en/api/shipments/po-list-query-paging-by-day) | `KR/TW` | `READ` | `IMPLEMENT` |
| 41 | `GET` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/ordersheets/{shipmentBoxId}/history` | [Delivery-status change history](https://developers.coupang.com/en/api/shipments/searching-delivery-status-change-history) | `KR/TW` | `READ` | `IMPLEMENT` |
| 42 | `GET` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/ordersheets/{shipmentBoxId}` | [Single PO query by shipment box](https://developers.coupang.com/en/api/shipments/single-po-query-using-shipmentboxid) | `KR/TW` | `READ` | `IMPLEMENT` |
| 43 | `GET` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/{orderId}/ordersheets` | [Single PO query by order ID](https://developers.coupang.com/en/api/shipments/single-po-query-using-orderid) | `INDEX KR/TW` | `READ` | `IMPLEMENT` |
| 44 | `ETC` | `N/A` | [South Korean cities and postal codes](https://developers.coupang.com/en/api/shipments/south-korean-cities-and-postal-codes) | `KR` | `REFERENCE` | `CONTRACT_ONLY` |
| 45 | `POST` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/orders/updateInvoices` | [Update waybills](https://developers.coupang.com/en/api/shipments/updating-waybills) | `KR` | `MUTATION` | `IMPLEMENT` |
| 46 | `POST` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/orders/invoices` | [Upload waybills](https://developers.coupang.com/en/api/shipments/uploading-waybills) | `KR` | `MUTATION` | `IMPLEMENT` |

### Returns — 7

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 47 | `PATCH` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/returnRequests/{receiptId}/approval` | [Approve return request](https://developers.coupang.com/en/api/returns/approve-request-for-return) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 48 | `GET` | `/v2/providers/openapi/apis/api/v6/vendors/{vendorId}/returnRequests/{receiptId}` | [Query one return request](https://developers.coupang.com/en/api/returns/query-one-return-request) | `KR/TW` | `READ` | `IMPLEMENT` |
| 49 | `GET` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/returnWithdrawRequests` | [Query return-withdrawal history by date](https://developers.coupang.com/en/api/returns/query-return-return-withdrawal-history-by-date-range) | `KR` | `READ` | `IMPLEMENT` |
| 50 | `POST` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/returnWithdrawList` | [Query return-withdrawal history by receipt ID](https://developers.coupang.com/en/api/returns/query-return-withdrawal-history-by-receiptid) | `KR` | `READ` | `IMPLEMENT` |
| 51 | `GET` | `/v2/providers/openapi/apis/api/v6/vendors/{vendorId}/returnRequests` | [Return/cancellation request list](https://developers.coupang.com/en/api/returns/return-cancellation-request-list-query) | `KR/TW` | `READ` | `IMPLEMENT` |
| 52 | `PATCH` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/returnRequests/{receiptId}/receiveConfirmation` | [Confirm return-product receipt](https://developers.coupang.com/en/api/returns/return-product-receipt-confirmation) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 53 | `POST` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/return-exchange-invoices/manual` | [Upload pickup waybill](https://developers.coupang.com/en/api/returns/upload-a-pickup-waybill) | `KR` | `MUTATION` | `IMPLEMENT` |

### Exchanges — 4

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 54 | `PATCH` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/exchangeRequests/{exchangeId}/receiveConfirmation` | [Confirm exchange-product receipt](https://developers.coupang.com/en/api/exchanges/check-the-receipt-of-exchange-requested-product) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 55 | `GET` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/exchangeRequests` | [Query exchange requests](https://developers.coupang.com/en/api/exchanges/query-a-list-of-exchange-requests) | `KR/TW` | `READ` | `IMPLEMENT` |
| 56 | `PATCH` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/exchangeRequests/{exchangeId}/rejection` | [Reject exchange request](https://developers.coupang.com/en/api/exchanges/reject-an-exchange-request) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 57 | `POST` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/exchangeRequests/{exchangeId}/invoices` | [Upload exchange-product waybill](https://developers.coupang.com/en/api/exchanges/upload-a-waybill-for-exchange-product) | `KR` | `MUTATION` | `IMPLEMENT` |

### Promotions & Coupons — 21

The three common contract/budget queries are ordinary read prerequisites and are `IMPLEMENT`.
Book cashback explicitly requires a negotiated contract, and coupon operations are contract-bound;
those 18 callers remain `ACCOUNT_GATED` until the relevant contract/entitlement is proven. Their
local contracts are still implementable, but provider calls remain blocked.

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 58 | `GET` | `/v2/providers/fms/apis/api/v2/vendors/{vendorId}/contract/list` | [Query contract list](https://developers.coupang.com/en/api/promotions/common-query-contract-list) | `KR` | `READ` | `IMPLEMENT` |
| 59 | `GET` | `/v2/providers/fms/apis/api/v2/vendors/{vendorId}/contract` | [Query single contract](https://developers.coupang.com/en/api/promotions/common-query-single-contract) | `KR` | `READ` | `IMPLEMENT` |
| 60 | `GET` | `/v2/providers/fms/apis/api/v1/vendors/{vendorId}/budgets` | [Query budget status](https://developers.coupang.com/en/api/promotions/common-run-a-query-on-budget-status) | `KR` | `READ` | `IMPLEMENT` |
| 61 | `POST` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/products/items/cashback` | [Apply book cashback](https://developers.coupang.com/en/api/promotions/book-applying-cashback) | `KR` | `MUTATION` | `ACCOUNT_GATED` |
| 62 | `GET` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/products/items/cashback` | [Query product cashback](https://developers.coupang.com/en/api/promotions/book-product-cashback-search) | `KR` | `READ` | `ACCOUNT_GATED` |
| 63 | `DELETE` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/products/items/cashback` | [Remove product cashback](https://developers.coupang.com/en/api/promotions/book-removing-product-cashback) | `KR` | `MUTATION` | `ACCOUNT_GATED` |
| 64 | `GET` | `/v2/providers/marketplace_openapi/apis/api/v1/coupons/transactionStatus` | [Check downloadable-coupon request status](https://developers.coupang.com/en/api/promotions/downloadable-coupon-check-requested-status) | `KR` | `READ` | `ACCOUNT_GATED` |
| 65 | `POST` | `/v2/providers/marketplace_openapi/apis/api/v1/coupons` | [Create downloadable coupon](https://developers.coupang.com/en/api/promotions/downloadable-coupon-creation) | `KR` | `MUTATION` | `ACCOUNT_GATED` |
| 66 | `POST` | `/v2/providers/marketplace_openapi/apis/api/v1/coupons/expire` | [Expire downloadable coupon](https://developers.coupang.com/en/api/promotions/downloadable-coupon-expiring-download-coupon) | `KR` | `MUTATION` | `ACCOUNT_GATED` |
| 67 | `PUT` | `/v2/providers/marketplace_openapi/apis/api/v1/coupon-items` | [Add downloadable-coupon item](https://developers.coupang.com/en/api/promotions/downloadable-coupon-item-creation) | `KR` | `MUTATION` | `ACCOUNT_GATED` |
| 68 | `GET` | `/v2/providers/marketplace_openapi/apis/api/v1/coupons/{couponId}` | [Query downloadable coupon](https://developers.coupang.com/en/api/promotions/downloadable-coupon-query-one-coupon-couponid) | `KR` | `READ` | `ACCOUNT_GATED` |
| 69 | `PUT` | `/v2/providers/fms/apis/api/v1/vendors/{vendorId}/coupons/{couponId}` | [Expire/disable instant coupon](https://developers.coupang.com/en/api/promotions/instant-discount-coupon-expiration-disable) | `KR` | `MUTATION` | `ACCOUNT_GATED` |
| 70 | `GET` | `/v2/providers/fms/apis/api/v1/vendors/{vendorId}/requested/{requestedId}` | [Check instant-coupon request status](https://developers.coupang.com/en/api/promotions/instant-discount-coupon-check-request-status) | `KR` | `READ` | `ACCOUNT_GATED` |
| 71 | `POST` | `/v2/providers/fms/apis/api/v2/vendors/{vendorId}/coupon` | [Create instant coupon](https://developers.coupang.com/en/api/promotions/instant-discount-coupon-creation) | `KR` | `MUTATION` | `ACCOUNT_GATED` |
| 72 | `POST` | `/v2/providers/fms/apis/api/v1/vendors/{vendorId}/coupons/{couponId}/items` | [Add item to instant coupon](https://developers.coupang.com/en/api/promotions/instant-discount-coupon-item-addition) | `INDEX KR/TW` | `MUTATION` | `ACCOUNT_GATED` |
| 73 | `GET` | `/v2/providers/fms/apis/api/v2/vendors/{vendorId}/{orderId}/coupons` | [Query instant coupons by order ID](https://developers.coupang.com/en/api/promotions/instant-discount-coupon-query-a-list-of-coupons-orderid) | `KR` | `READ` | `ACCOUNT_GATED` |
| 74 | `GET` | `/v2/providers/fms/apis/api/v2/vendors/{vendorId}/coupons` | [Query instant coupons by status](https://developers.coupang.com/en/api/promotions/instant-discount-coupon-query-a-list-of-coupons-status) | `KR` | `READ` | `ACCOUNT_GATED` |
| 75 | `GET` | `/v2/providers/fms/apis/api/v1/vendors/{vendorId}/coupons/{couponId}/items` | [Query instant-coupon items](https://developers.coupang.com/en/api/promotions/instant-discount-coupon-query-a-list-of-items-status) | `KR` | `READ` | `ACCOUNT_GATED` |
| 76 | `GET` | `/v2/providers/fms/apis/api/v2/vendors/{vendorId}/coupon` | [Query instant coupon by coupon ID](https://developers.coupang.com/en/api/promotions/instant-discount-coupon-query-one-coupon-couponid) | `KR` | `READ` | `ACCOUNT_GATED` |
| 77 | `GET` | `/v2/providers/fms/apis/api/v1/vendors/{vendorId}/coupons/{couponId}/items/{couponItemId}` | [Query instant coupon item by couponItemId](https://developers.coupang.com/en/api/promotions/instant-discount-coupon-query-one-coupon-couponitemid) | `KR` | `READ` | `ACCOUNT_GATED` |
| 78 | `GET` | `/v2/providers/fms/apis/api/v1/vendors/{vendorId}/coupons/{couponId}/items/{vendorItemId}` | [Query instant coupon item by vendorItemId](https://developers.coupang.com/en/api/promotions/instant-discount-coupon-query-one-coupon-vendoritemid) | `KR` | `READ` | `ACCOUNT_GATED` |

### Logistics Info — 8

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 79 | `ETC` | `N/A` | [Courier code](https://developers.coupang.com/en/api/logistics/courier-code) | `KR` | `REFERENCE` | `CONTRACT_ONLY` |
| 80 | `POST` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/returnShippingCenters` | [Create return location](https://developers.coupang.com/en/api/logistics/create-a-return-location) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 81 | `POST` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/outboundShippingCenters` | [Create shipping location](https://developers.coupang.com/en/api/logistics/create-a-shipping-location) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 82 | `GET` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/returnShippingCenters` | [Query return locations](https://developers.coupang.com/en/api/logistics/query-a-list-of-return-locations) | `KR/TW` | `READ` | `IMPLEMENT` |
| 83 | `GET` | `/v2/providers/marketplace_openapi/apis/api/v2/vendor/shipping-place/outbound` | [Query shipping location](https://developers.coupang.com/en/api/logistics/query-a-shipping-location) | `KR/TW` | `READ` | `IMPLEMENT` |
| 84 | `GET` | `/v2/providers/openapi/apis/api/v3/return/shipping-places/center-code` | [Query one return location](https://developers.coupang.com/en/api/logistics/query-one-return-location) | `KR/TW` | `READ` | `IMPLEMENT` |
| 85 | `PUT` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/returnShippingCenters/{returnCenterCode}` | [Update return location](https://developers.coupang.com/en/api/logistics/update-a-return-location) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 86 | `PUT` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/outboundShippingCenters/{outboundShippingPlaceCode}` | [Update shipping location](https://developers.coupang.com/en/api/logistics/update-a-shipping-location) | `KR/TW` | `MUTATION` | `IMPLEMENT` |

### Customer Service — 6

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 87 | `POST` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/onlineInquiries/{inquiryId}/replies` | [Answer product inquiry](https://developers.coupang.com/en/api/cs/answer-to-customer-product-inquiry) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 88 | `POST` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/callCenterInquiries/{inquiryId}/replies` | [Answer contact-center inquiry](https://developers.coupang.com/en/api/cs/answer-to-inquiries-via-coupang-contact-center) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 89 | `POST` | `/v2/providers/openapi/apis/api/v4/vendors/{vendorId}/callCenterInquiries/{inquiryId}/confirms` | [Confirm contact-center inquiry](https://developers.coupang.com/en/api/cs/coupang-contact-center-inquiry-check) | `KR/TW` | `MUTATION` | `IMPLEMENT` |
| 90 | `GET` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/onlineInquiries` | [Query customer product inquiries](https://developers.coupang.com/en/api/cs/customer-inquiry-query-by-product) | `KR/TW` | `READ` | `IMPLEMENT` |
| 91 | `GET` | `/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/callCenterInquiries` | [Query contact-center inquiries](https://developers.coupang.com/en/api/cs/query-of-coupang-contact-center-inquiries) | `KR/TW` | `READ` | `IMPLEMENT` |
| 92 | `GET` | `/v2/providers/openapi/apis/api/v5/vendors/callCenterInquiries/{inquiryId}` | [Query one contact-center inquiry](https://developers.coupang.com/en/api/cs/query-of-coupang-contact-center-single-inquiry) | `INDEX KR/TW` | `READ` | `IMPLEMENT` |

### Settlement — 2

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 93 | `GET` | `/v2/providers/openapi/apis/api/v1/revenue-history` | [Sales detail query](https://developers.coupang.com/en/api/settlement/sales-detail-query) | `KR` | `READ` | `IMPLEMENT` |
| 94 | `GET` | `/v2/providers/marketplace_openapi/apis/api/v1/settlement-histories` | [Settlement detail query](https://developers.coupang.com/en/api/settlement/settlement-detail-query) | `KR` | `READ` | `IMPLEMENT` |

### CGF / CGF lite — 1

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 95 | `POST` | `/v2/providers/openapi/apis/api/v1/vendors/{vendorId}/label/printOrderLabel` | [CGF Lite order label](https://developers.coupang.com/en/api/cgf/cgf-lite-order-label-api) | `INDEX KR/TW` | `DOCUMENT_READ` | `ACCOUNT_GATED` |

### Rocket Growth — 7

Rocket Growth callers remain blocked until program enrollment and the precise seller/account
contract are proven. Shared paths do not permit a normal marketplace product caller to substitute
for the Rocket Growth contract.

| # | Method | Path | Official title / official detail source | Market | Effect | Classification |
| ---: | --- | --- | --- | --- | --- | --- |
| 96 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories` | [Rocket Growth category list](https://developers.coupang.com/en/api/rocket-growth/how-to-get-category-list-for-rocket-growth-products) | `KR` | `READ` | `ACCOUNT_GATED` |
| 97 | `PUT` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products` | [Modify Rocket Growth or hybrid product](https://developers.coupang.com/en/api/rocket-growth/modify-product-rocket-growth-rocket-growthmarketplace-hybrid-products) | `KR` | `MUTATION` | `ACCOUNT_GATED` |
| 98 | `POST` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products` | [Create Rocket Growth or hybrid product](https://developers.coupang.com/en/api/rocket-growth/product-creation-rocket-growth-rocket-growthmarketplace-hybrid-products) | `KR` | `MUTATION` | `ACCOUNT_GATED` |
| 99 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products` | [Page Rocket Growth or hybrid products](https://developers.coupang.com/en/api/rocket-growth/product-list-paging-query-rocket-growth-rocket-growthmarketplace-hybrid-products) | `KR` | `READ` | `ACCOUNT_GATED` |
| 100 | `GET` | `/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{sellerProductId}` | [Query Rocket Growth or hybrid product](https://developers.coupang.com/en/api/rocket-growth/query-product-rocket-growth-or-rocket-growthmarketplace-hybrid-products) | `KR` | `READ` | `ACCOUNT_GATED` |
| 101 | `GET` | `/v2/providers/rg_open_api/apis/api/v1/vendors/{vendorId}/rg/inventory/summaries` | [Rocket Growth inventory](https://developers.coupang.com/en/api/rocket-growth/rg-inventory-api) | `KR` | `READ` | `ACCOUNT_GATED` |
| 102 | `GET` | `/v2/providers/rg_open_api/apis/api/v1/vendors/{vendorId}/rg/orders` | [Rocket Growth orders](https://developers.coupang.com/en/api/rocket-growth/rg-order-apilist-query) | `KR` | `READ` | `ACCOUNT_GATED` |

## Current implementation gap

ICBM currently has no Coupang-specific transport, signer, connection owner, endpoint registry, or
provider caller. C-P3 supplies only provider-zero reviewed metadata adoption and semantic mapping.
The mature SmartStore implementation is reusable only at common ownership boundaries; none of its
bearer-token, endpoint, identity, error, or payload semantics may be copied as Coupang semantics.

| Reusable common owner | Coupang-specific gap |
| --- | --- |
| CONNECT credential lifecycle and capability projection | HMAC-SHA256 signing, AccessKey/SecretKey handling, vendor identity, IP allowlist, clock/skew contract |
| REGISTER reviewed category metadata owner | Coupang category tree, category metadata, notices, attributes, options, unit/value semantics, adoption revision |
| Registration intent, audit, grants, brakes, retention | sellerProduct/product/vendorItem identity, safe response retention, exact read-back and reconciliation |
| PRODUCTS canonical product and AtomicSKU | Coupang sellerProduct/item request projection without canonical mutation or Cartesian synthesis |
| OPERATE order/claim state owners | Coupang ordersheets, shipmentBox, return receipt, exchange, waybill, cancellation, and status transition semantics |
| Existing provider-zero UI patterns | Coupang-specific blocked/account-gated states and recommendation provenance |

## Recommended implementation order

1. `C-AUTH-1`: HMAC signing, vendor identity, IP-allowlist and clock/error contracts; fake transport only.
2. `C-CAT-1`: category list/single/validity/metadata reads and reviewed metadata revision adoption.
3. `C-BRAND-1`: enrolled brand/by-ID/search reads.
4. `C-PRODUCT-READ-1`: product list, time-frame list, single, partial, history, inflow, SKU summary and item inventory reads.
5. `C-PRODUCT-WRITE-1`: create/modify/partial/approval with DRY_RUN projection, idempotency gap recording, read-back and UNKNOWN handling.
6. `C-ITEM-WRITE-1`: price, quantity, base price, sales stop/resume, delete, and auto-option mutations behind granular grants/brakes.
7. `C-ORDER-READ-1`: order lists/single/history plus acknowledgements and cancellation-stop checks.
8. `C-SHIP-1`: invoice upload/update and long-undelivery flow with bounded state transitions.
9. `C-CLAIM-1`: returns and exchanges.
10. `C-LOGISTICS-1`: shipping/return locations and versioned courier/postal reference contracts.
11. `C-CS-1` and `C-SETTLEMENT-1`: inquiries/replies and read-only settlement surfaces.
12. `C-PROMO-1`, `C-RG-1`, `C-CGF-1`: account-gated contracts, fakes, negative tests, and blocked UI states before any credentialed canary.
