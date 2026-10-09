"""The SearchSignal port (ADR-0028 §3; Canonical v3.1 §7.8, §18).

External search evidence per keyword — volumes, trends — for the tag task. It is a port in the
ADR-0012 manner, and it is **provider-zero**: no source is selected. Each candidate source (for
example the NAVER 검색광고 keyword tool or the DataLab shopping insight) needs its own credentials
and terms, so each is adopted only by its own owner decision and evidence: an official API or
permitted data only, its credentials in the OS secret store, loopback or a granted egress host
(AIT-06).

Until then the port answers ``NOT_CONFIGURED`` with no signal, the ``search_signal`` capability is
``NOT_CONFIGURED`` and never fails core readiness, and the tag task runs without it: its input
records "no signals" as such, never as an empty source that answered.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from app.stages.connect.contracts import CapabilityReport
from app.stages.connect.state import CapabilityStatus

SEARCH_SIGNAL_CAPABILITY_KEY: Final = "search_signal"
NO_SEARCH_SIGNAL_SOURCE: Final = "no search-signal source is configured (ADR-0028 §3)"


@dataclass(frozen=True)
class SearchSignals:
    """What a source said about the keywords asked, or that no source answered."""

    # The adopted source's name, or ``None`` when none is configured.
    source: str | None
    # ``NOT_CONFIGURED`` or ``ANSWERED``.
    status: str
    # Keyword → the source's own measures for it (only when ``ANSWERED``).
    by_keyword: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def as_input(self) -> dict[str, object]:
        return {
            "source": self.source,
            "status": self.status,
            "by_keyword": {key: dict(value) for key, value in sorted(self.by_keyword.items())},
        }


class SearchSignalPort(Protocol):
    def signals(self, keywords: Sequence[str]) -> SearchSignals: ...

    def capability(self) -> CapabilityReport: ...


class NoSearchSignal:
    """The port with no source: nothing is asked, nothing is answered."""

    def signals(self, keywords: Sequence[str]) -> SearchSignals:
        return SearchSignals(source=None, status="NOT_CONFIGURED")

    def capability(self) -> CapabilityReport:
        return CapabilityReport(
            key=SEARCH_SIGNAL_CAPABILITY_KEY,
            status=CapabilityStatus.NOT_CONFIGURED,
            detail=NO_SEARCH_SIGNAL_SOURCE,
        )
