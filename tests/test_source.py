r"""The sources themselves compile clean. An invalid escape sequence (`"\("`
where jq's interpolation needs a raw string) is only a warning today, but it
prints over the command's own output on every run, and a future Python turns
it into a SyntaxError."""

import os
import pathlib
import unittest
import warnings

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The package directories, not a walk from ROOT: `uv run ./repo` puts a
# `.venv` there, and third-party code warning is not this repo's failure.
PACKAGES = ("repo_lib", "tests")


def _sources():
    yield ROOT / "repo"
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


class SourceWarningTest(unittest.TestCase):
    def test_the_sources_are_all_found(self):
        found = {p.name for p in _sources()}
        self.assertIn("repo", found)
        self.assertIn("credentials.py", found)
        self.assertIn(os.path.basename(__file__), found)

    def test_no_compile_warnings(self):
        for path in _sources():
            with self.subTest(path=str(path.relative_to(ROOT))):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    compile(path.read_text(), str(path), "exec")
                self.assertEqual([str(w.message) for w in caught], [])


if __name__ == "__main__":
    unittest.main()
