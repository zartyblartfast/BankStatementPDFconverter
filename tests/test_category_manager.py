from contextlib import closing
import json
import sqlite3
from unittest.mock import patch
from tests.test_dashboard_security import DashboardFixture


class CategoryManagerTests(DashboardFixture):
    def test_add_category_with_subcategories_without_transaction_or_rule_changes(self):
        rules=self.paths.category_rules_path.read_bytes()
        db=self.paths.db_path.read_bytes()
        r=self.post('/api/manage-categories',{'category':'Pets','subcategories':['Food','Vet']})
        self.assertEqual(r.status_code,200)
        self.assertTrue(r.json['ok'])
        self.assertEqual(json.loads(self.paths.categories_path.read_text())['Pets'],['Food','Vet'])
        self.assertEqual(self.paths.db_path.read_bytes(),db)
        self.assertEqual(self.paths.category_rules_path.read_bytes(),rules)
        html=self.client.get('/exceptions',base_url=self.base).get_data(as_text=True)
        self.assertIn('Manage categories',html)
        self.assertIn('Pets',html)

    def test_add_subcategory_to_existing_category_preserves_tree(self):
        r=self.post('/api/manage-categories',{'category':'Fixture','subcategories':['Other']})
        self.assertEqual(r.status_code,200)
        self.assertEqual(json.loads(self.paths.categories_path.read_text()),{'Fixture':['Test','Other']})

    def test_invalid_names_and_duplicates_preserve_file(self):
        before=self.paths.categories_path.read_bytes()
        for payload in (None,{}, {'category':'','subcategories':['Valid']},
                        {'category':'Fixture','subcategories':[]},
                        {'category':'Fixture','subcategories':['test']},
                        {'category':'fixture','subcategories':['Other']},
                        {'category':'Valid','subcategories':['A','a']},
                        {'category':'Valid','subcategories':['A\nB']},
                        {'category':'Valid','subcategories':[42]}):
            r=self.post('/api/manage-categories',payload)
            self.assertEqual(r.status_code,400,repr(payload))
            self.assertEqual(self.paths.categories_path.read_bytes(),before)

    def test_requires_csrf(self):
        r=self.client.post('/api/manage-categories',base_url=self.base,json={'category':'Pets','subcategories':['Food']})
        self.assertEqual(r.status_code,403)

    def test_write_failure_keeps_existing_tree(self):
        before=self.paths.categories_path.read_bytes()
        with patch('scripts.services.category_manager.os.replace',side_effect=OSError('fixture failure')):
            r=self.post('/api/manage-categories',{'category':'Pets','subcategories':['Food']})
        self.assertEqual(r.status_code,500)
        self.assertEqual(self.paths.categories_path.read_bytes(),before)
