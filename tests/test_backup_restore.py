from contextlib import closing
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        (self.root / 'finance').mkdir(parents=True)
        (self.root / 'config').mkdir()
        self.db = self.root / 'finance' / 'ledger.db'
        self.writer = sqlite3.connect(self.db)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('PRAGMA wal_autocheckpoint=0')
        self.writer.execute('CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT)')
        self.writer.execute("INSERT INTO fixture VALUES (1, 'synthetic')")
        self.writer.commit()
        self.config = {'categories.json': b'{"Fixture":["Test"]}\n',
                       'category_rules.csv': b'merchant,category\nSYNTHETIC,Fixture\n',
                       'merchant_rules.json': b'{}\n'}
        for name, content in self.config.items():
            (self.root / 'config' / name).write_bytes(content)

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.backup_ledger'), 'backup helper missing')
        return importlib.import_module('scripts.backup_ledger')

    def test_verify_backup_checks_hashes_and_detects_config_tampering(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'verify_backup'), 'verification API missing')
        backup = api.create_backup(root=self.root)
        manifest = api.verify_backup(backup)
        self.assertEqual(manifest['version'], 1)
        (backup / 'config' / 'categories.json').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            api.verify_backup(backup)

    def test_verify_checks_sqlite_integrity_even_when_checksum_matches(self):
        api = self.api()
        backup = api.create_backup(root=self.root)
        ledger = backup / 'finance' / 'ledger.db'
        ledger.write_bytes(b'not a SQLite database')
        manifest_path = backup / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['finance/ledger.db'] = hashlib.sha256(ledger.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            api.verify_backup(backup)

    def test_foreign_key_invalid_snapshot_is_never_published(self):
        api = self.api()
        self.writer.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY); '
                                  'CREATE TABLE child(parent_id REFERENCES parent(id)); '
                                  'INSERT INTO child VALUES (99);')
        with self.assertRaisesRegex(ValueError, 'foreign-key'):
            api.create_backup(root=self.root, name='invalid')
        parent = self.root / 'finance' / 'backups'
        self.assertTrue(not parent.exists() or not list(parent.iterdir()))

    def test_unsafe_snapshot_names_are_rejected_without_writes(self):
        api = self.api()
        for name in ('', '.', '..', '../escape', 'nested/name', 'nested\\\\name',
                     str(Path(self.temp.name) / 'absolute-escape'), 'NUL', 'CON.txt',
                     'name.', 'name ', 'bad:name'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                api.create_backup(root=self.root, name=name)
        self.assertFalse((self.root / 'finance' / 'backups').exists())

    def test_verify_rejects_unsafe_or_incomplete_manifest(self):
        api = self.api()
        backup = api.create_backup(root=self.root)
        manifest_path = backup / 'manifest.json'
        original = json.loads(manifest_path.read_text())
        outside = backup.parent / 'outside.txt'
        outside.write_bytes(b'fixture only')
        unsafe = json.loads(json.dumps(original))
        unsafe['files']['../outside.txt'] = hashlib.sha256(outside.read_bytes()).hexdigest()
        incomplete = json.loads(json.dumps(original))
        del incomplete['files']['config/categories.json']
        for manifest in (unsafe, incomplete, {**original, 'version': 99},
                         {**original, 'files': []}, {**original, 'created_at': 'invalid'}):
            with self.subTest(manifest=manifest), self.assertRaisesRegex(ValueError, 'manifest'):
                manifest_path.write_text(json.dumps(manifest))
                api.verify_backup(backup)

    def test_existing_snapshot_destination_refused_before_source_access(self):
        api = self.api()
        parent = self.root / 'finance' / 'backups'
        destination = parent / 'existing'
        destination.mkdir(parents=True)
        marker = destination / 'keep.txt'
        marker.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            api.create_backup(root=self.root, name='existing', db_path=self.root / 'missing.db')
        self.assertEqual(marker.read_bytes(), b'keep')
        self.assertEqual(list(parent.iterdir()), [destination])

    def test_missing_source_database_never_creates_source_or_partial_backup(self):
        api = self.api()
        missing = self.root / 'missing.db'
        with self.assertRaises(FileNotFoundError):
            api.create_backup(root=self.root, db_path=missing)
        self.assertFalse(missing.exists())
        parent = self.root / 'finance' / 'backups'
        self.assertFalse(parent.exists())

    def test_corrupt_source_database_is_refused_without_partial_backup(self):
        api = self.api()
        corrupt = self.root / 'corrupt.db'
        for content in (b'', b'not SQLite'):
            corrupt.write_bytes(content)
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, 'integrity'):
                api.create_backup(root=self.root, db_path=corrupt)
            parent = self.root / 'finance' / 'backups'
            self.assertTrue(not parent.exists() or not list(parent.iterdir()))
            self.assertEqual(corrupt.read_bytes(), content)

    def test_restore_publishes_verified_snapshot_to_new_isolated_root(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'restore_backup'), 'restore API missing')
        backup = api.create_backup(root=self.root)
        destination = Path(self.temp.name) / 'restored'
        self.assertEqual(api.restore_backup(backup, destination, root=self.root), destination)
        self.assertEqual(api.verify_backup(destination), api.verify_backup(backup))
        for name, content in self.config.items():
            self.assertEqual((destination / 'config' / name).read_bytes(), content)
        with closing(sqlite3.connect(destination / 'finance' / 'ledger.db')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fixture').fetchone()[0], 1)

    def test_restore_refuses_live_tree_or_existing_destination_before_access(self):
        api = self.api()
        backup = api.create_backup(root=self.root)
        with self.assertRaisesRegex(ValueError, 'isolated'):
            api.restore_backup(backup, self.root / 'new', root=self.root)
        existing = Path(self.temp.name) / 'existing'
        existing.mkdir()
        with self.assertRaises(FileExistsError):
            api.restore_backup(self.root / 'missing', existing, root=self.root)
        self.assertEqual(list(existing.iterdir()), [])
        self.assertFalse((self.root / 'new').exists())

    def test_verify_refuses_unmanifested_files_including_wal_sidecars(self):
        api = self.api()
        backup = api.create_backup(root=self.root)
        extra = backup / 'finance' / 'ledger.db-wal'
        extra.write_bytes(b'untrusted')
        with self.assertRaisesRegex(ValueError, 'inventory'):
            api.verify_backup(backup)

    def test_snapshot_root_symlink_refused_by_verify_and_restore(self):
        api = self.api()
        backup = api.create_backup(root=self.root)
        link = Path(self.temp.name) / 'snapshot-link'
        destination = Path(self.temp.name) / 'linked-restore'
        try:
            link.symlink_to(backup, target_is_directory=True)
        except OSError:
            # Privilege fallback: simulate the link's detection/resolution only.
            original_symlink, original_resolve = Path.is_symlink, Path.resolve
            with patch.object(Path, 'is_symlink', lambda p: p == link or original_symlink(p)), \
                    patch.object(Path, 'resolve', lambda p, **kw: backup if p == link else original_resolve(p, **kw)):
                self._assert_root_link_refused(api, link, destination)
        else:
            self._assert_root_link_refused(api, link, destination)
            link.unlink()
        self.assertFalse(destination.exists())
        api.verify_backup(backup)

    def test_snapshot_root_junction_refused_by_verify_and_restore(self):
        api = self.api()
        backup = api.create_backup(root=self.root)
        link = Path(self.temp.name) / 'snapshot-junction'
        destination = Path(self.temp.name) / 'junction-restore'
        result = None
        if os.name == 'nt':
            result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(backup)],
                                    capture_output=True, text=True, check=False)
        if result is not None and result.returncode == 0:
            try:
                self.assertFalse(link.is_symlink())
                self.assertTrue(link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                self._assert_root_link_refused(api, link, destination)
            finally:
                link.rmdir()
        else:
            # Only emulate Windows lstat metadata if native junctions are unavailable.
            original_lstat = Path.lstat
            metadata = backup.lstat()
            with patch.object(Path, 'lstat', lambda p: SimpleNamespace(
                    st_mode=metadata.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    if p == backup else original_lstat(p)):
                self._assert_root_link_refused(api, backup, destination)
        self.assertFalse(destination.exists())
        api.verify_backup(backup)

    def test_verify_rejects_in_tree_reparse_member(self):
        api = self.api()
        backup = api.create_backup(root=self.root)
        member = backup / 'config'
        original_lstat = Path.lstat
        metadata = member.lstat()
        with patch.object(Path, 'lstat', lambda p: SimpleNamespace(
                st_mode=metadata.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                if p == member else original_lstat(p)):
            with self.assertRaisesRegex(ValueError, 'symlink|link|reparse'):
                api.verify_backup(backup)
        # Parent roots are not snapshot entries; do not over-reject Windows parents.
        parent_metadata = backup.parent.lstat()
        with patch.object(Path, 'lstat', lambda p: SimpleNamespace(
                st_mode=parent_metadata.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                if p == backup.parent else original_lstat(p)):
            api.verify_backup(backup)

    def _assert_root_link_refused(self, api, link, destination):
        for operation in ('verify', 'restore'):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'symlink|link|reparse'):
                if operation == 'verify':
                    api.verify_backup(link)
                else:
                    api.restore_backup(link, destination, root=self.root)

    def test_verify_rejects_symlinked_configuration(self):
        api = self.api()
        backup = api.create_backup(root=self.root)
        config = backup / 'config' / 'categories.json'
        moved = backup.parent / 'original-categories.json'
        config.rename(moved)
        try:
            config.symlink_to(moved)
        except OSError:
            # This host lacks symlink privilege: simulate only detection,
            # keeping the snapshot and all verification otherwise real.
            config.write_bytes(moved.read_bytes())
            original = Path.is_symlink
            with patch.object(Path, 'is_symlink', lambda p: p == config or original(p)):
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    api.verify_backup(backup)
            return
        with self.assertRaisesRegex(ValueError, 'symlink'):
            api.verify_backup(backup)

    def test_cli_create_verify_and_restore_use_only_explicit_fixture_paths(self):
        def run(*arguments):
            result = subprocess.run([sys.executable, '-m', 'scripts.backup_ledger', *arguments],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result

        run('create', '--root', str(self.root), '--name', 'cli-snapshot')
        backup = self.root / 'finance' / 'backups' / 'cli-snapshot'
        self.assertTrue(backup.is_dir(), 'CLI did not publish snapshot')
        self.assertIn('verified', run('verify', str(backup)).stdout)
        destination = Path(self.temp.name) / 'cli-restored'
        run('restore', str(backup), '--destination', str(destination), '--root', str(self.root))
        self.assertEqual(self.api().verify_backup(destination), self.api().verify_backup(backup))

    def test_copy_failure_cleans_partial_snapshot(self):
        api = self.api()
        (self.root / 'config' / 'categories.json').rename(self.root / 'saved-categories.json')
        with self.assertRaises(FileNotFoundError):
            api.create_backup(root=self.root, name='incomplete')
        self.assertEqual(list((self.root / 'finance' / 'backups').iterdir()), [])

    def test_restore_copy_failure_cleans_partial_directory(self):
        api = self.api()
        backup = api.create_backup(root=self.root)
        destination = Path(self.temp.name) / 'failed-restore'
        before = set(Path(self.temp.name).iterdir())
        # Fault injection for a mid-copy filesystem failure, not simulated SQLite.
        with patch.object(api.shutil, 'copyfile', side_effect=OSError('injected copy failure')):
            with self.assertRaises(OSError):
                api.restore_backup(backup, destination, root=self.root)
        self.assertFalse(destination.exists())
        self.assertEqual(set(Path(self.temp.name).iterdir()), before)

    def test_optional_merchant_config_and_statement_sources_are_not_required(self):
        api = self.api()
        (self.root / 'config' / 'merchant_rules.json').rename(self.root / 'saved-merchant.json')
        for directory, name in (('StatementsPDF', 'fixture.pdf'), ('TransactionsCSV', 'fixture.csv')):
            (self.root / directory).mkdir()
            (self.root / directory / name).write_bytes(b'synthetic source')
        backup = api.create_backup(root=self.root)
        manifest = api.verify_backup(backup)
        self.assertEqual(set(manifest['files']), {'finance/ledger.db', 'config/categories.json',
                                                'config/category_rules.csv'})
        self.assertFalse((backup / 'StatementsPDF').exists())
        self.assertFalse((backup / 'TransactionsCSV').exists())

    def test_tampered_restore_does_not_publish_destination(self):
        api = self.api()
        backup = api.create_backup(root=self.root)
        (backup / 'config' / 'category_rules.csv').write_bytes(b'tampered')
        destination = Path(self.temp.name) / 'tampered-restore'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            api.restore_backup(backup, destination, root=self.root)
        self.assertFalse(destination.exists())

    def test_backup_captures_uncheckpointed_wal_and_config_checksums(self):
        api = self.api()
        self.assertGreater(Path(str(self.db) + '-wal').stat().st_size, 0)
        source_hash = hashlib.sha256(self.db.read_bytes()).hexdigest()
        backup = api.create_backup(root=self.root, name='fixture-snapshot')
        self.assertEqual(backup, self.root / 'finance' / 'backups' / 'fixture-snapshot')
        with closing(sqlite3.connect(backup / 'finance' / 'ledger.db')) as conn:
            self.assertEqual(conn.execute('SELECT value FROM fixture').fetchall(), [('synthetic',)])
        manifest = json.loads((backup / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], 1)
        self.assertIn('created_at', manifest)
        self.assertEqual(manifest['protection'], 'unencrypted local snapshot')
        for name, content in self.config.items():
            relative = 'config/' + name
            self.assertEqual((backup / relative).read_bytes(), content)
            self.assertEqual(manifest['files'][relative], hashlib.sha256(content).hexdigest())
        self.assertEqual(set(manifest['files']), {'finance/ledger.db', *(f'config/{n}' for n in self.config)})
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), source_hash)
        self.assertEqual(self.writer.execute('SELECT COUNT(*) FROM fixture').fetchone()[0], 1)
