# Price

| Platform | Status | Captured official fact |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | Price belongs to the product contract; ICBM pricing/adoption rules remain authoritative. |
| Coupang | OFFICIAL_CAPTURED | Item price update: `PUT .../marketplace/vendor-items/{vendorItemId}/prices/{price}`; original-price endpoint also exists. |
| 11st | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED | Not captured. |
| Kakao Shopping | PARTIAL_OFFICIAL_CAPTURED | Product models expose sale price; standalone price endpoint not captured. |
| Gmarket / Auction | OFFICIAL_CAPTURED | `PUT https://sa2.esmplus.com/item/v1/goods/{goodsNo}/sell-status` is documented for price/stock/sale-period changes. |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | Price fields are present in product/order schemas; standalone price endpoint not captured. |
| SSG.COM | OFFICIAL_CAPTURED | New product API includes a dedicated price area. |

Sources:
- Coupang API index: https://developers.coupang.com/en/api
- ESM notice: https://etapi.gmarket.com/182
- SSG New product notice: https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055
