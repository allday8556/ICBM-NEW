# Product Information Notice

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Registration requires category/type-specific `productInfoProvidedNotice`; blanket fill-every-field is not supported by the accepted evidence. |
| Coupang | OFFICIAL_CAPTURED | Product create requires category-appropriate koshi/notice information. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | API docs expose separate product-information-notice data types for Talk Store/Gift. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | Product registration contains legal/product notice data; exact notice lookup endpoint not captured. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Product registration is category-driven; notice lookup contract not captured in this pass. |
| SSG.COM | OFFICIAL_CAPTURED | New product API has a dedicated notice area; official SSG notices direct sellers to notice classification/detail APIs before registration. |

Sources:
- Coupang: https://developers.coupang.com/en/api/products/product-creation
- Kakao docs: https://shopping-developers.kakao.com/hc/ko/categories/4406596840975-API-Docs
- SSG notice: https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1186587593
