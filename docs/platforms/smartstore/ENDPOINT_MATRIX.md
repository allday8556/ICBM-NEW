# SmartStore Endpoint Matrix

## Status

| Field | Value |
| --- | --- |
| Provider | NAVER SmartStore / Commerce API |
| Contract status | `M5_READBACK_IMAGE_UPLOAD_AND_CREATE_ADOPTED_FROZEN_FOR_REVIEW` |
| M2 integration mode | `OWN_STORE_SELF` |
| Adopted endpoint count | `6` (2 M2 CONNECT + 2 M5 read-backs + 1 M5 image upload + 1 M5 product CREATE) |
| M5 endpoints | `4 ADOPTED (2 read-back, 1 image upload, 1 product CREATE), 8 NOT_ADOPTED with recorded gaps` |
| Runtime verification | `PENDING` |
| Upstream version | `2.88.0` (M2 rows) / `2.89.0` (M5 PR-D rows, packet 5746489554; M5 image upload, Issue #89 decisions 5765557497 and 5765663972; M5 product CREATE, reviews 5768199984 and 5768247290 under ADR-0020 §4) |
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
- https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product (release 2.89.0, read through the Issue #89 official evidence reviews 5768199984 and 5768247290; `SOURCES.md` §5.2)
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
| `SMARTSTORE_PRODUCT_CREATE_V2` | `ADOPTED` | M5 CREATE adoption slice | `POST` | `/v2/products` | Product CREATE (`application/json`) | `OWN_STORE_SELF` | `상품` | Yes; no provider idempotency, no automatic retry, never resent on `UNKNOWN` (§4.3) |
| `SMARTSTORE_ORIGIN_PRODUCT_READ_V2` | `ADOPTED` | M5 PR-D | `GET` | `/v2/products/origin-products/{originProductNo}` | Origin-product read-back | `OWN_STORE_SELF` | `상품` | No |
| `SMARTSTORE_CHANNEL_PRODUCT_READ_V2` | `ADOPTED` | M5 PR-D | `GET` | `/v2/products/channel-products/{channelProductNo}` | Channel-product read-back | `OWN_STORE_SELF` | `상품` | No |
| `SMARTSTORE_PRODUCT_IMAGE_UPLOAD` | `ADOPTED` | M5 IMAGE UPLOAD amendment | `POST` | `/v1/product-images/upload` | One-artifact image upload (`multipart/form-data`, `imageFiles`) | `OWN_STORE_SELF` | `상품` | Side effect; durable upload-attempt owner provider-zero (ADR-0018 §3.4, migration `0026`); no ASSET sender wired |
| `SMARTSTORE_CATEGORY_LIST` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/categories` | Category discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_CATEGORY_READ` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/categories/{categoryId}` | Category validation | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_PRODUCT_ATTRIBUTE_LIST` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/product-attributes/attributes` | Attribute discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/product-attributes/attribute-values` | Attribute-value discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_STANDARD_OPTIONS` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/options/standard-options` | Standard-option discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_NOTICE_TYPES` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/products-for-provided-notice` | Notice-type discovery | `OWN_STORE_SELF` | `상품` | No |
| `SMARTSTORE_NOTICE_TYPE_READ` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/products-for-provided-notice/{productInfoProvidedNoticeType}` | Notice-type read | `OWN_STORE_SELF` | `상품` | No |
| `SMARTSTORE_PRODUCT_SEARCH` | `NOT_ADOPTED` | M5 candidate | `POST` | `/v1/products/search` | Duplicate lookup candidate | `OWN_STORE_SELF` | `상품` | No |

The remaining M5 rows are planning metadata only. Presence does not imply eventual adoption.

### 4.1 M5 PR-D adoption and its recorded gaps (packet 5746489554, release 2.89.0)

PR-D adopted the two product read-backs. Issue #89 decisions `5765557497` and `5765663972`
subsequently adopted IMAGE UPLOAD, and the CREATE adoption slice (ADR-0020 §4 slice 1) adopted
product CREATE under the contract of §4.3. The application remains `DRY_RUN`/provider-zero,
`product_registration.write` stays `UNVERIFIED`, and no application route invokes the upload or the
CREATE caller.

| Endpoint | Why it is still `NOT_ADOPTED` |
| --- | --- |
| `SMARTSTORE_PRODUCT_CREATE_V2` | **Closed for the endpoint contract by the CREATE adoption slice (§4.3, ADR-0020 §4).** The row is now `ADOPTED`: the success predicate, the timeouts, the redirect policy, the retention profile and the outcome classification are frozen in §4.3, and the provider's **absent** idempotency is recorded rather than assumed. The CREATE **body**'s structure is not part of that freeze and stays unproven, so no unit is sendable (§4.3, §17). The verdict below is unchanged |
| `SMARTSTORE_PRODUCT_SEARCH` | existence only: no request schema, so no strong duplicate key and no name filter is proven |
| `SMARTSTORE_PRODUCT_ATTRIBUTE_LIST` / `_VALUES` / `SMARTSTORE_STANDARD_OPTIONS` | each needs a category query key the packet does not name |
| `SMARTSTORE_CATEGORY_LIST` / `_READ`, `SMARTSTORE_NOTICE_TYPES` / `_TYPE_READ` | no response field is proven, so a deny-by-default retention profile would keep nothing |

**The CREATE and reconcile evidence review closed `INSUFFICIENT`** (Issue #89 `5768247290` →
`5768312853` → `5768347233`; ADR-0014 §17.2). Beyond the packet gaps above, the official contract
proves no CREATE idempotency or ambiguous-outcome replay safety, no `sellerManagementCode`
uniqueness, and no read-after-write freshness that would make a zero-result lookup an authoritative
absence — a seller-code search may return similar, partial or exact matches. No implementation may
proceed on the assumption that a deterministic provider lookup exists. The verdict stays
`INSUFFICIENT` and is not overturned, and overturning it is not the adoption condition (ADR-0014
§17.2, §28; ADR-0018 §6.1): CREATE and the positive-only reconcile path each need their own
separately authorized adoption slice — CREATE recording the provider's actual (absent) idempotency
and bound to the §28 never-resend rule (its slice landed, §4.3), SEARCH for positive-only reconcile
only (still to come). An `UNKNOWN` CREATE is never resent, a zero-result lookup never proves
absence, and the residual-risk acceptance stays a separate canary prerequisite.

Adopted read-back contract, in the registry and pinned by tests:

| Field | Both read-backs |
| --- | --- |
| Auth | `Authorization: Bearer {token}`, `AUTH_MODE=SELF` unchanged |
| Content type | none (no request body) |
| Timeouts | connect `5s`, read `15s` (ICBM policy) |
| Redirect | `NO_FOLLOW` |
| Success predicate | HTTP 200 AND the body parses as a JSON object (`m5d-origin-read-r1`, `m5d-channel-read-r1`) |
| Safe query keys | **none** (deny-by-default) |
| Retained response fields | `name`, `salePrice`, `stockQuantity`, `sellerManagementCode`, `sellerManagerCode`, `url` |

Adopted image-upload contract: bearer auth; `POST /v1/product-images/upload`; one artifact in one
`imageFiles` multipart part; HTTP 200 with `images[].url`; no query keys; only `url` is retained.
ICBM policy is no redirect, connect `5s`, read `30s`, no automatic retry. Any possibly transmitted
failure is `UPLOAD_UNKNOWN`, distinct from `RegistrationIntent.UNKNOWN`.

`endpoint_mapping_revision = m5-product-create-r1`, bound to the registry fingerprint, which also
covers the safe-retention profile `smartstore-safe-retention/v1` (ADR-0014 §15). The superseded
revisions `m2-connect-r1`, `m5-register-r1` and `m5-image-upload-r1` stay resolvable, so stored
evidence still names a known mapping.

### 4.2 CREATE wire-contract evidence — the evidence record the adopted contract of §4.3 is frozen from (`SOURCES.md` §5.2, release 2.89.0)

The architect's official evidence reviews on Issue #89 (`5768199984`, `5768247290`) establish the
following for `SMARTSTORE_PRODUCT_CREATE_V2`. **This section is the evidence record only.** The
contract frozen from it is §4.3, added by the CREATE adoption slice (ADR-0020 §4). Nothing here
grants LIVE authority.

| Field | Evidence | Source |
| --- | --- | --- |
| Method / path | `POST /v2/products` (`(v2) 상품 등록`) | packet 5746489554 |
| Auth | `Authorization: Bearer {token}`, `AUTH_MODE=SELF` unchanged, API group `상품` | packet 5746489554 |
| Request media type | JSON — Commerce API messages are JSON except file upload and download, so the body is `application/json` | review 5768199984 |
| Request body | the documented `originProduct` + channel-product structure (`원상품 정보 구조체`) | packet 5746489554, review 5768199984 |
| Documented success | HTTP `200` | reviews 5768199984, 5768247290 |
| Success identifiers | `originProductNo`, `smartstoreChannelProductNo`, `windowChannelProductNo` (since API docs `v2.68.0`) | review 5768199984 |
| Success product data | `originProduct`, the product data SmartStore successfully stored | review 5768199984 |
| Error contract | not re-enumerated per endpoint at `2.89.0`; the product-API mappings of `ERRORS.md` §10 apply — `BAD_REQUEST` read from `invalidInputs` **and** `message` (the documentation warns `invalidInputs` can be absent or insufficient), `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `INTERNAL_SERVER_ERROR`, and `308/PERMANENT_REDIRECT` never followed for a mutation (§11; `ERRORS.md` §17) | `ERRORS.md` §10 (`NAVER-P0-PRODUCT-CREATE`, `2.88.0`) |
| Idempotency | none: no idempotency key, request-correlation key, replay rule or duplicate-prevention guarantee | review 5768247290 |

Not proven, so fail-closed until an adoption slice or new evidence settles it:

- the exact success response `Content-Type` header value and its charset (a JSON body is proven, the
  header parameters are not);
- a per-endpoint enumeration of the CREATE error statuses at `2.89.0` (the `ERRORS.md` §10 mappings
  are the contract in the meantime).

Outcome rules that no evidence here changes:

- a timeout, a connection loss, a response loss or a `5xx` after transport handoff is `UNKNOWN`
  (`ERRORS.md` §14.3, §15.2) and **a CREATE in `UNKNOWN` is never resent** (ADR-0014 §28; ADR-0018
  §6.1, G3-07);
- **neither a `500` nor a zero-result lookup proves that a product was not registered** (ADR-0014
  §17.2, §28.2; ADR-0018 G3-15);
- the provider-evidence verdict stays `INSUFFICIENT` for idempotent replay and remote-absence proof,
  the canary stays `BLOCKED`, and execution stays `DRY_RUN`.

The code-side gap text for this row was carried by
`integrations/marketplaces/smartstore/registry.py` `ADOPTION_GAPS` until the CREATE adoption slice.
That entry is now gone, because the row is `ADOPTED` and a gap list records only unadopted
endpoints; the frozen contract is §4.3. The **body** gaps `product.py` records are a different
thing and stay (§4.3, "What is still unproven").

### 4.3 The adopted CREATE contract (ADR-0020 §4 slice 1)

`SMARTSTORE_PRODUCT_CREATE_V2` is `ADOPTED` from the evidence of §4.2. Adoption is an endpoint
contract in code — a request type, a response type, an error and outcome classification — and
**nothing else**. It is not a session, not a LIVE grant and not a canary: execution stays
`DRY_RUN`, `product_registration.write` stays `UNVERIFIED`, the provider-evidence verdict stays
`INSUFFICIENT` (§4.1), `SMARTSTORE_PRODUCT_SEARCH` stays `NOT_ADOPTED`, and the canary stays
`BLOCKED` on every other condition of ADR-0018 §6 and §10.

**What this adoption freezes, and what it explicitly does not.** It freezes the operational contract
fields §2 and §16 require before a `NOT_ADOPTED → ADOPTED` change: the method and path, the app
mode, the required group, the request media type, the timeout policy, the redirect policy, the
machine-checkable success predicate, the error and outcome classification, the deny-by-default
retention profile and, for this write, the provider's recorded **absent** idempotency together with
the read-back identity and the never-resend rule that stands where a reconcile path does not yet
exist. It does **not** freeze the CREATE body's internal structure: no reviewed evidence proves it
(see "What is still unproven" and §17), so the body is **not adopted**, the wire projection
assembles no document, and this endpoint is adopted **and unsendable** — every unit refuses locally,
before any network I/O. Adoption is therefore the contract a later, separately authorized body slice
would send under, never a statement that a CREATE can be sent. ADR-0018 §6.1's first bullet is
closed for the response and the error classification and for this request contract; the part of it
that is the body's structure stays open (ADR-0018 §6.1 amendment note).

| Field | Adopted value | Source |
| --- | --- | --- |
| Method / path | `POST /v2/products`, relative to `base_url` (§3) | packet 5746489554 |
| Auth | `Authorization: Bearer {token}`, `AUTH_MODE=SELF`, API group `상품` | packet 5746489554 |
| App mode | `OWN_STORE_SELF` | packet 5746489554 |
| Request media type | `application/json` | review 5768199984 |
| Request body | **not frozen, not adopted** — a body may only ever come from the frozen `RegistrationSnapshot`'s own projection, and that projection refuses to assemble one while the documented `originProduct` container and the channel-product structure beside it are unproven (see "What is still unproven"); the shape named in §4.2 is the evidence record, never a frozen request schema | packet 5746489554, review 5768199984 |
| Safe query keys | **none** (deny-by-default) | ICBM policy |
| Timeouts | connect `5s`, read `30s` | ICBM policy (§10) |
| Redirect | `NO_FOLLOW`; a 3xx is never followed for a mutation and is recorded as ambiguous | ICBM policy (§11); `ERRORS.md` §10.6, §17 |
| Success predicate | HTTP `200` **AND** the body parses as a JSON object **AND** it carries a usable `originProductNo` **AND** at least one usable channel-product number (`smartstoreChannelProductNo` / `windowChannelProductNo`) **AND** a non-empty `originProduct` stored-result object (`m5-product-create-r1`) | review 5768199984 (documented success, identifiers and stored product data) |
| Retained response fields | `originProductNo`, `smartstoreChannelProductNo`, `windowChannelProductNo`, plus the product profile `name`, `salePrice`, `stockQuantity`, `sellerManagementCode`, `sellerManagerCode`, `url` | review 5768199984; ADR-0014 §15 |
| Provider idempotency | **none** — recorded as `NONE_DOCUMENTED`, never assumed | review 5768247290 |
| Automatic retry budget | `0`: the adapter has no retry loop at all | ADR-0014 §9, §28.3 |
| Read-back identity | `originProductNo`, the identity `SMARTSTORE_ORIGIN_PRODUCT_READ_V2` is performed by; the channel numbers are retained with it so neither provider identity is lost | ADR-0014 §11, §28.2 |

**The predicate is the whole documented success document, and that is why it is stricter than
the read-backs'.** The reviews document what a successful CREATE answers with: the origin-product
number, the channel-product numbers and the `originProduct` data SmartStore stored. A 200 that
carries less is not that response — ICBM would have no provider identity to read back by, or no
stored result to compare — so it fails the predicate and is classified as an ambiguous outcome
(§9) rather than reported as a success with a missing part.

One qualification is stated rather than guessed: **the channel-product numbers are required as a
family, not individually.** Which channels a seller has is not a fact any review proves — a seller
without 쇼핑윈도 has no `windowChannelProductNo` — so at least one of the two must be usable.
Demanding both would turn a documented success into a false `UNKNOWN`, and an `UNKNOWN` CREATE is
never resent and is not resolvable by absence (ADR-0014 §28).

**Passing the predicate is not registration success.** ADR-0014 §11 confirms a registration only
through read-back and an exact comparison against the immutable Snapshot. A successful CREATE
proves `remote_outcome = APPLIED_PROVEN` and nothing more.

**Outcome classification** (`ERRORS.md` §2, §14, §15; ADR-0014 §28.3; ADR-0018 §6.1). The cause and
the mutation outcome are independent axes, and a transient or rate-limited cause never makes a
replay safe.

| CREATE evidence | `remote_outcome` |
| --- | --- |
| a local pre-submit refusal before transport handoff — an unprojectable Snapshot, a request-contract violation, an unusable bearer | `NOT_APPLIED_PROVEN` (`ERRORS.md` §15.1 item 1) |
| transmission-precluded transport evidence — ICBM's own egress refusal, or a DNS / TCP-connect / TLS-handshake failure on a connection this request opened | `NOT_APPLIED_PROVEN` (`ERRORS.md` §15.1) |
| a **definitive provider rejection**: a complete response with status `400`, `401`, `403`, `404`, `405`, `409` or `415` that carries a provider code attributing it to the API-server layer (`ERRORS.md` §5.3) and not to the gateway | `NOT_APPLIED_PROVEN` — the reviewed provider proof ADR-0014 §10's table and §28.3 admit; it is recorded on its own Attempt and **never passes through `UNKNOWN`** |
| any gateway-attributed code, whatever the status | `UNKNOWN` — `ERRORS.md` §25 Q2 is open, so a pre-service gateway rejection is not proven non-application |
| an **unattributed** response of one of those statuses: the body did not parse, or it carries no provider code | `UNKNOWN` — no layer is identified (`ERRORS.md` §5.1, §5.2, §8 Step 5), and a code-less `403` is as consistent with a pre-service gateway refusal as with an API-server one, so application cannot be excluded |
| a read/write timeout, a lost or reset connection, an uncertain transmission, a pooled-connection failure, an unobserved phase | `UNKNOWN` (`ERRORS.md` §15.2) |
| any `5xx`, `408`, `425` or `429` | `UNKNOWN` |
| a `3xx` (never followed) | `UNKNOWN` |
| a `2xx` that fails the success predicate — a missing identifier, a missing channel number, a missing stored `originProduct`, a malformed or truncated body | `UNKNOWN` |

**What an `UNKNOWN` CREATE may never do** (ADR-0014 §28.3, M5-08, M5-33; ADR-0018 G3-07). It is
never resent. It cannot open a CREATE Attempt, no grant is issued for it, its conflict scope stays
closed, and it ends only on the positive-only reconcile of ADR-0014 §28.2 — a separate, later
adoption slice — a read-back by an already known provider identity, or later machine proof of
non-application. **No lookup result, seller-side code, grant, proof or approval ever becomes
remote-absence evidence** (§4.1).

**What is still unproven, and therefore still refused.** Adopting the endpoint does not make any
unit sendable. The CREATE **body** still has parts no reviewed evidence names, and the wire
projection refuses to assemble a document while any of them stands:

- the container shape of `images` (only `images.*.url` is proven);
- the option-combination container and its option-name/value field names;
- the channel-product structure the body pairs with `originProduct` — neither its field name nor
  its shape is named.

Each is removed only by the slice that proves and emits it, in the same change. Until then every
provider-listing unit is unsendable, and that refusal is local, before any network I/O.

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

M5 adopted rows:

| Endpoint ID | Connect timeout | Read timeout | Reason |
| --- | ---: | ---: | --- |
| `SMARTSTORE_ORIGIN_PRODUCT_READ_V2` | `5s` | `15s` | Read-only product read-back. |
| `SMARTSTORE_CHANNEL_PRODUCT_READ_V2` | `5s` | `15s` | Read-only product read-back. |
| `SMARTSTORE_PRODUCT_IMAGE_UPLOAD` | `5s` | `30s` | One artifact upload; no automatic retry. |
| `SMARTSTORE_PRODUCT_CREATE_V2` | `5s` | `30s` | A mutation with no provider idempotency: cutting the read short manufactures the very ambiguity ADR-0014 §28 can never resolve by absence. Still bounded, and an expiry is `UNKNOWN`, never a rejection. |

Rules:

- timeout expiry is classified under `ERRORS.md`/`AUTH.md`; it is not proof of provider rejection;
- token timeout after possible transmission MUST preserve issuance uncertainty rather than invent credential failure;
- values are reviewed from measured M2 latency evidence, not silently tuned per call site;
- implementation SHALL expose the endpoint policy to tests so accidental fallback to client defaults is detectable.

---

## 11. Redirect policy

Generic automatic redirect following is forbidden for SmartStore integration clients unless an adopted endpoint explicitly permits it.

Every adopted endpoint, M2 and M5 alike, is:

`NO_FOLLOW`

For `SMARTSTORE_PRODUCT_CREATE_V2` this is load-bearing rather than conservative: HTTP 308 preserves
the method and body, so an automatically followed redirect would retransmit the CREATE body
(`ERRORS.md` §10.6). The adopted contract follows none, and records a 3xx as an ambiguous outcome
(§4.3).

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

Those values are filled only when M5 actually adopts the endpoint. `SMARTSTORE_PRODUCT_CREATE_V2`
is no longer such a row: its values are frozen in §4.3. `SMARTSTORE_PRODUCT_SEARCH` and the metadata
rows still are.

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
| Exact M5 registration endpoint set | `PARTIALLY ADOPTED`: the two read-backs, the image upload and the product CREATE (§4.3) are `ADOPTED`; `SMARTSTORE_PRODUCT_SEARCH` and the metadata rows stay `NOT_ADOPTED` |
| The CREATE body's image container, option-combination container and channel-product structure | `NOT PROVEN`: outside the adopted freeze, so the body is not adopted, every unit stays unsendable and the projection refuses every payload (§4.3). A CREATE can be sent only after a separately authorized slice proves and emits it |
| Whether the CREATE read timeout needs adjustment after measured latency | `MEASURE, THEN REVIEW` |
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
