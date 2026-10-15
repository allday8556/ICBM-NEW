# ADR-0042 — Store import, the common product across stores, and duplicate checking against the store catalog

Status: **PROPOSED** 2026-10-15. The owner's decisions are recorded (Issue #219 `6190407624`, `6190428447`, `6190978708`). It becomes **ACCEPTED** when it merges after the GPT + Claude cross-audit.

This ADR is the contract for three things:
- **S1:** bringing an operator's existing store catalog into ICBM;
- **S2:** managing one product's listings in every store from one 공통상품;
- **S3:** checking newly collected products against that catalog.

It lands before its code. Each slice of §14 cites it as canon. It **amends** ROADMAP §1.1, ADR-0013 §4, ADR-0014 §13 and §17.2, ADR-0018 §3, ADR-0023 M6-01 and ADR-0024, as §13 states. It supersedes none of them.

Decision owners:
- **Product direction:** the owner, in Issue #219.
  - `6190407624` (2026-10-15): S1 store sync, S2 managing the common product per store, and S3 duplicate checking on collect. Duplicate checking stays **ON**; the owner rejected turning it off.
  - `6190428447` (2026-10-15): both entry paths are supported, and ROADMAP §1.1 is **not** superseded.
    - **(a)** A new seller starts from an empty store: collect → product → register, as today.
    - **(b)** An existing operator moves in: S1 import → S2 linking → S3 dedupe of later collections.
    - Both converge on the same common product.
  - `6190978708` (2026-10-15): answers Q1–Q7 (§15).
  - The principle "실제 사용자 기준" (2026-10-15): each slice is accepted by a seller walkthrough on the real UI (§0, §14).
  - `6109102895`: the seller-risk principle. Where the seller bears the risk, ICBM shows the facts and the user decides. ICBM's own safety invariants stay hard.
- **Design decisions A1–A4** (§15): made by the implementation agent under ADR-0022, recorded here as decisions.
- **Already decided by canon:**
  - ADR-0013 §1, §4–§6: the canonical product **is** the `ProductGroup`; members, bindings and matching.
  - ADR-0014 §7, §13, §17.2 and §28: listing identity, the duplicate preflight, and the evidence verdict.
  - ADR-0018: the LIVE grant, the brake and the send-time stack.
  - ADR-0023: listing-state sync. ADR-0024: adopted listings.
  - ADR-0039: the seller-decides principle, as applied to supplier sales policy.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

**Context.** A usability check found every registration blocked by `DUPLICATE_EVIDENCE_MISSING`:
- the target policy has `duplicate_proof_required = true` (`app/stages/register/policy.py:218`);
- `integrations/marketplaces/smartstore/lookup.py` reports no lookup as available (`available() → False`). The adopted product search is for positive-only reconcile only and is never duplicate absence (ADR-0014 §13, §17.2, §28.2);
- so the final preflight can never be `READY` (`app/stages/register/preparation.py` `_duplicate_reasons`), and `freeze_current` freezes nothing (`app/stages/register/authoring.py`).

In parallel:
- ROADMAP §1.1 "Clean start" assumed an empty store;
- ADR-0024 linked only the 24 KM listings, by a seller-code convention;
- an operator with an existing store had no way to bring the catalog in. Duplicate checking cannot work without that catalog.

What it authorizes:
- this contract;
- the slices of §14, each its own PR;
- the endpoint adoption amendments those slices name, each recorded from official evidence under `ENDPOINT_MATRIX.md` §16;
- the amendments of §13, each effective when the slice it names lands.

What it does not authorize:
- **No marketplace mutation before S2c.** S1, S2a, S2b and S3 are read-only towards every marketplace.
- **No mutation outside ADR-0018.** S2c adds an UPDATE stage and a UI-issued batch approval (§11). Every listing update still needs, per listing:
  - LIVE;
  - a released brake;
  - its own exact grant;
  - an adopted endpoint;
  - retention;
  - an attempt owner.
- **No full-body overwrite.** An update sends only allowlisted fields through a partial endpoint (§11, A2).
- **No automatic merge on a weak signal.** Name, image or category similarity never links, attaches or merges anything without the user (§7).
- **No silent overwrite.** A seller-center edit is shown, and the user chooses (§10).
- **No remote-absence evidence for an `UNKNOWN` CREATE.** A catalog snapshot never resolves, releases or shortens an ADR-0014 §10/§28 ambiguity.
- **No buyer data.** The only contact data stored is the seller's own customer-service contact (§5.3).
- **No second product root.** A store listing is never a product (§3).
- **No DELETE beyond ADR-0018 §3.5.** An imported listing is never deleted by ICBM.
- **No Coupang call.** Coupang joins the same frame later, through Track C's adoptions (§14, S4).

Recorded by: Claude Code (Track A). The number 0042 was coordinated with the other tracks. Migrations start at `0067`; main's head is `0066_landed_cost`.

Date: 2026-10-15

---

## 0. 사용자 관점 / Seller walkthrough

This is what a seller does and sees, screen by screen. The screens are the existing ones: 설정, 수집관리, 통합DB, 등록관리, 주문관리 and 품절. New elements are marked **new**. Korean strings are the intended copy; the slices may refine the wording, not the meaning.

These two walkthroughs are the acceptance script of every slice (§14). Each slice runs the steps it delivers on the real UI, with the real store, before it is accepted.

### 0.1 Path (a) — a new seller starting from an empty store

