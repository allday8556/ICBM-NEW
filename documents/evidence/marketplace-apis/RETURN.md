# Return

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
| SSG.COM | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locator:** https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-request-return-pay-order-seller — `OFFICIAL_API_DOC`, current reference observed 2026-09-28 (research packet 1 and PR #141 pass; version not recorded by that capture).
- **Method / path:** `POST /v1/pay-order/seller/product-orders/{productOrderId}/claim/return/request`.
- **Request:** a return reason and a collection method are documented request inputs; their exact keys and values were not captured.
- **Missing:** API group of this endpoint, exact request keys/enums, success/response, errors, idempotency/replay, timeout semantics, rate limit.
- **ICBM:** not registered in `ENDPOINT_MATRIX.md` §4 → `NOT_ADOPTED`; runtime `UNVERIFIED`.

## Coupang

- **Locator:** https://developers.coupang.com/en/api/returns/return-cancellation-request-list-query — `OFFICIAL_API_DOC`, observed 2026-10-03.
- **Method / path:** `GET /v2/providers/openapi/apis/api/v6/vendors/{vendorId}/returnRequests` lists return/cancellation requests.
- **Item identity:** `data[].returnItems[]` includes `vendorItemId` (the page states return requests are submitted by it), `vendorItemName`, `cancelCount`, `purchaseCount`, `shipmentBoxId`, `sellerProductId` and seller product name. Partial return is possible, so the return quantity must be retained.
- **Canonical consequence:** the immutable provider option ID resolves to exactly one persisted MarketplaceRegistrationItem; product and option names are diagnostics only.
- **Missing:** complete query/paging/error contract, single-return read details, processing mutations, idempotency and rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Captured (https://shopping-developers.kakao.com/hc/ko/articles/4578928106639-, 2026-09-28):** the Claim API covers return request, collection complete, hold, withdrawal, pickup invoice and approval/refund.
- **Missing:** method/paths and fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Captured (2026-09-28):** claim/return APIs are documented on the official portal https://etapi.gmarket.com/category; no return endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured (2026-09-28):** claim APIs exist; no general-seller return endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/shpp/listExchangeTarget.ssg — `OFFICIAL_API_DOC`, observed 2026-09-28 (PR #141 pass).
- **Captured:** `POST /api/pd/{version}/listExchangeTarget.ssg` retrieves return/exchange recollection cases; the shipping/return APIs expose recollection, return/exchange reasons and status.
- **Missing:** host, request keys, response fields, and any return-processing (write) endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
