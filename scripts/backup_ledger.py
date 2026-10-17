"""Unencrypted local SQLite snapshots; not an encrypted disaster-recovery service."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid

from scripts.settings import get_settings


def _sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _is_snapshot_link(path: Path) -> bool:
    """Reject links/reparse points on the supplied entry, not its parent roots."""
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), 'st_file_attributes', 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False


def verify_backup(backup_path: str | Path) -> dict:
    """Validate hashes/integrity; refuse snapshot root/member links and reparse points."""
    backup = Path(backup_path)
    required = {'finance/ledger.db', 'config/categories.json', 'config/category_rules.csv'}
    if _is_snapshot_link(backup) or _is_snapshot_link(backup / 'manifest.json'):
        raise ValueError('Backup symlink refused')
    allowed = required | {'config/merchant_rules.json'}
    try:
        manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
        if (not isinstance(manifest, dict) or type(manifest.get('version')) is not int
                or manifest['version'] != 1
                or manifest.get('protection') != 'unencrypted local snapshot'
                or not isinstance(manifest.get('files'), dict)):
            raise ValueError('Invalid backup manifest')
        timestamp = datetime.fromisoformat(manifest['created_at'])
        inventory = set(manifest['files'])
        history_members = {name for name in inventory if re.fullmatch(
            r'config/\.category_rules\.csv\.history/(?:[0-9a-f]{32}\.json|state)', name)}
        if timestamp.tzinfo is None or not required <= inventory or not inventory <= allowed | history_members:
            raise ValueError('Invalid backup manifest')
        for checksum in manifest['files'].values():
            if not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum):
                raise ValueError('Invalid backup manifest checksum')
    except (OSError, TypeError, KeyError, ValueError) as exc:
        raise ValueError('Invalid backup manifest') from exc
    entries = []
    directories = [backup]
    resolved_backup = backup.resolve()
    while directories:
        for entry in directories.pop().iterdir():
            if _is_snapshot_link(entry) or not entry.resolve().is_relative_to(resolved_backup):
                raise ValueError('Backup symlink or reparse point refused')
            entries.append(entry)
            if entry.is_dir():
                directories.append(entry)
    actual = {p.relative_to(backup).as_posix() for p in entries if p.is_file()}
    if actual != set(manifest['files']) | {'manifest.json'}:
        raise ValueError('Backup inventory mismatch')
    for relative, checksum in manifest['files'].items():
        path = backup / relative
        if not path.is_file() or _sha256(path) != checksum:
            raise ValueError('Backup checksum mismatch')
    if history_members:
        from scripts.services import category_rules
        category_rules.history(backup / 'config/category_rules.csv')
    _check_database(backup / 'finance' / 'ledger.db', immutable=True)
    return manifest


def _check_database(path: Path, *, immutable: bool = False) -> None:
    try:
        with path.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\x00':
                raise ValueError('SQLite integrity check failed')
        uri = path.resolve().as_uri() + '?mode=ro' + ('&immutable=1' if immutable else '')
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite integrity check failed')
            if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('SQLite foreign-key check failed')
    except sqlite3.DatabaseError as exc:
        raise ValueError('SQLite integrity check failed') from exc


def restore_backup(backup_path: str | Path, destination: str | Path, *,
                   root: str | Path | None = None) -> Path:
    """Restore only to an explicitly named, new isolated project directory."""
    backup = Path(backup_path)
    requested = Path(destination)
    target = requested.resolve()
    if requested.exists() or requested.is_symlink() or target.exists():
        raise FileExistsError('Restore destination already exists')
    if _is_snapshot_link(backup):
        raise ValueError('Backup symlink or reparse point refused')
    backup = backup.resolve()
    live_root = get_settings(root).root
    if (target.is_relative_to(live_root) or live_root.is_relative_to(target)
            or target.is_relative_to(backup) or backup.is_relative_to(target)):
        raise ValueError('Restore destination must be isolated from the live project and snapshot')
    manifest = verify_backup(backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.restore-partial-', dir=target.parent))
    try:
        for relative in (*manifest['files'], 'manifest.json'):
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(backup / relative, output)
        verify_backup(staging)
        staging.rename(target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _rule_inventory(path):
    """Bounded recovery inventory, excluding transient atomic-write staging."""
    from scripts.services import category_rules
    directory = path.with_name('.' + path.name + '.history')
    if _is_snapshot_link(path) or _is_snapshot_link(directory):
        raise ValueError('Rule snapshot link or reparse point refused')
    members = []
    if directory.exists():
        for member in directory.iterdir():
            if _is_snapshot_link(member):
                raise ValueError('Rule history link or reparse point refused')
            if re.fullmatch(r'\.(?:[0-9a-f]{32}\.json|state)\.[0-9a-f]{32}\.tmp', member.name) and member.is_file():
                continue
            if not re.fullmatch(r'(?:[0-9a-f]{32}\.json|state)', member.name) or not member.is_file():
                raise ValueError('Invalid rule history inventory')
            members.append(member)
        category_rules.history(path)
    return {member: _sha256(member) for member in [path, *members]}


def _consistent_rule_copy(path, config_destination):
    from scripts.services import category_rules
    with category_rules._write_lock(path):
        before = _rule_inventory(path)
        for source in before:
            relative = source.relative_to(path.parent)
            target = config_destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        if before != _rule_inventory(path) or any(
                _sha256(config_destination / source.relative_to(path.parent)) != digest
                for source, digest in before.items()):
            raise ValueError('Rules or history changed during snapshot')


def create_backup(*, root: str | Path | None = None,
                  db_path: str | Path | None = None,
                  backup_dir: str | Path | None = None,
                  name: str | None = None) -> Path:
    """Snapshot the ledger plus category config, never statement PDFs/CSVs."""
    settings = get_settings(root)
    source = Path(db_path).resolve() if db_path is not None else settings.db_path
    parent = Path(backup_dir).resolve() if backup_dir is not None else settings.backup_dir
    timestamp = datetime.now(timezone.utc)
    if name is None:
        name = timestamp.strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex[:8]
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10))}
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', name)
            or name.endswith('.') or name.split('.')[0].upper() in reserved):
        raise ValueError('Unsafe snapshot name')
    target = parent / name
    if target.exists() or target.is_symlink():
        raise FileExistsError('Snapshot destination already exists')
    if not source.is_file():
        raise FileNotFoundError('Source SQLite database does not exist')
    _check_database(source)
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.partial-', dir=parent))
    try:
        ledger = staging / 'finance' / 'ledger.db'
        ledger.parent.mkdir()
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(ledger)) as dest:
                src.backup(dest)
        (staging / 'config').mkdir()
        _consistent_rule_copy(settings.category_rules_path, staging / 'config')
        for config in (settings.categories_path, settings.merchant_rules_path):
            if config == settings.merchant_rules_path and not config.exists():
                continue
            shutil.copyfile(config, staging / 'config' / config.name)
        files = {path.relative_to(staging).as_posix(): _sha256(path)
                 for path in sorted(staging.rglob('*')) if path.is_file()}
        manifest = {'version': 1, 'created_at': timestamp.isoformat(),
                    'protection': 'unencrypted local snapshot', 'files': files}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        verify_backup(staging)
        staging.rename(target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='Create an unencrypted local snapshot')
    create.add_argument('--root', type=Path)
    create.add_argument('--db', type=Path)
    create.add_argument('--backup-dir', type=Path)
    create.add_argument('--name')
    verify = commands.add_parser('verify', help='Verify snapshot checksums and SQLite integrity')
    verify.add_argument('snapshot', type=Path)
    restore = commands.add_parser('restore', help='Restore only to a new isolated directory')
    restore.add_argument('snapshot', type=Path)
    restore.add_argument('--destination', type=Path, required=True)
    restore.add_argument('--root', type=Path, help='Live project root to refuse as a restore target')
    args = parser.parse_args(argv)
    if args.command == 'create':
        target = create_backup(root=args.root, db_path=args.db,
                               backup_dir=args.backup_dir, name=args.name)
        print(f'Created unencrypted local snapshot: {target}')
    elif args.command == 'verify':
        verify_backup(args.snapshot)
        print('Backup verified (unencrypted local snapshot)')
    else:
        target = restore_backup(args.snapshot, args.destination, root=args.root)
        print(f'Restored verified snapshot to isolated directory: {target}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
