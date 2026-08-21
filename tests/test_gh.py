import subprocess
import unittest
from unittest.mock import patch

from repo_lib import gh


class FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GhWrapperTest(unittest.TestCase):
    """Exercises repo_lib.gh itself -- the subprocess.run boundary --
    rather than the gh.run/try_run boundary test_list_cmd.py mocks at.
    Higher-level tests can treat gh.run/try_run as the seam because this
    file is what proves that seam actually does what it claims."""

    def test_run_returns_stdout_on_success(self):
        with patch(
            "subprocess.run", return_value=FakeCompletedProcess(0, stdout="ok\n")
        ) as mock_run:
            result = gh.run(["api", "user"])
        self.assertEqual(result, "ok\n")
        mock_run.assert_called_once_with(
            ["gh", "api", "user"], capture_output=True, text=True
        )

    def test_run_raises_gherror_with_stderr_on_failure(self):
        with patch(
            "subprocess.run",
            return_value=FakeCompletedProcess(1, stderr="gh: not found\n"),
        ):
            with self.assertRaises(gh.GhError) as cm:
                gh.run(["api", "nope"])
        self.assertEqual(cm.exception.stderr, "gh: not found\n")

    def test_try_run_returns_ok_true_and_stdout_on_success(self):
        with patch(
            "subprocess.run", return_value=FakeCompletedProcess(0, stdout="{}\n")
        ):
            ok, output = gh.try_run(["api", "orgs/x"])
        self.assertTrue(ok)
        self.assertEqual(output, "{}\n")

    def test_try_run_returns_ok_false_and_stderr_on_failure(self):
        with patch(
            "subprocess.run",
            return_value=FakeCompletedProcess(1, stderr="gh: HTTP 404\n"),
        ):
            ok, output = gh.try_run(["api", "orgs/x"])
        self.assertFalse(ok)
        self.assertEqual(output, "gh: HTTP 404\n")

    def test_run_never_invokes_a_shell(self):
        # The whole point of subprocess.run(["gh", *args]) over a shell
        # string is no quoting/injection hazard -- assert the argument list
        # form rather than trusting a docstring to stay true.
        with patch(
            "subprocess.run", return_value=FakeCompletedProcess(0, stdout="")
        ) as mock_run:
            gh.run(["api", "repos/o/r; rm -rf /"])
        called_args = mock_run.call_args
        self.assertNotIn("shell", called_args.kwargs)
        self.assertEqual(
            called_args.args[0], ["gh", "api", "repos/o/r; rm -rf /"]
        )

    def test_run_with_input_returns_raw_stdout_bytes_on_success(self):
        # Bytes throughout, not text=True -- a secret's value is opaque
        # data, not something to decode/re-encode through a text codec.
        with patch(
            "subprocess.run",
            return_value=FakeCompletedProcess(0, stdout=b"\x00\x01ok"),
        ) as mock_run:
            result = gh.run_with_input(["secret", "set", "NAME"], b"the-value")
        self.assertEqual(result, b"\x00\x01ok")
        mock_run.assert_called_once_with(
            ["gh", "secret", "set", "NAME"], input=b"the-value", capture_output=True
        )
        self.assertNotIn("text", mock_run.call_args.kwargs)

    def test_run_with_input_raises_gherror_with_decoded_stderr_on_failure(self):
        with patch(
            "subprocess.run",
            return_value=FakeCompletedProcess(1, stderr=b"gh: HTTP 403: Forbidden\n"),
        ):
            with self.assertRaises(gh.GhError) as cm:
                gh.run_with_input(["secret", "set", "NAME"], b"the-value")
        self.assertEqual(cm.exception.stderr, "gh: HTTP 403: Forbidden\n")

    def test_run_with_input_never_invokes_a_shell(self):
        with patch(
            "subprocess.run", return_value=FakeCompletedProcess(0, stdout=b"")
        ) as mock_run:
            gh.run_with_input(["secret", "set", "NAME", "--repo", "o/r; rm -rf /"], b"v")
        self.assertNotIn("shell", mock_run.call_args.kwargs)
        self.assertEqual(
            mock_run.call_args.args[0],
            ["gh", "secret", "set", "NAME", "--repo", "o/r; rm -rf /"],
        )

    def test_require_gh_raises_when_gh_is_missing(self):
        with patch("shutil.which", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                gh.require_gh()
        self.assertEqual(cm.exception.code, 1)

    def test_require_gh_is_silent_when_gh_is_present(self):
        with patch("shutil.which", return_value="/usr/bin/gh"):
            gh.require_gh()  # must not raise


if __name__ == "__main__":
    unittest.main()
