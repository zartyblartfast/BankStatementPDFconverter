import unittest

from scripts.hsbc_pdf_to_txn import (
    _interpret_money_tokens,
    _is_noise_line,
    _looks_like_balance_marker,
    _looks_like_credit,
    _parse_money_token,
    _repair_split_money_tokens,
    _truncate_description,
    _find_money_tokens_from_end,
    _lookahead_sign_correction,
    _FX_RATE_LINE_RE,
    _NON_STERLING_RE,
    parse_lines_to_transactions,
)


class TestRepairSplitMoneyTokens(unittest.TestCase):
    def test_no_repair_needed(self):
        self.assertEqual(_repair_split_money_tokens(["hello", "12.34"]), ["hello", "12.34"])

    def test_merge_leading_digit_with_comma_token(self):
        # "1" + "234.56" — mid has no comma, so no merge
        self.assertEqual(_repair_split_money_tokens(["1", "234.56"]), ["1", "234.56"])
        # "1" + ",234.56" — mid starts with comma so not a valid money token → no merge
        self.assertEqual(_repair_split_money_tokens(["1", ",234.56"]), ["1", ",234.56"])
        # "1" + "1,234.56" — mid has comma AND is valid money token AND merged is valid → merge
        self.assertEqual(_repair_split_money_tokens(["1", "1,234.56"]), ["11,234.56"])

    def test_merge_fragment_ending_with_dot(self):
        self.assertEqual(_repair_split_money_tokens(["12.", "34"]), ["12.34"])

    def test_merge_fragment_ending_with_comma(self):
        self.assertEqual(_repair_split_money_tokens(["1,", "234.56"]), ["1,234.56"])

    def test_no_merge_unrelated_tokens(self):
        self.assertEqual(_repair_split_money_tokens(["hello", "world"]), ["hello", "world"])


class TestLooksLikeBalanceMarker(unittest.TestCase):
    def test_balance_brought_forward(self):
        self.assertTrue(_looks_like_balance_marker("BALANCE BROUGHT FORWARD"))
        self.assertTrue(_looks_like_balance_marker("BALANCEBROUGHTFORWARD"))

    def test_balance_carried_forward(self):
        self.assertTrue(_looks_like_balance_marker("Balance Carried Forward"))

    def test_opening_balance(self):
        self.assertTrue(_looks_like_balance_marker("OPENING BALANCE"))

    def test_closing_balance(self):
        self.assertTrue(_looks_like_balance_marker("Closing Balance"))

    def test_normal_description(self):
        self.assertFalse(_looks_like_balance_marker("VIS AMAZON UK"))
        self.assertFalse(_looks_like_balance_marker("DD NORTHNORTHANTS"))

    def test_partial_match_in_longer_string(self):
        self.assertTrue(_looks_like_balance_marker("some text BALANCECARRIEDFORWARD 38,083.71"))


class TestTruncateDescription(unittest.TestCase):
    def test_no_truncation(self):
        self.assertEqual(_truncate_description("VIS AMAZON UK"), "VIS AMAZON UK")

    def test_truncate_at_balance_carried_forward(self):
        self.assertEqual(
            _truncate_description("VIS AMAZON UK BALANCECARRIEDFORWARD 1,234.56"),
            "VIS AMAZON UK",
        )

    def test_truncate_at_balance_brought_forward(self):
        self.assertEqual(
            _truncate_description("DD NORTHANTS BALANCEBROUGHTFORWARD 500.00"),
            "DD NORTHANTS",
        )

    def test_truncate_date_range_footer(self):
        self.assertEqual(
            _truncate_description("VIS AMAZON 1 January 2026 to 31 January 2026"),
            "VIS AMAZON",
        )

    def test_empty_after_truncation(self):
        self.assertEqual(
            _truncate_description("BALANCECARRIEDFORWARD 1,000.00"),
            "",
        )


class TestLooksLikeCredit(unittest.TestCase):
    def test_cr_prefix(self):
        self.assertTrue(_looks_like_credit("CR WA888685A DWP XB"))

    def test_bgc_prefix(self):
        self.assertTrue(_looks_like_credit("BGC EMPLOYER SALARY"))

    def test_fpi_prefix(self):
        self.assertTrue(_looks_like_credit("FPI JOHN DOE RENT"))

    def test_dep_prefix(self):
        self.assertTrue(_looks_like_credit("DEP CASH DEPOSIT"))

    def test_cr_in_middle_not_credit(self):
        # CR in the middle is not a credit prefix
        self.assertFalse(_looks_like_credit("SOME CR TEXT"))

    def test_no_credit(self):
        self.assertFalse(_looks_like_credit("VIS AMAZON UK"))
        self.assertFalse(_looks_like_credit("DD NORTHNORTHANTS"))
        self.assertFalse(_looks_like_credit("FPO TRANSFER OUT"))

    def test_cr_substring_not_word(self):
        # "CREDIT" contains "CR" but not as a separate word
        self.assertFalse(_looks_like_credit("CREDIT CARD"))


