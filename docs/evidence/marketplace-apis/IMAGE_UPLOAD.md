# Image Upload

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_RELEASE_NOTE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

### Provenance

- **Locators:** `상품 이미지 다건 등록` https://apicenter.commerce.naver.com/docs/commerce-api/current/upload-product ; support https://github.com/commerce-api-naver/commerce-api/discussions/3467
- **Authority / version:** `OFFICIAL_API_DOC`, NAVER Commerce API 2.89.0 (2026-09-15); `OFFICIAL_SUPPORT` (NAVER maintainer answer #3467) for the URL-use line.
- **Source IDs / decisions:** `NAVER-P0-PACKET-289` (Issue #89 comment 5746489554); the IMAGE UPLOAD amendment decisions Issue #89 comments 5765557497 and 5765663972 (recorded in `ENDPOINT_MATRIX.md` §4.1 and ADR-0014 §17.1); research packet 1 (Issue #140 comment 5857814524).

### Provider contract

| Field | Provider fact | Source |
| --- | --- | --- |
| Method / path | `POST /v1/product-images/upload` | 5746489554 |
| Auth / group | `Authorization: Bearer {token}`; API group `상품` | 5746489554 |
| Request Content-Type | `multipart/form-data` | 5746489554; research packet 1 |
| Multipart part | image files are sent in the `imageFiles` part | 5765557497, 5765663972 (`ENDPOINT_MATRIX.md` §4.1) |
| Limits | up to 10 images per request; JPG, GIF, PNG or BMP | research packet 1; upload-product page (PR #141 pass) |
| Success | HTTP `200`; the response carries `images[].url` | 5765557497, 5765663972 (`ENDPOINT_MATRIX.md` §4.1) |
| Use of the URL | the URL returned here is used directly in `originProduct.images.*.url` (`representativeImage.url`, `optionalImages[].url`); no additional transform/finalize step is required; CREATE accepts only URLs returned by this API | `OFFICIAL_SUPPORT` #3467 (5746489554); 5861477977 |
| Errors | the page documents `308`, `400`, `401`, `403`, `404`, `500` with the JSON error body `code` / `message` / `invalidInputs` / `timestamp` ([ERRORS](ERRORS.md#smartstore)) | upload-product page (PR #141 pass) |
| Idempotency | no idempotency key or replay guarantee captured | — |

### Unresolved (exact)

- Per-file size and pixel/dimension limits.
- The response envelope beyond `images[].url`, and the ordering/correspondence rule between uploaded files and returned URLs for a multi-file request.
- Endpoint-specific error codes and whether a multi-file request can partially succeed.
- Lifetime/expiry of an uploaded URL that no CREATE references.
- Any idempotency, timeout or response-loss semantics.

### ICBM adoption and runtime state — ICBM-side, not provider facts

- `SMARTSTORE_PRODUCT_IMAGE_UPLOAD` is `ADOPTED` (M5 IMAGE UPLOAD amendment; `ENDPOINT_MATRIX.md` §4, §4.1; ADR-0014 §17.1): one artifact per request in one `imageFiles` part; no query keys; only `url` is retained; `endpoint_mapping_revision = m5-image-upload-r1`. A successful `images[].url` may cross the `PreparedAsset` boundary only when exactly one safe URL corresponds to that artifact and candidate fingerprint.
- ICBM policy: redirect `NO_FOLLOW`, connect `5s`, read `30s`, no automatic retry; any possibly transmitted failure is `UPLOAD_UNKNOWN`, distinct from `RegistrationIntent.UNKNOWN`.
- Runtime: `UNVERIFIED`. Execution is `DRY_RUN`/provider-zero and no application route invokes the upload.

## Coupang

- **Locator:** https://developers.coupang.com/en/api/products/product-creation — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the product-create schema contains image fields; no standalone upload endpoint captured.
- **Missing:** whether a standalone upload exists, and every image-field rule.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/categories/4406596840975-API-Docs — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the product docs require the image-upload API while preparing create data.
- **Missing:** the upload method/path and every request/response fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/20 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the product schema carries image data; no standalone upload contract captured.
- **Missing:** image-field keys/rules and any upload endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiNo=171&menuIdx=4 (affiliate product registration) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the captured (affiliate) product create accepts downloadable content-file URLs; no standalone upload contract captured.
- **Missing:** URL field keys/rules, a general-seller contract, any upload endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055 — `OFFICIAL_RELEASE_NOTE`, observed 2026-09-28 (PR #141 pass).
- **Captured:** the New product API includes media/detail areas; no standalone upload contract captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
