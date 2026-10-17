"""One CSV rule reader; exact uppercase merchant keys, legacy last-row wins."""
import csv
import hashlib
import io
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def read_rows(path):
    return _parse(Path(path).read_bytes())


def _parse(data):
    with io.StringIO(data.decode('utf-8-sig'), newline='') as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if not {'merchant_core', 'category', 'subcategory'} <= set(fields):
            raise ValueError('Rules CSV is missing required columns.')
        rows = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise ValueError('Rules CSV contains malformed rows.')
    return fields, rows


def key(value):
    return value.strip().upper()


def is_enabled(row):
    value = row.get('enabled', '').strip().lower()
    if value not in ('', 'true', 'false', '1', '0'):
        raise ValueError('Invalid rule enabled value.')
    return value not in ('false', '0')


def effective(rows):
    return {key(row['merchant_core']): row for row in rows if key(row['merchant_core'])}


def load_rules(path):
    _, rows = read_rows(path)
    return {merchant: {name: row.get(name, '') for name in ('category', 'subcategory', 'notes')}
            for merchant, row in effective(rows).items() if is_enabled(row)}


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _safe(path):
    """Reject linked configured targets and our own recovery members."""
    for part in (path, *path.parents):
        if part.exists():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
                raise ValueError('Rule storage must not use linked paths.')


def snapshot(path):
    path = Path(path)
    _safe(path)
    raw = path.read_bytes()
    fields, rows = _parse(raw)
    current = effective(rows)
    for row in current.values():
        is_enabled(row)
    return dict(digest=_digest(raw), fields=fields, rows=rows, rules=current, raw=raw)


def _history_dir(path):
    directory = Path(path).with_name('.' + Path(path).name + '.history')
    _safe(directory)
    return directory


def history(path, digest=None):
    """Applied event-ID chain, resolving one prepared publication after restart.

    Digests identify CSV bytes, not edits: creation undo may repeat old bytes.
    Legacy unambiguous digest chains remain readable without migrating on read.
    """
    directory = _history_dir(path)
    if not directory.exists():
        return []
    events = {}
    for member in directory.glob('*.json'):
        _safe(member)
        try:
            event = json.loads(member.read_text(encoding='utf-8'))
            required = {'id', 'key', 'before', 'after', 'before_digest', 'after_digest', 'before_csv', 'after_csv', 'at', 'undo_of'}
            if (not isinstance(event, dict) or set(event) not in (required, required | {'version', 'parent_id'})
                    or event['id'] != member.stem or not re.fullmatch('[0-9a-f]{32}', member.stem)):
                raise ValueError('Malformed recovery event.')
            if 'version' in event and (type(event['version']) is not int or event['version'] != 2 or
                    (event['parent_id'] is not None and not isinstance(event['parent_id'], str))):
                raise ValueError('Malformed recovery ancestry.')
            if (_digest(event['before_csv'].encode('utf-8')) != event['before_digest'] or
                    _digest(event['after_csv'].encode('utf-8')) != event['after_digest'] or
                    event['before_digest'] == event['after_digest']):
                raise ValueError('Recovery checksum mismatch.')
            if (effective(_parse(event['before_csv'].encode('utf-8'))[1]).get(event['key']) != event['before'] or
                    effective(_parse(event['after_csv'].encode('utf-8'))[1]).get(event['key']) != event['after']):
                raise ValueError('Recovery row mismatch.')
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError('Rule recovery history is damaged; reconcile before editing.') from exc
        events[event['id']] = event
    legacy = [e for e in events.values() if 'version' not in e]
    parents = {}
    for event in events.values():
        if 'version' in event:
            parent = event['parent_id']
        else:
            candidates = [e['id'] for e in legacy if e['after_digest'] == event['before_digest']]
            if len(candidates) > 1:
                raise ValueError('Rule recovery history has ambiguous legacy ancestry.')
            parent = candidates[0] if candidates else None
        if parent is not None and (parent not in events or events[parent]['after_digest'] != event['before_digest']):
            raise ValueError('Rule recovery history is damaged; invalid parent.')
        parents[event['id']] = parent
    # Validate every chain, including prepared/orphan events; never hide a cycle.
    checked = set()
    for identity in events:
        seen = set()
        current = identity
        while current is not None and current not in checked:
            if current in seen:
                raise ValueError('Rule recovery history contains a cycle.')
            seen.add(current)
            current = parents[current]
        checked.update(seen)
    current_digest = digest or snapshot(path)['digest']
    publication = directory / 'state'
    _safe(publication)
    if publication.exists():
        try:
            state = json.loads(publication.read_text(encoding='utf-8'))
            if (not isinstance(state, dict) or set(state) != {'version', 'head', 'pending', 'digest'} or
                    type(state['version']) is not int or state['version'] != 2 or
                    not isinstance(state['digest'], str)):
                raise ValueError('Invalid publication state.')
            for field in ('head', 'pending'):
                if state[field] is not None and (not isinstance(state[field], str) or state[field] not in events):
                    raise ValueError('Invalid publication identity.')
            head = state['head']
            if head is not None and events[head]['after_digest'] != state['digest']:
                raise ValueError('Invalid publication head.')
            pending = events[state['pending']] if state['pending'] is not None else None
            if pending is not None and (pending.get('version') != 2 or
                    parents[pending['id']] != head or pending['before_digest'] != state['digest']):
                raise ValueError('Invalid publication event.')
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('Rule recovery history has damaged publication state.') from exc
        if pending is not None and current_digest == pending['after_digest']:
            head = pending['id']
        elif current_digest != state['digest']:
            raise ValueError('Rules changed outside maintenance; recovery history needs reconciliation.')
    else:
        if len(legacy) != len(events):
            raise ValueError('Rule recovery history is missing publication state.')
        candidates = [e['id'] for e in legacy if e['after_digest'] == current_digest]
        if len(candidates) > 1:
            raise ValueError('Rule recovery history has ambiguous legacy ancestry.')
        head = candidates[0] if candidates else None
        if events and head is None and not any(e['before_digest'] == current_digest for e in legacy):
            raise ValueError('Rules changed outside maintenance; recovery history needs reconciliation.')
    result = []
    while head is not None:
        result.append(events[head])
        head = parents[head]
    return result


