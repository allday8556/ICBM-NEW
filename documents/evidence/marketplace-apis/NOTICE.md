# Product Information Notice

> Evidence catalog; status model and provenance conventions in [README](README.md). The two SmartStore notice reads are adopted, read only, by `ENDPOINT_MATRIX.md` §4.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + provider response | `PARTIAL` | `ADOPTED` (the two notice reads, read only) | `VERIFIED` for the two reads (2026-10-03 capture) |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_RELEASE_NOTE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locators:** `상품정보제공고시 상품군 목록 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/get-all-product-info-provided-notice-type-vo-product ; `상품정보제공고시 상품군 단건 조회` https://apicenter.commerce.naver.com/docs/commerce-api/current/get-product-info-provided-notice-type-vo-product ; `원상품 정보 구조체` schema (see [PRODUCT_CREATE](PRODUCT_CREATE.md#provenance)) — `OFFICIAL_API_DOC`, NAVER Commerce API 2.89.0 (2026-09-15); `NAVER-P0-PACKET-289` (5746489554), `NAVER-P0-FIELDS-CREATE-289` (5861477977), `NAVER-P0-REQUIRED-CREATE-289` (5861933729).
- **Notice-type lookup:** `GET /v1/products-for-provided-notice` (all notice product groups); `GET /v1/products-for-provided-notice/{productInfoProvidedNoticeType}` (one group); bearer; API group `상품`.
- **Notice in the product body:** `originProduct.detailAttribute.productInfoProvidedNotice` (required for registration; omissible on update only when a notice is already stored) with the required discriminator `productInfoProvidedNoticeType` and exactly one matching type child. The rules are recorded once in [PRODUCT_CREATE § Request structure](PRODUCT_CREATE.md#request-structure); the type-child fields are a not-captured, fail-closed gap of that record ([Coverage](PRODUCT_CREATE.md#coverage-and-remaining-gaps-exact)).
- **Error example:** a missing required notice field has produced `BAD_REQUEST` with structured `invalidInputs` (`NAVER-P1-BADREQ-NOTICE-1649`, `OFFICIAL_SUPPORT`).
- **Captured 2026-10-03 (notice coverage S0, owner directive):** the live type list (36 types) and every listed type's content fields (`fieldType`, `fieldName`, `fieldDescription`, `fieldAddDescription`, `fieldMaxLength`; 411 fields), cross-checked against the official OpenAPI `docs/2.0.0-RC.js` (wire type, required, omit semantics, deprecated). The full inventory and every disagreement between the two sources are in [NOTICE_SMARTSTORE_S0](NOTICE_SMARTSTORE_S0.md); the raw retained responses are [smartstore-notice-capture-2026-10-03.json](smartstore-notice-capture-2026-10-03.json). Unspaced reads were answered `429 RATE_LIMITED`; one read a second captured all 36.
- **Missing:** the current (2.90.0) wire schema of every type child — the cross-check uses the 2022 `2.0.0-RC` specification; `RENTAL_HA`'s child, which that specification does not have; errors of the two reads.
- **Captured since 2.90.0** (evidence packet `5916962285`, `NAVER-P0-NOTICE-CHILD-290`): the explicit type → member mapping of 36 types, and the member sets of `WEAR`, `SHOES`, `HOME_APPLIANCES`, `KITCHEN_UTENSILS`, `COSMETIC`, `GENERAL_FOOD` and `ETC` ([PRODUCT_CREATE § Coverage](PRODUCT_CREATE.md#coverage-and-remaining-gaps-exact)). The lookups' response fields are still missing.
- **ICBM:** `SMARTSTORE_NOTICE_TYPES` / `SMARTSTORE_NOTICE_TYPE_READ` are `ADOPTED` read only, to capture the provider notice schema (`ENDPOINT_MATRIX.md` §4; `POST /api/v1/connect/marketplaces/smartstore/notice-catalog/capture`); no value of a type-specific child that ICBM does not own may be invented. The CREATE projection selects the child of the reviewed notice type through the pinned table (`ENDPOINT_MATRIX.md` §4.1.1; Issue #89 architect resolution `5915900049` D2.3); an uncaptured type stays a gap. Runtime `UNVERIFIED`.

## Coupang

- **Locator:** https://developers.coupang.com/en/api/products/product-creation — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** product create requires category-appropriate koshi/notice information.
- **Missing:** notice keys, categories and any lookup endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/categories/4406596840975-API-Docs — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the API docs expose separate product-information-notice data types for Talk Store and Gift.
- **Missing:** type keys and fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/20 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** product registration contains legal/product notice data; no notice lookup endpoint captured.
- **Missing:** notice keys and any lookup endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiNo=171&menuIdx=4 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** product registration is category-driven; no notice lookup contract captured.
- **Missing:** every notice fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1186587593 — `OFFICIAL_RELEASE_NOTE`, observed 2026-09-28 (PR #141 pass).
- **Captured:** the New product API has a dedicated notice area; official notices direct sellers to the notice classification/detail APIs before registration.
- **Missing:** the classification/detail method/paths and fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
