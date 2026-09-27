# Authentication

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | OAuth2 client credentials; requests use `Authorization: Bearer {token}`; Commerce API exposes no OAuth scopes. |
| Coupang | OFFICIAL_CAPTURED | Wing issues Access Key/Secret Key; requests are HMAC-SHA256 signed; IP allowlisting is part of setup. |
| 11st | PARTIAL_OFFICIAL_CAPTURED | Official Open API portal/key issuance is confirmed; endpoint-level auth-header contract was not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | `Authorization: KakaoAK {admin_app_key}`, `Target-Authorization: KakaoAK {seller_app_key}`, `channel-ids`. |
| Gmarket / Auction | OFFICIAL_CAPTURED | JWT HS256 using issued secret; `Authorization: Bearer {JWT}`; master/site seller IDs participate in claims. |
| LotteON | OFFICIAL_CAPTURED | Seller center issues OpenAPI key; HTTPS REST, JSON/XML. |
| SSG.COM | OFFICIAL_CAPTURED | Captured EAPI pages require `Authorization` vendor API authentication key and Accept JSON/XML. |

Sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/auth
- Coupang: https://developers.coupang.com/en/getting-started/open-api-test-guide
- 11st portal: https://openapi.11st.co.kr/
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578909656975-API-공통-인증-헤더-구성
- Gmarket/Auction: https://etapi.gmarket.com/pages/API-가이드
- LotteON: https://ecapi.lotteon.com/apiService/?apiNm=GetStarted
- SSG example: https://eapi.ssgadm.com/info/shpp/listShppDirection.ssg
