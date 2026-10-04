# Category

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `LIST_COMPLETE` | `CATEGORY_LIST_ADOPTED` | `PROVIDER_READ_PENDING` |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locators:** `전체 카테고리 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/get-category-list-product ; `카테고리 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/get-category-product — `OFFICIAL_API_DOC`, NAVER Commerce API 2.90.0, re-read 2026-10-04.
- **Method / path:** `GET /v1/categories` (all categories); `GET /v1/categories/{categoryId}` (one category).
- **Auth / group:** `Authorization: Bearer {token}`; the AI-use guide groups category reads under API group `상품`.
- **In the product body:** `originProduct.leafCategoryId` is **required on product registration** (`NAVER-P0-REGISTRATION-CREATE-289`, Issue #89 comment 5862400626; [PRODUCT_CREATE](PRODUCT_CREATE.md#request-structure)).
- **Adopted list contract:** optional boolean query `last`; with `last=true`, HTTP 200 returns an array whose required fields are `wholeCategoryName`, `id`, `name`, `last`. Bearer authentication; API group `상품`; retained fields are exactly those four.
- **Still missing/out:** the richer single-category certification response is not needed by the list snapshot and `SMARTSTORE_CATEGORY_READ` stays `NOT_ADOPTED`; rate-limit behavior remains runtime measurement.
- **ICBM:** `SMARTSTORE_CATEGORY_LIST` is `ADOPTED` for read-only leaf-catalog synchronization; `SMARTSTORE_CATEGORY_READ` remains `NOT_ADOPTED`. No real provider read has yet been recorded as acceptance evidence.

## Coupang

- **Locator:** https://developers.coupang.com/en/api — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the Categories family includes a metadata query by `displayCategoryCode` and category recommendation.
- **Missing:** method/path and request/response of both.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/articles/4578910215055-ProductRequest — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `ProductRequest` requires a leaf `categoryId` obtained from category lookup.
- **Missing:** the category-lookup method/path and response.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/category — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** category APIs are documented on the official portal and product management is category-driven; the exact category API was not captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiNo=171&menuIdx=4 (affiliate product registration) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** product create requires a standard category plus one or more mapped display categories.
- **Missing:** category-lookup endpoints and the request keys; general-seller applicability.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/item/listStdCtgKeyPath.ssg — `OFFICIAL_API_DOC`, observed 2026-09-28 (PR #141 pass).
- **Captured:** standard category lookup path `/venInfo/{version}/listStdCtgKeyPath.ssg`.
- **Missing:** method, host, request/response fields, errors.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
