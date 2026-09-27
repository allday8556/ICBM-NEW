# Product Update

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Consult strict SmartStore ledger before implementation. |
| Coupang | PARTIAL_OFFICIAL_CAPTURED | Official product guide covers creation/modification; exact current update path not frozen here. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Talk Store | OFFICIAL_CAPTURED | `POST /v1/store/product/update`; form or JSON. Official docs warn omitted fields may reset/delete, so callers should read full product state first. |
| Gmarket / Auction | OFFICIAL_CAPTURED | `PUT https://sa2.esmplus.com/item/v1/goods/{goodsNo}`; official guide recommends feature-specific APIs for partial changes. |
| LotteON | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| SSG.COM | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |

Sources:
- Kakao: https://shopping-developers.kakao.com/hc/ko/articles/4578940635791-상품-등록-및-수정
- ESM: https://etapi.gmarket.com/
