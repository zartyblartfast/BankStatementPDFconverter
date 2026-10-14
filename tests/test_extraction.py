"""Synthetic lines exercise the real parser; mocks isolate PDF reader I/O only."""
import json
import unittest
import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal

LINES = [
    (1, 'Opening balance 0.00'),
    (1, 'Payments in 100.00'),
    (1, 'Payments out 30.00'),
    (1, 'Closing balance 70.00'),
    (1, '01 Jan 26 CR SYNTHETIC CREDIT 100.00'),
    (1, '02 Jan 26 VIS SYNTHETIC SHOP 30.00'),
]


def synthetic_pdf(lines):
    """One-page Helvetica PDF fixture, no generation dependency."""
    stream = ('BT /F1 10 Tf 50 750 Td 18 TL\n' + '\n'.join(
        '(' + line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') + ') Tj T*'
        for _, line in lines) + '\nET').encode('ascii')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
    ]
    document = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(document)
    document.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        document.extend(f'{offset:010d} 00000 n \n'.encode())
    document.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(document)


class RealSyntheticPDFTests(unittest.TestCase):
    def _assert_real_readers_flag_unfinished_eof(self, unfinished):
        from scripts.services.extraction import extract_statement
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / 'synthetic.pdf'
            pdf.write_bytes(synthetic_pdf(LINES + [(1, unfinished)]))
            for extractor in ('text', 'words'):
                with self.subTest(extractor=extractor):
                    result = extract_statement(pdf, extractor=extractor)
                    self.assertEqual(result.extractor, extractor)
                    self.assertEqual(len(result.transactions), 2)
                    self.assertEqual(result.reconciliation.differences, {
                        'payments_in': Decimal('0.00'), 'payments_out': Decimal('0.00'),
                        'balance_movement': Decimal('0.00')})
                    self.assertEqual(result.warnings, [f'1:7 | no amounts found | {unfinished}'])
                    self.assertEqual(result.reconciliation.status, 'needs_review')
                    self.assertEqual(result.to_dict()['warnings_count'], 1)
                    self.assertEqual(set(p.name for p in root.iterdir()), {'synthetic.pdf'})

    def test_real_readers_dated_unfinished_eof_requires_review_despite_matching_totals(self):
        self._assert_real_readers_flag_unfinished_eof('03 Jan 26 CR SYNTHETIC INCOMPLETE')

    def test_real_readers_undated_unfinished_eof_requires_review_despite_matching_totals(self):
        self._assert_real_readers_flag_unfinished_eof('DD SYNTHETIC INCOMPLETE')

    def _assert_real_cli_flags_unfinished_eof(self, unfinished):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / 'synthetic.pdf'
            pdf.write_bytes(synthetic_pdf(LINES + [(1, unfinished)]))
            for extractor in ('text', 'words'):
                with self.subTest(extractor=extractor):
                    outdir = root / f'explicit-exports-{extractor}'
                    proc = subprocess.run([sys.executable, '-m', 'scripts.hsbc_pdf_to_txn',
                        '--pdf', str(pdf), '--extractor', extractor, '--outdir', str(outdir),
                        '--min-txns', '1'], capture_output=True, text=True)
                    self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                    for metric in ('transaction_count: 2', 'any_parse_warnings_count: 1',
                                   'reconciliation_status: needs_review',
                                   'diff_credits_vs_statement_in: 0.00',
                                   'diff_debits_vs_statement_out: 0.00',
                                   'diff_txn_delta_vs_statement_delta: 0.00'):
                        self.assertIn(metric, proc.stdout)
                    self.assertNotIn('reconciliation_status: passed', proc.stdout)
                    self.assertEqual((outdir / 'synthetic_warnings.txt').read_text().splitlines(),
                                     [f'1:7 | no amounts found | {unfinished}'])
                    exported = json.loads((outdir / 'synthetic_transactions.json').read_text())
                    self.assertEqual(len(exported), 2)
                    self.assertEqual([row['description_raw'] for row in exported],
                                     ['CR SYNTHETIC CREDIT', 'VIS SYNTHETIC SHOP'])

    def test_real_cli_dated_unfinished_eof_returns_uncertainty_not_false_pass(self):
        self._assert_real_cli_flags_unfinished_eof('03 Jan 26 CR SYNTHETIC INCOMPLETE')

    def test_real_cli_undated_unfinished_eof_returns_uncertainty_not_false_pass(self):
        self._assert_real_cli_flags_unfinished_eof('DD SYNTHETIC INCOMPLETE')

    def test_real_readers_and_cli_preserve_legacy_csv_schema(self):
        import csv
        import subprocess
        import sys
        from scripts.services.extraction import extract_statement
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / 'synthetic.pdf'
            pdf.write_bytes(synthetic_pdf(LINES))
            for extractor in ('text', 'words'):
                with self.subTest(extractor=extractor):
                    result = extract_statement(pdf, extractor=extractor)
                    self.assertEqual(result.reconciliation.status, 'passed')
                    self.assertEqual(len(result.transactions), 2)
                    self.assertEqual(set(p.name for p in root.iterdir()), {'synthetic.pdf'})
            outdir = root / 'explicit-exports'
            proc = subprocess.run([sys.executable, '-m', 'scripts.hsbc_pdf_to_txn', '--pdf', str(pdf),
                '--outdir', str(outdir), '--min-txns', '1'], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('reconciliation_status: passed', proc.stdout)
            self.assertIn('total_credits: 100.00', proc.stdout)
            with (outdir / 'synthetic_transactions.csv').open(encoding='utf-8', newline='') as exported:
                reader = csv.DictReader(exported)
                self.assertEqual(reader.fieldnames, ['source_file', 'account_id', 'txn_date',
                    'description_raw', 'amount', 'currency', 'page', 'row_ref'])
                self.assertEqual(len(list(reader)), 2)
            self.assertEqual(len(json.loads((outdir / 'synthetic_transactions.json').read_text())), 2)


class ExtractionTests(unittest.TestCase):
    def test_hsbc_standalone_date_range_is_source_coverage(self):
        from scripts.services.extraction import extract_statement
        lines = [(1, '01 Jan 2026 to 31 Jan 2026')] + LINES
        with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=lines):
            result = extract_statement('synthetic.pdf', extractor='text')
        self.assertEqual(result.coverage['end'], '2026-01-31')
        self.assertEqual(result.coverage['source_row_ref'], '1:1')

    def test_hsbc_statement_header_range_infers_year_for_both_dates(self):
        from scripts.services.extraction import extract_statement
        lines = [(1, '11 September to 10 October 2026 Bank Identifier Code')] + LINES
        with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=lines):
            result = extract_statement('synthetic.pdf', extractor='text')
        self.assertEqual(result.coverage, {
            'start': '2026-09-11',
            'end': '2026-10-10',
            'source_row_ref': '1:1',
        })

    def test_conflicting_statement_totals_do_not_silently_pass(self):
        from scripts.services.extraction import extract_statement
        with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=[(1, 'Payments in 999.00')] + LINES):
            result = extract_statement('synthetic.pdf', extractor='text')
        self.assertEqual(result.reconciliation.status, 'needs_review')
        self.assertTrue(any('conflicting' in warning.lower() for warning in result.warnings))

    def test_text_extraction_preserves_parser_rows_without_output_writes(self):
        from scripts.services.extraction import extract_statement
        with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=LINES), \
             patch('pathlib.Path.mkdir', side_effect=AssertionError('implicit directory creation')):
            result = extract_statement('synthetic.pdf', account_id='TEST', extractor='text', source_file='source-key')
        self.assertEqual(len(result.transactions), 2)
        self.assertEqual(result.transactions[0].source_file, 'source-key')
        self.assertEqual(result.transactions[0].account_id, 'TEST')
        self.assertEqual(result.transactions[0].row_ref, '1:5')
        self.assertEqual(result.indexed_all_lines[4], (1, 5, LINES[4][1]))
        self.assertEqual(result.statement_totals['payments_in'], Decimal('100.00'))
        self.assertEqual(result.reconciliation.status, 'passed')
        self.assertIsNone(result.coverage)
        payload = result.to_dict()
        self.assertEqual(payload['transaction_count'], 2)
        self.assertNotIn('transactions', payload)
        self.assertNotIn('indexed_all_lines', payload)
        self.assertNotIn('SYNTHETIC', json.dumps(payload))
        self.assertNotIn('SYNTHETIC', repr(result))
        self.assertEqual(payload['reconciliation']['parsed_totals']['net'], '70.00')

    def test_default_words_keeps_diagnostics(self):
        from scripts.services.extraction import extract_statement
        from scripts.hsbc_parser.models import WordExtractorDiagnostics
        diag = WordExtractorDiagnostics(total_pdf_words=20, total_output_lines=6)
        with patch('scripts.services.extraction.parse_pdf_to_lines_words', return_value=(LINES, diag)) as words, \
             patch('scripts.services.extraction.parse_pdf_to_lines', side_effect=AssertionError('unneeded fallback')):
            result = extract_statement('synthetic.pdf')
        words.assert_called_once_with('synthetic.pdf')
        self.assertEqual(result.extractor, 'words')
        self.assertIs(result.extractor_diag, diag)

    def test_empty_words_falls_back_to_text(self):
        from scripts.services.extraction import extract_statement
        from scripts.hsbc_parser.models import WordExtractorDiagnostics
        with patch('scripts.services.extraction.parse_pdf_to_lines_words', return_value=([], WordExtractorDiagnostics())), \
             patch('scripts.services.extraction.parse_pdf_to_lines', return_value=LINES) as text:
            result = extract_statement('synthetic.pdf')
        text.assert_called_once_with('synthetic.pdf')
        self.assertEqual(result.extractor, 'text')
        self.assertEqual(result.reconciliation.status, 'passed')
        self.assertIsNone(result.extractor_diag)

    def test_unknown_extractor_rejected_before_reader(self):
        from scripts.services.extraction import extract_statement
        with patch('scripts.services.extraction.parse_pdf_to_lines') as reader:
            with self.assertRaises(ValueError):
                extract_statement('synthetic.pdf', extractor='invented')
        reader.assert_not_called()

    def test_suspect_merged_lines_require_review_and_serialize_counts_only(self):
        from scripts.services.extraction import extract_statement
        from scripts.hsbc_parser.models import WordExtractorDiagnostics
        diag = WordExtractorDiagnostics(suspect_merged_lines=[{'page': 1, 'line': 'PRIVATE SYNTHETIC', 'token_count': 40}])
        with patch('scripts.services.extraction.parse_pdf_to_lines_words', return_value=(LINES, diag)):
            result = extract_statement('synthetic.pdf')
        self.assertEqual(result.reconciliation.status, 'needs_review')
        self.assertEqual(result.to_dict()['extractor_diagnostics']['suspect_merged_line_count'], 1)
        self.assertNotIn('PRIVATE', json.dumps(result.to_dict()))

    def test_suspect_transaction_description_requires_review(self):
        from scripts.services.extraction import extract_statement
        lines = [(1, '01 Jan 26 CR ' + 'LONG ' * 100 + '100.00')]
        with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=lines):
            result = extract_statement('synthetic.pdf', extractor='text')
        self.assertEqual(result.reconciliation.status, 'needs_review')
        self.assertEqual(result.to_dict()['suspects_count'], 1)

    def test_coverage_comes_from_explicit_statement_period_not_transaction_dates(self):
        from scripts.services.extraction import extract_statement
        lines = [(1, 'Statement period: 01 January 2026 to 31 January 2026')] + LINES
        with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=lines):
            result = extract_statement('synthetic.pdf', extractor='text')
        self.assertEqual(result.coverage, {'start': '2026-01-01', 'end': '2026-01-31', 'source_row_ref': '1:1'})
        self.assertEqual(result.to_dict()['coverage'], result.coverage)

    def test_invalid_or_conflicting_periods_are_inconclusive(self):
        from scripts.services.extraction import extract_statement
        for headers in (
            ['Statement period: 31 January 2026 to 01 January 2026'],
            ['Statement period: invalid to invalid'],
            ['Statement period: 01 January 2026 to 31 January 2026',
             'Statement period: 01 February 2026 to 28 February 2026'],
        ):
            with self.subTest(headers=headers), patch('scripts.services.extraction.parse_pdf_to_lines',
                    return_value=[(1, h) for h in headers] + LINES):
                result = extract_statement('synthetic.pdf', extractor='text')
            self.assertIsNone(result.coverage)
            self.assertEqual(result.reconciliation.status, 'needs_review')


