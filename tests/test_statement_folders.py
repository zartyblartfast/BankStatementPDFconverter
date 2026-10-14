"""Saved folder scope uses synthetic ledgers/files only."""
from contextlib import closing
import importlib
import importlib.util
from pathlib import Path
import sqlite3
import unittest

from scripts.migrate_db import migrate_db
from tests.test_dashboard_security import DashboardFixture


class FolderTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        self.paths.statements_dir.mkdir()
        (self.paths.statements_dir / 'HSBC-IOM').mkdir()
        (self.paths.statements_dir / 'empty').mkdir()
        migrate_db(self.paths.db_path)
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_key,parser_verified,import_enabled) VALUES ('SECOND','Synthetic second','GBP',1,'hsbc_uk',1,1)")

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.services.statement_folders'), 'saved folder service missing')
        return importlib.import_module('scripts.services.statement_folders')

    def test_choices_prune_junctions_and_linked_lexical_ancestors(self):
        import os
        import subprocess
        api = self.api()
        nested = self.paths.statements_dir / 'HSBC-IOM' / 'archive'
        nested.mkdir()
        for name, target in (('linked', nested), ('cycle', self.paths.statements_dir)):
            link = self.paths.statements_dir / name
            made = subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)], capture_output=True)
            self.assertEqual(made.returncode, 0)
            self.addCleanup(lambda p=link: os.rmdir(p) if p.exists() else None)
        self.assertEqual(api.folder_choices(root=self.paths.statements_dir), ['.', 'HSBC-IOM', 'HSBC-IOM/archive', 'empty'])
        outer = self.root / 'linked-parent'
        made = subprocess.run(['cmd','/c','mklink','/J',str(outer),str(self.paths.statements_dir)], capture_output=True)
        self.assertEqual(made.returncode, 0)
        self.addCleanup(lambda: os.rmdir(outer) if outer.exists() else None)
        self.assertEqual(api.folder_choices(root=outer / 'HSBC-IOM'), [])

    def test_saved_root_excludes_other_account_subtree_in_page_and_review(self):
        from unittest.mock import patch
        from scripts import dashboard as d
        api = self.api()
        root_pdf = self.paths.statements_dir / 'same.pdf'
        other_pdf = self.paths.statements_dir / 'HSBC-IOM' / 'same.pdf'
        root_pdf.write_bytes(b'synthetic root')
        other_pdf.write_bytes(b'synthetic other')
        api.save_mapping('HSBC_UK', '.', db_path=self.paths.db_path, root=self.paths.statements_dir)
        api.save_mapping('SECOND', 'HSBC-IOM', db_path=self.paths.db_path, root=self.paths.statements_dir)
        root_value, other_value = (str(p.relative_to(self.root)) for p in (root_pdf, other_pdf))
        uk = self.client.get('/ingest?account_id=HSBC_UK', base_url=self.base).get_data(as_text=True)
        self.assertIn('value="'+root_value+'"', uk)
        self.assertNotIn('value="'+other_value+'"', uk)
        other = self.client.get('/ingest?account_id=SECOND', base_url=self.base).get_data(as_text=True)
        self.assertIn('value="'+other_value+'"', other)
        self.assertNotIn('value="'+root_value+'"', other)
        with patch.object(d, 'prepare_import') as extract:
            forged = self.client.post('/ingest', base_url=self.base, data={
                'csrf_token':self.token(), 'account_id':'HSBC_UK', 'pdf':other_value})
            self.assertEqual(forged.status_code, 400)
            extract.assert_not_called()

    def test_render_skips_files_disappearing_after_final_filtered_inventory(self):
        from scripts import dashboard as d
        from tests.test_extraction import synthetic_pdf, LINES
        from unittest.mock import patch

        api = self.api()
        outside = self.paths.statements_dir / 'other-account.pdf'
        outside.write_bytes(synthetic_pdf(LINES))
        api.save_mapping('HSBC_UK', '.', db_path=self.paths.db_path, root=self.paths.statements_dir)
        original_state = d._statement_folder_state
        for disappearance in ('file_deleted', 'parent_moved'):
            with self.subTest(disappearance=disappearance):
                folder = self.paths.statements_dir / disappearance
                folder.mkdir()
                missing = folder / 'missing.pdf'
                missing.write_bytes(synthetic_pdf(LINES))
                survivor = folder / 'survivor.pdf'
                survivor.write_bytes(synthetic_pdf(LINES))
                api.save_mapping('SECOND', disappearance, db_path=self.paths.db_path,
                                 root=self.paths.statements_dir)
                missing_value = str(missing.relative_to(self.root))
                survivor_value = str(survivor.relative_to(self.root))
                before = self.snapshot()
                calls = 0

                def disappear_after_inventory(account, accounts):
                    nonlocal calls
                    available, state = original_state(account, accounts)
                    calls += 1
                    if calls == 2:
                        self.assertIn(missing_value, available)
                        if disappearance == 'file_deleted':
                            missing.unlink()
                        else:
                            folder.rename(self.root / 'moved-parent')
                    return available, state

                with patch.object(d, '_statement_folder_state', side_effect=disappear_after_inventory):
                    response = self.client.get('/ingest?account_id=SECOND', base_url=self.base)
                self.assertEqual(calls, 2)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertNotIn('value="' + missing_value + '"', html)
                self.assertNotIn('value="' + str(outside.relative_to(self.root)) + '"', html)
                self.assertIn('id="account_id"', html)
                self.assertIn('value="SECOND" selected', html)
                self.assertIn('Save folder', html)
                if disappearance == 'file_deleted':
                    self.assertIn('value="' + survivor_value + '"', html)
                else:
                    self.assertNotIn('name="pdf"', html)
                    self.assertIn('No PDFs', html)
                self.assertEqual(self.snapshot(), before)

    def test_unconfigured_empty_account_can_save_folder_without_import(self):
        from scripts import dashboard as d
        from unittest.mock import patch
        page = self.client.get('/ingest?account_id=SECOND', base_url=self.base)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('id="account_id"', html)
        self.assertIn('Statement folder', html)
        self.assertIn('Save folder', html)
        self.assertIn('value="empty"', html)
        self.assertIn('Choose and save', html)
        self.assertNotIn('name="pdf"', html)
        with patch.object(d, 'prepare_import') as prepare:
            response = self.client.post('/ingest', base_url=self.base, data={
                'csrf_token':self.token(), 'action':'save_folder', 'account_id':'SECOND', 'folder_path':'empty'})
            self.assertEqual(response.status_code, 302)
            prepare.assert_not_called()
        page = self.client.get(response.location, base_url=self.base).get_data(as_text=True)
        self.assertIn('value="SECOND" selected', page)
        self.assertIn('value="empty" selected', page)
        self.assertIn('No PDFs', page)
        self.assertEqual(self.api().read_mappings(db_path=self.paths.db_path), {'SECOND':'empty'})
        no_selection = self.client.get('/ingest', base_url=self.base)
        self.assertEqual(no_selection.status_code, 200)
        self.assertIn('Select an account', no_selection.get_data(as_text=True))

    def test_commit_rechecks_safe_offered_scope_before_calling_importer(self):
        from scripts import dashboard as d
        from tests.test_extraction import synthetic_pdf, LINES
        from unittest.mock import patch
        import os, re, subprocess
        api = self.api()
        pdf = self.paths.statements_dir / 'HSBC-IOM' / 'fixture.pdf'
        pdf.write_bytes(synthetic_pdf([(1,'Statement period: 01 Jan 2026 to 31 Jan 2026')] + LINES))
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute('DELETE FROM transactions')
        api.save_mapping('SECOND','HSBC-IOM', db_path=self.paths.db_path, root=self.paths.statements_dir)
        review = self.client.post('/ingest', base_url=self.base, data={
            'csrf_token':self.token(), 'account_id':'SECOND', 'pdf':str(pdf.relative_to(self.root))})
        found = re.search(r'name="import_token" value="([^"]+)"', review.get_data(as_text=True))
        assert found is not None, 'real synthetic PDF should produce an eligible held preview'
        token = found.group(1)
        relocated = self.root / 'relocated'
        pdf.parent.rename(relocated)
        made = subprocess.run(['cmd','/c','mklink','/J',str(pdf.parent),str(relocated)], capture_output=True)
        self.assertEqual(made.returncode, 0)
        self.addCleanup(lambda: os.rmdir(pdf.parent) if pdf.parent.exists() else None)
        with patch.object(d, 'commit_import') as commit:
            response = self.client.post('/ingest', base_url=self.base, data={
                'csrf_token':self.token(), 'action':'commit', 'approve':'yes', 'import_token':token})
            self.assertEqual(response.status_code, 400)
            commit.assert_not_called()

    def test_corrupt_mapping_fails_closed_and_lexical_root_traversal_is_rejected(self):
        api = self.api()
        (self.paths.statements_dir / 'HSBC-IOM' / 'secret.pdf').write_bytes(b'synthetic')
        api.save_mapping('HSBC_UK','.', db_path=self.paths.db_path, root=self.paths.statements_dir)
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO account_statement_folders VALUES ('SECOND','HSBC-IOM/../HSBC-IOM')")
        with self.assertRaises(api.FolderError):
            api.account_pdfs('HSBC_UK', db_path=self.paths.db_path, root=self.paths.statements_dir)
        self.assertEqual(api.folder_choices(root=self.paths.statements_dir / 'HSBC-IOM' / '..'), [])

    def snapshot(self):
        with closing(sqlite3.connect(self.paths.db_path)) as conn:
            return list(conn.iterdump())

    def test_nested_child_mapping_is_excluded_from_its_mapped_parent(self):
        api = self.api()
        nested = self.paths.statements_dir / 'HSBC-IOM' / 'archive'
        nested.mkdir()
        sibling = self.paths.statements_dir / 'HSBC-IOM2'
        sibling.mkdir()
        for folder in (self.paths.statements_dir, nested.parent, nested, sibling):
            (folder / 'same.pdf').write_bytes(b'synthetic fixture')
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_key,parser_verified,import_enabled) VALUES ('THIRD','Synthetic third','GBP',1,'hsbc_uk',1,1)")
        for account, folder in (('HSBC_UK','.'), ('SECOND','HSBC-IOM'), ('THIRD','HSBC-IOM/archive')):
            api.save_mapping(account, folder, db_path=self.paths.db_path, root=self.paths.statements_dir)
        expected = {'HSBC_UK':['HSBC-IOM2/same.pdf','same.pdf'], 'SECOND':['HSBC-IOM/same.pdf'], 'THIRD':['HSBC-IOM/archive/same.pdf']}
        before = self.snapshot()
        for account, paths in expected.items():
            self.assertEqual(list(api.account_pdfs(account, db_path=self.paths.db_path, root=self.paths.statements_dir)), paths)
        self.assertEqual(self.snapshot(), before)

    def test_malformed_conflicting_unverified_saves_rollback_exactly(self):
        api = self.api()
        api.save_mapping('HSBC_UK','.', db_path=self.paths.db_path, root=self.paths.statements_dir)
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_verified,import_enabled) VALUES ('UNVERIFIED','Synthetic unverified','GBP',1,0,0)")
        before = self.snapshot()
        for account, folder in [('SECOND','.'), ('UNKNOWN','empty'), ('UNVERIFIED','empty'),
                ('SECOND ','empty'), ('second','empty'), ('','empty'), ('SECOND','../empty'),
                ('SECOND','/empty'), ('SECOND','C:/empty'), ('SECOND','./empty'),
                ('SECOND','empty/'), ('SECOND','HSBC-IOM/../empty'), ('SECOND','HSBC-IOM\\archive'),
                ('SECOND','missing'), ('SECOND',None), (None,'empty')]:
            with self.subTest(account=account, folder=folder), self.assertRaises(ValueError):
                api.save_mapping(account, folder, db_path=self.paths.db_path, root=self.paths.statements_dir)
            self.assertEqual(self.snapshot(), before)
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("CREATE TRIGGER reject_folder BEFORE INSERT ON account_statement_folders BEGIN SELECT RAISE(ABORT,'synthetic abort'); END")
        before = self.snapshot()
        response = self.client.post('/ingest', base_url=self.base, data={
            'csrf_token':self.token(), 'action':'save_folder', 'account_id':'SECOND', 'folder_path':'empty'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.snapshot(), before)

    def test_save_form_keeps_csrf_host_origin_guards(self):
        data = {'action':'save_folder', 'account_id':'SECOND', 'folder_path':'empty'}
        before = self.snapshot()
        self.assertEqual(self.client.post('/ingest', base_url=self.base, data=data).status_code, 403)
        data['csrf_token'] = self.token()
        for headers in ({'Host':'evil.example'}, {'Origin':'https://evil.example'}, {'Origin':'null'}, {'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.client.post('/ingest', base_url=self.base, data=data, headers=headers).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_missing_schema_is_explanatory_and_never_created(self):
        api = self.api()
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute('DROP TABLE account_statement_folders')
        before = self.snapshot()
        for method in (lambda: api.read_mappings(db_path=self.paths.db_path),
                       lambda: api.save_mapping('SECOND','empty', db_path=self.paths.db_path, root=self.paths.statements_dir)):
            with self.assertRaises(api.FolderSchemaMissing):
                method()
        page = self.client.get('/ingest?account_id=SECOND', base_url=self.base)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('explicit migration 003', html)
        self.assertIn('id="account_id"', html)
        self.assertNotIn('name="pdf"', html)
        self.assertEqual(self.snapshot(), before)
        missing = self.root / 'never-create.db'
        with self.assertRaises(FileNotFoundError):
            api.save_mapping('SECOND','empty', db_path=missing, root=self.paths.statements_dir)
        self.assertFalse(missing.exists())

    def test_unmapped_unknown_disappeared_folders_never_fallback(self):
        api = self.api()
        (self.paths.statements_dir / 'root.pdf').write_bytes(b'synthetic root')
        api.save_mapping('SECOND','empty', db_path=self.paths.db_path, root=self.paths.statements_dir)
        (self.paths.statements_dir / 'empty').rename(self.paths.statements_dir / 'renamed')
        before = self.snapshot()
        for account in ('HSBC_UK', 'SECOND', 'UNKNOWN'):
            with self.assertRaises(ValueError):
                api.account_pdfs(account, db_path=self.paths.db_path, root=self.paths.statements_dir)
            page = self.client.get('/ingest?account_id='+account, base_url=self.base)
            self.assertEqual(page.status_code, 200)
            self.assertNotIn('name="pdf"', page.get_data(as_text=True))
        self.assertIn('missing or unsafe', self.client.get('/ingest?account_id=SECOND', base_url=self.base).get_data(as_text=True))
        self.assertEqual(self.snapshot(), before)

    def test_real_preview_fingerprint_rejects_changed_mapping_even_when_pdf_remains_offered(self):
        import re
        from tests.test_extraction import synthetic_pdf, LINES
        api = self.api()
        pdf = self.paths.statements_dir / 'HSBC-IOM' / 'fixture.pdf'
        pdf.write_bytes(synthetic_pdf([(1,'Statement period: 01 Jan 2026 to 31 Jan 2026')] + LINES))
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute('DELETE FROM transactions')
        api.save_mapping('SECOND','HSBC-IOM', db_path=self.paths.db_path, root=self.paths.statements_dir)
        response = self.client.post('/ingest', base_url=self.base, data={
            'csrf_token':self.token(), 'account_id':'SECOND', 'pdf':str(pdf.relative_to(self.root))})
        found = re.search(r'name="import_token" value="([^"]+)"', response.get_data(as_text=True))
        assert found is not None, 'real synthetic PDF should produce an eligible held preview'
        token = found.group(1)
        api.save_mapping('SECOND','.', db_path=self.paths.db_path, root=self.paths.statements_dir)
        before = self.snapshot()
        result = self.client.post('/ingest', base_url=self.base, data={
            'csrf_token':self.token(), 'action':'commit', 'approve':'yes', 'import_token':token, 'account_id':'HSBC_UK'})
        html = result.get_data(as_text=True)
        self.assertIn('stale_preview', html)
        self.assertIn('value="SECOND" selected', html)
        self.assertEqual(self.snapshot(), before)

    def test_saved_mapping_is_read_by_a_fresh_process(self):
        import subprocess, sys, json
        api = self.api()
        api.save_mapping('SECOND','empty', db_path=self.paths.db_path, root=self.paths.statements_dir)
        code = ('import json,sys; from scripts.services.statement_folders import read_mappings; '
                'print(json.dumps(read_mappings(db_path=sys.argv[1])))')
        result = subprocess.run([sys.executable,'-c',code,str(self.paths.db_path)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'SECOND':'empty'})
        # Also verify the save_connection contract and SQL-level uniqueness/FK.
        with closing(sqlite3.connect(self.paths.db_path)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO account_statement_folders VALUES ('UNKNOWN','.')")
            conn.rollback()
            api.save_mapping('HSBC_UK','.', conn=conn, root=self.paths.statements_dir)
            conn.execute('BEGIN')
            with self.assertRaises(api.FolderError):
                api.save_mapping('HSBC_UK','HSBC-IOM', conn=conn, root=self.paths.statements_dir)
            conn.rollback()
        self.assertEqual(api.read_mappings(db_path=self.paths.db_path), {'HSBC_UK':'.','SECOND':'empty'})

    def test_save_empty_folder_persists_without_seeding_or_other_changes(self):
        api = self.api()
        self.assertEqual(api.read_mappings(db_path=self.paths.db_path), {})
        before = self.paths.category_rules_path.read_bytes()
        api.save_mapping('SECOND', 'empty', db_path=self.paths.db_path, root=self.paths.statements_dir)
        self.assertEqual(api.read_mappings(db_path=self.paths.db_path), {'SECOND':'empty'})
        with closing(sqlite3.connect(self.paths.db_path)) as conn:
            self.assertEqual(api.read_mappings(conn=conn), {'SECOND':'empty'})
            self.assertEqual(conn.execute('SELECT count(*) FROM accounts').fetchone()[0], 2)
        self.assertEqual(self.paths.category_rules_path.read_bytes(), before)
        self.assertEqual(list((self.paths.statements_dir / 'empty').iterdir()), [])
