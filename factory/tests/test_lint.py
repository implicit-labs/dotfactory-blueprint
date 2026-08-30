import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lint as factory_lint  # noqa: E402


class FactoryLintTests(unittest.TestCase):
    def test_accepts_small_safe_module(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safe.py"
            path.write_text(
                '"""Safe fixture."""\n\ndef answer(value=None):\n    return value\n',
                encoding="utf-8",
            )
            self.assertEqual([], factory_lint.lint_path(path))

    def test_reports_each_guardrail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe.py"
            path.write_text(
                "from fixture import *\n"
                "def unsafe(values=[]):\n"
                "\ttry:  \n"
                "\t\teval('values')\n"
                "\texcept:\n"
                "\t\treturn values",
                encoding="utf-8",
            )
            codes = {finding.code for finding in factory_lint.lint_path(path)}
            self.assertEqual(
                {"TXT001", "TXT002", "TXT003", "PY002", "PY003", "PY004", "PY005"},
                codes,
            )


if __name__ == "__main__":
    unittest.main()
