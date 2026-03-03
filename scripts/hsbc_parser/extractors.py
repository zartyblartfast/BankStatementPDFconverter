"""PDF extraction: convert PDF pages into (page_number, line_text) tuples."""

import logging
import re
from pathlib import Path

from scripts.hsbc_parser.models import WordExtractorDiagnostics
from scripts.hsbc_parser.patterns import (
    _DEFAULT_Y_TOLERANCE,
    _SUSPECT_MERGED_LINE_LEN,
    _SUSPECT_MERGED_TOKEN_THRESHOLD,
)

logger = logging.getLogger(__name__)


def _import_pdfplumber():
    """Lazy import with a clear error message."""
    try:
        import pdfplumber  # type: ignore
        return pdfplumber
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            "Missing dependency 'pdfplumber'. Install requirements.txt before running PDF extraction."
        ) from e


def parse_pdf_to_lines(pdf_path: str) -> list[tuple[int, str]]:
    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        resolved = pdf_file.resolve()
        cwd = Path.cwd()
        raise FileNotFoundError(
            f"PDF not found: {pdf_file} (resolved: {resolved}). Current working directory: {cwd}"
        )

    pdfplumber = _import_pdfplumber()

    lines: list[tuple[int, str]] = []
    with pdfplumber.open(str(pdf_file)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for raw_line in text.splitlines():
                line = raw_line.rstrip("\n").strip("\r")
                if line.strip() == "":
                    continue
                lines.append((i, line))
    return lines


def parse_pdf_to_lines_words(
    pdf_path: str,
    *,
    y_tolerance: float = _DEFAULT_Y_TOLERANCE,
) -> tuple[list[tuple[int, str]], WordExtractorDiagnostics]:
    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        resolved = pdf_file.resolve()
        cwd = Path.cwd()
        raise FileNotFoundError(
            f"PDF not found: {pdf_file} (resolved: {resolved}). Current working directory: {cwd}"
        )

    pdfplumber = _import_pdfplumber()

    diag = WordExtractorDiagnostics(y_tolerance=y_tolerance)

    out_lines: list[tuple[int, str]] = []
    with pdfplumber.open(str(pdf_file)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            words = page.extract_words() or []
            if not words:
                continue
            diag.total_pdf_words += len(words)

            try:
                words_sorted = sorted(words, key=lambda w: (float(w.get("top", 0.0)), float(w.get("x0", 0.0))))
            except (TypeError, ValueError):
                words_sorted = words

            groups: list[tuple[float, list[dict[str, object]]]] = []
            for w in words_sorted:
                top_v = w.get("top")
                try:
                    top = float(top_v) if top_v is not None else 0.0
                except (TypeError, ValueError):
                    top = 0.0

                if not groups:
                    groups.append((top, [w]))
                    continue

                prev_top, prev_words = groups[-1]
                if abs(top - prev_top) <= y_tolerance:
                    prev_words.append(w)
                else:
                    groups.append((top, [w]))

            page_line_count = 0
            for _top, gwords in groups:
                try:
                    gwords_sorted = sorted(gwords, key=lambda w: float(w.get("x0", 0.0)))
                except (TypeError, ValueError):
                    gwords_sorted = gwords

                texts: list[str] = []
                for w in gwords_sorted:
                    t = str(w.get("text", "")).strip()
                    if t:
                        texts.append(t)
                if not texts:
                    continue

                line = " ".join(texts).strip()
                if not line:
                    continue

                out_lines.append((i, line))
                page_line_count += 1
                token_count = len(texts)
                diag.tokens_per_line_counts[token_count] = (
                    diag.tokens_per_line_counts.get(token_count, 0) + 1
                )

                # Flag lines where a balance marker keyword appears alongside
                # other substantive tokens — strong signal of column mis-merge.
                line_norm = re.sub(r"[^a-z0-9]", "", line.lower())
                has_marker = (
                    "balancecarriedforward" in line_norm
                    or "balancebroughtforward" in line_norm
                )
                if has_marker and (token_count > _SUSPECT_MERGED_TOKEN_THRESHOLD or len(line) > _SUSPECT_MERGED_LINE_LEN):
                    diag.suspect_merged_lines.append(
                        {"page": i, "token_count": token_count, "line": line}
                    )

            diag.lines_per_page[i] = page_line_count

    diag.total_output_lines = len(out_lines)
    return out_lines, diag
