# ADR-0039 — Applying a supplier's sales policy is the user's choice, per supplier

Status: **ACCEPTED** 2026-10-11. The owner decided that applying a supplier's sales policy is a per-supplier setting:
- the channel restrictions (ADR-0031);
- the minimum resale price (ADR-0030 §2, ADR-0034, ADR-0035).

Both default to **apply**. When a policy is not applied, ICBM shows it and does not enforce it.

This ADR lands before its code.

Decision owners:
- **Product direction:** the owner, in Issue #219:
  - `6109102895`: "실사용자가 책임져야되는부분에서 우리는 구현해놓고 사용자가 결정할 수 있게만 해주면됨. 판매제한 판매이후 리스크는 판매자(사용자)가 책임져야되는부분";
  - `6109161005`: "각도매처별 판매정책 적용 비적용 항목을 두면 될것같은데";
  - `6109184868`: the toggle covers both the channel restrictions and the minimum resale price, and the default is apply.
- **Already decided by canon:**
  - ADR-0031: the sales-channel fact and its REGISTER refusal, which this ADR amends;
  - ADR-0013 §7: the pricing owner and the minimum sale price;
  - ADR-0018 §5 amendment: compliance is the seller's;
  - ADR-0014: the frozen preparation and snapshot.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- a versioned per-supplier sales-policy setting with two switches;
- the changed enforcement of §2 and §3;
- the settings surface of §4.

What it does not authorize:
- **no change to the facts.** The facts still record what the page says: `sales_channels` and `minimum_sale_price` are read as before;
- **no change to an unread statement's honesty.** `REVIEW_REQUIRED` facts stay `REVIEW_REQUIRED` in the product data;
- **no change to existing listings** (ADR-0031 §4.4).

Recorded by: Claude Code (Track B). Number 0039; ADR-0036 is Track A's.

Date: 2026-10-11

---

## 1. The setting

- **One revision per supplier.** For each supplier key, a versioned, append-only revision holds two switches:
  - `apply_channel_restrictions`, default `true`;
  - `apply_minimum_price`, default `true`.
- **Who changes it.** Changing a switch is an audited owner action through the settings save contract. The revision records who changed it and when. A supplier with no revision uses the defaults.
- **Pins.** A registration preparation pins the revision it was prepared under. A pricing snapshot pins whether the minimum was applied. A later change makes the preparation stale, and it is re-prepared, exactly as a target-policy change does.

## 2. Channel restrictions (amends ADR-0031 §4.1)

| the fact says, for the target | `apply_channel_restrictions = true` | `apply_channel_restrictions = false` |
| --- | --- | --- |
| forbidden (`CLOSED_MALL_ONLY`, or `LISTED` with the target) | `SOURCE_CHANNEL_FORBIDDEN`, blocking (unchanged) | `SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED`, a non-blocking warning |
| unresolved (`REVIEW_REQUIRED`) | `SOURCE_CHANNEL_UNRESOLVED`, blocking (unchanged) | `SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED`, a non-blocking warning |
| allowed or absent | none | none |

- **The warnings say the policy.** They quote the supplier's words and say the user chose not to apply that supplier's sales policy. Their Korean copy names the supplier.
- **ADR-0031 §4.1's line "`SOURCE_CHANNEL_FORBIDDEN` cannot be overridden"** now reads: it cannot be overridden per product. The user may decide per supplier not to apply the supplier's channel policy.
- **Coupang (Track C)** applies the same switch.

## 3. Minimum resale price

- **`apply_minimum_price = true`** (the default): pricing uses the source's minimum resale price as today.
- **`apply_minimum_price = false`:**
  - pricing does not use it: `SourceInputs.minimum_sale_price_krw` is `None` for pricing;
  - the minimum still shows beside the price, with the note that it is not applied;
  - the pricing snapshot records `minimum_applied = false` and the minimum the source stated.
- **An unresolved minimum.** A `REVIEW_REQUIRED` minimum blocks pricing only while the policy is applied. When it is not applied, it is a warning.

## 4. Settings surface

- **Where.** Each supplier card in 수집관리 → 공급처 관리 gets a 「판매정책」 section with two switches:
  - "판매채널 제한 적용";
  - "최저판매가 적용".
- **What it shows.** The section shows the current values, who changed them and when, and one line on what turning a switch off means: the user carries the risk.
- **Saving.** It saves through the settings save contract with optimistic concurrency, as the target policy does.

## Invariants

- **SP-01** The facts are unchanged. The switches change enforcement only.
- **SP-02** The default is to apply both policies.
- **SP-03** A preparation and a pricing snapshot pin the policy revision they used.
- **SP-04** A policy that is not applied is always shown, never hidden.

## Slices

| # | slice |
| --- | --- |
| P0 | this ADR |
| P1 | the policy owner (one migration, numbered 0061 or later) with the settings save contract; enforcement in the REGISTER preflight and in pricing; the pins; the supplier card UI |

## References

- ADR-0013 §7
- ADR-0014
- ADR-0018 §5
- ADR-0030 §2
- ADR-0031 §4
- ADR-0034
- ADR-0035
- Issue #219 `6109102895`, `6109161005`, `6109184868`
