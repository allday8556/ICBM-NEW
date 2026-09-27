# Error Semantics

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Product/image APIs document HTTP 308/400/401/403/404/500 and JSON `code/message/invalidInputs/timestamp`; SmartStore ERRORS.md remains authoritative. |
| Coupang | PARTIAL_OFFICIAL_CAPTURED | APIs use HTTP/JSON and HMAC; endpoint-specific errors exist, but no cross-endpoint taxonomy is frozen here. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | Common guide: success is HTTP 200; failures use non-200 status with common ErrorMessage JSON. |
| Gmarket / Auction | OFFICIAL_CAPTURED | Auth guide shows authorization failures may be blocked before the target API and gives HTTP 401 payload example. |
| LotteON | OFFICIAL_CAPTURED | Captured APIs return result code/message fields in endpoint-specific response envelopes. |
| SSG.COM | OFFICIAL_CAPTURED | Captured EAPI responses use `resultCode`, `resultMessage`, `resultDesc`; required-field failures are endpoint-specific. |

Sources:
- SmartStore image/error example: https://apicenter.commerce.naver.com/docs/commerce-api/current/upload-product
- Kakao common guide: https://shopping-developers.kakao.com/hc/ko/sections/6592286270223-API-공통-가이드
- Gmarket/Auction: https://etapi.gmarket.com/pages/API-가이드
- LotteON order: https://ecapi.lotteon.com/apiService/?apiNo=80&menuIdx=6
- SSG: https://eapi.ssgadm.com/info/shpp/listShppDirection.ssg
