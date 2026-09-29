# ADR-0004 — Registration automation guardrails

Status: **ACCEPTED**
Decision owner: Architect (ChatGPT)
Date: 2026-09-13

## Context

The approved v29 visual prototype shows registration automation controls that predate the clean architecture. Without a binding interpretation, their labels could be implemented as authority to retry unknown CREATE results, finalize AI category guesses, or silently push supplier-price changes to live marketplaces.

That would conflict with the canonical RegistrationAttempt, CategoryMapping, ComplianceGate and source-drift rules.

## Decision

### Registration failure auto-retry

Automatic retry is permitted only for errors classified as:

- `TRANSIENT`
- `RATE_LIMITED`

It is not a blind retry mechanism for:

- `UNKNOWN` — reconcile marketplace state first
- `VALIDATION` — requires corrected input
- `POLICY_BLOCKED` — requires policy/evidence resolution
- destructive/write outcomes whose result cannot be proven

### Category auto-matching

AI may rank category candidates. Automatic application is permitted only when an accepted, persisted `CategoryMapping` exists under the current marketplace category-tree version and remains valid.

AI candidate ranking by itself never finalizes a category.

### Automatic price adjustment

Source-price drift may automatically trigger recalculation and a price proposal. It does not by itself authorize a live marketplace price update.

A live update requires the later REGISTER/OPERATE update policy, execution-mode checks, audit coverage, and any user approval required by that policy.

### M0 treatment

During M0 all of these controls are visual shell only. They make no supplier or marketplace calls and perform no external writes.

## Consequences

The v29 labels can remain as visual reference without granting unsafe runtime semantics. M5 implementation must bind the controls to these guardrails explicitly.