| # | screen | the seller does | the seller sees |
| --- | --- | --- | --- |
| a1 | 설정 → 스마트스토어 연결 | enters the store's API credentials (existing M2 flow) | 「연결됨」. **new:** the first store sync starts by itself: 「스토어 상품 확인 중…」 |
| a2 | 통합DB → **new** 「스토어 상품」 tab | nothing | 「스토어 상품 0개 · 마지막 동기화 14:02」. An empty store is a valid, complete result |
| a3 | 수집관리 | collects a product (extension or URL, as today) | the product appears in 통합DB with its supplier and supplier price. **new:** if it matches a product already in ICBM by seller code or barcode, a notice reads 「기존 상품 ‘…’에 도매처로 연결됨 · 되돌리기」 instead of a second product |
| a4 | 통합DB → 등록 초안 → 등록관리 editor | prepares the listing (as today) | **new**, in the duplicate row: 「중복 없음 · 스토어 동기화 14:02 기준」. If the sync is older than 60 minutes: 「스토어 다시 확인 중…」, then the new result. A match shows 「이미 스토어에 있는 상품: …」 with 「같은 상품으로 연결」 / 「의도적으로 한 번 더 등록」 (the existing `DuplicateOverride`) |
| a5 | 등록관리 | registers (the existing LIVE flow, unchanged) | the result as today. On the next sync the listing appears in 「스토어 상품」 marked 「ICBM 등록」 |
| a6 | 통합DB → product → **new** 「스토어별 편집」 | changes the price, stock or sale status | **new:** 「미리보기」 per store, field by field (현재 → 변경). Then 「한 번에 반영」 (one approval, up to 50 listings), then the per-store result: 반영됨 / 재확인 필요 / 실패 |

### 0.2 Path (b) — an existing operator moving in

