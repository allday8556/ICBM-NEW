"""M2 SmartStore CONNECT acceptance harness (Issue #46 §2).

Acceptance tooling only: nothing in ``app/`` or ``integrations/`` imports it, and it changes no
product behavior. Every SmartStore request still goes through the real registry-gated
``SmartStoreEndpointCaller``; the harness only supplies that caller's transport (a durable budget
gate) and drives the unmodified application in child processes.

The default mode is DRY: a fake provider stands behind the real caller, and no SmartStore request
can be sent. REAL mode exists only behind the gates in ``gates.py`` and the campaign ledger's
approved-run state.
"""
