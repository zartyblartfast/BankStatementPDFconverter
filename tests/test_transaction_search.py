from contextlib import closing
import sqlite3
from tests.test_dashboard_security import DashboardFixture


class TransactionSearchTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute('DELETE FROM transactions')
            c.execute("INSERT INTO categories VALUES (1,'Income')")
            c.execute("INSERT INTO subcategories VALUES (1,1,'Rent')")
            for i,date,desc,amount,sid in (
                (1,'2026-05-21','CR Bradley Chambers Deposit',600,1),
                (2,'2026-06-19','CR CHAMBERS B W RENT',600,None),
                (3,'2026-08-01','CR OTHER',600,None),
                (4,'2026-08-31','CR OTHER',600,None),
                (5,'2026-08-20','DR CHAMBERS',-600,None),
                (6,'2026-08-21','CR percent % reference',15,None)):
                c.execute('INSERT INTO transactions(txn_id,txn_date,description_raw,amount,subcategory_id,source_file,row_ref,import_hash,merchant_core) VALUES (?,?,?,?,?,\'fixture\',?,?,?)',(i,date,desc,amount,sid,str(i),str(i),desc))
            c.commit()

    def test_search_both_name_variants_and_uncategorised_without_writes(self):
        before=self.paths.db_path.read_bytes()
        r=self.client.get('/transactions?q=chambers&direction=credit',base_url=self.base)
        self.assertEqual(r.status_code,200)
        html=r.get_data(as_text=True)
        self.assertIn('Bradley Chambers Deposit',html)
        self.assertIn('CHAMBERS B W RENT',html)
        self.assertIn('Income / Rent',html)
        self.assertIn('Uncategorised',html)
        self.assertIn('2 matches',html)
        self.assertNotIn('DR CHAMBERS',html)
        self.assertEqual(self.paths.db_path.read_bytes(),before)

    def test_combined_amount_direction_inclusive_date_filters(self):
        html=self.client.get('/transactions?start=2026-08-01&end=2026-08-31&amount=600&direction=credit',base_url=self.base).get_data(as_text=True)
        self.assertIn('2 matches',html)
        self.assertIn('2026-08-01',html)
        self.assertIn('2026-08-31',html)
        self.assertIn('£1,200.00',html)
        self.assertNotIn('DR CHAMBERS',html)

    def test_invalid_filters_are_errors_not_unfiltered_results(self):
        for query in ('start=2026-09-01&end=2026-08-01','amount=NaN','amount=1.234','amount=-1','start=bad','direction=other','page=0'):
            r=self.client.get('/transactions?'+query,base_url=self.base)
            self.assertEqual(r.status_code,400,query)
            self.assertNotIn('CR Bradley Chambers Deposit',r.get_data(as_text=True))

    def test_search_characters_are_literal_and_empty_results(self):
        html=self.client.get('/transactions?q=%25',base_url=self.base).get_data(as_text=True)
        self.assertIn('1 matches',html)
        self.assertIn('percent % reference',html)
        html=self.client.get('/transactions?q=nonexistent',base_url=self.base).get_data(as_text=True)
        self.assertIn('No matching transactions',html)

    def test_bulk_category_pair_has_dedicated_stable_layout(self):
        html=self.client.get('/transactions',base_url=self.base).get_data(as_text=True)
        import re
        pair=re.search(r'<div class="bulk-search-assignment">(.*?)</div>',html,re.S)
        self.assertIsNotNone(pair)
        assert pair is not None
        self.assertIn('id="searchBulkCategory"',pair.group(1))
        self.assertIn('id="searchBulkSubcategory"',pair.group(1))
        self.assertIn('id="searchBulkApply"',pair.group(1))
        self.assertNotIn('id="searchSelectionCount"',pair.group(1))

    def test_excluded_transactions_remain_searchable_and_flagged(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute('UPDATE transactions SET excluded=1 WHERE txn_id=1')
            c.commit()
        html=self.client.get('/transactions?q=Bradley',base_url=self.base).get_data(as_text=True)
        self.assertIn('1 matches',html)
        self.assertIn('Excluded from reports',html)
        self.assertIn('Income / Rent',html)

    def test_pagination_totals_cover_all_results_and_links_preserve_filters(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            for i in range(10,65):
                c.execute("INSERT INTO transactions(txn_date,description_raw,amount,source_file,row_ref,import_hash) VALUES('2026-01-01','Pagination',10,'fixture',?,?)",(str(i),str(i)))
            c.commit()
        html=self.client.get('/transactions?q=Pagination&page=2',base_url=self.base).get_data(as_text=True)
        self.assertIn('55 matches',html)
        self.assertIn('£550.00',html)
        self.assertIn('q=Pagination',html)
        self.assertIn('Showing 51–55',html)
