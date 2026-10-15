# Product Read

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `ADOPTED` (origin and channel read-backs) | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

### Provenance

- **Locators:** `(v2) 원상품 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/read-origin-product-product ; `(v2) 채널 상품 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/read-channel-product-1-product ; support https://github.com/commerce-api-naver/commerce-api/discussions/3170
- **Authority / version:** `OFFICIAL_API_DOC`, NAVER Commerce API 2.89.0 (2026-09-15); `OFFICIAL_SUPPORT` for the identity-stability line. Source IDs `NAVER-P0-PACKET-289` (5746489554), `NAVER-P0-PRODUCT-READ`, `NAVER-P0-REVIEW-CREATE-289` (5768199984). Below, `PACKET` = 5746489554, `REVIEW` = 5768199984 and `STATUS` = 5911962320 (`NAVER-P0-READ-STATUS-290`, `SOURCES.md` §5.4: a reading of the 2.90.0 reference made through GPT on 2026-09-30, not reviewed by a second reader).

### Provider contract

| Field | Origin-product read | Channel-product read | Source |
| --- | --- | --- | --- |
| Method / path | `GET /v2/products/origin-products/{originProductNo}` | `GET /v2/products/channel-products/{channelProductNo}` | PACKET |
| Auth | `Authorization: Bearer {token}` | same | PACKET; `NAVER-P0-AUTH` |
| API group | `상품` | `상품` | PACKET |
| Request body / Content-Type | none | none | `ENDPOINT_MATRIX.md` §4.1 |
| Response structure | the `원상품 정보 구조체` schema is used for read responses as well as registration (see [PRODUCT_CREATE](PRODUCT_CREATE.md#request-structure) for its captured keys) | not captured | PACKET |
| Response top level (2.90.0) | `groupProduct`, `originProduct`, `windowChannelProduct`, `smartstoreChannelProduct` | not captured | STATUS |
| Sale status (2.90.0) | `originProduct.statusType` — string, `상품 판매 상태 코드`; `WAIT`, `SALE`, `OUTOFSTOCK`, `UNADMISSION`, `REJECTION`, `SUSPENSION`, `CLOSE`, `PROHIBITION`, `DELETE`; the 200 schema gives no per-value meaning | not captured | STATUS |
| SmartStore display status (2.90.0) | `smartstoreChannelProduct.channelProductDisplayStatusType` — string, `전시 상태 코드(스마트스토어 채널 전용)`; `WAIT` 전시 대기, `ON` 전시 중, `SUSPENSION` 전시 중지. `windowChannelProduct.channelProductDisplayStatusType` carries the same values for the window channel (read-only) | not captured | STATUS |
| Lookup semantics | `originProductNo` and `channelProductNo` are provider-issued unique numbers, stable across later product edits (`OFFICIAL_SUPPORT` #3170) — a deterministic single-item lookup once the number is known, not a way to find a product whose CREATE response was lost | same | REVIEW |
| Errors | the read reference documents product error examples used by `ERRORS.md` (`NAVER-P0-PRODUCT-READ`); API-server `NOT_FOUND` is resource context, distinct from `GW.NOT_FOUND` (`ERRORS.md` §9.3, §10.4) | same | `ERRORS.md` |

### Unresolved (exact)

- The channel-product read response structure, and the JSON paths of the retained fields other than the two status members above.
- Any statement that `SALE` with `ON` means a buyer can see and purchase the listing: none is documented, and no single "visible and purchasable" field exists.
- The endpoint-specific error statuses/codes of both reads.
- Any read-after-write consistency window after a mutation (`ERRORS.md` Q5).
- Endpoint-specific rate limits.

### Store-catalog field paths (ADR-0042 S1a, tasks E4 and E5)

ADR-0042 §5.3 asks for the JSON path of each catalog field in the origin read. **S1a could not
re-read the official reference**: `apicenter.commerce.naver.com` was refused to the implementing
environment on 2026-10-15 (web fetcher and in-app browser alike), and no other route was used. So no
path below is a new observation. The table records which paths this catalog **already holds** from
official evidence, by two recorded facts together: the `원상품 정보 구조체` schema is used for the
responses of product read as well as for registration (PACKET), and the origin read's 200 response
carries it as its top-level member `originProduct`, beside `smartstoreChannelProduct` (STATUS). The
member paths themselves are the ones [PRODUCT_CREATE](PRODUCT_CREATE.md#request-structure) records
for that schema, each with its own source. A field with no recorded path is `NOT_CAPTURED` and is
neither retained nor stored.

| §5.3 group | Path in the origin read (2.89.0 schema; 2.90.0 envelope) | Status | Source |
| --- | --- | --- | --- |
| state | `originProduct.statusType`; `smartstoreChannelProduct.channelProductDisplayStatusType` | recorded (already read since `m5-published-state-r1`) | STATUS |
| name, price, stock | `originProduct.name`; `originProduct.salePrice`; `originProduct.stockQuantity` | recorded | PACKET, REQ, REG |
| category | `originProduct.leafCategoryId` (no value type is recorded) | recorded | REG |
| seller keys | `originProduct.detailAttribute.sellerCodeInfo.sellerManagementCode`, `.sellerBarcode`, `.sellerCustomCode1`, `.sellerCustomCode2` | recorded | FIELDS, REQ |
| options | `originProduct.detailAttribute.optionInfo.optionCombinationGroupNames.optionGroupName1` … `optionGroupName4`; `originProduct.detailAttribute.optionInfo.optionCombinations[]` with `id`, `optionName1` … `optionName4`, `stockQuantity`, `price`, `usable`, `sellerManagerCode` (combination form only; the simple, custom and standard forms' keys are not captured) | recorded | PACKET, FIELDS, REQ |
| shipping | `originProduct.deliveryInfo.deliveryType`, `.deliveryAttributeType`; `originProduct.deliveryInfo.deliveryFee.deliveryFeeType`, `.baseFee`, `.freeConditionalAmount`, `.deliveryFeePayType` | recorded | REQ, REG |
| seller customer service | `originProduct.detailAttribute.afterServiceInfo.afterServiceTelephoneNumber`, `.afterServiceGuideContent` (the seller's own A/S contact; owner `6190978708` Q6) | recorded | REQ |
| media | `originProduct.images.representativeImage.url`; `originProduct.images.optionalImages[].url` | recorded | PACKET, FIELDS |
| price (discount) | the immediate discount's value, unit and period, and a discounted price: only the container name `originProduct.customerBenefit` is recorded, none of its members | `NOT_CAPTURED` | — |
| seller keys | `modelName`, brand and manufacturer names | `NOT_CAPTURED` | — |
| category | `wholeCategoryName` of the listing (the category list's own member is not the origin read's) | `NOT_CAPTURED` | — |
| time (E5) | provider registration and modification times of the origin read | `NOT_CAPTURED` | — |

Content digests (§5.3: `detailContent`, the notice block, the tag set) are not taken in S1a: they
serve the change detection of S2b, which takes them with its own evidence check. Every
`NOT_CAPTURED` row above stays out of the retention profile and out of storage until an official
capture records its path; S1b shows such a field as 「데이터 없음」, never as a guess.

### ICBM adoption and runtime state — ICBM-side, not provider facts

- `SMARTSTORE_ORIGIN_PRODUCT_READ_V2` and `SMARTSTORE_CHANNEL_PRODUCT_READ_V2` are `ADOPTED` (M5 PR-D; `ENDPOINT_MATRIX.md` §4, §4.1). ICBM policy: connect `5s`, read `15s`; redirect `NO_FOLLOW`; success predicate HTTP 200 AND the body parses as a JSON object (`m5d-origin-read-r1`, `m5d-channel-read-r1`); no query keys (deny-by-default); retained response fields `name`, `salePrice`, `stockQuantity`, `sellerManagementCode`, `sellerManagerCode`, `url` (safe-retention profile `smartstore-safe-retention/v1`, ADR-0014 §15). The origin read also retains `statusType` and `channelProductDisplayStatusType` and reads them at exactly the two documented paths above (mapping revision `m5-published-state-r1`); the channel read does not. Reading them proves no published state by itself: it is proven only against the explicit expectation of the Snapshot's own CREATE projection, exactly `SALE/ON` (ICBM-side decision, Issue #89 architect resolution `5915900049` D1; ADR-0014 §11; `ENDPOINT_MATRIX.md` §4.1).
- Runtime: `UNVERIFIED`; execution is `DRY_RUN`/provider-zero. A missing read-back resource never proves `NOT_APPLIED_PROVEN` (`ERRORS.md` §10.4).
- **ADR-0042 S1a amendment (`ENDPOINT_MATRIX.md` §4.1.2; mapping revision `s1a-store-catalog-r1`).** The origin read's retained fields widen by the leaves of the "recorded" rows of the store-catalog table above, and nothing else. The store-catalog normalizer (`integrations/marketplaces/smartstore/catalog.py`, `smartstore-catalog-fields/v1`) reads each at exactly its path; the same leaf name anywhere else in the response is ignored. The REGISTER read-back normalizer and its comparison are unchanged.

## Coupang

- **Locator:** https://developers.coupang.com/en/api/products/querying-product — `OFFICIAL_API_DOC`, observed 2026-10-03.
- **Method / path:** `GET /v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{sellerProductId}`.
- **Identity response:** each `data.items[]` exposes `sellerProductItemId` (needed to modify option values), `vendorItemId` (immutable lowest-level option ID; null while temporarily saved), `externalVendorSku`, and provider exposure IDs including `productId`/`itemId`. `productId` can change through merge/separation and is not a canonical key.
- **Option/value response:** item name, prices, stock and attributes are returned; attribute rows include `exposed` and `editable`. The page warns that query results can include auto-extracted/recommended attributes and empty recommended values.
- **Comparison consequence:** compare the expected seller-controlled item-code set and intended purchase-option values through a Coupang-specific normalizer; do not require raw full-attribute-array equality. A null `vendorItemId` cannot complete operational item identity.
- **Missing:** read-after-write/approval timing, complete response nesting retained by an adapter, absence semantics, all errors and rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Locator:** https://openapi.11st.co.kr/ — no endpoint reference captured (2026-09-28).
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/articles/4578918482447- (상품 조회) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **By product ID:** `GET /v1/store/product?productId={productId}`; an unmatched product ID returns `404`.
- **By seller code:** `GET /v1/store/product/store_managed_code?code={code}`; returns an array because the seller code is explicitly not unique; no match returns an empty array ([PRODUCT_SEARCH](PRODUCT_SEARCH.md#kakao-shopping)).
- **Missing:** response field list/envelope; other error statuses; rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/20 — `OFFICIAL_API_DOC` (Product 2.0), observed 2026-09-28.
- **Method / URL:** `GET https://sa2.esmplus.com/item/v1/goods/{goodsNo}` (master `goodsNo`).
- **Missing:** response envelope and site-specific product-number fields; errors; rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured:** no general-seller single-product read contract (2026-09-28).
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locators:** New product guide https://eapi.ssgadm.com/info/online/item/itemRegisterAndSearch.ssg ; migration notice https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1255790911 — observed 2026-09-28 (PR #141 pass).
- **Captured:** the New online product API includes product detail/read by `itemId`.
- **Missing:** the New read method/path and every request/response/error fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
