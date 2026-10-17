"""Synthetic git repositories only; no live git/index operations."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.services import category_rules as rules


class RulePrivacyTests(unittest.TestCase):
    def test_actual_rule_artifacts_ignored_but_authority_stageable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            ignore = Path(__file__).resolve().parents[1] / '.gitignore'
            if ignore.exists():
                shutil.copyfile(ignore, root / '.gitignore')
            config = root / 'config'
            config.mkdir()
            path = config / 'category_rules.csv'
            path.write_text('merchant_core,category,subcategory\nSHOP,Old,Old\n')
            (config / 'categories.json').write_text('{}')
            rules.change(path, rules.snapshot(path)['digest'], 'SHOP', ('New', 'New'), True)
            artifacts = list((config / '.category_rules.csv.history').iterdir())
            # Exercise the actual staging filename while the replace is in flight.
            real_replace = rules.os.replace
            from unittest.mock import patch
            def inspect_staging(source, target):
                relative = Path(source).relative_to(root).as_posix()
                result = subprocess.run(['git', '-C', str(root), 'check-ignore', relative], capture_output=True)
                self.assertEqual(result.returncode, 0, relative)
                return real_replace(source, target)
            with rules._write_lock(path):
                artifacts.append(config / '.category_rules.csv.lock')
                for artifact in artifacts:
                    result = subprocess.run(['git', '-C', str(root), 'check-ignore', artifact.relative_to(root).as_posix()], capture_output=True)
                    self.assertEqual(result.returncode, 0, artifact.name)
                with patch.object(rules.os, 'replace', side_effect=inspect_staging):
                    rules._atomic_write(path, path.read_bytes())
                    rules._atomic_write(artifacts[0], artifacts[0].read_bytes())
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            staged = subprocess.run(['git', '-C', str(root), 'diff', '--cached', '--name-only'], capture_output=True, text=True, check=True).stdout.splitlines()
            self.assertIn('config/category_rules.csv', staged)
            self.assertIn('config/categories.json', staged)
            self.assertFalse(any('.history/' in p or p.endswith(('.lock', '.tmp')) for p in staged))
