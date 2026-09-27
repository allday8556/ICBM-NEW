# Rate Limit / Volume Limit

Only endpoint-specific facts actually captured are recorded.

| Platform | Status | Captured fact |
| --- | --- | --- |
| SmartStore | UNVERIFIED | No general seller API rate-limit contract captured in this pass. |
| Coupang | UNVERIFIED | No global requests-per-second/day limit captured from the official index in this pass. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | PARTIAL_OFFICIAL_CAPTURED | Shipment invoice registration limits one request to 100 waybills and processing is asynchronous. |
| Gmarket / Auction | OFFICIAL_CAPTURED | Product list search documents max 20 calls/minute. Do not generalize this to other endpoints. |
| LotteON | UNVERIFIED | No global rate-limit contract captured. |
| SSG.COM | UNVERIFIED | No global rate-limit contract captured. |

Sources:
- Kakao shipment: https://shopping-developers.kakao.com/hc/ko/articles/4578935244559-배송상품-발송처리
- ESM product search: https://etapi.gmarket.com/160
