import unittest


class SourceMatchingTests(unittest.TestCase):
    def test_stable_document_identity_ignores_root_and_preserves_account(self):
        from scripts.services.source_matching import document_identifier, source_identity
        self.assertEqual(document_identifier('C:/old/StatementsPDF/example.pdf'),
                         document_identifier('D:/new/StatementsPDF/example.pdf'))
        self.assertNotEqual(source_identity('A', 'example.pdf'), source_identity('B', 'example.pdf'))

    def test_exact_refs_preserve_distinct_edit_identities(self):
        from scripts.services.source_matching import SourceRow, ExistingRow, match_source_rows
        row = SourceRow('2026-01-01','SYNTHETIC',-100,'GBP',1,'1:1')
        from dataclasses import replace
        second = replace(row,row_ref='1:2')
        existing = (ExistingRow(7,row,'first-note'),ExistingRow(8,second,'second-note'))
        plan = match_source_rows((second,row),existing)
        self.assertEqual(plan.matches,((0,8),(1,7)))
        self.assertEqual(plan.ambiguous,())

    def test_ref_shift_with_different_metadata_is_not_permuted(self):
        from scripts.services.source_matching import SourceRow, ExistingRow, match_source_rows
        from dataclasses import replace
        row = SourceRow('2026-01-01','SYNTHETIC',-100,'GBP',1,'1:1')
        existing = (ExistingRow(7,row,'first'),ExistingRow(8,replace(row,row_ref='1:2'),'second'))
        plan = match_source_rows((replace(row,row_ref='1:2'),replace(row,row_ref='1:3')),existing)
        self.assertEqual(plan.matches,())
        self.assertEqual(plan.ambiguous,(0,1))
        self.assertEqual(plan.potential_removals,(7,8))

    def test_identical_refs_with_equal_metadata_preserve_both_occurrences(self):
        from scripts.services.source_matching import SourceRow, ExistingRow, match_source_rows
        row = SourceRow('2026-01-01','SYNTHETIC',-100,'GBP',1,'1:1')
        existing = (ExistingRow(7,row,'equal'), ExistingRow(8,row,'equal'))
        plan = match_source_rows((row, row), existing)
        self.assertEqual(plan.matches, ((0,7), (1,8)))
        self.assertEqual((plan.additions, plan.potential_removals, plan.ambiguous), ((), (), ()))

    def test_identical_refs_with_different_decisions_block_entire_group(self):
        from scripts.services.source_matching import SourceRow, ExistingRow, match_source_rows
        row = SourceRow('2026-01-01','SYNTHETIC',-100,'GBP',1,'1:1')
        existing = (ExistingRow(7,row,'resolved'), ExistingRow(8,row,'open'))
        plan = match_source_rows((row, row), existing)
        self.assertEqual(plan.matches, ())
        self.assertEqual(plan.ambiguous, (0,1))
        self.assertEqual(plan.potential_removals, (7,8))

    def test_unique_financial_key_matches_despite_shifted_provenance(self):
        from scripts.services.source_matching import SourceRow, ExistingRow, match_source_rows
        from dataclasses import replace
        row = SourceRow('2026-01-01','SYNTHETIC',-100,'GBP',1,'1:1')
        self.assertEqual(match_source_rows((replace(row,row_ref='2:4'),),(ExistingRow(7,row,'edited'),)).matches,((0,7),))

    def test_removed_and_changed_fields_are_candidates_not_matches(self):
        from scripts.services.source_matching import SourceRow, ExistingRow, match_source_rows
        from dataclasses import replace
        row = SourceRow('2026-01-01','SYNTHETIC',-100,'GBP',1,'1:1')
        plan = match_source_rows((replace(row,cents=-101),),(ExistingRow(7,row),))
        self.assertEqual(plan.matches,())
        self.assertEqual(plan.potential_removals,(7,))
        self.assertEqual(plan.changed_candidates,(0,))

    def test_identical_occurrences_are_not_collapsed(self):
        from scripts.services.source_matching import SourceRow, match_source_rows
        rows = (SourceRow('2026-01-01', 'SYNTHETIC', -100, 'GBP', 1, '1:1'),
                SourceRow('2026-01-01', 'SYNTHETIC', -100, 'GBP', 1, '1:2'))
        plan = match_source_rows(rows, ())
        self.assertEqual(plan.additions, (0, 1))


if __name__ == '__main__':
    unittest.main()
