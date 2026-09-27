# Product Create

> Research-only. SmartStore adoption remains owned by its strict ledger.

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | `POST /v2/products`; JSON request; documented HTTP 200 success. |
| Coupang | OFFICIAL_CAPTURED | `POST /v2/providers/seller_api/apis/api/v1/marketplace/seller-products`. Official product guide requires category/logistics prerequisites before create. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Official Open API Center/Product API family exists; current seller create endpoint detail not publicly captured. |
| Kakao Talk Store | OFFICIAL_CAPTURED | `POST /v1/store/product/register`; form or JSON; HTTP 200 returns `productId`. |
| Gmarket / Auction | OFFICIAL_CAPTURED | `POST https://sa2.esmplus.com/item/v1/goods`; one master `goodsNo` maps site-specific product numbers. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Official page: `POST https://openapi.lotteon.com/v1/openapi/product/v1/product/registration/request`; page is labelled “(계열사) 상품 등록”, so seller eligibility is not generalized. |
| SSG.COM | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | No public current create contract captured. |

Official sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product
- Coupang: https://developers.coupang.com/en/api/products/product-creation
- Coupang listing guide: https://developers.coupangcorp.com/hc/ko/article_attachments/360054636091
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578940635791-상품-등록-및-수정
- Gmarket/Auction: https://etapi.gmarket.com/
- LotteON: https://ecapi.lotteon.com/apiService/?apiMjrVerCd=V1&apiMnrVerNm=1.0&apiNm=(계열사)%20상품%20등록&apiNo=171&menuIdx=4
- 11st: https://openapi.11st.co.kr/openapi/OpenApiFrontMain.tmall
