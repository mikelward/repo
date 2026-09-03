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

    def test_run_retries_a_secondary_rate_limit_and_succeeds(self):
        # GitHub's own wording for this (docs.github.com/rest/using-the-
        # rest-api/rate-limits-for-the-rest-api), which gh relays verbatim.
        limited = FakeCompletedProcess(
            1,
            stderr=(
                "gh: You have exceeded a secondary rate limit. Please wait a few minutes "
                "before you try again. (HTTP 403)\n"
            ),
        )
        ok = FakeCompletedProcess(0, stdout="ok\n")
        with patch("subprocess.run", side_effect=[limited, ok]) as mock_run, patch(
            "repo_lib.gh.time.sleep"
        ) as mock_sleep:
            result = gh.run(["api", "user"])
        self.assertEqual(result, "ok\n")
        self.assertEqual(mock_run.call_count, 2)
        mock_sleep.assert_called_once_with(60)

    def test_run_does_not_retry_the_primary_rate_limit(self):
        # The fixed 5000/hour quota -- distinct wording from the secondary
        # limit above, and NOT retried: its reset can be up to an hour
        # away, so blocking a script that long with no explanation is
        # worse than failing and letting the caller decide to wait.
        with patch(
            "subprocess.run",
            return_value=FakeCompletedProcess(
                1, stderr="gh: API rate limit exceeded for user ID 12345. (HTTP 403)\n"
            ),
        ) as mock_run, patch("repo_lib.gh.time.sleep") as mock_sleep:
            with self.assertRaises(gh.GhError):
                gh.run(["api", "user"])
        self.assertEqual(mock_run.call_count, 1)
        mock_sleep.assert_not_called()

    def test_try_run_retries_a_secondary_rate_limit_and_succeeds(self):
        limited = FakeCompletedProcess(
            1, stderr="gh: You have exceeded a secondary rate limit. (HTTP 403)\n"
        )
        ok = FakeCompletedProcess(0, stdout="{}\n")
        with patch("subprocess.run", side_effect=[limited, ok]), patch("repo_lib.gh.time.sleep"):
            result = gh.try_run(["api", "orgs/x"])
        self.assertEqual(result, (True, "{}\n"))

    def test_run_with_input_does_not_retry_a_secondary_rate_limit(self):
        # run_with_input always carries a body -- it's a write, and a write
        # may be the second half of a check-then-act sequence whose safety
        # depends on the precondition check having run immediately before
        # it (Codex review, mikelward/repo#19: apply_gaps's ref-update
        # PATCH, _ensure_environment's existence check, more than one
        # secrets_cmd.py write). A 60s+ wait-and-retry between that check
        # and the write reopens the exact race PR #17 already fixed for
        # the ref-update PATCH specifically -- so no write retries here,
        # regardless of which gh.py entry point it came through.
        with patch(
            "subprocess.run",
            return_value=FakeCompletedProcess(
                1, stderr=b"gh: You have exceeded a secondary rate limit. (HTTP 403)\n"
            ),
        ) as mock_run, patch("repo_lib.gh.time.sleep") as mock_sleep:
            with self.assertRaises(gh.GhError):
                gh.run_with_input(["secret", "set", "NAME"], b"the-value")
        self.assertEqual(mock_run.call_count, 1)
        mock_sleep.assert_not_called()

    def test_run_does_not_retry_a_mutating_api_call(self):
        # gh.run() is not read-only either -- credentials.py's environment
        # delete is `gh.run(["api", "--method", "DELETE", ...])`. Detected
        # by --method, not by which of run/try_run/run_with_input was
        # called (Codex review, mikelward/repo#19).
        with patch(
            "subprocess.run",
            return_value=FakeCompletedProcess(
                1, stderr="gh: You have exceeded a secondary rate limit. (HTTP 403)\n"
            ),
        ) as mock_run, patch("repo_lib.gh.time.sleep") as mock_sleep:
            with self.assertRaises(gh.GhError):
                gh.run(["api", "--method", "DELETE", "repos/o/r/environments/e"])
        self.assertEqual(mock_run.call_count, 1)
        mock_sleep.assert_not_called()

    def test_a_persistent_secondary_rate_limit_still_fails_cleanly(self):
        # More secondary-limit failures than the retry budget allows --
        # must still fail with the ordinary GhError, not retry forever.
        limited = FakeCompletedProcess(
            1, stderr="gh: You have exceeded a secondary rate limit. (HTTP 403)\n"
        )
        with patch("subprocess.run", return_value=limited) as mock_run, patch(
            "repo_lib.gh.time.sleep"
        ) as mock_sleep:
            with self.assertRaises(gh.GhError):
                gh.run(["api", "user"])
        self.assertEqual(mock_run.call_count, gh._RATE_LIMIT_RETRY_ATTEMPTS)
        self.assertEqual(mock_sleep.call_count, gh._RATE_LIMIT_RETRY_ATTEMPTS - 1)

    def test_token_scopes_parses_the_x_oauth_scopes_header(self):
        raw = "HTTP/2.0 200 OK\r\nContent-Type: application/json\r\nX-OAuth-Scopes: gist, read:org, repo\r\n\r\n{}"
        with patch("subprocess.run", return_value=FakeCompletedProcess(0, stdout=raw)) as mock_run:
            scopes = gh.token_scopes()
        self.assertEqual(scopes, {"gist", "read:org", "repo"})
        mock_run.assert_called_once_with(
            ["gh", "api", "-i", "user"], capture_output=True, text=True
        )

    def test_token_scopes_header_name_matched_case_insensitively(self):
        raw = "HTTP/2.0 200 OK\r\nx-oauth-scopes: repo, workflow\r\n\r\n{}"
        with patch("subprocess.run", return_value=FakeCompletedProcess(0, stdout=raw)):
            self.assertEqual(gh.token_scopes(), {"repo", "workflow"})

    def test_token_scopes_returns_none_when_the_header_is_absent(self):
        # A fine-grained PAT or GitHub App installation token carries no
        # OAuth scopes at all -- this must read as "can't tell", not as
        # "confirmed empty" (mikelward/repo#18: a caller gating a write on
        # a missing scope must not block a token this can't answer for).
        raw = "HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n{}"
        with patch("subprocess.run", return_value=FakeCompletedProcess(0, stdout=raw)):
            self.assertIsNone(gh.token_scopes())

    def test_token_scopes_returns_none_on_a_failed_read(self):
        with patch(
            "subprocess.run",
            return_value=FakeCompletedProcess(1, stderr="gh: HTTP 401: Bad credentials\n"),
        ):
            self.assertIsNone(gh.token_scopes())

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
