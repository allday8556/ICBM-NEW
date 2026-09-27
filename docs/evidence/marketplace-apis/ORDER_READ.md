# Order Read

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | OFFICIAL_CAPTURED | Conditional product-order read: `GET /v1/pay-order/seller/product-orders`; detailed order-read family is documented. Research-only source capture; not registered/adopted in the strict SmartStore ledger. |
| Coupang | OFFICIAL_CAPTURED | Shipments & Orders family includes daily/minute order lists and single-order lookup; `GET .../vendors/{vendorId}/ordersheets` is indexed. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Current seller order endpoint detail not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | `GET /v1/shopping/order?order_id={order_id}`; bulk order read also exists. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | Official API guide confirms order-management family; exact order-read endpoint not captured here. |
| LotteON | OFFICIAL_CAPTURED | `POST https://openapi.lotteon.com/v1/openapi/order/v1/getOrderList`; returns order/product/shipping fields. |
| SSG.COM | OFFICIAL_CAPTURED | `POST /api/pd/{version}/listShppDirection.ssg` retrieves shipping-instructed orders. |

Sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-get-product-orders-with-conditions-pay-order-seller
- Coupang: https://developers.coupang.com/en/api/shipments
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578918827151-주문정보-조회
- LotteON: https://ecapi.lotteon.com/apiService/?apiNo=80&menuIdx=6
- SSG: https://eapi.ssgadm.com/info/shpp/listShppDirection.ssg
