# ADR-0031 — Supplier sales-channel restrictions: collected as a source fact, enforced at REGISTER

Status: **ACCEPTED** 2026-10-10. A supplier may forbid reselling a product on some marketplaces. ICBM collects that statement as a source fact and refuses to register the product on a marketplace the supplier forbids.

This ADR lands before its code. Each implementation slice of §7 cites it as canon.

Decision owners:
- **Product direction:** the owner, in Issue #219 comment `6086299406`, after the M8 reconnaissance of U-PICK and 건강산. The owner chose "수집+등록 차단": collect the restriction, and block registration on a forbidden marketplace.
- **Already decided by canon:**
  - ROADMAP §9 "Expansion gate": a contract change for a supplier needs architecture review first. This ADR is that review.
  - ADR-0010 §6–§8: the `ProductFactsRevision` contract, the two levels of facts and the evidence model.
  - ADR-0014: the registration preflight and its reasons.
  - ADR-0030: platform templates and site configurations.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- one new COVERAGE fact field, `sales_channels`, with its value type (§2);
- the extraction rules of §3 in every supplier parser, KM통상's included;
- one REGISTER preflight refusal (§4);
- the display of the restriction on the product and registration screens (§5);
- the provider-zero slices of §7.

What it does not authorize:
- **No real read and no LIVE.** Acceptance reads stay under ADR-0030 §8.
- **No change to a listing that already exists.** The adopted KM listings and earlier registrations are not re-judged or withdrawn (§4.4).
- **No AI or OCR.** A restriction stated only inside an image is not read.
- **No Coupang REGISTER code.** Coupang is Track C's. The fact names Coupang so that Track C can apply the same refusal (§4.3).

