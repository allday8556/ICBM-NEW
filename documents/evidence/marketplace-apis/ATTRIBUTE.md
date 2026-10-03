# Attribute

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_RELEASE_NOTE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locators:** `카테고리별 속성 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/get-attribute-list-product ; `카테고리별 속성값 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/get-attribute-value-list-product — `OFFICIAL_API_DOC`, NAVER Commerce API 2.89.0 (2026-09-15), `NAVER-P0-PACKET-289` (Issue #89 comment 5746489554).
- **Method / path:** `GET /v1/product-attributes/attributes`; `GET /v1/product-attributes/attribute-values`.
- **Auth / group:** `Authorization: Bearer {token}`; the AI-use guide groups attribute reads under API group `상품`.
- **Missing:** the category query key both reads need, every response field, errors, rate limit; how attribute values are carried in the CREATE body.
- **ICBM:** `SMARTSTORE_PRODUCT_ATTRIBUTE_LIST` / `_VALUES` are `NOT_ADOPTED` (`ENDPOINT_MATRIX.md` §4.1: the category query key is not named); runtime `UNVERIFIED`.

## Coupang

- **Locators:** https://developers.coupang.com/en/api/categories/category-metadata-query and https://developers.coupang.com/en/api/products/product-creation — `OFFICIAL_API_DOC`, observed 2026-10-03.
- **Metadata endpoint:** `GET /v2/providers/seller_api/apis/api/v1/marketplace/meta/category-related-metas/display-category-codes/{displayCategoryCode}`. Attribute rows carry `attributeTypeName`, `dataType` (`STRING | NUMBER | DATE` in the field table), `basicUnit`, `usableUnits`, `required` (`MANDATORY | OPTIONAL`), `groupNumber` (`NONE | 1 | 2`) and `exposed` (`EXPOSED` purchase option, `NONE` search attribute). The official Korean guide examples additionally carry `inputType` (`INPUT | SELECT`) and `inputValues`.
- **CREATE projection:** `items[].attributes[]` contains `attributeTypeName` (max 25) and `attributeValueName` (max 30, unit included). Values and units must match the exact category metadata; current policy disallows an unknown free purchase-option type.
- **Lifecycle:** metadata attributes may be added/modified/deleted before sales approval; after approval they may be added but not deleted or modified. Product read-back may include auto-extracted/recommended attributes, including empty recommended values, so raw-array equality is not a safe comparison contract.
- **Missing:** live all-category rows, response enum completeness, the guide/reference difference for `inputType`/`inputValues`, and the required group-rule contradiction recorded in [OPTION](OPTION.md#coupang).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/categories/4406596840975-API-Docs — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** product data types expose category/model/brand/manufacturer-driven attributes.
- **Missing:** attribute lookup endpoint and keys.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/20 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the product schema includes product/site attributes; no metadata endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiNo=171&menuIdx=4 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** product create is category-driven; no attribute lookup contract captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055 — `OFFICIAL_RELEASE_NOTE`, observed 2026-09-28 (PR #141 pass).
- **Captured:** the New product API includes a dedicated product-attribute area.
- **Missing:** its method/path and fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
