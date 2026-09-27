# Q&A / Customer Inquiry

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | OFFICIAL_CAPTURED | `GET /v1/contents/qnas` lists product inquiries; official family includes answer create/update. |
| Coupang | OFFICIAL_CAPTURED | Customer Service family exposes product inquiry reads/replies and call-center inquiry APIs. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | OFFICIAL_CAPTURED | `GET /v1/store/qna` lists product Q&A by qnaId/productId/answer state. |
| Gmarket / Auction | PARTIAL_OFFICIAL_CAPTURED | API guide lists CS inquiry-response management; exact endpoint not captured. |
| LotteON | OFFICIAL_CAPTURED | `POST https://openapi.lotteon.com/v1/openapi/customer/v1/getSellerInquiryList`. |
| SSG.COM | OFFICIAL_CAPTURED | Q&A list: `POST /api/postng/qnaList.ssg`; answer: `POST /api/postng/ansQna.ssg`. |

Sources:
- SmartStore: https://apicenter.commerce.naver.com/docs/commerce-api/current/get-comments-contents
- Coupang: https://developers.coupang.com/en/api/cs
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578925770511-상품문의-목록-조회
- LotteON: https://ecapi.lotteon.com/apiService/?apiNo=179&menuIdx=8
- SSG: https://eapi.ssgadm.com/info/itemQnaApi.ssg
