# Product Read

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `ADOPTED` (origin and channel read-backs) | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

### Provenance

- **Locators:** `(v2) 원상품 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/read-origin-product-product ; `(v2) 채널 상품 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/read-channel-product-1-product ; support https://github.com/commerce-api-naver/commerce-api/discussions/3170
- **Authority / version:** `OFFICIAL_API_DOC`, NAVER Commerce API 2.89.0 (2026-09-15); `OFFICIAL_SUPPORT` for the identity-stability line. Source IDs `NAVER-P0-PACKET-289` (5746489554), `NAVER-P0-PRODUCT-READ`, `NAVER-P0-REVIEW-CREATE-289` (5768199984). Below, `PACKET` = 5746489554 and `REVIEW` = 5768199984.

### Provider contract

| Field | Origin-product read | Channel-product read | Source |
| --- | --- | --- | --- |
| Method / path | `GET /v2/products/origin-products/{originProductNo}` | `GET /v2/products/channel-products/{channelProductNo}` | PACKET |
| Auth | `Authorization: Bearer {token}` | same | PACKET; `NAVER-P0-AUTH` |
| API group | `상품` | `상품` | PACKET |
| Request body / Content-Type | none | none | `ENDPOINT_MATRIX.md` §4.1 |
| Response structure | the `원상품 정보 구조체` schema is used for read responses as well as registration (see [PRODUCT_CREATE](PRODUCT_CREATE.md#request-structure) for its captured keys) | not captured | PACKET |
| Lookup semantics | `originProductNo` and `channelProductNo` are provider-issued unique numbers, stable across later product edits (`OFFICIAL_SUPPORT` #3170) — a deterministic single-item lookup once the number is known, not a way to find a product whose CREATE response was lost | same | REVIEW |
| Errors | the read reference documents product error examples used by `ERRORS.md` (`NAVER-P0-PRODUCT-READ`); API-server `NOT_FOUND` is resource context, distinct from `GW.NOT_FOUND` (`ERRORS.md` §9.3, §10.4) | same | `ERRORS.md` |

### Unresolved (exact)

- The channel-product read response structure and the JSON paths of every retained field in either response.
- The endpoint-specific error statuses/codes of both reads.
- Any read-after-write consistency window after a mutation (`ERRORS.md` Q5).
- Endpoint-specific rate limits.

### ICBM adoption and runtime state — ICBM-side, not provider facts

- `SMARTSTORE_ORIGIN_PRODUCT_READ_V2` and `SMARTSTORE_CHANNEL_PRODUCT_READ_V2` are `ADOPTED` (M5 PR-D; `ENDPOINT_MATRIX.md` §4, §4.1). ICBM policy: connect `5s`, read `15s`; redirect `NO_FOLLOW`; success predicate HTTP 200 AND the body parses as a JSON object (`m5d-origin-read-r1`, `m5d-channel-read-r1`); no query keys (deny-by-default); retained response fields `name`, `salePrice`, `stockQuantity`, `sellerManagementCode`, `sellerManagerCode`, `url` (safe-retention profile `smartstore-safe-retention/v1`, ADR-0014 §15).
- Runtime: `UNVERIFIED`; execution is `DRY_RUN`/provider-zero. A missing read-back resource never proves `NOT_APPLIED_PROVEN` (`ERRORS.md` §10.4).

## Coupang

- **Locator:** https://developers.coupang.com/en/api — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the Products family contains product, list and item reads.
- **Missing:** the method/path and every request/response fact of a single-product read.
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
