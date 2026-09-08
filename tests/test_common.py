import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from repo_lib import common


def _stderr(fn, *args):
    err = io.StringIO()
    with redirect_stderr(err):
        fn(*args)
    return err.getvalue()


class LevelPrefixTest(unittest.TestCase):
    def test_error_is_prefixed(self):
        self.assertEqual(_stderr(common.error, "boom"), "error: boom\n")

    def test_warn_is_prefixed(self):
        self.assertEqual(_stderr(common.warn, "careful"), "warning: careful\n")

    def test_info_carries_no_level_prefix(self):
        # The whole point: routine output is the message and nothing else.
        self.assertEqual(_stderr(common.info, "Checking owner/repo..."), "Checking owner/repo...\n")

    def test_every_level_goes_to_stderr_not_stdout(self):
        for fn, arg in ((common.error, "e"), (common.warn, "w"), (common.info, "i")):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                fn(arg)
            self.assertEqual(out.getvalue(), "", fn.__name__)


class ErrorLinesTest(unittest.TestCase):
    def test_only_the_headline_is_prefixed_details_are_indented(self):
        # Relaying a multi-line gh error: the headline is the error, the
        # detail lines nest under it without a second "error:" on each.
        got = _stderr(common.error_lines, "could not read owner/repo:", "line one\nline two")
        self.assertEqual(got, "error: could not read owner/repo:\n  line one\n  line two\n")

    def test_none_detail_prints_just_the_headline(self):
        self.assertEqual(_stderr(common.error_lines, "headline", None), "error: headline\n")


if __name__ == "__main__":
    unittest.main()
