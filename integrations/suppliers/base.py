from typing import Protocol


class SupplierAdapter(Protocol):
    """Boundary every supplier integration implements (ROADMAP §9).

    Site-specific extraction lives inside the adapter/profile, never in global product logic
    (CLAUDE.md §5.3). M0 declares identity only; the CONNECT operations and their contract are
    defined with the first implementation (K홀세일, M1).
    """

    @property
    def supplier_key(self) -> str: ...

    @property
    def base_url(self) -> str: ...

    @property
    def auth_required(self) -> bool: ...