| # | screen | the seller does | the seller sees |
| --- | --- | --- | --- |
| b1 | 설정 → 스마트스토어 연결 | connects the existing store | 「연결됨」, then **new** 「스토어 상품 가져오는 중 340 / 1,200」 |
| b2 | 통합DB → 「스토어 상품」 | filters by status (판매중, 품절, 판매중지 …) | every listing with its price, discount, stock, options, shipping, category, seller code and status, plus 「마지막 동기화 14:02」 and 「지금 동기화」. A listing that disappeared shows 「스토어에서 확인 안 됨」 until provider evidence ends it |
| b3 | 통합DB (product list) | nothing | every imported listing is a common product with the badge **「도매처 미연결」**. A notice reads 「12개 상품이 판매자코드·바코드로 자동 연결됨 · 확인 / 되돌리기」. Name-similar pairs wait in 「확인 필요」 with 「같은 상품」 / 「다른 상품」 |
| b4 | product → **new** 「기존 도매처 입력」 | pastes the supplier's product URL | COLLECT runs. The supplier is attached, and the badge becomes the supplier's name. With several suppliers the seller sees 「U-PICK ₩a · 건강산 ₩b · KM통상 ₩c」 and picks 「매입처」 |
| b5 | 수집관리, later | collects new products as usual | a product already in ICBM is attached as a supplier, never duplicated: 「기존 상품 ‘…’에 도매처로 연결됨 · 되돌리기」. A name-only match asks 「같은 상품인가요?」 |
| b6 | 통합DB → product | edits in the seller center, then opens ICBM | **new** 「스마트스토어에서 직접 수정됨: 판매가 12,900 → 11,900 (10/15 15:20)」, with 「스토어 값 유지」 / 「ICBM 값으로 반영」. Nothing is overwritten until the seller chooses |
| b7 | 「스토어별 편집」 | changes price, stock or status for one or many products (KM's 24 included) | the preview, 「한 번에 반영」, and the per-store result, as in a6 |
| b8 | 주문관리 / 품절 | as today | orders of imported listings resolve to the common product. Sold-out checks run for listings that have a supplier |

## 1. Two entry paths, one common product

```text
(a) new seller     COLLECT → Product (ProductGroup) → REGISTER ───────────┐
                                                                         ├─► common product (ProductGroup)
(b) moving in      S1 store import → S2a group per listing ─► S3 attach ─┘     ├─ supplier members (S3, 기존 도매처 입력)
                                                                               ├─ store listings (ICBM-registered, adopted, imported)
                                                                               └─ content revision + per-store overrides (S2b)
```

- **Both paths end in the `ProductGroup`** of ADR-0013 §1. After that, registration, edits, stock and orders are one flow.
- **Path (a) is unchanged.** Its duplicate check uses S1 too: an empty store's complete sync is the evidence (§6).
- **Path (b) adds the import.**
- **The paths can mix.** An operator can import a store and keep registering new products. A seller who started empty can later link listings made by hand.

## 2. Terms

| term | meaning |
| --- | --- |
| **store** | one marketplace account: `marketplace_key + marketplace_account_id` (GLOSSARY §1) |
| **store listing** | one provider listing in one store, as the catalog sync observes it. For SmartStore: one origin product (`originProductNo`) with its `STOREFARM` channel product. A provider-side object, never a product |
| **catalog sync** / **catalog snapshot** | one paginated read of a store's whole catalog (§5), and the observations of one `COMPLETE` sync |
| **listing link** | the durable statement that a store listing is a listing of one `ProductGroup` (§7) |
| **common product (공통상품)** | the UI name of the `ProductGroup`, the same identifier as `icbm_product_id`. **No new identity** |
| **supplier member** | a `GroupMember` whose `SourceProduct` a supplier sells (ADR-0013 §4) |
| **content revision / store override / projection** | §9 |
| **change plan / batch approval** | §11 |

## 3. Identity: no second product root

- **The common product is the `ProductGroup`.** S1 creates no product. S2a creates one `ProductGroup` per imported listing that no strong key links to an existing group (§7), through the existing `ProductStore.create_group`.
- **A group may have zero supplier members.** The schema allows it: `group_membership_revisions.member_count >= 0` (migration `0012`), and `create_group` takes no member. Such a group shows **「도매처 미연결」**.
  - Every readiness that needs source facts stays not ready: pricing from cost, stock recheck, and a new registration from facts.
  - The listing can still be operated and edited with direct values (§9).
  - **Evidence-capture task (S2a):** how the 통합DB list (G1-C) and readiness derivation render a zero-member `ACTIVE` group. Pinned by tests.
- **A store listing is not a registration.**
  - An ICBM `MarketplaceRegistration` stays REGISTER's (ADR-0014).
  - An adoption stays ADR-0024's.
  - An imported listing is a third kind, owned by StoreCatalog (§12).
  - All three reach the common product through a listing link. A registration keeps reaching it through its Items (ARCHITECTURE §5); that path is the link kind `REGISTERED_BY_ICBM`.
- **Items.** A link is at the listing level. A listing option combination maps to an `Item` or `AtomicSKU` only:
  - when a supplier member proves that configuration (ADR-0013 amendment C);
  - and the user confirms the mapping.

  Store option rows never become source facts or AtomicSKUs.

## 4. Evidence-capture tasks: provider facts not yet verified

Every item below is **unverified** at main `40a73ff3`. Each one is captured from the official NAVER Commerce API reference in the slice named, before any code depends on it. Until then, the dependent behaviour stays fail-closed.

| # | fact | slice |
| --- | --- | --- |
| E1 | whether `POST /v1/products/search` accepts a request with no seller-code keyword, and enumerates **every** `statusType` — `SALE`, `OUTOFSTOCK`, `SUSPENSION`, `CLOSE`, `PROHIBITION` and others. The evidence records only the seller-code request (`PRODUCT_SEARCH.md`) | S1a |
| E2 | whether the search has a status filter and a stable sort order | S1a |
| E3 | whether the search's `channelProducts[]` items carry name, price, discount, stock, status, category and delivery. The evidence records only identities and the seller code (`ENDPOINT_MATRIX.md` §4.1.2) | S1a |
| E4 | the JSON paths in the origin-product read of every §5.3 field (discount under `customerBenefit`, options, delivery, `leafCategoryId`, `sellerBarcode`, `modelName`, the customer-service contact) | S1a |
| E5 | whether provider registration and modification times are returned | S1a |
| E6 | the error code of the documented "more than 100,000 results" failure (`PRODUCT_SEARCH.md`) | S1a |
| E7 | the official partial-update endpoints for price, stock and sale status: method, path, body, response, errors. SmartStore UPDATE evidence is `NOT_CAPTURED` (`PRODUCT_UPDATE.md`, `PRICE.md`, `STOCK.md`) | S2c |
| E8 | the partial endpoints for name, images, detail, options, shipping, category and notices | S2d |

## 5. S1 — the store catalog sync (read only)

### 5.1 What is read

| need | endpoint | adoption state at main `40a73ff3` |
| --- | --- | --- |
| enumerate every origin product of the store | `SMARTSTORE_PRODUCT_SEARCH` `POST /v1/products/search` | `ADOPTED` **for positive-only reconcile only**: a seller-code-only request, and kept fields of identities plus the pagination envelope (`ENDPOINT_MATRIX.md` §4.1.2; `registry.py` `_PRODUCT_SEARCH_FIELDS`) |
| full fields of one listing | `SMARTSTORE_ORIGIN_PRODUCT_READ_V2` `GET /v2/products/origin-products/{originProductNo}` | `ADOPTED`, keeping `name`, `salePrice`, `stockQuantity`, `sellerManagementCode`, `sellerManagerCode`, `url`, `statusType`, `channelProductDisplayStatusType` only (`_ORIGIN_READ_FIELDS`) |

**S1a makes two adoption amendments.** Neither is a new endpoint:
1. **A catalog-enumeration request shape for the search** (E1, E2): no seller-code keyword, `page` from 1, `size` 500, and every-status and stable-sort parameters if they are documented.
   - A catalog over the provider's 100,000-result limit is `UNAVAILABLE`, never partial (E6).
2. **A widened, deny-by-default retention profile** on the search and the origin read, for the §5.3 fields. Each field must be named in the official schema (E3, E4).
   - If the search does not carry a field, each new or changed listing is read by its origin read, paced at 1 s (ADR-0023 §3).

The identity fields stay exactly as §4.1.2 reads them. The reconcile use of the search is unchanged.

### 5.2 Completeness and staleness

A sync is **`COMPLETE`** only when all of these hold:
- every page from 1 to the last is read;
- page *n* answers as page *n*;
- every page reports the same `totalElements` and `totalPages`;
- the last page reports `last = true`;
- the number of **distinct** `originProductNo` equals `totalElements`, and none appears twice;
- every page passes the documented-shape reading (`search.py` `read_page`).

Anything else is `INCOMPLETE`, `UNAVAILABLE` (no session, `429`, beyond the page budget) or `ERROR`. Such a sync concludes nothing.

What a `COMPLETE` sync proves:
- **Presence of what it read.**
- **Absence of a key, and only:**
  - for the duplicate preflight of §6;
  - relative to its `as_of`, which is the sync's **start** time;
  - while it is fresh.
- **Never** the remote absence of ADR-0014 §10, §14 and §28, and never `EXTERNALLY_REMOVED`.
  - A listing missing from a complete snapshot is `NOT_SEEN` (「스토어에서 확인 안 됨」).
  - It ends only on ADR-0023 §3 evidence: `statusType = DELETE` or a 404 of its origin read.

**Staleness (owner `6190978708` Q1).** A snapshot is registration evidence for **60 minutes** from `as_of`. The value `catalog_max_age = 60 min` lives in the target policy, and later changes are audited. When the snapshot is older, or an ICBM mutation of that store has started since `as_of`:
- the registration path first runs a **fresh sync automatically**. That is a routine read (ADR-0018 governance clarification), and the UI shows 「스토어 다시 확인 중…」;
- until that sync is `COMPLETE`, the result is `DUPLICATE_EVIDENCE_STALE`, never `NO_MATCH`.

### 5.3 What is stored: an allowlist

| group | fields (SmartStore names; paths per E3/E4) |
| --- | --- |
| identity | `originProductNo`, `channelProductNo`, `channelServiceType` |
| seller keys | `sellerManagementCode`, `sellerBarcode`, `sellerCustomCode1/2`, option `sellerManagerCode`; `modelName`, brand and manufacturer names where the schema has them |
| state | `statusType`, `channelProductDisplayStatusType` |
| price | `salePrice`; the immediate discount (value, unit, period) under `customerBenefit`; the discounted price if returned |
| stock | `stockQuantity`; per option combination `stockQuantity`, `price`, `usable` |
| options | option group names, combination values (`optionName1..4`), combination `id` |
| shipping | `deliveryType`, `deliveryAttributeType`, `deliveryFeeType`, `baseFee`, `freeConditionalAmount`, `deliveryFeePayType` |
| category | `leafCategoryId`; `wholeCategoryName` when returned |
| media | representative and optional image `url`s (provider-hosted; already a retained leaf) |
| seller customer service (owner `6190978708` Q6) | the **seller's own** customer-service contact as the listing states it: the A/S or customer-service telephone and guidance text, which notices require. It is business information of the seller, never a buyer's |
| content digests | SHA-256 of the sanitized `detailContent`, the notice block and the tag set, for change detection. The bodies are read on demand when edited (S2d) |
| time | provider registration and modification times, if returned (E5) |

**Never stored:**
- buyer data;
- address-book addresses (only their numbers, as `SMARTSTORE_ADDRESSBOOK_LIST` keeps);
- return and exchange addresses;
- tokens and signed URLs.

A field outside the allowlist is dropped before anything is hashed or stored (ADR-0011, ADR-0014 §15).

### 5.4 Cadence, storage and display

- **Cadence.** A durable periodic job (default 30 minutes, the ADR-0023 cadence), plus 「지금 동기화」 and the automatic pre-registration sync of §5.2. One sync per store at a time, bounded reads; a `429` pauses it.
- **Storage.** Per sync, one run row. Per store listing, one identity row, immutable. Per sync and listing, one append-only observation of the §5.3 allowlist, with per-field digests.
- **Display (owner Q2).** **The last complete sync time is always shown** wherever a duplicate result or store data is shown: 「마지막 동기화 14:02」. A running or failed sync is shown as such, never as an old success.

### 5.5 Residual risk — accepted (owner `6190978708` Q2)

The provider documents:
- no read-after-write freshness;
- no snapshot semantics;
- no stable sort.

So a listing created or edited in the seller center shortly before or during a sync may be missing from a `COMPLETE` snapshot. A simultaneous insert and delete may also hide one listing behind an unchanged `totalElements`.

The check narrows this risk in three ways:
- the 60-minute bound and the automatic re-sync;
- the per-unit seller-code spot check of §6;
- ICBM's own registry of registrations and `UNKNOWN` scopes.

**The owner accepted the remaining risk** — a duplicate listing visible in the seller's own store — on the condition that the last sync time is shown (§5.4).

## 6. The duplicate check against the snapshot (amends ADR-0014 §13)

The duplicate-evidence seam (`RegistrationPreparationService._current_duplicate_evidence`; `DuplicateEvidence` in `preparation.py`) gets a production source: the **catalog duplicate source**. For one provider-listing unit (group *G*, store *S*), it evaluates the current `COMPLETE`, fresh snapshot of *S*:

| finding in the snapshot of *S* | verdict / reason |
| --- | --- |
| a listing **linked** to *G* (any link kind) | `MATCH`, strong → `PROVIDER_DUPLICATE_FOUND`: one listing per store (owner S3) |
| an unlinked listing whose `sellerManagementCode` equals the unit's `smartstore-seller-management-code/v1` projection, or an owner-declared convention code of a member (ADR-0024 §2) | `MATCH`, strong (`SELLER_CODE`) |
| an unlinked listing whose `sellerBarcode` is a valid GTIN equal to a member's GTIN fact | `MATCH`, strong (`GTIN`) |
| an unlinked listing with an equal brand + `modelName` and no conflict | `MATCH`, weak (`OFFICIAL_KEY` as a signal) → `REVIEW_REQUIRED`; the user decides |
| an unlinked listing with a similar normalized name only | `MATCH`, weak (`NORMALIZED_NAME`) → `PROVIDER_DUPLICATE_WEAK_SIGNAL`, `REVIEW_REQUIRED`. The user links it (§7) or records 「다른 상품」 (revocable) |
| none of the above | `NO_MATCH`, with `keys_checked` and the snapshot's `as_of` |
| no `COMPLETE` snapshot, or a stale one | no evidence: `DUPLICATE_EVIDENCE_UNAVAILABLE` / `DUPLICATE_EVIDENCE_STALE`, and the automatic re-sync of §5.2 |

Rules:
- **Only for §13.** This is duplicate-preflight evidence for a **new** Intent.
  - It is never reconcile evidence and never remote absence for ADR-0014 §10/§14/§28.
  - It never frees an `UNKNOWN` conflict scope.
  - The ICBM-side checks of §13 run first and are unchanged: live or unresolved registrations, `UNKNOWN` scopes, `ADOPTED_LISTING_EXISTS`.
- **The positive spot check.** At the first CREATE copy, the unit's own projected seller code is searched (the §28.2 request, positive only).
  - One exact `STOREFARM` hit that the snapshot did not show is a `MATCH`.
  - Zero hits change nothing.
- **The fingerprint stays stable.** Only the verdict-relevant content enters the candidate fingerprint: verdict, matches, keys checked and the lookup contract version. Snapshot identity and time are excluded, so a re-sync that finds the same thing keeps the ASSET and CREATE stages on one fingerprint (ADR-0018 §3.1 amendment D4). Freshness is checked separately, at send time.
- **`DuplicateOverride` is unchanged** (§13): marketplace × account × group, explicit, audited, revocable.
- **`lookup.py` is replaced.** `available()` is true only while a `COMPLETE`, fresh snapshot exists for the account.

## 7. S2a — linking listings and creating common products

**Link kinds.** At most one `ACTIVE` link per store listing. At most one per group × store, unless a `DuplicateOverride` covers that group in that store.

| kind | made by | rule |
| --- | --- | --- |
| `REGISTERED_BY_ICBM` | derived | `originProductNo` equals an `ACTIVE` ICBM registration's `marketplace_product_id` |
| `ADOPTED` | ADR-0024 | an `ACTIVE` `operate_adopted_listings` row; its Item's group is the target. Read as a link, never rewritten |
| `STRONG_KEY` | the server, **automatically, with a notification** (owner `6190978708` Q3) | equal `sellerManagementCode` (at least 4 characters) or equal valid GTIN barcode, across stores or against a member's facts |
| `USER_CONFIRMED` | the user | any listing → any group, including name-similar candidates. Audited, reversible |

**Strong keys** are the seller code and the barcode (owner Q3), compared exactly after normalization: trim, case, and the GTIN check digit.
- A key shared by two or more listings of the **same** store is ambiguous. It is a candidate, never an automatic link.
- A strong match with a capacity, count or pack conflict in name or options is not linked; the conflict is shown (ADR-0013 §4).
- Each automatic link is announced (「자동 연결됨 · 되돌리기」), and undo ends the link with history kept.

**Weak signals propose only:** name similarity, brand + model, leaf category, a near price, image pHash. The user confirms 「같은 상품」 or 「다른 상품」. Nothing links on a weak signal.

**Common products for every imported listing (owner `6190978708` Q4).** After each `COMPLETE` sync, every listing that is not linked and not strongly linkable gets a new `ProductGroup`, automatically, with zero members and the badge 「도매처 미연결」.
- This runs in order: strong-key linking first, then creation for the rest, so one product sold in two stores becomes one common product when a strong key joins them.
- Weak cross-store candidates are offered afterwards. Accepting one is an ADR-0013 §4 MERGE: a `GroupChangeEvent`, the user picks the survivor, reversible.
- Unlinking ends a link. The group, the listing and every observation stay.

## 8. S3 — duplicate check on collect, and attaching a supplier

**On collect.** Before materialization (`app/stages/products/materialization.py`) creates a new one-member group for a source product with no confirmed group, it checks every group that has a link or a member:

| result | action |
| --- | --- |
| exactly one group by a strong key (an owner-declared seller-code convention, ADR-0024 §2, or a valid GTIN), no conflict | the source product joins that group as a supplier member, `CONFIRMED` automatically, **with a notification**: 「기존 상품 ‘…’에 도매처로 연결됨 · 되돌리기」 (owner Q3). Undo is an audited rejection that returns it to its own group (ADR-0013 §4 reversibility) |
| a strong key matching several groups, or a conflict | `CANDIDATE` for each, `REVIEW_REQUIRED`; the user picks one or 「새 상품」 |
| weak signals only (name, brand + model) | a new group as today, **plus** a candidate 「같은 상품인가요?」; nothing is attached without the user |
| nothing | a new group as today (path a) |

A source product already `CONFIRMED` elsewhere is never moved silently. A move is a user-chosen MERGE or SPLIT.

**Per-supplier prices.**
- Each supplier member keeps its own current source revision, and its price is that revision's fact (ADR-0013 §2–§3). The product shows them side by side.
- The supplier an Item is **procured** from is its `SourceBinding` (ADR-0013 §6). The user picks it as 「매입처」. The change is an audited close-and-open, and it stales pricing and readiness as today.
- Automatic substitution stays later OPERATE work.

**기존 도매처 입력.** On a 「도매처 미연결」 product, the user pastes a supplier product URL:
1. COLLECT's existing submit runs;
2. the recorded source product is attached as `USER_CONFIRMED`, with any conflict shown;
3. the user may then map listing options to proven configurations (§3) and choose 「매입처」.

## 9. S2b — the common-product edit model (decision A1)

```text
content revision (group)  ⊕  store override (group × store)  →  store projection  →  diff against the latest observation  →  preview
```

- **A1 — a new owner.** **CommonContent** (PRODUCT DB) holds the common product's group-scoped, append-only **content revision**.
  - It is separate from the REGISTER preparation.
  - Existing owners keep their truth: the M4 image selection and lineage (ADR-0013 §9), the Common Sales Option revision (ADR-0013 amendment A), the `DETAIL_COMPOSITION` profile (ADR-0014 §19), the notice-coverage owner and category metadata. A content revision **pins** their current revisions, and itself holds only the values no owner holds: name, tags, the delivery-template reference and direct store values.
  - **The REGISTER preparation reads from it.** A new listing's preparation is seeded from the projection, with the content and override revisions recorded as its provenance. It stays M5's authored input (M5-30); the Snapshot and Intent contract is unchanged.
- **Price.**
  - With a 매입처 binding, a store's price is M4's `PricingSnapshot` for that store's pricing context and fees (ADR-0013 §7). ADR-0039's minimum-price policy applies.
  - On a 「도매처 미연결」 product, the price is a direct value.
  - Either way, a store override may set it.
- **Store overrides.** An append-only revision per group × store; any field may be overridden for one store.
- **Projection and preview.**
  - The marketplace adapter renders content ⊕ override into marketplace fields and diffs against the listing's latest observation.
  - A field with no adopted partial endpoint shows 「이 스토어에 아직 반영 불가」. It is never silently skipped.

## 10. Seller-center edits: detect, show, let the user choose

- **The baseline.** Each linked listing has one: per field, the value ICBM last applied and proved by read-back, or the value the user last accepted from an observation.
- **External changes.** Each sync compares the observation with the baseline. A difference is an **external change**, shown on the product and in a list: 「스마트스토어에서 직접 수정됨: field, old → new, time」.
- **Never silently overwritten.** A plan that touches a field with an unresolved external change is blocked for that store until the user chooses, per field:
  - 「스토어 값 유지」: it becomes a store override, or the common value if the user says so;
  - or 「ICBM 값으로 반영」.
- **Before apply.** Each listing is read again right before its apply. If a touched field differs from the plan's recorded current value, that listing stops (`STORE_CHANGED_SINCE_PREVIEW`) and returns to the user.

## 11. S2c — one approval, every store (amends ADR-0018; decisions A2–A4)

**Scope of the first S2c (owner `6190978708` Q7):** price, stock and sale status. Name, images, detail, options, shipping, category and notices follow in S2d.

**Every linked listing is editable (owner Q5).** That covers ICBM-registered, adopted and imported listings, KM's 24 included. Every apply goes through the preview and one approval. ADR-0024 M6E-01 and ADR-0023 M6-01 are lifted **only** through this stage (§13).

**A2 — partial, allowlisted updates only.**
- A listing is updated only through official partial endpoints (E7, E8), with exactly the touched, allowlisted fields.
- A full-body replace or a read-modify-write of the whole product is never used. ICBM never sends a field it did not intend to change.
- A field with no adopted partial endpoint is not applied (§9).

**The change plan** is immutable. It is built from:
- one content revision and the override revisions;
- per listing: the exact listing, the fields, the **before** digest (the fresh observation) and the **after** payload digest.

**A4 — the batch approval.** The user approves the plan once (「한 번에 반영」). The server then, in one unit of work:
- records a **batch approval**:
  - the user, the plan and the authorization reference;
  - **at most 50 listings**;
  - **expiry 30 minutes** after approval;
- issues **one exact UPDATE grant per listing**, each binding:
  - the marketplace and account;
  - the UPDATE endpoint group;
  - the exact listing (`originProductNo`);
  - the before and after digests;
  - **a budget of 1**;
  - the approval's expiry.

  No wildcard and no unit-less grant exist (ADR-0018 §3.2).
- The approval is a server-validated protected action. "UI text and checkboxes are never authority" (§3.3) still holds: the grants are the authority.
- A bounded LIVE window opens only inside this approved scope, and never past the approval's expiry (ARCHITECTURE §13).
- More than 50 listings is several plans and several approvals.

**Every ADR-0018 brake and check still applies, per listing.** Each listing is admitted at send time, in the one unit of work that opens its attempt and spends its grant:
- `LIVE`;
- the brake `RELEASED`;
- its exact grant;
- the adopted endpoint;
- evidence retention proven;
- the fresh read equal to the before digest (§10).

Each listing then has:
- **one durable attempt**, opened before any byte is sent, as DELETE and DISPATCH do (ADR-0018 §3.5, ADR-0025 §5);
- **verification** by a read-back of the touched fields against the after values.

Listings are independent: one listing's failure fails only that listing, and the result is per listing.

**A3 — `UNKNOWN`.** An update whose outcome is unknown is **never resent blindly**. It is resolved by **read-back reconcile** before any retry:
- after-values read back → `APPLIED_PROVEN`;
- otherwise it stays 「재확인 필요」, and the listing is blocked for new plans.

A retry is possible only:
- after a recorded reconcile read-back that does not show the after-values;
- as a new plan built from a fresh read;
- with a new user approval and a new grant.

It is never automatic, and the spent grant is never reused.

## 12. Owners and storage (migrations from `0067`, one per slice)

| owner | holds | slice | migration (indicative) |
| --- | --- | --- | --- |
| **StoreCatalog** (OPERATE, `app/stages/operate/catalog/`) | sync runs; store listings (identity immutable); append-only observations (§5.3); `NOT_SEEN` / `EXTERNALLY_REMOVED` per ADR-0023 §3 | S1a | `0067_store_catalog` |
| **ListingLinks** (OPERATE) | listing ↔ group links, append-only with `ACTIVE → ENDED`; option-to-Item mappings; 「다른 상품」 decisions; automatic-link notifications | S2a | `0068_store_listing_links` |
| **ProductStore** (PRODUCT DB, existing) | groups, members, membership revisions, change events. S3 adds `match_method` values only | S3a | none: `group_members.match_method` is free text with only a non-empty CHECK (`0012`) |
| **CommonContent** (PRODUCT DB, A1) | content revisions, store-override revisions, baselines, external-change records | S2b | `0069_common_content` |
| **StoreUpdate** (OPERATE) | change plans and listings, batch approvals, UPDATE attempts and verifications; `live_grants` rebuilt with the `UPDATE` stage plus `live_grant_update_bindings` (as `0057` did for DISPATCH) | S2c | `0070_store_updates` |

Tables are append-only with delete guards, as in the canary scope (ADR-0018 §8).

New review conditions are `NOT_WIRED` kinds until their producer is wired (ADR-0016): a link candidate, an external change, an ambiguous strong key, a 「재확인 필요」 update. A count is never a false zero.

## 13. Amendments, each effective when its slice lands

| document | amendment | slice |
| --- | --- | --- |
| **ROADMAP §1.1** | **Amended, not superseded.** "Clean start" stays entry path (a). A note adds path (b): an existing operator's current store catalog may be imported read-only (S1) and linked to common products (S2a). Legacy ICBM-PROJECT IDs, rows and state are still **not** imported; what is imported is the live provider catalog, read fresh through adopted endpoints | S1a |
| **ADR-0014 §13** | The adopted provider lookup evidence for SmartStore is the catalog duplicate source of §6. Its `NO_MATCH` holds relative to a `COMPLETE` snapshot under 60 minutes old, and for new Intents only | S3b |
| **ADR-0014 §17.2** | A note scoping its last paragraph. "No lookup result … becomes remote-absence evidence" stays absolute for §10, §14 and §28. The bounded duplicate-preflight use of §6 is not remote-absence evidence and resolves no ambiguity. The verdict stays `INSUFFICIENT` | S3b |
| **ADR-0013 §4** | Materialization consults linked groups and members before creating a group (§8). A group may exist with zero `CONFIRMED` members when it is created for an imported listing (§7) | S2a, S3a |
| **ADR-0024** | Adoption becomes one link kind (`ADOPTED`). Its table and M6E-02..06 are unchanged. M6E-01 ("never writes an adopted listing") is lifted only through S2c's per-listing UPDATE grant (owner Q5) | S2a, S2c |
| **ADR-0023 M6-01** | OPERATE's no-write rule is lifted only for S2c's UPDATE stage, under ADR-0018 | S2c |
| **ADR-0018 §3** | A fifth mutation stage, `UPDATE`, and the UI-issued batch approval of §11: at most 50 listings, 30 minutes, one exact grant of budget 1 per listing, every brake and check per listing. Today every grant is issued by a protected CLI command (`app/interface/cli.py`) | S2c |
| **`ENDPOINT_MATRIX.md` §4, §4.1.2** | The catalog request shape and widened retention (S1a), and each partial UPDATE endpoint (S2c, S2d), each by its own adoption amendment | S1a, S2c, S2d |

## 14. Slices

**Every slice is accepted by a seller walkthrough on the real UI (owner principle 2026-10-15, 「실제 사용자 기준」).** The agent runs the §0 steps the slice delivers, in the running application, against the real store where the slice reads or writes it:
- at desktop and mobile width;
- with screenshots of what the seller sees at each step, recorded in the slice's acceptance record.

Tests and CI are necessary and never sufficient.

| # | slice | seller walkthrough at acceptance | provider |
| --- | --- | --- | --- |
| P0 | this ADR | — | none |
| **S1a** | Evidence capture E1–E6. The two adoption amendments (§5.1). StoreCatalog (`0067`), the sync job, completeness, the 60-minute staleness, the allowlist, the automatic first sync after connect | a1–a2, b1: connect, then the first sync's progress and result on the owner's real store (under the owner's go-ahead for the first real read) | read only |
| **S1b** | 통합DB 「스토어 상품」 tab: every listing, status filters, 「마지막 동기화」 always visible, 「지금 동기화」, 「스토어에서 확인 안 됨」 | a2, b2: filter by each status; the counts match the seller center | none |
| **S3b** | The catalog duplicate source (§6) replaces `lookup.py`. `DUPLICATE_EVIDENCE_STALE` and the automatic re-sync, the positive spot check, the stable fingerprint digest. The ADR-0014 notes. **This unblocks registration** for both paths | a4: the duplicate row shows 「중복 없음 · 동기화 시각 기준」, the re-sync after 60 minutes, and a real match blocking with its choices | read only |
| **S2a** | ListingLinks (`0068`): automatic strong-key links with notification, a common product per imported listing (「도매처 미연결」), weak candidates and confirm, adoptions read as links, unlink, MERGE | b3: after import, every listing is a common product; the automatic-link notice and undo; confirming and rejecting a name-similar pair | none |
| **S3a** | Dedupe on collect (§8): strong automatic attach with notification and undo, weak candidates, per-supplier prices, 「매입처」, 기존 도매처 입력 | a3, b4, b5: collect a product already in ICBM and see it attached, not duplicated; undo it; paste a supplier URL on a 「도매처 미연결」 product | supplier reads as COLLECT does |
| **S2b** | CommonContent (`0069`, A1): content revisions, store overrides, projections, preview, baselines, external-change detection; preparation seeded from it | b6, a6 (preview only): an edit made in the seller center is shown with both choices; the preview shows 현재 → 변경 per store | none |
| **S2c** | E7 and the partial-update adoptions for **price, stock and sale status**. The ADR-0018 amendment (UPDATE stage, batch approval ≤ 50 / 30 min, one grant per listing). StoreUpdate (`0070`): attempts, read-back verification, `UNKNOWN` reconcile (A3). 「한 번에 반영」 | a6, b7: one approval changes price, stock and status of a canary test product and one imported listing; the per-store result; an expired approval refuses (under the owner's go-ahead for the first real update) | **mutation** |
| **S2d** | E8 and the partial updates for name, images, detail, options, shipping, category and notices, one field group per PR | b7 per field group | **mutation** |
| **S4** | Coupang in the same frame: catalog sync, links, duplicate source and update stage, through Track C's adoptions (`CATALOG_PRODUCTS.md`; `C-LIVE-MUT-1` stays its own bounded scope) | the §0 steps for a Coupang store | per slice |

Order: S1a → S1b → S3b → S2a → S3a → S2b → S2c → S2d, then S4.

S2a comes before S3a. S3a attaches a collected product to a common product, and for an existing operator (path b, steps b4 and b5) those common products and their listing links are created by S2a. Path (a)'s step a3 needs only the groups that registration already creates.

S3b comes early because it unblocks registration. Each slice follows the global merge lane, and rule 14 sets its tier. S1a and S3b are `HIGH_RISK` (adoption, REGISTER safety), S2c and S2d are `HIGH_RISK` (mutation), and the others are `PROVIDER_ZERO`.

## 15. Resolved decisions

| # | question | decision | source |
| --- | --- | --- | --- |
| Q1 | how old may a snapshot be as registration evidence? | 60 minutes; then an automatic re-sync before registration | owner `6190978708` |
| Q2 | the residual risk of §5.5 | **accepted**, with the last sync time always shown | owner `6190978708` |
| Q3 | strong-key links | seller code and barcode link automatically, with a notification; name similarity → the user confirms | owner `6190978708` |
| Q4 | common products on import | created automatically for every imported listing, supplier empty, shown 「도매처 미연결」 | owner `6190978708` |
| Q5 | may imported listings be edited? | yes, KM's 24 included; every apply through preview + approval | owner `6190978708` |
| Q6 | seller contact fields | the seller's own customer-service contact is stored (business information, needed for notices) | owner `6190978708` |
| Q7 | first S2c fields | price, stock, sale status; name, images, detail and options later (S2d) | owner `6190978708` |
| A1 | content ownership | a new CommonContent owner, separate from the REGISTER preparation; the preparation reads from it | implementation agent (ADR-0022), 2026-10-15 |
| A2 | full body vs allowlist | partial, field-allowlisted updates only; never a full-body overwrite | implementation agent, 2026-10-15 |
| A3 | an `UNKNOWN` update | resolved by read-back reconcile before any retry; never a blind resend | implementation agent, 2026-10-15 |
| A4 | the batch approval | at most 50 listings, expiry 30 minutes; one exact grant per listing, budget 1; every ADR-0018 brake and check applies | implementation agent, 2026-10-15 |

What is not decided is a provider fact, not a choice. Those facts are the evidence-capture tasks E1–E8 (§4).

## Invariants

```text
SCP-01  the common product is the ProductGroup; a store listing, a link or a content revision is never a product identity
SCP-02  both entry paths — (a) empty store, (b) imported store — converge on the same ProductGroup and the same registration, edit and operation flows; ROADMAP §1.1 path (a) stays valid
SCP-03  a catalog sync is read only and stores only the allowlisted fields; the only contact stored is the seller's own customer-service contact; no buyer data, address, token or signed URL is stored
SCP-04  a sync is COMPLETE only on a full, consistent enumeration whose distinct origin products equal totalElements; anything else concludes nothing
SCP-05  a COMPLETE snapshot is duplicate-preflight evidence for new Intents only, relative to its start time and for 60 minutes; it is never remote absence for ADR-0014 §10/§14/§28 and never frees an UNKNOWN conflict scope
SCP-06  a stale or missing snapshot yields no duplicate evidence and triggers a re-sync; duplicate checking stays on and fail closed in both entry paths
SCP-07  the last complete sync time is shown wherever a duplicate result or store data is shown
SCP-08  only provider evidence (statusType DELETE or a 404 of the origin read) ends a listing; absence from a snapshot is NOT_SEEN
SCP-09  at most one ACTIVE link per store listing and one per group × store, unless a DuplicateOverride covers it
SCP-10  only seller code and barcode (valid GTIN) link or attach automatically, always with a notification and undo; every weak signal only proposes, and nothing merges without the user
SCP-11  a strong key shared within one store, or a strong match with a capacity, count or pack conflict, is never linked automatically
SCP-12  every imported listing not strongly linked becomes its own common product with no supplier, shown as 도매처 미연결
SCP-13  a collected source matching a common product becomes a supplier member of it, never a second product; a CONFIRMED member is never moved silently
SCP-14  per-supplier prices are each member's own facts; the procurement binding changes only by the user's audited choice
SCP-15  a seller-center change is detected, shown and never silently overwritten; the user chooses per field
SCP-16  a listing is updated only through partial endpoints with exactly the touched allowlisted fields, and only if its fresh read equals the plan's before digest
SCP-17  one UI approval covers at most 50 listings and expires in 30 minutes; it yields one exact UPDATE grant of budget 1 per listing; LIVE, brake, grant, adoption, retention and the attempt owner are checked per listing at send time
SCP-18  an UNKNOWN update is resolved by read-back reconcile before any retry and is never resent blindly; one listing's failure never fails another
SCP-19  no endpoint is called before its adoption; adoption is never a call and never LIVE authority
SCP-20  every slice is accepted by a seller walkthrough on the real UI, not by tests alone
```

## References

- ROADMAP §1.1, §2, §6, §8, §14
- ARCHITECTURE §4 (PRODUCT DB, OPERATE), §5 (Product, MarketplaceRegistration), §13
- GLOSSARY §1, §2, §4
- ADR-0004; ADR-0011; ADR-0013 §1, §4–§7, §9, amendments A–C; ADR-0014 §7, §10, §13, §14, §15, §17.2, §17.4, §19, §28, §29, M5-30; ADR-0016; ADR-0018 §3, §3.5, §4, §8, §10; ADR-0023 §3, M6-01; ADR-0024; ADR-0025 §5; ADR-0039
- `integrations/marketplaces/smartstore/registry.py` (`_PRODUCT_SEARCH_FIELDS`, `_ORIGIN_READ_FIELDS`), `search.py`, `lookup.py`
- `documents/contracts/platforms/smartstore/ENDPOINT_MATRIX.md` §4, §4.1.2, §16; `SOURCES.md` §5.3, §5.4
- `documents/evidence/marketplace-apis/PRODUCT_SEARCH.md`, `PRODUCT_READ.md`, `PRODUCT_CREATE.md`, `PRODUCT_UPDATE.md`, `PRICE.md`, `STOCK.md`
- `app/stages/register/preparation.py` (`_duplicate_reasons`, `DuplicateEvidence`), `authoring.py` (`freeze_current`, `_current_duplicate_evidence`), `policy.py` (`DuplicateKeyKind`, `STRONG_KEYS`)
- migrations `0012` (groups, members), `0048` (`operate_listing_observations`), `0051` (`operate_adopted_listings`), `0057` (DISPATCH grant rebuild)
- Issue #219 `6190407624`, `6190428447`, `6190978708`, `6109102895`, `6049563563`
