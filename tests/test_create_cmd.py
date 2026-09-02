import base64
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from repo_lib import gh, scaffold
from repo_lib.cli import main

# Fake content for the four files the scaffold fetches live -- enough to
# round-trip through base64, not meant to look like the real templates.
# None of the three carries a "branches: [main]" push filter -- verified
# against the real templates in mikelward/codex-review, none of them has
# one at all (codex-review-check.yml's push: trigger is unfiltered,
# codex-review-listener.yml doesn't use push:, codex-review.yml uses
# schedule:/pull_request_target:/etc.) -- so these fixtures don't either.
FAKE_TEMPLATE_CONTENT = {
    "codex-review.yml": "name: codex-review\non:\n  pull_request_target:\n",
    "codex-review-check.yml": "name: codex-review-check\non:\n  push:\n",
    "codex-review-listener.yml": "name: codex-review-listener\n",
}
# zizmor.yml is the one file that does carry a branches:-[main] push
# filter (and the only one _branches_line is ever applied to).
FAKE_ZIZMOR_WORKFLOW = "name: zizmor\non:\n  push:\n    branches: [main]\n"

# codex-review's three templates + zizmor.yml + the two generated policy
# files + ci.yml + TODO.md -- see scaffold.build_scaffold_files.
EXPECTED_SCAFFOLD_FILE_COUNT = len(scaffold.TEMPLATE_FILES) + 5


