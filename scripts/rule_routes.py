"""Small local rule maintenance UI/API, separate from transaction commits."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3

from flask import jsonify, render_template, request
from scripts.services import category_rules as store, rule_review as review
from scripts.services.search_bulk import hold, load, discard


@contextmanager
def read_ledger(path):
    if not path.is_file():
        yield None
        return
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        yield conn
    finally:
        conn.close()


def _integer(value):
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ValueError('A valid SQLite transaction or exception ID is required.')
    return value


def _definition_digest(tree):
    return hashlib.sha256(json.dumps(tree, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def public_rule(row):
    if row is None:
        return None
    return dict(category=row['category'], subcategory=row['subcategory'], enabled=store.is_enabled(row))


def _history(path, key, state):
    result = []
    latest = True
    for event in store.history(path, state['digest']):
        if event['key'] != key:
            continue
        result.append(dict(id=event['id'], at=event['at'], before=public_rule(event['before']),
                           after=public_rule(event['after']), undo_of=event['undo_of'],
                           can_undo=latest and state['rules'].get(key) == event['after']))
        latest = False
    return result


class DisplayScopeError(ValueError):
    pass


def display_scope(conn):
    accounts = review.registered_accounts(conn)
    values = request.args.getlist('account_id')
    if len(values) > 1 or (values and values[0] and values[0] not in {a['account_id'] for a in accounts}):
        raise DisplayScopeError('Choose one registered account or All accounts.')
    return accounts, values[0] if values and values[0] else None


def register_rules(app, dashboard):
    d = dashboard

    @app.get('/rules')
    def rules_page():
        query = request.args.get('q', request.args.get('key', '')).strip()
        error = None
        entries, suggestions = [], []
        accounts, account_id = [], None
        status = 200
        try:
            state = store.snapshot(d.RULES_CSV)
            with read_ledger(d.DB_PATH) as conn:
                accounts, account_id = display_scope(conn)
                evidence = list(review.rows(conn, account_id)) if conn else []
                candidates = review.candidates(conn, state['rules'], account_id) if conn else []
            by_key = {merchant: dict(key=merchant, rule=public_rule(row), notices=[])
                      for merchant, row in state['rules'].items()}
            for candidate in candidates:
                if candidate['rule'] is None:
                    suggestions.append(candidate)
                else:
                    by_key[candidate['key']]['notices'].append(candidate)
            counts = {}
            for row in evidence:
                counts[row['key']] = counts.get(row['key'], 0) + 1
            for merchant, entry in by_key.items():
                entry['match_count'] = counts.get(merchant, 0)
            entries = [entry for merchant, entry in sorted(by_key.items())
                       if (account_id is None or entry['match_count']) and query.casefold() in (merchant + ' ' + json.dumps(entry['rule'], ensure_ascii=False)).casefold()]
            suggestions = [entry for entry in suggestions if query.casefold() in
                           (entry['key'] + ' ' + json.dumps(entry['saved'], ensure_ascii=False)).casefold()]
        except (ValueError, OSError) as exc:
            error = str(exc)
            status = 400 if isinstance(exc, DisplayScopeError) else 409
        return render_template('rules.html', active_page='rules', entries=entries, suggestions=suggestions, query=query,
                               accounts=accounts, account_id=account_id or '',
                               error=error, cat_tree=d._load_cat_tree()), status

    @app.get('/api/rules')
    def rule_inspect():
        try:
            if not {'key'} <= set(request.args) <= {'key', 'account_id'} or len(request.args.getlist('key')) != 1:
                raise ValueError('Choose one exact matching key.')
            key = request.args['key']
            if not isinstance(key, str) or not key or key != store.key(key) or len(key) > 500:
                raise ValueError('Choose one exact uppercase matching key.')
            state = store.snapshot(d.RULES_CSV)
            with read_ledger(d.DB_PATH) as conn:
                _, account_id = display_scope(conn)
                evidence = review.matches(conn, key, account_id) if conn else []
            return jsonify(ok=True, key=key, rule=public_rule(state['rules'].get(key)), matches=evidence,
                           history=_history(d.RULES_CSV, key, state))
        except (ValueError, OSError) as exc:
            return jsonify(ok=False, error=str(exc)), 400 if isinstance(exc, DisplayScopeError) else 409

    @app.post('/api/rules/preview')
    def rule_preview():
        data = request.get_json(silent=True)
        allowed = ({'key', 'category', 'subcategory', 'enabled'},
                   {'txn_id', 'category', 'subcategory'}, {'exception_id', 'category', 'subcategory'}, {'undo_id'})
        if not isinstance(data, dict) or set(data) not in allowed:
            return jsonify(ok=False, error='Choose a rule action or one saved transaction.'), 400
        try:
            state = store.snapshot(d.RULES_CSV)
            tree = d._load_cat_tree()
            events = store.history(d.RULES_CSV, state['digest'])
            undo_id = data.get('undo_id')
            future = 'txn_id' in data or 'exception_id' in data
            source = None
            with read_ledger(d.DB_PATH) as conn:
                if undo_id is not None:
                    if not isinstance(undo_id, str):
                        raise ValueError('Choose an available history entry.')
                    event = next((e for e in events if e['id'] == undo_id), None)
                    if event is None:
                        raise ValueError('History entry is no longer available.')
                    key = event['key']
                    latest = next(e for e in events if e['key'] == key)
                    if latest != event or state['rules'].get(key) != event['after']:
                        raise ValueError('This rule has later edits; targeted undo is no longer safe.')
                    proposed = public_rule(event['before'])
                elif future:
                    if conn is None:
                        raise ValueError('Ledger is not available.')
                    source_id = _integer(data.get('txn_id', data.get('exception_id')))
                    if 'txn_id' in data:
                        row = conn.execute('SELECT description_raw FROM transactions WHERE txn_id=?', (source_id,)).fetchone()
                    else:
                        row = conn.execute('''SELECT t.description_raw FROM exceptions e
                            JOIN transactions t ON e.txn_id=t.txn_id WHERE e.exception_id=?''', (source_id,)).fetchone()
                    if row is None:
                        raise ValueError('Saved transaction is not available.')
                    key = review.merchant_key(row['description_raw'])
                    source = dict(kind='txn_id' if 'txn_id' in data else 'exception_id', id=source_id, key=key)
                    old = state['rules'].get(key)
                    if old and not store.is_enabled(old):
                        raise ValueError('This key is disabled. Use Rules to explicitly re-enable it; saving a category does not resurrect it.')
                    proposed = dict(category=data['category'], subcategory=data['subcategory'], enabled=True)
                else:
                    key = data['key']
                    if not isinstance(key, str) or key != store.key(key):
                        raise ValueError('Choose the exact uppercase matching key.')
                    if type(data['enabled']) is not bool:
                        raise ValueError('Enabled must be a boolean.')
                    proposed = {name: data[name] for name in ('category', 'subcategory', 'enabled')}
                if not key or len(key) > 500:
                    raise ValueError('No usable exact merchant key is available.')
                if proposed is not None:
                    category, subcategory = proposed['category'], proposed['subcategory']
                    current = state['rules'].get(key)
                    retain_disabled = (undo_id is None and not future and proposed['enabled'] is False and
                                       current is not None and
                                       (category, subcategory) == (current['category'], current['subcategory']))
                    if (undo_id is None and not future and proposed['enabled'] is False and
                            (current is None or (store.is_enabled(current) and not retain_disabled))):
                        raise ValueError('Disable must retain the exact existing rule destination.')
                    if not retain_disabled and (not isinstance(category, str) or not isinstance(subcategory, str) or subcategory not in tree.get(category, [])):
                        raise ValueError('Choose an existing Category / Subcategory.')
                evidence = review.matches(conn, key) if conn else []
                if not future and undo_id is None and key not in state['rules'] and not evidence:
                    raise ValueError('Choose an existing rule or a key needing review.')
            old = public_rule(state['rules'].get(key))
            held = dict(path=str(d.RULES_CSV.resolve()), db=str(d.DB_PATH.resolve()), digest=state['digest'],
                        definitions=_definition_digest(tree), evidence=review.fingerprint(evidence), key=key,
                        history_id=events[0]['id'] if events else None,
                        proposed=proposed, undo_id=undo_id, source=source)
            token = hold(d.csrf_token(), 'rule_preview', held)
            return jsonify(ok=True, preview_token=token, key=key, old=old, proposed=proposed,
                           noop=old == proposed, matches=evidence)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 409
        except OSError:
            app.logger.exception('Could not prepare rule preview')
            return jsonify(ok=False, error='Could not read rule storage. No rule action was applied.'), 500

    @app.post('/api/rules/confirm')
    def rule_confirm():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'preview_token', 'approved'} or data['approved'] is not True:
            return jsonify(ok=False, error='Explicit preview approval is required.'), 400
        try:
            held = load(data['preview_token'], d.csrf_token(), 'rule_preview')
            if (held['path'] != str(d.RULES_CSV.resolve()) or held['db'] != str(d.DB_PATH.resolve()) or
                    held['definitions'] != _definition_digest(d._load_cat_tree())):
                raise ValueError('Configuration changed. Preview again.')
            with read_ledger(d.DB_PATH) as conn:
                evidence = review.matches(conn, held['key']) if conn else []
                if review.fingerprint(evidence) != held['evidence']:
                    raise ValueError('Historical matching scope changed. Preview again.')
                if held['source']:
                    source = held['source']
                    if conn is None:
                        raise ValueError('Saved transaction is not available.')
                    query = ('SELECT description_raw FROM transactions WHERE txn_id=?' if source['kind'] == 'txn_id' else
                             'SELECT t.description_raw FROM exceptions e JOIN transactions t ON e.txn_id=t.txn_id WHERE e.exception_id=?')
                    row = conn.execute(query, (source['id'],)).fetchone()
                    if row is None or review.merchant_key(row['description_raw']) != source['key']:
                        raise ValueError('Saved transaction changed. Preview again.')
            # A failed/unknown publication cannot be replayed; a fresh preview is required.
            discard(data['preview_token'])
            proposed = held['proposed']
            destination = (proposed['category'], proposed['subcategory']) if proposed else ('Unused', 'Unused')
            result = store.change(d.RULES_CSV, held['digest'], held['key'], destination,
                                  proposed['enabled'] if proposed else False, undo_id=held['undo_id'],
                                  expected_history=held['history_id'])
            current = store.snapshot(d.RULES_CSV)
            if public_rule(current['rules'].get(held['key'])) != proposed:
                raise OSError('Rule read-back did not match approved action')
            return jsonify(ok=True, **result)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 409
        except OSError:
            app.logger.exception('Rule publication failed or outcome could not be verified')
            return jsonify(ok=False, error='Rule save failed or its outcome is unknown. Reload Rules to check before a fresh preview. Historical categories were not changed by this rule action.'), 500
