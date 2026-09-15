"""The layout of one reconnaissance campaign directory. For a REAL campaign it lies outside the
repository and every ordinary ICBM data directory; the recon data directory lives inside it."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReconPaths:
    root: Path

    @property
    def ledger(self) -> Path:
        return self.root / "ledger.sqlite3"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def captures(self) -> Path:
        # Encrypted raw bodies; never committed, never copied into findings.
        return self.root / "captures"

    @property
    def findings(self) -> Path:
        # Sanitized findings only: what may be reviewed on GitHub.
        return self.root / "findings"

    @property
    def preflight(self) -> Path:
        return self.root / "preflight.json"
