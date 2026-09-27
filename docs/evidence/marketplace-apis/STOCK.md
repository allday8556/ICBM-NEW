# Stock

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Stock quantity is part of product contracts; exact update path must follow SmartStore adoption truth. |
| Coupang | OFFICIAL_CAPTURED | Item quantity update: `PUT .../marketplace/vendor-items/{vendorItemId}/quantities/{quantity}`. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | PARTIAL_OFFICIAL_CAPTURED | Product structures expose stock quantity; standalone stock endpoint not captured. |
| Gmarket / Auction | OFFICIAL_CAPTURED | Price/stock update surface: `PUT .../goods/{goodsNo}/sell-status`. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Stock APIs exist in the API center; captured page was affiliate additional-product stock, not a general-seller contract. |
| SSG.COM | PARTIAL_OFFICIAL_CAPTURED | New product API includes sale-state/option-price areas; exact standalone stock endpoint not captured. |

Sources:
- Coupang API index: https://developers.coupang.com/en/api
- ESM notice: https://etapi.gmarket.com/182
- SSG New product notice: https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055