Sources: the reconnaissance notes of 2026-10-10 (Issue #219 `6086058056`); ADR-0010 §6–§8; ADR-0014; ADR-0030.

Recorded by: Claude Code (Track B). The number was confirmed free on main and on every open branch.

Date: 2026-10-10

---

## Context

The reconnaissance of 2026-10-10 found sales-channel restrictions on both new suppliers.
- **U-PICK** (Cafe24) shows a labelled row, `판매가능플랫폼`, on every product. Its value is one of:
  - `모든마켓 판매가능`;
  - `폐쇄몰`;
  - `폐쇄몰 전용/오픈마켓 판매불가`.

  A row labelled `판매정책` repeats the rule in prose, for example "모든 오픈마켓 및 소셜커머스에 판매 불가".
- **건강산** (Godomall) writes the restriction in the description text, for example `★ 쿠팡판매 불가 상품 ★`.

Registering a forbidden product breaks the supplier's terms. The supplier can then stop supplying the reseller. The canonical facts have no field for this today, so the statement is invisible after collection.

## Decision

### 1. A source fact, not an operator note

The restriction is what the supplier's page states about the product. It therefore belongs in the `ProductFactsRevision`, with its evidence, like a price or a sold-out state. It is not an operator setting. A new collection that states something else produces a new revision, and the newest revision decides.

### 2. The field

`sales_channels` is a **COVERAGE** field. Its value type is `SalesChannelsValue`:

| member | meaning |
| --- | --- |
| `scope` | `ALL_ALLOWED`: the page states that every marketplace is allowed. `CLOSED_MALL_ONLY`: the page restricts the product to closed malls or offline sale, so every open marketplace is forbidden. `LISTED`: the page forbids the marketplaces in `forbidden` and says nothing about the rest. |
| `forbidden` | the marketplace keys the page forbids by name (`smartstore`, `coupang`). Empty unless `scope` is `LISTED`. |
| `policy_text` | the page's own words, quoted (at most 200 characters) |

Statuses follow the existing rules:
- **`ABSENT`**: the page states no restriction. Registration is not blocked.
- **`CONFIRMED`**: the page's statement matched exactly one reading under §3.
- **`REVIEW_REQUIRED`**: the page states something about sales channels that §3 cannot read, or two statements contradict each other. The words are kept as evidence.

Every marketplace ICBM registers to is an **open marketplace**, SmartStore and Coupang included. `CLOSED_MALL_ONLY` therefore forbids every one of them, including any marketplace added later.

### 3. Extraction

Reading a restriction is site knowledge with a fixed shape. A parser never infers one.
- **The words are site words.** A site configuration lists them in label slots (ADR-0030 §3):

  | slot | meaning | example |
  | --- | --- | --- |
  | `sales_channel_row` | the label of a row that states the channels | `판매가능플랫폼` |
  | `channel_all_allowed` | a phrase meaning every marketplace is allowed | `모든마켓 판매가능` |
  | `channel_closed_only` | a phrase meaning closed malls only | `폐쇄몰`, `오픈마켓 판매불가` |
  | `channel_forbid_coupang` | a phrase forbidding Coupang | `쿠팡판매 불가` |
  | `channel_forbid_smartstore` | a phrase forbidding SmartStore | `스마트스토어 판매불가` |
- **Where the words are read:** a labelled row whose label is in `sales_channel_row`, and the description text. Words that appear only inside an image are not read.

  > **Amendment note (ADR-0035 §1).** The product name is also read, for the restricting
  > phrases only. It is never read for `channel_all_allowed`. A name restriction combines
  > exactly as a description restriction does. A single row that states an allowed phrase and
  > restrictions reads as its restrictions (`LISTED`); the `all_allowed` + restriction →
  > `REVIEW_REQUIRED` line below applies to separate statements (ADR-0035 §1, NR-03).

- **Matching:** a phrase matches when the page's text contains it, with whitespace ignored. The readings combine as follows:
  - only `channel_all_allowed` phrases match → `ALL_ALLOWED`;
  - any `channel_closed_only` phrase matches → `CLOSED_MALL_ONLY`. It is the strictest reading and wins over a `forbid` phrase;
  - only `forbid` phrases match → `LISTED`, with those marketplaces;
  - an `all_allowed` phrase together with any restricting phrase → `REVIEW_REQUIRED`;
  - a `sales_channel_row` row whose value matches no phrase → `REVIEW_REQUIRED`;
  - nothing matches and no such row exists → `ABSENT`.
- **Templates and KM통상.**
  - The `cafe24` and `godomall` templates implement this rule with empty default phrase lists. A site that has never configured its phrases still reads a `sales_channel_row` row: a row it cannot read stays `REVIEW_REQUIRED`, never silently allowed.
  - KM통상's parser reports the field `ABSENT`, because its reconnaissance found no such statement. This advances KM통상's extraction revision (`kmretail-4`). It changes nothing else KM통상 reads.

### 4. Enforcement at REGISTER

#### 4.1 The refusal

The registration preflight reads `sales_channels` from the current source revision of each Item's binding member: the same current revision the M4 readiness owners read for that member.

| the fact says | the target is SmartStore | preflight reason |
| --- | --- | --- |
| `ABSENT`, or `ALL_ALLOWED` | — | none |
| `CLOSED_MALL_ONLY` | forbidden | `SOURCE_CHANNEL_FORBIDDEN` (blocking) |
| `LISTED`, with `smartstore` in `forbidden` | forbidden | `SOURCE_CHANNEL_FORBIDDEN` (blocking) |
| `LISTED`, without `smartstore` | allowed | none |
| `REVIEW_REQUIRED` | unknown | `SOURCE_CHANNEL_UNRESOLVED` (blocking) |

- `SOURCE_CHANNEL_FORBIDDEN` cannot be overridden. The operator can only drop the product or ask the supplier.
- `SOURCE_CHANNEL_UNRESOLVED` clears only when a newer source revision reads the statement, after the site's words are corrected or the page changes. No operator click turns an unread statement into an allowed one.

#### 4.2 Where it is shown

The product screen shows the restriction beside the source facts. The registration editor shows the refusal reason in Korean:
- `공급사가 이 마켓 판매를 금지한 상품입니다`;
- `공급사 판매채널 문구를 확인할 수 없습니다`.

#### 4.3 Coupang (Track C)

The same fact names `coupang`. Track C's Coupang REGISTER applies the same table with Coupang as the target. This ADR does not write that code.

#### 4.4 Existing listings

A listing already registered or adopted is not re-judged by this ADR. A later decision may add an OPERATE warning.

### 5. Display

The value is shown as the supplier's words plus one Korean reading:
- `모든 마켓 판매 가능`;
- `폐쇄몰 전용 (오픈마켓 판매 불가)`;
- `쿠팡 판매 불가` (for `LISTED`: one line per forbidden marketplace);
- `확인 필요`.

An `ABSENT` field reads `판매채널 제한 없음`.

### 6. Contract changes, stated

- `app/stages/collect/facts.py`: a new `FIELD_REGISTRY` entry, `sales_channels` (COVERAGE, `SalesChannelsValue`). Every parser must report it, as every field already must. No new `FieldStatus` or `EvidenceKind` is added.
- No database migration. Fields are stored by key with their value as JSON.
- One new preflight reason pair in REGISTER, and its Korean text in the screens.
- Revisions stored before this ADR carry no `sales_channels` field. Reading one, the preflight treats it as `ABSENT`, because the field did not exist when the revision was recorded. A re-collection records the field.

### 7. Slices

| # | slice |
| --- | --- |
| C0 | this ADR |
| C1 | the field and value type; KM통상 `ABSENT` (`kmretail-4`); the template rule and slots in `cafe24` (and in `godomall` when it lands, ADR-0030 S4) |
| C2 | the REGISTER preflight refusal, and the product and registration screen display |

C1 and C2 are provider-zero.

## Invariants

- **SC-01** A restriction is a source fact with evidence, never an operator setting.
- **SC-02** A statement no rule can read is `REVIEW_REQUIRED`, never allowed.
- **SC-03** `CLOSED_MALL_ONLY` forbids every marketplace ICBM registers to.
- **SC-04** A forbidden or unresolved restriction blocks the registration preflight. Nothing overrides a forbidden one.
- **SC-05** Restriction words are site words in the site configuration, never a pattern or code.
- **SC-06** No restriction is read from an image.
- **SC-07** Existing listings are not changed by this ADR.

## Consequences

- A U-PICK product marked `폐쇄몰` can no longer reach SmartStore by mistake.
- A site whose phrases are not configured still blocks a product with an unreadable channel row. Configuring the phrases is part of that site's reconnaissance.
- KM통상's extraction revision advances once (`kmretail-4`) for the new field.

## References

- Issue #219 `6086058056` (reconnaissance approval), `6086199255` (건강산 requirements), `6086299406` (this direction)
- ROADMAP §9; ADR-0010; ADR-0014; ADR-0030
