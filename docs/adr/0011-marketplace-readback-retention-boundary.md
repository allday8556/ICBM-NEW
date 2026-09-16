# ADR-0011 — Marketplace raw read-back retention boundary

Status: **ACCEPTED**
Decision owner: Architect (ChatGPT)
Date: 2026-09-16
Related: Issue #58; ADR-0010 §9

## Context

M5 will need durable product/listing read-back evidence for REGISTER idempotency, reconcile, and read-back. A long-lived immutable or content-addressed artifact can be appropriate for that narrow evidence class.

M6 later introduces operational ingest. That data has a different lifecycle and must not automatically inherit the M5 storage pattern. This ADR freezes that boundary before implementation can generalize the nearest existing mechanism by accident.

## Decision

### 1. REGISTER registration evidence only

Long-lived immutable/content-addressed raw read-back artifacts may be used for **M5 registration and reconciliation evidence produced for REGISTER**: the CREATE attempt, the deterministic reconciliation that resolves an unknown CREATE result, and the registration read-back that proves what the marketplace recorded.

The allowance is bound to that purpose, never to the entity type of the payload. Being product/listing scoped is necessary but never sufficient. `docs/ARCHITECTURE.md` gives OPERATE the recurring listing state/read-back, the stock and sold-out synchronisation, orders, inquiries and claims; a recurring post-publication read of a listing's state, status or price is M6 operational ingest even though its payload is product/listing scoped, and it does not inherit this allowance.

The artifact must be sanitized of credentials, session material, credential-equivalent secret-bearing URL material (§3) and unrelated private account data.

This ADR does not define the final M5 schema, capture kinds, normalizer, or retention implementation.

### 2. No automatic inheritance into M6

The M5 REGISTER raw-read-back pattern is not architectural precedent for M6 operational ingest, including the recurring post-publication read of a listing's state, status or price, the stock and sold-out synchronisation, and orders, inquiries, claims, cancellations, exchanges or returns.

A payload being product/listing scoped is not what earns the allowance in §1, so an M6 operational read never inherits it by resembling one.

Before any long-lived raw M6 payload persistence is introduced, M6 must have a separate reviewed lifecycle contract covering minimum necessary data, retention, masking/redaction, deletion, and evidence requirements.

No implementation may cite this ADR as authorization for indefinite immutable retention of raw M6 operational payloads.

### 3. Secret-bearing material is removed before the artifact exists

ADR-0010 §9 treats a signed, expiring or tokenized URL as credential-equivalent and keeps secret-bearing URL material out of durable evidence, logs and audit. A marketplace raw read-back can carry the same class of URL, so that rule carries forward to M5 read-back evidence unchanged.

Sanitation is part of producing the artifact, not a view over it: credential-equivalent secret-bearing URL material is removed **before** the bytes are hashed, content-addressed or persisted. An implementation may not persist raw signed or tokenized URL material and treat a later redacted view, a masked render or a restricted read path as sufficient — once such material is inside an immutable content-addressed artifact, removing it destroys the evidence's own identity.

This ADR does not define a marketplace safe-query-key profile. The M5 contract must state one before any read-back is retained.

### 4. Fail-closed default

A generic pattern such as:

```text
marketplace read-back
→ raw artifact
→ immutable content-addressed storage
```

must not be generalized from REGISTER into OPERATE without a new architecture review.

The default for M6 is therefore no long-lived immutable raw operational-payload CAS until an M6-specific contract says otherwise.

### 5. Scope

This ADR changes no current M3 behavior and authorizes no runtime or schema change by itself.

It does not define M4 policy/enrichment schemas, the final M5 registration-evidence contract, an M6 operational schema, a retention duration, or the future detail_guidance feature.

Separate later contracts remain responsible for:

- M4: policy foundation, canonical commerce-model ownership, immutable enrichment-revision envelope.
- M5: product/listing read-back evidence, capture roles, comparison states, and renderer/normalizer provenance.
- M6: operational-data retention, masking/redaction, and deletion policy.

## Consequences

- M5 can preserve strong REGISTER registration evidence without silently deciding M6 lifecycle policy.
- A recurring post-publication listing read stays an M6 lifecycle question, whatever its payload is scoped to.
- Content-addressed evidence cannot become the place a credential-equivalent URL is kept.
- M6 must make an explicit reviewed lifecycle decision instead of copying the nearest storage mechanism.
- Immutable CAS remains available for the narrow evidence class where it is justified.
- M3 remains unchanged.

## Acceptance criteria

Before ACCEPTED:

1. M3 scope is unchanged.
2. M5 raw read-back evidence remains allowed but is not fully specified here.
3. The allowance names REGISTER registration and reconciliation evidence as its purpose, and a recurring post-publication listing/status/price read-back in OPERATE is M6 operational ingest that does not inherit it merely by being product/listing scoped.
4. Credential-equivalent secret-bearing URL material is removed before an artifact is hashed, content-addressed or persisted; a later redacted view is not sufficient.
5. M6 cannot inherit long-lived immutable raw CAS without a separate reviewed contract.
6. No M6 retention duration or schema is invented here.
7. M4 and M5 decisions stay in separate later ADRs.
