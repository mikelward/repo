import contextlib
import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import mock_open, patch

from repo_lib import gh
from repo_lib.cli import main


class FakeStdin:
    """Stands in for sys.stdin: controls whether it looks like a terminal
    and what the secret value read from it (via .buffer.read()) would be."""

    def __init__(self, isatty, data=b""):
        self._isatty = isatty
        self.buffer = io.BytesIO(data)

    def isatty(self):
        return self._isatty


class FakeGh:
    """Models a small fleet of repos for repo_lib.gh.run/try_run/
    run_with_input, closely mirroring repo-secrets_test's own fixture in
    mikelward/scripts but as plain Python state, no fake binary on PATH.
    """

    def __init__(self):
        self.repos = {}
        self.calls = []
        self.set_calls = []
        # repo -> secret name to "discover" (simulate someone else having
        # just created it) right after the FIRST read of that repo -- for
        # the write-time recheck race.
        self.race_repos = {}

    def add_repo(
        self,
        name,
        *,
        secrets=(),
        repo_reachable=True,
        env_secrets=None,
        env_error=None,
        env_get_check_error=None,
        set_fails=False,
    ):
        """env_secrets=None means the environment doesn't exist yet (only
        relevant when the command was given --env); pass a set to make it
        already exist. env_error='other' simulates a non-404 failure
        (403/500) reading the environment-scoped SECRETS-LIST endpoint --
        must NOT be read as "environment missing", unlike a real 404.
        env_get_check_error='other' simulates the same for the write-time
        GET repos/{repo}/environments/{env} existence recheck specifically
        (a distinct endpoint/call site from the secrets list)."""
        self.repos[name] = {
            "repo_reachable": repo_reachable,
            "secrets": set(secrets),
            "env_secrets": None if env_secrets is None else set(env_secrets),
            "env_error": env_error,
            "env_get_check_error": env_get_check_error,
            "set_fails": set_fails,
        }

    def _respond(self, repo, names):
        result = "".join(n + "\n" for n in sorted(names))
        if repo in self.race_repos:
            names.add(self.race_repos.pop(repo))
        return result

    def run(self, args):
        self.calls.append(list(args))
        # The plan-build/recheck reads use --paginate; the "does the repo
        # itself exist" probe after an environment 404 does not (mirroring
        # repo-secrets' own shell source, which only paginates the two
        # reads that actually enumerate a secrets list).
        assert args[0] == "api", args
        endpoint = args[1] if args[1] != "--paginate" else args[2]
        m = re.match(r"^repos/([^/]+/[^/]+)/environments/([^/]+)/secrets$", endpoint)
        if m:
            repo = m.group(1)
            st = self.repos[repo]
            if st["env_error"] == "other":
                raise gh.GhError(
                    "gh: HTTP 403: Resource protected by organization SAML enforcement\n"
                )
            if st["env_secrets"] is None:
                raise gh.GhError(f"gh: HTTP 404: Not Found (.../{endpoint})\n")
            return self._respond(repo, st["env_secrets"])
        m = re.match(r"^repos/([^/]+/[^/]+)/actions/secrets$", endpoint)
        if m:
            repo = m.group(1)
            st = self.repos[repo]
            if not st["repo_reachable"]:
                raise gh.GhError(f"gh: HTTP 404: Not Found (.../{endpoint})\n")
            return self._respond(repo, st["secrets"])
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    def try_run(self, args):
        self.calls.append(list(args))
        if args[1:2] == ["--method"]:
            assert args[2] == "PUT"
            endpoint = args[3]
            repo, env = re.match(
                r"^repos/([^/]+/[^/]+)/environments/([^/]+)$", endpoint
            ).groups()
            st = self.repos[repo]
            if st["env_secrets"] is None:
                st["env_secrets"] = set()
            return True, ""
        endpoint = args[1]
        repo, env = re.match(
            r"^repos/([^/]+/[^/]+)/environments/([^/]+)$", endpoint
        ).groups()
        st = self.repos[repo]
        if st["env_get_check_error"] == "other":
            return False, "gh: HTTP 403: Resource protected by organization SAML enforcement\n"
        if st["env_secrets"] is not None:
            return True, ""
        return False, f"gh: HTTP 404: Not Found (.../{endpoint})\n"

    def run_with_input(self, args, input_bytes):
        self.calls.append(list(args))
        assert args[:2] == ["secret", "set"]
        name = args[2]
        repo = args[args.index("--repo") + 1]
        env = args[args.index("--env") + 1] if "--env" in args else None
        self.set_calls.append((name, repo, env, input_bytes))
        st = self.repos[repo]
        if st["set_fails"]:
            raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
        if env:
            if st["env_secrets"] is None:
                st["env_secrets"] = set()
            st["env_secrets"].add(name)
        else:
            st["secrets"].add(name)
        return b""


