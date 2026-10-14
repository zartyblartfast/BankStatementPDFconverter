"""Side-effect-free project path configuration for local safety helpers."""
from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def statements_dir(self) -> Path:
        return self.root / 'StatementsPDF'

    @property
    def transactions_csv_dir(self) -> Path:
        return self.root / 'TransactionsCSV'

    @property
    def exports_dir(self) -> Path:
        return self.root / 'finance' / 'exports'

    @property
    def reports_dir(self) -> Path:
        return self.root / 'finance' / 'reports'

    @property
    def templates_dir(self) -> Path:
        return self.root / 'templates'

    @property
    def static_dir(self) -> Path:
        return self.root / 'static'

    @property
    def restore_check_dir(self) -> Path:
        """Trusted server setting, never a path supplied by a browser request."""
        default = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share')) / 'BankStatementPDFconverter' / 'restore_checks'
        return Path(os.environ.get('BANKSTATEMENT_RESTORE_CHECK_DIR', default)).expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.root / 'finance' / 'ledger.db'

    @property
    def backup_dir(self) -> Path:
        return self.root / 'finance' / 'backups'

    @property
    def config_dir(self) -> Path:
        return self.root / 'config'

    @property
    def categories_path(self) -> Path:
        return self.config_dir / 'categories.json'

    @property
    def category_rules_path(self) -> Path:
        return self.config_dir / 'category_rules.csv'

    @property
    def merchant_rules_path(self) -> Path:
        return self.config_dir / 'merchant_rules.json'


def get_settings(root: str | Path | None = None) -> ProjectPaths:
    """Explicit root wins; otherwise use the environment or this project root.

    No directories, connections or configuration files are created here.
    """
    return ProjectPaths(Path(root if root is not None else os.environ.get(
        'BANKSTATEMENT_PROJECT_ROOT', Path(__file__).resolve().parents[1]
    )).expanduser().resolve())
