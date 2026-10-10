"""The only C-AUTH-1 transport: an in-memory fake with no socket capability."""

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from integrations.marketplaces.coupang.auth import COUPANG_BASE_URL, RequestTarget, SignedHeaders


@dataclass(frozen=True)
class FakeRequest:
    target: RequestTarget
    headers: SignedHeaders = field(repr=False)
    body: bytes = field(default=b"", repr=False)

    @property
    def url(self) -> str:
        suffix = f"?{self.target.query}" if self.target.query else ""
        return f"{COUPANG_BASE_URL}{self.target.path}{suffix}"


@dataclass(frozen=True)
class FakeResponse:
    status_code: int
    body: Any = None


class FakeTransport:
    """Records prepared requests and returns explicitly queued local responses."""

    def __init__(self, responses: tuple[FakeResponse, ...] = ()) -> None:
        self._responses = deque(responses)
        self.requests: list[FakeRequest] = []

    def send(self, request: FakeRequest) -> FakeResponse:
        if not self._responses:
            raise RuntimeError("fake Coupang transport has no queued response")
        self.requests.append(request)
        return self._responses.popleft()
