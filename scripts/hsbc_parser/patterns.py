"""Regex patterns, constants, and noise lists for the HSBC PDF parser."""

import re

# ── Numeric thresholds & constants ──────────────────────────
_RECONCILIATION_TOLERANCE = 0.01
_DEFAULT_Y_TOLERANCE = 2.5
_SUSPECT_MERGED_TOKEN_THRESHOLD = 6
_SUSPECT_MERGED_LINE_LEN = 80
_SUSPECT_DESC_LEN_THRESHOLD = 160
_MIN_TXN_COUNT_DEFAULT = 5

# Transaction prefixes known to represent credits (income)
_CREDIT_PREFIXES = {"CR", "BGC", "FPI", "DEP"}

# ── Compiled regex patterns ─────────────────────────────────
_DATE_RE = re.compile(
    r"^\s*(?P<date>(?:\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2})|(?:\d{1,2}/\d{1,2}/\d{2,4}))\s+(?P<rest>.+?)\s*$"
)

_DATE_RANGE_FOOTER_RE = re.compile(
    r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+to\s+\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
    re.IGNORECASE,
)

_MONEY_TOKEN_RE = re.compile(
    r"^(?:£)?\(?-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?-?$"
)

_AMOUNT_IN_LINE_RE = re.compile(r"(?:£)?\(?-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?-?")

_DEBUG_AMOUNT_IN_LINE_RE = re.compile(
    r"(?:£\s*)?\(?-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\)?-?"
)

_DEBUG_KEYWORD_LINE_RE = re.compile(
    r"\b(charge|charges|fee|fees|interest|overdraft|service)\b", re.IGNORECASE
)

_TXN_PREFIX_RE = re.compile(
    r"^(?:DD|VIS|BP|CR|SO|ATM|DR|FPI|FPO|TFR|TRF|BGC|CHG|CHARGE|CASH|DEP|POS|CARD|OBP)\b|^\)\)\)",
    re.IGNORECASE,
)

_FX_RATE_LINE_RE = re.compile(
    r"^[A-Z]{3}\s+[\d,.]+\.\d{2}\s+@\s+",
)

_NON_STERLING_RE = re.compile(r"^(CR|DR)\s+Non-Sterling\b", re.IGNORECASE)

# ── Noise line patterns ─────────────────────────────────────
_NOISE_LINE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^contact\s+tel\b", re.IGNORECASE),
    re.compile(r"^see\s+reverse\b", re.IGNORECASE),
    re.compile(r"^text\s+phone\b", re.IGNORECASE),
    re.compile(r"^used\s+by\s+deaf\b", re.IGNORECASE),
    re.compile(r"^www\.hsbc\.co\.uk\b", re.IGNORECASE),
    re.compile(r"^your\s+statement\b", re.IGNORECASE),
    re.compile(r"^your\s+hsbc\b", re.IGNORECASE),
    re.compile(r"^account\s+name\b.*sort\s*code\b", re.IGNORECASE),
    re.compile(r"^international\s+bank\s+account\s+number\b", re.IGNORECASE),
    re.compile(r"^bank\s+identifier\s+code\b", re.IGNORECASE),
    re.compile(r"^account\s+summary\b", re.IGNORECASE),
    re.compile(r"^opening\s*balance\b", re.IGNORECASE),
    re.compile(r"^closing\s*balance\b", re.IGNORECASE),
    re.compile(r"^payments\s+in\b", re.IGNORECASE),
    re.compile(r"^payments\s+out\b", re.IGNORECASE),
    re.compile(r"^arranged\s*overdraft\s*limit\b", re.IGNORECASE),
    re.compile(r"^po\s+box\b", re.IGNORECASE),
    re.compile(r"^date\s+payment\s+type\b", re.IGNORECASE),
    re.compile(r"^sheet\s+number\b", re.IGNORECASE),
    re.compile(r"^mr\s+\w", re.IGNORECASE),
    re.compile(r"^information\s+about\s+the\s+financial\s+services\s+compensation\s+scheme\b", re.IGNORECASE),
    re.compile(r"\bfinancial\s+services\s+compensation\s+scheme\b", re.IGNORECASE),
    re.compile(r"\bfscs\b", re.IGNORECASE),
    re.compile(r"^interest\s+and\s+charges\b", re.IGNORECASE),
    re.compile(r"^overdrafts\b", re.IGNORECASE),
    re.compile(r"^additional\s+information\b", re.IGNORECASE),
    re.compile(r"^aer\b", re.IGNORECASE),
    re.compile(r"^ear\b", re.IGNORECASE),
]

# ── Suspect / truncation lists ──────────────────────────────
_SUSPECT_NOISE_TOKENS = [
    "PO Box",
    "www.hsbc.co.uk",
    "Your Statement",
    "Account Name",
    "Sortcode",
    "Sheet Number",
    "International Bank Account Number",
]

_DESCRIPTION_TRUNCATE_KEYWORDS = [
    "BALANCECARRIEDFORWARD",
    "BALANCEBROUGHTFORWARD",
]

_SUSPECT_CSV_FIELDNAMES = [
    "source_file", "account_id", "txn_date", "description_raw",
    "amount", "currency", "page", "row_ref",
    "suspect_reason", "description_len",
]
