# Shipment / Dispatch

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | OFFICIAL_CAPTURED | Official 발주/발송 family supports dispatch processing; exact dispatch path should be re-read before adoption. |
| Coupang | OFFICIAL_CAPTURED | `POST /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/orders/invoices` uploads waybills. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | `POST /v1/shopping/orders/deliveries/invoices`; max 100 waybills; processing is asynchronous and docs advise read-back delay. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | Delivery-management family exists; exact shipment endpoint not captured. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Shipping APIs exist; exact seller shipment-write endpoint not captured. |
| SSG.COM | OFFICIAL_CAPTURED | Shipping workflow is exposed under `/api/pd/{version}`; shipping-instructed orders are explicitly documented. |

Sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/commerce-api/current/발주-발송-처리
- Coupang: https://developers.coupang.com/en/api/shipments
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578935244559-배송상품-발송처리
- SSG: https://eapi.ssgadm.com/info/shpp/listShppDirection.ssg
