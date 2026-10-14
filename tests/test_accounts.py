from contextlib import closing
import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from scripts.init_db import _SCHEMA_SQL
from scripts.migrate_db import migrate_db

MIGRATIONS = Path(__file__).resolve().parents[1] / 'scripts/migrations'


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'fixture.db'
        self.chain = self.root / 'migrations'
        self.chain.mkdir()
        shutil.copy(MIGRATIONS / '001_import_metadata.sql', self.chain)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(_SCHEMA_SQL)
        migrate_db(self.db, migrations_dir=self.chain)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO categories VALUES (1,'Synthetic')")
            conn.execute("INSERT INTO subcategories VALUES (1,1,'Manual')")
            for ident, account, currency in [(42, 'HSBC_UK', 'GBP'), (43, 'OTHER', 'USD')]:
                conn.execute('''INSERT INTO transactions(txn_id,txn_date,description_raw,amount,
                    currency,account_id,import_hash,subcategory_id,user_note,excluded)
                    VALUES (?, '2026-01-02','Synthetic',-1.25,?,?,?,1,'manual',1)''',
                             (ident, currency, account, str(ident)))
            conn.execute("INSERT INTO edit_log(txn_id,field_changed,new_value) VALUES (42,'subcategory_id','1')")
            conn.execute("INSERT INTO exceptions(txn_id,type,status) VALUES (42,'uncategorised','resolved')")
            for ident, account in [(1, 'HSBC_UK'), (2, 'DOC_ONLY')]:
                conn.execute('''INSERT INTO source_documents(document_id,account_id,document_key,
                    source_identity,content_hash,reconciliation_status) VALUES (?,?,?, ?,?,'passed')''',
                             (ident, account, str(ident), str(ident), str(ident)))
            conn.execute("INSERT INTO transaction_sources VALUES (42,1,'row-1',1)")
            conn.execute("INSERT INTO source_document_aliases VALUES ('HSBC_UK','alias',1)")

    def apply_accounts(self):
        migration = MIGRATIONS / '002_accounts.sql'
        self.assertTrue(migration.is_file(), 'account migration missing')
        shutil.copy(migration, self.chain)
        return migrate_db(self.db, migrations_dir=self.chain)

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.services.accounts'), 'account service missing')
        return importlib.import_module('scripts.services.accounts')

    def test_registry_reads_are_explicit_read_only_and_do_not_create_files(self):
        self.apply_accounts()
        api = self.api()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        inventory = sorted(p.name for p in self.root.iterdir())
        accounts = api.list_accounts(db_path=self.db)
        self.assertEqual([a['account_id'] for a in accounts], ['DOC_ONLY','HSBC_UK','OTHER'])
        self.assertTrue(api.get_account('HSBC_UK', db_path=self.db)['import_enabled'])
        self.assertFalse(api.get_account('OTHER', db_path=self.db)['parser_verified'])
        self.assertIsNone(api.get_account('MISSING', db_path=self.db))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(api.list_accounts(conn=conn), accounts)
            self.assertFalse(conn.in_transaction)
            self.assertEqual(api.get_account('OTHER', conn=conn)['currency'], 'USD')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), inventory)
        missing = self.root / 'absent' / 'ledger.db'
        with self.assertRaises(FileNotFoundError):
            api.list_accounts(db_path=missing)
        self.assertFalse(missing.parent.exists())
        for kwargs in ({}, {'conn': None}, {'db_path': self.db, 'conn': sqlite3.connect(':memory:')}):
            try:
                with self.assertRaises(ValueError):
                    api.list_accounts(**kwargs)
            finally:
                connection = kwargs.get('conn')
                if connection is not None:
                    connection.close()

    def test_missing_schema_signals_setup_pending_without_mutation(self):
        api = self.api()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertTrue(hasattr(api, 'AccountSchemaMissing'), 'explicit missing schema signal missing')
        with self.assertRaises(api.AccountSchemaMissing):
            api.list_accounts(db_path=self.db)
        records = api.list_accounts(db_path=self.db, allow_legacy=True)
        self.assertEqual([a['account_id'] for a in records], ['DOC_ONLY','HSBC_UK','OTHER'])
        for account in records:
            self.assertTrue(account['setup_pending'])
            self.assertFalse(account['import_enabled'])
            self.assertFalse(account['parser_verified'])
            self.assertIsNone(account['parser_key'])
            self.assertEqual(account['display_name'], 'Account setup pending')
        self.assertEqual(api.get_account('OTHER', db_path=self.db, allow_legacy=True)['currency'], 'USD')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        with closing(sqlite3.connect(':memory:')) as conn:
            self.assertEqual(api.list_accounts(conn=conn, allow_legacy=True), [])
            self.assertEqual(conn.execute('SELECT name FROM sqlite_master').fetchall(), [])

    def test_legacy_reads_reject_observed_invalid_ids_before_sorting_without_mutation(self):
        api = self.api()
        invalid = ('hsbc_uk', ' HSBC_UK', 'HSBC_UK ', '', 'A-B', 'A' * 65,
                   'A\x00B', None, 1, b'HSBC_UK')
        for table in ('transactions', 'source_documents'):
            for index, account_id in enumerate(invalid):
                with self.subTest(table=table, account_id=account_id):
                    db = self.root / f'{table}-{index}.db'
                    with closing(sqlite3.connect(db)) as conn, conn:
                        # No affinity/NOT NULL constraints: preserve malformed legacy values exactly.
                        conn.execute('CREATE TABLE transactions(account_id, currency)')
                        conn.execute('CREATE TABLE source_documents(account_id)')
                        conn.execute("INSERT INTO transactions VALUES ('HSBC_UK','GBP')")
                        if table == 'transactions':
                            conn.execute('INSERT INTO transactions VALUES (?,?)', (account_id, 'GBP'))
                        else:
                            conn.execute('INSERT INTO source_documents VALUES (?)', (account_id,))
                    before = hashlib.sha256(db.read_bytes()).hexdigest()
                    inventory = sorted(p.name for p in self.root.iterdir())
                    with closing(sqlite3.connect(db)) as conn:
                        conn.execute('BEGIN')
                        traced = []
                        conn.set_trace_callback(traced.append)
                        with self.assertRaisesRegex(ValueError, 'Invalid account_id'):
                            api.list_accounts(conn=conn, allow_legacy=True)
                        self.assertTrue(conn.in_transaction)
                        self.assertTrue(all(sql.startswith('SELECT') for sql in traced))
                        conn.rollback()
                    with self.assertRaisesRegex(ValueError, 'Invalid account_id'):
                        api.get_account('HSBC_UK', db_path=db, allow_legacy=True)
                    self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
                    self.assertEqual(sorted(p.name for p in self.root.iterdir()), inventory)

    def test_account_contract_rejects_malformed_ids_flags_dates_currency_and_lifecycle(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'validate_account'), 'account validation missing')
        record = dict(account_id='HSBC_UK', display_name='HSBC UK', currency='GBP',
                      institution='HSBC UK', owned=True, opened_on='2020-01-01',
                      closed_on=None, parser_key='hsbc_uk', parser_verified=True,
                      import_enabled=True)
        self.assertEqual(api.validate_account(record), record)
        invalid = [('account_id', v) for v in (' hsbc_uk', 'hsbc_uk', 'HSBC_UK ', '', 'A-B', 1, 'A'*65)]
        invalid += [(field, value) for field in ('owned','parser_verified','import_enabled')
                    for value in (0, 1, 'true', None)]
        invalid += [('currency', v) for v in ('gbp',' GBP','GBPP',12,'')]
        invalid += [('opened_on', v) for v in ('2020-1-01','2020-02-30','2020-01-01T00:00:00',1)]
        invalid += [('closed_on','2019-12-31'), ('closed_on','2021-01-01'),
                    ('parser_verified',False), ('parser_key',None), ('currency',None),
                    ('owned',False), ('parser_key','hsbc_iom'), ('display_name','  ')]
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                api.validate_account(dict(record, **{field: value}))
        disabled = dict(record, currency=None, owned=False, parser_key=None,
                        parser_verified=False, import_enabled=False, closed_on='2021-01-01')
        self.assertEqual(api.validate_account(disabled), disabled)
        for ident in (' HSBC_UK', 'hsbc_uk', None, 1):
            with self.subTest(ident=ident), self.assertRaises(ValueError):
                api.get_account(ident, db_path=self.root / 'missing.db')

    def test_verified_uk_parser_rejects_non_gbp_even_when_import_disabled(self):
        self.apply_accounts()
        api = self.api()
        record = api.get_account('HSBC_UK', db_path=self.db)
        for currency in (None, 'USD', 'EUR'):
            with self.subTest(currency=currency):
                with self.assertRaises(ValueError):
                    api.validate_account(dict(record, currency=currency, import_enabled=False))
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute("UPDATE accounts SET currency=?,import_enabled=0 WHERE account_id='HSBC_UK'",
                                 (currency,))
                before = hashlib.sha256(self.db.read_bytes()).hexdigest()
                with self.assertRaises(ValueError):
                    api.list_accounts(db_path=self.db)
                self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_registry_read_validates_stored_contract_and_marks_unverified_setup_pending(self):
        self.apply_accounts()
        api = self.api()
        self.assertTrue(api.get_account('OTHER', db_path=self.db).get('setup_pending'), 'unknown setup state missing')
        self.assertFalse(api.get_account('HSBC_UK', db_path=self.db)['setup_pending'])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE accounts SET opened_on='2020-02-30' WHERE account_id='OTHER'")
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            api.list_accounts(db_path=self.db)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_invalid_historical_id_rolls_back_registry_without_relabeling(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET account_id=' malformed '")
        before = self.state()
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply_accounts()
        self.assertEqual(self.state(), before)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT version FROM schema_migrations').fetchall(), [(1,)])

    def assert_blob_historical_id_rolls_back(self, table):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(f'UPDATE "{table}" SET account_id=?', (b'HSBC_UK',))
            self.assertEqual(conn.execute(f'SELECT DISTINCT typeof(account_id) FROM "{table}"').fetchall(),
                             [('blob',)])
            tracking_before = conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        before = self.state()
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply_accounts()
        self.assertEqual(self.state(), before)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall(),
                             tracking_before)
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE name='accounts'").fetchall(), [])

    def test_blob_transaction_account_id_rolls_back_registry_without_relabeling(self):
        self.assert_blob_historical_id_rolls_back('transactions')

    def test_blob_source_document_account_id_rolls_back_registry_without_relabeling(self):
        self.assert_blob_historical_id_rolls_back('source_documents')

    def test_mixed_or_invalid_currency_is_unresolved_and_import_disabled(self):
        for currency in ('USD', 'gbp', ' GBP', 'GBP\x00USD'):
            with self.subTest(currency=currency):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute("UPDATE transactions SET account_id='HSBC_UK', currency=? WHERE txn_id=43", (currency,))
                api = self.api()
                legacy = api.get_account('HSBC_UK', db_path=self.db, allow_legacy=True)
                self.assertIsNone(legacy['currency'])
                self.assertFalse(legacy['import_enabled'])
        self.apply_accounts()
        registered = api.get_account('HSBC_UK', db_path=self.db)
        self.assertIsNone(registered['currency'])
        self.assertFalse(registered['import_enabled'])
        self.assertTrue(registered['setup_pending'])

    def test_uk_parser_trust_requires_valid_unanimous_gbp_evidence(self):
        cases = [(), ('GBP', 'USD'), ('USD', 'USD'), ('gbp', 'gbp'),
                 ('GBP\x00USD', 'GBP\x00USD'), ('GBP', 'GBP')]
        shutil.copy(MIGRATIONS / '002_accounts.sql', self.chain)
        for index, currencies in enumerate(cases):
            with self.subTest(currencies=currencies):
                db = self.root / f'evidence-{index}.db'
                shutil.copy(self.db, db)
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("UPDATE transactions SET account_id='OTHER'")
                    for ident, currency in zip((42, 43), currencies):
                        conn.execute('UPDATE transactions SET account_id=?,currency=? WHERE txn_id=?',
                                     ('HSBC_UK', currency, ident))
                self.assertEqual(migrate_db(db, migrations_dir=self.chain), [2])
                trusted = currencies == ('GBP', 'GBP')
                with closing(sqlite3.connect(db)) as conn:
                    self.assertEqual(conn.execute('''SELECT parser_key,parser_verified,import_enabled
                        FROM accounts WHERE account_id='HSBC_UK' ''').fetchone(),
                        ('hsbc_uk' if trusted else None, int(trusted), int(trusted)))
                record = self.api().get_account('HSBC_UK', db_path=db)
                self.assertEqual(record['setup_pending'], not trusted)

    def test_blob_currency_evidence_remains_unresolved_and_readable_after_migration(self):
        cases = [(b'GBP', b'GBP'), (b'USD', b'USD'),
                 ('GBP', b'GBP'), (b'GBP\x00USD', b'GBP\x00USD')]
        shutil.copy(MIGRATIONS / '002_accounts.sql', self.chain)
        api = self.api()
        for index, currencies in enumerate(cases):
            with self.subTest(currencies=currencies):
                db = self.root / f'blob-currency-{index}.db'
                shutil.copy(self.db, db)
                with closing(sqlite3.connect(db)) as conn, conn:
                    for ident, currency in zip((42, 43), currencies):
                        conn.execute('UPDATE transactions SET account_id=?,currency=? WHERE txn_id=?',
                                     ('HSBC_UK', currency, ident))
                    evidence = conn.execute('SELECT currency,typeof(currency) FROM transactions ORDER BY txn_id').fetchall()
                    self.assertEqual(evidence, [(c, 'blob' if isinstance(c, bytes) else 'text')
                                                for c in currencies])
                for record in (api.get_account('HSBC_UK', db_path=db, allow_legacy=True),
                               next(a for a in api.list_accounts(db_path=db, allow_legacy=True)
                                    if a['account_id'] == 'HSBC_UK')):
                    self.assertIsNone(record['currency'])
                    self.assertIsNone(record['parser_key'])
                    self.assertFalse(record['parser_verified'])
                    self.assertFalse(record['import_enabled'])
                self.assertEqual(migrate_db(db, migrations_dir=self.chain), [2])
                with closing(sqlite3.connect(db)) as conn:
                    self.assertEqual(conn.execute('''SELECT currency,parser_key,parser_verified,import_enabled
                        FROM accounts WHERE account_id='HSBC_UK' ''').fetchone(), (None, None, 0, 0))
                    self.assertEqual(conn.execute('SELECT currency,typeof(currency) FROM transactions ORDER BY txn_id').fetchall(),
                                     evidence)
                for record in (api.get_account('HSBC_UK', db_path=db),
                               next(a for a in api.list_accounts(db_path=db)
                                    if a['account_id'] == 'HSBC_UK')):
                    self.assertIsNone(record['currency'])
                    self.assertIsNone(record['parser_key'])
                    self.assertFalse(record['parser_verified'])
                    self.assertFalse(record['import_enabled'])
                    self.assertTrue(record['setup_pending'])

    def test_importing_service_never_opens_database(self):
        from unittest.mock import patch
        with patch.object(sqlite3, 'connect', side_effect=AssertionError('unexpected DB open')):
            importlib.reload(self.api())

    def test_embedded_nul_currency_evidence_is_not_accepted_as_gbp(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET account_id='HSBC_UK',currency=?", ('GBP\x00USD',))
        self.apply_accounts()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT currency,import_enabled FROM accounts WHERE account_id=?',
                                          ('HSBC_UK',)).fetchone(), (None, 0))

    def test_iom_observation_never_inherits_uk_parser(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET account_id='HSBC_IOM',currency='GBP' WHERE txn_id=43")
        self.apply_accounts()
        record = self.api().get_account('HSBC_IOM', db_path=self.db)
        self.assertEqual(record['display_name'], 'Account setup pending')
        self.assertIsNone(record['institution'])
        self.assertIsNone(record['parser_key'])
        self.assertFalse(record['parser_verified'])
        self.assertFalse(record['import_enabled'])
        self.assertFalse(record['owned'])

    def test_reads_preserve_caller_transaction_and_observe_committed_wal(self):
        self.apply_accounts()
        api = self.api()
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("UPDATE accounts SET display_name='Synthetic review' WHERE account_id='OTHER'")
            writer.commit()
            inventory = {p.name for p in self.root.iterdir()}
            hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in self.root.iterdir() if p.is_file() and not p.name.endswith('-shm')}
            self.assertEqual(api.get_account('OTHER', db_path=self.db)['display_name'], 'Synthetic review')
            self.assertEqual({p.name for p in self.root.iterdir()}, inventory)
            self.assertEqual({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in self.root.iterdir() if p.is_file() and not p.name.endswith('-shm')}, hashes)
            writer.execute("UPDATE accounts SET display_name='Uncommitted' WHERE account_id='OTHER'")
            traced = []
            writer.set_trace_callback(traced.append)
            self.assertEqual(api.get_account('OTHER', conn=writer)['display_name'], 'Uncommitted')
            self.assertTrue(writer.in_transaction)
            self.assertTrue(all(sql.startswith('SELECT') for sql in traced))
            writer.rollback()
        self.assertEqual(api.get_account('OTHER', db_path=self.db)['display_name'], 'Synthetic review')

    def state(self):
        with closing(sqlite3.connect(self.db)) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'schema_migrations'")]
            return {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}

    def test_migration_preserves_every_legacy_row_and_registers_observed_accounts(self):
        before = self.state()
        self.assertEqual(self.apply_accounts(), [2])
        after = self.state()
        for table, rows in before.items():
            self.assertEqual(after[table], rows, table)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('''SELECT account_id,display_name,currency,institution,
                owned,parser_key,parser_verified,import_enabled FROM accounts ORDER BY account_id''').fetchall(),
                [('DOC_ONLY','Account setup pending',None,None,0,None,0,0),
                 ('HSBC_UK','HSBC UK','GBP','HSBC UK',1,'hsbc_uk',1,1),
                 ('OTHER','Account setup pending','USD',None,0,None,0,0)])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(migrate_db(self.db, migrations_dir=self.chain), [])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)
