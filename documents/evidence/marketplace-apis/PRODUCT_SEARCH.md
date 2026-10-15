# Product Search / Listing Lookup

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint: the SmartStore adoption below is recorded by `ENDPOINT_MATRIX.md` §4.1.2.
>
> An empty search response is never remote absence unless the provider documents exact completeness/freshness semantics. No platform below does.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `ADOPTED` (positive-only reconcile, and the ADR-0042 store-catalog enumeration; `ENDPOINT_MATRIX.md` §4.1.2) | `UNVERIFIED` |
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

### Store-catalog enumeration evidence (ADR-0042 S1a, tasks E1–E3, E5, E6)

Captured 2026-10-15 for ADR-0042 §4. **The official reference was not re-read**: the
`apicenter.commerce.naver.com` page was refused to the implementing environment (both the web
fetcher and the in-app browser: "not allowed due to safety restrictions"), and no other route to it
was used. Every fact below is a NAVER `P1` official-support answer, read on GitHub on 2026-10-15;
each line names the discussion, the answer's author role and date, and what exactly was observed.
The source IDs are in `SOURCES.md` §5.11 and §6.

| Task | Fact | Status | Source |
| --- | --- | --- | --- |
| E1 | A request **without** `searchKeywordType`, setting only the paging fields `page` and `size`, returns every product registered in the SmartStore: "요청 데이터 내 페이징 관련 필드(`page`, `size`)만 설정하시면 스마트스토어에 등록된 모든 상품을 조회하실 수 있습니다." In the same answer: `totalElements` is the number of products the request found | `CAPTURED` (`OFFICIAL_SUPPORT`) | api-agency #874, NAVER maintainer answer of 2026-04-07 (`NAVER-P1-SEARCH-ALL-874`) |
| E1 | The request body is required but its fields are not; leaving `searchKeywordType` unset, or sending `{}`, lists the products as a whole. When `searchKeywordType` is set, the matching identifier list must be set and no other (`originProductNos` with `CHANNEL_PRODUCT_NO` is refused as `BAD_REQUEST` `invalidInputs` `originProductNos` `Exist`) | `CAPTURED` (`OFFICIAL_SUPPORT`) | commerce-api #1544, NAVER maintainer answer of 2024-03-18 (`NAVER-P1-SEARCH-ALL-1544`) |
| E1 | The list returns every product the request's criteria include, and a `{null}` request lists the products as a whole | `CAPTURED` (`OFFICIAL_SUPPORT`) | commerce-api #1546, NAVER maintainer answer of 2024-03-18 (`NAVER-P1-SEARCH-MODIFIED-1546`) |
| E1 | Whether that "every registered product" includes each `statusType` by name (`SALE`, `OUTOFSTOCK`, `SUSPENSION`, `CLOSE`, `PROHIBITION`, `WAIT`, `UNADMISSION`, `REJECTION`, `DELETE`): no source lists the statuses. A deleted product's inclusion is not stated | `NOT_CAPTURED` — the reference was refused | — |
| E2 | A status filter and a sort order. The names `productStatusTypes`, `orderType` (`NAME`, `NO`), `periodType` (`PROD_REG_DAY`), `fromDate`, `toDate` appear **only in users' own requests** in #248 and #1544; no NAVER answer states their meaning, values or defaults. No stable sort is stated anywhere | `NOT_CAPTURED` — the reference was refused; the user-authored names are not evidence | #248, #1544 (user requests only) |
| E3 | The list's items carry "basic product information (product name, sale status, price, stock, delivery, purchase/benefit and so on)" and nothing more detailed; detail is read through the channel-product or origin-product read. **No field name or JSON path is given** | `PARTIAL` — the existence of the information is captured, its fields are `NOT_CAPTURED` | commerce-api #1635, NAVER maintainer answer of 2024-04-23 (`NAVER-P1-SEARCH-FIELDS-1635`) |
| E5 | The list returns a product modification date named `modifiedDate`, "the date it was modified, whatever the call date". Its position in the response (item or channel product), its format and time zone are **not given**. A registration time is not mentioned | `PARTIAL` — the name only; position, format and the registration time `NOT_CAPTURED` | commerce-api #1546 (`NAVER-P1-SEARCH-MODIFIED-1546`) |
| E6 | More than 100,000 found products returns an error; NAVER advises splitting the query by a search period. **No HTTP status or error code is given** | `NOT_CAPTURED` — the code stays undocumented | api-agency #874 (`NAVER-P1-SEARCH-ALL-874`) |

