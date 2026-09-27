# Order Confirm / Acknowledge

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | OFFICIAL_CAPTURED | `POST /v1/pay-order/seller/product-orders/confirm`; max 30 product orders per request. Research-only source capture; not registered/adopted in the strict SmartStore ledger. |
| Coupang | OFFICIAL_CAPTURED | `PATCH /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/ordersheets/acknowledgement` changes status to Product in Preparation. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | PARTIAL_OFFICIAL_CAPTURED | Order-processing family exists; exact seller acknowledgement endpoint not captured in this pass. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | Order-management family exists; exact acknowledgement endpoint not captured. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Order integration status fields are documented; exact acknowledgement endpoint not captured. |
| SSG.COM | PARTIAL_OFFICIAL_CAPTURED | Shipping-direction workflow is captured; separate acknowledgement endpoint not captured. |

Sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-confirm-placed-product-orders-pay-order-seller
- Coupang API index: https://developers.coupang.com/en/api
