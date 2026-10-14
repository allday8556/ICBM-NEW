# Coupang Open API Authentication Contract

## Status

| Field | Value |
| --- | --- |
| Slice | `C-AUTH-1` |
| Scope | HMAC, credential/vendor, market, IP-allowlist, clock, auth-error and fake-transport contracts |
| Runtime endpoint adoption | `NONE` |
| Real provider calls | `FORBIDDEN` |
| Execution mode | `DRY_RUN / provider-zero` |

This slice creates no Coupang session, invokes no endpoint, stores no real credential and grants no
egress or mutation authority.

`credential readiness != endpoint adoption != LIVE authority`

## Official sources

| Source | Contract evidence |
| --- | --- |
| [Creating HMAC Signature](https://developers.coupang.com/en/getting-started/creating-hmac-signature) | HMAC-SHA256 message and `CEA` Authorization construction; provider gateway host |
| [OPEN API Test Guide](https://developers.coupang.com/en/getting-started/open-api-test-guide) | `Authorization`, `X-Requested-By`, `X-MARKET`; vendor and market context |
| [Issue Open API Key (NEW)](https://developers.coupang.com/en/getting-started/issue-open-api-keynew) | Wing issuance, Access/Secret key lifecycle, 180-day validity, linking-information propagation and no separate test environment |
| [Invalid signature FAQ](https://developers.coupang.com/en/faq/an-invalid-signature-error-is-returned-hmac-error) | vendor/key/path/time/timezone are distinct invalid-signature causes |
| [Expired signature FAQ](https://developers.coupang.com/en/faq/specified-signature-is-expired-401-error-return) | signatures last at most five minutes and must be generated anew for every call |
| [Not allowed IP FAQ](https://developers.coupang.com/en/faq/when-calling-the-api-a-403-forbidden-not-allowed-ip-error-occurs) | exact 403/IP evidence and up-to-30-minute linking propagation |
| [What is an OpenAPI key?](https://developers.coupang.com/en/faq/what-is-an-openapi-key) | Access Key + Secret Key authenticate and identify the vendor |

The official examples disable TLS verification in demonstration code. That is not adopted: ICBM
never disables certificate or hostname verification.

## HMAC construction

For every request, using an aware UTC clock:

1. format `signed-date` as `yyMMdd'T'HHmmss'Z'`;
2. form the exact message `signed-date + uppercase HTTP method + path + query`;
3. the query is the exact transmitted query without a leading `?`; empty query contributes the
   empty string;
4. compute lower-case hexadecimal HMAC-SHA256 with the Secret Key;
5. emit
   `CEA algorithm=HmacSHA256, access-key=…, signed-date=…, signature=…`.

No signature is cached. A fresh signature is created for every request. Request bodies do not enter
the documented signature message.

## Credential, vendor and market identity

- Access Key and Secret Key form one committed credential generation and never appear in repr,
  logs, errors or retained fake evidence.
- `vendorId` is explicit provider identity. It is never derived from an ICBM account id or a key.
- `X-Requested-By` carries that vendor id.
- `X-MARKET` is an explicit closed value, `KR` or `TW`; a later endpoint contract must also permit
  that market.
- Key validity/rotation is provider state. This slice does not guess expiry from local timestamps.

## IP allowlist

The provider exposes no allowlist-introspection endpoint in the official material used here. ICBM
therefore records only an operator declaration of the outbound IP and the credential generation
whose Wing linking information was configured. It is setup evidence, never proof that Coupang has
accepted the IP. Missing or generation-stale evidence keeps `setup_evidence_current=false`.

Even current declaration evidence grants neither endpoint adoption nor egress nor LIVE authority.

## Clock and error boundary

- A naive datetime is rejected locally. The signer converts an aware clock value to UTC.
- Official exact text `Specified signature is expired` at 401 maps to
  `AUTH / COUPANG_SIGNATURE_EXPIRED`.
- Other 401 responses map to contextual `AUTH / COUPANG_AUTH_REJECTED`; they do not prove which of
  key, vendor, path, time or timezone was wrong.
- Only the official `Not allowed IP` evidence at 403 maps to
  `POLICY_BLOCKED / COUPANG_IP_NOT_ALLOWED`; an unrelated 403 remains `UNKNOWN`.
- Unknown 4xx responses remain `UNKNOWN`. Classification alone never authorizes a retry.

## Provider-zero transport

C-AUTH-1 ships only an in-memory `FakeTransport`. It records a prepared request and returns an
explicitly queued fake response. Every provider-zero adapter continues to use that boundary.

## C-LIVE-R1 bounded extension

| Field | Value |
| --- | --- |
| Endpoint | `#25` — category auto-matching agreement |
| Runtime surface | One exact KR `GET`; no caller-selected URL, method, query or body |
| Calls per runner | `1` with no automatic retry |
| Mutation authority | `NONE` |
| Credential storage | One atomic OS-secret-store record |

The extension requires an approval bound to vendor id, credential generation, market and declared
source IP. The approval window is at most 24 hours. Immediately before use, a clock preflight must
be at most ten minutes old with no more than five seconds observed skew. The endpoint is signed
fresh, the HTTP client does not trust environment proxies or follow redirects, and an explicit
egress grant covers only `api-gateway.coupang.com` for that call. CI and test processes cannot
construct the live transport. The response is capped at 64 KiB and reduced to one boolean; raw
provider content and provider messages are never retained in results or errors.

The operator enters Access Key and Secret Key only through the no-echo
`icbm coupang save-credentials` prompts. They are not command-line options. The bounded call is
exposed as `icbm coupang read-canary`; its arguments bind approval, declared source IP and clock
preflight only and cannot select another provider target.

## C-LIVE-MUT-1 bounded extension

| Field | Value |
| --- | --- |
| Products | Exact user-bound synthetic `test 1` through `test 11` only |
| Runtime surface | Fixed create -> full saved-product update -> read-back -> delete paths on KR |
| Approval request | `requested=false`; sale approval is never requested |
| Retry | None; an in-flight journal barrier requires manual recovery after ambiguity |
| Delete authority | Only the `sellerProductId` returned by this run's successful create |

The owner accepts one closed payload bundle whose SHA-256 digest is bound into a current approval,
along with vendor id, credential generation and the declared Wing source IP. It rejects missing,
reordered or additional products, `test 12+`, changed synthetic identities, multiple items and any
request for sale approval. A durable journal records only state, digest and returned identities;
it is committed before each provider call so a crash or timeout can never cause a blind retry.

Each successful create is first read back to obtain the provider-assigned item identity, updated
through Coupang's full saved-product modification path, read back again and verified as the same
saved synthetic SKU, then deleted through that exact returned identity. The partial-update path is
not used because Coupang excludes temporarily saved products from that contract. Existing Coupang
products are never caller-selectable. The live client uses the same clock, redirect, proxy, response
cap, secret-store and explicit-egress constraints as C-LIVE-R1. CI and tests can supply only an
in-memory transport. The operator command requires the exclusive data-directory lease and takes a
reviewed bundle path and digest, never Access Key or Secret Key command-line arguments.

### Ambiguous create recovery

`icbm coupang recover-test-product-create` is the only recovery surface for a journal with exactly
one `create_inflight` record. It performs no mutation. It queries the exact bound
`externalVendorSku`, accepts exactly one positive candidate matching the bound vendor and product
name, then performs one provider-ID product read and verifies vendor, saved status, product name,
seller product identity, external SKU and provider-assigned item identity. Only that complete
positive chain may advance the journal to `created` so the normal owner can later resume.

An empty, multiple, malformed, unavailable or mismatching answer proves nothing and leaves the
barrier unchanged. In particular, zero results never authorize a repeated create. The recovery
owner is single-use, KR-only, bound to the exact eleven-product bundle digest and full journal key
set, uses the same fresh clock/IP/secret/egress guards, and has no caller-selectable product ID.
