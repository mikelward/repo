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
_RULES_RE = re.compile(r"^repos/([^/]+/[^/]+)/rules/branches/(.+)$")
_RULESETS_ALL_RE = re.compile(r"^repos/([^/]+/[^/]+)/rulesets\?includes_parents=true$")
_RULESET_ONE_RE = re.compile(r"^repos/([^/]+/[^/]+)/rulesets/([^/]+)$")
_MASTER_BRANCH_RE = re.compile(r"^repos/([^/]+/[^/]+)/branches/master$")


def _parse_api_args(rest):
    """Same shape as test_setup_cmd.py's own helper -- (endpoint, method,
    jq) for a `gh api ...` argv tail."""
    endpoint = None
    method = None
    jq = None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--paginate":
            i += 1
        elif a == "--method":
            method = rest[i + 1]
            i += 2
        elif a == "--jq":
            jq = rest[i + 1]
            i += 2
        else:
            endpoint = a
            i += 1
    return endpoint, method, jq


def _pull_request_rule(resolve=True):
    return {
        "type": "pull_request",
        "parameters": {"required_review_thread_resolution": resolve},
    }


def _status_checks_rule(contexts, strict=True):
    return {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": strict,
            "required_status_checks": [{"context": c} for c in contexts],
        },
    }


DEFAULT_EFFECTIVE_RULES = [
    _pull_request_rule(),
    _status_checks_rule(["lanes", "codex", "zizmor"]),
    {"type": "non_fast_forward", "parameters": {}},
    {"type": "deletion", "parameters": {}},
]


class FakeGh:
    """Models one repository's GitHub state for repo_lib.gh.run/try_run,
    scoped to exactly the endpoints repo_lib.audit_cmd calls -- no fake
    `gh` binary on PATH."""

    def __init__(self):
        self.calls = []
        self.default_branch = "main"
        self.default_branch_fails = False
        self.effective_rules = list(DEFAULT_EFFECTIVE_RULES)
        self.effective_rules_fails = None  # gh stderr text, or None
        # None means "one page" (self.effective_rules as a whole). Set to a
        # list of rule lists to model effective rules that genuinely span
        # more than one --paginate page.
        self.effective_rules_pages = None
        self.ruleset_ids = []
        self.ruleset_objects = {}  # id -> ruleset dict
        self.ruleset_read_fails = set()  # ids whose read fails
        self.rulesets_list_fails = None  # gh stderr text, or None
        self.master_exists = False
        self.master_error = None  # non-404 stderr text, or None

    def run(self, args):
        self.calls.append(list(args))
        assert args[0] == "api", args
        endpoint, method, jq = _parse_api_args(args[1:])

        m = _DEFAULT_BRANCH_RE.match(endpoint)
        if m and jq == ".default_branch":
            if self.default_branch_fails:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            return self.default_branch + "\n"

        m = _RULES_RE.match(endpoint)
        if m:
            if self.effective_rules_fails is not None:
                raise gh.GhError(self.effective_rules_fails)
            pages = self.effective_rules_pages or [self.effective_rules]
            if jq == ".[]":
                # Models --paginate --jq '.[]': jq unwraps each page's
                # array into one compact JSON object per line, so the
                # combined output is one rule per line regardless of how
                # many pages contributed them.
                return "".join(
                    json.dumps(rule) + "\n" for page in pages for rule in page
                )
            # No --jq: models raw --paginate output, which is each page's
            # own JSON array concatenated back-to-back -- NOT merged into
            # one array. A single page still happens to be one valid JSON
            # document; more than one page is not, and a bare json.loads
            # over it raises JSONDecodeError.
            return "".join(json.dumps(page) for page in pages)

        if _RULESETS_ALL_RE.match(endpoint):
            if self.rulesets_list_fails is not None:
                raise gh.GhError(self.rulesets_list_fails)
            return "".join(f"{rid}\n" for rid in self.ruleset_ids)

        m = _RULESET_ONE_RE.match(endpoint)
        if m:
            rid = m.group(2)
            if rid in self.ruleset_read_fails:
                raise gh.GhError(f"gh: HTTP 500: simulated failure reading ruleset {rid}\n")
            return json.dumps(self.ruleset_objects[rid])

        raise AssertionError(f"unexpected endpoint: {endpoint} (method={method} jq={jq})")

    def try_run(self, args):
        self.calls.append(list(args))
        assert args[0] == "api", args
        endpoint = args[1]
        if _MASTER_BRANCH_RE.match(endpoint):
            if self.master_error:
                return False, self.master_error
            if self.master_exists:
                return True, "{}\n"
            return False, "gh: HTTP 404: Not Found\n"
        raise AssertionError(f"unexpected try_run endpoint: {endpoint}")


