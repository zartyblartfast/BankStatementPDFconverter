"""Explicit saved UI file scope. Folder identity never establishes bank ownership."""
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import stat

from scripts.services.accounts import get_account, _validate_id


class FolderError(ValueError):
    pass


class FolderSchemaMissing(FolderError):
    pass


@contextmanager
def _connection(*, db_path=None, conn=None, write=False):
    if (db_path is None) == (conn is None):
        raise ValueError('Supply exactly one explicit db_path or conn')
    if conn is not None:
        yield conn
        return
    assert db_path is not None
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError('Folder database does not exist')
    connection = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True)
    try:
        if not write:
            connection.execute('PRAGMA query_only=ON')
        yield connection
    finally:
        connection.close()


def read_mappings(*, db_path=None, conn=None):
    with _connection(db_path=db_path, conn=conn) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_statement_folders'").fetchone():
            raise FolderSchemaMissing('Statement folder setup requires explicit migration 003; this page never creates it.')
        mappings = dict(connection.execute('SELECT account_id,folder_path FROM account_statement_folders ORDER BY account_id'))
        for account_id, folder in mappings.items():
            try:
                _validate_id(account_id)
            except ValueError as exc:
                raise FolderError('Invalid stored statement folder account identifier') from exc
            if not _canonical(folder):
                raise FolderError('Invalid stored statement folder path; explicit repair required')
        return mappings


def _canonical(folder):
    return (isinstance(folder, str) and bool(folder) and '\\' not in folder and ':' not in folder
            and '\x00' not in folder and (folder == '.' or
                all(part not in ('', '.', '..') for part in folder.split('/'))))


def _regular(path, kind):
    info = path.lstat()
    return (not (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) &
                 getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)) and kind(info.st_mode))


def _inventory(*, root):
    """Check lexical ancestors before resolve; prune every linked directory."""
    lexical = Path(root)
    if '..' in lexical.parts:
        return {}, {}
    root = Path(os.path.abspath(lexical))
    folders, pdfs = {}, {}
    try:
        for ancestor in reversed((root, *root.parents)):
            if not _regular(ancestor, stat.S_ISDIR):
                return folders, pdfs
        resolved = root.resolve()
        for directory, dirs, files in os.walk(root, followlinks=False):
            parent = Path(directory)
            if not _regular(parent, stat.S_ISDIR):
                dirs[:] = []
                continue
            folders[parent.relative_to(root).as_posix()] = parent
            safe = []
            for name in dirs:
                child = parent / name
                try:
                    if _regular(child, stat.S_ISDIR) and child.resolve().is_relative_to(resolved):
                        safe.append(name)
                except (OSError, ValueError, RuntimeError):
                    continue
            dirs[:] = sorted(safe)
            for name in sorted(files):
                candidate = parent / name
                if candidate.suffix.lower() != '.pdf':
                    continue
                try:
                    if _regular(candidate, stat.S_ISREG) and candidate.resolve().is_relative_to(resolved):
                        pdfs[candidate.relative_to(root).as_posix()] = candidate
                except (OSError, ValueError, RuntimeError):
                    continue
    except (OSError, ValueError, RuntimeError):
        return {}, {}
    return folders, pdfs


def folder_choices(*, root):
    return sorted(_inventory(root=root)[0])


def account_pdfs(account_id, *, root, db_path=None, conn=None):
    """Regular recursive PDFs, excluding subtrees assigned to other accounts."""
    with _connection(db_path=db_path, conn=conn) as connection:
        mappings = read_mappings(conn=connection)
        account = get_account(account_id, conn=connection)
        if account is None or not account['import_enabled']:
            raise FolderError('Select an available, verified account')
        selected = mappings.get(account_id)
        if selected is None:
            raise FolderError('Choose and save a statement folder for this account; no folder is inferred.')
        folders, pdfs = _inventory(root=root)
        if selected not in folders:
            raise FolderError('Saved statement folder is missing or unsafe; choose and save an available folder.')
        prefix = '' if selected == '.' else selected + '/'
        excluded = [path + '/' for other, path in mappings.items()
                    if other != account_id and path != '.' and path.startswith(prefix)]
        return dict(sorted((key, path) for key, path in pdfs.items()
                           if key.startswith(prefix) and not any(key.startswith(p) for p in excluded)))


def save_mapping(account_id, folder_path, *, root, db_path=None, conn=None):
    """Atomically save an exact offered folder for an existing import-enabled account.

    Caller-supplied connections must not already have an active transaction.
    No directories, accounts, schema, policy or import records are created.
    """
    with _connection(db_path=db_path, conn=conn, write=True) as connection:
        if connection.in_transaction:
            raise FolderError('Folder save requires an idle connection')
        connection.execute('BEGIN IMMEDIATE')
        try:
            read_mappings(conn=connection)
            account = get_account(account_id, conn=connection)
            if account is None or not account['import_enabled']:
                raise FolderError('Select an available, verified account')
            if folder_path not in folder_choices(root=root):
                raise FolderError('Select an exact available statement folder')
            connection.execute('INSERT INTO account_statement_folders(account_id,folder_path) VALUES (?,?) '
                               'ON CONFLICT(account_id) DO UPDATE SET folder_path=excluded.folder_path',
                               (account_id, folder_path))
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise FolderError('Statement folder is already assigned to another account') from exc
        except Exception:
            connection.rollback()
            raise
