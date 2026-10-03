# Product Create

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` (fail-closed) — for the M5 SmartStore-only scope the wire facts, nested request field names, the fields required on registration and their accepted values, M5-relevant conditional rules, limits/defaults and the success identifiers' names, top-level position and `integer<int64>` type are captured; some nesting and the `productInfoProvidedNotice` type children are not. Coverage lists them exactly | `ADOPTED` (contract only; `ENDPOINT_MATRIX.md` §4.1.1) | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_RELEASE_NOTE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

This section is the single current field-level representation of the SmartStore CREATE provider contract. `documents/contracts/platforms/smartstore/SOURCES.md` §5.2 (source IDs) and `ENDPOINT_MATRIX.md` §4.2 (adoption/safety state) point here.

### Provenance

| Item | Value |
| --- | --- |
| Official locators | `(v2) 상품 등록`: https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product ; `원상품 정보 구조체` schema: https://apicenter.commerce.naver.com/docs/commerce-api/current/schemas/%EC%9B%90%EC%83%81%ED%92%88-%EC%A0%95%EB%B3%B4-%EA%B5%AC%EC%A1%B0%EC%B2%B4 ; `스마트스토어 채널상품 정보 구조체` schema: https://apicenter.commerce.naver.com/docs/commerce-api/current/schemas/%EC%8A%A4%EB%A7%88%ED%8A%B8%EC%8A%A4%ED%86%A0%EC%96%B4-%EC%B1%84%EB%84%90%EC%83%81%ED%92%88-%EC%A0%95%EB%B3%B4-%EA%B5%AC%EC%A1%B0%EC%B2%B4 ; image upload: https://apicenter.commerce.naver.com/docs/commerce-api/current/upload-product |
| Supporting official support | https://github.com/commerce-api-naver/commerce-api/discussions/3170 (product identities, `sellerManagementCode` ownership); https://github.com/commerce-api-naver/commerce-api/discussions/3467 (upload URL used directly) |
| Authority | `OFFICIAL_API_DOC` (NAVER `P0`) for the wire, structure, status and identifier facts; `OFFICIAL_SUPPORT` (NAVER `P1`) where a line says so |
| Version / observed | NAVER Commerce API **2.89.0 (2026-09-15)**; field-level, required/conditional, registration-requirement/value-level and success-identifier extracts retrieved 2026-09-28 |
| Source IDs | `NAVER-P0-PACKET-289` (5746489554), `NAVER-P0-REVIEW-CREATE-289` (5768199984, 5768247290), `NAVER-P0-FIELDS-CREATE-289` (5861477977), `NAVER-P0-REQUIRED-CREATE-289` (5861933729), `NAVER-P0-REGISTRATION-CREATE-289` (5862400626), `NAVER-P0-VALUES-CREATE-289` (5868542027) — `SOURCES.md` §5.1–§5.2 |

Below, `PACKET` = 5746489554, `REVIEW` = 5768199984 / 5768247290, `FIELDS` = 5861477977, `REQ` = 5861933729, `REG` = 5862400626, `VALUES` = 5868542027. Where `REG` states a registration requirement or a value, it governs: it supersedes `REQ` for the registration requirement of `leafCategoryId` and top-level `stockQuantity`. `VALUES` (E1–E3) confirms the `statusType` registration value and the `naverShoppingRegistration` type that `REG` records, and adds the success identifiers' position and type.

### Wire

| Field | Provider fact | Source |
| --- | --- | --- |
| Method / path | `POST /v2/products` (`(v2) 상품 등록`), relative to the provider base URL `https://api.commerce.naver.com/external` (`ENDPOINT_MATRIX.md` §3) | PACKET, FIELDS |
| Auth | OAuth 2.0 client-credentials bearer token: `Authorization: Bearer {token}`; the Commerce API exposes no OAuth scopes ([AUTH](AUTH.md#smartstore)) | PACKET; `NAVER-P0-AUTH` |
| API group | `상품` (the AI-use guide groups product registration, modification, lookup, deletion and category/attribute reads under `상품`); no narrower permission name is documented | PACKET; `NAVER-P1-PRODUCT-GROUP-1835` |
| Request Content-Type | `application/json` (Commerce API messages are JSON except file upload/download) | REVIEW, FIELDS |

### Request structure

The `원상품 정보 구조체` schema states it is used for request and response in product registration, read and update (PACKET). In the Required column, **required** means the 2.89.0 schema marks the field required (REQ) or the endpoint's own registration rule requires it (REG); an endpoint-specific registration rule overrides a conclusion drawn only from a generic schema badge (REG §1). **not required** means the field is present and neither the schema nor a registration rule requires it; **conditional** gives the documented condition; **not stated** means the captured sources say nothing either way. A structure that is not required is sent only when the RegistrationSnapshot owns its data and the product's conditions call for it; the schema containing a structure is never a reason to send it (REQ).

**`originProduct` and images**

| Path | Required | Limits / default / rule | Source |
| --- | --- | --- | --- |
| `originProduct` | required object | — | FIELDS |
| `originProduct.statusType` | required string | on registration only `SALE` may be entered; `SUSPENSION` supplied on registration is registered as `SALE`; a stock of 0 registers the product `OUTOFSTOCK` regardless of the requested status. Schema values: `WAIT`, `SALE`, `OUTOFSTOCK`, `UNADMISSION`, `REJECTION`, `SUSPENSION`, `CLOSE`, `PROHIBITION`, `DELETE`; the endpoint-specific registration rule controls | REQ, REG, VALUES (E2) |
| `originProduct.name` | required | — | PACKET, REQ |
| `originProduct.detailContent` | required | — | PACKET, REQ |
| `originProduct.images` | required object | — | PACKET, FIELDS, REQ |
| `originProduct.images.representativeImage` | required object | — | FIELDS, REQ |
| `originProduct.images.representativeImage.url` | required string | must be a URL returned by the product-image upload API ([IMAGE_UPLOAD](IMAGE_UPLOAD.md#smartstore)); recommended image size 1000×1000 | PACKET, FIELDS, REQ |
| `originProduct.images.optionalImages` | not required (array) | at most 9 entries | PACKET, FIELDS, REQ |
| `originProduct.images.optionalImages[].url` | required in each entry | URL returned by the product-image upload API | FIELDS, REQ |
| `originProduct.salePrice` | required | at most 999,999,990 | PACKET, REQ |
| `originProduct.detailAttribute` | required object | substructures below | REQ |
| `originProduct.leafCategoryId` | **required on registration** | the official origin-product reference states 상품 등록 시 필수 | REG |
| `originProduct.stockQuantity` | **required on registration** | registration stock is at least 1; at most 99,999,999 | PACKET, REG |
| `originProduct.customerBenefit` and other feature-specific structures | not global CREATE requirements | keep their own feature conditions | REQ |

**`originProduct.deliveryInfo`** — not required globally: omitting it registers a no-delivery product; rental and certain quick-delivery products require it (REQ). The values below are provider facts only: ICBM sends only a delivery shape its canonical preparation/template data owns and never invents a delivery policy (REG §3).

| Path (when `deliveryInfo` is sent) | Required | Rule | Source |
| --- | --- | --- | --- |
| `deliveryInfo.deliveryType` | required | `DELIVERY` or `DIRECT` | REQ, REG |
| `deliveryInfo.deliveryAttributeType` | required | `NORMAL`, `TODAY`, `OPTION_TODAY`, `HOPE`, `TODAY_ARRIVAL`, `DAWN_ARRIVAL`, `ARRIVAL_GUARANTEE`, `SELLER_GUARANTEE`, `HOPE_SELLER_GUARANTEE`, `QUICK`, `PICKUP`, `QUICK_PICKUP` | REQ, REG |
| `deliveryInfo.deliveryFee` | required object | its fields below | REQ, REG |
| `…deliveryFee.deliveryFeeType` | not required | omitted → `FREE`; `FREE`, `CONDITIONAL_FREE`, `PAID`, `UNIT_QUANTITY_PAID`, `RANGE_QUANTITY_PAID` | REG |
| `…deliveryFee.baseFee` | not required | at most 100,000 | REG |
| `…deliveryFee.freeConditionalAmount` | conditional: required in the conditional-free case | — | REG |
| `…deliveryFee.repeatQuantity`, `secondBaseQuantity`, `secondExtraFee`, `thirdBaseQuantity`, `thirdExtraFee` | conditional on the fee mode | — | REG |
| `…deliveryFee.deliveryFeePayType` | not stated | `COLLECT`, `PREPAID`, `COLLECT_OR_PREPAID` | REG |
| `…deliveryFee.deliveryFeeByArea` | conditional: when used | has its own required/conditional subfields (not captured; see Coverage) | REG |
| `deliveryInfo.claimDeliveryInfo` | required object | — | REQ |
| `…claimDeliveryInfo.returnDeliveryFee` | required | — | REQ |
| `…claimDeliveryInfo.exchangeDeliveryFee` | required | — | REQ |
| delivery-mode fields | conditional on the delivery mode | for example `deliveryCompany` for `DELIVERY`; `outboundLocationId` for the documented seller-guarantee delivery attributes | REQ |

**`originProduct.detailAttribute` substructures**

| Path | Required | Limits / default / rule | Source |
| --- | --- | --- | --- |
| `…detailAttribute.sellerCodeInfo` | not required | container for the four seller codes below; none of them is globally required | FIELDS, REQ |
| `…sellerCodeInfo.sellerManagementCode` | not required | seller-authored, origin-product-level; **at most 30 characters** (`OFFICIAL_SUPPORT`, REVIEW); **no uniqueness guarantee** and no documented duplicate rejection (`OFFICIAL_SUPPORT` #3170, REVIEW) | FIELDS, REQ; REVIEW |
| `…sellerCodeInfo.sellerBarcode` | not required | — | FIELDS, REQ |
| `…sellerCodeInfo.sellerCustomCode1`, `…sellerCustomCode2` | not required | — | FIELDS, REQ |
| `…detailAttribute.optionInfo` | not required | if options are used, at least one supported option form is required; simple and combination forms cannot be mixed; the schema also documents custom and standard option structures | FIELDS, REQ |
| `…optionInfo.optionCombinationSortType` | not required (string) | combination form; omitted → `CREATE`; `CREATE`, `ABC`, `LOW_PRICE`, `HIGH_PRICE`; rental products allow only `CREATE` or `ABC` | FIELDS, REG |
| `…optionInfo.optionCombinationGroupNames.optionGroupName1` | conditional: required when the combination structure is used | — | FIELDS, REQ |
| `…optionCombinationGroupNames.optionGroupName2`, `…optionGroupName3` | not stated | ordinary combination options expose up to three option-name dimensions | PACKET, FIELDS |
| `…optionCombinationGroupNames.optionGroupName4` | not stated | branch/location-specific form only (fourth dimension) | PACKET, FIELDS |
| `…optionInfo.optionCombinations[]` | used with the combination form | array of combination rows | FIELDS |
| `…optionCombinations[].optionName1` | required in each entry | — | REQ |
| `…optionCombinations[].optionName2` … `optionName4` | not stated | — | FIELDS |
| `…optionCombinations[].id` | not stated | — | FIELDS |
| `…optionCombinations[].stockQuantity` | not required | default 0; at most 99,999,999 | PACKET, FIELDS, REQ |
| `…optionCombinations[].price` | not required | default 0; at most 999,999,990 | PACKET, FIELDS, REQ |
| `…optionCombinations[].usable` | not required | default `true` | FIELDS, REQ |
| `…optionCombinations[].sellerManagerCode` | not required | — | PACKET, FIELDS, REQ |
| `…optionCombinations[].skuYn` | not required (boolean) | SKU-generation flag; omitted on registration → NAVER records N; for option products the option-level value applies; ignored for an account without the documented fulfillment permission | FIELDS, REG |
| `…detailAttribute.productInfoProvidedNotice` | required for product registration | may be omitted on **update** only when a notice is already stored | PACKET, FIELDS, REQ |
| `…productInfoProvidedNotice.productInfoProvidedNoticeType` | required | discriminator selecting exactly the one matching type-specific child object | FIELDS, REQ |
| `…productInfoProvidedNotice.<type child>` | the one child matching the type | **Not captured (see Coverage below); fail-closed.** Child names include `wear`, `shoes`, `food`, `generalFood`, `dietFood` (examples, not the full list); each type has its own required/conditional field rules; some values are documented "미입력 시 상품상세 참조" and conditional fields are omitted when not applicable, so there is **no blanket fill-every-field rule** | PACKET, FIELDS, REQ |
| `…detailAttribute.afterServiceInfo` | not required | if used, `afterServiceTelephoneNumber` and `afterServiceGuideContent` are required | REQ |
| `…detailAttribute.originAreaInfo` | not required | if used, `originAreaCode` is required; `importer` is required for an imported origin; `content` is required for a direct-input origin | REQ |
| `…detailAttribute` unit-price fields | conditional: required for categories subject to mandatory unit-price display | when `unitPriceYn=true`, `totalCapacityValue`, `unitCapacity` and `indicationUnit` are all required | REQ |
| other category/feature-specific structures | own conditions | keep their own official conditional rules; not generalized to every product | REQ |

**`smartstoreChannelProduct`** (CREATE top level from FIELDS; field rules confirmed independently by the `스마트스토어 채널상품 정보 구조체` schema, REQ)

| Path | Required | Limits / default / rule | Source |
| --- | --- | --- | --- |
| `smartstoreChannelProduct` | required object | — | FIELDS |
| `smartstoreChannelProduct.naverShoppingRegistration` | required boolean | JSON `true` / `false` (네이버 쇼핑 등록 여부); stored as `false` for a non-advertiser account; no string or enum representation exists | FIELDS, REQ, REG, VALUES (E1) |
| `smartstoreChannelProduct.channelProductDisplayStatusType` | required | schema values `WAIT`, `ON`, `SUSPENSION`; writes accept only `ON` or `SUSPENSION` | FIELDS, REQ, REG |
| `smartstoreChannelProduct.channelProductName` | not required | omitted → the origin product name is used | FIELDS, REQ |
| `smartstoreChannelProduct.bbsSeq` | not required | — | FIELDS, REQ |
| `smartstoreChannelProduct.storeKeepExclusiveProduct` | not required | omitted → stored as `false` | FIELDS, REQ |

**`windowChannelProduct` — out of scope.** A separate Shopping Window channel structure, a sibling of `smartstoreChannelProduct`, not a SmartStore-channel field. It is not populated unless the registration unit explicitly targets that channel and that channel's own official contract is separately adopted; it is not part of the M5 SmartStore-only CREATE and is not a blocker for it (FIELDS, REQ).

### Success

| Field | Provider fact | Source |
| --- | --- | --- |
| Status | HTTP `200` | REVIEW, FIELDS |
| Response Content-Type | `application/json;charset=UTF-8` | FIELDS |
| Provider identifiers | `originProductNo`, `smartstoreChannelProductNo`, `windowChannelProductNo` (preserved in the success response since API docs `v2.68.0`), each a **direct top-level member** of the success object and each **`integer<int64>`**; the generated success example shows `originProductNo` and `smartstoreChannelProductNo` at the top level. `windowChannelProductNo` may be absent for a SmartStore-only CREATE. No stringified numeric representation is documented | REVIEW, FIELDS, REQ, VALUES (E3) |
| Stored product | `originProduct`: NAVER states this is the product data SmartStore successfully stored | REVIEW, FIELDS |
| Identity semantics | `originProductNo` and `channelProductNo` are provider-issued unique numbers that stay stable on later product edits (`OFFICIAL_SUPPORT` #3170) | REVIEW |

### Errors

- Documented statuses on the CREATE reference: `200`, `308`, `400`, `401`, `403`, `404`, `500` (FIELDS; research packet 1).
- API-server codes for these statuses: `PERMANENT_REDIRECT` (308), `BAD_REQUEST` (400), `UNAUTHORIZED` (401), `FORBIDDEN` (403), `NOT_FOUND` (404), `INTERNAL_SERVER_ERROR` (500) (`NAVER-P0-PRODUCT-CREATE`; `documents/contracts/platforms/smartstore/ERRORS.md` §5.3, §10).
- `BAD_REQUEST`: the reference warns that `invalidInputs` can be absent or insufficient and says to use `message` to understand the error (research packet 1; `ERRORS.md` §10.1). Structured `invalidInputs` entries carry name/type/message, with types such as `NotEmpty`, `NotValidEnum`, `NumberMax`; a missing required notice field has surfaced this way, and `BAD_REQUEST` can also express provider policy such as a restricted seller tag (`NAVER-P1-BADREQ-NOTICE-1649`, `NAVER-P1-BADREQ-POLICY-3529`; `ERRORS.md` §11).
- Gateway-layer errors (`GW.*`) can precede the API server on any call: [ERRORS](ERRORS.md#smartstore).

### Retry, idempotency and ambiguous outcome — provider facts

- No idempotency key, request-correlation key, replay rule or duplicate-prevention guarantee exists in the CREATE contract (REVIEW, FIELDS).
- No official statement that a timeout, connection loss, response loss or `5xx` proves the mutation was not applied, and no guarantee that repeating the same CREATE after an ambiguous outcome cannot create a second listing (REVIEW).
- The official service guidance to "retry after failure" is not an idempotency or no-effect guarantee (REVIEW).
- No endpoint-specific timeout or consistency window is documented.

### Lookup semantics relevant to CREATE

- A deterministic single-item read exists only once `originProductNo` / `channelProductNo` are known ([PRODUCT_READ](PRODUCT_READ.md#smartstore)); it does not resolve a CREATE whose response was lost before the IDs arrived (REVIEW).
- Seller-code search (`SELLER_CODE`) returns similar, partial or exact matches, with no uniqueness, completeness or freshness guarantee ([PRODUCT_SEARCH](PRODUCT_SEARCH.md#smartstore)).

### Rate limit

No CREATE-specific limit captured; the general gateway rate/quota model applies ([RATE_LIMIT](RATE_LIMIT.md#smartstore)).

### Coverage and remaining gaps (exact)

**Coverage:** `PARTIAL`, fail-closed. **Captured** for the M5 SmartStore-only CREATE scope (`originProduct` + `smartstoreChannelProduct`, combination-form options): the wire facts, the nested request field names, the fields required on registration and their accepted values (including `statusType`, the delivery values and the `deliveryFee` structure, `optionCombinationSortType`, `skuYn`, `naverShoppingRegistration` and the channel display-status write values), the M5-relevant conditional rules, the documented limits/defaults, and the success identifiers' names, top-level position and `integer<int64>` type (PACKET, REVIEW, FIELDS, REQ, REG, VALUES). **Not captured**, so no item below may be invented and any projection that needs one stays fail-closed / `REVIEW_REQUIRED` until the adoption slice records it from the cited schema:

- **`productInfoProvidedNotice` type-specific children** — *captured since 2026-10-03* (notice coverage S0, [NOTICE_SMARTSTORE_S0](NOTICE_SMARTSTORE_S0.md)): the current 2.90.0 reference's type → child mapping and every child's fields, wire types, required badges, deprecation, length bounds and omit semantics, cross-checked with the live notice capture. ICBM projects every documented child from that data (`smartstore-notice-schema/2.90.0-r1`; `ENDPOINT_MATRIX.md` §4.1.1). Still **not captured**: any child of `LODGMENT_RESERVATION`, `TRAVEL_PACKAGE`, `AIRLINE_TICKET` and `RENT_CAR`, and the wire form of `releaseDate` in `SEASON_APPLIANCES`, `OFFICE_APPLIANCES` and `SPORTS_EQUIPMENT` (the reference renders an object while stating `'yyyy-MM'`); ICBM never sends either. Earlier record: evidence packet `5916962285` (`NAVER-P0-NOTICE-CHILD-290`) captured the mapping and seven children's member sets.
- The full delivery-mode conditional list beyond the documented examples (`deliveryCompany`, `outboundLocationId`), the subfields of `deliveryFee.deliveryFeeByArea`, and the exact nesting of the unit-price fields inside `detailAttribute`.
- The keys of the simple, custom and standard option structures (outside the combination form).
- CREATE-specific domain error codes (`ERRORS.md` Q4) and the target/meaning of a `308` for this endpoint.

Two further items are recorded, but are not capture gaps in this scope:

- `windowChannelProduct` fields: out of scope (above).
- CREATE idempotency or ambiguous-outcome replay safety: documented as absent (above).

### ICBM adoption and runtime state — ICBM-side, not provider facts

- **Adoption:** `SMARTSTORE_PRODUCT_CREATE_V2` is `ADOPTED` (`ENDPOINT_MATRIX.md` §4, §4.1.1). Its own adoption slice (ADR-0020 §4 order 1) froze the success predicate, timeout, redirect, error classification, deny-by-default retention profile, the validated and frozen typed request projection and the fail-closed response contract against the facts above, and bumped the endpoint-mapping revision to `m5-create-r1` in the same change; its reconciliation to `VALUES` (E1–E3) bumped it to `m5-create-r2`. **Adoption is a contract, never a call**: no provider request is made, execution stays `DRY_RUN`, the ADR-0018 send-time safety stack refuses every mutation, and the provider-evidence verdict is neither overturned nor re-decided (ADR-0020 SA-05). Every item the Coverage section lists as not captured stays fail-closed, as a named gap that keeps the request unsendable. `VALUES` closes two request facts and no more: `statusType` is projected as `SALE` (E2), and only the *type* of `naverShoppingRegistration` is closed (E1) — which boolean ICBM publishes with has no ICBM-owned value source, so that ICBM-side gap keeps the request unsendable and neither `true` nor `false` is guessed. The channel display status is owned ICBM-side (Issue #89 architect resolution `5915900049` D1): every projection writes `ON`, one of the documented write values; nothing about the provider fact changes. *(Amendment note, Issue #89 architect resolution `5915900049` D2: the boolean is owned ICBM-side as `true` (D2.1), the registration `stockQuantity` as the seed `1` (D2.2), and the notice child is projected for the captured types only (D2.3); the provider facts above are unchanged, and the sentences below record the state before that decision.)* Likewise the registration `stockQuantity` that `REG` requires (at least 1) has no Snapshot or ICBM owner, so it is a named ICBM-side gap and is never sent as a guess. On the response side (E3) the top-level `integer<int64>` identifiers are read. `SMARTSTORE_PRODUCT_SEARCH` was adopted by its own later slice (SA-09), for positive-only reconcile only ([PRODUCT_SEARCH](PRODUCT_SEARCH.md#smartstore); `ENDPOINT_MATRIX.md` §4.1.2).
- **Runtime:** `UNVERIFIED`. `product_registration.write` is `UNVERIFIED`; execution is `DRY_RUN`; a real canary is `BLOCKED`; M5 is `PENDING`. The provider-evidence verdict stays `INSUFFICIENT` for idempotent replay and remote-absence proof (ADR-0014 §17.2).
- **Outcome rules:** a timeout, connection loss, response loss or `5xx` after transport handoff is `UNKNOWN` and a CREATE in `UNKNOWN` is never resent (`ERRORS.md` §14.3, §15.2; ADR-0014 §28). Neither a `500` nor a zero-result lookup proves the product was not registered (ADR-0014 §17.2, §28.2). An ordinary provider `4xx` received after transport handoff is **not** proof of non-application: the diagnostic class may be `AUTH` / `POLICY_BLOCKED` / `VALIDATION` / `TRANSIENT` where evidence supports it, but `remote_outcome` stays `UNKNOWN`, and `NOT_APPLIED_PROVEN` remains whitelist-only (`ERRORS.md` §15; architect ruling R2, Issue #89 comment 5861607665). `308` is never followed for a mutation (`ERRORS.md` §10.6, §17). A success is read at the **top level only** (E3): `originProductNo` and `smartstoreChannelProductNo` must each be a JSON integer in the signed 64-bit range — a boolean, a numeric string, a float or a nested value is never accepted — and `windowChannelProductNo`, when present, likewise; its absence alone never makes the documented SmartStore success unreadable. A readable success is `APPLIED_PROVEN` with `originProductNo` as the read-back identity; that is provider-side application evidence, **never a registration success**, which read-back and Snapshot comparison alone decide (ADR-0014 §11). A success whose identifiers are missing or of another type is `UNKNOWN`, never `NOT_APPLIED_PROVEN` and never resent (`ENDPOINT_MATRIX.md` §4.1.1).
- **Projection boundary:** construct only fields owned by the immutable RegistrationSnapshot/preparation and required by the selected product/channel conditions; do not send an optional structure merely because the schema contains it; unsupported category/feature conditions stay fail-closed / `REVIEW_REQUIRED`. Only an option shape the canonical ICBM contracts already allow may be projected; no value of a type-specific notice child that ICBM does not own may be invented (FIELDS §C, §F; REQ).
- **`sellerManagementCode` projection (architect ruling R1, Issue #89 comment 5861607665 — an ICBM decision, not a NAVER fact; implemented by the CREATE adoption slice):** the internal `listing_identity/v1` (`icbm-` + 32 hex, 37 characters) and ADR-0014 §7 are unchanged. The SmartStore `sellerManagementCode` is the provider projection `smartstore-seller-management-code/v1` = the first **30 lowercase hexadecimal characters** of `SHA-256("smartstore-seller-management-code/v1\0" + listing_identity)`: deterministic, versioned, stable for the listing identity, frozen into the RegistrationSnapshot outbound values and compared exactly on read-back / positive reconcile. It never replaces `listing_identity` locally. An exact match is only a positive candidate: zero results never prove absence, multiple exact candidates stay `REVIEW_REQUIRED`, and a single exact candidate still needs provider-ID read-back. The CREATE adoption slice made the code/ADR changes for R1 and R2: the projection is `integrations/marketplaces/smartstore/product.py`, the read-back comparison checks it exactly, and R2's whitelist-only `NOT_APPLIED_PROVEN` is the outcome classification of `ENDPOINT_MATRIX.md` §4.1.1.

## Coupang

- **Locator:** https://developers.coupang.com/en/api/products/product-creation — `OFFICIAL_API_DOC`, 2026 developer portal, observed 2026-09-28 (research packet 1).
- **Method / path:** `POST /v2/providers/seller_api/apis/api/v1/marketplace/seller-products`.
- **Auth:** HMAC-SHA256 signed request ([AUTH](AUTH.md#coupang)).
- **Content-Type:** the request example is `application/json`.
- **Prerequisites stated by the page:** shipping/return locations, category, koshi/notices and options are part of the create requirements.
- **Request content described:** category, seller product name, vendor, delivery/return info, approval flag, up to 200 items/options, price, stock, `unitCount`, `externalVendorSku`, images, notices, attributes. These are captured as descriptions; exact JSON keys and nesting were not captured except where written in code style.
- **Missing:** exact request keys/nesting and required/conditional rules; success status and response envelope, including the provider product identifier returned; error statuses/body; idempotency/replay; timeout semantics; rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Locator:** https://openapi.11st.co.kr/ — portal reachable, but the public fetch on 2026-09-28 returned no endpoint reference content (research packet 1). Old/third-party endpoint tables are not evidence.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locators:** Talk Store create/update https://shopping-developers.kakao.com/hc/ko/articles/4578940635791- ; gift create https://shopping-developers.kakao.com/hc/ko/articles/4578941029903- — `OFFICIAL_API_DOC`, observed 2026-09-28 (research packet 1).
- **Talk Store create:** `POST /v1/store/product/register`; accepts `application/x-www-form-urlencoded` and `application/json`; documented success HTTP `200`; response carries `productId`.
- **Gift create:** `POST /v1/gift/products/register`; success body returns `id`. Gift API permission is granted separately ([PERMISSIONS](PERMISSIONS.md#kakao-shopping)).
- **Request:** `ProductRequest` requires a leaf `categoryId` from category lookup ([CATEGORY](CATEGORY.md#kakao-shopping)); option types and product-information-notice data types exist ([OPTION](OPTION.md#kakao-shopping), [NOTICE](NOTICE.md#kakao-shopping)); preparing create data requires the image-upload API ([IMAGE_UPLOAD](IMAGE_UPLOAD.md#kakao-shopping)).
- **Auth / errors:** common auth headers ([AUTH](AUTH.md#kakao-shopping)); success HTTP 200, failures non-200 with common ErrorMessage JSON ([ERRORS](ERRORS.md#kakao-shopping)).
- **Eligibility:** API use requires integration review/selection; not every seller or solution provider is eligible.
- **Missing:** full `ProductRequest` field list and required/conditional rules; endpoint-specific error codes; idempotency/replay; timeout semantics; rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locators:** https://etapi.gmarket.com/20 ; portal https://etapi.gmarket.com/category — `OFFICIAL_API_DOC` (ESM Trading API, Product 2.0), observed 2026-09-28 (research packet 1).
- **Method / URL:** `POST https://sa2.esmplus.com/item/v1/goods`.
- **Identity:** one master `goodsNo` owns the site-specific Gmarket/Auction product numbers; keep them distinct.
- **Auth:** JWT HS256 bearer ([AUTH](AUTH.md#gmarket--auction)).
- **Missing:** request Content-Type; request keys and required rules; success status and response envelope; errors; idempotency; timeout semantics; rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiMjrVerCd=V1&apiMnrVerNm=1.0&apiNm=%28%EA%B3%84%EC%97%B4%EC%82%AC%29+%EC%83%81%ED%92%88+%EB%93%B1%EB%A1%9D&apiNo=171&menuIdx=4 — `OFFICIAL_API_DOC`, API version V1 / 1.0, observed 2026-09-28 (research packet 1).
- **Method / URL:** `POST https://openapi.lotteon.com/v1/openapi/product/v1/product/registration/request`.
- **Scope caution:** the page is labelled "(계열사) 상품 등록" (affiliate product registration); its eligibility and contract are not generalized to every seller.
- **Captured request facts:** requires a standard category plus one or more mapped display categories ([CATEGORY](CATEGORY.md#lotteon)); a product consists of one or more items ([OPTION](OPTION.md#lotteon)); accepts downloadable content-file URLs ([IMAGE_UPLOAD](IMAGE_UPLOAD.md#lotteon)).
- **Platform:** `https://openapi.lotteon.com`, RESTful GET/POST over HTTPS, JSON/XML, seller-center OpenAPI key ([AUTH](AUTH.md#lotteon)).
- **Missing:** a general-seller create contract; request keys and required rules; success status/response identifiers; errors; idempotency; timeout; rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locators:** New online product API notice https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055 ; old-version retirement notice https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1255790911 — `OFFICIAL_RELEASE_NOTE`, observed 2026-09-28 (PR #141 pass; research packet 1 captured no SSG endpoint reference).
- **Captured facts:** the New online product create/read/update family launched 2025-04-16; the old online product create/read/update APIs were scheduled to stop after 2026-03-31.
- **Missing:** the New create method/path and every request/response/error fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
