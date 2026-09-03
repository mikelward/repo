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
        template_resolve_fails=False,  # the codex-review main->sha resolve fails
        drift_zizmor_branches_line=False,  # re-spell zizmor.yml's own branches: line
        bootstrap_fails=False,
        blob_fails=False,
        tree_fails=False,
        # The gh token's own OAuth scopes, as scaffold._missing_workflow_
        # scope reads them (via gh.token_scopes -> X-OAuth-Scopes). None
        # (the default) models a token this can't tell the scopes of at
        # all (a fine-grained PAT/GitHub App token) -- never blocks. Pass
        # a tuple missing "workflow" to model mikelward/repo#18's real
        # cause.
        token_scopes=None,
        commit_fails=False,
        ref_fails=False,
        # push_initial_commit's own pre-bootstrap recheck: by default the
        # branch is still genuinely empty (a 404), matching every scenario
        # this file otherwise models. Set ref_precheck_has_commits to
        # model someone else having pushed to it in the meantime; set
        # ref_precheck_empty_409 to model the shape GitHub's real API
        # actually returns here for a genuinely brand-new repository --
        # HTTP 409 "Git Repository is empty" rather than 404, since a repo
        # `repo create` just made has zero git objects at all, not merely
        # a missing ref (Codex review, mikelward/repo#14); set
        # ref_precheck_fails for an unrelated read failure.
        ref_precheck_has_commits=False,
        ref_precheck_empty_409=False,
        # A 409 that isn't specifically "Git Repository is empty" -- some
        # other conflict against a branch that already has commits -- must
        # not be read as "safe to bootstrap" (Codex review, mikelward/repo#14).
        ref_precheck_ambiguous_409=False,
        ref_precheck_fails=False,
        bootstrap_commit_parents=None,  # models the bootstrap PUT landing on someone else's commit
    ):
        self.self_login = self_login
        self.org_exists = org_exists
        self.org_probe_stderr = org_probe_stderr
        self.self_login_fails = self_login_fails
        self.create_fails_stderr = create_fails_stderr
        self.default_branch = default_branch
        self.template_fetch_fails = template_fetch_fails
        self.template_resolve_fails = template_resolve_fails
        # The fixed commit sha every template fetch must be pinned to,
        # once _resolve_commit_sha resolves TEMPLATE_REPO's main -- the
        # fetch handler below asserts every one of the three uses exactly
        # this, not "main" independently, since that's the property the
        # fix under test (Codex review, mikelward/repo#14) exists to give.
        self.template_commit_sha = "faketemplateshaabc123"
        self.drift_zizmor_branches_line = drift_zizmor_branches_line
        self.bootstrap_fails = bootstrap_fails
        self.blob_fails = blob_fails
        self.tree_fails = tree_fails
        self.token_scopes = token_scopes
        self.commit_fails = commit_fails
        self.ref_fails = ref_fails
        self.ref_precheck_has_commits = ref_precheck_has_commits
        self.ref_precheck_empty_409 = ref_precheck_empty_409
        self.ref_precheck_ambiguous_409 = ref_precheck_ambiguous_409
        self.ref_precheck_fails = ref_precheck_fails
        self.bootstrap_commit_parents = bootstrap_commit_parents
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
        if args == ["api", "-i", "user"]:
            # gh.token_scopes()'s own read -- raw headers, a blank line,
            # then a body, same shape `gh api -i` really prints.
            header = (
                f"X-OAuth-Scopes: {', '.join(self.token_scopes)}\n" if self.token_scopes is not None else ""
            )
            return f"HTTP/2.0 200 OK\n{header}\n{{}}"
        if args[0] == "api" and args[1] == f"repos/{scaffold.TEMPLATE_REPO}/commits/main":
            if self.template_resolve_fails:
                raise gh.GhError("gh: HTTP 500 (fake template-resolve failure)\n")
            return self.template_commit_sha + "\n"
        if args[0] == "api" and args[1].startswith(
            f"repos/{scaffold.TEMPLATE_REPO}/contents/templates/"
        ):
            name = args[1].split("templates/", 1)[1].split("?", 1)[0]
            ref = args[1].split("?ref=", 1)[1] if "?ref=" in args[1] else None
            if ref != self.template_commit_sha:
                raise AssertionError(
                    f"template fetch for {name} used ref={ref!r}, expected the resolved "
                    f"{self.template_commit_sha!r} (Codex review, mikelward/repo#14)"
                )
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
        if args[0] == "api" and "/git/ref/heads/" in args[1]:
            # push_initial_commit's own recheck, right before it bootstraps,
            # that the branch is still empty. Singular git/ref/... -- the
            # documented read route; plural git/refs/... (used below, for
            # the PATCH) has no GET route at all (Codex review,
            # mikelward/repo#14).
            if self.ref_precheck_fails:
                return False, "gh: HTTP 500 (fake ref-precheck failure)\n"
            if self.ref_precheck_has_commits:
                return True, json.dumps({"object": {"sha": "concurrent-commit-sha"}})
            if self.ref_precheck_empty_409:
                return False, "gh: HTTP 409: Git Repository is empty.\n"
            if self.ref_precheck_ambiguous_409:
                return False, "gh: HTTP 409: Conflict\n"
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
            commit = {"sha": "bootstrap-commit-sha"}
            if self.bootstrap_commit_parents:
                commit["parents"] = self.bootstrap_commit_parents
            return json.dumps({"commit": commit}).encode()
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

    def test_scaffold_bootstraps_when_the_precheck_gets_the_real_409_shape(self):
        # This is the intended input for `repo create --scaffold`: a
        # repository this same run just created, which has zero git
        # objects at all. GitHub's real API returns HTTP 409 ("Git
        # Repository is empty") for that, not 404 -- the fixture's own
        # default (404, matching every other test in this class) never
        # actually exercised the code path `repo create --scaffold`'s
        # real input takes. Treating only 404 as "still empty, proceed"
        # would refuse to bootstrap here, so `repo create --scaffold`
        # could never succeed on a genuinely fresh repository (Codex
        # review, mikelward/repo#14).
        fake = FakeGh(self_login="mikelward", ref_precheck_empty_409=True)
        status, out, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        self.assertIsNotNone(fake.bootstrap_payload)
        self.assertIn(f"pushed the CI scaffold ({EXPECTED_SCAFFOLD_FILE_COUNT} files)", out)

    def test_ambiguous_409_precheck_refuses_rather_than_bootstrapping(self):
        # A 409 that isn't specifically "Git Repository is empty" -- some
        # other conflict -- must not be read as "safe to bootstrap": that
        # would let this write straight onto a branch that might actually
        # have commits (Codex review, mikelward/repo#14).
        fake = FakeGh(self_login="mikelward", ref_precheck_ambiguous_409=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not re-check", err)
        self.assertIsNone(fake.bootstrap_payload)

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

    def test_default_branch_with_url_metacharacters_is_percent_encoded_in_ref_calls(self):
        # A branch name can carry a `#`, which sent raw into a URL path
        # would be read as the start of a fragment (or otherwise misparse)
        # rather than as part of the ref name (Codex review,
        # mikelward/repo#14) -- credentials._ref_suffix hit the same class
        # of bug for a query-string ref (Codex, mikelward/repo#13).
        fake = FakeGh(self_login="mikelward", default_branch="release#1")
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        # Singular git/ref/... for the precheck GET, plural git/refs/...
        # for the PATCH that fast-forwards the branch -- see
        # scaffold._branch_ref_path's own docstring (Codex review,
        # mikelward/repo#14).
        precheck_expected = "repos/mikelward/newthing/git/ref/heads/release%231"
        self.assertTrue(
            any(len(c) > 1 and c[1] == precheck_expected for c in fake.calls if c[0] == "api"),
            f"no precheck call to {precheck_expected} in {fake.calls}",
        )
        self.assertEqual(fake.ref_endpoint, "repos/mikelward/newthing/git/refs/heads/release%231")

    def test_default_branch_with_a_slash_keeps_it_literal_in_ref_calls(self):
        # Unlike the query-string case above, an embedded "/" here is a
        # real ref path separator (refs/heads/release/1.0) and must stay
        # literal, not become %2F.
        fake = FakeGh(self_login="mikelward", default_branch="release/1.0")
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        self.assertEqual(fake.ref_endpoint, "repos/mikelward/newthing/git/refs/heads/release/1.0")

    def test_a_commit_created_with_a_parent_refuses_rather_than_discarding_it(self):
        # The precheck and the bootstrap PUT are two separate requests --
        # someone can still push a real first commit in between. The PUT
        # itself would succeed right on top of theirs (the Contents API
        # doesn't require an empty branch), so the only place that race is
        # visible is the returned commit's own parents (Codex review,
        # mikelward/repo#14).
        fake = FakeGh(self_login="mikelward")
        fake.bootstrap_commit_parents = ["someone-elses-commit-sha"]
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("already had a commit by the time the bootstrap write landed", err)
        self.assertEqual(fake.blobs, [])
        self.assertEqual(fake.tree_payload, None)

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

    def test_template_commit_resolve_failure_is_reported_and_nothing_is_fetched(self):
        # Resolving codex-review's main to a commit sha is the first call
        # build_scaffold_files makes for the three templates -- if it
        # fails, none of the three fetches should even be attempted
        # (Codex review, mikelward/repo#14).
        fake = FakeGh(self_login="mikelward", template_resolve_fails=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not resolve", err)
        self.assertIn("fetching the scaffold's template files failed", err)
        self.assertEqual(fake.blobs, [])

    def test_all_three_templates_are_fetched_at_the_same_resolved_commit(self):
        # FakeGh.run itself asserts every template fetch's ref equals the
        # one resolved sha (raising AssertionError otherwise), so a
        # regression back to each fetch resolving "main" independently
        # would fail this test even without the explicit check below --
        # this just makes the property being tested for legible on its own
        # (Codex review, mikelward/repo#14).
        fake = FakeGh(self_login="mikelward")
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        template_fetch_calls = [
            c
            for c in fake.calls
            if c[0] == "api" and c[1].startswith(f"repos/{scaffold.TEMPLATE_REPO}/contents/templates/")
        ]
        self.assertEqual(len(template_fetch_calls), len(scaffold.TEMPLATE_FILES))
        for call in template_fetch_calls:
            self.assertTrue(call[1].endswith(f"?ref={fake.template_commit_sha}"), call)

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
        self.assertIn("isn't a single, unambiguous 'branches: [main]' line", err)
        self.assertIn("fetching the scaffold's template files failed", err)
        self.assertEqual(fake.blobs, [])

    def test_drifted_zizmor_branches_line_on_the_main_branch_is_harmless(self):
        # No rewrite is attempted at all when the target really is "main"
        # -- the drift is irrelevant since there's nothing to re-point.
        fake = FakeGh(self_login="mikelward", default_branch="main", drift_zizmor_branches_line=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)

    def test_branches_line_refuses_two_top_level_on_push_parented_occurrences(self):
        # Two genuine `branches: [main]` lines, each under a TOP-LEVEL
        # on: push: -- leaves no way to tell which one is the real push
        # trigger this scaffold cares about. Rewriting whichever comes
        # first would leave the OTHER one still pointed at "main" while
        # reporting success (Codex review, mikelward/repo#14).
        text = (
            "name: zizmor\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
        )
        result = scaffold._branches_line(text, "trunk")
        self.assertIsNone(result)

    def test_branches_line_ignores_an_on_push_branches_shape_not_at_top_level(self):
        # Fresh evidence beyond the on:-grandparent fix: an on: push:
        # branches: [main] shape that ISN'T itself at the document's top
        # level (here nested under an unrelated "extra:" key) must not be
        # mistaken for the real, top-level trigger -- especially
        # dangerous when the real trigger is written in block style, since
        # then this nested lookalike would be the only bracket-style match
        # that otherwise clears the on:/push: ancestry check (Codex
        # review, mikelward/repo#14).
        text = (
            "name: zizmor\n"
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "extra:\n"
            "  on:\n"
            "    push:\n"
            "      branches: [main]\n"
        )
        result = scaffold._branches_line(text, "trunk")
        self.assertIsNone(result)

    def test_branches_line_ignores_a_push_key_not_nested_under_on(self):
        # Fresh evidence beyond the push:-parent-only fix: an unrelated
        # `push:` mapping elsewhere in the file (not itself under `on:`)
        # must not be mistaken for the real trigger either -- especially
        # dangerous when the real trigger is written in block style, since
        # then this lookalike would be the only bracket-style match at
        # all under a push: parent. Requiring the grandparent to be `on:`
        # too rejects it (Codex review, mikelward/repo#14).
        text = (
            "name: zizmor\n"
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "hooks:\n"
            "  push:\n"
            "    branches: [main]\n"
        )
        result = scaffold._branches_line(text, "trunk")
        self.assertIsNone(result)

    def test_branches_line_ignores_a_lookalike_key_with_a_different_parent(self):
        # A job's own unrelated key that happens to be named "branches"
        # too (a matrix entry) sits under matrix:, not push: -- one
        # whole-line match under push: and one under matrix: is NOT
        # ambiguous, since only the push:-parented one is the real
        # trigger. This is a precision improvement over the earlier
        # whole-line-only fix, which would have counted both and refused
        # a scaffold that's actually safe to rewrite.
        text = (
            "name: zizmor\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "jobs:\n"
            "  x:\n"
            "    strategy:\n"
            "      matrix:\n"
            "        branches: [main]\n"
        )
        result = scaffold._branches_line(text, "trunk")
        self.assertIsNotNone(result)
        self.assertIn('branches: ["trunk"]', result)
        # Only the real trigger line was rewritten -- the matrix entry's
        # own "branches: [main]" is untouched.
        self.assertIn("        branches: [main]\n", result)

    def test_branches_line_refuses_when_the_only_bracket_style_match_is_a_lookalike(self):
        # Fresh evidence beyond the earlier whole-line fix: the real on:
        # push: filter here is written in BLOCK style ("- main"), which
        # _BRANCHES_MAIN_LINE_RE never matches at all -- only a job's
        # unrelated "branches: [main]" matrix entry does, whose parent is
        # matrix:, not push:. A whole-line-only check would have found
        # exactly that one match, treated it as unambiguous, and rewritten
        # it -- leaving the REAL push filter still reading "main" while
        # reporting success. Requiring a push: parent means there is no
        # valid match at all here, so this must refuse rather than
        # silently rewrite the lookalike (Codex review, mikelward/repo#14).
        text = (
            "name: zizmor\n"
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "jobs:\n"
            "  x:\n"
            "    strategy:\n"
            "      matrix:\n"
            "        branches: [main]\n"
        )
        result = scaffold._branches_line(text, "trunk")
        self.assertIsNone(result)

    def test_branches_line_ignores_the_same_text_embedded_in_an_unrelated_line(self):
        # A bare substring search can't tell the real `on: push: branches:`
        # trigger apart from that exact text turning up somewhere else in
        # the file that ISN'T its own whole line -- here, inside a shell
        # one-liner in an unrelated step. Anchoring to a whole-line match
        # is what tells them apart: only the real trigger line matches, so
        # this still rewrites cleanly, leaving the one-liner's text alone.
        text = (
            "name: zizmor\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "jobs:\n"
            "  x:\n"
            "    steps:\n"
            "      - run: echo 'reminder: this filter says branches: [main]'\n"
        )
        result = scaffold._branches_line(text, "trunk")
        self.assertIsNotNone(result)
        self.assertIn('branches: ["trunk"]', result)
        self.assertIn("reminder: this filter says branches: [main]", result)

    def test_branches_line_ignores_a_commented_out_occurrence(self):
        # A commented-out line doesn't count as the real trigger, so with
        # exactly one REAL occurrence this still rewrites cleanly.
        text = (
            "name: zizmor\n"
            "# old filter used to read: branches: [main]\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
        )
        result = scaffold._branches_line(text, "trunk")
        self.assertIsNotNone(result)
        self.assertIn('branches: ["trunk"]', result)
        self.assertIn("# old filter used to read: branches: [main]", result)

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
        # the ref update are what's missing. Telling the user to "push
        # your initial commit by hand" here would be actively wrong: the
        # branch isn't empty any more, so a normal, independently rooted
        # push would be rejected as non-fast-forward against the bootstrap
        # commit that's already there (Codex review, mikelward/repo#14).
        self.assertIsNotNone(fake.bootstrap_payload)
        self.assertNotIn("push your initial commit by hand", err.lower())
        self.assertIn("may already carry a partial bootstrap commit", err)
        self.assertIn("repo setup mikelward/newthing --force", err)

    def test_tree_failure_is_reported(self):
        fake = FakeGh(self_login="mikelward", tree_fails=True)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("could not create the scaffold's tree", err)

    def test_missing_workflow_scope_is_caught_before_any_write(self):
        # mikelward/repo#18: the real-world cause of a persistent
        # git/trees 404 turned out to be a gh token missing the
        # `workflow` OAuth scope, not a timing window -- checked up front
        # now, before push_initial_commit is even called, rather than
        # discovered as an opaque 404 partway through it.
        fake = FakeGh(self_login="mikelward", token_scopes=("gist", "read:org", "repo"))
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 1)
        self.assertIn("workflow", err)
        self.assertIn("gh auth refresh", err)
        # Nothing attempted at all -- not even the bootstrap Contents-API
        # PUT, which would itself have succeeded (it targets
        # .github/lanes.conf, not a workflow path).
        self.assertIsNone(fake.bootstrap_payload)
        self.assertIsNone(fake.tree_payload)

    def test_unknown_token_scopes_do_not_block_the_scaffold(self):
        # A fine-grained PAT or GitHub App token carries no OAuth scopes
        # at all -- "can't tell" must not read as "missing", or every
        # such token would be refused a scaffold it could actually write.
        fake = FakeGh(self_login="mikelward", token_scopes=None)
        status, _, err = run_repo_create(fake, ["--private", "mikelward/newthing"])
        self.assertEqual(status, 0, err)
        self.assertIsNotNone(fake.tree_payload)

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