def _run(fake, argv):
    """Runs `repo audit <argv>` against `fake`, returning (exit_code,
    stdout, stderr)."""
    out, err = StringIO(), StringIO()
    with patch("repo_lib.gh.run", fake.run), patch("repo_lib.gh.try_run", fake.try_run), patch(
        "shutil.which", return_value="/usr/bin/gh"
    ), redirect_stdout(out), redirect_stderr(err):
        try:
            main(["audit", *argv])
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def _covering_ruleset(name="merge gates", include=None, exclude=None, bypass_actors=None):
    # Default `include` is the FULL hardened set (all three required
    # refs, literally) rather than just "~DEFAULT_BRANCH" -- so a test
    # focused purely on bypass-actor reporting doesn't also trip the
    # (correct, but unrelated) targeting-completeness gap as a side
    # effect. Tests that want to exercise targeting incompleteness pass
    # their own `include`.
    return {
        "id": 1,
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {
                "include": (
                    include
                    if include is not None
                    else ["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master"]
                ),
                "exclude": exclude or [],
            }
        },
        "bypass_actors": bypass_actors or [],
        "rules": [],
    }


class AuditCmdTest(unittest.TestCase):
    # ---- usage errors -----------------------------------------------

    def test_rejects_a_repo_that_is_not_owner_slash_repo(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["not-owner-repo"])
        self.assertEqual(code, 2)
        self.assertIn("OWNER/REPO", err)
        self.assertEqual(fake.calls, [])

    def test_rejects_a_control_character_in_a_check_name(self):
        fake = FakeGh()
        code, _, err = _run(fake, [REPO, "lanes\ncodex"])
        self.assertEqual(code, 2)
        self.assertIn("control character", err)
        self.assertEqual(fake.calls, [])

    def test_rejects_an_empty_check_name_instead_of_falling_back_to_defaults(self):
        fake = FakeGh()
        code, _, err = _run(fake, [REPO, ""])
        self.assertEqual(code, 2)
        self.assertIn("empty check name", err)
        self.assertEqual(fake.calls, [])

    def test_rejects_an_explicitly_empty_branch_instead_of_falling_back_to_default(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--branch", "", REPO])
        self.assertEqual(code, 2)
        self.assertIn("--branch", err)
        self.assertEqual(fake.calls, [])

    # ---- default branch name ------------------------------------------

    def test_default_branch_named_main_is_ok(self):
        fake = FakeGh()
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] the default branch is named 'main'", out)

    def test_default_branch_not_named_main_is_a_gap(self):
        fake = FakeGh()
        fake.default_branch = "trunk"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] the default branch is 'trunk', not 'main'", out)

    def test_explicit_branch_skips_the_default_branch_name_check(self):
        fake = FakeGh()
        code, out, err = _run(fake, ["--branch", "release", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("the default branch is", out)

    # ---- pull_request / conversation resolution ------------------------

    def test_missing_pull_request_rule_is_a_gap(self):
        fake = FakeGh()
        fake.effective_rules = [r for r in DEFAULT_EFFECTIVE_RULES if r["type"] != "pull_request"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] no pull_request rule", out)

    def test_present_pull_request_rule_is_ok(self):
        fake = FakeGh()
        code, out, err = _run(fake, [REPO])
        self.assertIn("[ok] a pull request is required before merging", out)

    def test_conversation_resolution_not_required_is_a_gap(self):
        fake = FakeGh()
        fake.effective_rules = [
            r if r["type"] != "pull_request" else _pull_request_rule(resolve=False)
            for r in DEFAULT_EFFECTIVE_RULES
        ]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] conversation resolution is not required", out)

    def test_conversation_resolution_required_is_ok(self):
        fake = FakeGh()
        code, out, _ = _run(fake, [REPO])
        self.assertIn("[ok] review conversations must be resolved", out)

    # ---- required_status_checks -----------------------------------------

    def test_each_named_check_reported_ok_or_gap(self):
        fake = FakeGh()
        fake.effective_rules = [
            r if r["type"] != "required_status_checks" else _status_checks_rule(["lanes"])
            for r in DEFAULT_EFFECTIVE_RULES
        ]
        code, out, err = _run(fake, [REPO, "lanes", "codex"])
        self.assertEqual(code, 1, err)
        self.assertIn("[ok] 'lanes' is a required status check", out)
        self.assertIn("[GAP] 'codex' is NOT a required status check", out)

    def test_default_checks_used_when_none_given(self):
        fake = FakeGh()
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        for check in ("lanes", "codex", "zizmor"):
            self.assertIn(f"[ok] '{check}' is a required status check", out)

    def test_no_required_status_checks_rule_at_all_is_a_gap(self):
        fake = FakeGh()
        fake.effective_rules = [
            r for r in DEFAULT_EFFECTIVE_RULES if r["type"] != "required_status_checks"
        ]
        code, out, err = _run(fake, [REPO, "lanes"])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] no required_status_checks rule at all -- named: lanes", out)

    def test_stale_base_allowed_to_merge_is_a_gap(self):
        fake = FakeGh()
        fake.effective_rules = [
            r
            if r["type"] != "required_status_checks"
            else _status_checks_rule(["lanes", "codex", "zizmor"], strict=False)
            for r in DEFAULT_EFFECTIVE_RULES
        ]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] branches do NOT need to be up to date", out)

    def test_branch_must_be_up_to_date_is_ok(self):
        fake = FakeGh()
        code, out, _ = _run(fake, [REPO])
        self.assertIn("[ok] the branch must be up to date with the base before merging", out)

    # ---- non_fast_forward / deletion -----------------------------------

    def test_missing_non_fast_forward_rule_is_a_gap(self):
        fake = FakeGh()
        fake.effective_rules = [r for r in DEFAULT_EFFECTIVE_RULES if r["type"] != "non_fast_forward"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] force pushes are allowed", out)

    def test_present_non_fast_forward_rule_is_ok(self):
        fake = FakeGh()
        code, out, _ = _run(fake, [REPO])
        self.assertIn("[ok] force pushes are blocked", out)

    def test_missing_deletion_rule_is_a_gap(self):
        fake = FakeGh()
        fake.effective_rules = [r for r in DEFAULT_EFFECTIVE_RULES if r["type"] != "deletion"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] branch deletion is allowed", out)

    def test_present_deletion_rule_is_ok(self):
        fake = FakeGh()
        code, out, _ = _run(fake, [REPO])
        self.assertIn("[ok] branch deletion is blocked", out)

    # ---- bypass actors --------------------------------------------------

    def test_bypass_actor_on_a_covering_ruleset_is_a_gap(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            bypass_actors=[{"actor_type": "Team", "actor_id": 5, "bypass_mode": "always"}]
        )
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] bypass actors configured on a ruleset covering main:", out)
        self.assertIn("merge gates: Team 5 (always)", out)

    def test_no_bypass_actor_on_any_covering_ruleset_is_ok(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset()
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] no bypass actor on any ruleset that plainly covers main", out)

    def test_a_ruleset_not_covering_the_branch_is_ignored(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(include=["refs/heads/release"])
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] no bypass actor on any ruleset that plainly covers main", out)
        self.assertNotIn("[CHECK]", out)

    # ---- targeting completeness: literal-first-then-glob-fallback -------

    def test_targeting_complete_with_all_three_refs_literal(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master"]
        )
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[ok] every ruleset covering main targets the default branch, "
            "refs/heads/main and refs/heads/master",
            out,
        )

    def test_targeting_gap_when_a_required_ref_is_missing(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=["~DEFAULT_BRANCH", "refs/heads/main"]  # refs/heads/master missing
        )
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] a ruleset covering main does not target all", out)
        self.assertIn("does not target refs/heads/master", out)

    def test_literal_coverage_checked_first_ahead_of_an_unrelated_glob(self):
        # All three required refs are already literal in `include`; an
        # unrelated glob sitting alongside them must not downgrade this
        # to "unevaluated" -- literal coverage is resolved first and
        # completely, regardless of what else `include` also contains.
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=[
                "~DEFAULT_BRANCH",
                "refs/heads/main",
                "refs/heads/master",
                "refs/heads/release/*",
            ]
        )
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] every ruleset covering main targets", out)
        self.assertNotIn("[CHECK]", out)

    def test_glob_fallback_for_a_genuinely_missing_ref_is_unevaluated_not_guessed(self):
        # INNER unevaluated path: the ruleset plainly COVERS the branch
        # (a literal refs/heads/main entry), but refs/heads/master is not
        # spelled out literally -- only reachable, maybe, through an
        # unrelated glob. This script does not reimplement GitHub's ref
        # matching, so it must not guess either way: it reports
        # "unevaluated", not "missing" and not "complete".
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/release/*"]
        )
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[CHECK]", out)
        self.assertIn("targeting not evaluated", out)
        # The "every ruleset covering ... targets ..." success claim must
        # be suppressed -- this audit was never actually completed for
        # this ruleset, so printing it would be a false all-clear.
        self.assertNotIn("every ruleset covering", out)
        self.assertNotIn("does not target all of the default branch", out)

    def test_glob_in_include_alone_is_the_outer_unevaluated_path(self):
        # OUTER unevaluated path #1: branch coverage itself is
        # undetermined (include is nothing but a glob) -- distinct from
        # the inner case above, where coverage was certain and only the
        # TARGETING check was left unresolved. Bypass actors on this
        # ruleset must also go unreported: whether it even covers the
        # branch is unknown.
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=["refs/heads/*"],
            bypass_actors=[{"actor_type": "Team", "actor_id": 9, "bypass_mode": "always"}],
        )
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[CHECK]", out)
        self.assertIn("(matches: refs/heads/*)", out)
        self.assertIn("[ok] no bypass actor on any ruleset that plainly covers main", out)
        self.assertNotIn("every ruleset covering", out)

    def test_glob_exclude_over_a_complete_include_is_the_other_outer_unevaluated_path(self):
        # OUTER unevaluated path #2: `include` literally covers the
        # branch and all three required refs, but `exclude` carries a
        # glob rather than a literal exclusion -- this script cannot tell
        # whether that glob reaches the branch, so the WHOLE ruleset
        # (coverage, not just targeting) is unevaluated, same as path #1
        # above but reached through the opposite side of the condition.
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master"],
            exclude=["refs/heads/experimental/*"],
        )
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[CHECK]", out)
        self.assertNotIn("every ruleset covering", out)

    def test_exclude_literally_carving_a_required_ref_back_out_is_a_gap(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master"],
            exclude=["refs/heads/master"],
        )
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("does not target refs/heads/master", out)

    def test_exclude_with_no_conflicting_literal_ref_is_complete(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master"],
            exclude=["refs/heads/experimental"],  # literal, but not one of the three
        )
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] every ruleset covering main targets", out)

    def test_tilde_all_covers_all_three_required_refs(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(include=["~ALL"])
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] every ruleset covering main targets", out)

    def test_tilde_all_with_exclude_carving_a_required_ref_back_out(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=["~ALL"], exclude=["refs/heads/master"]
        )
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("does not target refs/heads/master", out)

    # ---- explicit --branch naming the real default ----------------------

    def test_explicit_branch_that_is_the_real_default_still_runs_targeting_check(self):
        fake = FakeGh()
        fake.default_branch = "main"
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=["~DEFAULT_BRANCH", "refs/heads/main"]  # missing refs/heads/master
        )
        code, out, err = _run(fake, ["--branch", "main", REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("does not target refs/heads/master", out)
        # Resolving ~DEFAULT_BRANCH's real value costs a repos/{repo} call
        # even though --branch was given explicitly.
        self.assertTrue(any(c[1] == f"repos/{REPO}" for c in fake.calls if len(c) > 1))

    def test_explicit_branch_that_is_not_the_real_default_skips_targeting_check(self):
        fake = FakeGh()
        fake.default_branch = "main"
        fake.ruleset_ids = ["1"]
        # Would be a targeting gap on the default branch, but this
        # ruleset covers "release", which is never claiming to BE main.
        fake.ruleset_objects["1"] = _covering_ruleset(include=["refs/heads/release"])
        code, out, err = _run(fake, ["--branch", "release", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("every ruleset covering", out)
        self.assertNotIn("does not target", out)

    # ---- master branch check --------------------------------------------

    def test_master_branch_present_is_a_gap(self):
        fake = FakeGh()
        fake.master_exists = True
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] a branch literally named 'master' exists", out)

    def test_master_branch_absent_is_ok(self):
        fake = FakeGh()
        fake.master_exists = False
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] no branch literally named 'master'", out)

    def test_master_branch_read_failure_fails_closed(self):
        fake = FakeGh()
        fake.master_error = "gh: HTTP 403: Resource protected by organization SAML enforcement\n"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not check whether", err)
        self.assertIn("403", err)
        # A read failure is never reported as a gap or an ok -- "could not
        # tell" and "found a gap" are different findings. Not a bare
        # "master" substring check: the targeting-completeness summary
        # legitimately mentions "refs/heads/master" as part of the
        # hardened targeting it confirmed -- it's specifically the
        # branch-exists finding that must be absent.
        self.assertNotIn("branch literally named 'master'", out)

    # ---- hard failures (fail closed, never a guessed gap) ----------------

    def test_could_not_read_default_branch_is_fatal(self):
        fake = FakeGh()
        fake.default_branch_fails = True
        code, _, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not read {REPO}", err)
        # The underlying gh error (auth, rate limit, ...) is relayed, not
        # discarded -- matching every other API failure path in this file.
        self.assertIn("404", err)

    def test_could_not_resolve_default_branch_for_a_tilde_default_branch_ruleset_is_fatal(self):
        # Reaches the OTHER default-branch lookup: default_branch_matches(),
        # lazily fetching the real default to resolve a ruleset's
        # ~DEFAULT_BRANCH condition when --branch was given explicitly.
        fake = FakeGh()
        fake.default_branch_fails = True
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(
            include=["~DEFAULT_BRANCH", "refs/heads/main"]
        )
        code, _, err = _run(fake, ["--branch", "main", REPO])
        self.assertEqual(code, 1, err)
        self.assertIn(f"could not read {REPO}'s default branch", err)
        self.assertIn("404", err)

    def test_could_not_read_effective_rules_is_fatal(self):
        fake = FakeGh()
        fake.effective_rules_fails = "gh: HTTP 404: Not Found\n"
        code, _, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not read", err)
        self.assertIn("effective rules", err)

    def test_could_not_list_rulesets_is_fatal(self):
        fake = FakeGh()
        fake.rulesets_list_fails = "gh: HTTP 500: Internal Server Error\n"
        code, _, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not list", err)

    def test_could_not_read_a_single_ruleset_is_fatal(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_read_fails = {"1"}
        code, _, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not read ruleset 1", err)

    # ---- overall exit status ----------------------------------------------

    def test_clean_repo_exits_zero(self):
        fake = FakeGh()
        code, _, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)

    def test_any_single_gap_makes_the_whole_run_exit_nonzero(self):
        fake = FakeGh()
        fake.master_exists = True  # the only gap
        code, _, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)

    # ---- branch name encoding ----------------------------------------------

    def test_branch_name_is_percent_encoded_in_the_rules_endpoint(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--branch", "feature/x#1", REPO])
        self.assertEqual(code, 0, err)
        endpoints = [
            _parse_api_args(c[1:])[0] for c in fake.calls if c and c[0] == "api"
        ]
        self.assertIn(f"repos/{REPO}/rules/branches/feature%2Fx%231", endpoints)

    def test_effective_rules_request_is_paginated(self):
        # A branch's effective rules can span more than one page (e.g. many
        # rulesets contribute rules); an unpaginated request would silently
        # inspect only the first page and report gaps that aren't real.
        # --jq '.[]' is required alongside --paginate: without it, gh
        # concatenates each page's own JSON array rather than merging them,
        # so a plain json.loads over the raw multi-page output breaks.
        fake = FakeGh()
        code, _, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        rules_calls = [
            c for c in fake.calls if c and c[0] == "api" and "--paginate" in c
        ]
        matched = [
            c for c in rules_calls if _parse_api_args(c[1:])[0] == f"repos/{REPO}/rules/branches/main"
        ]
        self.assertEqual(len(matched), 1, rules_calls)
        self.assertEqual(_parse_api_args(matched[0][1:])[2], ".[]")

    def test_a_required_check_on_a_later_effective_rules_page_is_not_missed(self):
        # Regression: reading --paginate output with a single json.loads
        # (no --jq) doesn't just miss later pages -- gh's raw multi-page
        # output is several concatenated JSON arrays, so that json.loads
        # call raises JSONDecodeError the moment there's more than one
        # page. Modeling the rules as genuinely split across two pages
        # exercises the line-per-rule parsing that has to survive that.
        fake = FakeGh()
        fake.effective_rules_pages = [
            [_pull_request_rule(), _status_checks_rule(["lanes"])],
            [{"type": "non_fast_forward", "parameters": {}}, {"type": "deletion", "parameters": {}}],
        ]
        code, out, err = _run(fake, [REPO, "lanes"])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok]", out)


if __name__ == "__main__":
    unittest.main()
