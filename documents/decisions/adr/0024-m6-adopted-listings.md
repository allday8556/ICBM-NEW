# ADR-0024 — M6-E: adopting SmartStore listings ICBM did not create

Status: **ACCEPTED** 2026-10-08. It lands with its own PR (M6-E) and amends nothing in ADR-0023; it
adds a second kind of operated listing beside ICBM's own registrations.

Decision owners:
- **Product direction:** the owner, Issue #219 `6049563563`.
  - M6 acceptance is run on a KM listing the operator registered by hand and that is already on
    sale, not on a new LIVE test registration (original choice "이미 판매 중인 KM홀딩 상품 사용").
  - Those listings carry the seller product code `km` followed by the KM통상 product number,
    exactly ("상품코드 km으로 시작해", format confirmed as `km287`).
  - **Correction** (Issue #219 `6051294742`, runtime evidence): the listings carry upper-case
    `KM` (`KM88`, `KM190`), and the provider's seller-code search matches it case-sensitively.
    The convention is therefore `KM` + the KM통상 product number.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- an OPERATE owner of **adopted listings**: a SmartStore listing that exists on the provider, that
  ICBM did not create, and that the operator links to ICBM's canonical identity by a seller-code
  convention the owner declared;
- the read-only provider calls that prove one: the adopted product search and the adopted
  origin-product read;
- reading an adopted listing back, rechecking its source's stock, and linking its orders, exactly
  as ADR-0023 §3–§5 do for ICBM's own registrations;
- a REGISTER preflight refusal so ICBM never creates a second listing of an adopted Item.

What it does not authorize:
- **No marketplace mutation:** an adopted listing is never edited, repriced, stopped or deleted by
  ICBM (M6-01).
- **No REGISTER registration** for an adopted listing: it has no Intent, Snapshot or read-back
  comparison; ADR-0014's registration owner is unchanged.
- **No linking by name**, by similarity, or by a code outside a declared convention.

Sources: ADR-0013 (canonical identity), ADR-0014 §13 (duplicates) and §28.2 (seller-code search),
ADR-0023 (M6), `ENDPOINT_MATRIX.md` §4.1.2 (search) and §4 (origin read).

## 1. Identity

- An adopted listing is the pair *(SmartStore origin product, ICBM source product)*.
  - The source product is a COLLECT source identity (`supplier_key`, `source_product_id`).
  - Its canonical Item is the Item that source product's open binding names.
  - At most one `ACTIVE` adoption per source product, and one per SmartStore listing. An adoption that provider evidence ended stays as history; a later pass may adopt that source again as a new row, only through the same proof (§3).
- The link is made only by a **declared seller-code convention** (§2), never by a name.
- A source product that an `ACTIVE`, not ICBM-deleted registration already sells is not
  adopted: ICBM's own registration is the listing (`REGISTERED_BY_ICBM`).

## 2. Seller-code conventions

| Supplier | Convention | Example |
| --- | --- | --- |
| `kmretail` | `KM` + the source product id, exactly | source `287` → `KM287` |

- A convention is owner-declared. A new one needs an owner decision recorded in Issue #219.
- The code ICBM searches for is the convention applied to the source product id. It must match
  `^[A-Za-z]{2,8}[0-9]{1,15}$`; anything else is refused before any provider call.

## 3. Proof of one adoption

For each source product of the supplier (with a current revision and exactly one open-bound
Item), in stable order:

1. **Search** the adopted `SMARTSTORE_PRODUCT_SEARCH` by `SELLER_CODE` with the convention code,
   with the same bounded, consistent enumeration as ADR-0014 §28.2 (at most 4 pages of 500).
2. **Candidates** are only `STOREFARM` channel entries whose `sellerManagementCode` is exactly the
   code. The provider's own match is never trusted.
   - Zero candidates: `NOT_FOUND`.
   - More than one: `AMBIGUOUS`.
   - An enumeration that cannot complete: `UNAVAILABLE`.
3. **Read back** the one candidate's origin product with the adopted
   `SMARTSTORE_ORIGIN_PRODUCT_READ_V2`. It is adopted only if:
   - its `sellerManagementCode` is exactly the code;
   - its `statusType` is not `DELETE`.

   Otherwise the outcome is `MISMATCH` or `DELETED`.
4. **Record** the adoption: the origin and channel product numbers, the code, the source identity,
   the Item, the status the read showed, the actor and the time, audited.

Rules that hold throughout:
- **Pacing:** each provider call waits 1 s after the previous one, as order ingest does.
- **No retries:** a rate limit ends the pass, and what was not reached is not attempted.
- **Recorded once:** an adoption is never rewritten (trigger).
- **Removal:** it ends only as `EXTERNALLY_REMOVED`, on provider evidence (§4).
- **Re-adoption:** a removed listing can be adopted again as a new row.

## 4. Operating an adopted listing (ADR-0023 §3–§5)

- **Listing-state sync:** every pass also reads each `ACTIVE` adopted listing back.
  - Each read appends an observation to its own append-only table.
  - The fields are ADR-0023 §3's sanitized set, plus whether the seller code still matches.
  - `statusType = DELETE` or a 404 moves the adoption to `EXTERNALLY_REMOVED` (provider evidence
    only).
  - The pass shows drift and never repairs it.
- **Stock recheck:** an `ACTIVE` adopted listing's source product is a listed source, beside ICBM's
  registrations. Its sold-out judgment is STOCK review work as ADR-0023 §4 defines.
- **Orders:** an order of an adopted listing is a product ICBM did not register.
  - Its ADR-0023 resolution is recorded `UNMATCHED`, exactly as that contract says.
  - It is also linked to the adoption by its origin product id. The link is a separate,
    immutable row naming the adoption, the Item and the source identity.
  - A link is recorded once, at ingest or at the end of a later pass.
  - It is never made by name, and it never rewrites the order's resolution.

## 5. No second listing of an adopted Item

REGISTER's preflight reads, through an injected source, the Items of `ACTIVE` adopted listings of
the target marketplace. A unit with such an Item gets `ADOPTED_LISTING_EXISTS`. It is in the
DUPLICATE area, like `LIVE_REGISTRATION_EXISTS`, and an active duplicate override of the account
covers it the same way.

## 6. Storage and interface

- **Migration `0051`:**
  - `operate_adopted_listings`: unique per marketplace product and per marketplace × source
    product among `ACTIVE`; adoption columns immutable; the only change is `ACTIVE` →
    `EXTERNALLY_REMOVED`.
  - `operate_adopted_observations`: append-only.
  - `operate_order_adoption_links`: one per product order; append-only.
- **API:**
  - `GET /api/v1/operate/adoptions`
  - `POST /api/v1/operate/adoptions/run` with `{supplier_key}`: one adoption pass, returning one
    outcome per source product.
- **Screens:** the 등록관리 판매 상태 panel lists adopted listings beside ICBM's own, marked
  "가져온 상품"; the 주문관리 panel shows an order's adoption link.

## Invariants

```text
M6E-01  ICBM never writes an adopted listing; adoption is read-only provider work
M6E-02  a listing is adopted only by an owner-declared seller-code convention, an exact STOREFARM
        candidate, and an origin read-back carrying the same code and not DELETE — never by name
M6E-03  at most one ACTIVE adoption per source product and per origin product; an adoption is
        never rewritten, only ended as EXTERNALLY_REMOVED on provider evidence, and an ended one
        stays as history (a re-adoption is a new row, proven again)
M6E-04  a source product an ACTIVE ICBM registration sells is never adopted
M6E-05  an adopted listing is never a REGISTER registration, and an order's ADR-0023 resolution is
        never rewritten by an adoption link
M6E-06  REGISTER refuses a second listing of an adopted Item (ADOPTED_LISTING_EXISTS)
```
