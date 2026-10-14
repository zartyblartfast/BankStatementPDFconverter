import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.settings import get_settings


class WebPathsTests(unittest.TestCase):
    def test_shared_web_paths_are_side_effect_free(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'absent'
            paths = get_settings(root)
            for attr, relative in {'statements_dir': 'StatementsPDF', 'transactions_csv_dir': 'TransactionsCSV',
                                   'exports_dir': 'finance/exports', 'reports_dir': 'finance/reports',
                                   'templates_dir': 'templates', 'static_dir': 'static'}.items():
                self.assertEqual(getattr(paths, attr, None), root / relative)
            self.assertFalse(root.exists())

    def test_consumers_use_environment_root_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'absent'
            code = '''from pathlib import Path
from scripts import dashboard, ingest_pipeline, import_to_ledger, ledger_query
from scripts import report_income_vs_expense, report_income_by_category, report_expenditure_by_category, report_expenditure_heatmap
from scripts.settings import get_settings
s=get_settings()
assert dashboard.PROJECT_ROOT == s.root
assert dashboard.REPORTS_DIR == s.reports_dir
assert ingest_pipeline.EXPORTS_DIR == s.exports_dir
assert import_to_ledger.CATEGORIES_JSON == s.categories_path
assert ledger_query.DB_PATH == s.db_path
for m in [report_income_vs_expense, report_income_by_category, report_expenditure_by_category, report_expenditure_heatmap]:
 assert m.REPORTS_DIR == s.reports_dir
assert not s.root.exists()
'''
            result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'BANKSTATEMENT_PROJECT_ROOT': str(root)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
