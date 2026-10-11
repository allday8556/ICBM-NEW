# ADR-0038 — Overseas 구매대행 suppliers: source currency, FX snapshot, landed cost and SmartStore cross-border fields

Status: **PROPOSED** 2026-10-11. Its owner questions are answered (§8). It becomes ACCEPTED once reconnaissance of 1688 and Super Delivery confirms §1–§2 (scheduled 2026-10-12).

This is the currency and FX-snapshot ADR that ARCHITECTURE §6 and ROADMAP §14.3 require before any second-currency supplier.

Decision owners:
- **Product direction:** the owner, in Issue #219:
  - `6103784916`: the next suppliers are 1688 and Super Delivery;
  - `6109084429`: 구매대행 only; FX at 송금 보낼때 (TTS) with a user margin set in settings; user-entered shipping with an optional default; duties and taxes included above USD 150 by default and switchable; regulated goods are the user's judgement; options for multi-quantity; image translation is the goal;
  - ADR-0018 §5 amendment: compliance is the seller's.
- **Already decided by canon:**
  - ARCHITECTURE §6: pricing snapshots support "source currency + FX snapshot when needed";
  - ADR-0010: source facts are quoted, never guessed;
  - ADR-0013 §7: pricing owner, explicit pricing context;
  - ADR-0014 and ADR-0018: frozen snapshot and grants.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes, once ACCEPTED:
- a source currency other than KRW on a product facts revision, CNY and JPY first;
- an FX snapshot owner;
- the landed-cost inputs and settings of §4–§5;
- the SmartStore cross-border projection of §6.

What it does not authorize:
- 사입 (imported stock): later, separately;
- image translation: Image Studio, Issue #56. It is Track A's, after ADR-0036 (owner decision on #219), and has its own later ADR;
- the 1688 and Super Delivery collectors themselves: a later ADR after reconnaissance (ADR-0030 §9: neither fits a template);
- any LIVE write.

Recorded by: Claude Code (Track B). Number 0038; ADR-0036 is Track A's.

Date: 2026-10-11

---

## 1. Source truth stays in the source currency

- A product facts revision records the supplier's prices in the supplier's own currency (`currency` = `CNY` or `JPY`), quoted as the page states them. **Nothing is converted in the facts.**
- Money values carry their currency. KRW facts keep their current shape and meaning: `amount_krw` stays for KRW. A non-KRW value is recorded as an exact decimal amount in that currency's unit (CNY to 2 places; JPY whole yen), never as a float.
- **A revision states one currency.** A page that mixes currencies is `REVIEW_REQUIRED`.

## 2. 구매대행 is a fulfilment model on the supplier

- A site, or package, of an overseas supplier declares `fulfilment = PURCHASE_AGENCY` (구매대행): the product is bought abroad per order and shipped to the Korean buyer through a freight forwarder (배대지).
- This is a declared property of the supplier, not a guess per product. 사입 is not modelled now.

## 3. FX snapshot owner

- **Source and rate.** Korea Eximbank's public 현재환율 API (`oapi.koreaexim.go.kr`, `AP01`), rate type **TTS (송금 보낼때)**: the owner's choice. Codes `CNH` (위안화) and `JPY(100)`, per 100 yen.
- **Snapshot.** An immutable row per business day and currency: the date, the currency, the TTS, the publication's own fields as fetched, the fetch time, and the source. It is refreshed once a business day after 11:00 KST. On days without a publication, the last published rate is used and named.
- **"Live".** The owner asked for the rate to "change in real time". The publication is daily, so "live" means **the newest published snapshot at the time of pricing**. Each pricing snapshot pins the FX snapshot it used, so a price can always be explained.
- **Fail closed.**
  - No snapshot newer than **3 business days** → `PRICING_FX_STALE`.
  - No snapshot at all → `PRICING_FX_UNAVAILABLE`.
  - Both are `REVIEW_REQUIRED`, and nothing is priced on a guessed rate.

## 4. Landed cost of a 구매대행 unit

```text
supplier_cost_krw = ceil(source_price × TTS_per_unit × (1 + fx_margin%))
landed_cost_krw   = supplier_cost_krw
                  + overseas_shipping_krw            (user-entered per product, or the default)
                  + duties_and_taxes_krw             (only when included; see below)
```

- **`fx_margin%`** is a user setting (§5). The pricing snapshot pins the value used.
- **`overseas_shipping_krw`** covers the supplier's local delivery, the 배대지 and international freight.
  - It is entered by the user per product. When none is entered, the settings default applies.
  - When neither exists → `PRICING_OVERSEAS_SHIPPING_UNSET` (`REVIEW_REQUIRED`).
