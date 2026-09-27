# Product Search / Listing Lookup

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | `POST /v1/products/search` exists; filter availability does not prove uniqueness/completeness or zero-result absence. |
| Coupang | PARTIAL_OFFICIAL_CAPTURED | Official Product List Paging Query exists; exact contract not frozen here. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Current seller-side listing/search contract not captured. |
| Kakao Talk Store | OFFICIAL_CAPTURED | Seller-code lookup returns array; seller code is explicitly not unique; no match returns empty array. |
| Gmarket / Auction | OFFICIAL_CAPTURED | Product search endpoint `https://sa2.esmplus.com/item/v1/goods/search`; 2026 official notice changes its call rate. |
| LotteON | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| SSG.COM | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |

Do not interpret an empty search response as remote absence unless exact provider completeness/freshness semantics are documented.

Sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/commerce-api/current/search-product
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578918482447-상품-조회
- ESM: https://etapi.gmarket.com/
