import io
import json
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from repo_lib import gh
from repo_lib.cli import main


class FakeGh:
    """Stands in for repo_lib.gh.run/try_run. Records every call (for
    assertions on the exact gh invocation) and answers from canned fixture
    data, closely mirroring repo-list_test's own make_gh fixture in
    mikelward/scripts -- but as plain Python, no fake binary on PATH.
    """

    def __init__(
        self,
        self_login="mikelward",
        repos=(),
        org_exists=False,
        org_probe_stderr="gh: HTTP 404: Not Found (https://api.github.com/orgs/x)\n",
        self_login_fails=False,
        list_fails_stderr=None,
    ):
        self.self_login = self_login
        self.repos = repos  # iterable of (full_name, fork, archived)
        self.org_exists = org_exists
        self.org_probe_stderr = org_probe_stderr
        self.self_login_fails = self_login_fails
        self.list_fails_stderr = list_fails_stderr
        self.calls = []

    def run(self, args):
        self.calls.append(list(args))
        if args[:2] == ["api", "user"]:
            if self.self_login_fails:
                raise gh.GhError("gh: simulated auth failure\n")
            return self.self_login + "\n"
        if args[:2] == ["api", "--paginate"]:
            if self.list_fails_stderr is not None:
                raise gh.GhError(self.list_fails_stderr)
            lines = [
                json.dumps({"full_name": name, "fork": fork, "archived": archived})
                for name, fork, archived in self.repos
            ]
            return "\n".join(lines) + ("\n" if lines else "")
        raise AssertionError(f"unexpected gh.run call: {args}")

    def try_run(self, args):
        self.calls.append(list(args))
        if not (args[0] == "api" and args[1].startswith("orgs/")):
            raise AssertionError(f"unexpected gh.try_run call: {args}")
        if self.org_exists:
            return True, ""
        return False, self.org_probe_stderr


def run_repo_list(fake, argv):
    """Runs `repo list <argv>` against `fake`, returns (status, stdout,
    stderr)."""
    out = io.StringIO()
    err = io.StringIO()
    status = 0
    with patch("repo_lib.gh.run", side_effect=fake.run), patch(
        "repo_lib.gh.try_run", side_effect=fake.try_run
    ), patch("shutil.which", return_value="/usr/bin/gh"), redirect_stdout(
        out
    ), redirect_stderr(err):
        try:
            main(["list", *argv])
        except SystemExit as e:
            status = e.code if isinstance(e.code, int) else 1
    return status, out.getvalue(), err.getvalue()


