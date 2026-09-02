import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from repo_lib import gh
from repo_lib.cli import main


class FakeGh:
    """Stands in for repo_lib.gh.run/try_run/run_with_input, mirroring
    test_list_cmd.py's own fixture for the same self-login/org-probe
    pattern create_cmd.py reuses."""

    def __init__(
        self,
        self_login="mikelward",
        org_exists=False,
        org_probe_stderr="gh: HTTP 404: Not Found (https://api.github.com/orgs/x)\n",
        self_login_fails=False,
        create_fails_stderr=None,
    ):
        self.self_login = self_login
        self.org_exists = org_exists
        self.org_probe_stderr = org_probe_stderr
        self.self_login_fails = self_login_fails
        self.create_fails_stderr = create_fails_stderr
        self.calls = []
        self.posts = []  # (args, decoded json body)

    def run(self, args):
        self.calls.append(list(args))
        if args[:2] == ["api", "user"]:
            if self.self_login_fails:
                raise gh.GhError("gh: simulated auth failure\n")
            return self.self_login + "\n"
        raise AssertionError(f"unexpected gh.run call: {args}")

    def try_run(self, args):
        self.calls.append(list(args))
        if not (args[0] == "api" and args[1].startswith("orgs/")):
            raise AssertionError(f"unexpected gh.try_run call: {args}")
        if self.org_exists:
            return True, ""
        return False, self.org_probe_stderr

    def run_with_input(self, args, input_bytes):
        self.calls.append(list(args))
        if self.create_fails_stderr is not None:
            raise gh.GhError(self.create_fails_stderr)
        self.posts.append((args, json.loads(input_bytes)))
        return b""


def run_repo_create(fake, argv):
    out = io.StringIO()
    err = io.StringIO()
    status = 0
    with patch("repo_lib.gh.run", side_effect=fake.run), patch(
        "repo_lib.gh.try_run", side_effect=fake.try_run
    ), patch("repo_lib.gh.run_with_input", side_effect=fake.run_with_input), patch(
        "shutil.which", return_value="/usr/bin/gh"
    ), redirect_stdout(
        out
    ), redirect_stderr(err):
        try:
            main(["create", *argv])
        except SystemExit as e:
            status = e.code if isinstance(e.code, int) else 1
    return status, out.getvalue(), err.getvalue()


class CreateCmdTest(unittest.TestCase):
    def test_creates_under_self_when_owner_matches_authenticated_user(self):
        fake = FakeGh(self_login="mikelward")
        status, out, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        self.assertEqual(len(fake.posts), 1)
        args, body = fake.posts[0]
        self.assertEqual(args[:3], ["api", "--method", "POST"])
        self.assertIn("user/repos", args)
        self.assertEqual(body, {"name": "newthing", "private": True})
        self.assertIn("mikelward/newthing: created (private, empty)", out)
        self.assertIn("repo setup mikelward/newthing --force", out)

    def test_owner_match_is_case_insensitive(self):
        # Same reasoning as list_cmd.py's own case-insensitive comparison:
        # GitHub account names are case-insensitive, so a differently-cased
        # --owner (here, the positional OWNER/REPO) must still resolve to
        # the user/repos endpoint rather than probing for an org that isn't
        # one.
        fake = FakeGh(self_login="MikelWard")
        status, out, err = run_repo_create(fake, ["--public", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        self.assertIn("user/repos", fake.posts[0][0])

    def test_creates_under_a_visible_organization(self):
        fake = FakeGh(self_login="mikelward", org_exists=True)
        status, out, err = run_repo_create(fake, ["--public", "someorg/newthing"])
        self.assertEqual(status, 0, err)
        args, body = fake.posts[0]
        self.assertIn("orgs/someorg/repos", args)
        self.assertEqual(body, {"name": "newthing", "private": False})
        self.assertIn("someorg/newthing: created (public, empty)", out)

    def test_owner_neither_self_nor_a_visible_org_is_refused(self):
        fake = FakeGh(self_login="mikelward", org_exists=False)
        status, _, err = run_repo_create(fake, ["--public", "someoneelse/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("is neither the authenticated user", err)
        self.assertEqual(fake.posts, [])

    def test_org_probe_failure_other_than_404_is_not_treated_as_absent(self):
        # A 403/SSO/transient failure must not be read as "not an org" --
        # that would misroute the create to an endpoint that doesn't exist
        # for another user's personal account instead of reporting the
        # real problem.
        fake = FakeGh(
            self_login="mikelward",
            org_exists=False,
            org_probe_stderr="gh: HTTP 403: Forbidden\n",
        )
        status, _, err = run_repo_create(fake, ["--public", "someorg/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not determine whether 'someorg' is an organization", err)
        self.assertEqual(fake.posts, [])

    def test_malformed_owner_repo_is_a_usage_error(self):
        fake = FakeGh()
        status, _, err = run_repo_create(fake, ["--public", "not-owner-slash-repo"])
        self.assertEqual(status, 2)
        self.assertEqual(fake.calls, [])

    def test_visibility_flag_is_required(self):
        fake = FakeGh()
        status, _, _ = run_repo_create(fake, ["mikelward/newthing"])
        self.assertEqual(status, 2)
        self.assertEqual(fake.calls, [])

    def test_private_and_public_are_mutually_exclusive(self):
        fake = FakeGh()
        status, _, _ = run_repo_create(fake, ["--private", "--public", "mikelward/newthing"])
        self.assertEqual(status, 2)
        self.assertEqual(fake.calls, [])

    def test_create_failure_is_reported(self):
        fake = FakeGh(create_fails_stderr="gh: HTTP 422: name already exists on this account\n")
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not create mikelward/newthing", err)
        self.assertIn("name already exists", err)

    def test_self_login_failure_is_reported(self):
        fake = FakeGh(self_login_fails=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not determine the authenticated user", err)
        self.assertEqual(fake.posts, [])
