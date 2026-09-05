import base64
import hashlib
import json
import re
import urllib.parse
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
_RULESETS_LOOKUP_RE = re.compile(r"^repos/([^/]+/[^/]+)/rulesets\?includes_parents=false$")
_RULESET_ONE_RE = re.compile(r"^repos/([^/]+/[^/]+)/rulesets/([^/]+)$")
_MASTER_BRANCH_RE = re.compile(r"^repos/([^/]+/[^/]+)/branches/master$")
_COMMITS_HEAD_RE = re.compile(r"^repos/([^/]+/[^/]+)/commits\?per_page=1(?:&sha=(.+))?$")
_CHECK_RUNS_RE = re.compile(r"^repos/([^/]+/[^/]+)/commits/([^/]+)/check-runs$")
_STATUS_RE = re.compile(r"^repos/([^/]+/[^/]+)/commits/([^/]+)/status$")
_PULLS_RE = re.compile(r"^repos/([^/]+/[^/]+)/pulls\?state=(open|closed)&.*$")
_REPO_SECRETS_RE = re.compile(r"^repos/([^/]+/[^/]+)/actions/secrets$")
_ENVIRONMENTS_RE = re.compile(r"^repos/([^/]+/[^/]+)/environments$")
_ENV_SECRETS_RE = re.compile(r"^repos/([^/]+/[^/]+)/environments/([^/]+)/secrets$")
_ENV_ONE_RE = re.compile(r"^repos/([^/]+/[^/]+)/environments/([^/]+)$")
_ENV_POLICIES_RE = re.compile(r"^repos/([^/]+/[^/]+)/environments/([^/]+)/deployment-branch-policies$")
_WORKFLOW_FILE_RE = re.compile(r"^repos/([^/]+/[^/]+)/contents/\.github/workflows/([^/?]+)(?:\?ref=(.+))?$")
_WORKFLOWS_DIR_RE = re.compile(r"^repos/([^/]+/[^/]+)/contents/\.github/workflows(?:\?ref=(.+))?$")
_ROOT_CONTENTS_RE = re.compile(r"^repos/([^/]+/[^/]+)/contents(?:\?ref=(.+))?$")
_BRANCHES_RE = re.compile(r"^repos/([^/]+/[^/]+)/branches\?per_page=100$")


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
            # A context may be a bare name, or (name, integration_id) to
            # model a gate bound to one GitHub App.
            "required_status_checks": [
                {"context": c[0], "integration_id": c[1]}
                if isinstance(c, tuple)
                else {"context": c}
                for c in contexts
            ],
        },
    }


