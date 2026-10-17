from pathlib import Path
import tempfile
import unittest

from scripts.apply_categories import _load_rules
from scripts.services import category_rules as rules


class RuleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'rules.csv'
        self.path.write_text('merchant_core,channels,category,subcategory,notes\nSHOP,card,Old,Old,keep\n', encoding='utf-8')

    def test_change_writes_recoverable_history_and_does_not_change_other_columns(self):
        before = self.path.read_bytes()
        state = rules.snapshot(self.path)
        result = rules.change(self.path, state['digest'], 'SHOP', ('New', 'New'), True)
        self.assertTrue(result['changed'])
        self.assertEqual(_load_rules(self.path)['SHOP']['category'], 'New')
        self.assertEqual(rules.snapshot(self.path)['rules']['SHOP']['notes'], 'keep')
        history = rules.history(self.path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['id'], result['event_id'])
        self.assertEqual(history[0]['before_csv'].encode('utf-8'), before)
        self.assertEqual(history[0]['after']['enabled'], 'true')

    def test_noop_stale_disable_reenable_and_targeted_undo(self):
        from unittest.mock import patch
        first = rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), True)
        state = rules.snapshot(self.path)
        before = self.path.read_bytes()
        self.assertFalse(rules.change(self.path, state['digest'], 'SHOP', ('New', 'New'), True)['changed'])
        self.assertEqual(before, self.path.read_bytes())
        other = rules.change(self.path, state['digest'], 'OTHER', ('A', 'B'), True)
        rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('Old', 'Old'), True, undo_id=first['event_id'])
        self.assertEqual(_load_rules(self.path)['SHOP']['category'], 'Old')
        self.assertIn('OTHER', _load_rules(self.path))
        with self.assertRaisesRegex(ValueError, 'changed'):
            rules.change(self.path, state['digest'], 'SHOP', ('New', 'New'), True)
        disabled = rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('Old', 'Old'), False)
        self.assertNotIn('SHOP', _load_rules(self.path))
        rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('Old', 'Old'), True)
        with self.assertRaisesRegex(ValueError, 'later edits'):
            rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('Old', 'Old'), True, undo_id=disabled['event_id'])
        rules.change(self.path, rules.snapshot(self.path)['digest'], 'OTHER', ('A', 'B'), True, undo_id=other['event_id'])
        self.assertNotIn('OTHER', _load_rules(self.path))

    def test_failed_publication_is_not_history_and_retry_succeeds(self):
        from unittest.mock import patch
        import os
        before = self.path.read_bytes()
        real_replace = os.replace
        def fail_csv(source, target):
            if Path(target) == self.path:
                raise OSError('synthetic replace failure')
            return real_replace(source, target)
        with patch.object(rules.os, 'replace', side_effect=fail_csv):
            with self.assertRaises(OSError):
                rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), True)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(rules.history(self.path), [])
        self.assertEqual(len(list(self.path.parent.glob('.*.history/*.json'))), 1)
        rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), True)
        self.assertEqual(len(rules.history(self.path)), 1)

    def test_lock_collision_and_journal_failure_leave_csv_unchanged(self):
        from unittest.mock import patch
        before = self.path.read_bytes()
        with rules._write_lock(self.path):
            with self.assertRaisesRegex(ValueError, 'being edited'):
                rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), True)
        with patch.object(rules, '_atomic_write', side_effect=OSError('synthetic journal failure')):
            with self.assertRaises(OSError):
                rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), True)
        self.assertEqual(before, self.path.read_bytes())

    def test_damaged_history_fails_closed_with_clear_error(self):
        rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), True)
        directory = self.path.with_name('.rules.csv.history')
        (directory / 'damaged.json').write_text('{}', encoding='utf-8')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'damaged'):
            rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_parallel_writers_and_aba_undo_never_overwrite_later_edits(self):
        from concurrent.futures import ThreadPoolExecutor
        state = rules.snapshot(self.path)
        def save(destination):
            try:
                return rules.change(self.path, state['digest'], 'SHOP', (destination, destination), True)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ['First', 'Second']))
        self.assertEqual(sum(result is not None for result in results), 1)
        event_id = next(result['event_id'] for result in results if result)
        current = rules.snapshot(self.path)['rules']['SHOP']['category']
        rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('Middle', 'Middle'), True)
        rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', (current, current), True)
        with self.assertRaisesRegex(ValueError, 'later edits'):
            rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('Old', 'Old'), True, undo_id=event_id)

    def test_success_finalizes_identity_and_external_revert_is_not_prepared(self):
        import json
        first = rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), True)
        before = self.path.read_bytes()
        second = rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('Next', 'Next'), True)
        state = json.loads((self.path.with_name('.rules.csv.history') / 'state').read_text())
        self.assertEqual(state['head'], second['event_id'])
        self.assertIsNone(state['pending'])
        self.path.write_bytes(before)
        with self.assertRaisesRegex(ValueError, 'outside maintenance'):
            rules.history(self.path)
        with self.assertRaises(ValueError):
            rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('Old', 'Old'), True,
                         undo_id=first['event_id'])

    def test_restart_resolves_every_publication_failure_boundary(self):
        import json
        import subprocess
        import sys
        from unittest.mock import patch
        original = self.path.read_bytes()
        real_write = rules._atomic_write
        # Initial state, event, pending state, CSV, finalized state.
        for boundary in range(1, 6):
            for after_write in (False, True):
                with self.subTest(boundary=boundary, after_write=after_write):
                    import shutil
                    shutil.rmtree(self.path.with_name('.rules.csv.history'), ignore_errors=True)
                    self.path.write_bytes(original)
                    calls = 0
                    def fail(path, data):
                        nonlocal calls
                        calls += 1
                        if calls == boundary and not after_write:
                            raise OSError('synthetic publication boundary')
                        real_write(path, data)
                        if calls == boundary and after_write:
                            raise OSError('synthetic publication boundary')
                    with patch.object(rules, '_atomic_write', side_effect=fail), self.assertRaises(OSError):
                        rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), True)
                    applied = boundary == 5 or (boundary == 4 and after_write)
                    command = [sys.executable, '-B', '-c',
                        'import json,sys; from scripts.services import category_rules as r; print(json.dumps([e["id"] for e in r.history(sys.argv[1])]))', str(self.path)]
                    restarted = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                               capture_output=True, text=True, check=True)
                    self.assertEqual(len(json.loads(restarted.stdout)), int(applied))
                    self.assertEqual(self.path.read_bytes() == original, not applied)
                    rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('Retry', 'Retry'), True)
                    self.assertEqual(len(rules.history(self.path)), 1 + int(applied))

    def test_v2_cycles_missing_parents_state_and_checksums_fail_closed(self):
        import json
        self.path.write_bytes(b'merchant_core,category,subcategory,enabled,revision\r\nSHOP,Old,Old,true,base\r\n')
        created = rules.change(self.path, rules.snapshot(self.path)['digest'], 'NEW', ('New', 'New'), True)
        removed = rules.change(self.path, rules.snapshot(self.path)['digest'], 'NEW', ('New', 'New'), False,
                               undo_id=created['event_id'])
        directory = self.path.with_name('.rules.csv.history')
        event_path = directory / (created['event_id'] + '.json')
        original = event_path.read_bytes()
        for alteration, error in (({'parent_id': removed['event_id']}, 'cycle'),
                                  ({'parent_id': '0' * 32}, 'parent'),
                                  ({'after_digest': '0' * 64}, 'damaged')):
            with self.subTest(alteration=alteration):
                event = json.loads(original)
                event.update(alteration)
                event_path.write_text(json.dumps(event))
                with self.assertRaisesRegex(ValueError, error):
                    rules.history(self.path)
                event_path.write_bytes(original)
        state = directory / 'state'
        saved = state.read_bytes()
        for data in (b'{}', b'{"version": true}', b'not json'):
            state.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'publication state'):
                rules.history(self.path)
        state.unlink()
        with self.assertRaisesRegex(ValueError, 'missing publication state'):
            rules.history(self.path)
        state.write_bytes(saved)
        self.assertEqual(len(rules.history(self.path)), 2)

    def test_unambiguous_legacy_history_upgrades_only_on_write(self):
        import json
        first = rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), True)
        directory = self.path.with_name('.rules.csv.history')
        event_path = directory / (first['event_id'] + '.json')
        event = json.loads(event_path.read_text())
        del event['version'], event['parent_id']
        event_path.write_text(json.dumps(event))
        (directory / 'state').unlink()
        self.assertEqual(rules.history(self.path)[0]['id'], first['event_id'])
        self.assertFalse((directory / 'state').exists())
        second = rules.change(self.path, rules.snapshot(self.path)['digest'], 'OTHER', ('New', 'New'), True)
        self.assertEqual([e['id'] for e in rules.history(self.path)], [second['event_id'], first['event_id']])
        rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('Old', 'Old'), True,
                     undo_id=first['event_id'])
        self.assertEqual(rules.load_rules(self.path)['SHOP']['category'], 'Old')

    def test_external_edit_after_committed_history_blocks_further_writes(self):
        rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), True)
        self.path.write_bytes(self.path.read_bytes() + b'EXTERNAL,,A,B,,,\r\n')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'outside maintenance'):
            rules.change(self.path, rules.snapshot(self.path)['digest'], 'SHOP', ('New', 'New'), False)
        self.assertEqual(before, self.path.read_bytes())


class RuleLoaderTests(unittest.TestCase):
    def test_disabled_last_duplicate_does_not_revive_older_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rules.csv'
            path.write_text('merchant_core,channels,category,subcategory,notes,enabled\nSHOP,,Old,Old,legacy,\nshop,,New,New,disabled,false\nOTHER,,A,B,kept,\n', encoding='utf-8')
            before = path.read_bytes()
            rules = _load_rules(path)
            self.assertNotIn('SHOP', rules)
            self.assertEqual(rules['OTHER']['category'], 'A')
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)
