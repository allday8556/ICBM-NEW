# Product Create

> Research-only. SmartStore adoption remains owned by its strict platform ledger.

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Current strict ledger records the CREATE candidate and its later wire-contract evidence. Read `docs/platforms/smartstore/ENDPOINT_MATRIX.md` §4.1-§4.2 and `SOURCES.md` §5.2 directly; no adoption is implied here. |
| Coupang | OFFICIAL_CAPTURED | `POST /v2/providers/seller_api/apis/api/v1/marketplace/seller-products`. Official product guide requires category/logistics/notice/option prerequisites before create. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Official Open API portal exists; current seller create endpoint detail not publicly captured. |
| Kakao Talk Store | OFFICIAL_CAPTURED | `POST /v1/store/product/register`; form or JSON; documented HTTP 200 success. |
| Gmarket / Auction | OFFICIAL_CAPTURED | `POST https://sa2.esmplus.com/item/v1/goods`; one master `goodsNo` maps site-specific product numbers. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Captured official page: `POST https://openapi.lotteon.com/v1/openapi/product/v1/product/registration/request`; page is labelled “(계열사) 상품 등록”, so seller eligibility is not generalized. |
| SSG.COM | PARTIAL_OFFICIAL_CAPTURED | Official New online product create/read/update family launched 2025-04-16. Old online product create/read/update APIs were scheduled to stop after 2026-03-31. Exact New create path is not frozen in this research record. |

Official sources:
- SmartStore strict evidence: `docs/platforms/smartstore/ENDPOINT_MATRIX.md`, `docs/platforms/smartstore/SOURCES.md`
- Coupang: https://developers.coupang.com/en/api/products/product-creation
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578940635791-상품-등록-및-수정
- Gmarket/Auction: https://etapi.gmarket.com/20
- LotteON: https://ecapi.lotteon.com/apiService/?apiNo=171&menuIdx=4
- SSG New product notice: https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055
- SSG old-version retirement notice: https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1255790911
- 11st portal: https://openapi.11st.co.kr/
