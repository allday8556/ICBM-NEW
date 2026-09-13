# SmartStore Integration Contracts

This directory is the adopted ICBM integration contract set for NAVER SmartStore / Commerce API.

It is not a generic catalog of every NAVER API. It defines what ICBM currently trusts, what it may execute, what evidence is required, and what remains intentionally unadopted.

## 1. Recommended reading order

Read the documents in this order:

1. [`README.md`](./README.md)
   - orientation, document map, and current M2/M5 boundary.
2. [`CAPABILITY_MAPPING.md`](./CAPABILITY_MAPPING.md)
   - convergence point for `auth`, `write_scope`, `write`, contract freshness, workflow state, reason codes, and UI projection.
3. [`ACCOUNT_IDENTITY.md`](./ACCOUNT_IDENTITY.md)
   - what remote seller identity must be proven and how `accountUid` is used.
4. [`AUTH.md`](./AUTH.md)
   - how SmartStore authentication is acquired, persisted, renewed, recovered, and bound to session generations.
5. [`PERMISSIONS_SCOPES.md`](./PERMISSIONS_SCOPES.md)
   - provider API-group permission evidence and `write_scope` semantics.
6. [`ENDPOINT_MATRIX.md`](./ENDPOINT_MATRIX.md)
   - which endpoints are actually adopted, their method/path/request/response/success contracts, and execution allow-list status.
7. [`ERRORS.md`](./ERRORS.md)
   - how transport/provider failures are classified, when retry/reconciliation is allowed, and how uncertainty is preserved.
8. [`SOURCES.md`](./SOURCES.md)
   - source authority, provenance reverse index, runtime-evidence registry, and freshness/drift rules.

`CAPABILITY_MAPPING.md` is intentionally near the start because it is the convergence contract for the other documents. The owning documents remain authoritative for their specific domains.

## 2. Current M2 execution boundary

M2 is **SmartStore CONNECT**, not SmartStore REGISTER.

The only SmartStore endpoints currently `ADOPTED` for M2 execution are:

- `SMARTSTORE_AUTH_TOKEN`
  - `POST /external/v1/oauth2/token`
- `SMARTSTORE_SELLER_ACCOUNT`
  - `GET /external/v1/seller/account`

The exact endpoint contract and base-path rules are defined in `ENDPOINT_MATRIX.md`.

All other SmartStore endpoints currently remain:

`NOT_ADOPTED`

Consequences:

- `NOT_ADOPTED` means no network I/O through the SmartStore integration path.
- A documented NAVER endpoint is not automatically an adopted ICBM endpoint.
- A configured feature flag, desired capability, operator attestation, or historical success cannot bypass endpoint adoption.
- Product/category/image APIs mentioned for future planning remain unavailable to M2 runtime.

## 3. M2 capability boundary

M2 keeps three capability layers independent:

```text
auth
write_scope
write
```

A healthy M2 state may be:

```text
auth        = READY
write_scope = READY | MISSING | UNKNOWN
write       = UNVERIFIED
```

For product registration during M2:

`write = UNVERIFIED`

always.

M2 MUST NOT perform a product mutation merely to make `write` or `write_scope` look more complete.

The compact UI projection is defined in `CAPABILITY_MAPPING.md`, for example:

```text
인증      ● 연결됨
등록 권한 ◐ 권한 확인됨 (관리자 화면 확인)
실제 등록 ○ 미확인
```

The symbols carry evidence strength:

- `●` machine/runtime-proven strong evidence;
- `◐` operator-attested positive evidence;
- `○` unverified/unknown.

## 4. M5 boundary

M5 is where SmartStore product registration becomes eligible for adoption and proof.

Before any product write can become `READY`, M5 must separately review and adopt the required mutation/read-back endpoints and define at least:

- complete endpoint transaction set;
- required API-group union;
- success predicates;
- redirect behavior;
- idempotency/replay rules;
- read-back/reconciliation contract;
- bounded real CREATE acceptance evidence.

Until that happens:

```text
product endpoint = NOT_ADOPTED
product write    = UNVERIFIED
```

A successful permission attestation in M2 does not change this boundary.

## 5. Evidence and freshness

Documentation evidence and runtime proof are deliberately separate.

Core rules:

```text
retrieved_at != verified_at
provider documentation != runtime verification
contract_freshness != runtime capability truth
```

`SOURCES.md` indexes provider documentation, official support evidence, standards, implementation-library references, and measured runtime evidence.

A stale contract may coexist with still-valid runtime proof, but stale understanding blocks new expansion decisions until review.

## 6. Change rule

Do not extend SmartStore behavior by editing code first.

The order is:

```text
source/change detection
→ contract impact review
→ owning document update
→ SOURCES provenance update when applicable
→ tests/evidence requirements
→ implementation
→ runtime acceptance/read-back
```

Adding a new endpoint, workflow scope, durable reason code, auth mode, permission interpretation, or replay rule requires reviewed contract change before runtime use.

## 7. Current document set

The active SmartStore contract set is:

- `README.md`
- `CAPABILITY_MAPPING.md`
- `ACCOUNT_IDENTITY.md`
- `AUTH.md`
- `PERMISSIONS_SCOPES.md`
- `ENDPOINT_MATRIX.md`
- `ERRORS.md`
- `SOURCES.md`

These documents are self-contained where practical, while `SOURCES.md` acts as the provenance reverse index and drift-audit view.
