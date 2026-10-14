from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import dashboard
from scripts.init_db import _SCHEMA_SQL
from scripts.migrate_db import migrate_db


class TransferUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'ledger.db'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,account_id,import_hash,user_note) VALUES (1,'2026-01-31','<script>bad</script>',-10,'HSBC_UK','one','keep')")
            conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,account_id,import_hash) VALUES (2,'2026-02-02','Own Accounts',10,'IOM','two')")
            conn.commit()
        migrate_db(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE accounts SET owned=1,currency='GBP' WHERE account_id='IOM'")
        for key, value in [('DB_PATH', self.db), ('PROJECT_ROOT', self.root)]:
            p = patch.object(dashboard, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = dashboard.app.test_client()
        self.csrf = self.client.get('/api/csrf-token').json['csrf_token']
        self.refresh = patch.object(dashboard, '_regenerate_report')
        self.refresh_mock = self.refresh.start()
        self.addCleanup(self.refresh.stop)

    def sql(self, sql):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql).fetchall()

    def post(self, path, data, client=None, csrf=None):
        return (client or self.client).post(path, json=data, headers={'X-CSRF-Token': csrf or self.csrf, 'Origin': 'http://localhost'})

    def test_javascript_executes_exact_id_cancel_and_saved_warning_paths(self):
        import subprocess
        harness = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Element {
  constructor() { this.listeners = {}; this.textContent = ''; this.children = []; this.options = []; this.value = ''; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  replaceChildren() { this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const ids = ['transfer-preview','transfer-message','transfer-csrf','preview-title','preview-members','exact-pair','owned-counterpart','unmatched-dialog','close-unmatched','unmatched-form','cancel-transfer','confirm-transfer'];
const elements = Object.fromEntries(ids.map(id => [id,new Element()]));
elements['transfer-csrf'].value = 'local-csrf';
global.document = {getElementById: id => elements[id] || null, querySelectorAll: () => [], createElement: () => new Element()};
global.location = {reload: () => {}};
const requests = [];
global.fetch = async (url, request) => {
  requests.push({url, request});
  let body = {ok:true};
  if (url.endsWith('preview')) body = {ok:true, preview_token:'opaque',action:'pair',members:[{txn_id_text:'9223372036854775807',txn_date:'2026-01-31',account_name:'Demo',cents_text:'-1000',direction:'out',counterpart_name:'Other',description:'<script>untrusted</script>'}]};
  if (url.endsWith('confirm')) body = {ok:true,saved:true,warning:'Transfer decision saved, but reports could not be refreshed.'};
  return {ok:true,json:async () => body};
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const tick = () => new Promise(resolve => setImmediate(resolve));
const submit = () => elements['exact-pair'].listeners.submit({preventDefault(){},target:{elements:{first:{value:'9223372036854775807'},second:{value:'2'}}}});
(async () => {
  submit(); await tick();
  assert.equal(requests[0].request.body, '{"action":"pair","txn_ids":[9223372036854775807,2]}');
  assert.equal(requests[0].request.headers['X-CSRF-Token'], 'local-csrf');
  assert(elements['preview-members'].children[0].textContent.includes('#9223372036854775807'));
  assert(elements['preview-members'].children[0].textContent.includes('<script>untrusted</script>'));
  assert(elements['transfer-preview'].open);
  elements['cancel-transfer'].listeners.click(); await tick();
  assert(requests[1].url.endsWith('cancel'));
  assert(!elements['transfer-preview'].open);
  submit(); await tick();
  elements['confirm-transfer'].listeners.click(); await tick();
  assert(requests[3].url.endsWith('confirm'));
  assert.deepEqual(JSON.parse(requests[3].request.body), {preview_token:'opaque',approved:true});
  assert(elements['transfer-message'].textContent.includes('saved, but reports'));
  assert(elements['transfer-message'].children.some(node => node.textContent === 'Reload updated review'));
  console.log('Frontend exact-ID/cancel/confirm/saved-warning paths passed');
})().catch(error => {console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', harness, str(Path(__file__).resolve().parents[1] / 'static/transfers.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('paths passed', result.stdout)

    def test_retired_page_redirects_without_reading_or_mutating_saved_decisions(self):
        from scripts.services import transfers
        transfers.commit_transfer(transfers.prepare_pair(1, 2, db_path=self.db), approved=True)
        before = self.db.read_bytes()
        with patch.object(transfers, '_connection', side_effect=AssertionError('retired page must not open ledger')):
            response = self.client.get('/transfers')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/transactions')
        self.assertNotIn(b'transfer-preview', response.data)
        self.assertNotIn(b'src="/static/transfers.js"', response.data)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(self.sql('SELECT action FROM transfer_audit'), [('pair',)])
        self.refresh_mock.assert_not_called()

    def test_bookmarks_preserve_only_supported_transaction_search_filters(self):
        from urllib.parse import parse_qs, urlsplit
        supported = {'q': 'Own & Accounts', 'start': '2026-01-01', 'end': '2026-02-28',
                     'amount': '10.00', 'direction': 'credit'}
        response = self.client.get('/transfers', query_string={**supported, 'account_id': 'IOM',
            'status': 'paired', 'txn_id': '1', 'candidate_offset': '25', 'page': '2',
            'next': 'https://evil.example', 'action': 'revoke', 'approved': 'true'})
        self.assertEqual(response.status_code, 302)
        target = urlsplit(response.headers['Location'])
        self.assertEqual((target.scheme, target.netloc, target.path), ('', '', '/transactions'))
        self.assertEqual(parse_qs(target.query), {key: [value] for key, value in supported.items()})
        self.assertEqual(self.sql('SELECT * FROM transfer_audit'), [])
        self.refresh_mock.assert_not_called()

    def test_navigation_and_home_do_not_offer_retired_transfer_ui(self):
        for route in ('/', '/transactions'):
            with self.subTest(route=route):
                page = self.client.get(route)
                self.assertEqual(page.status_code, 200)
                self.assertNotIn(b'href="/transfers', page.data)
                self.assertNotIn(b'src="/static/transfers.js"', page.data)
                self.assertIn(b'href="/transactions"', page.data)

    def test_retained_api_and_asset_preserve_exact_large_id_evidence(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET txn_id=9223372036854775807 WHERE txn_id=1')
        state = self.post('/api/transfers/preview', {'action': 'pair', 'txn_ids': [9223372036854775807, 2]}).json
        self.assertEqual(state['members'][0]['txn_id_text'], '9223372036854775807')
        self.assertEqual(state['members'][0]['cents_text'], '-1000')
        asset = self.client.get('/static/transfers.js')
        self.assertEqual(asset.status_code, 200)
        self.assertIn(b'textContent', asset.data)
        self.assertNotIn(b'innerHTML', asset.data)
        self.assertIn(b'warning', asset.data)

        asset.close()

    def test_session_ownership_expiry_and_replay_are_enforced(self):
        state = self.post('/api/transfers/preview', {'action': 'pair', 'txn_ids': [1, 2]}).json
        token = state['preview_token']
        other = dashboard.app.test_client()
        other_csrf = other.get('/api/csrf-token').json['csrf_token']
        for action, body in [('cancel', {'preview_token': token}), ('confirm', {'preview_token': token, 'approved': True})]:
            self.assertEqual(self.post('/api/transfers/' + action, body, other, other_csrf).status_code, 409)
        with patch('time.monotonic', return_value=dashboard._TRANSFER_PREVIEWS[token]['expires'] + 1):
            self.assertEqual(self.post('/api/transfers/confirm', {'preview_token': token, 'approved': True}).status_code, 409)
        self.assertEqual(self.sql('SELECT * FROM transfer_audit'), [])

    def test_csrf_host_origin_guards_apply_to_all_transfer_actions(self):
        for action in ('preview', 'confirm', 'cancel'):
            url = '/api/transfers/' + action
            self.assertEqual(self.client.post(url, json={}).status_code, 403)
            for headers in [{'Origin': 'null'}, {'Origin': 'https://evil.example'}, {'Host': 'evil.example'}, {'Sec-Fetch-Site': 'cross-site'}]:
                headers['X-CSRF-Token'] = self.csrf
                self.assertEqual(self.client.post(url, json={}, headers=headers).status_code, 403)
        self.assertEqual(self.sql('SELECT * FROM transfer_audit'), [])

    def test_id_and_payload_bounds_reject_booleans_overflow_and_browser_paths(self):
        bad = [True, False, 0, -1, 9223372036854775808, '1', 1.0, None]
        for value in bad:
            for body in [{'action': 'pair', 'txn_ids': [value, 2]}, {'action': 'unmatched', 'txn_id': value, 'counterpart_account_id': 'IOM'}, {'action': 'revoke', 'group_id': value}]:
                with self.subTest(value=value, body=body):
                    self.assertEqual(self.post('/api/transfers/preview', body).status_code, 400)
        for body in [{'action': 'pair', 'txn_ids': [1,2], 'root': str(self.root)}, {'action': 'pair', 'txn_ids': [1,2], 'db_path': str(self.db)}, {'action': 'pair', 'txn_ids': [1]}, {'action': 'pair', 'txn_ids': [1,1]}, {'action': 'unmatched', 'txn_id': 1, 'counterpart_account_id': True}, ['bad']]:
            self.assertEqual(self.post('/api/transfers/preview', body).status_code, 400)
        self.assertEqual(self.post('/api/transfers/preview', {'action': 'pair', 'txn_ids': [1,2], 'padding': 'x'*5000}).status_code, 400)
        state = self.post('/api/transfers/preview', {'action': 'pair', 'txn_ids': [1,2]}).json
        for approved in (False, 1, 'true', None):
            self.assertEqual(self.post('/api/transfers/confirm', {'preview_token': state['preview_token'], 'approved': approved}).status_code, 409)
        self.assertEqual(self.post('/api/transfers/confirm', {'preview_token': state['preview_token'], 'approved': True, 'txn_ids': [1,2]}).status_code, 409)
        self.assertEqual(self.sql('SELECT * FROM transfer_audit'), [])

    def test_ledger_and_all_fixed_config_changes_stale_preview_without_save(self):
        config = self.root / 'config'
        config.mkdir()
        for filename in ('categories.json', 'category_rules.csv', 'merchant_rules.json'):
            with self.subTest(filename=filename):
                state = self.post('/api/transfers/preview', {'action': 'pair', 'txn_ids': [1,2]}).json
                (config / filename).write_text('changed', encoding='utf-8')
                response = self.post('/api/transfers/confirm', {'preview_token': state['preview_token'], 'approved': True})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json['error'], 'stale_preview')
        state = self.post('/api/transfers/preview', {'action': 'pair', 'txn_ids': [1,2]}).json
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET user_note='changed' WHERE txn_id=2")
        self.assertEqual(self.post('/api/transfers/confirm', {'preview_token': state['preview_token'], 'approved': True}).json['error'], 'stale_preview')
        self.assertEqual(self.sql('SELECT * FROM transfer_audit'), [])
        self.assertEqual(self.refresh_mock.call_count, 0)

    def test_category_reassignment_stales_preview(self):
        state = self.post('/api/transfers/preview', {'action': 'pair', 'txn_ids': [1,2]}).json
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO categories(category_id,name) VALUES (1,'Own Accounts')")
            conn.execute("INSERT INTO subcategories(subcategory_id,category_id,name) VALUES (1,1,'OwnAccountsOut')")
            conn.execute('UPDATE transactions SET subcategory_id=1 WHERE txn_id=1')
        self.assertEqual(self.post('/api/transfers/confirm', {'preview_token': state['preview_token'], 'approved': True}).json['error'], 'stale_preview')
        self.assertEqual(self.sql('SELECT * FROM transfer_audit'), [])

    def test_independent_unmatched_groups_need_explicit_undo_not_silent_merge(self):
        for txn_id, counterpart in [(1, 'IOM'), (2, 'HSBC_UK')]:
            state = self.post('/api/transfers/preview', {'action': 'unmatched', 'txn_id': txn_id, 'counterpart_account_id': counterpart}).json
            self.assertEqual(self.post('/api/transfers/confirm', {'preview_token': state['preview_token'], 'approved': True}).status_code, 200)
        before = self.sql('SELECT * FROM transfer_members ORDER BY member_id')
        response = self.post('/api/transfers/preview', {'action': 'pair', 'txn_ids': [1,2]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json['error'], 'already_confirmed')
        self.assertEqual(self.sql('SELECT * FROM transfer_members ORDER BY member_id'), before)
        self.assertEqual(len(self.sql('SELECT * FROM transfer_audit')), 2)
        self.assertEqual(self.client.get('/transfers').status_code, 302)

    def test_external_rows_and_notes_never_change(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO accounts(account_id,display_name,owned,currency,parser_verified,import_enabled) VALUES ('OTHER','External',0,'GBP',0,0)")
            conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,account_id,import_hash,user_note) VALUES (3,'2026-01-31','external fee',-1,'OTHER','external','protected')")
        before = self.sql('SELECT * FROM transactions ORDER BY txn_id')
        response = self.client.get('/transfers?start=2026-01-01&end=2026-02-28')
        self.assertNotIn(b'data-txn-id="3"', response.data)
        self.assertEqual(self.post('/api/transfers/preview', {'action':'unmatched','txn_id':1,'counterpart_account_id':'OTHER'}).status_code, 400)
        state = self.post('/api/transfers/preview', {'action':'pair','txn_ids':[1,2]}).json
        self.assertEqual(self.post('/api/transfers/confirm', {'preview_token':state['preview_token'],'approved':True}).status_code, 200)
        self.assertEqual(self.sql('SELECT * FROM transactions ORDER BY txn_id'), before)

    def test_redirected_search_keeps_raw_rows_and_saved_transfer_decisions(self):
        from scripts.services import transfers
        transfers.commit_transfer(transfers.prepare_pair(1, 2, db_path=self.db), approved=True)
        before = self.db.read_bytes()
        response = self.client.get('/transfers?start=2026-01-01&end=2026-02-28&status=paired', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.path, '/transactions')
        self.assertIn(b'2 matches', response.data)
        self.assertIn(b'&lt;script&gt;bad&lt;/script&gt;', response.data)
        self.assertNotIn(b'src="/static/transfers.js"', response.data)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(self.sql('SELECT action FROM transfer_audit'), [('pair',)])
        self.refresh_mock.assert_not_called()

    def test_invalid_search_filters_are_not_silently_dropped(self):
        before = self.db.read_bytes()
        response = self.client.get('/transfers?start=not-a-date&status=unknown', follow_redirects=True)
        self.assertEqual(response.request.path, '/transactions')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(before, self.db.read_bytes())
        self.refresh_mock.assert_not_called()

    def test_unmatched_later_pair_and_revoke_preserve_audit(self):
        def save(data):
            preview = self.post('/api/transfers/preview', data)
            self.assertEqual(preview.status_code, 200, preview.json)
            response = self.post('/api/transfers/confirm', {'preview_token': preview.json['preview_token'], 'approved': True})
            self.assertEqual(response.status_code, 200, response.json)
            return response.json['group_id']
        group = save({'action': 'unmatched', 'txn_id': 1, 'counterpart_account_id': 'IOM'})
        self.assertEqual(save({'action': 'pair', 'txn_ids': [1, 2]}), group)
        self.assertEqual(save({'action': 'revoke', 'group_id': group}), group)
        self.assertEqual(self.sql('SELECT action FROM transfer_audit ORDER BY audit_id'), [('unmatched',), ('pair',), ('revoke',)])
        self.assertEqual(self.sql('SELECT active FROM transfer_members'), [(0,), (0,)])
        self.assertEqual(self.refresh_mock.call_count, 3)

    def test_confirm_saves_exact_members_once_and_refresh_failure_is_saved_warning(self):
        before = self.sql('SELECT * FROM transactions ORDER BY txn_id')
        state = self.post('/api/transfers/preview', {'action': 'pair', 'txn_ids': [1, 2]}).json
        self.refresh_mock.side_effect = RuntimeError('synthetic failure')
        with self.assertLogs(dashboard.app.logger, level='ERROR'):
            response = self.post('/api/transfers/confirm', {'preview_token': state['preview_token'], 'approved': True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['saved'])
        self.assertIn('saved', response.json['warning'])
        self.assertEqual(self.sql('SELECT txn_id FROM transfer_members ORDER BY txn_id'), [(1,), (2,)])
        self.assertEqual(self.sql('SELECT action FROM transfer_audit'), [('pair',)])
        self.assertEqual(self.sql('SELECT * FROM transactions ORDER BY txn_id'), before)
        self.assertEqual(self.post('/api/transfers/confirm', {'preview_token': state['preview_token'], 'approved': True}).status_code, 409)
        self.assertEqual(self.refresh_mock.call_count, 1)

    def test_pair_preview_cancel_is_exact_and_readonly(self):
        before = self.sql('SELECT * FROM transactions ORDER BY txn_id')
        response = self.post('/api/transfers/preview', {'action': 'pair', 'txn_ids': [1, 2]})
        self.assertEqual(response.status_code, 200)
        state = response.json
        self.assertEqual([row['txn_id'] for row in state['members']], [1, 2])
        self.assertEqual([row['cents'] for row in state['members']], [-1000, 1000])
        self.assertNotIn('db_path', state)
        self.assertEqual(self.post('/api/transfers/cancel', {'preview_token': state['preview_token']}).status_code, 200)
        self.assertEqual(self.post('/api/transfers/confirm', {'preview_token': state['preview_token'], 'approved': True}).status_code, 409)
        self.assertEqual(self.sql('SELECT * FROM transactions ORDER BY txn_id'), before)
        self.assertEqual(self.sql('SELECT * FROM transfer_audit'), [])

    def test_missing_migration_or_database_does_not_block_or_trigger_setup(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM schema_migrations WHERE version=4')
        before = self.db.read_bytes()
        response = self.client.get('/transfers')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/transactions')
        self.assertEqual(before, self.db.read_bytes())
        missing = self.root / 'never-created.db'
        with patch.object(dashboard, 'DB_PATH', missing):
            response = self.client.get('/transfers')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/transactions')
        self.assertFalse(missing.exists())