- **Duties and taxes.**
  - The USD 150 test uses the declared value in USD: the supplier price plus local delivery, at the USD TTS from the same FX snapshot day.
  - At or under USD 150: duty-free list clearance assumed, so duties are 0.
  - The USD 150 threshold is not a user setting. ICBM maintains it as a versioned policy constant, updated by patches (owner `6109161005`).
  - Above USD 150 and **included** (the default, owner `6109084429`): `duties_and_taxes_krw = ceil(customs_value_krw × duty_rate%) + ceil((customs_value_krw + duty) × 10%)` (부가세). `duty_rate%` is a user setting with a default (§8).
  - Above USD 150 and **excluded** by the user's choice: duties are 0 in the cost, and the listing says the buyer pays them (§6).
- **The result** is the purchase cost the existing pricing owner (ADR-0013 §7) uses unchanged: margin, fees, minimum and rounding as today. The pricing snapshot additionally pins:
  - the FX snapshot;
  - the margin;
  - the shipping source (per product or default);
  - the duty decision and rate.

## 5. Settings (user-editable)

In the settings tab, under a new "해외 구매대행" card:

| setting | default |
| --- | --- |
| `fx_margin_percent` | **5** (owner `6109161005`) |
| `overseas_shipping_default_krw` | unset |
| `duties_included_over_usd150` | on |
| `duty_rate_percent` | 8, with a per-product override (phase 1; §8) |
| `smartstore_overseas_shipping_address_id` | the SmartStore 주소록 entry of the overseas 출고지, unset |
| `overseas_delivery_days` | 7–14 |
| `importer_label` | set by the user (owner `6109161005`) |

- Each setting is versioned like the existing target policy. A pricing snapshot pins the version it used.

## 6. SmartStore cross-border projection (Commerce API v2 product schema)

- **출고지.** `claimDeliveryInfo.shippingAddressId` = the overseas 출고지. The store must hold the 해외상품판매 permission. A missing address id is `REVIEW_REQUIRED`.
- **Duties field.** `detailAttribute.customsTaxType`: `INCLUDED` when duties are included or the value is at or under USD 150; `EXCLUDED` when the user chose to exclude them.
- **Customs clearance.** `deliveryInfo.businessCustomsClearanceSaleYn = false` for 구매대행.
- **Origin.**
  - `originAreaInfo.originAreaCode` is the import code (`02` group) with the supplier's country.
  - `importer` is required for imports. Its value is an owner question (§8).
- **KC.** `kcCertifiedProductExclusionYn = KC_EXEMPTION_OBJECT` with `kcExemptionType = OVERSEAS` (구매대행).
  - Whether a product category may be sold by 구매대행 is the user's judgement and responsibility (owner `6109084429`, ADR-0018 §5). ICBM does not decide it.
- **Orders.** The buyer's 개인통관고유부호 arrives on the order (`individualCustomUniqueCode`). The dispatch flow (ADR-0025) carries it to the forwarder step. Storing it follows the PII rules of the order owner.
- Each field has its own capability-mapping entry and contract test before LIVE.

## 7. Names and options in another language

- A collected name or option label in Chinese or Japanese is source truth, kept as stated.
- The listing's Korean name comes from the AI name stage (ADR-0026 and later), with the source text as input. Option labels get the same treatment in that stage.
- Image text translation is out of scope. It belongs to Track A's later Image Studio ADR (Issue #56), which consumes this ADR's foundation: source currency, overseas supplier, and the language of a source's text.

## 8. Owner answers and the open question

Answered (`6109161005`):
- the FX margin default is 5%, and the user can change it;
- the importer label is a user setting;
- delivery days have a default the user can change;
- the USD 150 threshold is patched by ICBM.

Per-item duty, confirmed and phased:
1. a default rate in settings, plus a per-product override (this ADR);
2. a duty table by SmartStore category, maintained and patched by ICBM;
3. optionally, an AI HS-code suggestion that the user confirms.

Phase 1 is in this ADR. Phase 2 follows. Phase 3 is optional.

## Invariants

- **FX-01** Source facts keep the source currency. Nothing is converted in a revision.
- **FX-02** Every price of an overseas unit pins one FX snapshot, the margin and the settings version.
- **FX-03** No price is made on a stale or missing rate.
- **FX-04** Duties are included above USD 150 unless the user excluded them, and the listing's `customsTaxType` says the same.
- **FX-05** ICBM never decides a category's eligibility for 구매대행. It projects the user's choice. Under the owner's principle (#219 `6109102895`), where the risk is the seller's, ICBM implements and the user decides.

## Slices

| # | slice |
| --- | --- |
| O0 | this ADR (after §8 and reconnaissance) |
| O1 | currency on facts and money values; FX snapshot owner and fetcher; settings card |
| O2 | landed-cost inputs in pricing, with the pins |
| O3 | SmartStore cross-border projection |
| O4+ | the 1688 and Super Delivery collectors (their own ADR after reconnaissance) |

Migrations are numbered 0062 or later.
