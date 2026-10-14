"""Read-only account registry. No default database, migration or creation API."""
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import re
import sqlite3


def _validate_id(account_id):
    if not isinstance(account_id, str) or re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', account_id) is None:
        raise ValueError('Invalid account_id; expected 1-64 uppercase ASCII identifier characters')


def validate_account(account):
    """Validate a complete record without coercion, normalization or persistence.

    None currency means unresolved evidence, not GBP. Only the existing UK
    parser is verified in this release; a parser key alone never enables imports.
    """
    required = {'account_id', 'display_name', 'currency', 'institution', 'owned',
                'opened_on', 'closed_on', 'parser_key', 'parser_verified', 'import_enabled'}
    if not isinstance(account, dict) or not required <= account.keys():
        raise ValueError('Incomplete account record')
    _validate_id(account['account_id'])
    for field in ('display_name', 'institution'):
        value = account[field]
        if value is None and field == 'institution':
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'Invalid {field}')
    for field in ('owned', 'parser_verified', 'import_enabled'):
        if type(account[field]) is not bool:
            raise ValueError(f'{field} must be a boolean')
    currency = account['currency']
    if currency is not None and (not isinstance(currency, str) or re.fullmatch(r'[A-Z]{3}', currency) is None):
        raise ValueError('Invalid currency; expected three uppercase ASCII letters or None')
    for field in ('opened_on', 'closed_on'):
        value = account[field]
        if value is not None:
            if not isinstance(value, str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value) is None:
                raise ValueError(f'Invalid {field}; expected YYYY-MM-DD')
            date.fromisoformat(value)
    if account['opened_on'] and account['closed_on'] and account['closed_on'] < account['opened_on']:
        raise ValueError('closed_on precedes opened_on')
    parser = account['parser_key']
    if parser is not None and (not isinstance(parser, str) or re.fullmatch(r'[a-z][a-z0-9_]{0,63}', parser) is None):
        raise ValueError('Invalid parser_key')
    if account['parser_verified'] and (parser != 'hsbc_uk' or currency != 'GBP'):
        raise ValueError('Parser is verified only for GBP accounts by this release')
    if account['import_enabled'] and not (account['owned'] and account['parser_verified']
                                        and parser == 'hsbc_uk' and currency == 'GBP'
                                        and account['closed_on'] is None):
        raise ValueError('Import requires an owned, open GBP account and verified UK parser')
    return dict(account)



@contextmanager
def _connection(*, db_path=None, conn=None):
    if (db_path is None) == (conn is None):
        raise ValueError('Supply exactly one explicit db_path or conn')
    if conn is not None:
        yield conn
        return
    assert db_path is not None
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError('Account database does not exist')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        yield connection
    finally:
        connection.close()


class AccountSchemaMissing(RuntimeError):
    """An explicit account migration is required; reads never install it."""


def _legacy_accounts(connection, tables):
    observed = set()
    currencies = {}
    if 'transactions' in tables:
        for account_id, currency in connection.execute('SELECT account_id, currency FROM transactions'):
            _validate_id(account_id)
            observed.add(account_id)
            currencies.setdefault(account_id, set()).add(currency)
    if 'source_documents' in tables:
        for (account_id,) in connection.execute('SELECT DISTINCT account_id FROM source_documents'):
            _validate_id(account_id)
            observed.add(account_id)
    result = []
    for account_id in sorted(observed):
        evidence = currencies.get(account_id, set())
        currency = next(iter(evidence)) if len(evidence) == 1 else None
        if not isinstance(currency, str) or len(currency) != 3 or not all('A' <= c <= 'Z' for c in currency):
            currency = None
        result.append(dict(account_id=account_id, display_name='Account setup pending',
                           currency=currency, institution=None, owned=False,
                           opened_on=None, closed_on=None, parser_key=None,
                           parser_verified=False, import_enabled=False, setup_pending=True))
    return result


def list_accounts(*, db_path=None, conn=None, allow_legacy=False):
    """Return stable-ID-ordered records using only SELECTs on supplied connections."""
    with _connection(db_path=db_path, conn=conn) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'accounts' not in tables:
            if allow_legacy:
                return _legacy_accounts(connection, tables)
            raise AccountSchemaMissing('Account registry missing; explicit migration required')
        cursor = connection.execute('SELECT * FROM accounts ORDER BY account_id')
        names = [column[0] for column in cursor.description]
        result = []
        for row in cursor:
            account = dict(zip(names, row))
            for field in ('owned', 'parser_verified', 'import_enabled'):
                if type(account[field]) is not int or account[field] not in (0, 1):
                    raise ValueError(f'Invalid stored {field}')
                account[field] = bool(account[field])
            account = validate_account(account)
            account['setup_pending'] = not (account['owned'] and account['parser_verified']
                                             and account['currency'] is not None)
            result.append(account)
        return result


def get_account(account_id, *, db_path=None, conn=None, allow_legacy=False):
    """Return one registry record, or None if that exact identifier is absent."""
    _validate_id(account_id)
    return next((a for a in list_accounts(db_path=db_path, conn=conn, allow_legacy=allow_legacy)
                 if a['account_id'] == account_id), None)
