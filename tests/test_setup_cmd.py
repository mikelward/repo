import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from repo_lib import gh
from repo_lib.cli import main

REPO = "owner/repo"

_DEFAULT_BRANCH_RE = re.compile(r"^repos/([^/]+/[^/]+)$")
_BRANCH_COUNT_RE = re.compile(r"^repos/([^/]+/[^/]+)/branches\?per_page=1$")
_COMMITS_HEAD_RE = re.compile(r"^repos/([^/]+/[^/]+)/commits\?per_page=1$")
_CHECK_RUNS_RE = re.compile(r"^repos/([^/]+/[^/]+)/commits/([^/]+)/check-runs$")
_STATUS_RE = re.compile(r"^repos/([^/]+/[^/]+)/commits/([^/]+)/status$")
_PULLS_RE = re.compile(r"^repos/([^/]+/[^/]+)/pulls\?state=(open|closed)&.*$")
_RULESETS_LOOKUP_RE = re.compile(r"^repos/([^/]+/[^/]+)/rulesets\?includes_parents=false$")
_RULESETS_ALL_RE = re.compile(r"^repos/([^/]+/[^/]+)/rulesets\?includes_parents=true$")
_RULESET_ONE_RE = re.compile(r"^repos/([^/]+/[^/]+)/rulesets/([^/]+)$")
_MASTER_BRANCH_RE = re.compile(r"^repos/([^/]+/[^/]+)/branches/master$")

_OWNERSHIP_JQ = ".enforcement, (.rules[].type)"


class FakeGh:
    """Models one repository's GitHub state for repo_lib.gh.run/try_run/
    run_with_input, closely enough to exercise repo_lib.rules without a
    fake `gh` binary on PATH. Only the two --jq forms rules.py actually
    sends to a single ruleset id are distinguished (the ownership check vs.
    the scope fetch) -- there is no third, so matching on the ownership
    JQ string and falling back to "scope fetch" for any other is safe."""

    def __init__(self):
        self.calls = []
        self.default_branch = "main"
        self.allow_rebase = "true"
        self.branch_count = "1"
        self.default_head_sha = "abc123"
        self.default_head_fails = False
        self.check_runs = {}
        self.statuses = {}
        self.open_prs = []
        self.closed_prs = []
        self.existing_ruleset_id = None
        self._name_lookup_calls = 0
        # Sentinel: unset means "always answer with existing_ruleset_id".
        # Set to model the name resolving to something else (None or a
        # different id) starting on the SECOND lookup by name -- the
        # rename-during-confirmation race.
        self.existing_ruleset_id_after_second_lookup = "__unset__"
        self.all_ruleset_ids = []
        self.ruleset_objects = {}
        self.master_exists = False
        self.master_error = None
        self.puts = []
        self.posts = []
        self.fail_default_branch = False
        self.fail_allow_rebase = False

    # -- repo_lib.gh.run/try_run/run_with_input replacements --------------

    def run(self, args):
        self.calls.append(list(args))
        assert args[0] == "api", args
        rest = args[1:]
        if rest and rest[0] == "--paginate":
            rest = rest[1:]
        endpoint = rest[0]
        jq = rest[rest.index("--jq") + 1] if "--jq" in rest else None

        m = _DEFAULT_BRANCH_RE.match(endpoint)
        if m and jq == ".default_branch":
            if self.fail_default_branch:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            return self.default_branch + "\n"
        if m and jq == ".allow_rebase_merge":
            if self.fail_allow_rebase:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            return self.allow_rebase + "\n"

        if _BRANCH_COUNT_RE.match(endpoint):
            return self.branch_count + "\n"

        if _COMMITS_HEAD_RE.match(endpoint):
            if self.default_head_fails:
                raise gh.GhError("gh: HTTP 409: Git Repository is empty.\n")
            return self.default_head_sha + "\n"

        m = _CHECK_RUNS_RE.match(endpoint)
        if m:
            names = self.check_runs.get(m.group(2), [])
            return "".join(json.dumps(n) + "\n" for n in names)

        m = _STATUS_RE.match(endpoint)
        if m:
            contexts = self.statuses.get(m.group(2), [])
            return "".join(json.dumps(c) + "\n" for c in contexts)

        m = _PULLS_RE.match(endpoint)
        if m:
            shas = self.open_prs if m.group(2) == "open" else self.closed_prs
            return "".join(sha + "\n" for sha in shas)

        if _RULESETS_LOOKUP_RE.match(endpoint):
            self._name_lookup_calls += 1
            if self._name_lookup_calls >= 2 and self.existing_ruleset_id_after_second_lookup != "__unset__":
                rid = self.existing_ruleset_id_after_second_lookup
            else:
                rid = self.existing_ruleset_id
            return f"{rid}\n" if rid else ""

        if _RULESETS_ALL_RE.match(endpoint):
            return "".join(f"{rid}\n" for rid in self.all_ruleset_ids)

        m = _RULESET_ONE_RE.match(endpoint)
        if m:
            rid = m.group(2)
            obj = self.ruleset_objects[rid]
            if jq == _OWNERSHIP_JQ:
                lines = [obj.get("enforcement", "")] + [r["type"] for r in obj.get("rules", [])]
                return "\n".join(lines) + "\n"
            if jq:
                ref_name = obj.get("conditions", {}).get("ref_name", {})
                return json.dumps(
                    {"include": ref_name.get("include", []), "exclude": ref_name.get("exclude", [])}
                ) + "\n"
            return json.dumps(obj)

        if _MASTER_BRANCH_RE.match(endpoint):
            if self.master_error:
                raise gh.GhError(self.master_error)
            if self.master_exists:
                return "{}\n"
            raise gh.GhError("gh: HTTP 404: Not Found\n")

        raise AssertionError(f"unexpected endpoint: {endpoint} (jq={jq})")

    def try_run(self, args):
        try:
            return True, self.run(args)
        except gh.GhError as e:
            return False, e.stderr

    def run_with_input(self, args, input_bytes):
        self.calls.append(list(args))
        assert args[0] == "api", args
        method = args[args.index("--method") + 1]
        body = json.loads(input_bytes.decode())
        if method == "PUT":
            self.puts.append((args, body))
        elif method == "POST":
            self.posts.append((args, body))
        else:
            raise AssertionError(f"unexpected method: {method}")
        return b""


