# Cancel

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | OFFICIAL_CAPTURED | `POST /v1/pay-order/seller/product-orders/:productOrderId/claim/cancel/request`. Research-only source capture; not registered/adopted in the strict SmartStore ledger. |
| Coupang | OFFICIAL_CAPTURED | Official order/return families expose order cancel and return/cancel request lists. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | Claim API documents seller/buyer cancellation flows; some successful actions are status-only responses. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | Claim-management family exists; exact cancel endpoint not captured. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Order/claim APIs exist; exact cancellation endpoint not captured. |
| SSG.COM | PARTIAL_OFFICIAL_CAPTURED | Return/exchange shipping APIs are captured; distinct cancellation endpoint not captured. |

Sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-request-cancel-pay-order-seller
- Coupang: https://developers.coupang.com/en/api
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578928106639-클레임-API-명세
