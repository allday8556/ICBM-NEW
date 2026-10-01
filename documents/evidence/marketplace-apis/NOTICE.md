# Product Information Notice

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
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
- **Missing:** every response field of both lookups (none proven, `SOURCES.md` §5.1); the full list of type values; the field set of each type child; errors; rate limit.
- **Captured since 2.90.0** (evidence packet `5916962285`, `NAVER-P0-NOTICE-CHILD-290`): the explicit type → member mapping of 36 types, and the member sets of `WEAR`, `SHOES`, `HOME_APPLIANCES`, `KITCHEN_UTENSILS`, `COSMETIC`, `GENERAL_FOOD` and `ETC` ([PRODUCT_CREATE § Coverage](PRODUCT_CREATE.md#coverage-and-remaining-gaps-exact)). The lookups' response fields are still missing.
- **ICBM:** `SMARTSTORE_NOTICE_TYPES` / `SMARTSTORE_NOTICE_TYPE_READ` are `NOT_ADOPTED` (`ENDPOINT_MATRIX.md` §4.1) and no discovery call is made; no value of a type-specific child that ICBM does not own may be invented. The CREATE projection selects the child of the reviewed notice type through the pinned table (`ENDPOINT_MATRIX.md` §4.1.1; Issue #89 architect resolution `5915900049` D2.3); an uncaptured type stays a gap. Runtime `UNVERIFIED`.

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
