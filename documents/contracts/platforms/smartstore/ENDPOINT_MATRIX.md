# SmartStore Endpoint Matrix

## Status

| Field | Value |
| --- | --- |
| Provider | NAVER SmartStore / Commerce API |
| Contract status | `M5_CREATE_SEARCH_READBACK_AND_IMAGE_UPLOAD_ADOPTED_FROZEN_FOR_REVIEW` |
| M2 integration mode | `OWN_STORE_SELF` |
| Adopted endpoint count | `7` (2 M2 CONNECT + 2 M5 read-backs + 1 M5 image upload + 1 M5 product CREATE + 1 M5 product search) |
| M5 endpoints | `5 ADOPTED (2 read-back, 1 image upload, 1 product CREATE, 1 product search), 7 NOT_ADOPTED with recorded gaps` |
| Runtime verification | `PENDING` |
| Upstream version | `2.88.0` (M2 rows) / `2.89.0` (M5 PR-D rows, packet 5746489554; M5 image upload, Issue #89 decisions 5765557497 and 5765663972) |
| Retrieved at | `2026-09-14` |
| Verified at | `null` |
| Review due | `2026-10-14` |

## Provenance

Primary upstream documentation:

- https://apicenter.commerce.naver.com/docs/commerce-api/current
- https://apicenter.commerce.naver.com/docs/auth
- https://apicenter.commerce.naver.com/docs/commerce-api/current/exchange-sellers-auth
- https://apicenter.commerce.naver.com/docs/commerce-api/current/get-account-info-by-account-no-sellers
- https://apicenter.commerce.naver.com/docs/restful-api
- https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product and its `원상품 정보 구조체` schema (release 2.89.0, read through the Issue #89 official evidence reviews 5768199984 and 5768247290, the field-level packet 5861477977, the required/conditional-field packet 5861933729 and the registration-requirement and value-level packet 5862400626; `SOURCES.md` §5.2)
- https://www.rfc-editor.org/rfc/rfc6749
- https://www.rfc-editor.org/rfc/rfc6750

Secondary official technical-support evidence:

- https://github.com/commerce-api-naver/commerce-api/discussions/780
  - `내스토어 애플리케이션` uses `type=SELF` only.
  - one own-store application is connected to one SmartStore account.
- https://github.com/commerce-api-naver/commerce-api/discussions/2425
  - `accountUid` and `accountId` identify a SmartStore uniquely under current provider guidance.
  - `accountUid` is the Commerce API integration-oriented identifier.
- https://github.com/commerce-api-naver/commerce-api/discussions/3751
  - own-store applications use `type=SELF`.
  - `account_id` MUST NOT be included for `type=SELF`.
  - token payload fields must be sent in `application/x-www-form-urlencoded` body format.

Freshness:

- Upstream version, endpoint path/method, auth mode, response schema, permission mapping, redirect behavior, or application-mode changes trigger immediate review.
- `review_due` is a fallback when automated upstream-change detection is absent or broken.
- Documentation inspection does not populate `verified_at`.

---

## 1. Purpose

This file is the SmartStore endpoint allow-list adopted by ICBM.

It is not a catalog of every NAVER endpoint.

Core rule:

`documented by provider != adopted by ICBM`

Only `ADOPTED` endpoints may be reachable from the applicable production integration path.

`NOT_ADOPTED` means no network call, even when the provider documents the endpoint and its URL is known.

For M2 CONNECT the adopted network surface is deliberately limited to:

1. token issuance/reissuance;
2. protected seller-account identity read.

Product registration, images, categories, attributes, options, and product read-back belong to later milestones.

---

## 2. Adoption states

| State | Meaning | Runtime rule |
| --- | --- | --- |
| `ADOPTED` | Required contract fields are frozen for the current milestone. | Callable only through the adopted endpoint contract. |
| `NOT_ADOPTED` | Known/planned endpoint whose operational contract is not frozen. | Must fail locally before network I/O. |

Changing `NOT_ADOPTED -> ADOPTED` requires review of method/path, app-mode eligibility, required groups, timeout policy, success predicate, error contract, redirects, and — for writes — idempotency/read-back/reconciliation.

---

## 3. Base URL

Provider base URL:

`https://api.commerce.naver.com/external`

Documented endpoint paths are modeled separately from the base URL.

| Field | Example |
| --- | --- |
| `base_url` | `https://api.commerce.naver.com/external` |
| `path_relative_to_base_url` | `/v1/seller/account` |
| wire URL | `https://api.commerce.naver.com/external/v1/seller/account` |

Every registry `Path` value in this document is relative to `base_url` unless explicitly stated otherwise.

ICBM MUST NOT maintain competing base-prefix logic that can omit `/external` or produce `/external/external`.

---

## 4. Master registry

| Endpoint ID | Adoption | Milestone | Method | Path (relative to `base_url`) | Purpose | App mode | Required group | Mutation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `SMARTSTORE_AUTH_TOKEN` | `ADOPTED` | M2 | `POST` | `/v1/oauth2/token` | Issue/reissue bearer token | `OWN_STORE_SELF` | `N/A` | No marketplace resource mutation |
| `SMARTSTORE_SELLER_ACCOUNT` | `ADOPTED` | M2 | `GET` | `/v1/seller/account` | Account identity proof | `OWN_STORE_SELF` | `판매자정보` | No |
| `SMARTSTORE_PRODUCT_CREATE_V2` | `ADOPTED` | M5 CREATE adoption slice | `POST` | `/v2/products` | Product CREATE | `OWN_STORE_SELF` | `상품` | Yes; adoption is not LIVE authority — execution stays `DRY_RUN`, the ADR-0018 send-time stack refuses every mutation, an `UNKNOWN` is never resent, and the adopted request is not sendable while required values stay uncaptured or without an ICBM-owned value (§4.1.1) |
| `SMARTSTORE_ORIGIN_PRODUCT_READ_V2` | `ADOPTED` | M5 PR-D | `GET` | `/v2/products/origin-products/{originProductNo}` | Origin-product read-back | `OWN_STORE_SELF` | `상품` | No |
| `SMARTSTORE_CHANNEL_PRODUCT_READ_V2` | `ADOPTED` | M5 PR-D | `GET` | `/v2/products/channel-products/{channelProductNo}` | Channel-product read-back | `OWN_STORE_SELF` | `상품` | No |
| `SMARTSTORE_PRODUCT_IMAGE_UPLOAD` | `ADOPTED` | M5 IMAGE UPLOAD amendment | `POST` | `/v1/product-images/upload` | One-artifact image upload (`multipart/form-data`, `imageFiles`) | `OWN_STORE_SELF` | `상품` | Side effect; durable upload-attempt owner provider-zero (ADR-0018 §3.4, migration `0026`); ASSET sender wired (`SmartStoreAssetSender`) to the CONNECT owner's committed bearer (ROADMAP §14 item 4), so unavailable without a proven current committed session; every upload still refused by the send-time stack under `M0_DRY_RUN_ONLY` |
| `SMARTSTORE_CATEGORY_LIST` | `ADOPTED` | Leaf-category catalog (owner decision 2026-10-04) | `GET` | `/v1/categories` with `last=true` | Current registrable leaf-category discovery | `OWN_STORE_SELF` | `상품` | No; read-only, no LIVE authority |
| `SMARTSTORE_ADDRESSBOOK_LIST` | `ADOPTED` | Settings delivery policy (owner directive 2026-10-07) | `GET` | `/v1/seller/addressbooks-for-page` with `page` | The seller's 출고지 / 반품·교환지 numbers for the target policy; only `addressBookNo`, `name`, `addressType` are retained, never an address or contact | `OWN_STORE_SELF` | `판매자정보` | No; read-only, never stored, no LIVE authority |
| `SMARTSTORE_ORDER_CHANGES` | `ADOPTED` | M6-C order reads (ADR-0023 §5, §6) | `GET` | `/v1/pay-order/seller/product-orders/last-changed-statuses` | Which product orders changed in a time window (§4.1.4) | `OWN_STORE_SELF` | `주문 판매자` | No; read-only |
| `SMARTSTORE_ORDER_DETAILS` | `ADOPTED` | M6-C order reads (ADR-0023 §5, §6) | `POST` | `/v1/pay-order/seller/product-orders/query` | The product orders by id, inside the ADR-0023 §7 allow-list (§4.1.4) | `OWN_STORE_SELF` | `주문 판매자` | No; a query by ids, read-only |
| `SMARTSTORE_CATEGORY_READ` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/categories/{categoryId}` | Category validation | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_PRODUCT_ATTRIBUTE_LIST` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/product-attributes/attributes` | Attribute discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/product-attributes/attribute-values` | Attribute-value discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_STANDARD_OPTIONS` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/options/standard-options` | Standard-option discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_NOTICE_TYPES` | `ADOPTED` | Notice coverage S0 (owner directive 2026-10-03) | `GET` | `/v1/products-for-provided-notice` | Official 상품정보제공고시 type list, read only to capture the provider notice schema | `OWN_STORE_SELF` | `상품` | No |
| `SMARTSTORE_NOTICE_TYPE_READ` | `ADOPTED` | Notice coverage S0 (owner directive 2026-10-03) | `GET` | `/v1/products-for-provided-notice/{productInfoProvidedNoticeType}` | One type's official content fields (`productInfoProvidedNoticeContents[]`: `fieldType`, `fieldName`, `fieldDescription`, `fieldAddDescription`, `fieldMaxLength`), read only to capture the provider notice schema | `OWN_STORE_SELF` | `상품` | No |
| `SMARTSTORE_PRODUCT_SEARCH` | `ADOPTED` | M5 SEARCH positive-only reconcile slice | `POST` | `/v1/products/search` | Positive-only reconcile lookup — never duplicate absence, never a CREATE authorization (§4.1.2) | `OWN_STORE_SELF` | `상품` | No |
| `SMARTSTORE_PRODUCT_DELETE_V2` | `ADOPTED` | DELETE slice (ADR-0018 §3.5) | `DELETE` | `/v2/products/origin-products/{originProductNo}` | Delete one ICBM-confirmed origin product (§4.1.3) | `OWN_STORE_SELF` | `상품` | Yes; destructive. Only an `ACTIVE` ICBM-confirmed registration, under its exact DELETE grant, inside a bounded LIVE window with the brake released; an `UNKNOWN` is never resent |

The remaining M5 rows are planning metadata only. Presence does not imply eventual adoption.

### 4.1 M5 PR-D adoption and its recorded gaps (packet 5746489554, release 2.89.0)

PR-D adopted the two product read-backs. Issue #89 decisions `5765557497` and `5765663972`
subsequently adopted IMAGE UPLOAD, and the separately authorized CREATE adoption slice
(ADR-0020 §4 order 1) later adopted `SMARTSTORE_PRODUCT_CREATE_V2`, whose frozen contract is
§4.1.1, and the SEARCH positive-only reconcile slice (order 2) `SMARTSTORE_PRODUCT_SEARCH`, whose
frozen contract is §4.1.2. The owner decision of 2026-10-04 subsequently adopted only the
leaf-category list with `last=true`; category single-read, attributes and options stay
`NOT_ADOPTED`. Each adoption is a contract and
never a call: the application remains `DRY_RUN`/provider-zero, `product_registration.write` stays
`UNVERIFIED`, and no application route invokes the upload caller or the CREATE caller.

| Endpoint | Why it is still `NOT_ADOPTED` |
| --- | --- |
| `SMARTSTORE_CATEGORY_READ` | the leaf catalog needs only the adopted complete leaf list; the richer single-category certification response has not been adopted |
| `SMARTSTORE_PRODUCT_ATTRIBUTE_LIST` / `_VALUES` / `SMARTSTORE_STANDARD_OPTIONS` | each needs a category query key the packet does not name |
| `SMARTSTORE_CATEGORY_LIST` / `_READ` | no response field is proven, so a deny-by-default retention profile would keep nothing |

**The CREATE and reconcile evidence review closed `INSUFFICIENT`** (Issue #89 `5768247290` →
`5768312853` → `5768347233`; ADR-0014 §17.2). Beyond the packet gaps above, the official contract
proves no CREATE idempotency or ambiguous-outcome replay safety, no `sellerManagementCode`
uniqueness, and no read-after-write freshness that would make a zero-result lookup an authoritative
absence — a seller-code search may return similar, partial or exact matches. Both rows stayed
`NOT_ADOPTED` under that review, and no implementation may proceed on the assumption that a
deterministic provider lookup exists — the later SEARCH adoption (§4.1.2) is positive-only and
assumes none. The verdict stays
`INSUFFICIENT` and is not overturned, and overturning it
is not the adoption condition (ADR-0014 §17.2, §28; ADR-0018 §6.1): CREATE and the positive-only
reconcile path each need their own separately authorized adoption slice — CREATE took its own
(§4.1.1), recording the provider's actual (absent) idempotency and bound to the §28 never-resend
rule; SEARCH took its own (§4.1.2), for positive-only reconcile only. An `UNKNOWN` CREATE is never
resent, a zero-result lookup never proves absence, and the residual-risk acceptance stays a
separate canary prerequisite.

Adopted read-back contract, in the registry and pinned by tests:

| Field | Both read-backs |
| --- | --- |
| Auth | `Authorization: Bearer {token}`, `AUTH_MODE=SELF` unchanged |
| Content type | none (no request body) |
| Timeouts | connect `5s`, read `15s` (ICBM policy) |
| Redirect | `NO_FOLLOW` |
| Success predicate | HTTP 200 AND the body parses as a JSON object (`m5d-origin-read-r1`, `m5d-channel-read-r1`) |
| Safe query keys | **none** (deny-by-default) |
| Retained response fields | `name`, `salePrice`, `stockQuantity`, `sellerManagementCode`, `sellerManagerCode`, `url`; **the origin read also** `statusType` and `channelProductDisplayStatusType` (the published-state read, below) |

**The published-state read (origin read only; mapping revision `m5-published-state-r1`).** The
documented 200 response of `(v2) 원상품 조회` (Commerce API 2.90.0; `SOURCES.md` §5.4,
`NAVER-P0-READ-STATUS-290`) has the top-level members `groupProduct`, `originProduct`,
`windowChannelProduct` and `smartstoreChannelProduct`. ICBM reads the sale status at exactly
`originProduct.statusType` and the SmartStore display status at exactly
`smartstoreChannelProduct.channelProductDisplayStatusType`, each only as a value of its documented
enumeration; a `windowChannelProduct` display status or a status nested anywhere else is never
taken for them. No endpoint is adopted or re-adopted by this: method, path, predicate, timeouts and
query keys are unchanged, and the channel read — whose response is not captured — retains neither
leaf. **Reading is not proving**: ADR-0014 §11 compares the published state against an explicit
expectation. That expectation is the Snapshot's own CREATE projection — the sale status `SALE`
and the display status `ON`, which every registration carries (Issue #89 architect resolution
`5915900049` D1; §4.1.1) — so it is exactly `SALE/ON` (`smartstore-readback-comparison/v3`).
Only a read-back that carries both and both equal it states a published state; `SALE/SUSPENSION`,
another sale status or a missing or unreadable half proves nothing and a read-back then confirms
nothing (`REGISTER_PUBLISHED_STATE_UNPROVEN`).

Adopted image-upload contract: bearer auth; `POST /v1/product-images/upload`; one artifact in one
`imageFiles` multipart part; HTTP 200 with `images[].url`; no query keys; only `url` is retained.
ICBM policy is no redirect, connect `5s`, read `30s`, no automatic retry. Any possibly transmitted
failure is `UPLOAD_UNKNOWN`, distinct from `RegistrationIntent.UNKNOWN`.

`endpoint_mapping_revision = m5-create-r2`, bound to the registry fingerprint, which also
covers the safe-retention profile `smartstore-safe-retention/v1` (ADR-0014 §15). The superseded
revisions `m2-connect-r1`, `m5-register-r1`, `m5-image-upload-r1` and `m5-create-r1` stay
resolvable, so stored evidence still names a known mapping. `m5-create-r2` binds the same
fingerprint as `m5-create-r1`: its reconciliation (below) changed no permission-relevant registry
content, only what the CREATE request sends and what its response contract reads.

### 4.1.1 CREATE adoption amendment (ADR-0020 §4 order 1)

**Amendment note, not a rewrite.** The `SMARTSTORE_PRODUCT_CREATE_V2` row of §4 moves from
`NOT_ADOPTED` to `ADOPTED`, its §4.1 gap row is removed because the slice closed it, and the
mapping revision is bumped to `m5-create-r1` with its own fingerprint in the same change. The
slice's reconciliation to the value-level packet `NAVER-P0-VALUES-CREATE-289` (Issue #89
`5868542027`, E1–E3; `SOURCES.md` §5.2) bumps it to `m5-create-r2` and the response contract to
`smartstore-create-response/v2`; it changes only the request projection (`statusType`), the
request-completeness gaps, the response reading and the outcome classification below. Nothing
else in this file changes, and the adoption slice touched **its own endpoint only**
(ADR-0020 §4): `SMARTSTORE_PRODUCT_SEARCH` stayed `NOT_ADOPTED` under it, and the positive-only
reconcile path took its own later slice (§4.1.2; ADR-0014 §28.2–§28.4, ADR-0018 §6.1, SA-09).

Adopted CREATE contract, in the registry and pinned by tests:

| Field | `SMARTSTORE_PRODUCT_CREATE_V2` |
| --- | --- |
| Auth | `Authorization: Bearer {token}`, `AUTH_MODE=SELF` unchanged, group `상품` |
| Method / path | `POST /v2/products` |
| Request media type | `application/json`; the body is the typed request document, validated against the request contract below and frozen as canonical JSON (sorted keys, UTF-8) by the wire projection. The caller accepts only that frozen document and re-validates it against the request contract at the wire boundary (the type's constructor proves nothing), so no arbitrary, directly built or post-projection-mutated body can be sent. The sender takes no projection from its caller: it always projects the frozen Snapshot itself, refuses on any gap that projection records, and recomputes completeness from the document body before any session or transport (`SMARTSTORE_CREATE_WIRE_NOT_SENDABLE`). Before it composes or encodes the request, the caller re-validates the document and reads completeness from its body and refuses any CREATE that lacks a part the provider requires on registration (`SMARTSTORE_CREATE_REQUEST_INCOMPLETE`, a local pre-handoff refusal); at this adoption that is every CREATE |
| Timeouts | connect `5s`, read `30s` (ICBM policy; no endpoint-specific timeout is documented) |
| Redirect | `NO_FOLLOW` — a `308` is never followed for a mutation (§11; `ERRORS.md` §10.6, §17) |
| Mutation | Yes |
| Success predicate | HTTP 200 AND the body parses as a JSON object (predicate revision `m5-create-r1`, unchanged by the reconciliation). The predicate asserts no identifier shape; the response contract below reads the identifiers |
| Response reading | `smartstore-create-response/v2`, from E3 (`5868542027`): `originProductNo`, `smartstoreChannelProductNo` and `windowChannelProductNo` are read from the **top level only** — nested objects are never searched — and each must be a JSON integer (a Python `int`, **never** a `bool`) in the signed 64-bit range; a numeric string, a float or any other type is refused. `originProductNo` and `smartstoreChannelProductNo` are required for a readable response; the absence of `windowChannelProductNo` alone never makes the documented SmartStore success unreadable, but a present one of another type does. A readable response yields `originProductNo` as the read-back identity; an unreadable one yields none, and its retained body is kept as evidence only |
| Safe query keys | **none** (deny-by-default) |
| Retained response fields | `originProductNo`, `smartstoreChannelProductNo`, `windowChannelProductNo`, plus the safe product leaves `name`, `salePrice`, `stockQuantity`, `sellerManagementCode`, `sellerManagerCode`, `url` |
| Idempotency | **none is invented**: no idempotency key, request-correlation key or replay header is sent, because the provider documents none |
| Request completeness | **Incomplete at this adoption, and that is the frozen state.** The provider requires `originProduct` **and** `smartstoreChannelProduct`. `originProduct.statusType` is projected as `SALE`, the only CREATE input (E2). The required `smartstoreChannelProduct.naverShoppingRegistration` is a captured JSON boolean (E1), but no ICBM-owned value source decides which boolean ICBM publishes with; no Snapshot or ICBM owner decides the registration `originProduct.stockQuantity`, which the endpoint requires to be at least 1 (`5862400626`); and no type-specific child for `productInfoProvidedNotice` is captured. None of them is ever invented — neither `true` nor `false` is guessed — so no Snapshot is sendable. *(Amendment note, Issue #89 architect resolution `5915900049` D1: `channelProductDisplayStatusType` is owned — every document carries `smartstoreChannelProduct` with exactly `channelProductDisplayStatusType = ON` — and the frozen schema admits that one channel member besides `originProduct`; `naverShoppingRegistration` is still not emitted.)* Until Issue #89 architect resolution `5915900049` D2, **no** Snapshot was sendable: every projection carried those named gaps and execution refused with `REGISTER_WIRE_NOT_SENDABLE`. The value-level packet alone did not make a request sendable; the later decision that gave those values an ICBM-owned source is what completes the request. *(Amendment note, Issue #89 architect resolution `5915900049` D2: every required part is now owned or captured, so a single-Item document of a captured notice type is complete and sendable, and the completeness gate refuses only a document without a notice — the state of every Snapshot whose reviewed type is not captured.)* |

Outcome classification, unchanged in substance by adoption (ADR-0014 §9–§10, §28; `ERRORS.md`
§15):

- `NOT_APPLIED_PROVEN` only on the transmission-precluded whitelist — a local pre-handoff refusal,
  the egress guard, or a new connection that failed in DNS/TCP-connect/TLS before a request byte
  was written;
- **everything else is `UNKNOWN`**: a timeout, a lost connection or response, a `5xx` after a
  possible handoff, an ordinary post-handoff `4xx` (architect ruling R2, Issue #89 `5861607665`),
  an unsafe redirect, and a `200` whose documented identifiers are unreadable — missing, nested,
  a numeric string, a boolean or outside the int64 range (response reading, above). An unreadable
  success is not a failure and never `NOT_APPLIED_PROVEN`: the product may exist, which is exactly
  why it is `UNKNOWN` and never resent;
- `APPLIED_PROVEN` is a 200 **and** readable documented identifiers; it hands `originProductNo` on
  as the read-back identity. It is provider-side application evidence only, never a registration
  success: read-back and Snapshot comparison stay separate and alone decide that (ADR-0014 §11);
- an `UNKNOWN` is **never** resent, its conflict scope stays closed, and no lookup, code, grant or
  approval ever becomes remote-absence evidence (ADR-0014 §17.2, §28; ADR-0018 G3-07, G3-15).

Request projection, from the immutable `RegistrationSnapshot` only: `originProduct` with
`statusType` = `SALE` (E2), `name`,
`detailContent`, `images.representativeImage.url` plus at most nine `optionalImages[].url`,
`salePrice`, `detailAttribute.sellerCodeInfo.sellerManagementCode`, the combination-form
`optionInfo` for an option listing, and `leafCategoryId`, which the endpoint requires on
registration (`5862400626`) and which is the operator-reviewed category the Snapshot froze. The provider-required
`smartstoreChannelProduct` is emitted with both required members, each carrying the value ICBM
owns: `channelProductDisplayStatusType = ON` (D1) and `naverShoppingRegistration = true` (D2.1).
`originProduct.stockQuantity` is the registration seed `1` (D2.2), and
`detailAttribute.productInfoProvidedNotice` carries exactly the child of the reviewed notice type
when the pinned 2.90.0 table captures that type (D2.3; `NAVER-P0-NOTICE-CHILD-290`) — amendment
notes, Issue #89 architect resolution `5915900049`. `windowChannelProduct` is out of scope and is
never emitted. That list is also the request-side allow-list, checked **deny-by-default** before the
document is frozen — the request-side twin of the retention profile: an unrecorded path, a value
outside a documented bound, an image URL that is not a prepared sanitized provider reference, or a
`sellerManagementCode` that is not this listing identity's projection is refused, never trimmed
into shape — including a `statusType` other than `SALE`. The frozen document is bound to its
Snapshot's listing identity, and the sender refuses, before any transport, a document whose
identity is not the executing Intent's (`SMARTSTORE_CREATE_IDENTITY_MISMATCH`, `NOT_APPLIED_PROVEN`
as a local pre-handoff refusal). Every value the official evidence does
not carry, or that no ICBM owner decides, stays **fail-closed** as a named gap, so the request is
not sendable — the REGISTER execution owner refuses with `REGISTER_WIRE_NOT_SENDABLE` before it
opens an Attempt, and the SmartStore sender, re-projecting the Snapshot itself, refuses again with
its adapter-level `SMARTSTORE_CREATE_WIRE_NOT_SENDABLE` — for: the child of a notice type the
pinned table does not capture (it is never taken from another type); and, for an option listing,
whether a combination price is absolute or a difference. *(Amendment note, Issue #89 architect resolution `5915900049` D2: the
value source of `naverShoppingRegistration` and the registration `stockQuantity` were in this list
and are owned now, and the notice child is projected for every captured type. A single-Item
listing whose reviewed notice type is captured and whose notice satisfies the child's documented
members is therefore **sendable**: the REGISTER owner no longer refuses it as not sendable, and
what stops it is the rest of the send path — no committed session, `M0_DRY_RUN_ONLY` and the
ADR-0018 send-time stack. Since ROADMAP §14 item 4 a proven committed session supplies the
bearer, and `M0_DRY_RUN_ONLY` and the stack still refuse.)*

**The display status is owned** (Issue #89 architect resolution `5915900049` D1): every CREATE
document carries `smartstoreChannelProduct.channelProductDisplayStatusType = ON`; any other value,
a missing channel object or an unowned channel member is refused before freezing (wire
`smartstore-register-wire/v3`). It is the first-vertical publication
decision, frozen with the Snapshot's projection and never derived from the provider or a session,
and the read-back compares against it (§4.1).

**The remaining CREATE values are owned** (Issue #89 architect resolution `5915900049` D2; wire
`smartstore-register-wire/v4`). Each is frozen with the Snapshot's projection and never derived
from the provider or a session:

- **`naverShoppingRegistration = true`** (D2.1) — ICBM's publication intent. It is no assertion
  that the account is a NAVER Shopping advertiser: the provider stores `false` for a
  non-advertiser, and no account capability is inferred from the value. It is not read back.
- **`originProduct.stockQuantity = 1`** (D2.2) — the registration seed, not a supplier quantity.
  A Snapshot exists only for a unit whose final preflight is `READY`, which a sold-out source never
  is (the M4 readiness it consumes is `BLOCKED` on `SOURCE_STOCK_SOLD_OUT`); no source quantity is
  fabricated. The read-back compares it exactly at `originProduct.stockQuantity`
  (`smartstore-readback-normalizer/v3`, `smartstore-readback-comparison/v4`): a different or
  missing value is `STOCK_QUANTITY_MISMATCH`, never a confirmation.
- **The `productInfoProvidedNotice` child** (D2.3; notice coverage S3, wire
  `smartstore-register-wire/v5`) — selected from the reviewed `CategoryMetadata.notice.notice_type`
  through the provider notice schema `smartstore-notice-schema/2.90.0-r1`
  (`integrations/marketplaces/smartstore/notice_schema.json`, derived mechanically from the retained
  2.90.0 `원상품 정보 구조체` reference and the live notice capture; inventory in
  `documents/evidence/marketplace-apis/NOTICE_SMARTSTORE_S0.md`). The schema documents a child for
  36 types; the four enum values without one (`LODGMENT_RESERVATION`, `TRAVEL_PACKAGE`,
  `AIRLINE_TICKET`, `RENT_CAR`) stay a named gap, are `NOTICE_TYPE_UNDOCUMENTED` (`BLOCKED`) in the
  preflight, and never fall back to another child. No child name is derived by casing a string.
  The same contract drives the three layers: the durable category metadata of a SmartStore
  category selects the notice type and takes that type's fields from the schema — hand-recorded
  notice rules beside it are superseded — so the preflight checks, and the projection validates,
  one set of fields. Only the Snapshot's own reviewed values are projected, each in its own JSON
  type (text, boolean, integer), under their reviewed field names. Deprecated fields and the three
  `releaseDate` fields whose wire form the sources disagree on (`SEASON_APPLIANCES`,
  `OFFICE_APPLIANCES`, `SPORTS_EQUIPMENT`) are never declared or sent; their text alternative
  `releaseDateText` is then required. A field the provider fills when it is omitted — the
  "미입력 시 상품상세 참조로 입력됩니다" fields and `KITCHEN_UTENSILS.importDeclaration`'s stated
  `false` — is never required input and is omitted rather than filled. The documented forms
  (`yyyy-MM`, `yyyy-MM-dd`), length bounds, integer widths (int32, int64), "required when the other
  is absent" pairs and the `GIFT_CARD` one-of store group are enforced, never repaired.

`sellerManagementCode` is the ICBM projection `smartstore-seller-management-code/v1` (architect
ruling R1, Issue #89 `5861607665`): the first 30 lowercase hexadecimal characters of
`SHA-256("smartstore-seller-management-code/v1\0" + listing_identity)`, deterministic, versioned,
frozen with the Snapshot and compared exactly on read-back and on a later positive reconcile. The
internal `listing_identity` is unchanged (ADR-0014 §7), and the code is **not** a provider
uniqueness proof.

The provider-evidence verdict stays `INSUFFICIENT` (§4.1, ADR-0014 §17.2), `product_registration.write`
stays `UNVERIFIED`, execution stays `DRY_RUN` / `M0_DRY_RUN_ONLY`, a real canary stays `BLOCKED`,
and M5 stays `PENDING`.

### 4.1.2 SEARCH positive-only reconcile adoption (ADR-0020 §4 order 2)

**Amendment note, not a rewrite.** The `SMARTSTORE_PRODUCT_SEARCH` row of §4 moves from
`NOT_ADOPTED` to `ADOPTED`, its §4.1 gap row is removed because the slice closed it with the
architect's official evidence resolution (Issue #89 `5904349289`, S1–S4; `SOURCES.md` §5.3), and the
mapping revision is bumped to `m5-search-r1` with its own fingerprint in the same change. The slice
touched **its own endpoint only**: every other row of §4 is unchanged.

Adopted search contract, in the registry and pinned by tests:

| Field | `SMARTSTORE_PRODUCT_SEARCH` |
| --- | --- |
| Auth | `Authorization: Bearer {token}`, `AUTH_MODE=SELF` unchanged, group `상품` |
| Method / path | `POST /v1/products/search` |
| Request media type | `application/json`; the body is exactly the documented seller-code search: `searchKeywordType` `SELLER_CODE`, `sellerManagementCode` — only ever the `smartstore-seller-management-code/v1` projection of an ICBM listing identity (30 lowercase hex characters, ruling R1) — `page` from 1 and `size` at most 500. No other filter is invented, and a request outside this shape is refused before any transport |
| Timeouts | connect `5s`, read `15s` (ICBM policy; no endpoint-specific timeout is documented) |
| Redirect | `NO_FOLLOW` |
| Mutation | **No** — a read |
| Success predicate | HTTP 200 AND a JSON object carrying every documented S2 member on the raw page — `contents` an array of objects each with an integer `originProductNo` and a `channelProducts` array whose entries carry integer `originProductNo` and `channelProductNo` and string `channelServiceType` and `sellerManagementCode`, `page`/`size`/`totalElements`/`totalPages` integers and `first`/`last` booleans (predicate revision `m5-search-r1`). It is checked before retention, which drops an empty array or a null, so a missing member never reads as an empty one; a page that fails it is `ERROR` |
| Response reading | `smartstore-product-search/v1` (`search.py`): each `contents[n].originProductNo` and each `channelProducts[m]` `originProductNo`/`channelProductNo` must be a JSON integer (never a `bool`) in the int64 range, `channelServiceType` one of `STOREFARM`/`WINDOW`/`AFFILIATE`, `sellerManagementCode` a string (required, as the predicate already proved on the raw page), and a channel entry must name its own item's origin product. Anything else is an undocumented page and proves nothing |
| Enumeration | every page is read, up to a bounded budget of 4 pages of 500 per check (ICBM policy). Page `n` must answer as page `n`, every page must report the same totals, the last page must be read and the items read must equal `totalElements`; otherwise nothing is concluded. A result larger than the budget is `UNAVAILABLE`, never a partial count |
| Safe query keys | **none** (deny-by-default) |
| Retained response fields | `originProductNo`, `channelProductNo`, `channelServiceType`, `sellerManagementCode`, `page`, `size`, `totalElements`, `totalPages`, `first`, `last` — product names and every other leaf are dropped |

What a lookup may conclude (ADR-0014 §28.2–§28.4), and nothing more:

- only a `STOREFARM` channel entry whose `sellerManagementCode` is **exactly** the projection is a
  candidate — the provider's own match is similar, partial or exact and is never trusted;
- zero exact candidates are `ZERO`: never absence, never a CREATE authorization;
- more than one is `MULTIPLE`: no automatic selection, `REVIEW_REQUIRED`;
- exactly one is only an identity-recovery candidate. It becomes presence (`ONE_VERIFIED`,
  `APPLIED_PROVEN` by `PROVIDER_LOOKUP`) only when the adopted origin read-back by its
  `originProductNo` carries exactly the same code; otherwise it is `ONE_MISMATCH`. Presence is not
  success: the read-back comparison of ADR-0014 §11 still decides that;
- a missing session, a `429` rate or quota refusal and a result beyond the read budget are
  `LOOKUP_UNAVAILABLE` (the next bounded check is deferred); every other failure, and an
  undocumented or inconsistent page, is `ERROR`. Neither proves anything.

Every check is recorded by the durable reconcile-check owner of ADR-0014 §28.4
(`registration_reconcile_checks`, migration `0031`, Issue #89 `5904349289` §A): single-flight per
Intent, finished exactly once, append-only and never deleted, with the SHA-256 of its sanitized
evidence and a bounded automatic schedule (the first automatic check when an Intent is found, then
waits of 15 minutes, 1, 6 and 24 hours, then none). A trigger while a check is in flight coalesces
into it. A check a crashed process left in flight is finished as `ERROR` when the next process starts
— one process owns a data directory (ADR-0006), so no live check can be closed that way — and single-
flight never blocks its Intent forever; that records only that nothing was observed. When presence is proven both provider identities are
persisted: `marketplace_product_id` stays the `originProductNo` and `marketplace_channel_product_id`
holds the `STOREFARM` `channelProductNo` (§B); a missing or ambiguous channel identity is never
guessed and proves nothing.

**Adoption is a contract, never a call.** Production wired the lookup with no committed session, so
every check was `LOOKUP_UNAVAILABLE` and no provider was read. *(Since ROADMAP §14 item 4 it reads
the CONNECT owner's committed bearer: without a proven current committed session every check is
still `LOOKUP_UNAVAILABLE`, and with one a check is a routine read-only provider read, reached only
from an operator route on an applied or `UNKNOWN` Intent.)* execution stays `DRY_RUN`,
`product_registration.write` stays `UNVERIFIED`, and the canary stays `BLOCKED` on every other
prerequisite. The provider-evidence verdict stays `INSUFFICIENT` (ADR-0014 §17.2): this adoption
needs no deterministic lookup and assumes none. The search is **never** a duplicate lookup —
duplicate evidence stays fail-closed (`lookup.py`; ADR-0014 §13) — and an `UNKNOWN` CREATE is still
never resent.

### 4.1.3 DELETE adoption (ADR-0018 §3.5; owner decision 2026-10-03)

**Amendment note, not a rewrite.** The `SMARTSTORE_PRODUCT_DELETE_V2` row is added to §4 as
`ADOPTED`, and the mapping revision is bumped to `m5-delete-r1` with its own fingerprint in the
same change. The slice touched **its own endpoint only**: every other row of §4 is unchanged.

| Field | `SMARTSTORE_PRODUCT_DELETE_V2` |
| --- | --- |
| Method / path | `DELETE /v2/products/origin-products/{originProductNo}` (`originProductNo` `integer<int64>`) |
| Auth | `Authorization: Bearer {token}`, `AUTH_MODE=SELF` unchanged, group `상품` |
| Request | no query, no body |
| Success | HTTP `200` with the common response object; nothing of it is retained |
| Everything else | `UNKNOWN` — a 4xx after the handoff (the provider refuses a deletion while an order or a claim is open or the product is under a sale ban), a 5xx, a redirect, a timeout, a 200 that is not a JSON object — never resent; only an origin read-back resolves it |
| `NOT_APPLIED_PROVEN` | only the transmission-precluded whitelist (§15.1 of `ERRORS.md`) |
| Bulk | none; one product per call |
| Evidence | `documents/evidence/marketplace-apis/PRODUCT_DELETE.md` § SmartStore |

Adoption is never authority: a deletion runs only through `RegistrationDeletionService` and the
send-time stack (ADR-0018 §3.5), for one `ACTIVE` ICBM-confirmed registration, under its exact
DELETE grant, inside a bounded LIVE window with the brake released.

### 4.1.4 Order read adoption (M6-C; ADR-0023 §5–§7)

**Amendment note, not a rewrite.** The `SMARTSTORE_ORDER_CHANGES` and `SMARTSTORE_ORDER_DETAILS`
rows are added to §4 as `ADOPTED`, and the mapping revision is bumped to `m6-orders-r1` with its own
fingerprint in the same change. The slice touched **its own endpoints only**: every other row of §4
is unchanged. Evidence: `SOURCES.md` §5.6 (`NAVER-P0-ORDER-READ-2901`, Commerce API `2.90.1`).

| Field | `SMARTSTORE_ORDER_CHANGES` | `SMARTSTORE_ORDER_DETAILS` |
| --- | --- | --- |
| Auth | `Authorization: Bearer {token}`, `AUTH_MODE=SELF`, group `주문 판매자` (`ORDER_READ.md`; the reference page names none) | same |
| Method / path | `GET /v1/pay-order/seller/product-orders/last-changed-statuses` | `POST /v1/pay-order/seller/product-orders/query` |
| Request | query `lastChangedFrom` and `lastChangedTo` (both sent, KST `yyyy-MM-dd'T'HH:mm:ss.SSS+09:00`, from ≤ to), `limitCount` 1..300, and `moreSequence` only as the provider's own continuation with its `moreFrom` | `application/json` body `{"productOrderIds": [...]}`, 1..300 distinct ids; `quantityClaimCompatibility` is not sent |
| Timeouts / redirect | connect `5s`, read `30s`; `NO_FOLLOW` | same |
| Success predicate | HTTP 200 and a JSON object with a `data` object (an answer without it proves no window was read and is never an empty one, even though the envelope does not badge it required) carrying an integer `count`, a `lastChangeStatuses` array whose entries carry string `productOrderId` and `lastChangedDate`, and a `more` naming both `moreFrom` and `moreSequence` when present (`m6-order-changes-r1`) | HTTP 200 and a `data` array whose every entry has a `productOrder` object with a string `productOrderId` (`m6-order-details-r1`) |
| Paging | ascending by change time; a `more` continues with `moreFrom` as the next `lastChangedFrom` and its `moreSequence`; ICBM reads at most 50 pages per window and never goes back in time | none; at most 300 ids per query |
| Retained fields | `count`, `productOrderId`, `orderId`, `lastChangedType`, `lastChangedDate`, `productOrderStatus`, `claimType`, `claimStatus`, `moreFrom`, `moreSequence` | the ADR-0023 §7 allow-list, applied after the caller narrows each entry to the scalar members of `order`, `productOrder` (and its `shippingAddress`) and `delivery` — every claim, its addresses, the seller's `takingAddress`, coupons and promotions are dropped first, and never the orderer's id, name or phone, a payment means or a commission |
| Errors | `ERRORS.md` §8 unchanged: a `429`/`GW.RATE_LIMIT`/`GW.QUOTA_LIMIT` is `RATE_LIMITED`; every other failure is classified and proves nothing | same |

**Adoption is not ingest authority.** The reads are wired only to the OPERATE order owner
(`app/stages/operate/orders.py`) through `SmartStoreOrderSource`, with the CONNECT owner's committed
bearer; without a proven current committed session nothing is read. Neither read writes the
marketplace (M6-01), and no order data leaves the local data root (M6-11).

### 4.2 CREATE request/response evidence — evidence only, not adoption (`SOURCES.md` §5.2, release 2.89.0)

The architect's official evidence on Issue #89 (packet `5746489554`, reviews `5768199984` and
`5768247290`, field-level packet `5861477977`, required/conditional-field packet `5861933729`,
registration-requirement and value-level packet `5862400626`) establishes the following for
`SMARTSTORE_PRODUCT_CREATE_V2`. **This section adopted nothing:** it is the evidence the later
adoption slice read. The adopted contract itself is §4.1.1, which froze the success predicate,
timeout, redirect, error classification, retention profile, validated typed request projection and
fail-closed response contract from these facts and from the value-level packet `5868542027` — and
gained no LIVE authority by doing so (ADR-0020 §2.4, SA-05).

Compact wire summary. The nested request keys, required/optional/conditional rules, limits and
defaults are recorded once, field by field with sources, in
`documents/evidence/marketplace-apis/PRODUCT_CREATE.md` § SmartStore; this matrix does not repeat them.

| Field | Evidence | Source |
| --- | --- | --- |
| Method / path | `POST /v2/products` (`(v2) 상품 등록`) | 5746489554 |
| Auth | `Authorization: Bearer {token}`, `AUTH_MODE=SELF` unchanged, API group `상품` | 5746489554 |
| Request media type | `application/json` | 5768199984, 5861477977 |
| Request top level | `originProduct` (required object), `smartstoreChannelProduct` (required object); `windowChannelProduct` is a separate Shopping Window channel structure, out of the SmartStore-only scope | 5861477977, 5861933729 |
| Required on registration | `originProduct`: `statusType` (registration value `SALE`), `leafCategoryId`, `name`, `detailContent`, `images`, `salePrice`, `stockQuantity` (at least 1), `detailAttribute`; `productInfoProvidedNotice` for registration; `smartstoreChannelProduct`: `naverShoppingRegistration` (boolean), `channelProductDisplayStatusType` (`ON` or `SUSPENSION` for writes). Accepted values and conditional rules: capability record | 5861933729, 5862400626 |
| Documented success | HTTP `200`, `Content-Type: application/json;charset=UTF-8` | 5768199984, 5861477977 |
| Success identifiers | `originProductNo`, `smartstoreChannelProductNo`, `windowChannelProductNo` (since API docs `v2.68.0`; may be absent for a SmartStore-only CREATE), plus `originProduct`, the product data SmartStore successfully stored | 5768199984, 5861477977, 5861933729 |
| Documented statuses | `200`, `308`, `400`, `401`, `403`, `404`, `500`; their meaning is the product-API error contract of `ERRORS.md` §10 — `BAD_REQUEST` read from `invalidInputs` **and** `message`, `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `INTERNAL_SERVER_ERROR`, and `308/PERMANENT_REDIRECT` never followed for a mutation (§11; `ERRORS.md` §17) | 5861477977; `ERRORS.md` §10 |
| Idempotency | none: no idempotency key, request-correlation key, replay rule or duplicate-prevention guarantee | 5768247290, 5861477977 |

Not proven, so fail-closed until an adoption slice or new evidence settles it:

- sending a structure merely because the schema contains it: only fields owned by the immutable
  RegistrationSnapshot/preparation and required by the selected product/channel conditions may be
  projected; an unsupported category/feature condition or an enumerated value the evidence does not
  list stays fail-closed / `REVIEW_REQUIRED`;
- a projection for the simple, custom or standard option structures: only an option shape the
  canonical ICBM contracts already allow may be projected;
- the field set of a type-specific notice child (not captured) and any value of it that ICBM does
  not own.

Outcome rules that no evidence here changes:

- a timeout, a connection loss, a response loss or a `5xx` after transport handoff is `UNKNOWN`
  (`ERRORS.md` §14.3, §15.2) and **a CREATE in `UNKNOWN` is never resent** (ADR-0014 §28; ADR-0018
  §6.1, G3-07);
- **neither a `500` nor a zero-result lookup proves that a product was not registered** (ADR-0014
  §17.2, §28.2; ADR-0018 G3-15);
- the provider-evidence verdict stays `INSUFFICIENT` for idempotent replay and remote-absence proof,
  the canary stays `BLOCKED`, and execution stays `DRY_RUN`.

The code-side gap text for this row was in `integrations/marketplaces/smartstore/registry.py`
`ADOPTION_GAPS`; the CREATE adoption slice removed it together with freezing the adopted contract
(§4.1.1). This evidence record itself is unchanged by that: it states what the provider documents,
and the fail-closed items above are still fail-closed in the adopted projection, as named gaps.

---

## 5. M2 CONNECT graph

The complete M2 network sequence is:

`SMARTSTORE_AUTH_TOKEN`

`-> durable session commit per AUTH.md`

`-> SMARTSTORE_SELLER_ACCOUNT`

`-> identity comparison/binding per ACCOUNT_IDENTITY.md`

`-> AUTH_READY candidate`

No product endpoint is part of this graph.

### M2 adopted permission union

`required_groups(SMARTSTORE_AUTH_TOKEN) = {}`

`required_groups(SMARTSTORE_SELLER_ACCOUNT) = {판매자정보}`

Therefore:

`M2_ADOPTED_ENDPOINT_GROUP_UNION = {판매자정보}`

`PERMISSIONS_SCOPES.md` separately tracks `상품` as the planned product-write permission baseline.

That does **not** make a product endpoint part of M2.

`상품 permission observed != product endpoint adopted`

`상품 permission observed != product write verified`

M2 MUST NOT issue a product request merely to discover or reconfirm product permission.

M2 `write_scope` is populated, when evidence exists, from the non-network provider-admin attestation/introspection contract in `PERMISSIONS_SCOPES.md`; `ENDPOINT_MATRIX.md` does not add a product network call for that purpose.

---

## 6. `SMARTSTORE_AUTH_TOKEN`

### Contract

| Field | Value |
| --- | --- |
| Adoption | `ADOPTED` |
| Method | `POST` |
| Path relative to `base_url` | `/v1/oauth2/token` |
| Wire URL | `https://api.commerce.naver.com/external/v1/oauth2/token` |
| App mode | `OWN_STORE_SELF` |
| Grant | Client Credentials |
| Content-Type | `application/x-www-form-urlencoded` |
| Bearer required | No |
| Required API group | `N/A` |
| Connect timeout | `5s` |
| Read timeout | `30s` |
| Redirect policy | `NO_FOLLOW` |
| Lifecycle owner | `AUTH.md` |

The timeout values are ICBM M2 policy, not provider guarantees. They are intentionally asymmetric with the seller-account read because an avoidably short token read timeout creates expensive issuance uncertainty. Runtime acceptance SHALL record observed latency and may propose a reviewed policy change; implementation MUST NOT silently substitute arbitrary library defaults.

### Request predicate

| Field | M2 rule |
| --- | --- |
| `client_id` | Required; current own-store application ID |
| `timestamp` | Required; generated under `AUTH.md` |
| `client_secret_sign` | Required; generated from the current credential generation |
| `grant_type` | Exact `client_credentials` |
| `type` | Exact `SELF` |
| `account_id` | **Forbidden** for `OWN_STORE_SELF` |

The required fields belong in the form body. Query-string substitution is not accepted by the ICBM contract.

A locally detectable violation should fail before network I/O.

### Success predicate

`token_endpoint_success =`

`HTTP 200`

`AND JSON object parses`

`AND access_token is non-empty string`

`AND expires_in is positive integer`

`AND token_type equals Bearer case-insensitively`

`token_type` remains part of the success predicate deliberately.

OAuth 2.0 defines `token_type` as a required token-response field and states that a client MUST NOT use an access token when it does not understand the token type. ICBM M2 implements the Bearer token scheme only, and NAVER documents protected calls as `Authorization: Bearer {access_token}`. Therefore a non-Bearer token type is provider/contract drift, not a warning that ICBM may ignore.

Case changes such as `bearer` vs `Bearer` are accepted by the case-insensitive comparison.

If a 200 response fails this predicate:

- semantic success is false;
- `session_generation` MUST NOT advance;
- evidence is handled as schema/provider-contract drift under `ERRORS.md`.

Token endpoint success does **not** prove account identity or `AUTH_READY`.

### Session boundary

A successful response creates a token candidate only.

`token_endpoint_success != session_generation_committed`

The new session generation exists only after the complete local session bundle is durably committed under `AUTH.md`.

### Current provider token behavior

Current documentation states:

- default lifetime: 10,800 seconds / 180 minutes;
- at least 30 minutes remaining: existing token may be returned;
- fewer than 30 minutes remaining: new token may be issued;
- an older token remains usable until its own expiry after a new token is issued.

These facts do not resolve the `remote success + local commit unknown` crash case; that remains owned by `AUTH.md` and runtime measurement.

### Documented endpoint-level responses

| HTTP | Provider-level description | Handling |
| --- | --- | --- |
| `200` | Token issued/reissued | Must pass success predicate |
| `400` | Validation error | Evidence-based diagnosis; not automatic credential-invalid truth |
| `403` | Access/authorization error | Diagnose application/lifecycle/auth evidence |
| `500` | Temporary internal system error | Bounded recovery only |

Transport/gateway failures may also occur.

### Redirect

The adopted endpoint contract does not list redirect as a normal result.

`redirect_policy = NO_FOLLOW`

Unexpected redirect is contract/error evidence and MUST NOT be followed by a generic client.

### Retry

This POST is an authentication-control operation, not a marketplace CREATE.

Retry/reissue semantics are owned by `AUTH.md`, including bounded issuance, atomic session commit, lifecycle rules, and first-token crash uncertainty.

A read timeout after the request may have reached NAVER does not prove token issuance failed. It remains an authentication-session uncertainty owned by `AUTH.md`; it MUST NOT be translated into user re-approval without evidence.

---

## 7. `SMARTSTORE_SELLER_ACCOUNT`

### Contract

| Field | Value |
| --- | --- |
| Adoption | `ADOPTED` |
| Method | `GET` |
| Path relative to `base_url` | `/v1/seller/account` |
| Wire URL | `https://api.commerce.naver.com/external/v1/seller/account` |
| App mode | `OWN_STORE_SELF` |
| Authentication | `Authorization: Bearer {access_token}` |
| Required API group | `판매자정보` |
| Mutation | No |
| Connect timeout | `5s` |
| Read timeout | `10s` |
| Redirect policy | `NO_FOLLOW` |
| Identity owner | `ACCOUNT_IDENTITY.md` |

The timeout values are ICBM M2 policy, not provider guarantees. This read may use the shorter read timeout because it is non-mutating and already has bounded retry/re-auth rules. Runtime acceptance SHALL measure observed latency before any future timeout-policy change.

### Request predicate

The request MUST:

- use `GET` on the exact adopted path;
- use the bearer token from the current committed `session_generation`;
- preserve credential/session-generation linkage in evidence;
- contain no product/business mutation payload.

A token candidate that was never durably committed MUST NOT produce durable account-proof evidence.

### Endpoint success predicate

`accountUid` is the strong identity field required by `ACCOUNT_IDENTITY.md`.

`accountId` is secondary/corroborating and is recorded when available.

Therefore:

`seller_account_endpoint_success =`

`HTTP 200`

`AND JSON object parses`

`AND accountUid is non-empty string`

A missing optional/secondary `accountId` does not by itself manufacture failure of the primary identity proof path; it is preserved as schema/evidence context and reviewed if provider behavior diverges from the documented response contract.

This distinction keeps the matrix aligned with:

`provider_account_uid = primary identity`

`provider_account_id = secondary identity when available`

### Bound-account identity predicate

For an already bound MarketplaceAccount:

`account_identity_proven = seller_account_endpoint_success`

`AND observed.accountUid == expected.provider_account_uid`

A mismatch remains:

`AUTH_MISMATCH -> REVIEW_REQUIRED`

Weak fields and `accountId` correlation MUST NOT override an `accountUid` mismatch.

### First binding

For an unbound account:

- endpoint success supplies current authenticated identity evidence;
- explicit first binding is still required;
- the binding unit is committed atomically under `ACCOUNT_IDENTITY.md`;
- successful read alone does not silently bind the account.

### AUTH_READY effect

For an already bound account:

`AUTH_READY =`

`current AUTH.md invariants hold`

`AND seller_account_endpoint_success`

`AND accountUid match`

`AND evidence belongs to current credential/session generations`

Token success by itself remains insufficient.

### Permission group

This endpoint requires `판매자정보` under the adopted M2 contract.

Consequences:

- if the account-read proof path cannot be completed, token issuance alone cannot establish `AUTH_READY`;
- this failure MUST NOT automatically become product `write_scope=MISSING`;
- `상품` is not part of the M2 network endpoint union.

### Documented domain error surface

| HTTP | Provider code | Meaning | Matrix rule |
| --- | --- | --- | --- |
| `400` | `GENERAL_ERROR` | Unhandled error | Use `ERRORS.md`; do not assume input validation |
| `401` | `UNAUTHORIZED` | Access authority unavailable | Diagnose auth/permission/context; no unconditional mapping |
| `403` | `ROLE_NOT_FOUND` | Role missing | Provider condition evidence |
| `403` | `PROVISION_NOT_FOUND` | Terms/agreement required | Provider condition; not credential invalidity |
| `403` | `INVALID_CHANNEL_STATUS` | Invalid channel state | Provider/account state evidence |
| `403` | `INVALID_STORE_STATUS` | Invalid store state | Provider/account state evidence |
| `403` | `INVALID_REPRESENT_STATUS` | Invalid representative state | Provider/account state evidence |
| `403` | `INVALID_MEMBER_STATUS` | Invalid member state | Provider/account state evidence |
| `403` | `INVALID_INTERLOCK_STATUS` | Invalid integration state | Provider/integration evidence |
| `403` | `RESOURCE_NOT_AVAILABLE` | Resource unavailable | Diagnose before canonical classification |
| `404` | `CHANNEL_NOT_FOUND` | Channel absent | No global NOT_FOUND mapping |
| `404` | `STORE_NOT_FOUND` | Store absent | No global NOT_FOUND mapping |
| `404` | `REPRESENT_NOT_FOUND` | Representative absent | No global NOT_FOUND mapping |
| `404` | `MEMBER_NOT_FOUND` | Member absent | No global NOT_FOUND mapping |
| `404` | `INTERLOCK_NOT_FOUND` | Interlock data absent | No global NOT_FOUND mapping |
| `500` | `PARSING_FAIL` | JSON parsing failure | Evidence-based classification |
| `500` | `SERDES_FAIL` | Serialization/deserialization failure | Evidence-based classification |
| `500` | `ENCDEC_FAIL` | Encryption/decryption failure | Evidence-based classification |
| `500` | `GENERAL_ERROR` | Unhandled error | Candidate transient/unknown depending evidence |

These are endpoint evidence inputs, not new canonical error classes.

### Redirect

The endpoint's adopted response set does not include redirect.

`redirect_policy = NO_FOLLOW`

### Retry

The endpoint is read-only.

Retry follows `ERRORS.md` and `AUTH.md`:

- bounded transient retry;
- bounded auth recovery only when AUTH cause is positively established;
- no hot loop;
- `UNKNOWN` does not become credential invalidity;
- mismatch never causes automatic account switching.

---

## 8. M2 completion contract

For an already bound account:

| Step | Required result |
| --- | --- |
| 1 | `SMARTSTORE_AUTH_TOKEN` success predicate passes |
| 2 | token/session bundle is durably committed as current `session_generation` |
| 3 | `SMARTSTORE_SELLER_ACCOUNT` success predicate passes using that generation |
| 4 | observed `accountUid` matches canonical `provider_account_uid` |
| 5 | `auth=READY` may be established |

For first binding, step 4 is replaced by the explicit atomic binding flow.

Product capability remains separate:

`auth READY != write_scope READY != write READY`

No M5 endpoint may be called to complete M2 CONNECT.

---

## 9. Mandatory success-predicate gate

Every `ADOPTED` endpoint MUST have a machine-checkable success predicate.

For **every** response, including HTTP 2xx:

`receive`

`-> parse endpoint contract`

`-> evaluate success_predicate`

`-> only then declare semantic success`

An endpoint without a frozen success predicate is not eligible for automated execution.

This closes the `ERRORS.md` OPERATION_RESULT gap: HTTP success is never the final success decision by itself.

---

## 10. Timeout policy

Timeouts are endpoint-contract fields, not incidental HTTP-library defaults.

M2 baseline:

| Endpoint ID | Connect timeout | Read timeout | Reason |
| --- | ---: | ---: | --- |
| `SMARTSTORE_AUTH_TOKEN` | `5s` | `30s` | Favor avoiding avoidable token-issuance uncertainty. |
| `SMARTSTORE_SELLER_ACCOUNT` | `5s` | `10s` | Read-only operation with bounded retry/re-auth paths. |

Rules:

- timeout expiry is classified under `ERRORS.md`/`AUTH.md`; it is not proof of provider rejection;
- token timeout after possible transmission MUST preserve issuance uncertainty rather than invent credential failure;
- values are reviewed from measured M2 latency evidence, not silently tuned per call site;
- implementation SHALL expose the endpoint policy to tests so accidental fallback to client defaults is detectable.

---

## 11. Redirect policy

Generic automatic redirect following is forbidden for SmartStore integration clients unless an adopted endpoint explicitly permits it.

Both M2 endpoints are:

`NO_FOLLOW`

Future redirect adoption must freeze:

- accepted status;
- allowed target host/path;
- method/body behavior;
- credential-forwarding behavior;
- replay/idempotency safety.

This is mandatory before any mutating endpoint with possible 308 behavior can become `ADOPTED`.

---

## 12. M5 rows are intentionally incomplete

M5 candidate rows do not freeze:

- required groups;
- app-mode eligibility;
- timeout policy;
- success predicate;
- domain errors;
- redirect behavior;
- idempotency;
- read-back identity;
- consistency window;
- `remote_outcome` reconciliation;
- retry budget;
- cleanup behavior.

Those values are filled only when M5 actually adopts the endpoint.

No code may substitute guessed defaults for `TBD_AT_ADOPTION`.

---

## 13. Static/repository enforcement

M2 implementation SHALL make the matrix enforceable, not documentary only.

The SmartStore integration layer MUST NOT directly own a raw/general-purpose HTTP client in adapters, services, or feature code.

All provider network calls MUST pass through one registry-gated SmartStore endpoint caller (for example, an abstraction equivalent to `caller.call(endpoint_id, request_data)`) that:

- resolves only an `ADOPTED` endpoint ID;
- owns/receives the raw HTTP transport internally;
- composes `base_url + path_relative_to_base_url` exactly once;
- applies the endpoint method, timeout, redirect, auth, and success-predicate policy;
- rejects `NOT_ADOPTED` before transport handoff.

The illustrative class/function name above is not frozen; the ownership boundary is.

SmartStore feature/adaptor code MUST NOT receive a raw HTTP client that permits arbitrary URL construction or direct `get/post/request` calls around the registry.

Acceptance must prove at least:

1. the M2 SmartStore runtime registry exposes exactly the two `ADOPTED` endpoint IDs;
2. `NOT_ADOPTED` resolution fails locally before network I/O;
3. base URL/method/path come from one provider endpoint contract, not duplicated raw strings;
4. token requests enforce form encoding, `type=SELF`, and absent `account_id`;
5. account reads require the current committed bearer session;
6. endpoint-specific connect/read timeouts are applied and library defaults cannot silently replace them;
7. generic auto-redirect is disabled;
8. every adopted endpoint has a success predicate;
9. malformed 2xx responses fail closed;
10. evidence records contain the endpoint ID and relevant generation IDs;
11. static/repository tests reject SmartStore adapter/service code that constructs or owns a raw HTTP client outside the approved registry-gated caller boundary;
12. raw SmartStore URL literals and dynamic URL assembly outside the approved caller boundary are rejected to the extent enforceable by repository checks, with the raw-client ownership rule as the primary control rather than string matching alone.

The implementation mechanism is not frozen here; the observable ownership and call-path invariants are.

---

## 14. M2 acceptance requirements

### 14.1 Allow-list

Runtime M2 endpoint set must be exactly:

- `SMARTSTORE_AUTH_TOKEN`
- `SMARTSTORE_SELLER_ACCOUNT`

No M5 candidate call is allowed.

### 14.2 Token request

Verify:

- POST `/external/v1/oauth2/token`;
- form-urlencoded body;
- required fields present;
- `grant_type=client_credentials`;
- `type=SELF`;
- `account_id` absent;
- no credentials/signatures leak to logs.

### 14.3 Token timeout policy

Verify:

- connect timeout is `5s`;
- read timeout is `30s`;
- a post-transmission read timeout does not get rewritten as credential invalidity or automatic user re-approval;
- timeout evidence is preserved for `AUTH.md` recovery/measurement.

### 14.4 Token success predicate

Fixtures must include:

- valid Bearer token body -> pass;
- 200 missing/empty `access_token` -> fail;
- nonpositive/invalid `expires_in` -> fail;
- case variants of `Bearer` -> pass;
- unsupported/non-Bearer `token_type` -> fail closed as token-type/contract drift;
- predicate failure does not advance `session_generation`.

### 14.5 Session boundary

Provider 200 alone does not make the session current.

Only durable local commit may advance `session_generation`.

### 14.6 Seller account request

Verify:

- GET `/external/v1/seller/account`;
- bearer belongs to current committed session generation;
- no mutation occurs.

### 14.7 Seller account timeout policy

Verify:

- connect timeout is `5s`;
- read timeout is `10s`;
- timeout uses bounded read/auth recovery rules and does not preserve READY solely from persistence.

### 14.8 Seller account success predicate

Fixtures must include:

- 200 + non-empty `accountUid` -> endpoint predicate passes;
- 200 missing/empty `accountUid` -> fail closed;
- malformed 200 schema -> fail closed;
- `accountId` is retained as secondary evidence when available but is not substituted for missing `accountUid`.

### 14.9 Identity comparison

For a bound account:

- matching `accountUid` may satisfy identity proof;
- mismatching `accountUid` -> `AUTH_MISMATCH -> REVIEW_REQUIRED`;
- weak fields and `accountId` do not auto-resolve mismatch.

### 14.10 Permission union and write-scope source

Verify:

`M2_ADOPTED_ENDPOINT_GROUP_UNION = {판매자정보}`

and that separate `상품` permission evidence does not make product endpoints callable.

If M2 records `write_scope`, its permission evidence comes from the `PERMISSIONS_SCOPES.md` provider-admin attestation/introspection path, not from a hidden product API probe.

### 14.11 NOT_ADOPTED fail-closed

Attempt to resolve a candidate M5 endpoint through M2 runtime configuration.

Also attempt a direct/raw SmartStore URL call from feature/adaptor code in a controlled static/test fixture.

Expected:

- local rejection;
- zero provider network calls;
- no raw-URL fallback;
- no raw HTTP client available to ordinary SmartStore feature/adaptor code.

### 14.12 Redirect fail-closed

Inject 3xx/308 for each adopted endpoint.

Expected:

- no automatic follow;
- no credential/body forwarding;
- event retained as contract/error evidence.

---

## 15. Runtime evidence before `verified_at`

`verified_at` remains null until real authorized M2 evidence covers both adopted endpoints.

| Endpoint | Minimum measured evidence |
| --- | --- |
| `SMARTSTORE_AUTH_TOKEN` | method/wire URL, content type, SELF body shape, absent `account_id`, applied timeout policy, HTTP status, token result fields, observed `expires_in`, sanitized trace/evidence reference |
| `SMARTSTORE_SELLER_ACCOUNT` | method/wire URL, applied timeout policy, current session generation, HTTP status, observed `accountUid`, `accountId` when available, expected identity, comparison result, sanitized trace/evidence reference |

Also prove:

- no `NOT_ADOPTED` M5 endpoint was called;
- ordinary SmartStore integration code did not bypass the registry through a raw HTTP client;
- redirects were not automatically followed;
- success predicates executed before READY transitions;
- evidence belongs to the correct credential/session generations;
- secrets and unrelated PII were sanitized.

---

## 16. Change control

Before any endpoint becomes `ADOPTED`:

1. confirm current official method/path/schema;
2. confirm application-mode eligibility;
3. confirm required groups;
4. freeze connect/read timeout policy;
5. freeze a machine-checkable success predicate;
6. freeze endpoint/domain error behavior;
7. freeze redirect behavior;
8. for writes, freeze idempotency/replay and `remote_outcome` reconciliation;
9. freeze read-back/consistency rules;
10. add acceptance tests/evidence requirements;
11. independently audit before implementation use.

Upstream changes do not silently rewrite this matrix.

---

## 17. Open questions

| Question | Status |
| --- | --- |
| Real runtime behavior and latency of both M2 adopted endpoints | `PENDING M2 ACCEPTANCE` |
| Whether M2 timeout values need adjustment after measured latency | `MEASURE, THEN REVIEW` |
| Token remote-success/local-commit-unknown behavior | `OWNED BY AUTH.md / MEASUREMENT REQUIRED` |
| Exact M5 registration endpoint set | `NOT_ADOPTED / M5 DESIGN REQUIRED` |
| Exact M5 required API-group union | `NOT FROZEN` |

---

## 18. Final M2 contract

`ADOPTED_ENDPOINTS = {SMARTSTORE_AUTH_TOKEN, SMARTSTORE_SELLER_ACCOUNT}`

`POST /external/v1/oauth2/token = ADOPTED`

`GET /external/v1/seller/account = ADOPTED`

`registry path = relative to base_url`

`product/category/image endpoints = NOT_ADOPTED for M2`

`M2 adopted group union = {판매자정보}`

`상품 permission observation != endpoint adoption`

`M2 write_scope evidence != hidden product network probe`

`token connect/read timeout = 5s/30s`

`seller-account connect/read timeout = 5s/10s`

`2xx != semantic success until success_predicate passes`

`unsupported token_type != usable token`

`token success != session commit`

`session commit != account identity proof`

`accountUid = required primary identity`

`accountId = secondary evidence when available`

`SmartStore feature/adaptor code != raw HTTP client owner`

`all provider calls -> adopted endpoint registry caller`

`NOT_ADOPTED = no network call`

`future mutation adoption requires timeout + idempotency + read-back + remote-outcome contract before use`

Unknown or unfrozen endpoint behavior remains unavailable until explicitly adopted.
