"""What the SmartStore adapter proves about its own endpoint contracts (M5 PR-F §C).

It reads the endpoint registry and nothing else: an endpoint is adopted when the registry adopted
it, and the gap of an unadopted one is the registry's own recorded reason. It judges nothing, and
it deliberately holds **no transport import at all** — the canary plan and the offline acceptance
run read adoption without pulling a provider client into the process.
"""

from collections.abc import Mapping

from integrations.marketplaces.smartstore.registry import ADOPTED, ADOPTION_GAPS, EndpointId


class SmartStoreAdoption:
    """The adapter's adoption facts, as `app.register.canary.AdoptionFacts` reads them."""

    def adoption(self) -> Mapping[str, bool]:
        return {endpoint.value: endpoint in ADOPTED for endpoint in EndpointId}

    def gaps(self) -> Mapping[str, str]:
        return {endpoint.value: gap for endpoint, gap in ADOPTION_GAPS.items()}


__all__ = ["SmartStoreAdoption"]