class FakeGh:
    """Stands in for repo_lib.gh.run/try_run/run_with_input across both
    create_cmd.py's own calls (self-login, org probe, the create POST) and
    scaffold.py's (two template-source reads, then blob/tree/commit/ref
    writes for the initial commit). Everything succeeds by default; each
    `*_fails` flag turns one specific step into a failure so a test can
    isolate it."""

    def __init__(
        self,
        self_login="mikelward",
        org_exists=False,
        org_probe_stderr="gh: HTTP 404: Not Found (https://api.github.com/orgs/x)\n",
        self_login_fails=False,
        create_fails_stderr=None,
        default_branch="main",
        template_fetch_fails=None,  # the file name whose fetch should fail
        drift_zizmor_branches_line=False,  # re-spell zizmor.yml's own branches: line
        bootstrap_fails=False,
        blob_fails=False,
        tree_fails=False,
        commit_fails=False,
        ref_fails=False,
        # push_initial_commit's own pre-bootstrap recheck: by default the
        # branch is still genuinely empty (a 404), matching every scenario
        # this file otherwise models. Set ref_precheck_has_commits to
        # model someone else having pushed to it in the meantime; set
        # ref_precheck_fails for a non-404 read failure.
        ref_precheck_has_commits=False,
        ref_precheck_fails=False,
    ):
        self.self_login = self_login
        self.org_exists = org_exists
        self.org_probe_stderr = org_probe_stderr
        self.self_login_fails = self_login_fails
        self.create_fails_stderr = create_fails_stderr
        self.default_branch = default_branch
        self.template_fetch_fails = template_fetch_fails
        self.drift_zizmor_branches_line = drift_zizmor_branches_line
        self.bootstrap_fails = bootstrap_fails
        self.blob_fails = blob_fails
        self.tree_fails = tree_fails
        self.commit_fails = commit_fails
        self.ref_fails = ref_fails
        self.ref_precheck_has_commits = ref_precheck_has_commits
        self.ref_precheck_fails = ref_precheck_fails
        self.calls = []
        self.posts = []  # (args, decoded body) for the repo-create call only
        self.bootstrap_payload = None  # decoded body of the contents PUT
        self.blobs = []  # decoded body of every git/blobs call, in order
        self.tree_payload = None
        self.commit_payload = None
        self.ref_payload = None
        self.ref_endpoint = None
        self._blob_counter = 0

    def run(self, args):
        self.calls.append(list(args))
        if args[:2] == ["api", "user"]:
            if self.self_login_fails:
                raise gh.GhError("gh: simulated auth failure\n")
            return self.self_login + "\n"
        if args[0] == "api" and args[1].startswith(
            f"repos/{scaffold.TEMPLATE_REPO}/contents/templates/"
        ):
            name = args[1].split("templates/", 1)[1].split("?", 1)[0]
            if self.template_fetch_fails == name:
                raise gh.GhError(f"gh: HTTP 404: Not Found (fake, {name})\n")
            return base64.b64encode(FAKE_TEMPLATE_CONTENT[name].encode()).decode() + "\n"
        if args[0] == "api" and args[1].startswith(
            f"repos/{scaffold.ZIZMOR_SOURCE_REPO}/contents/.github/workflows/zizmor.yml"
        ):
            if self.template_fetch_fails == "zizmor.yml":
                raise gh.GhError("gh: HTTP 404: Not Found (fake, zizmor.yml)\n")
            content = FAKE_ZIZMOR_WORKFLOW
            if self.drift_zizmor_branches_line:
                content = content.replace("branches: [main]", 'branches: ["main"]')
            return base64.b64encode(content.encode()).decode() + "\n"
        raise AssertionError(f"unexpected gh.run call: {args}")

    def try_run(self, args):
        self.calls.append(list(args))
        if args[0] == "api" and args[1].startswith("orgs/"):
            if self.org_exists:
                return True, ""
            return False, self.org_probe_stderr
        if args[0] == "api" and "/git/refs/heads/" in args[1]:
            # push_initial_commit's own recheck, right before it bootstraps,
            # that the branch is still empty.
            if self.ref_precheck_fails:
                return False, "gh: HTTP 500 (fake ref-precheck failure)\n"
            if self.ref_precheck_has_commits:
                return True, json.dumps({"object": {"sha": "concurrent-commit-sha"}})
            return False, "gh: HTTP 404: Not Found\n"
        raise AssertionError(f"unexpected gh.try_run call: {args}")

    def run_with_input(self, args, input_bytes):
        self.calls.append(list(args))
        # ["api", "--method", "POST", endpoint, "--input", "-"]
        endpoint = args[3]
        if endpoint == "user/repos" or endpoint.startswith("orgs/"):
            if self.create_fails_stderr is not None:
                raise gh.GhError(self.create_fails_stderr)
            body = json.loads(input_bytes)
            self.posts.append((args, body))
            return json.dumps(
                {
                    "name": body["name"],
                    "full_name": f"{self.self_login}/{body['name']}",
                    "private": body["private"],
                    "default_branch": self.default_branch,
                }
            ).encode()
        if "/contents/" in endpoint:
            # The bootstrap write -- the one commit GitHub allows against a
            # genuinely empty repository; see push_initial_commit's own
            # docstring for why this has to come first.
            if self.bootstrap_fails:
                raise gh.GhError("gh: HTTP 500 (fake bootstrap failure)\n")
            self.bootstrap_payload = json.loads(input_bytes)
            return json.dumps({"commit": {"sha": "bootstrap-commit-sha"}}).encode()
        if endpoint.endswith("/git/blobs"):
            if self.blob_fails:
                raise gh.GhError("gh: HTTP 500 (fake blob failure)\n")
            self._blob_counter += 1
            self.blobs.append(json.loads(input_bytes))
            return json.dumps({"sha": f"blob-sha-{self._blob_counter}"}).encode()
        if endpoint.endswith("/git/trees"):
            if self.tree_fails:
                raise gh.GhError("gh: HTTP 500 (fake tree failure)\n")
            self.tree_payload = json.loads(input_bytes)
            return json.dumps({"sha": "tree-sha"}).encode()
        if endpoint.endswith("/git/commits"):
            if self.commit_fails:
                raise gh.GhError("gh: HTTP 500 (fake commit failure)\n")
            self.commit_payload = json.loads(input_bytes)
            return json.dumps({"sha": "commit-sha"}).encode()
        if endpoint.startswith("repos/") and "/git/refs/heads/" in endpoint:
            # PATCH (update), not POST (create) -- the bootstrap write
            # above already created the branch.
            if args[2] != "PATCH":
                raise AssertionError(f"expected PATCH for a ref update, got: {args}")
            if self.ref_fails:
                raise gh.GhError("gh: HTTP 500 (fake ref failure)\n")
            self.ref_payload = json.loads(input_bytes)
            self.ref_endpoint = endpoint
            return b"{}"
        raise AssertionError(f"unexpected gh.run_with_input call: {args}")


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


