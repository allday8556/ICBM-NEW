class OperateService:
    """OPERATE owner. Order and inquiry sync arrive in M6 with a connected marketplace; with no
    marketplace account there is nothing to sync, so both collections are empty."""

    def order_count(self) -> int:
        return 0

    def inquiry_count(self) -> int:
        return 0
