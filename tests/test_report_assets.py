import json
import re
from pathlib import Path

from scripts.report_assets import LEGACY_API_BASE, LEGACY_CHART_TAG, localize_report_html
from tests.test_dashboard_security import DashboardFixture
from scripts.report_income_vs_expense import _build_html as ive
from scripts.report_income_by_category import _build_html as ibc
from scripts.report_expenditure_by_category import _build_html as ebc
from scripts.report_expenditure_heatmap import _build_html as heatmap
from scripts import dashboard as d


class AssetTests(DashboardFixture):
    def test_report_client_executes_session_token_flow_and_refuses_file(self):
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('Optional Node.js runtime not installed')
        source = Path(d.app.static_folder) / 'report-client.js'
        code = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const calls = [];
const context = {URL, Headers, window:{location:{protocol:'http:', origin:'http://127.0.0.1:5000', href:'http://127.0.0.1:5000/report/raw/income_vs_expense'}},
fetch: async (path, options) => {calls.push({path, options}); return {ok:true, json:async()=>({csrf_token:'synthetic-session-token'})};}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
await context.ledgerFetch('/api/update-note', {method:'POST',headers:{'Content-Type':'application/json'},body:'{"txn_id":1,"note":"fixture"}'});
assert.equal(calls.length,2);
assert.equal(calls[0].path,'/api/csrf-token');
assert.equal(calls[0].options.credentials,'same-origin');
assert.equal(calls[1].options.headers.get('X-CSRF-Token'),'synthetic-session-token');
assert.equal(calls[1].options.credentials,'same-origin');
await assert.rejects(()=>context.ledgerFetch('https://evil.example/api/update-note',{method:'POST'}),/same-origin/);
context.window.location.protocol='file:';
await assert.rejects(()=>context.ledgerFetch('/api/update-note',{method:'POST'}),/local dashboard/);
assert.equal(calls.length,2);
})().catch(e=>{console.error(e); process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_reports_use_local_chart_and_session_client(self):
        for build in [ive, ibc, ebc]:
            text = build([], ['2026-01'])
            self.assertNotIn('https://cdn.jsdelivr.net', text)
            self.assertIn('/static/vendor/chart.umd-4.4.8.js', text)
        text = ive([], ['2026-01'])
        self.assertIn('/static/report-client.js', text)
        self.assertIn('ledgerFetch(', text)
        self.assertNotIn("? 'http://127.0.0.1:5000'", text)
        self.assertIn('local dashboard', text)

    def test_existing_report_is_localized_without_disk_changes(self):
        self.paths.reports_dir.mkdir(parents=True)
        path = self.paths.reports_dir / 'income_vs_expense.html'
        old = b'''<html><head><script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script></head><body><script>
const API_BASE = (window.location.protocol === 'file:') ? 'http://127.0.0.1:5000' : '';
fetch(API_BASE + '/api/update-note', {method:'POST'});
</script></body></html>'''
        path.write_bytes(old)
        response = self.client.get('/report/raw/income_vs_expense', base_url=self.base)
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertNotIn('https://cdn.jsdelivr.net', text)
        self.assertIn('/static/vendor/chart.umd-4.4.8.js', text)
        self.assertIn('/static/report-client.js', text)
        self.assertIn("ledgerFetch('/api/update-note'", text)
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(self.client.get('/report/raw/unknown', base_url=self.base).status_code, 404)

    def test_legacy_adaptation_preserves_embedded_data(self):
        note = '\n'.join([LEGACY_CHART_TAG, LEGACY_API_BASE,
            "fetch(API_BASE + '/api/update-note'", "fetch(API_BASE + '/api/categories'",
            "fetch(API_BASE + '/api/recategorize'", '<script src="/static/report-client.js"></script>',
            'const ALL_MONTHS = ["not the real months"];'])
        txns = [{'user_note': note, 'nested': {'values': [[], {'category': note}]}}]
        months = ['2026-01', note]
        old = ('<html><head>' + LEGACY_CHART_TAG + '</head><body><script>\n'
            + 'const ALL_TXNS = ' + json.dumps(txns) + ';\n'
            + 'const ALL_MONTHS = ' + json.dumps(months) + ';\n'
            + LEGACY_API_BASE + '\n'
            + "fetch(API_BASE + '/api/update-note', {method:'POST'});\n"
            + "fetch(API_BASE + '/api/categories');\n"
            + "fetch(API_BASE + '/api/recategorize', {method:'POST'});\n"
            + '</script></body></html>')
        self.paths.reports_dir.mkdir(parents=True)
        path = self.paths.reports_dir / 'income_vs_expense.html'
        path.write_text(old, encoding='utf-8')
        before = path.read_bytes()
        response = self.client.get('/report/raw/income_vs_expense', base_url=self.base)
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for name, expected in [('ALL_TXNS', txns), ('ALL_MONTHS', months)]:
            match = re.search(r'^const ' + name + r'\s*=\s*', text, re.MULTILINE)
            self.assertEqual(json.JSONDecoder().raw_decode(text, match.end())[0], expected)
        self.assertIn("ledgerFetch('/api/update-note'", text)
        self.assertIn("ledgerFetch('/api/categories'", text)
        self.assertIn("ledgerFetch('/api/recategorize'", text)
        self.assertIn("const API_BASE = '';", text)
        self.assertIn('<script src="/static/report-client.js"></script>\n</head>', text)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(localize_report_html(text), text)

    def test_report_json_cannot_terminate_script_and_decodes_unchanged(self):
        malicious = '</ScRiPt><script>globalThis.breakout = true;</script><!-- <>& café \\ "'
        txns = [{'txn_id': 1, 'date': '2026-01-01', 'month': '2026-01',
                 'statement_date': '2026-01', 'amount': -1, 'merchant': 'SYNTHETIC',
                 'category': malicious, 'subcategory': malicious, 'user_note': malicious,
                 'excluded': 0, 'exclude_reason': None,
                 'nested': {'items': [[], {'note': malicious}]}}]
        months = ['2026-01', malicious]
        legacy = ('<html><head>' + LEGACY_CHART_TAG + '</head><body><script>\n'
                  + 'const ALL_TXNS = ' + json.dumps(txns) + ';\n'
                  + 'const ALL_MONTHS = ' + json.dumps(months) + ';\n'
                  + LEGACY_API_BASE + '\n'
                  + "fetch(API_BASE + '/api/update-note', {method:'POST'});\n"
                  + '</script></body></html>')
        self.paths.reports_dir.mkdir(parents=True)
        path = self.paths.reports_dir / 'income_vs_expense.html'
        path.write_text(legacy, encoding='utf-8')
        before = path.read_bytes()
        served = self.client.get('/report/raw/income_vs_expense', base_url=self.base)
        self.assertEqual(served.status_code, 200)
        cases = [('legacy', served.get_data(as_text=True))]
        cases.extend((build.__module__, build(txns, months)) for build in [ive, ibc, ebc, heatmap])
        for label, text in cases:
            with self.subTest(report=label):
                for name, expected in [('ALL_TXNS', txns), ('ALL_MONTHS', months)]:
                    match = re.search(r'^const ' + name + r'\s*=\s*', text, re.MULTILINE)
                    decoded, end = json.JSONDecoder().raw_decode(text, match.end())
                    self.assertEqual(decoded, expected)
                    payload = text[match.end():end]
                    self.assertFalse(any(char in payload for char in '<>&'), payload)
                self.assertNotIn('<script>globalThis.breakout', text)
                self.assertEqual(localize_report_html(text), localize_report_html(localize_report_html(text)))
        self.assertEqual(path.read_bytes(), before)

    def test_adaptation_changes_only_executable_calls_and_exact_asset_tags(self):
        literal = "fetch(API_BASE + '/api/update-note'"
        markup = '<pre>' + LEGACY_API_BASE + '</pre>'
        code = ('<script>\n'
                + '// ' + LEGACY_API_BASE + '\n'
                + '/* ' + literal + ' */\n'
                + 'const example = ' + json.dumps(literal) + ';\n'
                + 'const template = `' + literal + '`;\n'
                + LEGACY_API_BASE + '\n'
                + "fetch(API_BASE + '/api/update-note', {});\n</script>")
        html = '<html><head>' + LEGACY_CHART_TAG + '</head><body>' + markup + code + '</body></html>'
        text = localize_report_html(html)
        self.assertIn(markup, text)
        self.assertIn('// ' + LEGACY_API_BASE, text)
        self.assertIn('/* ' + literal + ' */', text)
        self.assertIn('const example = ' + json.dumps(literal), text)
        self.assertIn('const template = `' + literal + '`', text)
        self.assertIn("ledgerFetch('/api/update-note', {});", text)
        self.assertIn("\nconst API_BASE = '';\n", text)

    def test_generated_and_legacy_script_data_execute_without_breakout(self):
        import shutil
        import subprocess
        from html.parser import HTMLParser
        if not shutil.which('node'):
            self.skipTest('Optional Node.js runtime not installed')

        class Scripts(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inline = []
                self.current = None

            def handle_starttag(self, tag, attrs):
                if tag == 'script' and not dict(attrs).get('src'):
                    self.current = ''

            def handle_data(self, data):
                if self.current is not None:
                    self.current += data

            def handle_endtag(self, tag):
                if tag == 'script' and self.current is not None:
                    self.inline.append(self.current)
                    self.current = None

        txns = [{'txn_id': 1, 'date': '2026-01-01', 'statement_date': '2026-01',
                 'month': '2026-01', 'amount': -1, 'merchant': 'SYNTHETIC',
                 'category': 'Fixture', 'subcategory': 'Test', 'excluded': 0,
                 'user_note': '</script><script>globalThis.breakout=true;</script> & " café'}]
        months = ['2026-01']
        fixtures = []
        for build in [ive, ibc, ebc, heatmap]:
            new = build(txns, months)
            legacy = new.replace('<script src="/static/vendor/chart.umd-4.4.8.js"></script>', LEGACY_CHART_TAG)
            legacy = legacy.replace('<script src="/static/report-client.js"></script>', '')
            legacy = legacy.replace("const API_BASE = '';", LEGACY_API_BASE)
            for route in ['categories', 'recategorize', 'update-note']:
                legacy = legacy.replace(f"ledgerFetch('/api/{route}'", f"fetch(API_BASE + '/api/{route}'")
            # Reproduce pre-fix unsafe JSON without touching a generated file.
            for name, value in [('ALL_TXNS', txns), ('ALL_MONTHS', months)]:
                match = re.search(r'^const ' + name + r'\s*=\s*', legacy, re.MULTILINE)
                _, end = json.JSONDecoder().raw_decode(legacy, match.end())
                legacy = legacy[:match.end()] + json.dumps(value) + legacy[end:]
            for text in [new, localize_report_html(legacy)]:
                parser = Scripts()
                parser.feed(text)
                self.assertEqual(len(parser.inline), 2 if build is ive else 1)
                declarations = []
                for name in ['ALL_TXNS', 'ALL_MONTHS']:
                    match = re.search(r'^const ' + name + r'\s*=\s*', text, re.MULTILINE)
                    _, end = json.JSONDecoder().raw_decode(text, match.end())
                    declarations.append(text[match.start():end] + ';')
                fixtures.append({'scripts': parser.inline, 'data': '\n'.join(declarations)})
        code = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const input = JSON.parse(fs.readFileSync(0,'utf8'));
for (const fixture of input.fixtures) {
  for (const source of fixture.scripts) new vm.Script(source);
  const context = vm.createContext({});
  const result = vm.runInContext(fixture.data + '; JSON.stringify([ALL_TXNS, ALL_MONTHS]);', context);
  assert.deepStrictEqual(JSON.parse(result), [input.txns, input.months]);
  assert.equal(context.breakout, undefined);
}
'''
        result = subprocess.run(['node', '-e', code], input=json.dumps({'fixtures': fixtures, 'txns': txns, 'months': months}),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_vendor_and_client_are_served_locally(self):
        vendor = self.client.get('/static/vendor/chart.umd-4.4.8.js', base_url=self.base)
        self.addCleanup(vendor.close)
        self.assertEqual(vendor.status_code, 200)
        self.assertIn(b'Chart.js v4.4.8', vendor.data[:500])
        self.assertGreater(len(vendor.data), 100000)
        license = self.client.get('/static/vendor/Chart.js-LICENSE.md', base_url=self.base)
        self.addCleanup(license.close)
        self.assertEqual(license.status_code, 200)
        self.assertIn(b'MIT License', license.data)
        client = self.client.get('/static/report-client.js', base_url=self.base)
        self.addCleanup(client.close)
        self.assertEqual(client.status_code, 200)
        self.assertIn(b'/api/csrf-token', client.data)
        self.assertIn(b'X-CSRF-Token', client.data)
        self.assertIn(b'file:', client.data)