class ExtractionCLITests(unittest.TestCase):
    def test_cli_export_error_returns_one(self):
        from scripts.services.extraction import extract_statement
        from scripts.hsbc_pdf_to_txn import main
        with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=LINES):
            result = extract_statement('synthetic.pdf', extractor='text')
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / 'synthetic.pdf'
            pdf.touch()
            with patch('scripts.hsbc_pdf_to_txn.extract_statement', return_value=result), \
                 patch('scripts.hsbc_pdf_to_txn.write_output', side_effect=PermissionError('synthetic')), \
                 self.assertLogs('hsbc_parser', level='ERROR'):
                self.assertEqual(main(['--pdf', str(pdf), '--min-txns', '1', '--quiet']), 1)

    def test_cli_uses_shared_result_preserves_exports_and_stdout_metrics(self):
        from scripts.services.extraction import extract_statement
        from scripts.hsbc_pdf_to_txn import main
        with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=LINES):
            result = extract_statement('synthetic.pdf', extractor='text')
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / 'synthetic.pdf'
            pdf.touch()
            stdout = io.StringIO()
            with patch('scripts.hsbc_pdf_to_txn.extract_statement', return_value=result, create=True) as extract, \
                 patch('scripts.hsbc_pdf_to_txn.write_output') as writer, redirect_stdout(stdout):
                code = main(['--pdf', str(pdf), '--outdir', str(Path(directory) / 'exports'), '--min-txns', '1'])
            self.assertEqual(code, 0)
            extract.assert_called_once_with(pdf, account_id='HSBC_UK', extractor='words')
            self.assertEqual(writer.call_args.kwargs['txns'], result.transactions)
            self.assertEqual(writer.call_args.kwargs['statement_totals']['payments_in'], 100.0)
            for metric in ('transaction_count: 2', 'total_credits: 100.00', 'total_debits: -30.00',
                           'diff_credits_vs_statement_in: 0.00', 'diff_debits_vs_statement_out: 0.00',
                           'diff_txn_delta_vs_statement_delta: 0.00', 'reconciliation_status: passed'):
                self.assertIn(metric, stdout.getvalue())

    def test_cli_failed_reconciliation_never_exports_and_returns_one(self):
        from scripts.services.extraction import extract_statement
        from scripts.hsbc_pdf_to_txn import main
        with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=LINES[0:4] + [(1, '01 Jan 26 CR SYNTHETIC CREDIT 101.00')]):
            result = extract_statement('synthetic.pdf', extractor='text')
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / 'synthetic.pdf'
            pdf.touch()
            with patch('scripts.hsbc_pdf_to_txn.extract_statement', return_value=result), \
                 patch('scripts.hsbc_pdf_to_txn.write_output') as writer:
                self.assertEqual(main(['--pdf', str(pdf), '--min-txns', '1', '--quiet']), 1)
            writer.assert_not_called()

    def test_cli_reader_error_and_minimum_count_return_one_without_exports(self):
        from scripts.services.extraction import extract_statement
        from scripts.hsbc_pdf_to_txn import main
        with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=LINES):
            result = extract_statement('synthetic.pdf', extractor='text')
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / 'synthetic.pdf'
            pdf.touch()
            for reader_error in (False, True):
                with self.subTest(reader_error=reader_error), \
                     patch('scripts.hsbc_pdf_to_txn.extract_statement', return_value=result,
                           side_effect=RuntimeError('synthetic reader error') if reader_error else None), \
                     patch('scripts.hsbc_pdf_to_txn.write_output') as writer:
                    self.assertEqual(main(['--pdf', str(pdf), '--quiet']), 1)
                writer.assert_not_called()

    def test_cli_review_and_unavailable_export_diagnostics_but_return_two(self):
        from scripts.services.extraction import extract_statement
        from scripts.hsbc_pdf_to_txn import main
        for lines in (LINES[4:], [LINES[1]] + LINES[4:]):
            with patch('scripts.services.extraction.parse_pdf_to_lines', return_value=lines):
                result = extract_statement('synthetic.pdf', extractor='text')
            with tempfile.TemporaryDirectory() as directory:
                pdf = Path(directory) / 'synthetic.pdf'
                pdf.touch()
                with patch('scripts.hsbc_pdf_to_txn.extract_statement', return_value=result), \
                     patch('scripts.hsbc_pdf_to_txn.write_output') as writer:
                    self.assertEqual(main(['--pdf', str(pdf), '--min-txns', '1', '--quiet']), 2)
                writer.assert_called_once()
