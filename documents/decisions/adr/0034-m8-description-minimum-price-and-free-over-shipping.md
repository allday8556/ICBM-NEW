# ADR-0034 — A minimum price stated in the description, and a free-over shipping policy priced at its base fee

Status: **ACCEPTED** 2026-10-10. Two reading rules that the first Godomall site, 건강산, needs, both decided by the owner:
- a reseller minimum price that the supplier writes as a sentence in the product description is collected;
- a shipping policy of "a base fee, free over an order amount" is priced at its base fee.

This ADR lands before its code. The `godomall` template slice (ADR-0030 §11 S4) cites it as canon.

Decision owners:
- **Product direction:** the owner, in Issue #219:
  - `6086199255`: 건강산 states its minimum resale price inside the description, and it must be collected reliably.
  - `6088081139`: "기본 배송비로 계산". A conditional policy with a free-over threshold is priced at its base fee for consignment orders.
- **Already decided by canon:**
  - ADR-0010 §7: facts are quoted from the page and never guessed. A conditional shipping policy is never flattened into a fixed fee *in the facts*.
  - ADR-0030: templates and site words.
  - ADR-0032 §4: a fee range is priced at its highest amount.
  - The pricing owner (`app/stages/products/pricing.py`): today a CONDITIONAL shipping fact is `PRICING_SHIPPING_CONDITIONAL`.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- the description-minimum reading of §1, in any template, read only from site words;
- the pricing rule of §2;
- the `godomall` template slice that applies both.

What it does not authorize:
- no change to how the shipping *fact* is recorded: a free-over policy stays `CONDITIONAL`, with its words, its base fee and its threshold;
- no real read;
- no change for KM통상 or U-PICK. Neither states a minimum in its description, and neither has a free-over policy in its captures.

Sources: Issue #219 `6086058056` (건강산 reconnaissance), `6086199255`, `6088081139`; ADR-0010 §7; ADR-0030; ADR-0032.

Recorded by: Claude Code (Track B). The number was confirmed free on main and on every open branch (ADR-0033 is Track A's).

Date: 2026-10-10

---

## Context

The 건강산 reconnaissance (2026-10-10) found two things.

1. **The minimum resale price is a sentence in the description**, not a labelled row:
   - `판매가격절대준수 1개 21,000원 이상 판매 부탁드립니다. (배송비 3,000원 별도)`, with `2개 묶음 40,900원 / 3개 이상 묶음 판매 시 개당 21,000원` and `배송비 포함 24,000원 이상` on the lines around it;
   - `판매가격절대준수 4,400원 이상 판매 부탁드립니다.`
2. **The shipping fee is a base fee that becomes free over an order amount:**
   - `배송비 3,000원`, with the layer `금액별배송비: 0원 이상 ~ 200,000원 미만 3,000원 / 200,000원 이상 0원`.

A consignment reseller ships one or two units per order, so the free-over threshold almost never applies.

## Decision

### 1. A minimum price stated in the description

- **Site words.** A template reads it only from the site's phrases, held in the label slot `minimum_price_phrase` (for 건강산: `판매가격절대준수`, `판매가격 준수`). A template declares no phrase of its own.
- **Where.** Only in the description block the page closed. Each paragraph or list item is one sentence unit. Nothing is read from an image.
- **What a sentence states.** In a unit that contains a phrase, the minimum is the first amount written as `N원 이상`, optionally preceded by `1개`, that follows the phrase. An amount for a bundle (`2개 묶음 …`) or one including shipping (`배송비 포함 …`) is not a per-unit minimum and is never read as one.
- **The reading** fills `minimum_sale_price` (`MoneyValue`, label = the phrase), with the sentence quoted as evidence:
  - one per-unit amount across every unit that names a phrase → `CONFIRMED`;
  - two different per-unit amounts, or a phrase with no readable amount → `REVIEW_REQUIRED`, with the words kept;
  - no phrase anywhere → the labelled-row reading applies as before. If a labelled minimum row also exists and its amount differs, the field is `REVIEW_REQUIRED`.
- Nothing is guessed. A sentence the rule cannot read never yields an amount.

### 2. A free-over shipping policy is priced at its base fee

- **The fact stays truthful.** A page that states a base fee and a free-over threshold records `ShippingValue(kind=CONDITIONAL, fee_krw=<base fee>, free_over_krw=<threshold>, policy_text=<the page's words>)`. Its evidence quotes both.
- **The pricing owners read it as the base fee.** `source_inputs` (and every owner that prices the supplier shipping) prices a `CONDITIONAL` fact that carries both `fee_krw` and `free_over_krw` at `fee_krw`. This is the owner's rule. The policy words stay visible beside the price.
- **Any other conditional shape stays `PRICING_SHIPPING_CONDITIONAL`, as today:** a condition without a base fee, a quantity tier or a region surcharge.
- A product whose single unit already costs more than the threshold is still priced at the base fee. This overstates the cost and never understates it, which the owner accepted.

### 3. Slice

| # | slice |
| --- | --- |
| G0 | this ADR |
| G1 | the `godomall` template (ADR-0030 §4, S4), applying §1 and its own free-over reading; the pricing rule of §2; 건강산's site configuration in `RECON` and its offline proof on the reconnaissance captures |

G1 is provider-zero.

## Invariants

- **DM-01** A description minimum is read only from site phrases, only from a closed description, and only as a per-unit `N원 이상` amount.
- **DM-02** Bundle amounts and shipping-inclusive amounts are never a per-unit minimum.
- **DM-03** Two different per-unit amounts, or a phrase without one, are `REVIEW_REQUIRED`.
- **FO-01** A free-over policy is recorded as `CONDITIONAL`, with its base fee and threshold.
- **FO-02** Pricing reads a `CONDITIONAL` fact with both a base fee and a free-over threshold at the base fee; any other conditional stays refused.

## Consequences

- 건강산 products carry their minimum resale price, and pricing can respect it.
- 건강산 products can be priced without operator intervention, at the base fee.

## References

- Issue #219 `6086058056`, `6086199255`, `6088081139`
- ADR-0010 §7; ADR-0030; ADR-0032
