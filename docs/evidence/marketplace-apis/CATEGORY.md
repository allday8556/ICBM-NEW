# Category

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Category list/read endpoints are already preserved in SmartStore P0 evidence. |
| Coupang | OFFICIAL_CAPTURED | Category family includes metadata query by displayCategoryCode and category recommendation. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | ProductRequest requires a leaf `categoryId` obtained from category lookup. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | Product management is category-driven; exact category API was not re-captured here. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Product create requires standard category plus one or more mapped display categories. |
| SSG.COM | OFFICIAL_CAPTURED | Standard category lookup: `/venInfo/{version}/listStdCtgKeyPath.ssg`. |

Sources:
- Coupang: https://developers.coupang.com/en/api
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578910215055-ProductRequest
- LotteON: https://ecapi.lotteon.com/apiService/?apiNo=171&menuIdx=4
- SSG: https://eapi.ssgadm.com/info/item/listStdCtgKeyPath.ssg