class TestIsNoiseLine(unittest.TestCase):
    def test_empty_line(self):
        self.assertTrue(_is_noise_line(""))
        self.assertTrue(_is_noise_line("   "))

    def test_date_range_footer(self):
        self.assertTrue(_is_noise_line("1 January 2026 to 31 January 2026"))

    def test_header_patterns(self):
        self.assertTrue(_is_noise_line("Your Statement for January"))
        self.assertTrue(_is_noise_line("Account Name Sort Code"))
        self.assertTrue(_is_noise_line("Payments In 4,209.16"))
        self.assertTrue(_is_noise_line("Payments Out 3,404.81"))

    def test_normal_transaction_line(self):
        self.assertFalse(_is_noise_line("01 Jan 26 VIS AMAZON UK 12.34 0.00 987.66"))
        self.assertFalse(_is_noise_line("VIS AMAZON UK"))


class TestParseMoneyToken(unittest.TestCase):
    def test_simple(self):
        self.assertAlmostEqual(_parse_money_token("12.34"), 12.34)

    def test_with_commas(self):
        self.assertAlmostEqual(_parse_money_token("1,234.56"), 1234.56)

    def test_with_pound(self):
        self.assertAlmostEqual(_parse_money_token("£12.34"), 12.34)

    def test_parentheses_negative(self):
        self.assertAlmostEqual(_parse_money_token("(12.34)"), -12.34)

    def test_trailing_minus(self):
        self.assertAlmostEqual(_parse_money_token("12.34-"), -12.34)

    def test_leading_minus(self):
        self.assertAlmostEqual(_parse_money_token("-12.34"), -12.34)

    def test_zero(self):
        self.assertAlmostEqual(_parse_money_token("0.00"), 0.0)


class TestFindMoneyTokensFromEnd(unittest.TestCase):
    def test_three_tokens(self):
        tokens = ["VIS", "AMAZON", "12.34", "0.00", "987.66"]
        remaining, money = _find_money_tokens_from_end(tokens, max_count=3)
        self.assertEqual(money, ["12.34", "0.00", "987.66"])
        self.assertEqual(remaining, ["VIS", "AMAZON"])
        # Original list must not be mutated
        self.assertEqual(tokens, ["VIS", "AMAZON", "12.34", "0.00", "987.66"])

    def test_no_money_tokens(self):
        tokens = ["VIS", "AMAZON", "UK"]
        remaining, money = _find_money_tokens_from_end(tokens, max_count=3)
        self.assertEqual(money, [])
        self.assertEqual(remaining, ["VIS", "AMAZON", "UK"])

    def test_one_token(self):
        tokens = ["BALANCE", "1,234.56"]
        remaining, money = _find_money_tokens_from_end(tokens, max_count=3)
        self.assertEqual(money, ["1,234.56"])
        self.assertEqual(remaining, ["BALANCE"])

    def test_max_count_respected(self):
        tokens = ["1.00", "2.00", "3.00", "4.00"]
        remaining, money = _find_money_tokens_from_end(tokens, max_count=2)
        self.assertEqual(money, ["3.00", "4.00"])
        self.assertEqual(remaining, ["1.00", "2.00"])


