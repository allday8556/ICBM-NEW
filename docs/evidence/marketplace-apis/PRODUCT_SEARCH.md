# Product Search / Listing Lookup

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Current strict ledger proves only that `POST /v1/products/search` exists for this candidate; request-schema filters, strong duplicate keys, uniqueness/completeness and zero-result absence are not proven. See `docs/platforms/smartstore/ENDPOINT_MATRIX.md` §4.1. |
| Coupang | OFFICIAL_CAPTURED | Official index exposes product list paging and lookup by `externalVendorSkuCode`. Do not infer uniqueness/completeness from endpoint existence. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Current seller-side listing/search contract not captured. |
| Kakao Talk Store | OFFICIAL_CAPTURED | Seller-code lookup returns an array; seller code is explicitly not unique; no match returns an empty array. |
| Gmarket / Auction | OFFICIAL_CAPTURED | `POST https://sa2.esmplus.com/item/v1/goods/search`; filters include goodsNo/siteGoodsNo/managedCode/siteId/keyword; documented max 20 calls/minute. |
| LotteON | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | General-seller product-search contract not captured. |
| SSG.COM | PARTIAL_OFFICIAL_CAPTURED | New online product guide includes registration/search surface; exact search request/response contract not frozen here. |

Do not interpret an empty search response as remote absence unless exact provider completeness/freshness semantics are documented.

Sources:
- SmartStore upstream: https://apicenter.commerce.naver.com/docs/commerce-api/current/search-product
- SmartStore strict evidence: `docs/platforms/smartstore/ENDPOINT_MATRIX.md`
- Coupang: https://developers.coupang.com/en/api
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578918482447-상품-조회
- ESM: https://etapi.gmarket.com/160
- SSG: https://eapi.ssgadm.com/info/online/item/itemRegisterAndSearch.ssg
