# ADR-0007 — Supplier-generic CONNECT: capability boundary, secrets, protected-read proof, capability readiness

Status: **PROPOSED** — records the frozen M1 contract; becomes ACCEPTED with the M1 architect PASS. The implementation choices listed under "Choices for review" are Claude's and await that review.
Decision owner: Architect (ChatGPT). Sources: Issue #7 body; freeze `5653533064`; clarification `5653567880`; addenda `5653591871` and `5653608622`; audit note `5653615136`.
Recorded by: Claude Code, per Issue #7. The number was confirmed free in `docs/adr/` immediately before writing.
Date: 2026-09-13
Related: ADR-0001 (stack, secrets row, Playwright via jobs), ADR-0002 (in-process worker), ADR-0005 (job states, only TRANSIENT/RATE_LIMITED retry), ADR-0006 (single data-directory owner, mutation-target invariant)
Supplier name: **KM통상** (`https://kmretail.co.kr`, `supplier_key = kmretail`) is the canonical display name per Issue #7 addendum `5654634584` (the user's decision of 2026-09-14). ROADMAP and the Issue #7 body call this supplier "K홀세일". The key and package identity stay `kmretail`.

---

## Context

M1 is the first milestone with real supplier-account egress. It has to prove that ICBM can authenticate to KM통상 and keep that connection safe:

- no plaintext secret at rest;
- no login storm;
- no unattributed egress;
- a connection is proven, not badged.

M1 must also avoid designing the core around one site. KM통상 is the first adapter, not the model.

## Decision

### 1. Port and adapter boundary: object capability, not only imports

- **A supplier implementation is site knowledge only.** It is an immutable `SupplierDefinition` in `integrations/suppliers/<key>/`, made of three parts:
  - `SupplierProfile`: key, base URL, auth flag, declared egress hosts, `RequestPolicy`.
  - `ProtectedReadProbe`: a target path plus two pure predicates over an immutable `ProbeResponse`.
  - `LoginFormSpec`: selectors for the login form.
- **The supplier receives no client, page, context, transport handle or callback.** No `httpx.Client`, no Playwright `Page` or `BrowserContext`, no `_raw` escape hatch. The only thing its code ever sees is a `ProbeResponse` (status, path, redirect path, body).
- **Everything that acts is common infrastructure:**
  - pacing (concurrency cap and minimum spacing);
  - HTTP and browser execution;
  - request attribution;
  - `SingleFlightAuth`;
  - session lifecycle;
  - the proof procedure;
  - the PAUSED loop guard;
  - allowlisted audit and log payloads.
- **Core CONNECT (`app/connect`) consumes the `SupplierGateway` port.** Its policy-enforcing implementation (`integrations/suppliers/transport`) is the only place raw clients are used.
- **Repository rules enforce the boundary** (`tests/unit/test_repository_rules.py`):
  - supplier packages import only the port's data types (no network, browser, logging or `app.*`);
  - raw clients are imported only by the listed owners;
  - egress grants are opened only by the common transport;
  - every predicate takes exactly one `ProbeResponse` (`tests/unit/test_supplier_probes.py`).

### 2. `SupplierConnection`: canonical identity and one state machine

- There is one row per `supplier_key` (`UNIQUE`), so the identity cannot be duplicated.
- The connection state is one explicit state machine with validated transitions (`app/connect/state.py`):

```text
DISCONNECTED → SESSION_CHECK | AUTHENTICATING
SESSION_CHECK → READY | AUTH_EXPIRED | DEGRADED
AUTH_EXPIRED → REAUTHENTICATING → VERIFYING → READY
AUTHENTICATING → VERIFYING → READY
rejected logins → PAUSED  (left only by an audited operator resume)
```

- `READY` can be entered only from a verification step, only with a proven protected read, and only with `last_verified_at`. A database `CHECK` enforces the last condition.
- `auth_state` and `session_state` (the ROADMAP §4.1 names) are views of this one state, not stored separately.
- A new process holds no proof: every state except PAUSED restarts as `DISCONNECTED`.
- Safe counters record the real-login budget: `real_login_attempts`, `session_reuse_count`, `reauth_count`, `last_login_attempt_at`, `consecutive_auth_failures`.

### 3. Secret boundary