class ListCmdTest(unittest.TestCase):
    def test_lists_self_repos_sorted_excluding_forks_and_archived(self):
        fake = FakeGh(
            repos=[
                ("b/repo", False, False),
                ("a/repo", False, False),
                ("forked/repo", True, False),
                ("old/repo", False, True),
            ]
        )
        status, out, _ = run_repo_list(fake, [])
        self.assertEqual(status, 0)
        self.assertEqual(out, "a/repo\nb/repo\n")

    def test_include_forks_includes_a_fork(self):
        fake = FakeGh(
            repos=[("kept/repo", False, False), ("forked/repo", True, False)]
        )
        status, out, _ = run_repo_list(fake, ["--include-forks"])
        self.assertEqual(status, 0)
        self.assertEqual(out, "forked/repo\nkept/repo\n")

    def test_include_archived_includes_an_archived_repo(self):
        fake = FakeGh(repos=[("kept/repo", False, False), ("old/repo", False, True)])
        status, out, _ = run_repo_list(fake, ["--include-archived"])
        self.assertEqual(status, 0)
        self.assertEqual(out, "kept/repo\nold/repo\n")

    def test_owner_matching_self_still_uses_user_repos(self):
        fake = FakeGh(repos=[("a/repo", False, False)])
        status, _, _ = run_repo_list(fake, ["--owner", "mikelward"])
        self.assertEqual(status, 0)
        endpoints = [c[2] for c in fake.calls if c[:2] == ["api", "--paginate"]]
        self.assertEqual(endpoints, ["user/repos"])

    def test_owner_matching_self_in_a_different_case_still_uses_user_repos(self):
        fake = FakeGh(repos=[("a/repo", False, False)])
        status, out, _ = run_repo_list(fake, ["--owner", "MikelWard"])
        self.assertEqual(status, 0)
        self.assertEqual(out, "a/repo\n")
        endpoints = [c[2] for c in fake.calls if c[:2] == ["api", "--paginate"]]
        self.assertEqual(endpoints, ["user/repos"])

    def test_self_listing_restricts_affiliation_to_owner(self):
        fake = FakeGh(repos=[("a/repo", False, False)])
        run_repo_list(fake, [])
        paginate_call = next(c for c in fake.calls if c[:2] == ["api", "--paginate"])
        self.assertIn("affiliation=owner", paginate_call)

    def test_org_listing_does_not_restrict_by_affiliation(self):
        fake = FakeGh(org_exists=True, repos=[("org/repo", False, False)])
        run_repo_list(fake, ["--owner", "some-org"])
        paginate_call = next(c for c in fake.calls if c[:2] == ["api", "--paginate"])
        self.assertNotIn("affiliation=owner", paginate_call)
        self.assertEqual(paginate_call[2], "orgs/some-org/repos")

    def test_owner_for_a_visible_org_uses_orgs_owner_repos(self):
        fake = FakeGh(org_exists=True, repos=[("org/private-repo", False, False)])
        status, out, _ = run_repo_list(fake, ["--owner", "some-org"])
        self.assertEqual(status, 0)
        self.assertEqual(out, "org/private-repo\n")

    def test_owner_for_another_individual_falls_back_to_public_only_with_a_note(self):
        fake = FakeGh(org_exists=False, repos=[("other/public-repo", False, False)])
        status, out, _ = run_repo_list(fake, ["--owner", "someone-else"])
        self.assertEqual(status, 0)
        self.assertEqual(out, "other/public-repo\n")
        paginate_call = next(c for c in fake.calls if c[:2] == ["api", "--paginate"])
        self.assertEqual(paginate_call[2], "users/someone-else/repos")

    def test_org_probe_failure_that_is_not_a_404_is_a_hard_error(self):
        # 403/SSO/transient must NOT be read as "not an org" -- falling
        # back to the public-only endpoint would silently drop every
        # private repo in a real, existing org this call merely couldn't
        # reach right now.
        fake = FakeGh(
            org_exists=False,
            org_probe_stderr="gh: HTTP 403: Resource protected by organization SAML enforcement\n",
        )
        status, out, _ = run_repo_list(fake, ["--owner", "some-org"])
        self.assertEqual(status, 1)
        self.assertEqual(out, "")
        # Never reached the listing call at all.
        self.assertFalse(any(c[:2] == ["api", "--paginate"] for c in fake.calls))

    def test_a_failed_page_read_prints_no_partial_list(self):
        fake = FakeGh(list_fails_stderr="gh: HTTP 500: Internal Server Error\n")
        status, out, _ = run_repo_list(fake, [])
        self.assertEqual(status, 1)
        self.assertEqual(out, "")

    def test_an_unauthenticated_gh_is_reported_not_treated_as_an_empty_fleet(self):
        fake = FakeGh(self_login_fails=True)
        status, out, err = run_repo_list(fake, [])
        self.assertEqual(status, 1)
        self.assertEqual(out, "")
        # The underlying gh error must reach the user, not just a generic
        # "could not determine the authenticated user" with the reason
        # discarded.
        self.assertIn("simulated auth failure", err)

    def test_rejects_an_owner_name_with_disallowed_characters(self):
        fake = FakeGh()
        status, out, _ = run_repo_list(fake, ["--owner", "not a name"])
        self.assertEqual(status, 2)
        self.assertEqual(out, "")

    def test_rejects_an_explicitly_empty_owner(self):
        # Covers both --owner '' and --owner= -- argparse hands them to us
        # identically as args.owner == "", unlike the shell version, which
        # had to parse the two forms separately.
        for argv in (["--owner", ""], ["--owner="]):
            with self.subTest(argv=argv):
                fake = FakeGh()
                status, out, _ = run_repo_list(fake, argv)
                self.assertEqual(status, 2)
                self.assertEqual(out, "")
                self.assertEqual(fake.calls, [])

    def test_rejects_an_unexpected_positional_argument(self):
        fake = FakeGh()
        status, out, _ = run_repo_list(fake, ["extra-arg"])
        self.assertEqual(status, 2)
        self.assertEqual(out, "")

    def test_an_empty_fleet_is_success_not_an_error(self):
        fake = FakeGh(repos=[])
        status, out, _ = run_repo_list(fake, [])
        self.assertEqual(status, 0)
        self.assertEqual(out, "")

    def test_gh_not_installed_is_reported(self):
        with patch("shutil.which", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                main(["list"])
            self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
