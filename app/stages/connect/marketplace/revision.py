"""Endpoint-mapping revision seam (M2 instructions §5.1).

Permission evidence is invalidated when the endpoint-to-permission mapping changes. The current
revision comes from the SmartStore endpoint registry in code (PR-A), never from parsing
documentation at runtime. PR-B owns only this domain-facing contract; PR-C consumes it and PR-A
supplies the one authoritative implementation. There is deliberately no default implementation,
so no temporary revision string can exist.
"""

from typing import Protocol


class EndpointMappingRevisionProvider(Protocol):
    def current_revision(self) -> str:
        """The mapping revision that current permission evidence must carry."""
        ...