- **Credentials** are stored in the OS secret store only (`supplier:<key>:username` / `:password`).
- **The authenticated session is credential-equivalent.** It is stored as an AES-256-GCM blob at `<ICBM_DATA_DIR>/sessions/<key>.enc`.
  - The 256-bit key lives in the OS secret store.
  - The supplier key is bound into the authenticated data, so one supplier's blob cannot be replayed as another's.
  - Replacement is atomic: encrypted temp file, then fsync, then rename.
  - A blob that cannot be authenticated or decrypted is discarded, never partially reused.
- **Only cookies for the supplier's own hosts** are kept in the session payload.
- **Credential form** (Issue #7 addendum `5654634584`):
  - The operator's loopback UI may read the saved **login ID** on demand from the OS secret store. It is never persisted anywhere else.
  - The password is never returned. The UI shows only a masked stored state (`•••••••• · 저장됨`) until the operator explicitly chooses 비밀번호 변경.
  - A save without a password keeps the stored one. A password containing the mask character is rejected (`SUPPLIER_PASSWORD_MASK_REJECTED`), so the indicator can never replace the credential.
- **No plaintext secret appears in the database, logs, audit payloads, API responses or evidence.** Two defences apply:
  - Validation errors no longer echo submitted values.
  - The API accepts credentials and never returns them.
- **Browser containment:**
  - The browser runs headless in an off-the-record context, with no persistent profile.
  - Tracing, HAR, video and screenshots are all off.
  - Service workers and downloads are blocked.

### 4. Protected-read proof: the only READY proof

The common procedure (`app/connect/proof.py`) applies to every supplier.

1. **Unauthenticated control request** against target P. It must satisfy the unauthenticated expectation and must not look authenticated.
2. **Authenticated request** against the same P. It must satisfy the authenticated predicate and must not look unauthenticated.

HTTP status is never proof. For KM통상, the unauthenticated control of `/myshop/index.html` answers **200**. Its `xans-myshop` page skeleton is present anyway, so status and skeleton prove nothing.

Only the following results are accepted:

- The control must show the logged-off state module or the login-check redirect.
- The authenticated read must show the logged-on state module **and** the member logout action together.

Any other outcome is not accepted:

- A target that the control does not prove to be gated fails with `SUPPLIER_TARGET_NOT_AUTH_GATED` (POLICY_BLOCKED).
- Contradictory or unrecognised pages, and a predicate that raises, are all `UNRECOGNIZED`.

Neither outcome is ever READY. A login submission never marks READY by itself.

### 5. Automatic connection, bounded re-authentication and the loop guard

- **Lazy by default.** Startup loads metadata only and makes zero supplier requests.
- **Reuse first.** A protected operation reuses the stored session. Only a missing or expired session leads to **one** authentication per operation, followed by the same proof.
- **Single flight.** `SingleFlightAuth` makes callers that arrive while a flight is in progress wait for it and share its **outcome, whether a proof or a failure** (PR #9 review `5191372031`). A burst with a rejected password therefore submits exactly one login and counts exactly one failure.
- **Serialised credential replacement.** The flight runs under a per-supplier lifecycle lock. Credential replacement, resume and the auto-connect switch take the same lock. A login still using the old credentials completes first, and replacement then discards its session, so an old-account session can never be the READY connection after new credentials are saved.
- **Auto-connect setting.** `auto_connect` is a per-supplier operator setting, on by default. It governs automatic operations only; the manual connection test stays available. No startup auto-connect exists.
- **Loop guard.** Each rejected login counts toward `RequestPolicy.auth_retry_limit`. So does a login the protected read shows was not effective. Reaching the limit moves the connection to `PAUSED`:
  - no further automatic or manual authentication reaches the supplier;
  - leaving PAUSED requires the operator's resume, which is audited with `prior_state`, failure count and limit, `resumed_at` and actor;
  - saving new credentials does not lift the pause.
- **One attempt per connection test.** The connection-test job has **one attempt**, so a job-level retry can never submit the real login again.
- **Error classes:**
  - rejected or ineffective login → `AUTH`;
  - site or network trouble → `TRANSIENT`;
  - HTTP 429 → `RATE_LIMITED`.

### 6. Capability readiness separated from core readiness

`GET /api/ready` keeps core readiness in `status` and `checks`, and adds capability readiness. The capability does not affect the core verdict.

| Field | Content |
| --- | --- |
| `overall` | `READY` or `NOT_READY` |
| `capabilities` | one entry per supplier, e.g. `supplier:kmretail` |
| `degraded_capabilities` | every capability that is not READY |

The HTTP status depends only on the core:

- a core failure answers 503;
- a healthy core answers 200, whatever the supplier capability says.

Capability status is derived from the state machine: `READY`, `NOT_CONFIGURED`, `DISCONNECTED`, `CONNECTING`, `AUTH_EXPIRED`, `DEGRADED` or `PAUSED`. Keys are instance-scoped.

### 7. Supplier egress allow-boundary

The global egress guard stays installed and blocking.

**HTTP requests.** The common transport opens `EGRESS.grant(owner, profile.egress_hosts)`:

- inside the grant, only those hosts and the addresses they resolve to are reachable;
- the grant applies only in the calling context;
- anything else stays blocked and counted in `external_attempts`;
- granted events are counted per owner.

**Browser login.** The browser runs out of process, so every browser request is routed through the same host allowlist. Blocked browser requests are counted on the request record.

KM통상's hosts are `kmretail.co.kr` and `login2.cafe24ssl.com`, the Cafe24 secure-login encryption host. On 2026-09-13 the login page's form and encryption scripts were verified to load under exactly these two hosts, without any credential.

### 8. Observability and safe payloads

Every supplier request logs one `supplier.request` line with these fields:

- `supplier_key`
- `request_kind` (`CONTROL_READ`, `PROTECTED_READ` or `AUTHENTICATE`)
- `transport` (`HTTP` or `BROWSER`)
- `started_at`, `finished_at`, `latency_ms`
- `retry_count`, `result_class`, `http_status`
- `target`

Audit and log payloads in CONNECT come only from the allowlist builder `app.core.safe_payload`:

- unknown fields and non-scalar values raise, so a new field cannot leak by default;
- runtime secret scanning (`app/system/secret_scan.py`) is the second, independent control;
- the scan covers five encodings (raw, URL, JSON, Base64 at every alignment, HTML entities) and reports counts only.

### 9. Scope

M1 contains no ProductFacts, product list or detail collection, AI, SmartStore or registration work. A repository rule fixes the schema to `jobs`, `job_attempts`, `audit_events` and `supplier_connections`.

## Choices for review

These are implementation choices within the contract, recorded for the architect's review:

1. **The `cryptography` dependency** (AES-256-GCM) is added for the session blob, pinned in `constraints.txt`. The alternative was Windows DPAPI via ctypes, which is Windows-only and would leave CI's Ubuntu leg without a real cipher.
2. **Browser login, HTTP protected reads.** KM통상's login form is encrypted client-side by Cafe24 AuthSSL, so authentication runs in the browser. The resulting supplier-host cookies are then replayed over HTTP for the protected reads, which keeps every read inside the egress grant. If a supplier binds sessions to the browser, a browser-side read belongs in the common transport, not in the adapter.
3. **The browser channel defaults to the locally installed Edge** (`ICBM_BROWSER_CHANNEL=msedge`): a Chromium engine, with nothing downloaded.
4. **KM통상's authenticated predicate** (logged-on module plus logout action) matches the Cafe24 storefront convention. It must be confirmed by the first real login.
   - A mismatch fails safe as `UNRECOGNIZED`, never READY.
   - Informational signals (`logout_text`, `member_modify_link`) are recorded, as marker names only, so the page can be diagnosed without storing content.

## Consequences

- A second supplier is added by writing a new `SupplierDefinition`. No core CONNECT change is needed, and no new policy.
- The real account sees at most one login per operator connection test. A normal acceptance run uses exactly two logins: the first connection and one forced-expiry refresh.
- Adding a new request kind or supplier resource requires changing the common gateway, not the adapter.

## References

- Issue #7 and the comments listed above
- `app/connect/`, `app/core/egress.py`, `app/core/safe_payload.py`, `app/system/secret_scan.py`, `app/system/readiness.py`
- `integrations/suppliers/base.py`, `integrations/suppliers/transport/`, `integrations/suppliers/kmretail/`
- `tests/unit/test_supplier_probes.py`, `tests/unit/test_egress_grant.py`, `tests/unit/test_supplier_sessions.py`, `tests/integration/test_connect_lifecycle.py`, `tests/integration/test_connect_api.py`, `tests/integration/test_ownership_children.py`, `tests/unit/test_repository_rules.py`
- `scripts/m1_acceptance.py`
