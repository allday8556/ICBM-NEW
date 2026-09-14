"""Naver SmartStore adapter, M2 CONNECT (docs/platforms/smartstore/, M2 PR-A).

- ``registry``: the one provider endpoint contract (host, base URL, methods, paths, timeouts,
  redirect policy, success predicates) and the endpoint-mapping revision.
- ``signing``: the canonical token request builder.
- ``transmission`` / ``classify``: pure evidence rules.
- ``caller``: the only code that owns an HTTP client for SmartStore.
"""
