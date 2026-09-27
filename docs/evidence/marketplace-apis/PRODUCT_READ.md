# Product Read

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Existing origin/channel read-back endpoints are owned by the SmartStore strict ledger. |
| Coupang | PARTIAL_OFFICIAL_CAPTURED | Official Products family contains product/list/item reads; exact single-product read contract is not frozen here. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Product API portal exists; current endpoint detail not captured. |
| Kakao Talk Store | OFFICIAL_CAPTURED | `GET /v1/store/product?productId={productId}`; unmatched product ID returns 404. |
| Kakao by seller code | OFFICIAL_CAPTURED | `GET /v1/store/product/store_managed_code?code={code}`; returns an array because seller code is explicitly not unique. |
| Gmarket / Auction | OFFICIAL_CAPTURED | `GET https://sa2.esmplus.com/item/v1/goods/{goodsNo}`. |
| LotteON | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | General-seller single-product read contract not captured in this pass. |
| SSG.COM | PARTIAL_OFFICIAL_CAPTURED | Official New online product API includes product detail/read by itemId. Exact New read path is not frozen here. |

Sources:
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578918482447-상품-조회
- Gmarket/Auction: https://etapi.gmarket.com/20
- SSG New product guide: https://eapi.ssgadm.com/info/online/item/itemRegisterAndSearch.ssg
- SSG migration notice: https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1255790911
