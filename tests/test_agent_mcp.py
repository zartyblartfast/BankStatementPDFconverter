from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from scripts.init_db import init_db
from scripts.migrate_db import migrate_db
from scripts.services.transfers import prepare_pair, commit_transfer

REPO = Path(__file__).resolve().parents[1]
AGENT_PYTHON = REPO / ".venv-agent/Scripts/python.exe"


@unittest.skipUnless(AGENT_PYTHON.is_file(), "Install the optional isolated MCP environment first.")
class AgentMCPTests(unittest.TestCase):
    def test_real_stdio_protocol_readonly_tools_and_privacy_gate(self):
        self._exercise_protocol(False)

    def test_confirmed_transfers_over_actual_stdio_aggregate_contract(self):
        self._exercise_protocol(True)

    def test_legacy_real_amounts_over_actual_stdio_without_transfer_schema(self):
        self._exercise_protocol(False, legacy=True)

    def test_legacy_real_amounts_over_actual_stdio_with_confirmed_transfers(self):
        self._exercise_protocol(True, legacy=True)

    def _exercise_protocol(self, confirmed, legacy=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            tree = root / "config/categories.json"
            tree.write_text('{"Fixture":["Test"]}')
            db = root / "finance/ledger.db"
            init_db(db_path=db)
            with closing(sqlite3.connect(db)) as conn:
                for i, amount in ((1, 600), (2, -100)):
                    conn.execute(
                        "INSERT INTO transactions(txn_date,description_raw,amount,source_file,row_ref,import_hash) VALUES('2026-01-01','Synthetic',?,'fixture',?,?)",
                        (amount, str(i), str(i)),
                    )
                if legacy:
                    conn.execute('UPDATE transactions SET amount=? WHERE txn_id=1', (600.0000000000001,))
                    conn.execute('UPDATE transactions SET amount=? WHERE txn_id=2', (-100.00000000000003,))
                conn.commit()
            if confirmed:
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,account_id,source_file,row_ref,import_hash) VALUES ('2026-01-02','Synthetic',-600,'IOM','fixture','3','3')")
                if legacy:
                    with closing(sqlite3.connect(db)) as conn, conn:
                        conn.execute('UPDATE transactions SET amount=? WHERE txn_id=3', (-600.0000000000001,))
                migrate_db(db)
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("UPDATE accounts SET owned=1,currency='GBP' WHERE account_id='IOM'")
                commit_transfer(prepare_pair(1, 3, db_path=db), approved=True)
            before = db.read_bytes()
            defs = tree.read_bytes()
            completed = subprocess.run(
                [str(AGENT_PYTHON), "-m", "tests.mcp_analysis_probe", "--fixture-root", str(root)]
                + (['--confirmed'] if confirmed else []),
                cwd=REPO,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["handshake"], "ok")
            self.assertEqual(result["aggregate_calls"], 4)
            self.assertTrue(result["details_denied"])
            self.assertTrue(result["write_tool_absent"])
            self.assertEqual(result["invalid_inputs_rejected"], 3)
            self.assertEqual(result["opted_in"]["handshake"], "ok")
            self.assertFalse(result["opted_in"]["details_denied"])
            self.assertTrue(result["opted_in"]["write_tool_absent"])
            self.assertEqual(result["opted_in"]["invalid_inputs_rejected"], 3)
            self.assertEqual(before, db.read_bytes())
            self.assertEqual(defs, tree.read_bytes())
