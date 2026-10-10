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
explicitly queued fake response. The Coupang package imports no HTTP client, opens no socket and
requests no egress grant. A real transport belongs to a later endpoint-adoption slice with its own
official detail-page contract, retention, timeouts, redirect policy and safety review.
