from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch
from tests.test_dashboard_security import DashboardFixture
from scripts import dashboard as d
from scripts.backup_ledger import verify_backup


class SafetyTests(DashboardFixture):
    def test_restore_check_rejects_resolved_junction_backup_root_and_ancestors(self):
        import subprocess
        from scripts.backup_ledger import create_backup, restore_backup
        external = Path(self.temp.name) / 'external' / 'backups'
        external.mkdir(parents=True)
        link = self.paths.backup_dir
        if os.name == 'nt':
            result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(external)],
                                    capture_output=True, text=True)
            if result.returncode:
                self.skipTest('Native junction creation unavailable: ' + result.stderr)
            self.addCleanup(os.rmdir, link)
        else:
            link.symlink_to(external, target_is_directory=True)
            self.addCleanup(link.unlink)
        self.assertEqual(link.resolve(), external.resolve())
        snapshot = create_backup(root=self.root, name='junction-fixture')
        for location in [external, external / 'checks', external.parent, snapshot.resolve()]:
            with self.subTest(location=location), \
                    patch.dict(os.environ, {'BANKSTATEMENT_RESTORE_CHECK_DIR': str(location)}), \
                    patch('scripts.backup_ledger.restore_backup', wraps=restore_backup) as restore:
                response = self.client.post('/safety/restore-check', base_url=self.base,
                    data={'csrf_token': self.token(), 'approve_restore': 'yes'})
                self.assertEqual(response.status_code, 400)
                restore.assert_not_called()
                self.assertFalse((external / 'checks').exists())
                self.assertFalse((external / 'state.json').exists())
                self.assertEqual(sorted(p.name for p in external.iterdir()), [snapshot.name])
        # A sibling of the resolved backup root remains a legitimate isolated copy.
        isolated = external.parent / 'isolated-checks'
        with patch.dict(os.environ, {'BANKSTATEMENT_RESTORE_CHECK_DIR': str(isolated)}):
            response = self.client.post('/safety/restore-check', base_url=self.base,
                data={'csrf_token': self.token(), 'approve_restore': 'yes'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(isolated.iterdir()), [])
        state = json.loads((external / 'state.json').read_text())
        self.assertEqual(state['restore_check']['status'], 'passed')
        self.assertEqual(state['restore_check']['counts']['transactions'], 1)


    def test_safety_get_is_read_only_private_and_has_forms(self):
        before = hashlib.sha256(self.paths.db_path.read_bytes()).hexdigest()
        page = self.client.get('/safety', base_url=self.base)
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        for phrase in ['127.0.0.1', 'unencrypted', 'off-device', 'No migrations', 'csrf_token', 'href="/"', 'approve_restore']:
            self.assertIn(phrase, text)
        self.assertNotIn('SYNTHETIC', text)
        self.assertFalse(self.paths.backup_dir.exists())
        self.assertEqual(hashlib.sha256(self.paths.db_path.read_bytes()).hexdigest(), before)

    def test_backup_and_explicit_restore_check_preserve_live_data_and_clean_copy(self):
        original = self.paths.db_path.read_bytes()
        cfg = self.paths.categories_path.read_bytes()
        restore_parent = Path(self.temp.name) / 'isolated-checks'
        with patch.dict(os.environ, {'BANKSTATEMENT_RESTORE_CHECK_DIR': str(restore_parent)}):
            result = self.client.post('/safety/backup', base_url=self.base, data={'csrf_token': self.token()}, follow_redirects=True)
            self.assertEqual(result.status_code, 200)
            snapshots = [p for p in self.paths.backup_dir.iterdir() if p.is_dir()]
            self.assertEqual(len(snapshots), 1)
            manifest = verify_backup(snapshots[0])
            self.assertEqual(manifest['protection'], 'unencrypted local snapshot')
            result = self.client.post('/safety/restore-check', base_url=self.base, data={'csrf_token': self.token()})
            self.assertEqual(result.status_code, 400)
            self.assertFalse(restore_parent.exists())
            result = self.client.post('/safety/restore-check', base_url=self.base,
                data={'csrf_token': self.token(), 'approve_restore': 'yes'}, follow_redirects=True)
            self.assertEqual(result.status_code, 200)
            self.assertIn('Restore check passed', result.get_data(as_text=True))
            self.assertEqual(list(restore_parent.iterdir()), [])
            state = json.loads((self.paths.backup_dir / 'state.json').read_text())
            self.assertEqual(state['restore_check']['status'], 'passed')
            self.assertEqual(state['restore_check']['counts']['transactions'], 1)
            self.assertNotIn('SYNTHETIC', json.dumps(state))
        self.assertEqual(self.paths.db_path.read_bytes(), original)
        self.assertEqual(self.paths.categories_path.read_bytes(), cfg)

    def test_restore_selects_newest_manifest_not_lexical_name(self):
        from scripts.backup_ledger import create_backup
        older = create_backup(root=self.root, name='z-older')
        newer = create_backup(root=self.root, name='a-newer')
        isolated = Path(self.temp.name) / 'checks'
        with patch.dict(os.environ, {'BANKSTATEMENT_RESTORE_CHECK_DIR': str(isolated)}):
            result = self.client.post('/safety/restore-check', base_url=self.base,
                data={'csrf_token': self.token(), 'approve_restore': 'yes'})
            self.assertEqual(result.status_code, 302)
        state = json.loads((self.paths.backup_dir / 'state.json').read_text())
        self.assertEqual(state['restore_check']['snapshot'], newer.name)

    def test_mutation_waits_for_snapshot_lock(self):
        import threading
        done = threading.Event()
        started = threading.Event()
        errors = []
        # Hold snapshot's shared lock in this thread, then submit a real client mutation.
        def mutate():
            try:
                client = d.app.test_client()
                token = client.get('/api/csrf-token', base_url=self.base).json['csrf_token']
                started.set()
                response = client.post('/api/update-note', base_url=self.base, json={'txn_id': 1, 'note': 'after snapshot'}, headers={'X-CSRF-Token': token})
                self.assertEqual(response.status_code, 200)
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        with d.MUTATION_LOCK:
            worker = threading.Thread(target=mutate)
            worker.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(done.wait(.1))
            with closing(sqlite3.connect(self.paths.db_path)) as conn:
                self.assertIsNone(conn.execute('SELECT user_note FROM transactions WHERE txn_id=1').fetchone()[0])
        worker.join(2)
        self.assertTrue(done.is_set())
        self.assertEqual(errors, [])

    def test_safety_actions_guarded_and_restore_root_must_be_isolated(self):
        for route in ['/safety/backup', '/safety/restore-check']:
            self.assertEqual(self.client.post(route, base_url=self.base).status_code, 403)
        self.client.post('/safety/backup', base_url=self.base, data={'csrf_token': self.token()})
        with patch.dict(os.environ, {'BANKSTATEMENT_RESTORE_CHECK_DIR': str(self.root / 'unsafe')}):
            response = self.client.post('/safety/restore-check', base_url=self.base,
                data={'csrf_token': self.token(), 'approve_restore': 'yes'})
            self.assertEqual(response.status_code, 400)
            self.assertFalse((self.root / 'unsafe').exists())
