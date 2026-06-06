from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openmia_webhooker import cli


class CLITests(unittest.TestCase):
    def test_hidden_openmia_banner(self) -> None:
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            exit_code = cli.main(["openmia"])

        output = out.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertTrue(output.startswith("\n"))
        self.assertTrue(output.endswith("\n\n"))
        self.assertIn("$$$$$$\\", output)
        self.assertIn(r"\______/ $$  ____/", output)
        self.assertIn(r"\__|", output)

    def test_help_does_not_show_hidden_openmia_banner_command(self) -> None:
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            exit_code = cli.main(["--help"])

        output = out.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("claude-code-watch", output)
        self.assertIn("{codex,claude-code,claude-code-run,claude-code-watch,doctor}", output)
        self.assertNotIn("openmia}", output)


if __name__ == "__main__":
    unittest.main()
