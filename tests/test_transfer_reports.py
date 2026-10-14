from contextlib import closing
from pathlib import Path
import importlib
import json
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.init_db import _SCHEMA_SQL
from scripts.migrate_db import migrate_db
from scripts.services import transfers, movement_policy

MODULES = ['report_income_vs_expense', 'report_income_by_category',
           'report_expenditure_by_category', 'report_expenditure_heatmap']

class TransferReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/'fixture.db'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute("INSERT INTO categories(category_id,name) VALUES(1,'Income renamed expense')")
            conn.execute("INSERT INTO subcategories(subcategory_id,category_id,name) VALUES(1,1,'Own Accounts')")
            rows = [(1,'2026-01-31',-10,'HSBC_UK',1,0), (2,'2026-02-02',10,'IOM',1,0),
                    (3,'2026-01-31',-1.23,'HSBC_UK',None,0), (4,'2026-01-31',2,'HSBC_UK',1,0),
                    (5,'2026-01-31',-3,'HSBC_UK',None,1)]
            for i,date,amount,account,sub,excluded in rows:
                conn.execute('INSERT INTO transactions(txn_id,txn_date,description_raw,amount,account_id,import_hash,subcategory_id,excluded,user_note,source_file) VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (i,date,'fixture',amount,account,str(i),sub,excluded,'keep </ScRiPt> &','StatementsPDF/2026-02-10_fixture.pdf'))
            conn.commit()
        migrate_db(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE accounts SET owned=1,currency='GBP' WHERE account_id='IOM'")
        transfers.commit_transfer(transfers.prepare_pair(1,2,db_path=self.db),approved=True)

    def test_all_four_generated_reports_execute_exact_adjusted_and_raw_views(self):
        import subprocess
        rows, months = self.query()
        for name in MODULES:
            with self.subTest(report=name):
                module = importlib.import_module('scripts.'+name)
                html = module._build_html(rows,months)
                self.assertIn('data-movement-policy="confirmed-v1"',html)
                self.assertIn('id="omittedRows"',html)
                self.assertNotIn('<details open',html)
                self.assertNotIn('keep </ScRiPt>',html)
                scripts = re.findall(r'<script>(.*?)</script>',html,re.S)
                result = subprocess.run(['node',str(Path(__file__).with_name('report_classic_probe.js'))],
                    input=json.dumps({'html':html,'scripts':scripts,'helper':str(Path(__file__).parents[1]/'static/report-classic.js')}),
                    text=True,capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr)

    def test_served_policy_refreshes_current_flags_and_account_period_scope(self):
        from scripts import report_assets
        rows, months = self.query()
        html = importlib.import_module('scripts.report_expenditure_heatmap')._build_html(rows,months)
        self.assertTrue(hasattr(report_assets,'serve_report_html'),'served scope adapter missing')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET excluded=1 WHERE txn_id=3')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('BEGIN')
            served = report_assets.serve_report_html(html,conn=conn,account_ids=['HSBC_UK'],
                period={'start':'2026-01-01','end':'2026-01-31','kind':'month'},name='expenditure_heatmap')
            self.assertTrue(conn.in_transaction,'caller snapshot must be preserved')
            match = re.search(r'const REPORT_POLICY\s*=\s*',served)
            evidence,_=json.JSONDecoder().raw_decode(served,match.end())
            self.assertEqual(evidence['raw']['debits_pence'],'1423')
            self.assertEqual(evidence['adjusted']['debits_pence'],'0')
            self.assertIn('confirmed_counterpart_outside_scope',evidence['warnings'])
            self.assertEqual(evidence['scope']['account_ids'],['HSBC_UK'])
            match = re.search(r'const ALL_TXNS\s*=\s*',served)
            records,_=json.JSONDecoder().raw_decode(served,match.end())
            self.assertEqual(len(records),4)
            with self.assertRaises(ValueError):
                report_assets.serve_report_html(html,conn=conn,account_ids=['invalid'],period={},name='expenditure_heatmap')

    def test_legacy_serving_warns_without_false_policy_stamp_or_file_rewrite(self):
        from scripts.report_assets import localize_report_html, serve_report_html
        legacy='<html><head></head><body><script>\nconst ALL_TXNS = [{"txn_id":1,"account_id":"HSBC_UK","date":"2026-01-31","statement_date":"2026-02-10"}];\nconst ALL_MONTHS = ["2026-02-10"];\n</script></body></html>'
        localized=localize_report_html(legacy)
        self.assertIn('data-legacy-policy-warning',localized)
        self.assertIn('Regenerate',localized)
        self.assertNotIn('data-movement-policy="confirmed-v1"',localized)
        self.assertEqual(localize_report_html(localized),localized)
        with closing(sqlite3.connect(self.db)) as conn:
            filtered=serve_report_html(legacy,conn=conn,account_ids=['IOM'],period={},name='income_vs_expense')
            match=re.search(r'const ALL_TXNS\s*=\s*',filtered)
            self.assertEqual(json.JSONDecoder().raw_decode(filtered,match.end())[0],[])
        self.assertNotIn('data-legacy-policy-warning',legacy)

    def test_wrapper_preserves_literal_account_and_existing_calendar_query(self):
        from scripts.dashboard import app
        from flask import render_template
        period={'kind':'month','year':'2026','month':'1','start':'2026-01-01','end':'2026-01-31',
                'query':'period=month&year=2026&month=1','label':'January','range':'fixture','method':'transaction date','error':None}
        with app.test_request_context('/report'):
            html=render_template('report.html',reports=[('income_vs_expense','Income vs Expenditure')],
                current_report='income_vs_expense',current_report_label='Income vs Expenditure',
                period=period,year_options=[2026],month_options=[(1,'January')],account_id='IOM',
                accounts=[{'account_id':'HSBC_UK','display_name':'UK'},{'account_id':'IOM','display_name':'IoM'}])
        self.assertIn('name="account_id"',html)
        self.assertIn('value="IOM" selected',html)
        self.assertIn('period=month&amp;year=2026&amp;month=1&amp;account_id=IOM',html)

    def test_revoked_history_is_evidence_not_active_principal(self):
        rows,_=self.query()
        group_id=int(rows[0]['transfer']['group_id'])
        transfers.commit_transfer(transfers.prepare_revoke(group_id,db_path=self.db),approved=True)
        rows,_=self.query()
        by_id={r['txn_id']:r for r in rows}
        self.assertIsNone(by_id['1']['transfer'])
        self.assertEqual(rows.policy['adjusted']['debits_pence'],'1123')
        self.assertIn('transfer_history',by_id['1'])
        history=by_id['1']['transfer_history']
        self.assertEqual(history[0]['status'],'revoked')
        self.assertEqual([a['action'] for a in history[0]['audit']],['pair','revoke'])
        self.assertEqual(history[0]['active'],0)
        unmatched=transfers.commit_transfer(transfers.prepare_unmatched(3,'IOM',db_path=self.db),approved=True)
        rows,_=self.query()
        self.assertEqual(rows.policy['adjusted']['debits_pence'],'1000')
        self.assertIn('explicit_unmatched_transfers',rows.policy['warnings'])
        self.assertEqual(next(r for r in rows if r['txn_id']=='3')['transfer']['status'],'unmatched')

    def test_source_evidence_preserves_canonical_document_identity(self):
        from scripts.report_data import query_report
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO source_documents(document_id,account_id,document_key,source_identity,content_hash,reconciliation_status) VALUES(1,'HSBC_UK','StatementsPDF/nested/original.pdf','fixture-source','fixture-hash','passed')")
            conn.execute("INSERT INTO transaction_sources(txn_id,document_id,row_ref,occurrence) VALUES(3,1,'page:1:row:3',1)")
        rows,_=query_report(db_path=self.db,account_ids=['HSBC_UK'])
        evidence=next(r for r in rows if r['txn_id']=='3')['source_evidence'][0]
        self.assertIn('document',evidence)
        self.assertEqual(evidence['document']['document_key'],'StatementsPDF/nested/original.pdf')
        self.assertEqual(evidence['row_ref'],'page:1:row:3')

    def test_actual_generators_write_only_fixture_reports_with_unsafe_pence_and_ids(self):
        import subprocess
        from decimal import localcontext, Inexact, Rounded
        from scripts import report_income_vs_expense
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE categories SET name='__proto__'")
            conn.execute("UPDATE subcategories SET name='constructor'")
            conn.execute('UPDATE transactions SET amount=-90071992547410.0 WHERE txn_id=3')
            conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,account_id,import_hash) VALUES(9007199254740993,'2026-01-31','tiny fee',-0.01,'HSBC_UK','unsafe-id')")
        report_dir=Path(self.temp.name)/'reports'
        with patch.object(report_income_vs_expense,'DB_PATH',self.db), localcontext() as context:
            context.prec=3
            context.traps[Inexact]=True
            context.traps[Rounded]=True
            for name in MODULES:
                with self.subTest(report=name):
                    module=importlib.import_module('scripts.'+name)
                    with patch.object(module,'REPORTS_DIR',report_dir),patch.object(module.webbrowser,'open'):
                        module.main()
                    html=(report_dir/(name.removeprefix('report_')+'.html')).read_text(encoding='utf-8')
                    self.assertNotIn('keep </ScRiPt>',html)
                    result=subprocess.run(['node',str(Path(__file__).with_name('report_classic_probe.js'))],
                        input=json.dumps({'html':html,'scenario':'unsafe','scripts':re.findall(r'<script>(.*?)</script>',html,re.S),
                            'helper':str(Path(__file__).parents[1]/'static/report-classic.js')}),capture_output=True,text=True)
                    self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(list(report_dir.glob('*.html'))),4)

    def test_unstamped_builders_warn_and_note_text_cannot_forge_policy_freshness(self):
        from scripts.report_assets import localize_report_html
        row={'txn_id':1,'date':'2026-01-31','month':'2026-01','statement_date':'2026-02-10','amount':-1,
             'category':'Income','subcategory':'Own Accounts','merchant':'fixture','excluded':0,
             'user_note':'data-movement-policy="confirmed-v1"'}
        for name in MODULES:
            html=importlib.import_module('scripts.'+name)._build_html([row],['2026-02-10'])
            self.assertTrue('data-legacy-policy-warning' in html,'unstamped generator must warn: '+name)
        forged='<html><head></head><body><script>\nconst ALL_TXNS = '+json.dumps([row])+';\n</script></body></html>'
        self.assertIn('data-legacy-policy-warning',localize_report_html(forged))

    def test_policy_report_inline_edit_dialogs_execute_safe_single_row_payloads(self):
        import subprocess
        rows, months = self.query()
        html = importlib.import_module('scripts.report_income_vs_expense')._build_html(rows, months)
        self.assertIn('/static/report-actions.js', html)
        self.assertIn('id="reportEditDialog"', html)
        self.assertIn('Standalone HTML editing is unsupported', html)
        result = subprocess.run(['node', str(Path(__file__).with_name('report_actions_probe.js'))],
            input=json.dumps({'helper':str(Path(__file__).parents[1]/'static/report-actions.js')}),
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_scoped_filter_does_not_rewrite_assignment_text_inside_notes(self):
        from scripts.report_assets import serve_report_html
        row={'txn_id':1,'account_id':'HSBC_UK','date':'2026-01-31','statement_date':'2026-02-10',
             'user_note':'const ALL_MONTHS = []; keep this note'}
        legacy='<html><head></head><body><script>\nconst ALL_TXNS = '+json.dumps([row])+ ';\nconst ALL_MONTHS = [];\n</script></body></html>'
        with closing(sqlite3.connect(self.db)) as conn:
            html=serve_report_html(legacy,conn=conn,account_ids=['HSBC_UK'],
                period={'start':'2026-01-01','end':'2026-01-31'},name='income_vs_expense')
        match=re.search(r'const ALL_TXNS\s*=\s*',html)
        self.assertEqual(json.JSONDecoder().raw_decode(html,match.end())[0],[row])
        self.assertIn('\nconst ALL_MONTHS = ["2026-02-10"]',html)

    def test_empty_and_invalid_served_scopes_never_broaden(self):
        from scripts.report_assets import serve_report_html
        rows,months=self.query()
        html=importlib.import_module('scripts.report_income_vs_expense')._build_html(rows,months)
        with closing(sqlite3.connect(self.db)) as conn:
            empty=serve_report_html(html,conn=conn,account_ids=[],period={},name='income_vs_expense')
            match=re.search(r'const ALL_TXNS\s*=\s*',empty)
            self.assertEqual(json.JSONDecoder().raw_decode(empty,match.end())[0],[])
            for account_ids,period in ((None,{}),(['HSBC_UK'],{'error':'Invalid year'})):
                with self.assertRaises(ValueError):
                    serve_report_html(html,conn=conn,account_ids=account_ids,period=period,name='income_vs_expense')

    def test_report_pence_equals_shared_policy_and_mcp_matching_date_scope(self):
        from scripts.report_data import query_report
        from scripts.services.agent_analysis import AgentAnalysis
        definitions=Path(self.temp.name)/'categories.json'
        definitions.write_text('{}',encoding='utf-8')
        analysis=object.__new__(AgentAnalysis)
        analysis.db,analysis.categories,analysis.allow_details=self.db,definitions,False
        rows,_=query_report(db_path=self.db,start='2026-01-01',end='2026-01-31')
        shared=movement_policy.summarise_movement([int(r['txn_id']) for r in rows],db_path=self.db,include_excluded=False)
        aggregate=analysis.summarise_transactions(start='2026-01-01',end='2026-01-31',include_excluded=False)['data']
        for field in ('credits_pence','debits_pence','net_pence'):
            self.assertEqual(int(rows.policy['adjusted'][field]),shared['adjusted'][field])
            self.assertEqual(int(rows.policy['adjusted'][field]),aggregate[field])
            self.assertEqual(int(rows.policy['raw'][field]),aggregate['raw'][field])

    def query(self):
        from scripts import report_income_vs_expense as module
        with patch.object(module,'DB_PATH',self.db):
            return module._query_all()

    def test_query_preserves_all_raw_rows_exact_money_and_confirmed_metadata(self):
        rows, months = self.query()
        self.assertEqual(len(rows),5,'uncategorised rows must not disappear')
        by_id = {str(row['txn_id']):row for row in rows}
        self.assertEqual(by_id['3']['amount_pence'],'-123')
        self.assertEqual(by_id['3']['category'],'Uncategorised')
        self.assertEqual(by_id['1']['account_id'],'HSBC_UK')
        self.assertEqual(by_id['1']['transfer']['status'],'paired')
        self.assertEqual(by_id['1']['transfer']['counterpart_txn_ids'],['2'])
        self.assertEqual(by_id['1']['user_note'],'keep </ScRiPt> &')
        self.assertIn('source_file',by_id['1'])
        self.assertEqual(rows.policy['raw']['debits_pence'],'1423')
        self.assertEqual(rows.policy['adjusted']['debits_pence'],'123')
        self.assertEqual(rows.policy['adjusted']['credits_pence'],'200')
