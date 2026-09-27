# Exchange

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | OFFICIAL_CAPTURED | Exchange family covers collection completion, redelivery, hold/release and rejection/withdrawal. |
| Coupang | OFFICIAL_CAPTURED | Exchange family exposes request list, receipt confirmation, rejection and waybill upload. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | Claim API covers exchange request, collection, hold/withdrawal and redelivery waybill flow. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | Claim family exists; exact exchange endpoint not captured. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Claim APIs exist; exact general-seller exchange endpoint not captured. |
| SSG.COM | OFFICIAL_CAPTURED | Return/exchange recollection API captured at `/api/pd/{version}/listExchangeTarget.ssg`. |

Sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/commerce-api/current/교환
- Coupang API index: https://developers.coupang.com/en/api
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578928106639-클레임-API-명세
- SSG: https://eapi.ssgadm.com/info/shpp/listExchangeTarget.ssg