def run_repo_secrets(fake, argv, *, isatty=False, stdin_data=b"", confirm=None):
    """Runs `repo secrets <argv>` against `fake`, returns (status, stdout,
    stderr)."""
    out = io.StringIO()
    err = io.StringIO()
    status = 0
    patches = [
        patch("repo_lib.gh.run", side_effect=fake.run),
        patch("repo_lib.gh.try_run", side_effect=fake.try_run),
        patch("repo_lib.gh.run_with_input", side_effect=fake.run_with_input),
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("sys.stdin", new=FakeStdin(isatty, stdin_data)),
    ]
    if confirm is not None:
        patches.append(patch("builtins.input", return_value=confirm))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with redirect_stdout(out), redirect_stderr(err):
            try:
                main(["secrets", *argv])
            except SystemExit as e:
                status = e.code if isinstance(e.code, int) else 1
    return status, out.getvalue(), err.getvalue()


class SecretsCmdTest(unittest.TestCase):
    def test_writes_a_new_repository_level_secret_from_stdin(self):
        fake = FakeGh()
        fake.add_repo("o/a")
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "--force", "o/a"], stdin_data=b"s3cr3t"
        )
        self.assertEqual(status, 0)
        self.assertEqual(out, "o/a: set 'TOKEN'\n")
        self.assertEqual(fake.set_calls, [("TOKEN", "o/a", None, b"s3cr3t")])

    def test_force_skips_the_question_but_still_prints_the_plan(self):
        # --force is "I have already decided, stop asking" -- not
        # "apply silently". An unattended run still needs the
        # OVERWRITES-an-existing-value warning in its own output.
        fake = FakeGh()
        fake.add_repo("o/existing", secrets={"TOKEN"})
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "--force", "o/existing"], stdin_data=b"s3cr3t"
        )
        self.assertEqual(status, 0)
        self.assertIn("About to set secret 'TOKEN' (repository-level) on:", err)
        self.assertIn("o/existing: OVERWRITES an existing value", err)
        self.assertNotIn("Apply this to every repository above?", err)

    def test_writes_a_new_environment_scoped_secret_creating_the_environment(self):
        fake = FakeGh()
        fake.add_repo("o/a")  # env_secrets=None -> environment doesn't exist yet
        status, out, err = run_repo_secrets(
            fake,
            ["--name", "TOKEN", "--env", "prod", "--force", "o/a"],
            stdin_data=b"s3cr3t",
        )
        self.assertEqual(status, 0)
        self.assertEqual(out, "o/a: set 'TOKEN' (environment 'prod')\n")
        put_calls = [c for c in fake.calls if c[1:2] == ["--method"]]
        self.assertEqual(len(put_calls), 1)
        self.assertEqual(fake.set_calls, [("TOKEN", "o/a", "prod", b"s3cr3t")])

    def test_environment_scoped_write_does_not_recreate_an_existing_environment(self):
        fake = FakeGh()
        fake.add_repo("o/a", env_secrets=set())  # environment already exists, empty
        status, out, err = run_repo_secrets(
            fake,
            ["--name", "TOKEN", "--env", "prod", "--force", "o/a"],
            stdin_data=b"s3cr3t",
        )
        self.assertEqual(status, 0)
        # Never PUT to environments/prod -- would silently reset its
        # protection settings if it already exists.
        put_calls = [c for c in fake.calls if c[1:2] == ["--method"]]
        self.assertEqual(put_calls, [])

    def test_a_non_404_environment_existence_recheck_refuses_to_put(self):
        # A 403/500/transient failure on the write-time GET
        # repos/{repo}/environments/{env} recheck must NOT be treated as
        # "confirmed missing, safe to create" -- only a genuine 404 may.
        # Otherwise a real, existing environment this call merely couldn't
        # reach right now gets PUT anyway, silently resetting its
        # protection settings.
        fake = FakeGh()
        fake.add_repo("o/a", env_get_check_error="other")
        status, out, err = run_repo_secrets(
            fake,
            ["--name", "TOKEN", "--env", "prod", "--force", "o/a"],
            stdin_data=b"s3cr3t",
        )
        self.assertEqual(status, 1)
        put_calls = [c for c in fake.calls if c[1:2] == ["--method"]]
        self.assertEqual(put_calls, [])
        self.assertEqual(fake.set_calls, [])

    def test_overwrite_is_case_insensitive_and_needs_no_environment_creation(self):
        fake = FakeGh()
        fake.add_repo("o/a", secrets={"token"})
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "--force", "o/a"], stdin_data=b"new-value"
        )
        self.assertEqual(status, 0)
        self.assertEqual(out, "o/a: set 'TOKEN'\n")

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        # No --file, and stdin looks like an interactive terminal with
        # nothing piped in -- a dry run can't affect and doesn't need the
        # value, so it must succeed anyway rather than being refused for a
        # value it will never read.
        fake = FakeGh()
        fake.add_repo("o/existing", secrets={"TOKEN"})
        fake.add_repo("o/new")
        status, out, err = run_repo_secrets(
            fake,
            ["--name", "TOKEN", "--dry-run", "o/existing", "o/new"],
            isatty=True,
        )
        self.assertEqual(status, 0)
        self.assertIn("About to set secret 'TOKEN' (repository-level) on:", out)
        self.assertIn("o/existing: OVERWRITES an existing value", out)
        self.assertIn("o/new: new", out)
        self.assertEqual(fake.set_calls, [])

    def test_dry_run_exits_nonzero_when_a_repo_could_not_be_read(self):
        fake = FakeGh()
        fake.add_repo("o/bad", repo_reachable=False)
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "--dry-run", "o/bad"], stdin_data=b"s3cr3t"
        )
        self.assertEqual(status, 1)
        self.assertIn("SKIPPED", out)

    def test_all_repos_unreadable_is_a_hard_error_without_confirming(self):
        # Nothing left to write, so (like --dry-run) no value is needed --
        # an interactive, unpiped stdin must not be refused here either.
        fake = FakeGh()
        fake.add_repo("o/bad", repo_reachable=False)
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "o/bad"], isatty=True
        )
        self.assertEqual(status, 1)
        self.assertEqual(out, "")
        self.assertEqual(fake.set_calls, [])

    def test_a_bad_repo_does_not_abort_the_rest_of_the_batch(self):
        fake = FakeGh()
        fake.add_repo("o/bad", repo_reachable=False)
        fake.add_repo("o/good")
        status, out, err = run_repo_secrets(
            fake,
            ["--name", "TOKEN", "--force", "o/bad", "o/good"],
            stdin_data=b"s3cr3t",
        )
        self.assertEqual(status, 1)
        self.assertIn("o/good: set 'TOKEN'", out)
        self.assertIn("failed on: o/bad", err)

    def test_a_non_404_environment_read_failure_is_a_hard_error_not_a_probe(self):
        # 403/SSO/transient on the environment-scoped read must NOT be
        # read as "environment missing" -- only a genuine 404 triggers the
        # repo-existence probe.
        fake = FakeGh()
        fake.add_repo("o/a", env_error="other")
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "--env", "prod", "o/a"], stdin_data=b"s3cr3t"
        )
        self.assertEqual(status, 1)
        self.assertEqual(fake.set_calls, [])

    def test_confirmation_declined_writes_nothing(self):
        # A confirmation prompt needs a real terminal on stdin -- so the
        # value has to come from --file here, not stdin (an isatty stdin
        # refuses to supply the value at all; see the interactive-terminal
        # refusal test below).
        fake = FakeGh()
        fake.add_repo("o/a")
        with patch("builtins.open", mock_open(read_data=b"s3cr3t")):
            status, out, err = run_repo_secrets(
                fake,
                ["--name", "TOKEN", "--file", "value.txt", "o/a"],
                isatty=True,
                confirm="n",
            )
        self.assertEqual(status, 1)
        self.assertEqual(fake.set_calls, [])

    def test_confirmation_accepted_writes(self):
        fake = FakeGh()
        fake.add_repo("o/a")
        with patch("builtins.open", mock_open(read_data=b"s3cr3t")):
            status, out, err = run_repo_secrets(
                fake,
                ["--name", "TOKEN", "--file", "value.txt", "o/a"],
                isatty=True,
                confirm="y",
            )
        self.assertEqual(status, 0)
        self.assertEqual(len(fake.set_calls), 1)

    def test_no_tty_and_no_force_refuses(self):
        fake = FakeGh()
        fake.add_repo("o/a")
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "o/a"], isatty=False, stdin_data=b"s3cr3t"
        )
        self.assertEqual(status, 1)
        self.assertEqual(fake.set_calls, [])

    def test_a_secret_created_by_someone_else_since_the_plan_was_built_is_refused(self):
        fake = FakeGh()
        fake.add_repo("o/a")
        fake.race_repos["o/a"] = "TOKEN"
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "--force", "o/a"], stdin_data=b"s3cr3t"
        )
        self.assertEqual(status, 1)
        self.assertIn("was created by", err)
        self.assertEqual(fake.set_calls, [])

    def test_dedupes_repos_case_insensitively_preserving_first_occurrence(self):
        fake = FakeGh()
        fake.add_repo("o/a")
        status, out, err = run_repo_secrets(
            fake,
            ["--name", "TOKEN", "--force", "o/a", "O/A"],
            stdin_data=b"s3cr3t",
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(fake.set_calls), 1)

    def test_value_from_a_file(self):
        fake = FakeGh()
        fake.add_repo("o/a")
        with patch("builtins.open", mock_open(read_data=b"from-file")):
            status, out, err = run_repo_secrets(
                fake,
                ["--name", "TOKEN", "--force", "--file", "value.txt", "o/a"],
            )
        self.assertEqual(status, 0)
        self.assertEqual(fake.set_calls, [("TOKEN", "o/a", None, b"from-file")])

    def test_unreadable_file_is_a_usage_error(self):
        fake = FakeGh()
        fake.add_repo("o/a")
        with patch("builtins.open", side_effect=OSError("no such file")):
            status, out, err = run_repo_secrets(
                fake,
                ["--name", "TOKEN", "--force", "--file", "missing.txt", "o/a"],
            )
        self.assertEqual(status, 2)
        self.assertEqual(fake.set_calls, [])

    def test_rejects_an_empty_secret_value(self):
        fake = FakeGh()
        fake.add_repo("o/a")
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "--force", "o/a"], stdin_data=b""
        )
        self.assertEqual(status, 2)
        self.assertEqual(fake.set_calls, [])

    def test_refuses_to_read_the_value_from_an_interactive_terminal(self):
        fake = FakeGh()
        fake.add_repo("o/a")
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "--force", "o/a"], isatty=True
        )
        self.assertEqual(status, 2)
        self.assertEqual(fake.set_calls, [])

    def test_rejects_a_secret_name_starting_with_a_digit(self):
        fake = FakeGh()
        status, out, err = run_repo_secrets(
            fake, ["--name", "1TOKEN", "--force", "o/a"], stdin_data=b"s3cr3t"
        )
        self.assertEqual(status, 2)

    def test_rejects_a_secret_name_with_disallowed_characters(self):
        fake = FakeGh()
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN-1", "--force", "o/a"], stdin_data=b"s3cr3t"
        )
        self.assertEqual(status, 2)

    def test_rejects_the_reserved_github_prefix(self):
        fake = FakeGh()
        status, out, err = run_repo_secrets(
            fake, ["--name", "GITHUB_TOKEN", "--force", "o/a"], stdin_data=b"s3cr3t"
        )
        self.assertEqual(status, 2)
        self.assertIn("reserved", err)

    def test_rejects_an_explicitly_empty_env(self):
        for argv in (["--env", ""], ["--env="]):
            with self.subTest(argv=argv):
                fake = FakeGh()
                status, out, err = run_repo_secrets(
                    fake,
                    ["--name", "TOKEN", "--force", *argv, "o/a"],
                    stdin_data=b"s3cr3t",
                )
                self.assertEqual(status, 2)

    def test_rejects_an_env_name_with_disallowed_characters(self):
        fake = FakeGh()
        status, out, err = run_repo_secrets(
            fake,
            ["--name", "TOKEN", "--force", "--env", "prod env", "o/a"],
            stdin_data=b"s3cr3t",
        )
        self.assertEqual(status, 2)

    def test_rejects_an_explicitly_empty_file(self):
        for argv in (["--file", ""], ["--file="]):
            with self.subTest(argv=argv):
                fake = FakeGh()
                status, out, err = run_repo_secrets(
                    fake,
                    ["--name", "TOKEN", "--force", *argv, "o/a"],
                    stdin_data=b"s3cr3t",
                )
                self.assertEqual(status, 2)

    def test_rejects_a_repo_that_is_not_owner_slash_repo(self):
        fake = FakeGh()
        status, out, err = run_repo_secrets(
            fake, ["--name", "TOKEN", "--force", "not-a-repo"], stdin_data=b"s3cr3t"
        )
        self.assertEqual(status, 2)

    def test_rejects_a_dot_segment_owner_repo_or_env(self):
        # `.` and `..` pass the character check but are reserved path
        # segments; spliced into `repos/{repo}/...` or
        # `.../environments/{env}/...` they would address a different
        # endpoint than the one that was validated.
        cases = [
            ["--name", "TOKEN", "--force", "../a"],
            ["--name", "TOKEN", "--force", "./a"],
            ["--name", "TOKEN", "--force", "o/.."],
            ["--name", "TOKEN", "--force", "o/."],
            ["--name", "TOKEN", "--force", "--env", ".", "o/a"],
            ["--name", "TOKEN", "--force", "--env", "..", "o/a"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                fake = FakeGh()
                status, out, err = run_repo_secrets(fake, argv, stdin_data=b"s3cr3t")
                self.assertEqual(status, 2)

    def test_gh_not_installed_is_reported(self):
        fake = FakeGh()
        with patch("shutil.which", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                main(["secrets", "--name", "TOKEN", "--force", "o/a"])
            self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
