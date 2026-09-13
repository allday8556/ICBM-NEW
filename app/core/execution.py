"""Global external-write execution mode (CLAUDE.md §7.1, ARCHITECTURE.md §13)."""

from enum import StrEnum


class ExecutionMode(StrEnum):
    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"
