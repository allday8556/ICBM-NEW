# Product Search / Listing Lookup

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.
>
> An empty search response is never remote absence unless the provider documents exact completeness/freshness semantics. No platform below does.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

### Provenance

- **Official locators:** `상품 목록 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/search-product (NAVER Commerce API 2.89.0, 2026-09-15).
- **Official support (`OFFICIAL_SUPPORT`, NAVER `P1`):** #1828 SELLER_CODE matching https://github.com/commerce-api-naver/commerce-api/discussions/1828 ; #3164 SELLER_CODE request fields https://github.com/commerce-api-naver/commerce-api/discussions/3164 ; #3170 identities and `sellerManagementCode` ownership https://github.com/commerce-api-naver/commerce-api/discussions/3170 ; api-agency #874 pagination https://github.com/commerce-api-naver/api-agency/discussions/874
- **Source IDs / reviews:** `NAVER-P0-PACKET-289` (5746489554); `NAVER-P0-REVIEW-CREATE-289` (5768199984, 5768247290 — the reviews that read the support answers above); research packet 1 (Issue #140 comment 5857814524).

### Provider contract

| Field | Provider fact | Source |
| --- | --- | --- |
| Method / path | `POST /v1/products/search` (`상품 목록 조회`) | PACKET (5746489554) |
| Auth / group | `Authorization: Bearer {token}`; API group `상품` | 5746489554 |
| Request Content-Type | JSON under the Commerce API default (JSON except file upload/download); not separately captured for this endpoint | 5768199984 |
| Seller-code search inputs | `searchKeywordType: SELLER_CODE` together with `sellerManagementCode: <value>` | `OFFICIAL_SUPPORT` #3164 (5768247290); research packet 1 |
| Other conditions | when searching by seller-management code or product number, other search conditions are not applied to the result | `OFFICIAL_SUPPORT` (5768199984) |
| Match semantics | `SELLER_CODE` search may return products whose `sellerManagementCode` is **similar, partially matching or exactly matching**; exact-only matching is explicitly not guaranteed | `OFFICIAL_SUPPORT` #1828 (5768199984, 5768247290) |
| Key uniqueness | `sellerManagementCode` is seller-authored origin-product data, at most 30 characters; no uniqueness per seller account, origin product, channel or active catalogue is documented, and no duplicate rejection | `OFFICIAL_SUPPORT` #3170 (5768199984, 5768247290) |
| Pagination / count | `page` / `size` enumerate products and `totalElements` is the count; more than 100,000 matching products may return an error | `OFFICIAL_SUPPORT` api-agency #874 (5768247290) |
| Freshness / completeness | no read-after-CREATE consistency, indexing-delay bound, snapshot semantics or completeness-at-query-time is documented; `totalElements = 0` is only a response result | 5768247290 |
| Provider identities | `originProductNo` / `channelProductNo` are provider-issued unique numbers, stable across edits | `OFFICIAL_SUPPORT` #3170 (5768199984) |

### Unresolved (exact)

- The full request schema: filters other than `searchKeywordType` / `sellerManagementCode`, including whether barcode/GTIN or a name filter exists (5746489554 left the strong-key question open).
- The response item structure and whether each item carries `originProductNo` / `channelProductNo` for read-back.
- Endpoint error statuses/codes and rate limit.
- Any exact-match, uniqueness, completeness or freshness guarantee (documented as absent).

### ICBM adoption and runtime state — ICBM-side, not provider facts

- `SMARTSTORE_PRODUCT_SEARCH` is `NOT_ADOPTED` (`ENDPOINT_MATRIX.md` §4, §4.1); runtime `UNVERIFIED`. The deterministic-reconcile review closed `INSUFFICIENT` (ADR-0014 §17.2).
- A later, separately authorized slice may adopt it for **positive-only** reconcile: an exact client-side comparison against the `smartstore-seller-management-code/v1` projection ([PRODUCT_CREATE](PRODUCT_CREATE.md#icbm-adoption-and-runtime-state--icbm-side-not-provider-facts); ruling R1, Issue #89 comment 5861607665) is only a positive candidate; zero results never prove absence; multiple exact candidates stay `REVIEW_REQUIRED`; a single exact candidate still needs provider-ID read-back.

## Coupang

- **Locator:** https://developers.coupang.com/en/api — `OFFICIAL_API_DOC`, observed 2026-09-28; list paging page https://developers.coupang.com/en/api/products/product-list-paging-query (research packet 1).
- **Captured:** the official index exposes product list paging and a lookup by `externalVendorSkuCode`.
- **Missing:** method/path and request/response of both; uniqueness/completeness/freshness semantics (do not infer them from endpoint existence).
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