@contextmanager
def _write_lock(path):
    lock = Path(path).with_name('.' + Path(path).name + '.lock')
    _safe(lock)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError('Rules are being edited, or a previous process stopped. Review the lock before retrying.') from exc
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            stream.write(str(os.getpid()))
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        lock.unlink()


def _atomic_write(path, data):
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def change(path, expected_digest, merchant, destination, enabled, undo_id=None, *, expected_history=()):
    """Publish a CSV replacement after durable recovery evidence, not two-store atomicity."""
    path = Path(path)
    _safe(path)
    merchant = key(merchant)
    if not merchant or len(merchant) > 500 or type(enabled) is not bool:
        raise ValueError('Invalid rule key or enabled state.')
    if not isinstance(destination, (tuple, list)) or len(destination) != 2 or any(not isinstance(v, str) or not v for v in destination):
        raise ValueError('A category and subcategory are required.')
    with _write_lock(path):
        state = snapshot(path)
        if state['digest'] != expected_digest:
            raise ValueError('Rules changed. Preview again before saving.')
        events = history(path, state['digest'])
        head = events[0]['id'] if events else None
        if expected_history != () and expected_history != head:
            raise ValueError('Rules changed. Preview again before saving.')
        before = state['rules'].get(merchant)
        restore = None
        if undo_id is not None:
            event = next((e for e in events if e['id'] == undo_id and e['key'] == merchant), None)
            latest = next((e for e in events if e['key'] == merchant), None)
            if not event or latest != event or before != event['after']:
                raise ValueError('This rule has later edits; targeted undo is no longer safe.')
            restore = event['before']
        elif before and (before['category'], before['subcategory'], is_enabled(before)) == (*destination, enabled):
            return dict(changed=False, event_id=None)
        fields, rows = list(state['fields']), [dict(r) for r in state['rows']]
        for field in ('enabled', 'revision'):
            if field not in fields:
                fields.append(field)
        index = next((i for i in range(len(rows)-1, -1, -1) if key(rows[i]['merchant_core']) == merchant), None)
        event_id = uuid.uuid4().hex
        if undo_id and restore is None:
            del rows[index]
            after = None
        else:
            after = dict(restore if undo_id else (before or {f: '' for f in fields}))
            after['merchant_core'] = (before or {}).get('merchant_core', merchant)
            if not undo_id:
                after.update(category=destination[0], subcategory=destination[1], enabled=str(enabled).lower())
            after['revision'] = event_id
            if index is None:
                rows.append(after)
            else:
                rows[index] = after
        # Normalize missing optional fields so equality guards survive CSV round-trips.
        rows = [{field: row.get(field, '') for field in fields} for row in rows]
        if after is not None:
            after = next(r for r in reversed(rows) if key(r['merchant_core']) == merchant)
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        new_raw = buffer.getvalue().encode('utf-8')
        event = dict(id=event_id, key=merchant, before=before, after=after, version=2, parent_id=head,
                     before_digest=state['digest'], after_digest=_digest(new_raw),
                     before_csv=state['raw'].decode('utf-8'), after_csv=new_raw.decode('utf-8'),
                     at=datetime.now(timezone.utc).isoformat(), undo_of=undo_id)
        directory = _history_dir(path)
        directory.mkdir(exist_ok=True)
        publication = directory / 'state'
        publication_state = dict(version=2, head=head, pending=None, digest=state['digest'])
        # Initialize before creating v2 evidence, so a failed state write cannot
        # leave a first orphan event with no recoverable publication baseline.
        if not publication.exists():
            _atomic_write(publication, json.dumps(publication_state).encode('utf-8'))
        _atomic_write(directory / (event_id + '.json'), json.dumps(event, ensure_ascii=False).encode('utf-8'))
        publication_state['pending'] = event_id
        _atomic_write(publication, json.dumps(publication_state).encode('utf-8'))
        # Check again after journal IO to catch non-cooperating external editors.
        if _digest(path.read_bytes()) != state['digest']:
            raise ValueError('Rules changed during save. No replacement was made.')
        _atomic_write(path, new_raw)
        # Finalize successful publications. An interruption here is recoverable
        # from pending + exact CSV bytes; a completed save cannot look prepared
        # merely because an external editor later restores its old CSV bytes.
        _atomic_write(publication, json.dumps(dict(version=2, head=event_id, pending=None,
                                                  digest=event['after_digest'])).encode('utf-8'))
        return dict(changed=True, event_id=event_id)
