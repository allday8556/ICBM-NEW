# 건강산 — acceptance record (ADR-0030 §8.3)

**Verdict: PASS.** 건강산 is flipped to `ACTIVE` by the PR that adds this record (ADR-0030 PT-07).

## Site and go-ahead

- **Site.** 건강산, `https://www.ggsan.com`. Platform: Godomall.
- **Configuration.** `integrations/suppliers/sites/ggsan.json`.
- **Owner go-ahead.** Issue #219 `6097929512`. The owner named five products: 1000001010, 1000004584, 1000004509, 1000003785 and 1000004433.
- **Diversity.**
  - 1000004509 is sold out.
  - 1000001010 and 1000003785 state per-quantity minimums in their descriptions.
  - 1000004433 and 1000004584 state restrictions in words, but those name no marketplace ICBM lists.
  - No option product was among the picks, and none was seen in reconnaissance.

## Signed-in reads

- **Collections.** They ran through the application's own route and its CONNECT session, using the credentials the owner stored.
- **Source pages.** The owner signed in to the built-in browser and the agent read the member pages there.
  - Godomall keeps one session per member, so the two sign-ins displace each other. The application signs in again with its stored credentials.
  - Only product rows, purchase controls and the description were read.

## The bar (§8.3)

- no confidently wrong fact;
- every CORE field `CONFIRMED`, or explicitly `REVIEW_REQUIRED` / `ABSENT` under ADR-0013 ruling B;
- every read within the caps;
- the same verdicts on a repeat from a fresh server process.

## 1. Preview run 0 (2026-10-10, `godomall-1+ggsan-1`)

Run 0 was not an acceptance run. Every fact was checked against the member page. It found five problems:
- the hidden floating cart layer (`#frmCartTabViewLayer`) copies the order list, so on-sale options went REVIEW and the sold-out product's options were `ABSENT` (fail-open);
- the 287-entry `지역별배송비` list held shipping for review;
- 1000001010's `(1개)12,900원 이상 …` minimum was recorded `ABSENT` (confidently wrong);
- pacing dropped `?goodsNo=`;
- the signed-out mypage redirects by script (#305).

All are fixed in `godomall-2` / `ggsan-2` (#309, ADR-0035).

## 2. Acceptance run 1 (2026-10-10 20:55–20:57 UTC, build main `14254292`, `godomall-2+ggsan-2`, fingerprint `db55736a2d4e…`)

| product | list / purchase | minimum | shipping (priced) | stock | options | channels | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1000001010 | 20,000 / 7,500 | **12,900** (`(1개)`, ADR-0035 §4) | CONDITIONAL 3,000, free over 200,000; `287개 지역 3,000원~5,000원` | on sale | ABSENT | ABSENT | — |
| 1000004584 | 29,970 / 13,500 | 19,980 | CONDITIONAL 3,000 / 200,000; region summary | on sale | ABSENT | ABSENT | "쿠팡 … 최저가 매칭 절대금지" is a pricing condition, not a channel ban |
| 1000004509 | 50,000 / 25,000 | none | FIXED 3,000 | **SOLD_OUT** | **REVIEW** (no order list in the form) | ABSENT | — |
| 1000003785 | 49,000 / 23,000 | 45,000 | CONDITIONAL 3,000 / 200,000; region summary | on sale | ABSENT | ABSENT | one detail image over 5 MiB → `REVIEW_REQUIRED` / `OVERSIZE` (cap enforced) |
| 1000004433 | 55,000 / 18,000 | 26,900 | CONDITIONAL 3,000 / 200,000; region summary | on sale | ABSENT | ABSENT | "(티몬,위메프 판매금지)": no marketplace ICBM lists |

- Every value matches the member page checked in run 0.
- **No confidently wrong fact.**
- `notice` is `REVIEW_REQUIRED` on the products that show a notice table.

## 3. Repeat from a fresh server process (20:58–21:00 UTC)

- 8790 was restarted on the same build, 건강산 reconnected, and the five products were collected again.
- **Result: zero differences.** Every field's status, value and fingerprint, and every image's role, asset and status, is identical to run 1.

## 4. Caps

- One product read per collection.
- Images per product: 3–4.
- Largest accepted asset: 3,846,217 bytes.
- The one image over 5 MiB was refused and held (`OVERSIZE`), never stored.
- Pacing now runs per `goodsNo`.

## 5. Verdict and flip

**PASS** on every line of the bar.

The flip PR sets `status: ACTIVE` and advances the site revision from `ggsan-2` to `ggsan-3`. The status change is the only difference between them.

**For the owner.** Some 건강산 description images are larger than the 5 MiB per-image limit (Issue #219 `6086299406`). Those products keep that image under review.
