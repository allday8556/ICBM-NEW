<!-- Moved verbatim from CLAUDE.md §5 (Architectural rules) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §5` references resolve here (PATH_MIGRATION_MAP). -->

## 5. Architectural rules

### 5.1 Direction of dependency

```text
Approved standalone HTML UI
    ↓
NEW application contract
    ↓
NEW service/domain owner
    ↓
NEW adapter/integration
    ↓
external system + read-back
    ↓
NEW canonical DB state
    ↓
UI state
```

- Business rules live in services/contracts, never duplicated in the UI.
- The UI displays server-owned state. It does not re-decide pricing, readiness, compliance, or stock.
- The canonical ICBM product ID is the spine. Collection, registration, orders, stock, inquiries, analytics, and fulfillment resolve back to it.
- No downstream screen owns a second copy of product truth.

### 5.2 Canonical product spine

```text
CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE
```

Fulfillment is inside OPERATE. AI, OCR, learning, pricing, compliance, jobs, audit, and analytics are supporting capabilities, not parallel top-level systems.

### 5.3 Adapter rule

Core product logic stays platform-neutral. A supplier or marketplace implements the approved adapter contract.

Site-specific extraction belongs inside that supplier adapter/profile. Marketplace payload specifics belong inside that marketplace adapter.

**A new adapter may not change canonical Product / Pricing / Operation contracts merely to accommodate one site.** Contract changes require architecture review first.

### 5.4 Facts vs enrichment

Raw source facts and evidence stay separate from AI enrichment.

AI may validate or propose enrichment, but it never fabricates source facts. Ambiguous evidence becomes `REVIEW_REQUIRED`, never a confident guess.
