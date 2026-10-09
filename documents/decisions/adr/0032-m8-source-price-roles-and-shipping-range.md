# ADR-0032 — Source price roles and shipping-fee ranges

Status: **ACCEPTED** 2026-10-10. A supplier page that states several prices now says which one is the purchase price, through its site's words. A shipping fee stated as a range is read as its highest amount.

This ADR lands before its code. Each implementation slice of §5 cites it as canon.

Decision owners:
- **Product direction:** the owner, in Issue #219 comment `6086421199`, after the M8 reconnaissance. The owner wrote "배송비 범위로 나오면 높은 가격으로 책정, 도매가는 회원가 소비자가는 원가, 최저가는 최저가":
  1. a shipping-fee range is priced at its highest amount;
  2. the wholesale purchase price is the page's `회원가`;
  3. `소비자가` is the original (list) price;
  4. the minimum price is the page's own minimum-price statement.
- **Already decided by canon:**
  - ADR-0010 §7: prices keep their exact source labels; a conditional shipping policy is never flattened into a fixed fee by guessing.
  - The pricing owner (`app/stages/products/pricing.py`) refuses several prices as `PRICING_PURCHASE_PRICE_AMBIGUOUS`, "never chosen by label, name or order". This ADR replaces that rule for prices that carry a declared role (§2).
  - ADR-0030: site words live in label slots of a site configuration.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- an optional `role` on `SourcePrice` (§2);
- the purchase-price selection by role in the pricing owners (§3);
- the shipping-range reading (§4);
- the provider-zero slices of §5.

What it does not authorize:
- no migration (fact values are stored as JSON);
- no change to a stored revision; a revision recorded before this ADR keeps its prices without roles and keeps today's reading;
- no real read and no LIVE.

Sources: Issue #219 `6086421199`; the reconnaissance notes of 2026-10-10; ADR-0010 §7; ADR-0030 §3.

Recorded by: Claude Code (Track B). The number was confirmed free on main and on every open branch.

Date: 2026-10-10

---

## Context

The reconnaissance of 2026-10-10 found pages that state more than one price.
- **U-PICK** shows `소비자가` (16,900 or 118,000원) and `회원가` (the member's wholesale price, 5,000 or 24,000원).
- **건강산** shows `정가` (crossed out) and `판매가`, the member's price.

Today such a product stops in pricing with `PRICING_PURCHASE_PRICE_AMBIGUOUS`, because no contract says which price is the purchase cost. The owner has now said it.

U-PICK also states some shipping fees as a range. In `3,000원 ~ 4,000원` the fee depends on the quantity ordered. Today's parser reads the first amount, which understates the cost.

## Decision

### 1. The minimum price

The page's own minimum-price statement is `minimum_sale_price`, as it already is. The template label slot `minimum_price` holds the words. U-PICK adds `폐쇄몰 최저판매가` to `최저판매가`. 건강산's description sentence is read by its template (ADR-0030 S4, Issue #219 `6086199255`).

### 2. Price roles

`SourcePrice` gains an optional `role`:

| role | meaning |
| --- | --- |
| `PURCHASE` | the price the reseller pays the supplier (도매가) |
| `LIST` | the original, consumer or list price the page shows for reference (소비자가, 정가) |
| none | a price whose role the site has not declared |

- A role comes only from the site's words. Two new template label slots hold them: `purchase_price` and `list_price`. A price whose label is in neither slot carries no role.
- A label may sit in at most one role slot. A site that puts one word in both is refused at binding (ADR-0030 §3).
- The label is still kept exactly as the page wrote it.

For the first sites:

| site | `purchase_price` | `list_price` |
| --- | --- | --- |
| U-PICK | `회원가` | `소비자가` |
| 건강산 | `판매가` | `정가` |
| KM통상 | — (one price, unchanged) | — |

### 3. Choosing the purchase price

The pricing owners (`pricing.py` `source_inputs` and the atomic-SKU economics store) choose the purchase price by these rules, in order:
1. **Any price has role `PURCHASE`:** if exactly one does, it is the purchase price. If more than one does, the result is `PRICING_PURCHASE_PRICE_AMBIGUOUS`.
2. **No price has role `PURCHASE`, and exactly one price is stated without a role:** that price is the purchase price, as today. Any `LIST` prices beside it are ignored as cost.
3. **Anything else:** `PRICING_PURCHASE_PRICE_AMBIGUOUS`, as today. This includes:
   - several prices without a role;
   - only `LIST` prices, including a page whose only stated price is `LIST`.

A `LIST` price is never the purchase price, even when it is the only price on the page. `LIST` prices are kept as facts.

The purchase price is never chosen by label text, name or order. It is chosen only by a role the site declared.

### 4. Shipping-fee ranges

When the shipping-fee cell states a range of amounts (`A원 ~ B원`), the fee is the **highest** amount. The rule:
- the value is `FIXED`, with `fee_krw` set to the highest amount;
- `policy_text` keeps the page's own words, the range included;
- the evidence quotes the cell and names the reading (normalized: the highest amount).

A cell with several amounts that is not a range stays `REVIEW_REQUIRED`, as today. This rule is the owner's, not an inference. It applies to every template and to KM통상's parser.

### 5. Slices

| # | slice |
| --- | --- |
| P0 | this ADR |
| P1 | `SourcePrice.role`; the `purchase_price` and `list_price` slots and the range rule in the `cafe24` template, and in `godomall` when it lands; KM통상's range rule, in the same `kmretail-4` revision as ADR-0031 C1; the pricing owners' selection (§3) |

P1 is provider-zero.

## Invariants

- **PR-01** A purchase price is chosen by a declared `PURCHASE` role, or by being the only price that carries no role. It is never chosen by label text, name or order.
- **PR-02** A `LIST` price is never a cost.
- **PR-03** A role comes only from site words in the site configuration.
- **PR-04** A shipping range is read as its highest amount, and its words are kept.
- **PR-05** Stored revisions are not rewritten; prices without roles keep today's reading.

## Consequences

- U-PICK and 건강산 products can be priced without operator intervention, at the member price.
- A U-PICK product with a quantity-dependent shipping fee is priced at the highest fee. This can overstate the cost for small orders, which the owner accepted.

## References

- Issue #219 `6086421199` (this direction), `6086199255` (건강산 minimum price)
- ADR-0010 §7; ADR-0030; ADR-0031