class ScaffoldFlagTest(unittest.TestCase):
    """--scaffold is on by default and pushes one initial commit; every
    other CreateCmdTest above already exercises the default-on path
    incidentally (FakeGh's scaffold steps all succeed by default) -- these
    cover the scaffold's own content and its failure modes specifically."""

    def test_scaffold_pushes_every_file_across_the_bootstrap_and_real_commit(self):
        fake = FakeGh(self_login="mikelward")
        status, out, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        # The bootstrap write is the alphabetically-first path -- the one
        # commit GitHub allows against a genuinely empty repository.
        self.assertIsNotNone(fake.bootstrap_payload)
        self.assertEqual(fake.bootstrap_payload["branch"], "main")
        # Every file, the bootstrapped one included, ends up in the real
        # commit's tree -- nothing is silently missing from it.
        self.assertEqual(len(fake.blobs), EXPECTED_SCAFFOLD_FILE_COUNT)
        self.assertEqual(len(fake.tree_payload["tree"]), EXPECTED_SCAFFOLD_FILE_COUNT)
        paths = {entry["path"] for entry in fake.tree_payload["tree"]}
        self.assertEqual(
            paths,
            {
                ".github/workflows/codex-review.yml",
                ".github/workflows/codex-review-check.yml",
                ".github/workflows/codex-review-listener.yml",
                ".github/workflows/zizmor.yml",
                ".github/zizmor.yml",
                ".github/lanes.conf",
                ".github/workflows/ci.yml",
                "TODO.md",
            },
        )
        # The real commit is parented on the bootstrap commit, and the
        # branch is fast-forwarded (PATCH, not a ref creation) to it.
        self.assertEqual(fake.commit_payload["parents"], ["bootstrap-commit-sha"])
        self.assertEqual(fake.commit_payload["tree"], "tree-sha")
        self.assertEqual(fake.ref_endpoint, "repos/mikelward/newthing/git/refs/heads/main")
        self.assertEqual(fake.ref_payload, {"sha": "commit-sha"})
        self.assertIn(f"pushed the CI scaffold ({EXPECTED_SCAFFOLD_FILE_COUNT} files)", out)
        self.assertIn("repo setup mikelward/newthing --force", out)

    def test_no_scaffold_flag_skips_the_scaffold_push(self):
        fake = FakeGh(self_login="mikelward")
        status, out, err = run_repo_create(fake, ["--private", "--no-scaffold", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        self.assertEqual(fake.blobs, [])
        self.assertIsNone(fake.tree_payload)
        self.assertIn("Push your initial commit (workflows included), then run:", out)
        self.assertIn("repo setup mikelward/newthing --force", out)

    def test_branches_line_rewritten_and_quoted_for_a_non_main_default_branch(self):
        fake = FakeGh(self_login="mikelward", default_branch="trunk")
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        self.assertEqual(fake.ref_endpoint, "repos/mikelward/newthing/git/refs/heads/trunk")
        contents = [blob["content"] for blob in fake.blobs]
        # Quoted, not a bare scalar -- a branch name can contain YAML
        # flow-syntax characters (a comma, a brace) a bare `[trunk]` would
        # misparse.
        self.assertTrue(any('branches: ["trunk"]' in c for c in contents))
        self.assertFalse(any("branches: [main]" in c for c in contents))
        self.assertFalse(any("branches: [trunk]" in c for c in contents))

    def test_branch_name_with_yaml_flow_characters_is_quoted_safely(self):
        # A branch name containing a comma or brace is unusual but valid
        # for git; the substitution has to survive it without producing a
        # YAML sequence with the wrong number of entries.
        fake = FakeGh(self_login="mikelward", default_branch="release,next")
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        contents = [blob["content"] for blob in fake.blobs]
        self.assertTrue(any('branches: ["release,next"]' in c for c in contents))

    def test_missing_default_branch_in_the_create_response_is_reported(self):
        fake = FakeGh(self_login="mikelward", default_branch="")
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("response named no default branch", err)
        self.assertEqual(fake.blobs, [])

    def test_template_fetch_failure_is_reported_and_nothing_is_pushed(self):
        fake = FakeGh(self_login="mikelward", template_fetch_fails="codex-review-check.yml")
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not fetch", err)
        self.assertIn("codex-review-check.yml", err)
        self.assertIn("fetching the scaffold's template files failed", err)
        # The repository itself was still created -- only the scaffold push
        # is what's missing.
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(fake.blobs, [])

    def test_zizmor_workflow_fetch_failure_is_reported(self):
        fake = FakeGh(self_login="mikelward", template_fetch_fails="zizmor.yml")
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not fetch", err)
        self.assertEqual(fake.blobs, [])

    def test_drifted_zizmor_branches_line_on_a_non_main_branch_fails_closed(self):
        # Codex review: if zizmor.yml ever re-spells its own
        # "branches: [main]" filter, a bare .replace() would silently
        # become a no-op -- landing a workflow still filtered to main
        # while the real default branch is something else. Must fail the
        # whole scaffold rather than push that.
        fake = FakeGh(
            self_login="mikelward",
            default_branch="trunk",
            drift_zizmor_branches_line=True,
        )
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("no longer spells its push filter literally", err)
        self.assertIn("fetching the scaffold's template files failed", err)
        self.assertEqual(fake.blobs, [])

    def test_drifted_zizmor_branches_line_on_the_main_branch_is_harmless(self):
        # No rewrite is attempted at all when the target really is "main"
        # -- the drift is irrelevant since there's nothing to re-point.
        fake = FakeGh(self_login="mikelward", default_branch="main", drift_zizmor_branches_line=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)

    def test_a_concurrent_initial_commit_refuses_the_bootstrap_rather_than_discarding_it(self):
        # Someone else pushed a real first commit to the branch between
        # this repo's creation and the scaffold push (or, for repo
        # setup's own reuse of this function, between plan_gaps seeing an
        # empty branch and the confirmation prompt finishing) -- the
        # bootstrap write must refuse rather than build a scaffold-only
        # tree on top of it and fast-forward over what's there (Codex
        # review, mikelward/repo#14).
        fake = FakeGh(self_login="mikelward", ref_precheck_has_commits=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("someone pushed to it since the plan was built", err)
        self.assertIn("pushing the scaffold commit failed", err)
        self.assertEqual(fake.bootstrap_payload, None)
        self.assertEqual(fake.blobs, [])

    def test_a_failed_precheck_read_is_reported_and_nothing_is_pushed(self):
        fake = FakeGh(self_login="mikelward", ref_precheck_fails=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not re-check", err)
        self.assertEqual(fake.bootstrap_payload, None)

    def test_bootstrap_failure_is_reported(self):
        fake = FakeGh(self_login="mikelward", bootstrap_fails=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not create the bootstrap commit", err)
        self.assertIn("pushing the scaffold commit failed", err)
        self.assertEqual(fake.blobs, [])

    def test_blob_failure_is_reported(self):
        fake = FakeGh(self_login="mikelward", blob_fails=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not create a blob", err)
        self.assertIn("pushing the scaffold commit failed", err)
        # The bootstrap commit already landed -- only the real commit and
        # the ref update are what's missing.
        self.assertIsNotNone(fake.bootstrap_payload)

    def test_tree_failure_is_reported(self):
        fake = FakeGh(self_login="mikelward", tree_fails=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not create the scaffold's tree", err)

    def test_commit_failure_is_reported(self):
        fake = FakeGh(self_login="mikelward", commit_fails=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not create the scaffold's commit", err)

    def test_ref_failure_is_reported(self):
        fake = FakeGh(self_login="mikelward", ref_fails=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not fast-forward main to the scaffold's commit", err)
