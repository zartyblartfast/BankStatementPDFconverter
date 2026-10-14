from contextlib import closing
import hashlib
import importlib
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'fixture.db'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE fixture (id INTEGER PRIMARY KEY)')
            conn.commit()
        self.migrations = self.root / 'migrations'
        self.migrations.mkdir()

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.migrate_db'), 'migration runner missing')
        return importlib.import_module('scripts.migrate_db')

    def test_second_run_is_idempotent(self):
        api = self.api()
        (self.migrations / '001_fixture.sql').write_text('CREATE TABLE added_fixture(id);')
        self.assertEqual(api.migrate_db(self.db, migrations_dir=self.migrations), [1])
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(api.migrate_db(self.db, migrations_dir=self.migrations), [])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_changed_or_missing_applied_migrations_are_rejected(self):
        api = self.api()
        migration = self.migrations / '001_fixture.sql'
        migration.write_text('CREATE TABLE added_fixture(id);')
        api.migrate_db(self.db, migrations_dir=self.migrations)
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        migration.write_text('CREATE TABLE altered_fixture(id);')
        with self.assertRaisesRegex(ValueError, 'changed'):
            api.migrate_db(self.db, migrations_dir=self.migrations)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        migration.rename(self.root / 'removed.sql')
        with self.assertRaisesRegex(ValueError, 'missing'):
            api.migrate_db(self.db, migrations_dir=self.migrations)

    def test_retroactive_version_rejected_before_any_pending_sql(self):
        api = self.api()
        (self.migrations / '002_second.sql').write_text('CREATE TABLE second_fixture(id);')
        self.assertEqual(api.migrate_db(self.db, migrations_dir=self.migrations), [2])
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with closing(sqlite3.connect(self.db)) as conn:
            tracking = conn.execute('SELECT * FROM schema_migrations').fetchall()
            state = list(conn.iterdump())
        (self.migrations / '001_retroactive.sql').write_text('INSERT INTO fixture VALUES (1);')
        (self.migrations / '003_pending.sql').write_text('CREATE TABLE third_fixture(id);')
        from unittest.mock import patch
        executed = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(executed.append)
            return conn

        with patch.object(sqlite3, 'connect', side_effect=traced_connect):
            with self.assertRaisesRegex(ValueError, 'retroactive'):
                api.migrate_db(self.db, migrations_dir=self.migrations)
        self.assertFalse(any('INSERT INTO fixture' in sql or 'CREATE TABLE third_fixture' in sql
                             for sql in executed), executed)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM schema_migrations').fetchall(), tracking)
            self.assertEqual(list(conn.iterdump()), state)

    def test_multi_statement_sql_handles_trigger_and_quoted_semicolons(self):
        api = self.api()
        (self.migrations / '001_multi.sql').write_text('''
            CREATE TABLE events(value TEXT);
            CREATE TRIGGER record_fixture AFTER INSERT ON fixture BEGIN
                INSERT INTO events VALUES ('fixture; inserted');
                INSERT INTO events VALUES ('another');
            END;
            INSERT INTO fixture VALUES (1);
            -- trailing comment
        ''')
        self.assertEqual(api.migrate_db(self.db, migrations_dir=self.migrations), [1])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT value FROM events').fetchall(),
                             [('fixture; inserted',), ('another',)])

    def test_migration_cannot_escape_runner_transaction(self):
        api = self.api()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        (self.migrations / '001_escape.sql').write_text(
            'CREATE TABLE leaked(id); COMMIT; INSERT INTO missing VALUES (1);')
        with self.assertRaisesRegex(ValueError, 'forbidden'):
            api.migrate_db(self.db, migrations_dir=self.migrations)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='leaked'").fetchone())
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone())

    def test_missing_database_is_not_created(self):
        api = self.api()
        missing = self.root / 'missing.db'
        with self.assertRaises(FileNotFoundError):
            api.migrate_db(missing, migrations_dir=self.migrations)
        self.assertFalse(missing.exists())

    def test_duplicate_versions_are_rejected_before_database_mutation(self):
        api = self.api()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        (self.migrations / '001_first.sql').write_text('CREATE TABLE first_fixture(id);')
        (self.migrations / '001_duplicate.sql').write_text('CREATE TABLE second_fixture(id);')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            api.migrate_db(self.db, migrations_dir=self.migrations)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_invalid_migration_filenames_are_rejected_before_mutation(self):
        api = self.api()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        for name in ('000_zero.sql', '1_short.sql', '001.sql', '001_bad-name.sql'):
            path = self.migrations / name
            path.write_text('CREATE TABLE added_fixture(id);')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'filename'):
                api.migrate_db(self.db, migrations_dir=self.migrations)
            self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
            path.rename(self.root / name)

    def test_migrations_cannot_change_tracking_or_connection_settings(self):
        api = self.api()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        migration = self.migrations / '001_forbidden.sql'
        for operation in ('PRAGMA user_version=9;', "ATTACH DATABASE ':memory:' AS extra;",
                          'DELETE FROM schema_migrations;', 'DROP TABLE schema_migrations;'):
            migration.write_text(operation)
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'forbidden'):
                api.migrate_db(self.db, migrations_dir=self.migrations)
            self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_missing_migrations_directory_is_not_silently_empty(self):
        api = self.api()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with self.assertRaises(FileNotFoundError):
            api.migrate_db(self.db, migrations_dir=self.root / 'missing')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_cli_requires_explicit_apply_and_database(self):
        command = [sys.executable, '-m', 'scripts.migrate_db', '--db', str(self.db),
                   '--migrations-dir', str(self.migrations)]
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        rejected = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        result = subprocess.run([*command, '--apply'], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], 0)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {'fixture', 'schema_migrations'})

    def test_failed_batch_rolls_back_schema_data_and_tracking_in_wal_database(self):
        api = self.api()
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('INSERT INTO fixture VALUES (99)')
        writer.commit()
        (self.migrations / '001_valid.sql').write_text('CREATE TABLE transient(id); INSERT INTO fixture VALUES (1);')
        (self.migrations / '002_invalid.sql').write_text('INSERT INTO missing_table VALUES (1);')
        with self.assertRaisesRegex(sqlite3.OperationalError, 'missing_table'):
            api.migrate_db(self.db, migrations_dir=self.migrations)
        self.assertEqual(writer.execute('SELECT id FROM fixture').fetchall(), [(99,)])
        tables = {row[0] for row in writer.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'fixture'})

    def test_importing_helpers_does_not_open_database(self):
        from unittest.mock import patch
        with patch.object(sqlite3, 'connect', side_effect=AssertionError('unexpected connection')):
            importlib.reload(self.api())
            importlib.reload(importlib.import_module('scripts.backup_ledger'))
            importlib.reload(importlib.import_module('scripts.settings'))

    def test_empty_scaffold_adds_only_tracking_to_explicit_fixture_database(self):
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO fixture VALUES (99)')
        self.assertEqual(api.migrate_db(self.db, migrations_dir=self.migrations), [])
        with closing(sqlite3.connect(self.db)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {'fixture', 'schema_migrations'})
            self.assertEqual(conn.execute('SELECT * FROM fixture').fetchall(), [(99,)])
            self.assertEqual(conn.execute('SELECT * FROM schema_migrations').fetchall(), [])

    def test_default_migration_chain_is_additive_to_legacy_ledger(self):
        from scripts.init_db import _SCHEMA_SQL

        api = self.api()
        legacy_db = self.root / 'legacy.db'
        with closing(sqlite3.connect(legacy_db)) as conn, conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.executescript(_SCHEMA_SQL)
            conn.execute("INSERT INTO categories VALUES (1, 'Synthetic')")
            conn.execute("INSERT INTO subcategories VALUES (1, 1, 'Manual')")
            conn.execute("""INSERT INTO transactions(
                txn_id, txn_date, description_raw, amount, subcategory_id,
                source_file, import_hash, excluded, exclude_reason, user_note)
                VALUES (42, '2026-01-02', 'SYNTHETIC', -1.25, 1,
                        'synthetic.pdf', 'legacy-hash', 1, 'synthetic reason', 'manual note')""")
            conn.execute("""INSERT INTO exceptions(txn_id, type, status, resolved_at)
                VALUES (42, 'uncategorised', 'resolved', '2026-01-03T00:00:00')""")
            conn.execute("""INSERT INTO edit_log(txn_id, field_changed, old_value, new_value)
                VALUES (42, 'subcategory_id', NULL, '1')""")
            conn.execute("INSERT INTO import_log(source_file, row_count) VALUES ('synthetic.pdf', 1)")
            legacy_schema = conn.execute(
                'SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name').fetchall()
            legacy_tables = {row[1] for row in legacy_schema if row[0] == 'table'}
            legacy_rows = {table: conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
                           for table in legacy_tables}

        self.assertEqual(api.migrate_db(legacy_db), [1, 2, 3, 4])
        metadata_tables = {'source_documents', 'source_document_aliases',
                           'source_document_contents', 'transaction_sources', 'import_batches',
                           'transfer_groups', 'transfer_members', 'transfer_audit'}
        migration = Path(__file__).resolve().parents[1] / 'scripts/migrations/001_import_metadata.sql'
        with closing(sqlite3.connect(legacy_db)) as conn:
            schema = conn.execute(
                'SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name').fetchall()
            self.assertEqual([row for row in schema if row[2] in legacy_tables], legacy_schema)
            self.assertEqual({row[1] for row in schema if row[0] == 'table'},
                             legacy_tables | metadata_tables | {'schema_migrations', 'accounts', 'account_statement_folders'})
            for table, rows in legacy_rows.items():
                with self.subTest(legacy_table=table):
                    self.assertEqual(conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall(), rows)
            for table in metadata_tables:
                with self.subTest(metadata_table=table):
                    self.assertEqual(conn.execute(f'SELECT * FROM "{table}"').fetchall(), [])
            self.assertEqual(conn.execute(
                'SELECT version, name, checksum FROM schema_migrations ORDER BY version').fetchall(),
                [(1, migration.name, hashlib.sha256(migration.read_bytes()).hexdigest()),
                 (2, '002_accounts.sql', hashlib.sha256(
                     migration.with_name('002_accounts.sql').read_bytes()).hexdigest()),
                 (3, '003_account_statement_folders.sql', hashlib.sha256(
                     migration.with_name('003_account_statement_folders.sql').read_bytes()).hexdigest()),
                 (4, '004_internal_transfers.sql', hashlib.sha256(
                     migration.with_name('004_internal_transfers.sql').read_bytes()).hexdigest())])
            self.assertEqual(conn.execute('SELECT * FROM account_statement_folders').fetchall(), [])
            self.assertEqual(conn.execute('SELECT account_id FROM accounts').fetchall(), [('HSBC_UK',)])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            migrated_state = list(conn.iterdump())
        before_repeat = hashlib.sha256(legacy_db.read_bytes()).hexdigest()
        self.assertEqual(api.migrate_db(legacy_db), [])
        self.assertEqual(hashlib.sha256(legacy_db.read_bytes()).hexdigest(), before_repeat)
        with closing(sqlite3.connect(legacy_db)) as conn:
            self.assertEqual(list(conn.iterdump()), migrated_state)

    def test_migrations_apply_in_version_order_and_record_checksums(self):
        api = self.api()
        first = b'CREATE TABLE first_fixture(id INTEGER PRIMARY KEY);\n'
        second = b'CREATE TABLE second_fixture(id INTEGER PRIMARY KEY);\n'
        (self.migrations / '002_second.sql').write_bytes(second)
        (self.migrations / '001_first.sql').write_bytes(first)
        self.assertEqual(api.migrate_db(self.db, migrations_dir=self.migrations), [1, 2])
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute('SELECT version, name, checksum FROM schema_migrations ORDER BY version').fetchall()
            self.assertEqual(rows, [(1, '001_first.sql', hashlib.sha256(first).hexdigest()),
                                    (2, '002_second.sql', hashlib.sha256(second).hexdigest())])
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name='second_fixture'").fetchone())
