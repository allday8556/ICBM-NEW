# Tracking / Waybill

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | PARTIAL_OFFICIAL_CAPTURED | Tracking/dispatch information belongs to the order-shipment family; exact read contract not captured here. |
| Coupang | OFFICIAL_CAPTURED | Shipments & Orders family includes tracking upload and shipment status. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | Shipment registration accepts courier/invoice data; order reads return delivery data. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | Delivery family exists; exact tracking endpoint not captured. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Order data includes shipment-related fields; tracking endpoint not captured. |
| SSG.COM | OFFICIAL_CAPTURED | Shipping/return APIs use waybill fields and shipping/recollection state. |

Sources:
- Coupang: https://developers.coupang.com/en/api/shipments
- Kakao shipment: https://shopping-developers.kakao.com/hc/ko/articles/4578935244559-배송상품-발송처리
- SSG return/exchange: https://eapi.ssgadm.com/info/shpp/listExchangeTarget.ssg
