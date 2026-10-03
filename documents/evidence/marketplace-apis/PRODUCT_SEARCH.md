# Product Search / Listing Lookup

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint: the SmartStore adoption below is recorded by `ENDPOINT_MATRIX.md` §4.1.2.
>
> An empty search response is never remote absence unless the provider documents exact completeness/freshness semantics. No platform below does.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `ADOPTED` (positive-only reconcile; `ENDPOINT_MATRIX.md` §4.1.2) | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

### Provenance

- **Official locators:** `상품 목록 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/search-product (NAVER Commerce API 2.89.0, 2026-09-15).
- **Official support (`OFFICIAL_SUPPORT`, NAVER `P1`):** #1828 SELLER_CODE matching https://github.com/commerce-api-naver/commerce-api/discussions/1828 ; #3164 SELLER_CODE request fields https://github.com/commerce-api-naver/commerce-api/discussions/3164 ; #3170 identities and `sellerManagementCode` ownership https://github.com/commerce-api-naver/commerce-api/discussions/3170 ; api-agency #874 pagination https://github.com/commerce-api-naver/api-agency/discussions/874
- **Source IDs / reviews:** `NAVER-P0-PACKET-289` (5746489554); `NAVER-P0-REVIEW-CREATE-289` (5768199984, 5768247290 — the reviews that read the support answers above); research packet 1 (Issue #140 comment 5857814524); `NAVER-P0-SEARCH-289` (Issue #89 comment 5904349289, the architect's S1–S4 resolution of the official reference; `SOURCES.md` §5.3).

Below, `SEARCH` = 5904349289.

### Provider contract

| Field | Provider fact | Source |
| --- | --- | --- |
| Method / path | `POST /v1/products/search` (`상품 목록 조회`) | PACKET (5746489554), SEARCH (S1) |
| Auth / group | `Authorization: Bearer {token}`; API group `상품` | 5746489554 |
| Request Content-Type | `application/json`; the body is a required object | SEARCH (S1) |
| Seller-code search inputs | `searchKeywordType` (string enum including `SELLER_CODE`) together with `sellerManagementCode` (string) | SEARCH (S1); `OFFICIAL_SUPPORT` #3164 (5768247290) |
| Pagination inputs | `page` `integer<int32>`, **first page 1**, default 1; `size` `integer<int32>`, default 50, **maximum 500** | SEARCH (S1) |
| Other filters | not required for a seller-code search; none is sent merely to narrow a result | SEARCH (S1) |
| Other conditions | when searching by seller-management code or product number, other search conditions are not applied to the result | `OFFICIAL_SUPPORT` (5768199984) |
| Success | HTTP `200`, `application/json;charset=UTF-8` | SEARCH (S2) |
| Response top level | `contents` (`object[]`), `page` (`int32`), `size` (`int32`), `totalElements` (`int64`), `totalPages` (`int32`), `first` (`boolean`), `last` (`boolean`) | SEARCH (S2) |
| `contents[n]` | `originProductNo` (`int64`) and `channelProducts` (`object[]`); support confirms `contents[n].originProductNo` is returned | SEARCH (S2) |
| `contents[n].channelProducts[m]` | `originProductNo` (`int64`), `channelProductNo` (`int64`), `channelServiceType` (`STOREFARM`, `WINDOW` or `AFFILIATE`), `sellerManagementCode` (string) | SEARCH (S2) |
| Documented statuses | `200`, `308`, `400`, `401`, `403`, `404`, `500`; the gateway additionally defines `429` `GW.RATE_LIMIT` / `GW.QUOTA_LIMIT` and gateway/service failures (503/504 classes) | SEARCH (S3) |
| Rate / quota | no fixed endpoint-specific numeric limit; enforcement is dynamic, with rate/quota headers, and a `429` rate or quota refusal is an authoritative refusal | SEARCH (S4) |
| Match semantics | `SELLER_CODE` search may return products whose `sellerManagementCode` is **similar, partially matching or exactly matching**; exact-only matching is explicitly not guaranteed | `OFFICIAL_SUPPORT` #1828 (5768199984, 5768247290) |
| Key uniqueness | `sellerManagementCode` is seller-authored origin-product data, at most 30 characters; no uniqueness per seller account, origin product, channel or active catalogue is documented, and no duplicate rejection | `OFFICIAL_SUPPORT` #3170 (5768199984, 5768247290) |
| Large results | more than 100,000 matching products may return an error; the endpoint-specific status/code of that error is **not documented** | `OFFICIAL_SUPPORT` api-agency #874 (5768247290); SEARCH (S3) |
| Freshness / completeness | no read-after-CREATE consistency, indexing-delay bound, snapshot semantics or completeness-at-query-time is documented; `totalElements = 0` is only a response result | 5768247290 |
| Provider identities | `originProductNo` / `channelProductNo` are provider-issued unique numbers, stable across edits | `OFFICIAL_SUPPORT` #3170 (5768199984) |

### Unresolved (exact)

- The endpoint-specific status/code of the more-than-100,000-results error (not invented; any undocumented error proves nothing).
- Any exact-match, uniqueness, completeness or freshness guarantee (documented as absent).

### ICBM adoption and runtime state — ICBM-side, not provider facts

- `SMARTSTORE_PRODUCT_SEARCH` is `ADOPTED` for **positive-only reconcile only** (`ENDPOINT_MATRIX.md` §4, §4.1.2; ADR-0020 §4 order 2; ADR-0014 §28.2–§28.4). Adoption is a contract, never a call: execution stays `DRY_RUN`, the lookup reads the CONNECT owner's committed bearer (ROADMAP §14 item 4), so without a proven current committed session every lookup is `UNAVAILABLE`, and runtime stays `UNVERIFIED`. The deterministic-reconcile review closed `INSUFFICIENT` (ADR-0014 §17.2) and is not overturned.
- It is read exactly as above and no wider: the seller-code search carries the `smartstore-seller-management-code/v1` projection ([PRODUCT_CREATE](PRODUCT_CREATE.md#icbm-adoption-and-runtime-state--icbm-side-not-provider-facts); ruling R1, Issue #89 comment 5861607665); every page within a bounded read budget is enumerated and checked for consistency; only a `STOREFARM` channel entry whose code is **exactly** the projection is a candidate. A partial or inconsistent enumeration is never a count.
- What a lookup may conclude: zero exact candidates never prove absence; multiple exact candidates stay `REVIEW_REQUIRED`; a single exact candidate is only an identity-recovery candidate, which becomes presence (`APPLIED_PROVEN`) only when the adopted origin read-back by its `originProductNo` carries the same code. `contents[n].originProductNo` is the read-back identity and the `STOREFARM` `channelProductNo` the channel identity; both are persisted (Issue #89 5904349289 §B). An unavailable, rate- or quota-refused, failed or undocumented answer proves nothing.
- It is **never** a duplicate lookup and never authorizes a CREATE: duplicate evidence stays fail-closed (ADR-0014 §13, §17.2).

## Coupang

- **Locators:** external seller-code query https://developers.coupang.com/en/api/products/query-a-summary-of-product-info ; list paging https://developers.coupang.com/en/api/products/product-list-paging-query — `OFFICIAL_API_DOC`, observed 2026-10-03.
- **Seller-code method / path:** `GET /v2/providers/seller_api/apis/api/v1/marketplace/seller-products/external-vendor-sku-codes/{externalVendorSkuCode}`. Coupang says to wait at least one minute after product creation.
- **Response:** `data` is an array of vendor products, including `sellerProductId`, product name, display category, vendor, sale dates, brand and status (`IN_REVIEW | SAVED | APPROVING | APPROVED | PARTIAL_APPROVED | DENIED | DELETED`).
- **Safety consequence:** an array result does not establish uniqueness; no complete freshness or zero-result absence guarantee is documented. At most it supplies positive candidates that still require provider-ID product read and Snapshot comparison.
- **Missing:** uniqueness, completeness, zero-result meaning, stable freshness/read-after-write beyond the one-minute minimum, errors and rate limit; Track C did not complete the general list-paging contract.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Locator:** https://openapi.11st.co.kr/ — no endpoint reference captured (2026-09-28).
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/articles/4578918482447- (상품 조회) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Seller-code lookup:** `GET /v1/store/product/store_managed_code?code={code}` returns an array; the seller code is explicitly **not unique**; no match returns an empty array.
- **Missing:** completeness/freshness semantics (an empty array is not absence); response item fields; rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/160 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Method / URL:** `POST https://sa2.esmplus.com/item/v1/goods/search`.
- **Filters include:** `goodsNo`, `siteGoodsNo`, `managedCode`, `siteId`, keyword.
- **Rate limit:** documented maximum 20 calls/minute for this endpoint only.
- **Missing:** exact request/response keys and paging; match/uniqueness/completeness/freshness semantics; errors.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured:** no general-seller product-search contract (2026-09-28).
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/online/item/itemRegisterAndSearch.ssg — observed 2026-09-28 (PR #141 pass).
- **Captured:** the New online product guide includes a registration/search surface.
- **Missing:** the search method/path and every request/response fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
