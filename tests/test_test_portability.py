"""Test fixtures must respect the caller's temporary-directory policy."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class TestFixturePortability(unittest.TestCase):
    def test_account_fixtures_preserve_and_use_caller_tmpdir(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as parent:
            env = dict(os.environ, TMPDIR=parent,
                       BANKSTATEMENT_PROJECT_ROOT=str(root))
            probe = """
import os
from pathlib import Path
import runpy
import sys

expected = Path(sys.argv[1]).resolve()
namespace = runpy.run_path('tests/test_accounts.py')
assert Path(os.environ['TMPDIR']).resolve() == expected, 'test module replaced caller TMPDIR'
case = namespace['AccountTests']()
try:
    case.setUp()
    assert case.root.parent.resolve() == expected, 'fixture ignored caller TMPDIR'
finally:
    case.doCleanups()
"""
            result = subprocess.run(
                [sys.executable, '-c', probe, parent], cwd=root, env=env,
                capture_output=True, text=True, timeout=60,
            )
            # Do not include subprocess output, which may expose local paths.
            self.assertEqual(result.returncode, 0,
                             'account fixture portability subprocess failed')