class TestInterpretMoneyTokens(unittest.TestCase):
    def test_three_tokens_debit(self):
        mi = _interpret_money_tokens(
            ["12.34", "0.00", "987.66"],
            has_balance_column=True,
            description_full="VIS AMAZON",
            last_balance=1000.0,
            row_ref="1:2",
            line="test",
            page=1,
            txn_date="2026-01-01",
            description="VIS AMAZON",
            prev_txn=None,
        )
        self.assertAlmostEqual(mi.amount, -12.34)
        self.assertAlmostEqual(mi.parsed_balance, 987.66)
        self.assertEqual(mi.warnings, [])

    def test_three_tokens_credit(self):
        mi = _interpret_money_tokens(
            ["0.00", "100.00", "1100.00"],
            has_balance_column=True,
            description_full="CR SALARY",
            last_balance=1000.0,
            row_ref="1:2",
            line="test",
            page=1,
            txn_date="2026-01-01",
            description="CR SALARY",
            prev_txn=None,
        )
        self.assertAlmostEqual(mi.amount, 100.0)
        self.assertAlmostEqual(mi.parsed_balance, 1100.0)

    def test_three_tokens_both_zero(self):
        mi = _interpret_money_tokens(
            ["0.00", "0.00", "1000.00"],
            has_balance_column=True,
            description_full="OPENING BALANCE",
            last_balance=None,
            row_ref="1:2",
            line="test",
            page=1,
            txn_date="2026-01-01",
            description="OPENING BALANCE",
            prev_txn=None,
        )
        self.assertIsNone(mi.amount)
        self.assertAlmostEqual(mi.parsed_balance, 1000.0)

    def test_one_token_balance_marker(self):
        mi = _interpret_money_tokens(
            ["38083.71"],
            has_balance_column=True,
            description_full="BALANCE BROUGHT FORWARD",
            last_balance=None,
            row_ref="1:2",
            line="test",
            page=1,
            txn_date="2026-01-01",
            description="BALANCE BROUGHT FORWARD",
            prev_txn=None,
        )
        self.assertIsNone(mi.amount)
        self.assertAlmostEqual(mi.parsed_balance, 38083.71)

    def test_no_balance_column_two_tokens_debit(self):
        mi = _interpret_money_tokens(
            ["50.00", "0.00"],
            has_balance_column=False,
            description_full="DD PAYMENT",
            last_balance=None,
            row_ref="1:2",
            line="test",
            page=1,
            txn_date="2026-01-01",
            description="DD PAYMENT",
            prev_txn=None,
        )
        self.assertAlmostEqual(mi.amount, -50.0)


class TestBalanceMarkerFiltering(unittest.TestCase):
    def test_balance_marker_not_emitted_as_transaction(self):
        lines = [
            (1, "Date Description Money Out Money In Balance"),
            (1, "10 Dec 25 BALANCE BROUGHT FORWARD . 38,083.71"),
            (1, "11 Dec 25 VIS AMAZON UK 16.00 0.00 38,067.71"),
        ]
        txns, warnings, _, _ = parse_lines_to_transactions(
            lines, source_file="dummy.pdf"
        )
        # Only AMAZON should be emitted, not balance marker
        self.assertEqual(len(txns), 1)
        self.assertAlmostEqual(txns[0].amount, -16.0)

    def test_balance_marker_continuation_not_merged(self):
        lines = [
            (1, "Date Description Money Out Money In Balance"),
            (1, "11 Dec 25 VIS AMAZON UK 16.00 0.00 38,067.71"),
            (1, "BALANCECARRIEDFORWARD 38,067.71"),
        ]
        txns, warnings, _, _ = parse_lines_to_transactions(
            lines, source_file="dummy.pdf"
        )
        self.assertEqual(len(txns), 1)
        self.assertNotIn("BALANCECARRIEDFORWARD", txns[0].description_raw)


class TestHsbcPdfToTxn(unittest.TestCase):
    def test_parse_three_money_columns_and_wrapped_description(self):
        lines = [
            (1, "Date Description Money Out Money In Balance"),
            (1, "01 Jan 26 CARD PURCHASE 12.34 0.00 987.66"),
            (1, "AMAZON UK"),
            (1, "02 Jan 26 TRANSFER IN 0.00 100.00 1087.66"),
        ]

        txns, warnings, _used_row_refs, _recon_gaps = parse_lines_to_transactions(
            lines, source_file="dummy.pdf"
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(txns), 2)

        self.assertEqual(txns[0].txn_date, "2026-01-01")
        self.assertEqual(txns[0].amount, -12.34)
        self.assertEqual(txns[0].description_raw, "CARD PURCHASE AMAZON UK")
        self.assertEqual(txns[0].page, 1)
        self.assertTrue(txns[0].row_ref.startswith("1:"))

        self.assertEqual(txns[1].txn_date, "2026-01-02")
        self.assertEqual(txns[1].amount, 100.00)

    def test_parse_two_money_tokens_with_balance_reconciliation_and_row_ref_dedupe(self):
        # Dedup is by row_ref only.  Two identical content lines on different
        # line numbers get different row_refs and are BOTH kept.
        lines = [
            (1, "Date Description Money Out Money In Balance"),
            (1, "01/01/26 OPENING BALANCE 0.00 0.00 1000.00"),
            (1, "02/01/26 SALARY 1000.00 2000.00"),
            (1, "02/01/26 SALARY 1000.00 2000.00"),
            (1, "03/01/26 BILL PAYMENT 50.00 1950.00"),
        ]

        txns, warnings, _used_row_refs, _recon_gaps = parse_lines_to_transactions(
            lines, source_file="dummy.pdf"
        )

        self.assertEqual(len(txns), 3)

        self.assertEqual(txns[0].txn_date, "2026-01-02")
        self.assertEqual(txns[0].amount, 1000.0)

        self.assertEqual(txns[1].txn_date, "2026-01-02")
        self.assertEqual(txns[1].amount, 1000.0)
        self.assertNotEqual(txns[0].row_ref, txns[1].row_ref)

        self.assertEqual(txns[2].txn_date, "2026-01-03")
        self.assertEqual(txns[2].amount, -50.0)


