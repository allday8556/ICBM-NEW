<!-- Moved verbatim from CLAUDE.md §6 (Immutable domain rules) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §6` references resolve here (PATH_MIGRATION_MAP). -->

## 6. Immutable domain rules

### 6.1 Pricing

```text
if minimum_sale_price exists:
    final_sale_price = minimum_sale_price
    price_basis = MINIMUM_SALE_PRICE
else:
    final_sale_price = target_margin_price
    price_basis = TARGET_MARGIN
```

Never reintroduce `max(target_margin_price, minimum_sale_price)`.

Preserve supplier quantity tiers as original `(quantity, total_price)` facts. Do not infer or flatten source totals.

### 6.2 Options / SKU

Preserve atomic source SKU identity. Same weight does not justify flattening different count, grade, or option identity.

### 6.3 Stock

```text
BUY/CART active                     → ON_SALE
SOLD OUT + no active purchase path → SOLD_OUT
mixed/insufficient evidence        → REVIEW_REQUIRED
```

### 6.4 ProductFacts revisions

Collection writes append-oriented/revisioned `ProductFactsRevision` records. Never silently mutate historical source facts in place.

`Product` resolves each member source product through that member's current source revision; this pointer is not an acceptance or confirmation status.

### 6.5 Marketplace CREATE

Real CREATE uses `RegistrationAttempt`.

If a CREATE result is unknown:

- do not blindly retry CREATE
- reconcile only with evidence ADR-0014 §10 admits: a provider read-back or a provider lookup under an adopted contract, transmission-precluded evidence, or another explicitly reviewed machine or provider proof
- only retry after proving no marketplace product was created

The deterministic seller product code is ICBM's own correlation identity for a provider-listing unit (ADR-0014 §7). It is not a provider uniqueness guarantee, so it does not by itself make reconciliation deterministic:

- a lookup that returns nothing is not proof that no product was created
- an operator assertion is never the evidence
- no lookup contract that could prove remote absence is adopted (ADR-0014 §17.2), so a possibly transmitted CREATE that no admissible evidence resolves stays `UNKNOWN` with a `REVIEW_REQUIRED` overlay and its conflict scope stays closed

The current provider-evidence verdict is recorded in `documents/acceptance/milestones/M5.md` §9 as
milestone status, not as a second immutable rule. New reviewed provider evidence or an accepted
contract may update that verdict. Until then, the rules above remain fail-closed: an unresolved
possibly transmitted CREATE stays `UNKNOWN`, is never blindly resent and keeps its conflict scope
closed.