DEFAULT_EFFECTIVE_RULES = [
    _pull_request_rule(),
    _status_checks_rule(["lanes", "codex", "zizmor"]),
    {"type": "required_linear_history", "parameters": {}},
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
        # Other branches: name -> {workflow name: text}; see the setup fake.
        self.branch_workflows = {}
        self.allow_auto_merge = "true"
        self.auto_merge_fails = None  # gh stderr text, or None
        self.delete_branch_on_merge = "true"
        self.delete_branch_on_merge_fails = None  # gh stderr text, or None
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
        self.rulesets_lookup_fails = None  # the by-name lookup's own failure
        # Which ids the repository itself owns; None means all of them.
        # Only consulted for an includes_parents=false lookup.
        self.repo_owned_ruleset_ids = None
        self.master_exists = False
        self.master_error = None  # non-404 stderr text, or None
        self.master_redirect_name = None  # branch a renamed master redirects to
        # A healthy repo: every default check has reported on the head, so
        # the never-reported walk short-circuits after one commit.
        self.default_head_sha = "abc123"
        self.default_head_fails = False
        # ref -> head sha, for branch-specific gates. Any ref not listed
        # answers with default_head_sha.
        self.branch_heads = {}
        self.check_runs = {"abc123": ["lanes", "codex", "zizmor"]}
        self.check_runs_fails = None  # gh stderr text, or None
        self.statuses = {}
        self.open_prs = []
        self.closed_prs = []
        # The secrets audit. Names only, as the API itself answers.
        self.repo_secrets = []
        self.repo_secrets_fails = None  # gh stderr text, or None
        self.environments = {}  # name -> list of environment secret names
        self.environments_fails = None
        self.env_secrets_fails = set()  # environment names whose read fails
        # name -> which branches may reach the environment: None (any
        # branch), "protected", or a list of custom policy patterns.
        self.env_policies = {}
        self.env_read_fails = set()  # environment names whose own read fails
        # None models a repository with no .github/workflows at all (404);
        # a list is the file names the directory holds.
        self.workflow_files = None
        self.workflows_error = None  # non-404 stderr text, or None
        # A caller file's text, by name; a hub caller not listed here reads
        # as the fleet's own shape, an inheriting job-level `uses:`.
        self.workflow_texts = {}
        self.workflow_text_fails = set()  # file names whose read fails
        self.root_contents_error = None  # stderr text for the root listing, or None to succeed

    def run(self, args):
        self.calls.append(list(args))
        assert args[0] == "api", args
        endpoint, method, jq = _parse_api_args(args[1:])

        m = _DEFAULT_BRANCH_RE.match(endpoint)
        if m and jq == ".default_branch":
            if self.default_branch_fails:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            return self.default_branch + "\n"
        if m and jq == ".allow_auto_merge":
            if self.auto_merge_fails is not None:
                raise gh.GhError(self.auto_merge_fails)
            return self.allow_auto_merge + "\n"
        if m and jq == ".delete_branch_on_merge":
            if self.delete_branch_on_merge_fails is not None:
                raise gh.GhError(self.delete_branch_on_merge_fails)
            return self.delete_branch_on_merge + "\n"

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

        m = _COMMITS_HEAD_RE.match(endpoint)
        if m:
            if self.default_head_fails:
                raise gh.GhError("gh: HTTP 409: Git Repository is empty.\n")
            ref = urllib.parse.unquote(m.group(2)) if m.group(2) else None
            return self.branch_heads.get(ref, self.default_head_sha) + "\n"

        m = _CHECK_RUNS_RE.match(endpoint)
        if m:
            if self.check_runs_fails is not None:
                raise gh.GhError(self.check_runs_fails)
            # Models --jq '[.name, .app.id]': an entry may be a bare name
            # (no App binding) or a (name, app id) pair.
            return "".join(
                json.dumps(list(n) if isinstance(n, tuple) else [n, None]) + "\n"
                for n in self.check_runs.get(m.group(2), [])
            )

        m = _STATUS_RE.match(endpoint)
        if m:
            return "".join(
                json.dumps(c) + "\n" for c in self.statuses.get(m.group(2), [])
            )

        m = _PULLS_RE.match(endpoint)
        if m:
            shas = self.open_prs if m.group(2) == "open" else self.closed_prs
            return "".join(sha + "\n" for sha in shas)

        if _RULESETS_LOOKUP_RE.match(endpoint) or (
            _RULESETS_ALL_RE.match(endpoint) and jq and "select(.name ==" in jq
        ):
            # rules.find_legacy_rulesets's own by-name lookup, over the
            # repository's OWN rulesets only.
            if self.rulesets_lookup_fails is not None:
                raise gh.GhError(self.rulesets_lookup_fails)
            match = re.search(r"select\(\.name == (\".*?\")\)", jq or "")
            wanted = json.loads(match.group(1)) if match else None
            # includes_parents decides whether an org- or enterprise-level
            # ruleset is in the answer. Honored rather than ignored, so a
            # test asserting the audit counts an inherited one is actually
            # exercising that and not passing because the fake returns
            # everything either way.
            owned_only = "includes_parents=false" in endpoint
            return "".join(
                f"{rid}\n"
                for rid, obj in self.ruleset_objects.items()
                if obj.get("name") == wanted
                and not (owned_only and rid not in self._repo_owned_ids())
            )

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

        if _BRANCHES_RE.match(endpoint):
            return "".join(n + "\n" for n in [self.default_branch, *self.branch_workflows])

        m = _WORKFLOW_FILE_RE.match(endpoint)
        if m and jq == ".content":
            name, ref = m.group(2), (urllib.parse.unquote(m.group(3)) if m.group(3) else None)
            if name in self.workflow_text_fails:
                raise gh.GhError("gh: HTTP 500: boom\n")
            return base64.encodebytes(self._text(name, ref).encode()).decode()

        m = _REPO_SECRETS_RE.match(endpoint)
        if m and jq == ".secrets[].name":
            if self.repo_secrets_fails is not None:
                raise gh.GhError(self.repo_secrets_fails)
            return "".join(name + "\n" for name in self.repo_secrets)

        m = _ENVIRONMENTS_RE.match(endpoint)
        if m and jq == ".environments[].name":
            if self.environments_fails is not None:
                raise gh.GhError(self.environments_fails)
            return "".join(name + "\n" for name in self.environments)

        m = _ENV_SECRETS_RE.match(endpoint)
        if m and jq == ".secrets[].name":
            env = urllib.parse.unquote(m.group(2))
            if env in self.env_secrets_fails:
                raise gh.GhError("gh: HTTP 500: boom\n")
            assert env in self.environments, f"secrets read for an environment that does not exist: {env}"
            return "".join(name + "\n" for name in self.environments[env])

        m = _ENV_POLICIES_RE.match(endpoint)
        if m:
            env = urllib.parse.unquote(m.group(2))
            policy = self.env_policies.get(env)
            assert isinstance(policy, list), f"branch policies listed for {env}, whose policy is {policy!r}"
            return "".join(
                (f"tag {p[4:]}" if p.startswith("tag:") else f"branch {p}") + "\n" for p in policy
            )

        m = _ENV_ONE_RE.match(endpoint)
        if m and method is None and jq is None:
            env = urllib.parse.unquote(m.group(2))
            if env in self.env_read_fails:
                raise gh.GhError("gh: HTTP 500: boom\n")
            assert env in self.environments, f"read of an environment that does not exist: {env}"
            policy = self.env_policies.get(env)
            if policy is None:
                branch_policy = None
            elif policy == "protected":
                branch_policy = {"protected_branches": True, "custom_branch_policies": False}
            else:
                branch_policy = {"protected_branches": False, "custom_branch_policies": True}
            return json.dumps({"name": env, "deployment_branch_policy": branch_policy, "protection_rules": []})

        raise AssertionError(f"unexpected endpoint: {endpoint} (method={method} jq={jq})")

    def _repo_owned_ids(self):
        if self.repo_owned_ruleset_ids is None:
            return set(self.ruleset_objects)
        return set(self.repo_owned_ruleset_ids)

    def _text(self, name, ref):
        """A workflow's text on `ref` (None: the default branch); a file
        not given a text reads as an inheriting caller of the hub it is
        named after."""
        if ref and name in self.branch_workflows.get(ref, {}):
            return self.branch_workflows[ref][name]
        if name in self.workflow_texts:
            return self.workflow_texts[name]
        hub = name.rsplit(".", 1)[0]
        return (
            f"jobs:\n  update:\n    uses: mikelward/{hub}/.github/workflows/{hub}.yml@main\n"
            "    secrets: inherit\n"
        )

    def _blob(self, name, ref):
        return hashlib.sha1(self._text(name, ref).encode()).hexdigest()

    def try_run(self, args):
        self.calls.append(list(args))
        assert args[0] == "api", args
        endpoint = args[1]
        if _MASTER_BRANCH_RE.match(endpoint):
            if self.master_error:
                return False, self.master_error
            if self.master_redirect_name:
                # GitHub 301s a renamed branch's old name to the new one and
                # gh follows it, so the call succeeds -- reporting the name
                # it landed on, not the one asked for.
                return True, f"{self.master_redirect_name}\n"
            if self.master_exists:
                return True, "master\n"
            return False, "gh: HTTP 404: Not Found\n"
        m = _WORKFLOWS_DIR_RE.match(endpoint)
        if m:
            ref = urllib.parse.unquote(m.group(2)) if m.group(2) else None
            if self.workflows_error:
                return False, self.workflows_error
            if self.workflow_files is None:
                return False, "gh: HTTP 404: Not Found\n"
            files = [*self.workflow_files, *(self.branch_workflows.get(ref, {}) if ref else {})]
            return True, "".join(f"{n} {self._blob(n, ref)}\n" for n in dict.fromkeys(files))
        if _ROOT_CONTENTS_RE.match(endpoint):
            if self.root_contents_error:
                return False, self.root_contents_error
            return True, "3\n"
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


def _covering_ruleset(name="main", include=None, exclude=None, bypass_actors=None):
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

    # ---- required_linear_history / non_fast_forward / deletion ----------

    def test_missing_required_linear_history_rule_is_a_gap(self):
        fake = FakeGh()
        fake.effective_rules = [
            r for r in DEFAULT_EFFECTIVE_RULES if r["type"] != "required_linear_history"
        ]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[GAP] merge commits are allowed", out)

    def test_present_required_linear_history_rule_is_ok(self):
        fake = FakeGh()
        code, out, _ = _run(fake, [REPO])
        self.assertIn("[ok] commit history must be linear", out)

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
        self.assertIn("main: Team 5 (always)", out)

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

    # ---- required check nothing produces ---------------------------------

    def test_required_check_that_never_reported_is_a_gap(self):
        # The ruleset lists it, so the rule-level check reads clean -- but
        # nothing posts it, so every pull request waits forever.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("[ok] 'zizmor' is a required status check", out)
        self.assertIn("required but never reported: 'zizmor'", out)

    def test_all_required_checks_reporting_is_ok(self):
        fake = FakeGh()
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] every required check has reported", out)

    def test_check_reporting_read_failure_fails_closed(self):
        # "could not tell" is not "never reported" -- it must not print as
        # a gap, and it must not print as an ok either.
        fake = FakeGh()
        fake.check_runs_fails = "gh: HTTP 500: simulated failure\n"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not tell which of", err)
        # The underlying gh error survives: which call failed, on which
        # commit, and what GitHub actually said -- without it a rate limit
        # and a permissions problem read identically.
        self.assertIn("reading check runs for abc123", err)
        self.assertIn("HTTP 500: simulated failure", err)
        self.assertNotIn("required but never reported", out)
        self.assertNotIn("every required check has reported", out)

    def test_enforced_context_outside_the_named_checks_is_still_scanned(self):
        # A stale gate nobody asked about blocks merges just as hard. The
        # scan is built from what the ruleset enforces, not from the
        # names on the command line.
        fake = FakeGh()
        fake.effective_rules = [
            _pull_request_rule(),
            _status_checks_rule(["lanes", "codex", "zizmor", "obsolete-ci"]),
            {"type": "non_fast_forward", "parameters": {}},
            {"type": "deletion", "parameters": {}},
        ]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("required but never reported: 'obsolete-ci'", out)

    def test_same_name_from_another_app_does_not_satisfy_a_bound_gate(self):
        # The gate is bound to App 42; the only 'lanes' run ever seen came
        # from App 7. GitHub will not accept it, so neither does the audit.
        fake = FakeGh()
        fake.effective_rules = [
            _pull_request_rule(),
            _status_checks_rule([("lanes", 42), "codex", "zizmor"]),
            {"type": "non_fast_forward", "parameters": {}},
            {"type": "deletion", "parameters": {}},
        ]
        fake.check_runs = {fake.default_head_sha: [("lanes", 7), "codex", "zizmor"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1, err)
        # The name DOES report, just never from App 42 -- saying it "never
        # reported" reads as false to someone watching lanes run, and
        # `repo setup` would not fix it: _build_update_body reuses the
        # existing entry by context, stale binding included.
        self.assertIn(
            "required but never reported by the App it is bound to: "
            "'lanes' (needs App 42)",
            out,
        )
        self.assertIn("repoint the ruleset entry", out)
        self.assertNotIn("required but never reported: 'lanes'", out)

    def test_missing_names_containing_spaces_stay_separable(self):
        # Check names can contain spaces -- this repo has one called
        # "Classify the diff" -- so a space-joined list leaves the reader
        # unable to tell one missing check from three.
        fake = FakeGh()
        fake.effective_rules = [
            _pull_request_rule(),
            _status_checks_rule(["unit tests", "lint checks"]),
            {"type": "non_fast_forward", "parameters": {}},
            {"type": "deletion", "parameters": {}},
        ]
        fake.check_runs = {fake.default_head_sha: []}
        code, out, err = _run(fake, [REPO, "unit tests"])
        self.assertEqual(code, 1, err)
        self.assertIn("'unit tests', 'lint checks'", out)
        self.assertNotIn("unit tests lint checks", out)

    def test_branch_specific_gate_is_scanned_on_the_audited_branch(self):
        # A check produced only on pushes to `release` never appears on the
        # default branch's head. Scanning that head while auditing
        # `release` reports a working gate as never reported.
        fake = FakeGh()
        fake.ruleset_objects["1"] = _covering_ruleset(include=["refs/heads/release"])
        fake.effective_rules = [
            _pull_request_rule(),
            _status_checks_rule(["release-build"]),
            {"type": "required_linear_history", "parameters": {}},
            {"type": "non_fast_forward", "parameters": {}},
            {"type": "deletion", "parameters": {}},
        ]
        fake.branch_heads = {"release": "rel999"}
        fake.check_runs = {
            fake.default_head_sha: [],  # never reported on main
            "rel999": ["release-build"],  # but reports on release
        }
        code, out, err = _run(fake, ["--branch", "release", REPO, "release-build"])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] every required check has reported", out)
        self.assertNotIn("required but never reported", out)


    def test_a_gate_nothing_produces_and_one_bound_wrong_report_separately(self):
        # Two faults, two fixes: one name nothing posts at all, one that
        # posts from the wrong App. They must not share a remedy line.
        fake = FakeGh()
        fake.effective_rules = [
            _pull_request_rule(),
            _status_checks_rule([("lanes", 42), "missing-gate"]),
            {"type": "non_fast_forward", "parameters": {}},
            {"type": "deletion", "parameters": {}},
        ]
        fake.check_runs = {fake.default_head_sha: [("lanes", 7)]}
        code, out, err = _run(fake, [REPO, "lanes"])
        self.assertEqual(code, 1, err)
        self.assertIn(
            "required but never reported: 'missing-gate' -- add the check, "
            "or run `repo setup`",
            out,
        )
        self.assertIn(
            "required but never reported by the App it is bound to: "
            "'lanes' (needs App 42)",
            out,
        )

    def test_bound_gate_satisfied_by_its_own_app_is_ok(self):
        fake = FakeGh()
        fake.effective_rules = [
            _pull_request_rule(),
            _status_checks_rule([("lanes", 42), "codex", "zizmor"]),
            {"type": "required_linear_history", "parameters": {}},
            {"type": "non_fast_forward", "parameters": {}},
            {"type": "deletion", "parameters": {}},
        ]
        fake.check_runs = {fake.default_head_sha: [("lanes", 42), "codex", "zizmor"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] every required check has reported", out)

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

    def test_renamed_master_is_not_reported_as_an_existing_branch(self):
        # A repository renamed master -> main keeps a 301 from the old name,
        # and gh follows it, so the endpoint answers 200 with main's record.
        # Reading only the exit status turns every such rename into a
        # standing false gap -- on exactly the repositories that closed the
        # backdoor by renaming.
        fake = FakeGh()
        fake.master_redirect_name = "main"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] no branch literally named 'master'", out)
        self.assertNotIn("[GAP] a branch literally named 'master' exists", out)

    def test_master_branch_lookup_without_a_name_fails_closed(self):
        # 200 but nothing to identify the branch by is "could not tell",
        # which is neither a gap nor an ok.
        fake = FakeGh()
        fake.master_redirect_name = None
        fake.master_exists = True
        with patch.object(fake, "try_run", lambda args: (True, "\n")):
            code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not check whether", err)
        self.assertNotIn("branch literally named 'master'", out)

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
            [
                {"type": "required_linear_history", "parameters": {}},
                {"type": "non_fast_forward", "parameters": {}},
                {"type": "deletion", "parameters": {}},
            ],
        ]
        code, out, err = _run(fake, [REPO, "lanes"])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok]", out)


