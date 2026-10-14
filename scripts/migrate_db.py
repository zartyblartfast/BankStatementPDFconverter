"""Explicit, versioned SQLite migrations. Importing this module never opens a DB."""
from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3

from scripts.settings import get_settings


_TRACKING_SQL = '''CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)'''


def _statements(sql: str):
    """Use SQLite's parser boundaries, retaining trigger bodies and quoted ';'."""
    start = 0
    for index, character in enumerate(sql):
        if character == ';' and sqlite3.complete_statement(sql[start:index + 1]):
            yield sql[start:index + 1]
            start = index + 1
    if sql[start:].strip():
        yield sql[start:]


def migrate_db(db_path: str | Path, *, migrations_dir: str | Path | None = None) -> list[int]:
    """Opt-in only: apply supplied SQL migrations and return newly applied versions."""
    source = Path(db_path).resolve()
    if not source.is_file():
        raise FileNotFoundError('Migration database does not exist')
    directory = Path(migrations_dir) if migrations_dir is not None else get_settings().root / 'scripts' / 'migrations'
    if not directory.is_dir():
        raise FileNotFoundError('Migration directory does not exist')
    migrations = []
    for path in directory.glob('*.sql'):
        match = re.fullmatch(r'([0-9]{3,})_[a-z][a-z0-9_]*\.sql', path.name)
        if match is None or int(match[1]) < 1:
            raise ValueError('Invalid migration filename')
        content = path.read_bytes()
        migrations.append((int(match[1]), path.name,
                           hashlib.sha256(content).hexdigest(), content.decode('utf-8')))
    migrations.sort()
    if len({migration[0] for migration in migrations}) != len(migrations):
        raise ValueError('Duplicate migration versions')
    with closing(sqlite3.connect(source.as_uri() + '?mode=rw', uri=True)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(_TRACKING_SQL)
            applied = []
            existing = {row[0]: (row[1], row[2]) for row in conn.execute(
                'SELECT version, name, checksum FROM schema_migrations')}
            available = {version: (name, checksum) for version, name, checksum, _ in migrations}
            for version, signature in existing.items():
                if version not in available:
                    raise ValueError('Applied migration is missing')
                if available[version] != signature:
                    raise ValueError('Applied migration has changed')
            highest_applied = max(existing, default=0)
            if any(version not in existing and version <= highest_applied
                   for version, _, _, _ in migrations):
                raise ValueError('Pending migration has a retroactive version')
            for version, name, checksum, sql in migrations:
                if version in existing:
                    continue
                def authorizer(action, arg1, arg2, database, trigger):
                    if (action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                                   sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)
                            or 'schema_migrations' in ((arg1 or '').casefold(), (arg2 or '').casefold())):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                conn.set_authorizer(authorizer)
                try:
                    for statement in _statements(sql):
                        conn.execute(statement)
                except sqlite3.DatabaseError as exc:
                    if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_AUTH:
                        raise ValueError('Migration contains a forbidden operation') from exc
                    raise
                finally:
                    conn.set_authorizer(None)
                conn.execute('INSERT INTO schema_migrations(version, name, checksum) VALUES (?, ?, ?)',
                             (version, name, checksum))
                applied.append(version)
        return applied


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True, help='Existing database; no implicit live target')
    parser.add_argument('--migrations-dir', type=Path)
    parser.add_argument('--apply', action='store_true', required=True,
                        help='Explicit opt-in to transactional schema tracking/migrations')
    args = parser.parse_args(argv)
    applied = migrate_db(args.db, migrations_dir=args.migrations_dir)
    print(f'Applied {len(applied)} migration(s); tracking verified')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
