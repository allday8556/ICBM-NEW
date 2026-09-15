# ADR-0011 — Marketplace raw read-back retention boundary

Status: **PROPOSED**
Decision owner: Architect (ChatGPT)
Date: 2026-09-16
Related: Issue #58; ADR-0010

## Context

M5 will need durable product/listing read-back evidence for REGISTER idempotency, reconcile, and read-back. A long-lived immutable or content-addressed artifact can be appropriate for that narrow evidence class.

M6 later introduces operational ingest. That data has a different lifecycle and must not automatically inherit the M5 storage pattern. This ADR freezes that boundary before implementation can generalize the nearest existing mechanism by accident.

## Decision

### 1. Product/listing evidence only

Long-lived immutable/content-addressed raw read-back artifacts may be used for marketplace PRODUCT / LISTING registration-detail evidence when they are product/listing scoped and sanitized of credentials, session material, and unrelated private account data.

This ADR does not define the final M5 schema, capture kinds, normalizer, or retention implementation.

### 2. No automatic inheritance into M6

The M5 product/listing raw-read-back pattern is not architectural precedent for M6 operational ingest, including orders, inquiries, claims, cancellations, exchanges, or returns.

Before any long-lived raw M6 payload persistence is introduced, M6 must have a separate reviewed lifecycle contract covering minimum necessary data, retention, masking/redaction, deletion, and evidence requirements.

No implementation may cite this ADR as authorization for indefinite immutable retention of raw M6 operational payloads.

### 3. Fail-closed default

A generic pattern such as:

```text
marketplace read-back
→ raw artifact
→ immutable content-addressed storage
```

must not be generalized from REGISTER into OPERATE without a new architecture review.

The default for M6 is therefore no long-lived immutable raw operational-payload CAS until an M6-specific contract says otherwise.

### 4. Scope

This ADR changes no current M3 behavior and authorizes no runtime or schema change by itself.

It does not define M4 policy/enrichment schemas, the final M5 registration-evidence contract, an M6 operational schema, a retention duration, or the future detail_guidance feature.

Separate later contracts remain responsible for:

- M4: policy foundation, canonical commerce-model ownership, immutable enrichment-revision envelope.
- M5: product/listing read-back evidence, capture roles, comparison states, and renderer/normalizer provenance.
- M6: operational-data retention, masking/redaction, and deletion policy.

## Consequences

- M5 can preserve strong product/listing registration evidence without silently deciding M6 lifecycle policy.
- M6 must make an explicit reviewed lifecycle decision instead of copying the nearest storage mechanism.
- Immutable CAS remains available for the narrow evidence class where it is justified.
- M3 remains unchanged.

## Acceptance criteria

Before ACCEPTED:

1. M3 scope is unchanged.
2. M5 product/listing raw read-back evidence remains allowed but is not fully specified here.
3. M6 cannot inherit long-lived immutable raw CAS without a separate reviewed contract.
4. No M6 retention duration or schema is invented here.
5. M4 and M5 decisions stay in separate later ADRs.