What the S1a code does with each gap (ICBM-side, fail-closed): the catalog request is exactly
`{"page": n, "size": 500}` (E1), with no status filter and no sort parameter (E2); the search's
retention is **not** widened, so every §5.3 field is read from the origin read — for new listings,
a rolling refresh that keeps each listing's details under 24 hours old, and 「지금 동기화」 (E3,
`ENDPOINT_MATRIX.md` §4.1.2 S1a amendment); no provider
registration or modification time is read or stored (E5); and a provider error at any page,
including the undocumented over-100,000 one, is `ERROR` and concludes nothing (E6). A
`totalElements` above 100,000 on a readable page is `UNAVAILABLE`. That a "every registered
product" enumeration covers every live status is not taken on trust either: a listing the catalog
already knows that is missing from a complete enumeration is read by its origin read, and a live
one (any status but `DELETE`) makes the sync `INCOMPLETE` (ADR-0042 §5.2, `ENDPOINT_MATRIX.md`
§4.1.2 S1a amendment).

### ICBM adoption and runtime state — ICBM-side, not provider facts

- `SMARTSTORE_PRODUCT_SEARCH` is `ADOPTED` for **positive-only reconcile only** (`ENDPOINT_MATRIX.md` §4, §4.1.2; ADR-0020 §4 order 2; ADR-0014 §28.2–§28.4). Adoption is a contract, never a call: execution stays `DRY_RUN`, the lookup reads the CONNECT owner's committed bearer (ROADMAP §14 item 4), so without a proven current committed session every lookup is `UNAVAILABLE`, and runtime stays `UNVERIFIED`. The deterministic-reconcile review closed `INSUFFICIENT` (ADR-0014 §17.2) and is not overturned.
- It is read exactly as above and no wider: the seller-code search carries the `smartstore-seller-management-code/v1` projection ([PRODUCT_CREATE](PRODUCT_CREATE.md#icbm-adoption-and-runtime-state--icbm-side-not-provider-facts); ruling R1, Issue #89 comment 5861607665); every page within a bounded read budget is enumerated and checked for consistency; only a `STOREFARM` channel entry whose code is **exactly** the projection is a candidate. A partial or inconsistent enumeration is never a count.
- What a lookup may conclude: zero exact candidates never prove absence; multiple exact candidates stay `REVIEW_REQUIRED`; a single exact candidate is only an identity-recovery candidate, which becomes presence (`APPLIED_PROVEN`) only when the adopted origin read-back by its `originProductNo` carries the same code. `contents[n].originProductNo` is the read-back identity and the `STOREFARM` `channelProductNo` the channel identity; both are persisted (Issue #89 5904349289 §B). An unavailable, rate- or quota-refused, failed or undocumented answer proves nothing.
- It is **never** a duplicate lookup and never authorizes a CREATE: duplicate evidence stays fail-closed (ADR-0014 §13, §17.2).
- **ADR-0042 S1a amendment (`ENDPOINT_MATRIX.md` §4.1.2).** The same endpoint also carries a second request shape, the store-catalog enumeration: `{"page": n, "size": 500}` and nothing else (E1 above), read only by the StoreCatalog owner (`app/stages/operate/catalog/`). Its response contract, predicate, retention and the reconcile use are unchanged. A complete enumeration (ADR-0042 §5.2) proves the presence of what it read; it is never remote absence for ADR-0014 §10/§14/§28 and, until S3b wires it under ADR-0042 §6, it is not duplicate evidence either.

## Coupang

- **Locators:** external seller-code query https://developers.coupang.com/en/api/products/query-a-summary-of-product-info ; list paging https://developers.coupang.com/en/api/products/product-list-paging-query — `OFFICIAL_API_DOC`, observed 2026-10-03.
- **Seller-code method / path:** `GET /v2/providers/seller_api/apis/api/v1/marketplace/seller-products/external-vendor-sku-codes/{externalVendorSkuCode}`. Coupang says to wait at least one minute after product creation.
- **Response:** `data` is an array of vendor products, including `sellerProductId`, product name, display category, vendor, sale dates, brand and status (`IN_REVIEW | SAVED | APPROVING | APPROVED | PARTIAL_APPROVED | DENIED | DELETED`).
- **Safety consequence:** an array result does not establish uniqueness; no complete freshness or zero-result absence guarantee is documented. At most it supplies positive candidates that still require provider-ID product read and Snapshot comparison.
- **Missing:** uniqueness, completeness, zero-result meaning, stable freshness/read-after-write beyond the one-minute minimum, errors and rate limit; Track C did not complete the general list-paging contract.
- **ICBM:** generally `NOT_ADOPTED`, `UNVERIFIED`. One narrow exception is adopted for
  `C-LIVE-MUT-1` ambiguous-create recovery only: the exact bound `externalVendorSku` may produce a
  positive candidate, which must then pass the provider-ID product read-back and all bound identity
  checks before the journal can advance. Zero, multiple, stale, unavailable or mismatching answers
  prove nothing, never authorize CREATE and leave the mutation barrier intact. This exception does
  not adopt general product search or list paging.

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
