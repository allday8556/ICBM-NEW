"""AI provider adapters (ADR-0012 §1, ADR-0027).

Only an adopted adapter lives here: the CLIProxyAPI sidecar adapter and the probe that reads the
process actually serving its endpoint. COLLECT never imports this package (ADR-0010 §13).
"""
