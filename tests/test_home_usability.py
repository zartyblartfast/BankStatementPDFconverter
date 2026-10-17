"""Synthetic rendered-template UX checks: no dashboard import or live ledger access."""
from pathlib import Path
from html.parser import HTMLParser
from typing import Any
import json
import shutil
import subprocess
import unittest
from flask import Flask, render_template

ROOT = Path(__file__).resolve().parents[1]

class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []
    def all(self, tag=None, **attrs):
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and all(child.attrs.get(k) == v for k, v in attrs.items()):
                    found.append(child)
                found.extend(child.all(tag, **attrs))
        return found
    def text(self):
        return ''.join(c.text() if isinstance(c, Node) else c for c in self.children)

class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.root = Node(); self.current = self.root; self.feed(html)
    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current); self.current.children.append(node)
        if tag not in ('input', 'br', 'meta', 'link', 'img', 'hr'): self.current = node
    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag: self.current = node.parent; break
            node = node.parent
    def handle_data(self, data): self.current.children.append(data)

def synthetic_context(**changes) -> dict[str, Any]:
    stats = dict(total_txns=4, statements=2, statement_range='2026-01 to 2026-02',
                 open_exceptions=3, total_income='9,007,199,254,740.93', total_expense='20.03',
                 net='-20.03', net_raw=-2003, last_import='2026-02-03',
                 coverage_status='partial', coverage_range='2026-01 to 2026-02',
                 coverage_complete_months=0, coverage_summary='0 complete; 1 partial; 1 missing',
                 uncategorised=2, categorisation_summary='2 uncategorised',
                 average_available=False, average_income='—', average_expense='—', average_net='—',
                 average_net_raw=0, average_summary='No complete covered months available for averages',
                 average_warnings=['Missing synthetic statement evidence <unsafe>'],
                 coverage_by_account=[dict(account_id='SYNTHETIC', complete_months=[], warnings=['Synthetic account warning'])],
                 movement=dict(raw=dict(credits_pence=12345, debits_pence=2345, net_pence=10000),
                               transfers=dict(in_pence=1000, out_pence=1000, in_count=1, out_count=1, unmatched_count=0),
                               warnings=['confirmed_counterpart_outside_scope']))
    stats.update(changes)
    return dict(stats=stats, recent_imports=[dict(source_file='<synthetic>.pdf', row_count=4, imported_at='2026-02-03')],
                accounts=[dict(account_id='SYNTHETIC', display_name='<Synthetic account>')], account_id='SYNTHETIC',
                period=dict(kind='all', year=2026, month=2, start='', end='', label='All dates'),
                active_page='home', exception_count=3)

class HomeUsabilityTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'))
        self.app.jinja_env.globals['csrf_token'] = lambda: 'synthetic-token'
    def render(self, template='home.html', context=None, **changes):
        context = context or synthetic_context(**changes)
        with self.app.test_request_context('/'):
            return render_template(template, **context)
    def test_navigation_everyday_routes_tools_active_and_keyboard(self):
        for active, route in [('home','/'), ('transactions','/transactions'), ('report','/report'),
                              ('planning','/planning'), ('ingest','/ingest'), ('exceptions','/exceptions'),
                              ('rules','/rules'), ('safety','/safety')]:
            with self.subTest(active=active):
                context = synthetic_context(); context['active_page'] = active
                doc = Document(self.render('base.html', context)).root
                nav = doc.all('nav')[0]
                labels = {a.attrs['href']: a.text().strip() for a in nav.all('a')}
                self.assertEqual(labels['/'], 'Overview')
                for url, label in [('/transactions','Transactions'),('/report','Reports'),('/planning','Planning'),
                                   ('/ingest','Import statements'),('/rules','Categorisation rules'),('/safety','Backups & safety')]:
                    self.assertEqual(labels[url], label)
                self.assertIn('Needs categorising', labels['/exceptions'])
                tools = nav.all('details')[0]
                self.assertNotIn('open', tools.attrs)
                self.assertIn('Tools', tools.all('summary')[0].text())
                self.assertEqual({a.attrs['href'] for a in tools.all('a')}, {'/rules','/safety'})
                selected = nav.all('a', href=route)[0]
                self.assertEqual(selected.attrs.get('aria-current'), 'page')
                self.assertIn('active', selected.attrs.get('class',''))
                if active in ('rules', 'safety'):
                    self.assertIn('active', tools.all('summary')[0].attrs.get('class',''))
                self.assertTrue(nav.all('span', **{'class':'badge'}))
                main = doc.all('main', id='main-content')[0]
                self.assertEqual(main.attrs.get('tabindex'), '-1')
                self.assertEqual(doc.all('a', href='#main-content')[0].text().strip(), 'Skip to main content')
                css = doc.all('style')[0].text()
                self.assertIn(':focus-visible', css)
                self.assertIn('flex-wrap:wrap', css)
                self.assertTrue(doc.all('script', src='/static/rule_actions.js'))

    def test_overview_prioritises_scoped_money_then_averages_attention_and_details(self):
        html = self.render()
        main = Document(html).root.all('main')[0]
        self.assertEqual(main.all('h1')[0].text(), 'Overview')
        headings = [n.text() for n in main.all('h2')]
        for text in ['Money in', 'Money out', 'Net money movement']:
            self.assertIn(text, main.text())
        self.assertLess(html.index('name="account_id"'), html.index('Money in'))
        self.assertLess(html.index('Money in'), html.index('Complete-month average'))
        self.assertLess(html.index('Complete-month average'), html.index('Needs attention'))
        self.assertLess(html.index('Needs attention'), html.index('Calculation details'))
        self.assertIn('£9,007,199,254,740.93', main.text())
        self.assertIn('£20.03', main.text())
        self.assertIn('-£20.03', main.text())
        self.assertIn('confirmed own-account transfers', main.text())
        self.assertIn('hidden report rows', main.text())
        self.assertIn('not an account balance', main.text())
        self.assertIn('not inferred income or spending', main.text())
        details = {d.all('summary')[0].text().strip(): d for d in main.all('details')}
        for title in ['Calculation details', 'Import activity & counts']:
            self.assertNotIn('open', details[title].attrs)
        calculation = details['Calculation details'].text()
        for text in ['Raw credits', 'Raw debits', 'Confirmed transfer in', 'Coverage by account',
                     'Missing synthetic statement evidence <unsafe>', 'Synthetic account warning',
                     'confirmed counterpart outside scope', 'every selected account']:
            self.assertIn(text, calculation)
        self.assertIn('Interpretation warnings', main.text())
        self.assertIn('consult Calculation details', main.text())
        self.assertIn('Last import (all accounts)', details['Import activity & counts'].text())
        self.assertIn('Transactions (selected account/period)', details['Import activity & counts'].text())
        self.assertIn('Import history (all accounts)', details['Import activity & counts'].text())
        for href, label in [('/ingest','Import statements'),('/report','View reports'),('/exceptions','Needs categorising')]:
            self.assertTrue(any(label == a.text().strip() for a in main.all('a', href=href)))
        self.assertNotIn('<unsafe>', html)
        self.assertNotIn('<synthetic>', html)
        self.assertIn('&lt;synthetic&gt;.pdf', html)

    def test_averages_explain_missing_evidence_without_turning_it_into_zero(self):
        html = self.render()
        average = next(n for n in Document(html).root.all('div', **{'class':'card'}) if n.all('h2') and n.all('h2')[0].text() == 'Complete-month average')
        self.assertIn('Unavailable', average.text())
        self.assertNotIn('£—', average.text())
        self.assertNotIn('£0.00', average.text())
        self.assertIn('No complete calendar months with verified statement evidence for every selected account', average.text())
        for net, expected in [('+0.00','+£0.00'), ('-20.03','-£20.03'), ('123.45','£123.45')]:
            rendered = self.render(average_available=True, average_income='1,234.56', average_expense='20.03',
                                   average_net=net, average_summary='Based on 2 complete covered months', average_warnings=[])
            text = Document(rendered).root.text()
            self.assertIn('£1,234.56', text)
            self.assertIn(expected, text)
            self.assertIn('Based on 2 complete covered months', text)
            self.assertNotIn('Unavailable', text)

    def test_attention_keeps_global_exceptions_separate_from_empty_scoped_evidence(self):
        context = synthetic_context(total_txns=0, total_income='0.00', total_expense='0.00', net='0.00', net_raw=0,
                                    uncategorised=0, categorisation_summary='0 uncategorised', coverage_status='unavailable',
                                    coverage_summary='No verified coverage')
        context['recent_imports'] = []
        context['accounts'] = []
        context['stats'].pop('movement')
        html = self.render(context=context)
        doc = Document(html).root
        attention = doc.all('section', **{'aria-labelledby':'attention-heading'})[0]
        self.assertIn('3 open exceptions (all accounts and dates)', attention.text())
        self.assertEqual(attention.all('a')[0].attrs['href'], '/exceptions')
        self.assertIn('No transactions in the selected account/period', doc.text())
        self.assertIn('No uncategorised rows in the selected account/period', attention.text())
        self.assertIn('Coverage is incomplete or unavailable', attention.text())
        self.assertIn('No imports recorded', doc.text())
        self.assertNotIn('all clear', doc.text().lower())
        for count in [0, 1]:
            context['stats']['open_exceptions'] = count
            text = Document(self.render(context=context)).root.all('section', **{'aria-labelledby':'attention-heading'})[0].text()
            self.assertIn(f'{count} open exception' + ('s' if count != 1 else '') + ' (all accounts and dates)', text)
            self.assertIn('Coverage is incomplete or unavailable', text)
        complete = self.render(coverage_status='complete', average_available=True, average_warnings=[],
                               coverage_by_account=[], movement=None, uncategorised=0)
        self.assertNotIn('Coverage is incomplete or unavailable', complete)
        self.assertNotIn('Interpretation warnings', complete)

    def test_period_fallback_keeps_all_fields_available_and_explains_presets(self):
        doc = Document(self.render('_movement_scope.html')).root
        form = doc.all('form')[0]
        self.assertEqual(form.attrs['method'], 'get')
        self.assertIn('data-movement-scope', form.attrs)
        self.assertEqual(form.all('button')[0].text(), 'Apply filters')
        self.assertEqual([n.attrs['name'] for n in form.all('select')], ['account_id', 'period'])
        self.assertEqual([n.attrs['name'] for n in form.all('input')], ['year', 'month', 'start', 'end'])
        for label in form.all('label'):
            self.assertNotIn('hidden', label.attrs)
            for control in label.all('input') + label.all('select'):
                self.assertNotIn('disabled', control.attrs)
                self.assertNotIn('hidden', control.attrs)
        self.assertEqual(form.all('input', name='year')[0].attrs['max'], '9999')
        self.assertEqual(form.all('input', name='month')[0].attrs['max'], '12')
        self.assertIn('To', next(n for n in form.all('label') if n.all('input', name='end')).text())
        self.assertIn('Year for calendar year or month; Month for calendar month; From and To for custom dates', form.text())
        script = doc.all('script', src='/static/period_scope.js')[0]
        self.assertIn('defer', script.attrs)
        self.assertIn('<Synthetic account>', form.text())
        self.assertNotIn('<Synthetic account>', self.render('_movement_scope.html'))

    @unittest.skipUnless(shutil.which('node'), 'Node required for actual period controller probe')
    def test_period_controller_disables_stale_fields_and_restores_values(self):
        controller = ROOT / 'static' / 'period_scope.js'
        node = shutil.which('node')
        assert node is not None
        self.assertTrue(controller.exists(), 'Period enhancement controller is missing')
        for kind in ['all', 'year', 'month', 'custom']:
            context = synthetic_context()
            context['period'].update(kind=kind, start='2026-01-02', end='2026-02-03')
            form = Document(self.render('_movement_scope.html', context)).root.all('form')[0]
            controls = [dict(n.attrs) for n in form.all('input') + form.all('select')]
            for n, attrs in zip(form.all('input') + form.all('select'), controls):
                if n.tag == 'select':
                    attrs['value'] = next((o.attrs['value'] for o in n.all('option') if 'selected' in o.attrs), '')
            result = subprocess.run([node, str(ROOT / 'tests' / 'period_scope_probe.js')],
                                    input=json.dumps(dict(script=controller.read_text(encoding='utf-8'), controls=controls)),
                                    text=True, capture_output=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('all presets; stale fields disabled; values retained; isolated scope', result.stdout)

if __name__ == '__main__': unittest.main()
