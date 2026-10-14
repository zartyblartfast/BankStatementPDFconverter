"""Synthetic, read-only CSV validator tests. No real bank files are used."""
import importlib
import tempfile
import subprocess
import sys
import json
import hashlib
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path


class CSVValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.api = importlib.import_module('scripts.csv_validation')

    def csv(self, text, name='bank.csv', **kwargs):
        path = self.root / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode('utf-8'))
        return self.api.load_csv_transactions(path, 'test-account', **kwargs)

    def test_parse_bom_quoted_description_and_occurrences(self):
        doc = self.csv('\ufeff01/01/2025,"Shop, Test",-12.30\n01/01/2025,"Shop, Test",-12.30\n',
                       coverage_start='2025-01-01', coverage_end='2025-01-31')
        self.assertEqual(len(doc.transactions), 2)
        a, b = doc.transactions
        self.assertEqual(a.txn_date, date(2025, 1, 1))
        self.assertEqual(a.amount, Decimal('-12.30'))
        self.assertEqual(a.description_raw, 'Shop, Test')
        self.assertEqual((a.row_ref, b.row_ref), (1, 2))
        self.assertNotEqual(a.occurrence_id, b.occurrence_id)
        self.assertEqual(doc.coverage_end, date(2025, 1, 31))

    def test_unescaped_quotes_in_unquoted_fields_fail_atomically(self):
        for description in ('Unquoted"description', 'Unquoted"', ' "Not at field start"'):
            with self.subTest(description=description):
                text = '01/01/2025,Valid,1.00\n02/01/2025,' + description + ',1.00\n'
                with self.assertRaises(self.api.CSVValidationError) as caught:
                    self.csv(text)
                self.assertEqual(str(caught.exception), 'Row 2: malformed CSV')
                self.assertNotIn(description, str(caught.exception))

    def test_characters_after_closing_quotes_fail_atomically(self):
        for description in ('"Quoted"trailing', '"Quoted" ', '"Quoted"\\"'):
            with self.subTest(description=description):
                with self.assertRaises(self.api.CSVValidationError):
                    self.csv('01/01/2025,' + description + ',1.00\n')

    def test_escaped_quotes_bom_and_multiline_preserve_occurrences(self):
        for newline in ('\n', '\r\n', '\r'):
            with self.subTest(newline=repr(newline)):
                text = ('\ufeff01/01/2025,"Shop ""Test"", branch",-1.00' + newline +
                        '02/01/2025,"""Two' + newline + 'lines""",2.00' + newline +
                        '03/01/2025,"""",3.00' + newline +
                        '04/01/2025,Next,4.00')
                doc = self.csv(text)
                self.assertEqual([t.description_raw for t in doc.transactions],
                                 ['Shop "Test", branch', '"Two' + newline + 'lines"', '"', 'Next'])
                self.assertEqual([t.row_ref for t in doc.transactions], [1, 2, 4, 5])
                self.assertEqual([t.occurrence_id for t in doc.transactions],
                                 [f'{doc.sha256}:{line}' for line in (1, 2, 4, 5)])

    def test_invalid_rows_fail_atomically(self):
        cases = [
            '01/01/2025,Shop,-1.00,extra\n', '\n',
            '31/02/2025,Shop,-1.00\n', '2025-01-01,Shop,-1.00\n',
            'date,description,amount\n', '01/01/2025, ,-1.00\n',
            '01/01/2025,Shop,1.001\n', '01/01/2025,Shop,NaN\n',
            '01/01/2025,Shop,1e2\n', '01/01/2025,Shop,1.0\n',
            '01/01/2025,Shop, 1.00\n', '01/01/2025,"unterminated,-1.00\n',
            b'01/01/2025,Shop\xff,-1.00\n', b'\xff\xfe0\x001\x00',
        ]
        for text in cases:
            with self.subTest(case=cases.index(text)):
                with self.assertRaises(self.api.CSVValidationError):
                    self.csv(text)
        with self.assertRaises(self.api.CSVValidationError):
            self.csv('01/01/2025,Shop,1.00\n', coverage_start='2025-02-01',
                     coverage_end='2025-01-01')

    def txn(self, amount='-12.30', description='Shop Test', day='2025-01-01', **extra):
        return dict(txn_date=day, description_raw=description, amount=amount, **extra)

    def compare(self, pdf, docs, **kwargs):
        options = dict(account_id='test-account', coverage_start='2025-01-01',
                       coverage_end='2025-01-31', pdf_coverage_start='2025-01-01',
                       pdf_coverage_end='2025-01-31', pdf_complete=True)
        options.update(kwargs)
        return self.api.compare_transactions(pdf, docs, **options)

    def covered_csv(self, text, name='bank.csv', **kwargs):
        options = dict(coverage_start='2025-01-01', coverage_end='2025-01-31')
        options.update(kwargs)
        return self.csv(text, name, **options)

    def test_exact_match_preserves_equal_payment_multiplicity(self):
        doc = self.covered_csv('01/01/2025,Shop Test,-12.30\n' * 2)
        result = self.compare([self.txn(), self.txn(description=' SHOP  Test ')], [doc])
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['counts']['matched'], 2)
        self.assertEqual(result['totals']['csv']['count'], 2)
        self.assertEqual(result['totals']['csv']['expense'], '24.60')
        self.assertEqual(result['totals']['csv']['net'], '-24.60')
        # Identical occurrences are an aggregate match, not invented one-to-one IDs.
        self.assertEqual(len(result['matched'][0]['pdf']), 2)
        self.assertEqual(len(result['matched'][0]['csv']), 2)
        self.assertEqual(result['pdf_only'], [])
        self.assertEqual(result['csv_only'], [])

    def test_missing_or_incomplete_coverage_never_passes(self):
        doc = self.covered_csv('01/01/2025,Shop Test,-12.30\n')
        cases = [dict(account_id=None), dict(coverage_start=None), dict(coverage_end=None),
                 dict(pdf_coverage_start=None), dict(pdf_coverage_end=None),
                 dict(pdf_complete=False), dict(pdf_complete='true'),
                 dict(pdf_coverage_start='2025-01-02'), dict(pdf_coverage_end='2025-01-30'),
                 dict(coverage_start='2025-02-01')]
        for options in cases:
            with self.subTest(options=options):
                report = self.compare([self.txn()], [doc], **options)
                self.assertEqual(report['status'], 'unavailable')
                self.assertTrue(report['coverage_warnings'])
                self.assertEqual(report['matched'], [])
        unknown = self.csv('01/01/2025,Shop Test,-12.30\n')
        self.assertEqual(self.compare([self.txn()], [unknown])['status'], 'unavailable')
        self.assertEqual(self.compare([], [])['status'], 'unavailable')

    def test_account_mismatch_never_compares(self):
        doc = self.covered_csv('01/01/2025,Shop Test,-12.30\n')
        for options in [dict(pdf_account_id='other'), {}]:
            pdf = [self.txn(account_id='other')] if not options else [self.txn()]
            self.assertEqual(self.compare(pdf, [doc], **options)['status'], 'unavailable')
        from dataclasses import replace
        self.assertEqual(self.compare([self.txn()], [replace(doc, account_id='other')])['status'],
                         'unavailable')
        self.assertEqual(self.compare([self.txn()], [replace(doc, complete=False)])['status'],
                         'unavailable')

    def test_inclusive_boundaries_and_out_of_requested_period(self):
        doc = self.covered_csv('01/01/2025,First,-1.00\n31/01/2025,Last,2.00\n'
                               '01/02/2025,Outside,3.00\n', coverage_end='2025-02-28')
        pdf = [self.txn('-1.00', 'First'), self.txn('2.00', 'Last', '2025-01-31'),
               self.txn('5.00', 'Different outside', '2025-02-01')]
        report = self.compare(pdf, [doc], pdf_coverage_end='2025-02-28')
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['counts']['matched'], 2)
        self.assertEqual(report['excluded_outside_period'], dict(pdf=1, csv=1))

    def test_coverage_gap_and_transactions_outside_declared_window(self):
        a = self.covered_csv('01/01/2025,First,1.00\n', 'a.csv', coverage_end='2025-01-14')
        b = self.covered_csv('31/01/2025,Last,2.00\n', 'b.csv', coverage_start='2025-01-16')
        report = self.compare([], [a, b])
        self.assertEqual(report['status'], 'unavailable')
        self.assertIn('csv_coverage_gap', [w['code'] for w in report['coverage_warnings']])
        bad = self.covered_csv('01/02/2025,Outside,1.00\n')
        self.assertEqual(self.compare([], [bad])['status'], 'unavailable')

    def test_invalid_pdf_amounts_and_dates_are_unavailable(self):
        doc = self.covered_csv('01/01/2025,Shop Test,-12.30\n')
        for amount in [12.3, '-12.300', 'NaN', Decimal('Infinity')]:
            self.assertEqual(self.compare([self.txn(amount)], [doc])['status'], 'unavailable')
        self.assertEqual(self.compare([self.txn(day='2025-02-30')], [doc])['status'], 'unavailable')

    def test_identical_entire_files_skip_one_not_equal_payments(self):
        text = '01/01/2025,Shop Test,-12.30\n' * 2
        a, b = self.covered_csv(text, 'a.csv'), self.covered_csv(text, 'b.csv')
        report = self.compare([self.txn(), self.txn()], [a, b])
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['totals']['csv']['count'], 2)
        self.assertEqual(len(report['skipped_duplicates']), 1)

    def test_partly_overlapping_differing_downloads_require_review(self):
        a = self.covered_csv('01/01/2025,First,1.00\n15/01/2025,Repeated,-2.00\n',
                             'a.csv', coverage_end='2025-01-20')
        b = self.covered_csv('15/01/2025,Repeated,-2.00\n31/01/2025,Last,3.00\n',
                             'b.csv', coverage_start='2025-01-10')
        report = self.compare([], [a, b])
        self.assertEqual(report['status'], 'needs_review')
        self.assertEqual(report['reason'], 'overlap_requires_review')
        self.assertEqual(len(report['overlap_requires_review']), 1)
        self.assertTrue(report['overlap_requires_review'][0]['rows'])
        self.assertIsNone(report['totals']['csv'])  # No silent combined count/totals.
        self.assertEqual(report['matched'], [])

    def test_overlap_review_counts_outside_rows_after_exact_file_skip(self):
        a = self.covered_csv('01/01/2025,First,1.00\n01/02/2025,Outside A,2.00\n',
                             'a.csv', coverage_end='2025-02-10')
        duplicate = self.covered_csv('01/01/2025,First,1.00\n01/02/2025,Outside A,2.00\n',
                                     'duplicate.csv', coverage_end='2025-02-10')
        b = self.covered_csv('15/01/2025,Second,3.00\n15/02/2025,Outside B,4.00\n',
                             'b.csv', coverage_start='2025-01-10', coverage_end='2025-02-28')
        report = self.compare([self.txn('1.00', 'First'),
                               self.txn('5.00', 'Outside PDF', '2025-02-01')],
                              [a, duplicate, b], pdf_coverage_end='2025-02-28')
        self.assertEqual(report['status'], 'needs_review')
        self.assertEqual(report['reason'], 'overlap_requires_review')
        self.assertIsNone(report['totals']['csv'])
        self.assertTrue(report['totals_provisional'])
        self.assertEqual(report['totals']['pdf']['count'], 1)
        self.assertEqual([d['totals']['count'] for d in report['csv_document_totals']], [1, 1])
        self.assertEqual(len(report['skipped_duplicates']), 1)
        self.assertEqual(len(report['overlap_requires_review']), 1)
        self.assertEqual(report['matched'], [])
        self.assertEqual(report['excluded_outside_period'], dict(pdf=1, csv=2))

    def test_overlap_without_shared_rows_still_requires_review(self):
        a = self.covered_csv('01/01/2025,First,1.00\n', 'a.csv')
        b = self.covered_csv('31/01/2025,Last,3.00\n', 'b.csv')
        self.assertEqual(self.compare([], [a, b])['reason'], 'overlap_requires_review')

    def test_adjacent_nonoverlapping_windows_combine(self):
        a = self.covered_csv('01/01/2025,First,1.00\n', 'a.csv', coverage_end='2025-01-15')
        b = self.covered_csv('31/01/2025,Last,3.00\n', 'b.csv', coverage_start='2025-01-16')
        report = self.compare([self.txn('1.00', 'First'), self.txn('3.00', 'Last', '2025-01-31')], [a, b])
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['totals']['csv']['income'], '4.00')

    def test_description_variation_is_candidate_not_forced_match(self):
        doc = self.covered_csv('01/01/2025,Shop Ref123,-12.30\n')
        report = self.compare([self.txn(description='Shop Ref999')], [doc])
        self.assertEqual(report['status'], 'needs_review')
        self.assertEqual(report['counts']['matched'], 0)
        self.assertEqual(report['ambiguous'][0]['reason'], 'description_candidate')
        self.assertEqual(report['counts']['ambiguous'], 1)
        self.assertEqual(report['pdf_only'], [])

    def test_unequal_description_candidates_fail_with_unpaired_refs(self):
        for pdf_count, csv_count in ((2, 1), (1, 2)):
            with self.subTest(pdf_count=pdf_count, csv_count=csv_count):
                doc = self.covered_csv('01/01/2025,Bank naming,-12.30\n' * csv_count)
                pdf = [self.txn(description='PDF naming', row_ref=f'line-{i}',
                                source='synthetic-pdf') for i in range(pdf_count)]
                report = self.compare(pdf, [doc])
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['counts'],
                                 dict(matched=0, pdf_only=0, csv_only=0, ambiguous=1))
                group = report['ambiguous'][0]
                self.assertEqual(group['reason'], 'description_candidate')
                self.assertEqual([t['row_ref'] for t in group['pdf']],
                                 [f'line-{i}' for i in range(pdf_count)])
                self.assertEqual([t['occurrence_id'] for t in group['pdf']],
                                 [f'pdf:{i + 1}' for i in range(pdf_count)])
                self.assertEqual([t['source'] for t in group['pdf']],
                                 ['synthetic-pdf'] * pdf_count)
                self.assertEqual([t['row_ref'] for t in group['csv']],
                                 list(range(1, csv_count + 1)))
                self.assertEqual([t['occurrence_id'] for t in group['csv']],
                                 [t.occurrence_id for t in doc.transactions])
                self.assertEqual([t['description_raw'] for t in group['csv']],
                                 ['Bank naming'] * csv_count)
                self.assertEqual(report['totals']['pdf']['count'], pdf_count)
                self.assertEqual(report['totals']['csv']['count'], csv_count)
                self.assertNotEqual(report['totals']['pdf']['expense'],
                                    report['totals']['csv']['expense'])
                self.assertFalse(report['totals_provisional'])

    def test_candidate_occurrence_shortages_fail_even_when_all_totals_cancel(self):
        for amount in ('1.00', '-1.00', '0.00'):
            for split_windows in (False, True):
                with self.subTest(amount=amount, split_windows=split_windows):
                    first = f'01/01/2025,Bank first,{amount}\n'
                    last = f'31/01/2025,Bank last,{amount}\n' * 2
                    if split_windows:
                        docs = [self.covered_csv(first, 'first.csv', coverage_end='2025-01-15'),
                                self.covered_csv(last, 'last.csv', coverage_start='2025-01-16')]
                    else:
                        docs = [self.covered_csv(first + last)]
                    pdf = [self.txn(amount, 'PDF first', row_ref='first-a'),
                           self.txn(amount, 'PDF first', row_ref='first-b'),
                           self.txn(amount, 'PDF last', '2025-01-31', row_ref='last-a')]
                    report = self.compare(pdf, docs)
                    self.assertEqual(report['totals']['pdf'], report['totals']['csv'])
                    self.assertEqual(report['status'], 'failed')
                    self.assertEqual(report['counts'],
                                     dict(matched=0, pdf_only=0, csv_only=0, ambiguous=2))
                    self.assertEqual([(len(g['pdf']), len(g['csv']))
                                      for g in report['ambiguous']], [(2, 1), (1, 2)])
                    self.assertEqual([g['reason'] for g in report['ambiguous']],
                                     ['description_candidate', 'description_candidate'])
                    self.assertEqual([t['row_ref'] for g in report['ambiguous'] for t in g['pdf']],
                                     ['first-a', 'first-b', 'last-a'])
                    self.assertEqual([t['occurrence_id'] for g in report['ambiguous'] for t in g['csv']],
                                     [t.occurrence_id for d in docs for t in d.transactions])
                    self.assertEqual(report['overlap_requires_review'], [])

    def test_equal_cardinality_description_candidates_remain_review(self):
        doc = self.covered_csv('01/01/2025,Bank naming,-12.30\n' * 2)
        report = self.compare([self.txn(description='PDF naming', row_ref='a'),
                               self.txn(description='PDF naming', row_ref='b')], [doc])
        self.assertEqual(report['status'], 'needs_review')
        self.assertEqual(report['totals']['pdf'], report['totals']['csv'])
        self.assertEqual(report['counts'], dict(matched=0, pdf_only=0, csv_only=0, ambiguous=1))
        self.assertEqual([t['row_ref'] for t in report['ambiguous'][0]['pdf']], ['a', 'b'])
        self.assertEqual(len(report['ambiguous'][0]['csv']), 2)

    def test_overlap_does_not_assert_candidate_shortages_or_combined_totals(self):
        a = self.covered_csv('01/01/2025,Bank naming,-12.30\n', 'a.csv')
        b = self.covered_csv('01/01/2025,Other bank naming,-12.30\n' * 2, 'b.csv')
        report = self.compare([self.txn(description='PDF naming')], [a, b])
        self.assertEqual(report['status'], 'needs_review')
        self.assertEqual(report['reason'], 'overlap_requires_review')
        self.assertIsNone(report['totals']['csv'])
        self.assertTrue(report['totals_provisional'])
        self.assertEqual(report['counts'], dict(matched=0, pdf_only=0, csv_only=0, ambiguous=0))
        self.assertEqual([d['totals']['count'] for d in report['csv_document_totals']], [1, 2])
        self.assertEqual(len(report['overlap_requires_review'][0]['rows']), 3)

    def test_unequal_identical_duplicates_are_not_paired(self):
        doc = self.covered_csv('01/01/2025,Shop Test,-12.30\n')
        report = self.compare([self.txn(), self.txn()], [doc])
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['matched'], [])
        self.assertEqual(report['ambiguous'][0]['reason'], 'multiplicity_mismatch')
        self.assertEqual(len(report['ambiguous'][0]['pdf']), 2)
        self.assertEqual(len(report['ambiguous'][0]['csv']), 1)

    def test_net_cancelling_gross_differences_fail(self):
        doc = self.covered_csv('01/01/2025,Credit,20.00\n02/01/2025,Debit,-20.00\n')
        pdf = [self.txn('10.00', 'Credit'), self.txn('-10.00', 'Debit', '2025-01-02')]
        report = self.compare(pdf, [doc])
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['counts']['pdf_only'], 2)
        self.assertEqual(report['counts']['csv_only'], 2)
        self.assertEqual(report['totals']['pdf']['net'], report['totals']['csv']['net'])
        self.assertNotEqual(report['totals']['pdf']['income'], report['totals']['csv']['income'])
        self.assertEqual(report['totals']['pdf']['expense'], '10.00')

    def test_exact_matching_precedes_ambiguous_candidates(self):
        doc = self.covered_csv('01/01/2025,Shop Test,-12.30\n01/01/2025,Other,-12.30\n')
        report = self.compare([self.txn(), self.txn(description='Other revised')], [doc])
        self.assertEqual(report['counts']['matched'], 1)
        self.assertEqual(report['counts']['ambiguous'], 1)
        self.assertEqual(report['status'], 'needs_review')

    def test_large_decimal_totals_do_not_round_to_default_context(self):
        amount = '123456789012345678901234567890.12'
        doc = self.covered_csv(f'01/01/2025,Huge,{amount}\n' * 2)
        report = self.compare([self.txn(amount, 'Huge'), self.txn(amount, 'Huge')], [doc])
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['totals']['csv']['income'], '246913578024691357802469135780.24')

    def cli(self, description='PRIVATE DESCRIPTION', complete=True, out=None, csv_bytes=None,
            pdf_rows=None):
        bank = self.root / 'private-bank-filename.csv'
        bank.write_bytes(csv_bytes if csv_bytes is not None else
                         f'01/01/2025,{description},-12.30\n'.encode())
        pdf = self.root / 'private-pdf-filename.json'
        pdf.write_text(json.dumps(dict(account_id='test-account', coverage_start='2025-01-01',
                                       coverage_end='2025-01-31', complete=complete,
                                       transactions=([self.txn(description=description)]
                                                     if pdf_rows is None else pdf_rows))), encoding='utf-8')
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (bank, pdf)]
        args = [sys.executable, '-m', 'scripts.csv_validation', '--pdf-json', str(pdf),
                '--csv', str(bank), '--account-id', 'test-account',
                '--from-date', '2025-01-01', '--to-date', '2025-01-31']
        if out is not None:
            args += ['--out', str(out)]
        result = subprocess.run(args, capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in (bank, pdf)])
        for secret in ('PRIVATE DESCRIPTION', 'private-bank-filename', 'private-pdf-filename', '-12.30'):
            self.assertNotIn(secret, result.stdout + result.stderr)
        return result

    def test_cli_summary_and_explicit_local_json_report(self):
        out = self.root / 'report.json'
        result = self.cli(out=out)
        self.assertEqual(result.returncode, 0)
        self.assertIn('status=passed', result.stdout)
        report = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(report['counts']['matched'], 1)
        self.assertEqual(report['matched'][0]['csv'][0]['description_raw'], 'PRIVATE DESCRIPTION')

    def test_cli_html_escapes_untrusted_descriptions(self):
        out = self.root / 'report.html'
        result = self.cli(description='</pre><script>alert(1)</script>', out=out)
        self.assertEqual(result.returncode, 0)
        text = out.read_text(encoding='utf-8')
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)

    def test_cli_malformed_or_incomplete_inputs_never_pass(self):
        for options in [dict(complete=False), dict(csv_bytes=b'bad,row\n'),
                        dict(csv_bytes=b'01/01/2025,Unquoted"description,-12.30\n')]:
            result = self.cli(**options)
            self.assertEqual(result.returncode, 3)
            self.assertIn('status=unavailable', result.stdout)

    def test_cli_refuses_existing_report_without_modification(self):
        out = self.root / 'report.json'
        out.write_text('existing', encoding='utf-8')
        result = self.cli(out=out)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(out.read_text(encoding='utf-8'), 'existing')
        self.assertIn('status=unavailable', result.stdout)

    def test_cli_unavailable_report_has_stable_schema(self):
        out = self.root / 'unavailable.json'
        result = self.cli(csv_bytes=b'bad,row\n', out=out)
        self.assertEqual(result.returncode, 3)
        report = json.loads(out.read_text(encoding='utf-8'))
        for key in ('totals', 'totals_provisional', 'overlap_requires_review',
                    'skipped_duplicates', 'excluded_outside_period', 'coverage_start', 'coverage_end'):
            self.assertIn(key, report)
        self.assertIsNone(report['totals']['csv'])

    def test_pdf_source_row_refs_and_signs_are_preserved(self):
        doc = self.covered_csv('01/01/2025,Shop Test,12.30\n')
        report = self.compare([self.txn(row_ref='line-42', source='synthetic-pdf')], [doc])
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['pdf_only'][0]['row_ref'], 'line-42')
        self.assertEqual(report['pdf_only'][0]['source'], 'synthetic-pdf')
        self.assertEqual(report['totals']['pdf']['expense'], '12.30')
        self.assertEqual(report['totals']['csv']['income'], '12.30')

    def test_multiline_source_refs_are_start_physical_lines(self):
        doc = self.csv('01/01/2025,"Two\nlines",1.00\n02/01/2025,Next,2.00\n')
        self.assertEqual([t.row_ref for t in doc.transactions], [1, 3])

    def test_invalid_pdf_row_refs_cannot_break_json_safe_reports(self):
        doc = self.covered_csv('01/01/2025,Shop Test,-12.30\n')
        for row_ref in (object(), Decimal('1.00'), True, None):
            report = self.compare([self.txn(row_ref=row_ref)], [doc])
            self.assertEqual(report['status'], 'unavailable')
            json.dumps(report)

    def test_cli_unequal_candidates_fail_and_keep_refs_in_report(self):
        for pdf_count, csv_count in ((2, 1), (1, 2)):
            with self.subTest(pdf_count=pdf_count, csv_count=csv_count):
                out = self.root / f'candidate-{pdf_count}-{csv_count}.json'
                anchor = self.txn('5.00', 'Exact anchor', '2025-01-02', row_ref='anchor')
                pdf = [self.txn(description='PRIVATE DESCRIPTION', row_ref=f'line-{i}')
                       for i in range(pdf_count)] + [anchor]
                bank = (b'01/01/2025,Bank naming,-12.30\n' * csv_count +
                        b'02/01/2025,Exact anchor,5.00\n')
                result = self.cli(pdf_rows=pdf, csv_bytes=bank, out=out)
                self.assertEqual(result.returncode, 1)
                self.assertIn('status=failed matched=1 pdf_only=0 csv_only=0 ambiguous_groups=1',
                              result.stdout)
                report = json.loads(out.read_text(encoding='utf-8'))
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['counts'],
                                 dict(matched=1, pdf_only=0, csv_only=0, ambiguous=1))
                self.assertEqual([t['row_ref'] for t in report['ambiguous'][0]['pdf']],
                                 [f'line-{i}' for i in range(pdf_count)])
                self.assertEqual([t['row_ref'] for t in report['ambiguous'][0]['csv']],
                                 list(range(1, csv_count + 1)))

    def test_ambiguous_gross_discrepancies_fail_despite_equal_net(self):
        doc = self.covered_csv('01/01/2025,Bank credit,1.00\n' * 2 +
                               '02/01/2025,Bank debit,-1.00\n' * 2)
        pdf = [self.txn('1.00', 'PDF credit'),
               self.txn('-1.00', 'PDF debit', '2025-01-02')]
        report = self.compare(pdf, [doc])
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['totals']['pdf']['net'], report['totals']['csv']['net'])
        self.assertNotEqual(report['totals']['pdf']['income'], report['totals']['csv']['income'])
        self.assertNotEqual(report['totals']['pdf']['expense'], report['totals']['csv']['expense'])
        self.assertEqual(report['counts'], dict(matched=0, pdf_only=0, csv_only=0, ambiguous=2))

    def test_cli_difference_and_candidate_exit_codes(self):
        failed = self.cli(csv_bytes=b'01/01/2025,PRIVATE DESCRIPTION,12.30\n')
        review = self.cli(csv_bytes=b'01/01/2025,Changed description,-12.30\n')
        self.assertEqual(failed.returncode, 1)
        self.assertIn('status=failed', failed.stdout)
        self.assertEqual(review.returncode, 2)
        self.assertIn('status=needs_review', review.stdout)


if __name__ == '__main__':
    unittest.main()
