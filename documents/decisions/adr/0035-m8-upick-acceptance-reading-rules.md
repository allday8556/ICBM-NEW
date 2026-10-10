# ADR-0035 — Restrictions in the product name, region surcharges, and a per-quantity minimum row

Status: **ACCEPTED** 2026-10-10. The U-PICK acceptance run (ADR-0030 §8.3) found three readings that the templates must change. The owner decided all three:
- a sales-channel restriction written in the product name is read;
- a region surcharge is priced at the base fee, and its words are kept;
- a minimum row that states one minimum per quantity yields the per-unit minimum.

This ADR lands before its code.

Decision owners:
- **Product direction:** the owner, in Issue #219 `6097181737` (recorded from chat on 2026-10-10):
  - "상품명에 적힌 판매금지 문구도 판매채널 제한으로 읽는다";
  - region surcharges: "기본 배송비로 계산";
  - a per-quantity minimum: read the one-unit amount as the per-unit minimum. Multi-quantity selling is a later, separate decision.
- **Already decided by canon:**
  - ADR-0010 §7: facts are quoted from the page and never guessed;
  - ADR-0030: templates and site words;
  - ADR-0031: sales-channel restrictions and their fail-closed combinations;
  - ADR-0032: price roles and the shipping-range reading;
  - ADR-0034: the description minimum and the free-over pricing rule.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- §1 to §3, in the `cafe24` and `godomall` templates;
- a U-PICK site revision that adds the restriction phrases its pages use.

What it does not authorize:
- **No multi-quantity listing.** That covers SmartStore 복수구매할인, quantity options and N-pack listings, and needs its own ADR.
- **No change for KM통상.** Its parser and `kmretail-4` are unchanged; its pages state no restriction in the name and no per-quantity minimum.
- **No real read** beyond the owner-approved acceptance runs.

Sources:
- Issue #219: `6097077145` (the acceptance go-ahead and the five products), `6097181737` (run 1 and the decisions);
- ADR-0030 §2, §8.3; ADR-0031 §3; ADR-0032 §4; ADR-0034 §1, §2.

Recorded by: Claude Code (Track B). The number was confirmed free on main and on every open branch.

Date: 2026-10-10

---

## Context

U-PICK's acceptance run 1 (`cafe24-2+upick-1`, five products the owner named) checked every fact against the signed-in source page.

**Read correctly:** names, member prices, minimums, shipping ranges, stock (one sold-out product) and options (U-PICK sells no option product).

**Three findings:**
- **4992.**
  - Its channel row reads `모든마켓 판매 가능`, but its product name ends `- 쿠팡 등록 불가`.
  - ADR-0031 §3 reads only the channel row and the description, so the fact was `ALL_ALLOWED`: confidently wrong.
  - 4072's name likewise ends `- 쿠팡 판매 금지`.
- **Region surcharges on most products.**
  - Most products carry a `추가배송비` row, e.g. `제주도 3,000원/도서산간 5,000원 추가`.
  - The `cafe24` template ignores that row, so its words are lost.
  - ADR-0034 §2 sends a Godomall region layer that states an amount to review.
  - The two templates disagree, and neither keeps the words.
- **4072's minimum row.**
  - It reads `1개 13,900원 이상/ 2개 27,500원 이상 / 3개 40,800원 이상 …`.
  - A money cell must state one amount, so the field was `REVIEW_REQUIRED`.
  - The other quantities are minimums for multi-unit sales, which ICBM does not list yet.

## Decision

### 1. A restriction in the product name

This amends ADR-0031 §3, "Where the words are read".

- **Where.** The product name is read for the **restricting** phrases: `channel_closed_only`, `channel_forbid_coupang` and `channel_forbid_smartstore`.
  - The name is the text the template already reads as `original_name`'s source: the name row, and the title.
  - `channel_all_allowed` is never read from a name.
- **How it combines.** A restriction found in the name combines exactly as one found in the description (ADR-0031 §3):
  - a name restriction beside an all-allowed channel row → `REVIEW_REQUIRED`;
  - a name restriction with no row → `LISTED`, or `CLOSED_MALL_ONLY`.
  - Nothing is inferred from words that are not a configured phrase.
- **Phrases.** U-PICK's site revision adds the phrases its pages use for Coupang: `쿠팡 판매 금지`, `쿠팡 등록 불가`, `쿠팡,토스 판매금지`.
  - Matching ignores whitespace, as ADR-0031 §3 says.
  - A phrase naming a marketplace ICBM does not list (토스) restricts nothing else.

### 2. A region surcharge is priced at the base fee

This amends ADR-0034 §2.

- **The fact keeps the words.** A region-surcharge statement goes into the shipping fact's `policy_text` beside the base fee:
  - the `cafe24` template's `추가배송비` row (a site label slot);
  - the `godomall` template's `지역별배송비` layer.
- **The amount is not priced.** The fact's kind and fee are what the base fee alone gives (`FIXED`, `FREE`, or the free-over `CONDITIONAL`). The surcharge applies only to orders to those regions.
- **What stays under review:**
  - a surcharge statement that cannot be kept as words (URL material) → `REVIEW_REQUIRED`;
  - a surcharge row stated twice with different words → `REVIEW_REQUIRED`.
- **What this replaces.** ADR-0034 §2's sentence "A region-fee layer that states any amount is held for review" is replaced by this section.

### 3. A per-quantity minimum row

This amends the money-cell rule for `minimum_sale_price` rows (ADR-0030 §2, ADR-0032).

- **When it applies.** The cell is exactly a list of quantity minimums. The first is `1개 N원 이상`, and every other entry is `k개 M원 이상` with distinct quantities `k ≥ 2`. Separators are `/`, `,` or line breaks.
- **What it reads.** `minimum_sale_price` is `N`, the one-unit amount.
  - The evidence quotes the whole cell, with the reading normalized to `N`.
  - The other quantities are kept only in that evidence. They are never divided into a unit price and never applied to a single unit.
- **What stays under review:**
  - a cell whose first entry is not `1개`;
  - a cell that mixes other words;
  - a cell with a repeated quantity;
  - a cell with any amount the list shape does not account for.

## Invariants

- **NR-01** Only restricting phrases are read in a product name. An allowed reading never comes from a name.
- **NR-02** A name restriction beside an all-allowed row is `REVIEW_REQUIRED`.
- **RS-01** A region surcharge never changes the priced fee, and its words are kept in `policy_text`.
- **QM-01** A per-quantity minimum row yields its `1개` amount and nothing else. Any other shape stays `REVIEW_REQUIRED`.

## Slices

| # | slice |
| --- | --- |
| U0 | this ADR |
| U1 | `cafe24-3` and `godomall-2` applying §1 to §3, plus the fail-closed text fixes found in run 1 (a text made only of invisible characters is empty; a no-break space is a space); `upick-2` with the phrases of §1 and the `추가배송비` label; offline proof on reduced captures |
| U2 | U-PICK acceptance run 2 on the redeployed build, then the run from a fresh server process; the record `documents/acceptance/suppliers/upick.md`; the flip to `ACTIVE` |

Each slice finalizes in its turn of the global merge lane.

## Consequences

- 4992-like products are held for review instead of being allowed on Coupang.
- Most U-PICK products keep their region-surcharge words and are priced at the base fee.
- A per-quantity minimum becomes a usable single-unit minimum. Multi-quantity listing waits for its own ADR.

## References

- ADR-0010 §7
- ADR-0030 §2, §8.3
- ADR-0031 §3
- ADR-0032
- ADR-0034 §1, §2
- Issue #219 `6097077145`, `6097181737`
