# Option / Variant

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Standard-option metadata and product option structures are preserved in SmartStore evidence. |
| Coupang | OFFICIAL_CAPTURED | Product creation requires option information; `vendorItemId` is the immutable option-level management key. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | ProductRequest supports option types; product registration docs provide option-type examples. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | 2.0 goods support master/site product and option structures; exact option-metadata endpoint not captured. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Product consists of one or more items; option requirements vary by category. |
| SSG.COM | OFFICIAL_CAPTURED | New product API includes option/price area; official notices describe option attribute model changes. |

Sources:
- Coupang: https://developers.coupang.com/en/getting-started/coupang-open-api
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578940635791-상품-등록-및-수정
- Gmarket/Auction: https://etapi.gmarket.com/20
- SSG: https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055