class DuplicateRulesetAuditTest(unittest.TestCase):
    def test_one_ruleset_under_the_managed_name_is_ok(self):
        fake = FakeGh()
        fake.ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = _covering_ruleset(name="main")
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] exactly one ruleset is named 'main'", out)

    def test_no_ruleset_of_that_name_is_not_reported_as_exactly_one(self):
        # None and exactly one are different answers, and the version that
        # only saw the extras could not tell them apart -- so a repository
        # protected solely by an inherited or differently-named ruleset
        # got a false "exactly one ruleset is named 'main'" (Codex review,
        # mikelward/repo#33).
        fake = FakeGh()
        fake.ruleset_ids = []
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] no ruleset is named 'main'", out)
        self.assertNotIn("exactly one ruleset", out)

    def test_an_inherited_ruleset_of_the_same_name_counts_too(self):
        # An org- or enterprise-level ruleset named `main` aggregates with
        # the repository's own, so counting only what the repository owns
        # would report "just the one" over two that both apply (Codex
        # review, mikelward/repo#33). `repo setup` asks the opposite
        # question -- it can only write a ruleset the repository owns.
        fake = FakeGh()
        fake.ruleset_ids = ["1", "77"]
        for rid in ("1", "77"):
            fake.ruleset_objects[rid] = _covering_ruleset(name="main")
            fake.ruleset_objects[rid]["id"] = int(rid)
        fake.repo_owned_ruleset_ids = ["1"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[CHECK] more than one ruleset is named 'main'", out)
        self.assertIn("ids 1, 77", out)
        self.assertIn("inherited from the organization and not changeable here: 77", out)
        self.assertIn("`repo setup` writes 1", out)

    def test_an_inherited_ruleset_that_sorts_first_is_not_named_as_setups_target(self):
        # With parents included the inherited ruleset comes FIRST whenever
        # the organization's predates the repository's, which is the
        # common case -- so "setup writes the first" would name the one
        # setup never touches. Setup's target is the first the repository
        # OWNS, read from its own lookup (Codex review, mikelward/repo#33).
        fake = FakeGh()
        fake.ruleset_ids = ["3", "9"]
        for rid in ("3", "9"):
            fake.ruleset_objects[rid] = _covering_ruleset(name="main")
            fake.ruleset_objects[rid]["id"] = int(rid)
        fake.repo_owned_ruleset_ids = ["9"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("inherited from the organization and not changeable here: 3", out)
        self.assertIn("`repo setup` writes 9", out)
        self.assertNotIn("writes 3", out)

    def test_only_inherited_rulesets_say_setup_writes_none(self):
        fake = FakeGh()
        fake.ruleset_ids = ["3", "4"]
        for rid in ("3", "4"):
            fake.ruleset_objects[rid] = _covering_ruleset(name="main")
            fake.ruleset_objects[rid]["id"] = int(rid)
        fake.repo_owned_ruleset_ids = []
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("`repo setup` writes none of them", out)

    def test_a_second_one_under_the_same_name_is_a_check_not_a_gap(self):
        # Nothing here resolves it -- `repo setup` writes the first and
        # says it is leaving the other alone, because what to do when the
        # two disagree is undecided. So it neither passes nor fails the
        # audit: a [FIX] would claim setup closes it, and a [GAP] would
        # fail every repository over a state no command can fix.
        fake = FakeGh()
        fake.ruleset_ids = ["1", "2"]
        for rid in ("1", "2"):
            fake.ruleset_objects[rid] = _covering_ruleset(name="main")
            fake.ruleset_objects[rid]["id"] = int(rid)
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[CHECK] more than one ruleset is named 'main'", out)
        self.assertIn("ids 1, 2", out)
        self.assertIn("`repo setup` writes 1 and leaves the rest alone", out)
        self.assertNotIn("inherited", out)


class LegacyRulesetAuditTest(unittest.TestCase):
    def test_no_legacy_ruleset_is_ok(self):
        code, out, err = _run(FakeGh(), [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] no ruleset left under a name this tool used before 'main'", out)

    def test_a_legacy_named_ruleset_is_a_fix_naming_setup(self):
        # The read-only half of what `repo setup` already reports: asking
        # "which repositories still have two?" should not mean running a
        # command that writes against each of them.
        fake = FakeGh()
        fake.ruleset_ids = ["9"]
        fake.ruleset_objects["9"] = _covering_ruleset(name="merge gates")
        fake.ruleset_objects["9"]["id"] = 9
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] 'merge gates' (id 9) is a ruleset name this tool used before", out)
        self.assertIn("`repo setup` adopts it", out)

    def test_every_ruleset_sharing_a_legacy_name_is_reported(self):
        # A ruleset's name is not unique within a repository, so reporting
        # only the first would answer "which repositories still have a
        # duplicate?" with a number that is too low (Codex review,
        # mikelward/repo#31).
        fake = FakeGh()
        fake.ruleset_ids = ["9", "10"]
        for rid in ("9", "10"):
            fake.ruleset_objects[rid] = _covering_ruleset(name="merge gates")
            fake.ruleset_objects[rid]["id"] = int(rid)
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] 'merge gates' (id 9)", out)
        self.assertIn("[FIX] 'merge gates' (id 10)", out)

    def test_a_failed_lookup_exits_1_rather_than_reading_as_none(self):
        fake = FakeGh()
        fake.rulesets_lookup_fails = "gh: HTTP 500: boom\n"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertNotIn("no ruleset left under a name", out)


class AutoMergeAuditTest(unittest.TestCase):
    def test_auto_merge_allowed_is_ok(self):
        code, out, err = _run(FakeGh(), [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] auto-merge is allowed on the repository", out)

    def test_auto_merge_off_is_a_fix_naming_setup(self):
        fake = FakeGh()
        fake.allow_auto_merge = "false"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] auto-merge is not allowed on the repository -- the weekly batch cannot arm it "
            f"on its pull requests; `repo setup {REPO}` enables it",
            out,
        )

    def test_a_failed_auto_merge_read_exits_1(self):
        fake = FakeGh()
        fake.auto_merge_fails = "gh: HTTP 500: boom\n"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not read whether {REPO} allows auto-merge:", err)
        self.assertNotIn("auto-merge is", out)


