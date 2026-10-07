from collections.abc import Callable


class OperateService:
    """OPERATE read model for the screens. Orders come from the M6-D order owner (ADR-0023 §5);
    inquiry sync is not part of M6, so there is nothing to count there yet."""

    def __init__(
        self,
        *,
        order_count: Callable[[], int] | None = None,
        order_capability: Callable[[], str] | None = None,
    ) -> None:
        self._order_count = order_count
        self._order_capability = order_capability

    def order_count(self) -> int:
        return 0 if self._order_count is None else self._order_count()

    def order_capability(self) -> str:
        """``CONNECTED`` once an order read has succeeded; until then no count is a zero."""
        return "NOT_CONNECTED" if self._order_capability is None else self._order_capability()

    def inquiry_count(self) -> int:
        return 0
