"""Common supplier transport: the only place raw network and browser clients are used.

Suppliers never receive anything from this package except the immutable ``ProbeResponse`` their
predicates read. Pacing, the egress grant, request attribution and browser containment all live
here, so no supplier-specific code can bypass them (ADR-0007, repository rules).
"""
