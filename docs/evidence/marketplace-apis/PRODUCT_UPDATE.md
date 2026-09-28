# Product Update

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_RELEASE_NOTE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Captured (NAVER Commerce API 2.89.0, `NAVER-P0-PACKET-289` / Issue #89 comment 5746489554):** the `원상품 정보 구조체` schema is used for request/response in product registration, read **and update** (captured keys: [PRODUCT_CREATE](PRODUCT_CREATE.md#request-structure)); the AI-use guide places product modification in API group `상품`.
- **Missing:** the update method/path(s), request Content-Type, whether a full or partial body is required, success status/response, errors, idempotency/replay and timeout semantics.
- **ICBM:** no UPDATE endpoint is registered in `ENDPOINT_MATRIX.md` §4 → `NOT_ADOPTED`; runtime `UNVERIFIED`. The M5 PR-D packet excluded UPDATE/DELETE adoption (Issue #89 comment 5746489554).

## Coupang

- **Locator:** https://developers.coupang.com/en/api — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `PUT .../seller-products/{sellerProductId}/partial` (path prefix elided in the capture) plus item-level price/quantity changes ([PRICE](PRICE.md#coupang), [STOCK](STOCK.md#coupang)).
- **Missing:** the full path prefix, request body, success/response, errors, idempotency.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (portal https://openapi.11st.co.kr/ returned no endpoint reference, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/articles/4578940635791- (상품 등록 및 수정) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `POST /v1/store/product/update`; the full-update flow requires all product information, not only the changed fields.
- **Missing:** request field list, success/response, errors, idempotency.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/20 — `OFFICIAL_API_DOC` (Product 2.0), observed 2026-09-28.
- **Captured:** `PUT https://sa2.esmplus.com/item/v1/goods/{goodsNo}`; the official guide recommends feature-specific APIs for partial changes (for example [PRICE](PRICE.md#gmarket--auction)).
- **Missing:** request body and full/partial rule, success/response, errors, idempotency.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured:** no general-seller update contract (2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055 — `OFFICIAL_RELEASE_NOTE`, observed 2026-09-28 (PR #141 pass).
- **Captured:** the New online product API splits updates by area: basic, shipping, detail, attributes, notice, option/price, price and sale status.
- **Missing:** the method/path and request/response of each area.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
