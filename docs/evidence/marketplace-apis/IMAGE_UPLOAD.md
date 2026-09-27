# Image Upload

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | OFFICIAL_CAPTURED | `POST /v1/product-images/upload`; multipart/form-data; max 10 JPG/GIF/PNG/BMP; returns image URLs. Research-only catalog summary; strict SmartStore ledger remains authoritative for adoption/runtime status. |
| Coupang | PARTIAL_OFFICIAL_CAPTURED | Product create schema contains image fields; no standalone upload endpoint captured. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | PARTIAL_OFFICIAL_CAPTURED | Product docs require image-upload API while preparing create data; exact upload endpoint not captured here. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | Product schema carries image data; standalone upload contract not captured. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Captured product create accepts downloadable content-file URLs; standalone upload contract not captured. |
| SSG.COM | PARTIAL_OFFICIAL_CAPTURED | New product API includes media/detail areas; standalone upload contract not captured. |

Sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/commerce-api/current/upload-product
- Coupang: https://developers.coupang.com/en/api/products/product-creation
- Kakao API docs: https://shopping-developers.kakao.com/hc/ko/categories/4406596840975-API-Docs
- LotteON create example: https://ecapi.lotteon.com/apiService/?apiNo=171&menuIdx=4
- SSG New product notice: https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055
