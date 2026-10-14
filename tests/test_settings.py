import importlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class SettingsTests(unittest.TestCase):
    def test_paths_use_project_root_or_explicit_isolated_root(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.settings'), 'settings module missing')
        settings = importlib.import_module('scripts.settings')
        with patch.dict(os.environ, {}, clear=True):
            default = settings.get_settings()
            self.assertEqual(default.root, Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as temp:
            paths = settings.get_settings(temp)
            self.assertEqual(paths.root, Path(temp).resolve())
            self.assertEqual(paths.db_path, paths.root / 'finance' / 'ledger.db')
            self.assertEqual(paths.backup_dir, paths.root / 'finance' / 'backups')
            self.assertEqual(paths.config_dir, paths.root / 'config')
            self.assertEqual(paths.categories_path, paths.config_dir / 'categories.json')
            self.assertEqual(paths.category_rules_path, paths.config_dir / 'category_rules.csv')
            self.assertEqual(paths.merchant_rules_path, paths.config_dir / 'merchant_rules.json')
            self.assertFalse(paths.db_path.exists())

    def test_environment_root_can_be_overridden_explicitly(self):
        from scripts.settings import get_settings
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with patch.dict(os.environ, {'BANKSTATEMENT_PROJECT_ROOT': first}):
                self.assertEqual(get_settings().root, Path(first).resolve())
                self.assertEqual(get_settings(second).root, Path(second).resolve())

    def test_init_db_closes_connection_when_schema_execution_fails(self):
        from contextlib import closing
        import sqlite3
        api = importlib.import_module('scripts.init_db')
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'fixture.db'
            # Hold a real connection so garbage collection cannot hide a leak.
            with closing(sqlite3.connect(target)) as conn:
                with patch.object(api.sqlite3, 'connect', return_value=conn), \
                        patch.object(api, '_SCHEMA_SQL', 'INVALID SCHEMA SQL;'):
                    with self.assertRaises(sqlite3.OperationalError):
                        api.init_db(db_path=target)
                with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
                    conn.execute('SELECT 1')

    def test_init_db_accepts_explicit_isolated_database_path(self):
        from scripts.init_db import init_db
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'finance' / 'ledger.db'
            self.assertIn('db_path', __import__('inspect').signature(init_db).parameters)
            self.assertEqual(init_db(db_path=target), target)
            self.assertTrue(target.is_file())
