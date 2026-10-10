# U-PICK — acceptance record (ADR-0030 §8.3)

**Verdict: PASS.** U-PICK is flipped to `ACTIVE` by the PR that adds this record (ADR-0030 PT-07).

## Site and go-ahead

- **Site.** U-PICK B2B, `https://upickb2b.com`. Platform: Cafe24.
- **Configuration.** `integrations/suppliers/sites/upick.json`.
- **Owner go-ahead.** Issue #219 `6097077145`: "upick 인수 수집 진행해". The owner named five products: 4837, 4072, 4290, 4992, 4955.
- **Diversity.**
  - U-PICK sells no product with options. The owner confirmed this in Issue #219 `6097181737`.
  - 4072 stands in for an option product: it has a per-quantity minimum.
  - 4290 is sold out.

## Signed-in reads

- **Collections.** They ran through the application's own route and its CONNECT session, using the credentials the owner stored in the settings UI. The agent typed no credential and read no credential store.
- **Source pages.** The owner signed in to the built-in browser. Only product rows, option controls and purchase controls were read. No member text was read or kept.

## The bar (§8.3)

- no confidently wrong fact;
- every CORE field `CONFIRMED`, or explicitly `REVIEW_REQUIRED` / `ABSENT` under ADR-0013 ruling B;
- every read within the caps;
- the same verdicts on a repeat from a fresh server process.

## 1. Run 1 (2026-10-10, `cafe24-2+upick-1`): not passed

Every fact was checked against the signed-in source page.
- 4992's `sales_channels` was `ALL_ALLOWED`, but its name says `쿠팡 등록 불가`.
- 4837's `detail_description` was `CONFIRMED` on `﻿﻿`.

The owner's decisions (Issue #219 `6097181737`, `6097576508`) are recorded in ADR-0035 (#307) and implemented in `cafe24-3` / `upick-2` (#309).

## 2. Run 2 (2026-10-10 20:48–20:50 UTC, build main `14254292`, `cafe24-3+upick-2`, fingerprint `4df40f4d4928…`)

| product | name | member price (PURCHASE) | minimum | shipping (priced) | stock | options | channels | description |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 4837 | ✓ | 8,500 | none (`판매가 자율`) | range → 7,000, region words kept | on sale | ABSENT | `CLOSED_MALL_ONLY` | REVIEW (only invisible characters) |
| 4072 | ✓ | 7,200 | **13,900** (per-quantity row, ADR-0035 §3) | range → 4,000 | on sale | ABSENT | `LISTED` coupang (row and name; 토스 words kept) | ✓ |
| 4290 | ✓ | 11,500 | 14,900 | range → 9,000, region words kept | **SOLD_OUT** | ABSENT | `ALL_ALLOWED` | REVIEW (empty) |
| 4992 | ✓ (NBSP fixed) | 16,000 | 24,900 | range → 9,000, region words kept | on sale | ABSENT | **REVIEW**: an all-allowed row beside `쿠팡 등록 불가` in the name (NR-02) | REVIEW (empty) |
| 4955 | ✓ | 14,000 | 29,800 | 3,000, region words kept | on sale | ABSENT | `ALL_ALLOWED` | REVIEW (empty) |

- Every value matches the source page as checked in run 1. Each run 1 finding is now read as ADR-0035 says.
- **No confidently wrong fact.**
- `notice` and the other COVERAGE fields are `ABSENT` or `REVIEW_REQUIRED`, as the pages state.

## 3. Repeat from a fresh server process (20:51–20:53 UTC)

- 8790 was restarted on the same build, U-PICK reconnected with its stored credentials, and the five products were collected again.
- **Result: zero differences.** Every field's status, value and fingerprint, and every image's role and asset, is identical to run 2. Facts status, extraction revision and fingerprint are the same.

## 4. Caps

- One product read per collection.
- Images per product: 5–8, within `max_image_refs` 30.
- Largest asset: 3,142,023 bytes, within the 5 MiB `max_image_bytes`.
- Every image is `CONFIRMED`.
- The same-product interval was respected.

## 5. Verdict and flip

**PASS** on every line of the bar.

The flip PR sets `status: ACTIVE` and advances the site revision from `upick-2` to `upick-3`.
- The status change is the only difference between them: no label, region, host or limit changes.
- The file digest is part of the extraction fingerprint (ADR-0030 §4), which is why the revision advances.