class DeleteBranchOnMergeAuditTest(unittest.TestCase):
    def test_delete_branch_on_merge_allowed_is_ok(self):
        code, out, err = _run(FakeGh(), [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[ok] a merged pull request's head branch is deleted automatically", out
        )

    def test_delete_branch_on_merge_off_is_a_fix_naming_setup(self):
        fake = FakeGh()
        fake.delete_branch_on_merge = "false"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            f"[FIX] {REPO} does not delete a merged pull request's head branch automatically -- "
            f"branches accumulate until `repo cleanup` sweeps them; `repo setup {REPO}` enables it",
            out,
        )

    def test_a_failed_delete_branch_on_merge_read_exits_1(self):
        fake = FakeGh()
        fake.delete_branch_on_merge_fails = "gh: HTTP 500: boom\n"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not read whether {REPO} deletes branches on merge:", err)
        self.assertNotIn("deletes branches on merge", out)


class LanesCredentialAuditTest(unittest.TestCase):
    """The lanes App pair, audited like the other fleet credentials, plus
    the environment it lives in: the App publishes the required status, so
    an environment any branch can reach hands a same-repo pull request's
    push-triggered run the same reach."""

    PUBLISHER = (
        "jobs:\n  init:\n    runs-on: ubuntu-latest\n    environment: lanes\n"
        "    steps:\n      - uses: mikelward/lanes@main\n        with:\n"
        "          mode: init\n          app-id: ${{ secrets.LANES_APP_ID }}\n"
        "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n"
    )
    PAIR = ["LANES_APP_ID", "LANES_APP_PRIVATE_KEY"]
    MOVE = f"`repo setup --credential LANES_APP_ID=PATH --credential LANES_APP_PRIVATE_KEY=PATH {REPO}`"

    def _publisher(self, text=PUBLISHER):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": text}
        return fake

    def test_the_pair_in_a_restricted_environment_is_ok(self):
        fake = self._publisher()
        fake.environments = {"lanes": self.PAIR}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] lanes: the App credential lives in the 'lanes' environment", out)
        self.assertIn("[ok] lanes: environment 'lanes' admits only the trusted base branch", out)
        self.assertNotIn("[FIX]", out)
        # "Protected branches only" is not the guarantee: every branch while
        # no branch-protection rule exists, and a ruleset is not one.
        fake.env_policies = {"lanes": "protected"}
        code, out, err = _run(fake, [REPO])
        self.assertIn(
            "[FIX] lanes: environment 'lanes' admits protected branches, which is every branch while no "
            "branch-protection rule exists (a ruleset is not one) and every protected branch otherwise -- "
            "restrict it to 'main' in the environment's settings",
            out,
        )
        self.assertNotIn("[ok] lanes: environment", out)

    def test_a_repository_copy_is_a_fix_naming_the_move(self):
        fake = self._publisher()
        fake.repo_secrets = list(self.PAIR)
        fake.environments = {"lanes": self.PAIR}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] lanes: LANES_APP_ID, LANES_APP_PRIVATE_KEY is a repository secret, which reaches every job "
            "of every workflow -- a same-repo pull request's push-triggered run included, which is the hole "
            f"trusted publishing exists to close -- {self.MOVE} moves it into the 'lanes' environment",
            out,
        )
        self.assertNotIn("holds no App credential", out)
        self.assertNotIn("[ok] lanes: the App credential", out)
        # A fleet credential, so not also listed for review by hand.
        self.assertNotIn("[CHECK] repository secrets", out)

    def test_no_credential_anywhere_is_a_fix_naming_the_set(self):
        fake = self._publisher()
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] lanes: environment 'lanes' holds no App credential (LANES_APP_ID and LANES_APP_PRIVATE_KEY) "
            f"-- the workflow cannot publish the `lanes` status the ruleset requires; {self.MOVE} sets one",
            out,
        )
        self.assertNotIn("environment 'lanes' can be reached", out)
        # Half a pair at repository level is named as half.
        fake.repo_secrets = ["LANES_APP_ID"]
        code, out, err = _run(fake, [REPO])
        self.assertIn(", and LANES_APP_ID is only half of the App pair --", out)

    def test_a_publishing_job_without_the_environment_is_a_fix(self):
        fake = self._publisher(self.PUBLISHER.replace("    environment: lanes\n", ""))
        fake.environments = {"lanes": self.PAIR}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] lanes: ci publishes the lanes status as the App from a job that does not declare "
            "`environment: lanes`, so a credential in that environment never reaches it -- `init` fails "
            "outright and `gate` silently falls back to the ambient check-run; declare the environment on "
            "every job that takes `app-id`",
            out,
        )
        self.assertNotIn("[ok] lanes: the App credential", out)

    def test_a_branch_copy_without_the_environment_is_a_check_not_a_fix(self):
        # The default branch's publisher declares the environment; a copy
        # on a branch does not, and a branch copy cannot reach a restricted
        # environment either way -- it is named, and it is not the [FIX]
        # that holds the move back (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.branch_workflows = {"feature": {"ci.yml": self.PUBLISHER.replace("    environment: lanes\n", "")}}
        fake.environments = {"lanes": self.PAIR}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[CHECK] lanes: ci on feature publishes the lanes status as the App from a job that does not "
            "declare `environment: lanes`; a branch copy runs from its branch, which the restricted "
            "environment shuts out anyway, so it loses the credential when the repository copy moves",
            out,
        )
        self.assertNotIn("[FIX] lanes: ci on feature", out)
        self.assertIn("[ok] lanes: the App credential lives in the 'lanes' environment", out)

    def test_a_publisher_only_on_a_branch_is_a_check_not_ok(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.branch_workflows = {"feature": {"ci.yml": self.PUBLISHER}}
        fake.environments = {"lanes": self.PAIR}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[CHECK] lanes: no workflow on the default branch publishes the lanes status as the App; "
            "ci on feature does from a branch, which reaches the environment once merged",
            out,
        )
        self.assertNotIn("[ok] lanes: the App credential", out)
        self.assertNotIn("no workflow here publishes", out)

    def test_half_the_pair_handed_to_the_action_is_a_fix(self):
        fake = self._publisher(self.PUBLISHER.replace("          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n", ""))
        fake.environments = {"lanes": self.PAIR}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] lanes: ci hands mikelward/lanes one of `app-id` and `app-private-key` without the other, "
            "so the step cannot authenticate as the App and publishes nothing; hand it both",
            out,
        )
        # Not unused -- the pair is plainly meant for it -- and not healthy.
        self.assertNotIn("no workflow here publishes", out)
        self.assertNotIn("[ok] lanes: the App credential", out)

    def test_an_open_environment_is_a_fix_naming_setup(self):
        fake = self._publisher()
        fake.environments = {"lanes": self.PAIR}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] lanes: the App credential lives in the 'lanes' environment", out)
        self.assertIn(
            "[FIX] lanes: environment 'lanes' can be reached from any branch -- a same-repo pull request's "
            "push-triggered workflow reads the App credential exactly as the trusted jobs do; "
            f"`repo setup {REPO}` restricts it to 'main'",
            out,
        )

    def test_setup_is_not_promised_where_its_planner_stops_first(self):
        # The lanes planner reports and returns on a workflow finding,
        # before any restriction -- so naming the command flatly told a
        # reader to run something that leaves the environment open (Codex,
        # mikelward/repo#36).
        fake = self._publisher(self.PUBLISHER.replace("    environment: lanes\n", ""))
        fake.environments = {"lanes": self.PAIR}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            f"`repo setup {REPO}` restricts it to 'main' -- but not while ci publishes from a job "
            f"that does not declare `environment: lanes`: the command reports that and stops first",
            out,
        )
        # The credential recommendations beside it carry the same caveat,
        # since the planner stops before the move and the write too
        # (Codex, mikelward/repo#36).
        moving = self._publisher(self.PUBLISHER.replace("    environment: lanes\n", ""))
        moving.environments = {"lanes": self.PAIR}
        moving.repo_secrets = list(self.PAIR)
        code, out, err = _run(moving, [REPO])
        self.assertEqual(code, 0, err)
        recommendations = [line for line in out.splitlines() if "`repo setup" in line]
        # The move, the environment's policy, and nothing silently missing.
        self.assertTrue(any("moves it into the 'lanes' environment" in line for line in recommendations))
        self.assertTrue(any("restricts it to 'main'" in line for line in recommendations))
        for line in recommendations:
            self.assertIn("but not while", line, line)
        # Half a pair stops it the same way, and so does an unreadable
        # mention -- each is its own early return.
        half = self._publisher(
            self.PUBLISHER.replace(
                "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n", ""
            )
        )
        half.environments = {"lanes": self.PAIR}
        code, out, err = _run(half, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("but not while ci hands", out)

    def test_a_pair_held_nowhere_stops_the_plain_setup_too(self):
        # A pair held in neither scope makes the move report and the
        # planner return before the policy, so `repo setup <repo>` leaves
        # the environment open -- and the recommendation said flatly that
        # it restricts it (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.environments = {"lanes": set()}
        fake.repo_secrets = []
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            f"`repo setup {REPO}` restricts it to 'main' -- but not while neither the repository "
            f"nor the 'lanes' environment holds a usable LANES_APP_ID and LANES_APP_PRIVATE_KEY "
            f"for it to place: the command reports that and stops first",
            out,
        )
        # But NOT the recommendation whose own command supplies the pair:
        # qualifying that one with the condition it resolves would read as
        # a contradiction.
        supplying = [line for line in out.splitlines() if "sets one" in line]
        self.assertEqual(len(supplying), 1, out)
        self.assertNotIn("but not while", supplying[0])

    def test_a_policy_someone_set_stops_the_move_commands_too(self):
        # `repo setup` rewrites nobody's policy, so it drops the whole move
        # rather than place the pair behind an environment it cannot vouch
        # for -- and naming the command flatly advertised one that exits
        # nonzero having moved nothing (Codex, mikelward/repo#36). Unlike
        # the other blockers this one stops the `--credential` commands as
        # well: supplying the pair does not settle the policy.
        fake = self._publisher()
        fake.environments = {"lanes": set()}
        fake.env_policies = {"lanes": ["release/*"]}
        fake.repo_secrets = list(self.PAIR)
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        moving = [line for line in out.splitlines() if "moves it into" in line]
        self.assertEqual(len(moving), 1, out)
        self.assertIn(
            "but not while environment 'lanes' is set to a policy `repo setup` does not rewrite",
            moving[0],
        )
        # The policy's own line already tells the reader to set it by hand
        # and says the command rewrites nobody's, so it needs no caveat --
        # it promises `repo setup` nothing.
        policy = [line for line in out.splitlines() if "in the environment's settings" in line]
        self.assertEqual(len(policy), 1, out)
        self.assertNotIn("but not while", policy[0])

    def test_a_restriction_left_half_done_names_setup(self):
        fake = self._publisher()
        fake.environments = {"lanes": self.PAIR}
        fake.env_policies = {"lanes": []}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] lanes: environment 'lanes' admits no branch at all -- custom-policy mode with no branch "
            "named, the state a restriction leaves when its second write fails, so no job reaches the "
            f"credential; `repo setup {REPO}` completes it with 'main'",
            out,
        )

    def test_a_policy_naming_more_than_the_default_branch_is_fixed_by_hand(self):
        fake = self._publisher()
        fake.environments = {"lanes": self.PAIR}
        fake.env_policies = {"lanes": ["main", "tag:v*"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] lanes: environment 'lanes' is restricted to 'main', 'tag:v*', not to 'main' alone -- "
            "restrict it to 'main' in the environment's settings (`repo setup` rewrites no policy someone set)",
            out,
        )
        fake.default_branch = "trunk"
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, [REPO])
        self.assertIn("is restricted to 'main', not to 'trunk' alone", out)

    def test_a_credential_nothing_publishes_with_is_stale(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        # The ambient pattern hands the action no credential.
        fake.workflow_texts = {
            "ci.yml": "jobs:\n  classify:\n    steps:\n      - uses: mikelward/lanes@main\n        with:\n          mode: classify\n"
        }
        fake.environments = {"lanes": ["LANES_APP_PRIVATE_KEY"]}
        fake.repo_secrets = ["LANES_APP_ID"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] LANES_APP_ID as a repository secret and LANES_APP_PRIVATE_KEY in the 'lanes' environment, "
            "but no workflow here publishes the lanes status as the App (a mikelward/lanes step taking "
            f"`app-id`) -- `repo setup {REPO}` deletes it",
            out,
        )
        self.assertNotIn("moves it into", out)
        # The environment's policy is not read for an unused credential.
        self.assertNotIn("environment 'lanes' can be reached", out)
        # Nothing anywhere and nothing publishing: nothing to say.
        fake.environments = {}
        fake.repo_secrets = []
        code, out, err = _run(fake, [REPO])
        self.assertNotIn("lanes:", out)

    def test_a_shape_the_reader_cannot_resolve_is_cannot_tell(self):
        fake = self._publisher("jobs: {init: {steps: [{uses: mikelward/lanes@main, with: {app-id: x}}]}}\n")
        fake.environments = {"lanes": self.PAIR}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] lanes: ci mentions mikelward/lanes in a shape this cannot read as a step -- whether the "
            "App credential is used there cannot be told; write it as a step-level `uses:` (`repo setup` "
            "moves or deletes nothing of its until then)",
            out,
        )
        self.assertNotIn("[ok] lanes", out)
        self.assertNotIn("deletes it", out)

    def test_a_failed_environment_read_exits_1(self):
        fake = self._publisher()
        fake.environments = {"lanes": self.PAIR}
        fake.env_read_fails = {"lanes"}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not read {REPO}'s 'lanes' environment:", err)
        self.assertNotIn("environment 'lanes' can be reached", out)


