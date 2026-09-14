"""The layout of one campaign directory. It lies outside the repository and every ordinary ICBM
data directory (``gates.dedicated_problems``); both data directories live inside it."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CampaignPaths:
    root: Path

    @property
    def ledger(self) -> Path:
        return self.root / "ledger.sqlite3"

    @property
    def marker(self) -> Path:
        return self.root / "campaign.json"

    def data_dir(self, role: str) -> Path:
        return self.root / "data" / role

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def evidence(self) -> Path:
        return self.evidence_dir / "m2-campaign-evidence.json"

    @property
    def run_dir(self) -> Path:
        return self.root / "run"

    @property
    def preflight(self) -> Path:
        return self.root / "preflight" / "status.json"

    @property
    def approval(self) -> Path:
        return self.root / "approval.json"

    @property
    def regression_dir(self) -> Path:
        return self.root / "regression"

    # DRY campaigns only: the fixture scenario and the fixture secret store.
    @property
    def scenario(self) -> Path:
        return self.root / "dry-scenario.json"

    @property
    def dry_keyring(self) -> Path:
        return self.root / "dry-keyring.json"

    def marker_file(self, crash_attempt: int) -> Path:
        return self.run_dir / f"crash-boundary-{crash_attempt}.json"
