# Option / Variant

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` + `OFFICIAL_RELEASE_NOTE` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_RELEASE_NOTE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locators:** `카테고리별 표준형 옵션 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/get-standard-option-by-category-product ; `원상품 정보 구조체` schema (see [PRODUCT_CREATE](PRODUCT_CREATE.md#provenance)) — `OFFICIAL_API_DOC`, NAVER Commerce API 2.89.0 (2026-09-15); `NAVER-P0-PACKET-289` (5746489554), `NAVER-P0-FIELDS-CREATE-289` (5861477977), `NAVER-P0-REQUIRED-CREATE-289` (5861933729), `NAVER-P0-REGISTRATION-CREATE-289` (5862400626).
- **Standard-option metadata:** `GET /v1/options/standard-options`; bearer; API group `상품`.
- **Option structure in the product body:** `originProduct.detailAttribute.optionInfo` (not globally required; simple and combination forms cannot be mixed). The combination form's keys, required/optional rules, defaults and limits (`NAVER-P0-REQUIRED-CREATE-289`, Issue #89 comment 5861933729, among others) are recorded once in [PRODUCT_CREATE § Request structure](PRODUCT_CREATE.md#request-structure); this file does not repeat them.
- **Missing:** the category query key of the standard-option read and its response fields; the keys of the simple, custom and standard option structures; errors; rate limit.
- **ICBM:** `SMARTSTORE_STANDARD_OPTIONS` is `NOT_ADOPTED` (`ENDPOINT_MATRIX.md` §4.1: category query key not named); the option body is part of the CREATE request, adopted as a contract only (`ENDPOINT_MATRIX.md` §4.1.1); only an option shape the canonical ICBM contracts already allow may be projected (Issue #89 comment 5861477977 §C). Runtime `UNVERIFIED`.

## Coupang

- **Locators:** category metadata https://developers.coupang.com/en/api/categories/category-metadata-query ; Korean guide https://developers.coupang.com/ko/getting-started/guide-to-creating-product-listings ; CREATE https://developers.coupang.com/en/api/products/product-creation ; current open-option policy https://developers.coupang.com/en/notices/notice-on-changes-to-all-category-purchase-options-sepember-9-2024 — `OFFICIAL_API_DOC` + `OFFICIAL_RELEASE_NOTE`, observed 2026-10-03.
- **Metadata contract:** `data.attributes[]` exposes `attributeTypeName`, `dataType`, `basicUnit`, `usableUnits`, `required`, `groupNumber` and `exposed`; the official guide examples also show `inputType` and `inputValues`. `exposed=EXPOSED` is a purchase option, while `NONE` is a search attribute. `groupNumber=NONE` is independent; equal non-`NONE` group numbers are alternative attribute types from which one is chosen.
- **Required rules captured:** `MANDATORY + NONE + EXPOSED` is a required axis. The Korean guide and FAQ state `MANDATORY + group 1 + EXPOSED` is a required choice group. The English guide says `OPTIONAL` for that second case, and group `2` requiredness is not independently documented; those cases stay `AMBIGUOUS` until reviewed evidence resolves them.
- **CREATE shape:** `items[]` is the vendor-item option list, maximum 200. Each item carries `attributes[]` with `attributeTypeName` (max 25) and `attributeValueName` (max 30, including its unit); at least one value is required and an exposed attribute cannot be added when every item has the same value. `itemName` is unique and max 150. `bundleType=AB` cannot configure options.
- **Policy overlay:** from 2024-10-10, free/open purchase options are not a fallback; only purchase options present in category metadata may be registered for the target categories. Mandatory-option enforcement was strengthened in 2026; missing/invalid values may fail CREATE or restrict exposure.
- **Identity:** `vendorItemId` is the immutable lowest option-level key. `externalVendorSku` is seller-controlled per item and returned in orders; its captured length, charset, uniqueness and reuse contract remain missing.
- **Detailed audit:** [Track C packet](../../reviews/COUPANG-OPTION-SKU-COMMON-CORE-BY-CODEX.md).
- **Missing:** live all-category metadata, the official-language contradiction, group `2` required semantics, constant-required-value behavior, `externalVendorSku` wire limits and read-after-write timing.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/articles/4578940635791- (상품 등록 및 수정) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `ProductRequest` supports option types; the registration docs give option-type examples.
- **Missing:** option keys, types and required rules.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/20 — `OFFICIAL_API_DOC` (Product 2.0), observed 2026-09-28.
- **Captured:** 2.0 goods support master/site product and option structures; no option-metadata endpoint captured.
- **Missing:** option keys and required rules.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiNo=171&menuIdx=4 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** a product consists of one or more items; option requirements vary by category.
- **Missing:** item/option keys and rules.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055 — `OFFICIAL_RELEASE_NOTE`, observed 2026-09-28 (PR #141 pass).
- **Captured:** the New product API includes an option/price area; official notices describe option-attribute model changes.
- **Missing:** method/path and option fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
