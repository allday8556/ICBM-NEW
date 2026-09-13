# ADR-0003 — UI reproduction strategy

Status: **ACCEPTED**
Decision owner: Architect (ChatGPT)
Date: 2026-09-13

## Context

The approved v29 prototype is a single-file HTML/CSS/JS artifact accumulated through many visual iterations. It is the visual/product UI Source of Truth, but its internal CSS contains many one-off font sizes, paddings, radii and legacy prototype-only helper generations.

Literal code/value transplantation would preserve the patch chain rather than the intended UI and would contradict the clean-rebuild rule.

## Decision

ICBM-NEW reproduces v29 **structurally and visually, not literally**.

M0 preserves:

- page hierarchy and top-level IA
- visual composition and information density
- navigation and interaction shape
- responsive behaviour
- modals/drawers/empty states
- status semantics
- help-tooltip behaviour
- accessibility improvements
- platform-identity presentation

M0 may normalize implementation values into a small design-token layer such as `tokens.css` for:

- typography
- spacing
- radii
- surfaces/borders/shadows
- common controls
- status colours

Token consolidation is acceptable only when the resulting screen remains materially faithful to the approved v29 visual result.

## Explicit non-inheritance

Do not port the prototype's accumulated help-system generations, demo-state owners, patch-specific CSS/JS owners, or business logic. Implement one clean owner per UI concern.

Prototype JavaScript is interaction reference, not runtime truth.

## Verification

M0 acceptance must include visual checks for all ten top-level screens at representative landscape and portrait widths. Meaningful deviations from v29 must be documented in `docs/acceptance/M0.md`.

## Consequences

- The clean implementation can simplify the prototype's internal patch history.
- Visual fidelity remains testable against one approved artifact.
- Future UI revisions update the Source of Truth without forcing preservation of historical CSS accidents.