class SecretsAuditTest(unittest.TestCase):
    """Where the fleet credentials live, and what else sits repository-wide.

    A placement finding is [FIX], not [GAP]: `repo setup` closes it, and
    it does not count toward the exit status until the fleet has been
    through setup (TODO.md). So the exit code is asserted 0 wherever a
    [FIX] is the only finding, on purpose -- flipping that is the
    promotion, and it should have to change these tests."""

    def _hub_repo(self, hub="gradle-update"):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", f"{hub}.yml"]
        return fake

    def test_a_repository_with_no_secrets_and_no_workflows_is_clean(self):
        code, out, err = _run(FakeGh(), [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] no repository-level secrets", out)
        self.assertNotIn("[CHECK] repository secrets", out)
        self.assertNotIn("[FIX]", out)

    def test_a_batch_credential_kept_as_a_repository_secret_is_a_fix_naming_the_move(self):
        fake = self._hub_repo()
        fake.repo_secrets = ["GRADLE_UPDATE_PAT"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] gradle-update: GRADLE_UPDATE_PAT is a repository secret", out)
        self.assertIn(
            f"`repo setup --credential GRADLE_UPDATE_PAT=PATH {REPO}` moves it into the 'gradle-update' environment",
            out,
        )
        # Not double-reported as "no credential in the environment": the
        # move is the one fix, and the repository copy is what still
        # reaches the update job even once the environment holds one.
        self.assertNotIn("holds no batch credential", out)
        self.assertNotIn("[GAP]", out)

    def test_a_repository_copy_is_still_a_fix_once_the_environment_holds_one(self):
        fake = self._hub_repo()
        fake.repo_secrets = ["GRADLE_UPDATE_PAT"]
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] gradle-update: GRADLE_UPDATE_PAT is a repository secret", out)
        self.assertNotIn("[ok] gradle-update:", out)

    def test_a_caller_naming_its_secrets_is_a_fix_even_with_the_credential_scoped(self):
        # `repo setup` calls this NOT FIXED: an environment credential never
        # reaches a caller that names its secrets. The audit reads the
        # caller the same way, so it cannot print [ok] over it.
        fake = self._hub_repo()
        fake.workflow_texts = {
            "gradle-update.yml": "jobs:\n  update:\n    uses: mikelward/gradle-update/.github/workflows/gradle-update.yml@main\n    secrets:\n      token: ${{ secrets.GRADLE_UPDATE_PAT }}\n"
        }
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] gradle-update: gradle-update passes its secrets by name, so a credential in the "
            "'gradle-update' environment never reaches the batch -- convert the caller to `secrets: inherit`",
            out,
        )
        self.assertNotIn("[ok] gradle-update:", out)
        # A `gradle-update.yml` that calls nothing is no caller at all: the
        # batch does not run here, so its scoped credential is the stale
        # one -- the same reading `repo setup` deletes it on.
        fake.workflow_texts = {"gradle-update.yml": "name: x\non: push\njobs: {}\n"}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] batch credential(s) for a batch this repository does not run, in the "
            "'gradle-update' environment: GRADLE_UPDATE_PAT",
            out,
        )
        self.assertNotIn("[ok] gradle-update:", out)

    def test_a_failed_caller_read_exits_1(self):
        fake = self._hub_repo()
        fake.workflow_text_fails = {"gradle-update.yml"}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        # The read names the default branch, since `workflow_texts`
        # pins it rather than letting the API resolve "the default".
        self.assertIn("could not read owner/repo's .github/workflows/gradle-update.yml on branch main:", err)

    def test_a_caller_with_the_yaml_extension_is_the_batch_too(self):
        # GitHub runs `.yaml` workflows as readily as `.yml`; a batch missed
        # for its extension would have its real credential reported stale.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "gradle-update.yaml"]
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] gradle-update: the batch credential lives in the 'gradle-update' environment", out)
        self.assertNotIn("does not run", out)

    def test_every_caller_file_of_a_batch_is_read(self):
        # `<hub>.yml` and `<hub>.yaml` can coexist, and GitHub runs both; the
        # one that names its secrets is the finding however the other
        # reads, and the [ok] is withheld.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "gradle-update.yml", "gradle-update.yaml"]
        fake.workflow_texts = {
            "gradle-update.yaml": (
                "jobs:\n  update:\n"
                "    uses: mikelward/gradle-update/.github/workflows/gradle-update.yml@main\n"
                "    secrets:\n      token: ${{ secrets.GRADLE_UPDATE_PAT }}\n"
            )
        }
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] gradle-update: gradle-update passes its secrets by name", out)
        self.assertNotIn("[ok] gradle-update:", out)

    def test_a_caller_under_another_name_is_read_too(self):
        # GitHub runs a workflow whatever it is named, so a second caller
        # naming its secrets is the finding even though `<hub>.yml` inherits.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "gradle-update.yml", "weekly.yml"]
        fake.workflow_texts = {
            "weekly.yml": (
                "jobs:\n  update:\n"
                "    uses: mikelward/gradle-update/.github/workflows/gradle-update.yml@main\n"
                "    secrets:\n      token: ${{ secrets.GRADLE_UPDATE_PAT }}\n"
            )
        }
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] gradle-update: weekly passes its secrets by name", out)
        self.assertNotIn("[ok] gradle-update:", out)

    def test_a_mention_no_caller_resolves_is_cannot_tell(self):
        # Neither "runs" nor "does not run": the credential is not stale,
        # and the finding names the file to rewrite.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "batch.yml"]
        fake.workflow_texts = {
            "batch.yml": "jobs: {update: {uses: mikelward/gradle-update/.github/workflows/gradle-update.yml@main}}\n"
        }
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] gradle-update: batch mentions mikelward/gradle-update/ in a shape this cannot read as a caller",
            out,
        )
        self.assertNotIn("does not run", out)

    SYNC = (
        "jobs:\n  sync:\n"
        "    uses: mikelward/ci-commit-artifact/.github/workflows/commit-artifact.yml@main\n"
        "    secrets: inherit\n"
    )

    def test_a_hub_with_an_unread_mention_beside_a_caller_is_cannot_tell(self):
        # `repo setup` refuses every move for such a hub, so neither an [ok]
        # nor a move recommendation may contradict it.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "gradle-update.yml", "batch.yml"]
        fake.workflow_texts = {
            "batch.yml": "jobs: {update: {uses: mikelward/gradle-update/.github/workflows/gradle-update.yml@main}}\n"
        }
        fake.repo_secrets = ["GRADLE_UPDATE_PAT"]
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] gradle-update: batch mentions mikelward/gradle-update/", out)
        self.assertNotIn("[ok] gradle-update:", out)
        self.assertNotIn("moves it into the 'gradle-update' environment", out)
        self.assertNotIn("does not run", out)

    def test_the_commit_back_token_is_audited_like_a_batch(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": self.SYNC}
        fake.environments = {"ci-commit-artifact": ["CI_COMMIT_ARTIFACT_TOKEN"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] ci-commit-artifact: the token lives in the 'ci-commit-artifact' environment", out)
        # Behind a caller naming its secrets the environment copy is idle.
        fake.workflow_texts = {
            "ci.yml": self.SYNC.replace(
                "secrets: inherit", "secrets:\n      push-token: ${{ secrets.CI_COMMIT_ARTIFACT_TOKEN }}"
            )
        }
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] ci-commit-artifact: ci passes its secrets by name", out)
        self.assertNotIn("[ok] ci-commit-artifact:", out)
        # Nowhere at all: the commit-back pushes as GITHUB_TOKEN.
        fake.workflow_texts = {"ci.yml": self.SYNC}
        fake.environments = {}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] ci-commit-artifact: environment 'ci-commit-artifact' holds no CI_COMMIT_ARTIFACT_TOKEN", out
        )
        # Nothing calls the workflow: the token is stale wherever it sits.
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.environments = {"ci-commit-artifact": ["CI_COMMIT_ARTIFACT_TOKEN"]}
        fake.repo_secrets = ["CI_COMMIT_ARTIFACT_TOKEN"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] CI_COMMIT_ARTIFACT_TOKEN as a repository secret and in the 'ci-commit-artifact' "
            "environment, but no workflow here calls mikelward/ci-commit-artifact",
            out,
        )
        self.assertNotIn("moves it into", out)

    def test_a_caller_on_another_branch_is_read_too(self):
        # A push to `feature` runs its workflows from `feature`; its caller
        # naming its secrets is the finding, and the hub is not stale.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.branch_workflows = {
            "feature": {
                "weekly.yml": (
                    "jobs:\n  update:\n"
                    "    uses: mikelward/gradle-update/.github/workflows/gradle-update.yml@main\n"
                    "    secrets:\n      token: ${{ secrets.GRADLE_UPDATE_PAT }}\n"
                )
            }
        }
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] gradle-update: weekly on feature passes its secrets by name", out)
        self.assertNotIn("does not run", out)

    def test_a_credential_in_the_environment_alone_is_ok(self):
        fake = self._hub_repo()
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] gradle-update: the batch credential lives in the 'gradle-update' environment", out)
        self.assertNotIn("[FIX]", out)

    def test_the_app_pair_in_the_environment_counts_as_the_credential(self):
        fake = self._hub_repo("rust-update")
        fake.environments = {"rust-update": ["RUST_UPDATE_APP_ID", "RUST_UPDATE_APP_PRIVATE_KEY"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] rust-update: the batch credential lives in the 'rust-update' environment", out)
        self.assertNotIn("[FIX]", out)

    def test_half_an_app_pair_in_the_environment_is_no_credential(self):
        fake = self._hub_repo("rust-update")
        fake.environments = {"rust-update": ["RUST_UPDATE_APP_ID"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] rust-update: environment 'rust-update' holds no batch credential", out)

    def test_an_environment_listed_in_another_case_is_the_hub_environment(self):
        # GitHub environment names are case-insensitive, so `Gradle-Update`
        # is the hub's environment: read under the name GitHub lists, and
        # its credential counts.
        fake = self._hub_repo()
        fake.environments = {"Gradle-Update": ["GRADLE_UPDATE_PAT"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] gradle-update: the batch credential lives in the 'gradle-update' environment", out)
        self.assertTrue(any("/environments/Gradle-Update/secrets" in " ".join(c) for c in fake.calls))

    def test_half_an_app_pair_at_repository_level_is_both_a_move_and_a_missing_credential(self):
        # The lone half has to move like any repository copy, but moving it
        # still leaves the batch with nothing that opens a pull request --
        # unlike a whole credential at repository level, which the move
        # alone fixes -- so both findings are reported.
        fake = self._hub_repo("rust-update")
        fake.repo_secrets = ["RUST_UPDATE_APP_ID"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] rust-update: RUST_UPDATE_APP_ID is a repository secret", out)
        self.assertIn(
            "[FIX] rust-update: environment 'rust-update' holds no batch credential (RUST_UPDATE_PAT, or RUST_UPDATE_APP_ID and RUST_UPDATE_APP_PRIVATE_KEY), and RUST_UPDATE_APP_ID is only half of the App pair -- the batch opens its pull requests as GITHUB_TOKEN; "
            f"`repo setup --credential RUST_UPDATE_PAT=PATH {REPO}` sets one",
            out,
        )

    def test_an_app_pair_split_across_the_scopes_is_still_a_credential(self):
        # One half at repository level, the other in the environment: the
        # called workflow receives both (the first through `inherit`, the
        # second from its environment), so the batch has a credential. The
        # repository half still has to move; the missing-credential finding
        # would be false.
        fake = self._hub_repo("rust-update")
        fake.repo_secrets = ["RUST_UPDATE_APP_ID"]
        fake.environments = {"rust-update": ["RUST_UPDATE_APP_PRIVATE_KEY"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] rust-update: RUST_UPDATE_APP_ID is a repository secret", out)
        self.assertNotIn("holds no batch credential", out)

    def test_the_move_names_every_repository_copy(self):
        fake = self._hub_repo("rust-update")
        fake.repo_secrets = ["RUST_UPDATE_APP_ID", "RUST_UPDATE_APP_PRIVATE_KEY"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] rust-update: RUST_UPDATE_APP_ID, RUST_UPDATE_APP_PRIVATE_KEY is a repository secret",
            out,
        )
        self.assertIn(
            f"`repo setup --credential RUST_UPDATE_APP_ID=PATH --credential RUST_UPDATE_APP_PRIVATE_KEY=PATH {REPO}` moves it",
            out,
        )
        self.assertNotIn("holds no batch credential", out)

    def test_secret_names_are_matched_in_any_case(self):
        # GitHub secret names are case-insensitive, so a credential listed
        # in another spelling is still the batch credential: at repository
        # level it is still the copy that has to move, and in the
        # environment it still counts.
        fake = self._hub_repo()
        fake.repo_secrets = ["gradle_update_pat"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[FIX] gradle-update: GRADLE_UPDATE_PAT is a repository secret", out)
        self.assertNotIn("[CHECK] repository secrets", out)
        fake = self._hub_repo("rust-update")
        fake.environments = {"rust-update": ["rust_update_app_id", "Rust_Update_App_Private_Key"]}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] rust-update: the batch credential lives in the 'rust-update' environment", out)

    def test_a_consumer_with_no_credential_anywhere_is_a_fix(self):
        fake = self._hub_repo("npm-update")
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] npm-update: environment 'npm-update' holds no batch credential (NPM_UPDATE_PAT, or NPM_UPDATE_APP_ID and NPM_UPDATE_APP_PRIVATE_KEY) -- the batch opens its pull requests as GITHUB_TOKEN; "
            f"`repo setup --credential NPM_UPDATE_PAT=PATH {REPO}` sets one",
            out,
        )
        # Nothing was read from an environment that does not exist.
        self.assertFalse(any("/environments/npm-update/secrets" in " ".join(c) for c in fake.calls))

    def test_a_credential_for_a_batch_the_repository_does_not_run_is_stale(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.repo_secrets = ["NPM_UPDATE_PAT"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] batch credential(s) for a batch this repository does not run: NPM_UPDATE_PAT -- "
            f"`repo setup {REPO}` deletes them",
            out,
        )

    def test_a_credential_left_in_the_environment_of_a_batch_no_longer_run_is_stale(self):
        # The caller was removed but the credential stayed in the hub's
        # environment -- the place the audit steers it to. Nothing uses it,
        # so it is the same finding as a stale repository secret.
        fake = self._hub_repo()  # runs gradle-update only
        fake.environments = {
            "gradle-update": ["GRADLE_UPDATE_PAT"],
            "rust-update": ["RUST_UPDATE_PAT", "OTHER"],
        }
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] batch credential(s) for a batch this repository does not run, in the 'rust-update' environment: RUST_UPDATE_PAT -- "
            f"`repo setup {REPO}` deletes them",
            out,
        )
        # The environment of a batch that IS run, and an unrelated secret
        # in the stale one, are not reported as stale.
        stale = [line for line in out.splitlines() if "does not run" in line]
        self.assertEqual(len(stale), 1, out)
        self.assertNotIn("gradle-update", stale[0])
        self.assertNotIn("OTHER", stale[0])
        self.assertIn("[ok] gradle-update: the batch credential lives in the 'gradle-update' environment", out)

    def test_the_commit_back_token_at_repository_level_is_a_fix(self):
        fake = self._hub_repo()
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        fake.repo_secrets = ["CI_COMMIT_ARTIFACT_TOKEN"]
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": self.SYNC}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "[FIX] CI_COMMIT_ARTIFACT_TOKEN is a repository secret, which reaches every job of every workflow -- "
            f"`repo setup --credential CI_COMMIT_ARTIFACT_TOKEN=PATH {REPO}` moves it into the 'ci-commit-artifact' environment",
            out,
        )
        # A fleet credential, so not also listed for review by hand.
        self.assertNotIn("[CHECK] repository secrets", out)
        self.assertIn("[ok] no repository-level secrets beyond the fleet credentials reported above", out)

    def test_other_repository_secrets_are_listed_for_review_not_flagged(self):
        fake = self._hub_repo()
        fake.environments = {"gradle-update": ["GRADLE_UPDATE_PAT"]}
        fake.repo_secrets = ["RELEASE_KEYSTORE_BASE64", "VERCEL_TOKEN"]
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[CHECK] repository secrets", out)
        self.assertIn("    RELEASE_KEYSTORE_BASE64\n    VERCEL_TOKEN\n", out)
        self.assertNotIn("[ok] no repository-level secrets", out)
        self.assertNotIn("[FIX]", out)

    def test_a_repository_holding_only_fleet_credentials_says_so(self):
        fake = self._hub_repo()
        fake.repo_secrets = ["GRADLE_UPDATE_PAT"]
        code, out, err = _run(fake, [REPO])
        self.assertIn("[ok] no repository-level secrets beyond the fleet credentials reported above", out)

    def test_a_failed_secrets_read_exits_1_and_reports_no_finding(self):
        fake = FakeGh()
        fake.repo_secrets_fails = "gh: HTTP 403: Resource not accessible\n"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not list owner/repo's repository secrets:", err)
        tail = out.split("[ok] no branch literally named 'master'")[-1]
        self.assertNotIn("[GAP]", tail)
        self.assertNotIn("[FIX]", tail)

    def test_a_failed_environments_read_exits_1(self):
        fake = FakeGh()
        fake.environments_fails = "gh: HTTP 500: boom\n"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not list owner/repo's environments:", err)

    def test_a_failed_environment_secrets_read_exits_1(self):
        fake = self._hub_repo()
        fake.environments = {"gradle-update": []}
        fake.env_secrets_fails = {"gradle-update"}
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not list owner/repo's 'gradle-update' environment secrets:", err)

    def test_a_hidden_workflows_directory_fails_closed(self):
        # A fine-grained token without Contents access gets the same 404 an
        # absent directory does. Taken as "no batch", the audit would skip
        # every credential check and tell the operator to delete the real
        # credentials as stale; the root listing failing too is the tell.
        fake = FakeGh()
        fake.repo_secrets = ["GRADLE_UPDATE_PAT"]
        fake.root_contents_error = "gh: HTTP 404: Not Found\n"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not tell whether owner/repo has workflows", err)
        self.assertNotIn("does not run", out)

    def test_an_empty_repository_runs_no_batch(self):
        # No commits, so no workflows: GitHub's own message for the root
        # listing says so, and that is a plain answer rather than a hidden
        # directory.
        fake = FakeGh()
        fake.root_contents_error = "gh: This repository is empty. (HTTP 404)\n"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] no repository-level secrets", out)

    def test_a_non_404_workflows_listing_failure_exits_1(self):
        fake = FakeGh()
        fake.workflows_error = "gh: HTTP 500: boom\n"
        code, out, err = _run(fake, [REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not list owner/repo's workflows on branch main:", err)


if __name__ == "__main__":
    unittest.main()
