# Product Update

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Consult the strict SmartStore ledger before implementation. |
| Coupang | OFFICIAL_CAPTURED | Official index includes `PUT .../seller-products/{sellerProductId}/partial` plus item-level price/quantity changes. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Talk Store | OFFICIAL_CAPTURED | `POST /v1/store/product/update`; full-update flow requires all product information, not only changed fields. |
| Gmarket / Auction | OFFICIAL_CAPTURED | `PUT https://sa2.esmplus.com/item/v1/goods/{goodsNo}`; official guide recommends feature-specific APIs for partial changes. |
| LotteON | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | General-seller update contract not captured. |
| SSG.COM | OFFICIAL_CAPTURED | New online product API splits updates by area: basic, shipping, detail, attributes, notice, option/price, price and sale status. |

Sources:
- Coupang: https://developers.coupang.com/en/api
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578940635791-상품-등록-및-수정
- ESM: https://etapi.gmarket.com/20
- SSG New product notice: https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055