class TestFxRateLineRegex(unittest.TestCase):
    def test_matches_usd(self):
        self.assertIsNotNone(_FX_RATE_LINE_RE.match("USD 24.00 @ 1.25"))

    def test_matches_eur(self):
        self.assertIsNotNone(_FX_RATE_LINE_RE.match("EUR 100.50 @ 0.85"))

    def test_matches_with_four_dp_rate(self):
        self.assertIsNotNone(_FX_RATE_LINE_RE.match("USD 5.00 @ 1.2820"))

    def test_no_match_visa_rate(self):
        self.assertIsNone(_FX_RATE_LINE_RE.match("Visa Rate 3.90"))

    def test_no_match_transaction(self):
        self.assertIsNone(_FX_RATE_LINE_RE.match("VIS AMAZON 12.34"))

    def test_no_match_lowercase_currency(self):
        self.assertIsNone(_FX_RATE_LINE_RE.match("usd 24.00 @ 1.25"))


class TestNonSterlingRegex(unittest.TestCase):
    def test_dr_non_sterling(self):
        m = _NON_STERLING_RE.match("DR Non-Sterling Transaction Fee")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "DR")

    def test_cr_non_sterling(self):
        m = _NON_STERLING_RE.match("CR Non-Sterling Transaction Fee")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "CR")

    def test_case_insensitive(self):
        self.assertIsNotNone(_NON_STERLING_RE.match("dr non-sterling"))

    def test_no_match_other_dr(self):
        self.assertIsNone(_NON_STERLING_RE.match("DR LOAN REPAYMENT"))

    def test_no_match_cr_transaction(self):
        self.assertIsNone(_NON_STERLING_RE.match("CR JAMES SYKES"))


class TestLookaheadSignCorrection(unittest.TestCase):
    def test_cr_non_sterling_flips_to_credit(self):
        lines = [
            (1, 1, "Visa Rate 3.90"),
            (1, 2, "CR Non-Sterling"),
            (1, 3, "Transaction Fee 0.10 41,699.26"),
        ]
        result = _lookahead_sign_correction(-3.90, 0, lines)
        self.assertAlmostEqual(result, 3.90)

    def test_dr_non_sterling_keeps_debit(self):
        lines = [
            (1, 1, "Visa Rate 8.00"),
            (1, 2, "DR Non-Sterling"),
            (1, 3, "Transaction Fee 0.22 42,000.00"),
        ]
        result = _lookahead_sign_correction(-8.00, 0, lines)
        self.assertAlmostEqual(result, -8.00)

    def test_no_non_sterling_returns_unchanged(self):
        lines = [
            (1, 1, "Cleaning 30.00"),
            (1, 2, "VIS AMAZON UK"),
        ]
        result = _lookahead_sign_correction(-30.00, 0, lines)
        self.assertAlmostEqual(result, -30.00)

    def test_unrelated_cr_line_does_not_flip(self):
        lines = [
            (1, 1, "pisp1640175747 20.00"),
            (1, 2, "CR LOAN CAPITAL FROM"),
        ]
        result = _lookahead_sign_correction(-20.00, 0, lines)
        self.assertAlmostEqual(result, -20.00)

    def test_skips_noise_lines(self):
        lines = [
            (1, 1, "Visa Rate 9.05"),
            (1, 2, ""),
            (1, 3, "CR Non-Sterling"),
        ]
        result = _lookahead_sign_correction(-9.05, 0, lines)
        self.assertAlmostEqual(result, 9.05)

    def test_at_end_of_lines_returns_unchanged(self):
        lines = [
            (1, 1, "Visa Rate 3.90"),
        ]
        result = _lookahead_sign_correction(-3.90, 0, lines)
        self.assertAlmostEqual(result, -3.90)


if __name__ == "__main__":
    unittest.main()
