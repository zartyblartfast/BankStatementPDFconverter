from contextlib import closing
import sqlite3
from unittest.mock import patch
from scripts import dashboard as d
from scripts.services import category_rules
from tests.test_dashboard_security import DashboardFixture


class RuleMaintenanceAPITests(DashboardFixture):
    def edit(self, **changes):
        return dict(key='SYNTHETIC', category='Fixture', subcategory='Test', enabled=True, **changes)

    def test_preview_confirm_create_history_and_replay_without_changing_ledger(self):
        before = self.paths.db_path.read_bytes()
        preview = self.post('/api/rules/preview', self.edit())
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json['matches'][0]['txn_id'], '1')
        self.assertIsNone(preview.json['old'])
        response = self.post('/api/rules/confirm', dict(preview_token=preview.json['preview_token'], approved=True))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['changed'])
        self.assertEqual(self.paths.db_path.read_bytes(), before)
        detail = self.client.get('/api/rules?key=SYNTHETIC', base_url=self.base)
        self.assertEqual(len(detail.json['history']), 1)
        self.assertNotIn('before_csv', str(detail.json))
        self.assertEqual(self.post('/api/rules/confirm', dict(preview_token=preview.json['preview_token'], approved=True)).status_code, 409)
        d._regenerate_report.assert_not_called()

    def test_create_remove_repeated_csv_remains_editable_and_restorable(self):
        from pathlib import Path
        from scripts import backup_ledger as backup
        path = self.paths.category_rules_path
        path.write_bytes(b'merchant_core,pattern,category,subcategory,notes,enabled,revision\r\nOTHER,,Fixture,Test,untouched,true,original\r\n')
        # An unrelated maintained key must survive every action and restoration.
        category_rules.change(path, category_rules.snapshot(path)['digest'], 'OTHER', ('Fixture', 'Test'), False)
        original = path.read_bytes()
        unrelated = category_rules.snapshot(path)['rules']['OTHER']
        stale = self.post('/api/rules/preview', self.edit())
        created = self.create()
        self.assertEqual(created.status_code, 200)
        removed = self.confirm(self.post('/api/rules/preview', dict(undo_id=created.json['event_id'])))
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(path.read_bytes(), original)
        detail = self.client.get('/api/rules?key=SYNTHETIC', base_url=self.base)
        self.assertEqual(detail.status_code, 200, detail.get_data(as_text=True))
        self.assertEqual(len(detail.json['history']), 2)
        self.assertIsNone(detail.json['rule'])
        self.assertEqual(self.confirm(stale).status_code, 409)
        self.assertEqual(self.post('/api/rules/preview', dict(undo_id=created.json['event_id'])).status_code, 409)
        edited = self.create()
        self.assertEqual(edited.status_code, 200)
        snapshot = backup.create_backup(root=self.root)
        backup.verify_backup(snapshot)
        restored = backup.restore_backup(snapshot, Path(self.temp.name) / 'repeated-restored', root=self.root)
        target = restored / 'config/category_rules.csv'
        self.assertEqual(category_rules.snapshot(target)['rules']['OTHER'], unrelated)
        self.assertEqual(category_rules.history(target)[0]['id'], edited.json['event_id'])
        with patch.object(d, 'RULES_CSV', target):
            self.assertEqual(self.client.get('/api/rules?key=SYNTHETIC', base_url=self.base).status_code, 200)
            undone = self.confirm(self.post('/api/rules/preview', dict(undo_id=edited.json['event_id'])))
            self.assertEqual(undone.status_code, 200)
            self.assertEqual(self.client.get('/api/rules?key=SYNTHETIC', base_url=self.base).status_code, 200)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(category_rules.snapshot(path)['rules']['OTHER'], unrelated)

    def test_rules_page_small_search_and_exception_review_links_are_available(self):
        self.create()
        response=self.client.get('/rules?q=SYN',base_url=self.base)
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('SYNTHETIC',html)
        self.assertIn('View details', html)
        response=self.client.get('/exceptions',base_url=self.base)
        self.assertEqual(response.status_code,200)
        self.assertIn('Optional rule review',response.get_data(as_text=True))

    def confirm(self, preview):
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        return self.post('/api/rules/confirm', dict(preview_token=preview.json['preview_token'], approved=True))

    def create(self):
        return self.confirm(self.post('/api/rules/preview', self.edit()))

    def test_disable_obsolete_destination_only_retains_existing_exact_pair(self):
        path = self.paths.category_rules_path
        path.write_bytes(b'merchant_core,pattern,category,subcategory,notes\r\nSYNTHETIC,,Removed,Old,keep\r\n')
        before = path.read_bytes()
        preview = self.post('/api/rules/preview',
            dict(key='SYNTHETIC', category='Removed', subcategory='Old', enabled=False))
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        for payload in (
                dict(key='SYNTHETIC', category='Removed', subcategory='Changed', enabled=False),
                dict(key='SYNTHETIC', category='Fixture', subcategory='Test', enabled=False),
                dict(key='MISSING', category='Removed', subcategory='Old', enabled=False)):
            self.assertEqual(self.post('/api/rules/preview', payload).status_code, 409)
            self.assertEqual(path.read_bytes(), before)
        disabled = self.confirm(self.post('/api/rules/preview',
            dict(key='SYNTHETIC', category='Removed', subcategory='Old', enabled=False)))
        self.assertEqual(disabled.status_code, 200)
        row = category_rules.snapshot(path)['rules']['SYNTHETIC']
        self.assertEqual((row['category'], row['subcategory'], row['notes']), ('Removed', 'Old', 'keep'))
        self.assertFalse(category_rules.is_enabled(row))
        self.assertNotIn('SYNTHETIC', category_rules.load_rules(path))
        self.assertEqual(self.post('/api/rules/preview',
            dict(key='SYNTHETIC', category='Removed', subcategory='Old', enabled=True)).status_code, 409)
        self.assertEqual(self.post('/api/rules/preview',
            dict(txn_id=1, category='Removed', subcategory='Old')).status_code, 409)
        valid = self.confirm(self.post('/api/rules/preview', self.edit()))
        self.assertEqual(valid.status_code, 200)
        self.assertIn('SYNTHETIC', category_rules.load_rules(path))

    def test_conflict_disable_noop_and_safe_undo(self):
        self.create()
        old = self.paths.category_rules_path.read_bytes()
        preview = self.post('/api/rules/preview', self.edit())
        self.assertTrue(preview.json['noop'])
        self.assertFalse(self.confirm(preview).json['changed'])
        self.assertEqual(self.paths.category_rules_path.read_bytes(), old)
        off = self.post('/api/rules/preview', dict(key='SYNTHETIC', category='Fixture', subcategory='Test', enabled=False))
        self.assertTrue(off.json['old']['enabled'])
        self.assertFalse(off.json['proposed']['enabled'])
        disabled = self.confirm(off)
        self.assertNotIn('SYNTHETIC', category_rules.load_rules(self.paths.category_rules_path))
        future = self.post('/api/rules/preview', dict(txn_id=1,category='Fixture',subcategory='Test'))
        self.assertEqual(future.status_code, 409)
        self.assertIn('disabled', future.json['error'])
        undo = self.post('/api/rules/preview', dict(undo_id=disabled.json['event_id']))
        self.assertEqual(self.confirm(undo).status_code, 200)
        self.assertIn('SYNTHETIC', category_rules.load_rules(self.paths.category_rules_path))
        self.assertEqual(self.post('/api/rules/preview', dict(undo_id=disabled.json['event_id'])).status_code, 409)

    def test_stale_rules_definitions_historical_scope_and_source_reject(self):
        for mode in ('rules', 'definitions', 'scope', 'source'):
            with self.subTest(mode=mode):
                preview = self.post('/api/rules/preview', dict(txn_id=1,category='Fixture',subcategory='Test'))
                self.assertEqual(preview.status_code, 200)
                if mode == 'rules':
                    self.paths.category_rules_path.write_bytes(self.paths.category_rules_path.read_bytes() + b'OTHER,,Fixture,Test,note\n')
                elif mode == 'definitions':
                    self.paths.categories_path.write_text('{"Fixture":["Test","Other"]}')
                else:
                    with closing(sqlite3.connect(self.paths.db_path)) as c, c:
                        c.execute("UPDATE transactions SET description_raw=?", ('synthetic changed' if mode == 'source' else 'synthetic',))
                        if mode == 'scope':
                            c.execute("UPDATE transactions SET txn_date='2026-01-02'")
                result = self.confirm(preview)
                self.assertEqual(result.status_code, 409)
                self.assertEqual(category_rules.load_rules(self.paths.category_rules_path).get('SYNTHETIC'), None)

    def test_security_ownership_cancel_validation_and_no_arbitrary_paths(self):
        preview = self.post('/api/rules/preview', self.edit())
        self.assertEqual(self.client.post('/api/rules/confirm', json=dict(preview_token=preview.json['preview_token'],approved=True),base_url=self.base).status_code,403)
        other = d.app.test_client()
        token = other.get('/api/csrf-token',base_url=self.base).json['csrf_token']
        self.assertEqual(other.post('/api/rules/confirm',json=dict(preview_token=preview.json['preview_token'],approved=True),base_url=self.base,headers={'X-CSRF-Token':token}).status_code,409)
        for route in ('/api/rules/preview', '/api/rules/confirm'):
            self.assertEqual(self.post(route, {}, Origin='https://foreign.example').status_code,403)
        self.assertEqual(self.client.get('/api/rules?key=SYNTHETIC',base_url='http://foreign.example').status_code,403)
        for payload in (None, {}, dict(self.edit(), path='C:/arbitrary.csv'),dict(txn_id=True,category='Fixture',subcategory='Test'),dict(txn_id=9223372036854775808,category='Fixture',subcategory='Test'),dict(key='synthetic',category='Fixture',subcategory='Test',enabled=True),dict(key='SYNTHETIC',category='Unknown',subcategory='X',enabled=True)):
            self.assertIn(self.post('/api/rules/preview',payload).status_code,(400,409))
        self.assertEqual(self.post('/api/rules/confirm',dict(preview_token=preview.json['preview_token'],approved=False)).status_code,400)
        self.assertEqual(category_rules.load_rules(self.paths.category_rules_path),{})

    def test_expired_preview_is_rejected_without_writes(self):
        from scripts.services import search_bulk
        preview=self.post('/api/rules/preview',self.edit())
        with patch.object(search_bulk.time,'monotonic',return_value=10**12):
            response=self.confirm(preview)
        self.assertEqual(response.status_code,409)
        self.assertEqual(category_rules.load_rules(self.paths.category_rules_path),{})

    def test_post_replace_failure_reports_unknown_not_false_rollback(self):
        preview=self.post('/api/rules/preview',self.edit())
        real_write=category_rules._atomic_write
        def publish_then_fail(path, raw):
            real_write(path,raw)
            if path == self.paths.category_rules_path:
                raise OSError('synthetic failure after commit point')
        with patch.object(category_rules,'_atomic_write',side_effect=publish_then_fail):
            result=self.confirm(preview)
        self.assertEqual(result.status_code,500)
        self.assertIn('outcome is unknown',result.json['error'])
        self.assertIn('SYNTHETIC',category_rules.load_rules(self.paths.category_rules_path))
        self.assertEqual(len(category_rules.history(self.paths.category_rules_path)),1)
        self.assertEqual(self.confirm(preview).status_code,409)

    def test_rule_destination_conflict_preview_requires_approval_and_preserves_history(self):
        self.create()
        self.paths.categories_path.write_text('{"Fixture":["Test","Other"]}')
        before=self.paths.category_rules_path.read_bytes()
        preview=self.post('/api/rules/preview',dict(txn_id=1,category='Fixture',subcategory='Other'))
        self.assertEqual(preview.status_code,200)
        self.assertEqual(preview.json['old']['subcategory'],'Test')
        self.assertEqual(preview.json['proposed']['subcategory'],'Other')
        self.assertEqual(self.paths.category_rules_path.read_bytes(),before)
        self.assertEqual(self.confirm(preview).status_code,200)
        self.assertEqual(category_rules.load_rules(self.paths.category_rules_path)['SYNTHETIC']['subcategory'],'Other')
        with closing(sqlite3.connect(self.paths.db_path)) as conn:
            self.assertIsNone(conn.execute('SELECT subcategory_id FROM transactions').fetchone()[0])

    def test_failed_rule_save_consumes_preview_and_retains_saved_category(self):
        saved = self.post('/api/search-recategorize',dict(txn_id=1,old_subcategory_id=None,category='Fixture',subcategory='Test'))
        self.assertTrue(saved.json['ok'])
        preview = self.post('/api/rules/preview',dict(txn_id=1,category='Fixture',subcategory='Test'))
        with patch.object(category_rules, '_atomic_write',side_effect=OSError('synthetic')):
            result = self.confirm(preview)
        self.assertEqual(result.status_code,500)
        self.assertIn('unknown',result.json['error'])
        self.assertEqual(self.confirm(preview).status_code,409)
        with closing(sqlite3.connect(self.paths.db_path)) as conn:
            self.assertIsNotNone(conn.execute('SELECT subcategory_id FROM transactions WHERE txn_id=1').fetchone()[0])
        self.assertEqual(category_rules.load_rules(self.paths.category_rules_path),{})
