"""Rule recovery lifecycle uses synthetic projects only."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from tests import test_backup_restore as fixtures
from scripts import backup_ledger as backup
from scripts.services import category_rules as rules


class RuleBackupTests(fixtures.BackupTests):
    def edited(self):
        path = self.root / 'config/category_rules.csv'
        path.write_text('merchant_core,category,subcategory\nSHOP,Old,Old\n')
        event = rules.change(path, rules.snapshot(path)['digest'], 'SHOP', ('New', 'New'), True)
        return path, event['event_id']

    def test_edit_backup_verify_restore_inspect_and_guarded_undo(self):
        path, event_id = self.edited()
        snapshot = backup.create_backup(root=self.root)
        manifest = backup.verify_backup(snapshot)
        member = f'config/.category_rules.csv.history/{event_id}.json'
        self.assertIn(member, manifest['files'])
        self.assertFalse(any(p.endswith(('.lock', '.tmp')) for p in manifest['files']))
        restored = backup.restore_backup(snapshot, Path(self.temp.name) / 'rule-restored', root=self.root)
        target = restored / 'config/category_rules.csv'
        self.assertEqual(rules.history(target)[0]['id'], event_id)
        rules.change(target, rules.snapshot(target)['digest'], 'SHOP', ('Old', 'Old'), True, undo_id=event_id)
        self.assertEqual(rules.load_rules(target)['SHOP']['category'], 'Old')
        with self.assertRaises(ValueError):
            rules.change(target, rules.snapshot(target)['digest'], 'SHOP', ('Old', 'Old'), True, undo_id=event_id)
        self.assertEqual(rules.load_rules(path)['SHOP']['category'], 'New')

    def test_transient_staging_excluded_and_legacy_snapshot_without_history_restores(self):
        path, event = self.edited()
        directory = path.parent / '.category_rules.csv.history'
        (path.parent / ('.category_rules.csv.' + 'a' * 32 + '.tmp')).write_text('private staging')
        (directory / ('.' + event + '.json.' + 'b' * 32 + '.tmp')).write_text('private staging')
        snapshot = backup.create_backup(root=self.root)
        self.assertFalse(any(name.endswith(('.tmp', '.lock')) for name in backup.verify_backup(snapshot)['files']))
        # Old snapshots have no history inventory at all, even with new CSV columns.
        import shutil
        shutil.rmtree(snapshot / 'config/.category_rules.csv.history')
        manifest_path = snapshot / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files'] = {name: digest for name, digest in manifest['files'].items() if '.history/' not in name}
        manifest_path.write_text(json.dumps(manifest))
        destination = backup.restore_backup(snapshot, Path(self.temp.name) / 'legacy-restored', root=self.root)
        self.assertEqual(rules.history(destination / 'config/category_rules.csv'), [])
        self.assertEqual(rules.load_rules(destination / 'config/category_rules.csv')['SHOP']['category'], 'New')

    def test_checksummed_malformed_history_rejected_and_missing_member_refused(self):
        _, event = self.edited()
        snapshot = backup.create_backup(root=self.root)
        member = snapshot / 'config/.category_rules.csv.history' / (event + '.json')
        member.write_text('{}')
        manifest_path = snapshot / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files'][member.relative_to(snapshot).as_posix()] = hashlib.sha256(member.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'history|recovery'):
            backup.verify_backup(snapshot)
        member.unlink()
        with self.assertRaisesRegex(ValueError, 'inventory'):
            backup.verify_backup(snapshot)

    def test_publication_state_is_checksummed_and_semantically_validated(self):
        self.edited()
        snapshot = backup.create_backup(root=self.root)
        name = 'config/.category_rules.csv.history/state'
        manifest_path = snapshot / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        self.assertIn(name, manifest['files'])
        member = snapshot / name
        member.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            backup.verify_backup(snapshot)
        manifest['files'][name] = hashlib.sha256(member.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'publication state'):
            backup.verify_backup(snapshot)
        member.unlink()
        del manifest['files'][name]
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'missing publication state'):
            backup.verify_backup(snapshot)

    def test_busy_rule_writer_explicitly_refuses_snapshot(self):
        path, _ = self.edited()
        with rules._write_lock(path), self.assertRaisesRegex(ValueError, 'edited|lock'):
            backup.create_backup(root=self.root)

    def test_damaged_history_is_not_published(self):
        path, event = self.edited()
        (path.parent / '.category_rules.csv.history' / (event + '.json')).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'history|recovery'):
            backup.create_backup(root=self.root)

    def test_history_inventory_rejects_unsafe_names_and_extra_members(self):
        self.edited()
        snapshot = backup.create_backup(root=self.root)
        manifest_path = snapshot / 'manifest.json'
        original = json.loads(manifest_path.read_text())
        for name in ('config/.category_rules.csv.history/../escape.json',
                     'config/.category_rules.csv.history/nested/event.json',
                     'config/.category_rules.csv.history/not-an-event.json',
                     'config/.category_rules.csv.lock',
                     'config/.category_rules.csv.any.tmp'):
            altered = json.loads(json.dumps(original))
            altered['files'][name] = '0' * 64
            manifest_path.write_text(json.dumps(altered))
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'manifest'):
                backup.verify_backup(snapshot)
        manifest_path.write_text(json.dumps(original))
        extra = snapshot / 'config/.category_rules.csv.history/extra.tmp'
        extra.parent.mkdir(exist_ok=True)
        extra.write_text('private')
        with self.assertRaisesRegex(ValueError, 'inventory'):
            backup.verify_backup(snapshot)

    def test_history_reparse_member_refused(self):
        path, event = self.edited()
        member = path.parent / '.category_rules.csv.history' / (event + '.json')
        original = backup._is_snapshot_link
        with patch.object(backup, '_is_snapshot_link', side_effect=lambda p: p == member or original(p)):
            with self.assertRaisesRegex(ValueError, 'link|reparse'):
                backup.create_backup(root=self.root)

    def test_external_rule_change_during_copy_fails_without_publication(self):
        path, _ = self.edited()
        real_copy = backup.shutil.copyfile
        def copy(source, destination):
            result = real_copy(source, destination)
            if Path(source) == path:
                path.write_bytes(path.read_bytes() + b'OTHER,Old,Old,true,external\n')
            return result
        with patch.object(backup.shutil, 'copyfile', side_effect=copy):
            with self.assertRaisesRegex(ValueError, 'changed|inconsistent|history'):
                backup.create_backup(root=self.root, name='raced')
        self.assertFalse((self.root / 'finance/backups/raced').exists())