def _run(fake, argv, isatty=False):
    """Runs `repo setup <argv>` against `fake`, returning (exit_code,
    stdout, stderr). A string SystemExit (`raise SystemExit("message")`,
    used for the --secret/--app not-yet-implemented refusals) is folded
    into stderr here, the same as the real interpreter does when it
    propagates all the way out uncaught -- `_run`'s own try/except would
    otherwise swallow the message along with the exit."""
    out, err = StringIO(), StringIO()
    with patch("repo_lib.gh.run", fake.run), patch("repo_lib.gh.try_run", fake.try_run), patch(
        "repo_lib.gh.run_with_input", fake.run_with_input
    ), patch("shutil.which", return_value="/usr/bin/gh"), patch(
        "sys.stdin.isatty", return_value=isatty
    ), redirect_stdout(out), redirect_stderr(err):
        try:
            main(["setup", *argv])
            code = 0
        except SystemExit as e:
            if isinstance(e.code, int):
                code = e.code
            else:
                if e.code is not None:
                    print(e.code, file=err)
                code = 1
    return code, out.getvalue(), err.getvalue()


class SetupCmdTest(unittest.TestCase):
    def test_rejects_a_repo_that_is_not_owner_slash_repo(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "not-owner-repo"])
        self.assertEqual(code, 2)
        self.assertIn("OWNER/REPO", err)
        self.assertEqual(fake.calls, [])  # never even called gh

    def test_no_rules_and_rule_are_contradictory(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--no-rules", "--rule", "lanes", REPO])
        self.assertEqual(code, 2)
        self.assertIn("contradictory", err)

    def test_secret_is_not_yet_implemented(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--secret", "TOKEN=/tmp/x", REPO])
        self.assertEqual(code, 1)
        self.assertIn("not yet implemented", err)
        self.assertEqual(fake.calls, [])

    def test_app_is_not_yet_implemented(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--app", "codex", REPO])
        self.assertEqual(code, 1)
        self.assertIn("not yet implemented", err)
        self.assertEqual(fake.calls, [])

    def test_create_targets_default_branch_main_and_master(self):
        # The hardened targeting: a freshly created ruleset's conditions
        # must include ~DEFAULT_BRANCH, refs/heads/main, AND
        # refs/heads/master together, not the old single-target shape.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.posts), 1)
        body = fake.posts[0][1]
        self.assertEqual(
            body["conditions"]["ref_name"]["include"],
            ["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master"],
        )
        self.assertEqual(body["conditions"]["ref_name"]["exclude"], [])
        contexts = [
            c["context"]
            for rule in body["rules"]
            if rule["type"] == "required_status_checks"
            for c in rule["parameters"]["required_status_checks"]
        ]
        self.assertEqual(contexts, ["lanes", "codex", "zizmor"])
        self.assertIn(f"{REPO}: created ruleset", out)

    def test_default_checks_used_when_no_rule_given(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, _, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        contexts = [
            c["context"]
            for rule in fake.posts[0][1]["rules"]
            if rule["type"] == "required_status_checks"
            for c in rule["parameters"]["required_status_checks"]
        ]
        self.assertEqual(contexts, ["lanes", "codex", "zizmor"])

    def test_update_leaves_existing_scope_and_unmanaged_fields_alone(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "42"
        fake.all_ruleset_ids = ["42"]
        fake.ruleset_objects["42"] = {
            "id": 42,
            "name": "merge gates",
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [{"actor_id": 1, "actor_type": "Team"}],
            "conditions": {"ref_name": {"include": ["refs/heads/release"], "exclude": []}},
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [{"context": "old-check", "integration_id": 9}],
                    },
                },
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_review_thread_resolution": True,
                        "allowed_merge_methods": ["rebase"],
                        "required_approving_review_count": 0,
                        "dismiss_stale_reviews_on_push": False,
                        "require_code_owner_review": False,
                        "require_last_push_approval": False,
                    },
                },
            ],
        }
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        body = fake.puts[0][1]
        # Scope, target, and bypass_actors are all untouched -- an UPDATE
        # only ever edits the two managed rules.
        self.assertEqual(body["conditions"]["ref_name"]["include"], ["refs/heads/release"])
        self.assertEqual(body["bypass_actors"], [{"actor_id": 1, "actor_type": "Team"}])
        checks_rule = next(r for r in body["rules"] if r["type"] == "required_status_checks")
        self.assertEqual(
            checks_rule["parameters"]["required_status_checks"], [{"context": "lanes"}]
        )
        self.assertIn("scope is unchanged", out)

    def test_update_preserves_integration_id_for_a_matching_context(self):
        # An existing entry's integration_id (which binds a required check
        # to a specific GitHub App) survives an update -- adding a second
        # check ("codex") is what makes this a real change to write, while
        # "lanes"'s own entry must come through untouched.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "merge gates",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [{"context": "lanes", "integration_id": 55}],
                    },
                },
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_review_thread_resolution": True,
                        "allowed_merge_methods": ["rebase"],
                        "required_approving_review_count": 0,
                        "dismiss_stale_reviews_on_push": False,
                        "require_code_owner_review": False,
                        "require_last_push_approval": False,
                    },
                },
            ],
        }
        code, _, err = _run(fake, ["--force", "--rule", "lanes", "--rule", "codex", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        checks_rule = next(
            r for r in fake.puts[0][1]["rules"] if r["type"] == "required_status_checks"
        )
        self.assertEqual(
            checks_rule["parameters"]["required_status_checks"],
            [{"context": "lanes", "integration_id": 55}, {"context": "codex"}],
        )

    def test_refuses_to_write_when_the_ruleset_was_renamed_during_confirmation(self):
        # `existing` (id 7) was looked up by name once already, before this
        # ran to completion; if that name no longer resolves to id 7 by the
        # time the write is about to happen -- renamed away, or reassigned
        # to a different ruleset -- the PUT must not silently land on
        # whatever ruleset id 7 now denotes.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.existing_ruleset_id_after_second_lookup = None  # renamed away
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "merge gates",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [{"context": "lanes"}],
                    },
                },
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_review_thread_resolution": True,
                        "allowed_merge_methods": ["rebase"],
                        "required_approving_review_count": 0,
                        "dismiss_stale_reviews_on_push": False,
                        "require_code_owner_review": False,
                        "require_last_push_approval": False,
                    },
                },
            ],
        }
        code, _, err = _run(fake, ["--force", "--rule", "lanes", "--rule", "codex", REPO])
        self.assertEqual(code, 1)
        self.assertIn("no longer resolves", err)
        self.assertEqual(fake.puts, [])
        self.assertEqual(fake.posts, [])

    def test_unchanged_ruleset_reports_nothing_to_do_and_does_not_write(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "merge gates",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [{"context": "lanes"}],
                    },
                },
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_review_thread_resolution": True,
                        "allowed_merge_methods": ["rebase"],
                        "required_approving_review_count": 0,
                        "dismiss_stale_reviews_on_push": False,
                        "require_code_owner_review": False,
                        "require_last_push_approval": False,
                    },
                },
            ],
        }
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.puts, [])
        self.assertIn("already matches; nothing to do", out)

    def test_dry_run_makes_no_writes(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.puts, [])
        self.assertIn("would create ruleset", out)

    def test_missing_check_blocks_without_force(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: []}  # 'lanes' never reported
        code, _, err = _run(fake, ["--dry-run", "--force", "--rule", "lanes", REPO])
        # --force still skips the confirmation prompt but NOT the
        # never-reported guard -- only rerunning without --dry-run and
        # WITH --force does that (matching repo-rules: --force overrides
        # the guard, --dry-run alone never applies anything).
        self.assertEqual(code, 0, err)  # dry-run's own preview succeeded
        self.assertIn("never reported", err)

        code, _, err = _run(fake, ["--rule", "lanes", REPO])  # no --force at all
        self.assertEqual(code, 1)
        self.assertIn("never reported", err)
        self.assertEqual(fake.posts, [])

    def test_missing_check_allowed_with_force(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: []}
        code, out, err = _run(fake, ["--force", "--rule", "never-reported", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("never reported", err)
        self.assertIn("--force given", err)
        self.assertEqual(len(fake.posts), 1)

    def test_rebase_disabled_blocks_with_a_clear_error(self):
        fake = FakeGh()
        fake.allow_rebase = "false"
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("rebase merging disabled", err)
        self.assertEqual(fake.posts, [])

    def test_ruleset_with_unmanaged_rule_type_is_refused(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "3"
        fake.all_ruleset_ids = ["3"]
        fake.ruleset_objects["3"] = {
            "id": 3,
            "name": "merge gates",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [{"type": "non_fast_forward", "parameters": {}}],
        }
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("does not manage", err)
        self.assertIn("non_fast_forward", err)
        self.assertEqual(fake.puts, [])

    def test_conflicting_ruleset_blocks_the_write(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.all_ruleset_ids = ["9"]
        fake.ruleset_objects["9"] = {
            "id": 9,
            "name": "squash only",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
            "rules": [
                {"type": "pull_request", "parameters": {"allowed_merge_methods": ["squash"]}}
            ],
        }
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("excludes rebase", err)
        self.assertIn("squash only", err)
        self.assertEqual(fake.posts, [])

    def test_undecidable_scope_blocks_the_write(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.all_ruleset_ids = ["9"]
        fake.ruleset_objects["9"] = {
            "id": 9,
            "name": "glob ruleset",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/*"], "exclude": []}},
            "rules": [
                {"type": "pull_request", "parameters": {"allowed_merge_methods": ["squash"]}}
            ],
        }
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("cannot evaluate", err)
        self.assertEqual(fake.posts, [])

    def test_gh_failure_reading_the_repo_surfaces_a_clear_error(self):
        fake = FakeGh()
        fake.fail_default_branch = True
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not read {REPO}", err)
        self.assertEqual(fake.posts, [])

    def test_gh_failure_writing_the_ruleset_surfaces_a_clear_error(self):
        class FailingWriteGh(FakeGh):
            def run_with_input(self, args, input_bytes):
                raise gh.GhError("gh: HTTP 403: Resource not accessible by integration\n")

        fake = FailingWriteGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not create ruleset", err)
        self.assertIn("403", err)

    def test_no_terminal_and_no_force_refuses_without_writing(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        code, _, err = _run(fake, ["--rule", "lanes", REPO], isatty=False)
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(fake.posts, [])

    def test_confirmed_interactively_applies_the_change(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        with patch("builtins.input", return_value="y"):
            code, out, err = _run(fake, ["--rule", "lanes", REPO], isatty=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.posts), 1)

    def test_declined_interactively_makes_no_writes(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        with patch("builtins.input", return_value="n"):
            code, _, err = _run(fake, ["--rule", "lanes", REPO], isatty=True)
        self.assertEqual(code, 1)
        self.assertIn("not confirmed", err)
        self.assertEqual(fake.posts, [])

    def test_no_rules_skips_the_ruleset_step_but_still_checks_master(self):
        fake = FakeGh()
        fake.master_exists = True
        code, _, err = _run(fake, ["--no-rules", "--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.puts, [])
        self.assertIn("branch literally named 'master'", err)

    def test_master_branch_warning_fires_when_master_exists(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.master_exists = True
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("branch literally named 'master'", err)
        # It warns; it does not fail the run over an advisory finding.
        self.assertEqual(code, 0)

    def test_no_master_branch_warning_when_master_is_absent(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.master_exists = False
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("master", err)

    def test_master_branch_check_failure_is_reported_but_not_fatal(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.master_error = "gh: HTTP 403: Resource protected by organization SAML enforcement\n"
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("could not check whether", err)
        self.assertIn("403", err)


if __name__ == "__main__":
    unittest.main()
