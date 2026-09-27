# Return

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | OFFICIAL_CAPTURED | `POST /v1/pay-order/seller/product-orders/:productOrderId/claim/return/request`; reason and collection method are documented. |
| Coupang | OFFICIAL_CAPTURED | Returns family includes list/single read, approval, receive confirmation and pickup waybill. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | Claim API covers return request, collection complete, hold, withdrawal, pickup invoice and approval/refund. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | Claim family exists; exact return endpoint not captured. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Claim APIs exist; exact general-seller return endpoint not captured. |
| SSG.COM | OFFICIAL_CAPTURED | `POST /api/pd/{version}/listExchangeTarget.ssg` retrieves return/exchange recollection cases. |

Sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-request-return-pay-order-seller
- Coupang: https://developers.coupang.com/en/api/returns
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578928106639-클레임-API-명세
- SSG: https://eapi.ssgadm.com/info/shpp/listExchangeTarget.ssg
