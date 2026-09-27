# Product Read

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Existing read-back endpoints are owned by SmartStore strict ledger. |
| Coupang | PARTIAL_OFFICIAL_CAPTURED | Official product API family exists; exact current single-product read contract not frozen here. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Product API family exists; endpoint detail not captured. |
| Kakao Talk Store | OFFICIAL_CAPTURED | `GET /v1/store/product?productId={productId}`; unmatched product ID returns 404. |
| Kakao by seller code | OFFICIAL_CAPTURED | `GET /v1/store/product/store_managed_code?code={code}`; returns array because seller code is not unique. |
| Gmarket / Auction | OFFICIAL_CAPTURED | `GET https://sa2.esmplus.com/item/v1/goods/{goodsNo}`. |
| LotteON | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured in this pass. |
| SSG.COM | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |

Sources:
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578918482447-상품-조회
- Gmarket/Auction: https://etapi.gmarket.com/
