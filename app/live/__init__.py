"""Gate 3 area 1 (ADR-0018 §3, §4, §10, §12): the pre-LIVE safety owners, provider-zero.

- ``model``: the stages, states, codes and the ASSET replay-conflict key (§3.4), pure.
- ``models`` / ``store``: the durable LIVE grant, the protected-write brake and the ASSET
  upload-attempt owner, and the only writer of their tables.
- ``stack``: the send-time safety stack (§4.3) and the derived stage readiness (§10).
- ``assets``: the ASSET upload path, the only way an upload may ever be handed to a sender.

Nothing here reaches a provider. The execution policy stays ``M0_DRY_RUN_ONLY``: the stack's first
layer refuses every mutation at this main, whatever else is recorded.
"""
