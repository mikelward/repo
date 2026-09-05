"""The `repo` entry point itself: the one place the PyYAML requirement is
checked, so a missing install is a sentence naming the fix rather than a
traceback out of repo_lib."""

import os
import subprocess
import sys
import unittest

REPO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "repo")


class EntryPointTest(unittest.TestCase):
    def test_a_missing_pyyaml_names_the_install(self):
        # `-S` leaves site-packages off sys.path, which is where PyYAML
        # lives however it was installed; the standard library and repo_lib
        # (which the script puts on the path itself) are unaffected.
        result = subprocess.run(
            [sys.executable, "-S", REPO, "--help"], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("PyYAML is not installed", result.stderr)
        self.assertIn("uv run ./repo", result.stderr)
        self.assertIn("python3 -m pip install pyyaml", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_with_pyyaml_the_command_runs(self):
        result = subprocess.run([sys.executable, REPO, "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: repo", result.stdout)


if __name__ == "__main__":
    unittest.main()
