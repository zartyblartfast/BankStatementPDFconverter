"""Run all statement PDFs and collect reconciliation results."""
import subprocess
import sys
from pathlib import Path


def _is_zero(v: str) -> bool:
    """Check if a diff value string is effectively zero."""
    if v == "?":
        return True
    try:
        return abs(float(v)) < 0.005
    except ValueError:
        return False


pdf_dir = Path("StatementsPDF")
outdir = Path("finance/exports")
pdfs = sorted(pdf_dir.glob("*.pdf"))

results = []
for pdf in pdfs:
    print(f"\n{'='*60}")
    print(f"Processing: {pdf.name}")
    print(f"{'='*60}")
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.hsbc_pdf_to_txn", "--pdf", str(pdf)],
        capture_output=True, text=True
    )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    
    # Parse key metrics from stdout
    metrics = {}
    for line in stdout.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            metrics[key.strip()] = val.strip()
    
    result = {
        "file": pdf.name,
        "exit_code": proc.returncode,
        "txn_count": metrics.get("transaction_count", "?"),
        "credits": metrics.get("total_credits", "?"),
        "debits": metrics.get("total_debits", "?"),
        "diff_credits": metrics.get("diff_credits_vs_statement_in", "?"),
        "diff_debits": metrics.get("diff_debits_vs_statement_out", "?"),
        "diff_delta": metrics.get("diff_txn_delta_vs_statement_delta", "?"),
        "warnings": metrics.get("any_parse_warnings_count", "?"),
        "suspects": metrics.get("suspects_count", "?"),
    }
    results.append(result)
    
    if stderr:
        print(f"STDERR: {stderr}")
    
    # Print concise summary
    ok = _is_zero(result["diff_credits"]) and _is_zero(result["diff_debits"]) and _is_zero(result["diff_delta"])
    status = "PASS" if ok and proc.returncode == 0 else "FAIL"
    print(f"  {status} | txns={result['txn_count']} | credits={result['credits']} | debits={result['debits']}")
    print(f"       | diff_credits={result['diff_credits']} | diff_debits={result['diff_debits']} | diff_delta={result['diff_delta']}")
    print(f"       | warnings={result['warnings']} | suspects={result['suspects']}")

# Final summary
print(f"\n{'='*60}")
print("SUMMARY")
print(f"{'='*60}")
total = len(results)
passed = sum(1 for r in results if _is_zero(r["diff_credits"]) and _is_zero(r["diff_debits"]) and _is_zero(r["diff_delta"]) and r["exit_code"] == 0)
total_txns = sum(int(r["txn_count"]) for r in results if r["txn_count"] != "?")
total_warnings = sum(int(r["warnings"]) for r in results if r["warnings"] != "?")
total_suspects = sum(int(r["suspects"]) for r in results if r["suspects"] != "?")

print(f"Statements: {passed}/{total} PASS")
print(f"Total transactions: {total_txns}")
print(f"Total warnings: {total_warnings}")
print(f"Total suspects: {total_suspects}")

if passed < total:
    print("\nFAILED statements:")
    for r in results:
        if not (_is_zero(r["diff_credits"]) and _is_zero(r["diff_debits"]) and _is_zero(r["diff_delta"]) and r["exit_code"] == 0):
            print(f"  {r['file']}: diff_credits={r['diff_credits']} diff_debits={r['diff_debits']} diff_delta={r['diff_delta']}")
