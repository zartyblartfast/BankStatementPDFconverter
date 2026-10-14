"""Standalone read-only bank CSV validation. No ledger or extraction imports."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path


class CSVValidationError(ValueError):
    """Invalid input; messages deliberately omit paths and financial contents."""


def _date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise CSVValidationError('Expected ISO date YYYY-MM-DD')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CSVValidationError('Invalid calendar date') from exc


@dataclass(frozen=True)
class Transaction:
    txn_date: date
    description_raw: str
    amount: Decimal
    account_id: str
    source: str
    row_ref: int | str
    occurrence_id: str


@dataclass(frozen=True)
class CSVDocument:
    account_id: str
    coverage_start: date | None
    coverage_end: date | None
    transactions: tuple[Transaction, ...]
    source: str
    sha256: str
    complete: bool = True


def _strict_csv_lines(text):
    """Validate quote placement; csv.reader(strict=True) permits bare quotes.

    Keep physical lines unchanged and quote state across multiline fields.
    The reader still handles column decoding and unterminated quoted fields.
    """
    state = 'start'
    for line in io.StringIO(text, newline=''):
        for char in line:
            if state == 'quoted':
                if char == '"':
                    state = 'closed'
            elif char == '"':
                if state not in ('start', 'closed'):
                    raise csv.Error('Quote in unquoted field')
                state = 'quoted'
            elif char in ',\r\n':
                state = 'start'
            else:
                if state == 'closed':
                    raise csv.Error('Character after closing quote')
                state = 'unquoted'
        yield line


def load_csv_transactions(path, account_id, coverage_start=None, coverage_end=None):
    """Read headerless UTF-8[-BOM] date,description,signed-two-decimal CSV.

    Coverage is a caller declaration from the download request, never inferred.
    Row refs are the one-based start physical line (including quoted multiline rows).
    Invalid files raise CSVValidationError atomically: no partial document returned.
    """
    if not isinstance(account_id, str) or not account_id.strip():
        raise CSVValidationError('An explicit account identifier is required')
    start = _date(coverage_start) if coverage_start is not None else None
    end = _date(coverage_end) if coverage_end is not None else None
    if start and end and start > end:
        raise CSVValidationError('Coverage start is after end')
    source = str(Path(path))
    try:
        data = Path(path).read_bytes()
        text = data.decode('utf-8-sig')
    except (OSError, UnicodeError) as exc:
        raise CSVValidationError('CSV unavailable or not valid UTF-8') from exc
    digest = hashlib.sha256(data).hexdigest()
    reader = csv.reader(_strict_csv_lines(text), strict=True)
    rows = []
    try:
        while True:
            row_ref = reader.line_num + 1
            try:
                row = next(reader)
            except StopIteration:
                break
            if len(row) != 3:
                raise CSVValidationError(f'Row {row_ref}: expected exactly three columns')
            if not re.fullmatch(r'\d{2}/\d{2}/\d{4}', row[0]):
                raise CSVValidationError(f'Row {row_ref}: expected DD/MM/YYYY date')
            try:
                txn_date = datetime.strptime(row[0], '%d/%m/%Y').date()
            except ValueError as exc:
                raise CSVValidationError(f'Row {row_ref}: invalid date') from exc
            if not row[1].strip() or '\x00' in row[1] or '\ufeff' in row[1]:
                raise CSVValidationError(f'Row {row_ref}: invalid description')
            if not re.fullmatch(r'[+-]?\d+\.\d{2}', row[2]):
                raise CSVValidationError(f'Row {row_ref}: expected signed decimal cents')
            rows.append(Transaction(txn_date, row[1], Decimal(row[2]), account_id,
                                    source, row_ref, f'{digest}:{row_ref}'))
    except csv.Error as exc:
        raise CSVValidationError(f'Row {row_ref}: malformed CSV') from exc
    return CSVDocument(account_id, start, end, tuple(rows), source, digest)


def _pdf_rows(rows, account_id):
    result = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            raise CSVValidationError('PDF transactions must be mappings')
        amount = row.get('amount')
        # Never convert a float to Decimal: that could legitimise prior rounding.
        if not isinstance(amount, (str, Decimal)) or not re.fullmatch(
                r'[+-]?\d+\.\d{2}', str(amount)):
            raise CSVValidationError('PDF amount must be decimal cents (string or Decimal)')
        description = row.get('description_raw')
        if not isinstance(description, str) or not description.strip():
            raise CSVValidationError('PDF description is required')
        row_ref = row.get('row_ref', index)
        if isinstance(row_ref, bool) or not isinstance(row_ref, (str, int)):
            raise CSVValidationError('PDF row_ref must be a string or integer')
        result.append(Transaction(_date(row.get('txn_date')), description, Decimal(amount),
                                  row.get('account_id', account_id), str(row.get('source', 'pdf')),
                                  row_ref, f'pdf:{index}'))
    return result


def _normalise(description):
    return ' '.join(description.casefold().split())


def _key(txn):
    return txn.txn_date, txn.amount, _normalise(txn.description_raw)


def _detail(txn):
    return dict(txn_date=txn.txn_date.isoformat(), description_raw=txn.description_raw,
                amount=format(txn.amount, '.2f'), account_id=txn.account_id,
                source=txn.source, row_ref=txn.row_ref, occurrence_id=txn.occurrence_id)


def _totals(rows):
    # Context is sized from actual inputs, avoiding default-precision rounding.
    with localcontext() as ctx:
        ctx.prec = max([len(t.amount.as_tuple().digits) for t in rows] + [1]) + len(str(len(rows))) + 4
        income = sum((t.amount for t in rows if t.amount > 0), Decimal('0.00'))
        expense = -sum((t.amount for t in rows if t.amount < 0), Decimal('0.00'))
        return dict(count=len(rows), income=format(income, '.2f'),
                    expense=format(expense, '.2f'), net=format(income - expense, '.2f'))


def _empty_report(account_id=None):
    return dict(status='unavailable', reason=None, account_id=account_id,
                coverage_start=None, coverage_end=None, matched=[], pdf_only=[], csv_only=[],
                ambiguous=[], coverage_warnings=[], skipped_duplicates=[], overlap_requires_review=[],
                csv_document_totals=[],
                counts=dict(matched=0, pdf_only=0, csv_only=0, ambiguous=0),
                totals=dict(pdf=None, csv=None), totals_provisional=False,
                excluded_outside_period=dict(pdf=0, csv=0))


def compare_transactions(pdf_transactions, csv_documents, *, account_id=None,
                         coverage_start=None, coverage_end=None,
                         pdf_coverage_start=None, pdf_coverage_end=None,
                         pdf_complete=False, pdf_account_id=None):
    """Return JSON-safe findings; explicit inclusive account/window required.

    PDF rows are mappings with txn_date (ISO/date), description_raw, amount
    (Decimal or two-decimal string), optional account_id/source/row_ref.
    Equal duplicate groups match as a multiset, without inventing pair identities.
    """
    report = _empty_report(account_id)
    warnings = report['coverage_warnings']
    try:
        start, end = _date(coverage_start), _date(coverage_end)
        ps, pe = _date(pdf_coverage_start), _date(pdf_coverage_end)
        if start > end or ps > pe:
            raise CSVValidationError('Invalid interval')
        report.update(coverage_start=start.isoformat(), coverage_end=end.isoformat())
        if not isinstance(account_id, str) or not account_id.strip():
            warnings.append(dict(code='account_required'))
        if pdf_account_id is not None and pdf_account_id != account_id:
            warnings.append(dict(code='pdf_account_mismatch'))
        if pdf_complete is not True or ps > start or pe < end:
            warnings.append(dict(code='pdf_coverage_incomplete'))
        all_pdf = _pdf_rows(pdf_transactions, account_id)
        docs = list(csv_documents)
        if any(t.account_id != account_id for t in all_pdf):
            warnings.append(dict(code='pdf_account_mismatch'))
        if any(not ps <= t.txn_date <= pe for t in all_pdf):
            warnings.append(dict(code='pdf_row_outside_declared_coverage'))
        intervals = []
        for d in docs:
            if not isinstance(d, CSVDocument):
                raise CSVValidationError('Expected CSVDocument')
            if d.account_id != account_id or any(t.account_id != account_id for t in d.transactions):
                warnings.append(dict(code='csv_account_mismatch', source=d.source))
            if d.complete is not True or d.coverage_start is None or d.coverage_end is None:
                warnings.append(dict(code='csv_coverage_unknown', source=d.source))
                continue
            ds, de = _date(d.coverage_start), _date(d.coverage_end)
            if ds > de:
                raise CSVValidationError('Invalid CSV interval')
            if any(not ds <= t.txn_date <= de for t in d.transactions):
                warnings.append(dict(code='csv_row_outside_declared_coverage', source=d.source))
            if ds <= end and de >= start:
                intervals.append((max(start, ds).toordinal(), min(end, de).toordinal()))
        cursor = start.toordinal()
        for a, b in sorted(intervals):
            if a > cursor:
                break
            cursor = max(cursor, b + 1)
        if cursor <= end.toordinal():
            warnings.append(dict(code='csv_coverage_gap'))
    except (CSVValidationError, TypeError, AttributeError) as exc:
        warnings.append(dict(code='invalid_or_missing_input'))
        report['reason'] = 'invalid_or_missing_input'
        return report
    if warnings:
        report['reason'] = 'coverage_unavailable'
        return report
    # Only byte-identical files with identical provenance declarations are skipped.
    # An equal row fingerprint is NOT evidence of a duplicate transaction.
    unique, seen = [], {}
    for d in docs:
        identity = (d.account_id, d.coverage_start, d.coverage_end, d.sha256)
        if identity in seen:
            report['skipped_duplicates'].append(dict(source=d.source,
                                                     duplicate_of=seen[identity].source))
        else:
            seen[identity] = d
            unique.append(d)
    docs = unique
    for index, a in enumerate(docs):
        for b in docs[index + 1:]:
            lo = max(a.coverage_start, b.coverage_start, start)
            hi = min(a.coverage_end, b.coverage_end, end)
            if lo <= hi:
                refs = [_detail(t) for d in (a, b) for t in d.transactions if lo <= t.txn_date <= hi]
                report['overlap_requires_review'].append(dict(
                    sources=[a.source, b.source], coverage_start=lo.isoformat(),
                    coverage_end=hi.isoformat(), rows=refs))
    pdf = [t for t in all_pdf if start <= t.txn_date <= end]
    bank_all = [t for d in docs for t in d.transactions]
    bank = [t for t in bank_all if start <= t.txn_date <= end]
    report['excluded_outside_period'] = dict(pdf=len(all_pdf) - len(pdf), csv=len(bank_all) - len(bank))
    if report['overlap_requires_review']:
        report.update(status='needs_review', reason='overlap_requires_review',
                      totals=dict(pdf=_totals(pdf), csv=None), totals_provisional=True)
        report['csv_document_totals'] = [dict(source=d.source, totals=_totals(
            [t for t in d.transactions if start <= t.txn_date <= end])) for d in docs]
        return report
    pg, cg = defaultdict(list), defaultdict(list)
    for t in pdf:
        pg[_key(t)].append(t)
    for t in bank:
        cg[_key(t)].append(t)
    matched, pdf_only, csv_only, ambiguous = [], [], [], []
    remaining_p, remaining_c = defaultdict(list), defaultdict(list)
    multiplicity_mismatch = False
    for key in sorted(pg.keys() | cg.keys()):
        p, c = pg[key], cg[key]
        if p and len(p) == len(c):
            matched.append(dict(count=len(p), pdf=[_detail(t) for t in p],
                                csv=[_detail(t) for t in c]))
        elif p and c:
            multiplicity_mismatch = True
            ambiguous.append(dict(reason='multiplicity_mismatch', pdf=[_detail(t) for t in p],
                                  csv=[_detail(t) for t in c]))
        else:
            remaining_p[key[:2]].extend(p)
            remaining_c[key[:2]].extend(c)
    # Weaker same-date/same-amount groups are suggestions only; references remain
    # intact and descriptions are never stripped to fabricate merchant identity.
    for key in sorted(remaining_p.keys() | remaining_c.keys()):
        p, c = remaining_p[key], remaining_c[key]
        if p and c:
            # The missing occurrence is certain even when its identity is not.
            # Keep every candidate ref unpaired; shortages can cancel in totals.
            if len(p) != len(c):
                multiplicity_mismatch = True
            ambiguous.append(dict(reason='description_candidate', pdf=[_detail(t) for t in p],
                                  csv=[_detail(t) for t in c]))
        else:
            pdf_only.extend(_detail(t) for t in p)
            csv_only.extend(_detail(t) for t in c)
    totals = dict(pdf=_totals(pdf), csv=_totals(bank))
    # Complete, compatible, nonoverlapping coverage makes these totals definitive,
    # independently of whether descriptions identify individual occurrences.
    totals_mismatch = totals['pdf'] != totals['csv']
    status = ('failed' if pdf_only or csv_only or multiplicity_mismatch or totals_mismatch else
              'needs_review' if ambiguous else 'passed')
    report.update(status=status, matched=matched, ambiguous=ambiguous,
                  pdf_only=pdf_only, csv_only=csv_only,
                  counts=dict(matched=sum(g['count'] for g in matched), pdf_only=len(pdf_only),
                              csv_only=len(csv_only), ambiguous=len(ambiguous)),
                  totals=totals)
    return report


def main(argv=None):
    """CLI: private detailed findings only go to an explicit NEW local report."""
    import argparse
    import html
    import json

    parser = argparse.ArgumentParser(description='Read-only bank CSV comparison; no ledger imports')
    parser.add_argument('--pdf-json', required=True, help='Normalized PDF JSON with explicit metadata')
    parser.add_argument('--csv', nargs='+', required=True, help='Headerless UTF-8 bank CSV files')
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--from-date', required=True, help='Inclusive YYYY-MM-DD')
    parser.add_argument('--to-date', required=True, help='Inclusive YYYY-MM-DD')
    parser.add_argument('--out', help='NEW local .json or .html detailed report; never overwrites')
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.pdf_json).read_text(encoding='utf-8-sig'), parse_float=Decimal)
        if not isinstance(payload, dict) or not isinstance(payload.get('transactions'), list):
            raise CSVValidationError('Normalized PDF JSON object required')
        if not isinstance(payload.get('account_id'), str) or not payload['account_id'].strip():
            raise CSVValidationError('PDF account metadata required')
        docs = [load_csv_transactions(p, args.account_id, args.from_date, args.to_date)
                for p in args.csv]
        report = compare_transactions(
            payload['transactions'], docs, account_id=args.account_id,
            coverage_start=args.from_date, coverage_end=args.to_date,
            pdf_coverage_start=payload.get('coverage_start'),
            pdf_coverage_end=payload.get('coverage_end'), pdf_complete=payload.get('complete'),
            pdf_account_id=payload['account_id'])
    except (OSError, UnicodeError, ValueError, TypeError):
        report = _empty_report(args.account_id)
        report.update(reason='invalid_or_unavailable_input',
                      coverage_warnings=[dict(code='invalid_or_unavailable_input')])
    if args.out:
        try:
            target = Path(args.out)
            if target.suffix.lower() not in ('.json', '.html'):
                raise CSVValidationError('Report extension must be .json or .html')
            content = json.dumps(report, ensure_ascii=True, indent=2)
            if target.suffix.lower() == '.html':
                content = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                           '<title>CSV validation report</title><h1>CSV validation report</h1>'
                           '<pre>' + html.escape(content) + '</pre></html>')
            # Exclusive creation prevents existing source-file/report overwrite.
            with target.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(content)
        except (OSError, ValueError, TypeError):
            print('status=unavailable reason=report_output_unavailable')
            return 3
    counts = report['counts']
    print(f"status={report['status']} matched={counts['matched']} "
          f"pdf_only={counts['pdf_only']} csv_only={counts['csv_only']} "
          f"ambiguous_groups={counts['ambiguous']}")
    return {'passed': 0, 'failed': 1, 'needs_review': 2, 'unavailable': 3}[report['status']]


if __name__ == '__main__':
    raise SystemExit(main())
