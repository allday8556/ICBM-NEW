# Claim — cross-claim status

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | PARTIAL_OFFICIAL_CAPTURED | Current provider docs expose claim status plus cancel/return/exchange families. Research-only source note; not registered/adopted in the strict SmartStore ledger. |
| Coupang | OFFICIAL_CAPTURED | Returns and Exchanges are first-class API families; order API also exposes cancellation. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | Claim API unifies cancellation, exchange and return using `claimId` and `orderIds`. |
| Gmarket / Auction | OFFICIAL_CAPTURED | ESM API guide explicitly lists claim management as an API family. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Order schema includes claim number and related fields; dedicated claim endpoint capture remains. |
| SSG.COM | OFFICIAL_CAPTURED | Shipping/return APIs expose recollection, return/exchange reasons and status. |

Sources:
- SmartStore upstream: https://apicenter.commerce.naver.com/docs/commerce-api/current
- Gmarket/Auction API guide: https://etapi.gmarket.com/pages/API-가이드
- LotteON order: https://ecapi.lotteon.com/apiService/?apiNo=80&menuIdx=6
- SSG return/exchange: https://eapi.ssgadm.com/info/shpp/listExchangeTarget.ssg
