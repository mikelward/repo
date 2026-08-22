import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from repo_lib import apps, gh, rules
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
_ACTIONS_SECRETS_RE = re.compile(r"^repos/([^/]+/[^/]+)/actions/secrets$")
_ENV_SECRETS_RE = re.compile(r"^repos/([^/]+/[^/]+)/environments/([^/]+)/secrets$")
_ENV_ONE_RE = re.compile(r"^repos/([^/]+/[^/]+)/environments/([^/]+)$")
_USER_INSTALLATIONS_RE = re.compile(r"^user/installations$")
_INSTALL_REPOS_RE = re.compile(r"^user/installations/([^/]+)/repositories$")
_INSTALL_REPO_ONE_RE = re.compile(r"^user/installations/([^/]+)/repositories/([^/]+)$")
_INSTALL_SLUG_JQ_RE = re.compile(r'app_slug=="([^"]*)"')
_INSTALL_OWNER_JQ_RE = re.compile(r'== \("([^"]*)" \| ascii_downcase\)')

_OWNERSHIP_JQ = ".enforcement, (.rules[].type)"


def _parse_api_args(rest):
    """Returns (endpoint, method, jq) for a `gh api ...` argv tail (already
    past 'api'). Handles --paginate/--method/--jq/--input in any order, so
    a single parser serves both the ruleset endpoints (which never combine
    --method with --jq) and the newer secrets/App ones (some of which use
    --method with no --jq at all, e.g. the App-membership PUT)."""
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
        elif a == "--input":
            i += 2
        else:
            endpoint = a
            i += 1
    return endpoint, method, jq


class FakeGh:
    """Models one repository's GitHub state for repo_lib.gh.run/try_run/
    run_with_input, closely enough to exercise repo_lib.rules, apps, and
    the secrets_cmd functions setup_cmd reuses -- without a fake `gh`
    binary on PATH. Only the two --jq forms rules.py actually sends to a
    single ruleset id are distinguished (the ownership check vs. the scope
    fetch) -- there is no third, so matching on the ownership JQ string and
    falling back to "scope fetch" for any other is safe.
    """

    def __init__(self):
        self.calls = []
        # -- ruleset step state (unchanged) --
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
        # Set (with existing_ruleset_id_lookup_threshold below) to model
        # the name resolving to something else (None or a different id)
        # starting on some later by-name lookup -- simulating a rename or
        # swap somewhere in the sequence of by-name lookups a run makes.
        # There are, in order: #1 the --dry-run preview's own internal
        # lookup (also the one apply_ruleset now hands back directly via
        # report["fingerprint"] -- setup_cmd makes no second, separate
        # lookup of its own to capture "what was previewed", since that
        # had its own race -- see AGENTS.md/rules.py's own Codex-review
        # comments on report["fingerprint"]); #2 the real apply's own
        # initial lookup; #3 the real apply's own write-time fingerprint
        # recheck. Two different rename windows are worth telling apart,
        # hence the configurable threshold rather than a fixed "second" or
        # "third": threshold 2 simulates the rename having already
        # happened by the time the real apply starts (caught by
        # apply_ruleset's pre-write fingerprint comparison against an
        # EARLIER call's expected_fingerprint, since call #1's answer --
        # what the preview saw and reported -- won't match call #2's);
        # threshold 3 simulates it happening DURING the real apply's own
        # execution, between its own two internal lookups (caught by the
        # SAME comparison, just against this call's own near-top
        # fingerprint instead, since #1 and #2 would still agree).
        self.existing_ruleset_id_after_second_lookup = "__unset__"
        self.existing_ruleset_id_lookup_threshold = 2
        self.all_ruleset_ids = []
        self.ruleset_objects = {}
        # A single ruleset id's CONTENT changing partway through a run --
        # distinct from the id itself changing, above. Keyed by call count
        # of reads of that SPECIFIC id (not a global counter), so a swap
        # simulated via existing_ruleset_id_after_second_lookup and a
        # content change simulated here can be combined, or used alone,
        # without one's counter perturbing the other's threshold.
        self._ruleset_object_reads = {}
        self.ruleset_objects_after_change = {}  # rid -> replacement object
        self.ruleset_content_change_threshold = 1
        self.master_exists = False
        self.master_error = None
        self.puts = []
        self.posts = []
        self.fail_default_branch = False
        self.fail_allow_rebase = False

        # -- shared "does the repo itself exist" switch --
        # Controls repos/{repo} (.id and every other jq on it) AND
        # repos/{repo}/actions/secrets -- the same underlying repository,
        # so one switch for "this repo cannot be read at all".
        self.repo_missing = False
        self.repo_id = "999"

        # -- secrets step state --
        self.secret_names = set()  # repo-level secret names, at preview time
        self.secret_names_after_recheck = None  # None => same as secret_names
        self.env_secret_names = {}  # env -> set(names); absent env => doesn't exist yet
        self.env_secret_names_after_recheck = {}  # env -> set(); absent => same as env_secret_names
        self._list_calls = {}  # key (None or env name) -> call count, for the recheck race
        self.fail_secret_recheck = set()  # keys (None or env name) whose 2nd+ list call errors
        self.env_create_fails = set()  # env names whose creation PUT fails
        self.env_get_check_fails = set()  # env names whose existence GET fails non-404
        self.set_fails = set()  # secret names whose `secret set` fails
        self.written_secrets = []  # (name, repo, env, value)

        # -- App-installation step state --
        self.installations = []  # (slug, id, selection, account)
        self.install_members = {}  # install_id -> set(full_name)
        self.install_add_fails = set()  # install_ids whose membership PUT fails

    # -- repo_lib.gh.run/try_run/run_with_input replacements --------------

    def _secret_names_for(self, env):
        key = env
        self._list_calls[key] = self._list_calls.get(key, 0) + 1
        count = self._list_calls[key]
        if count >= 2 and key in self.fail_secret_recheck:
            raise gh.GhError("gh: simulated failure on revalidation\n")
        if count >= 2:
            if env is None and self.secret_names_after_recheck is not None:
                return self.secret_names_after_recheck
            if env is not None and env in self.env_secret_names_after_recheck:
                return self.env_secret_names_after_recheck[env]
        return self.secret_names if env is None else self.env_secret_names.get(env, set())

    def run(self, args):
        self.calls.append(list(args))
        assert args[0] == "api", args
        endpoint, method, jq = _parse_api_args(args[1:])

        m = _DEFAULT_BRANCH_RE.match(endpoint)
        if m and jq == ".default_branch":
            if self.fail_default_branch:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            return self.default_branch + "\n"
        if m and jq == ".allow_rebase_merge":
            if self.fail_allow_rebase:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            return self.allow_rebase + "\n"
        if m and jq == ".id" and method is None:
            if self.repo_missing:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            return self.repo_id + "\n"

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
            if (
                self._name_lookup_calls >= self.existing_ruleset_id_lookup_threshold
                and self.existing_ruleset_id_after_second_lookup != "__unset__"
            ):
                rid = self.existing_ruleset_id_after_second_lookup
            else:
                rid = self.existing_ruleset_id
            return f"{rid}\n" if rid else ""

        if _RULESETS_ALL_RE.match(endpoint):
            return "".join(f"{rid}\n" for rid in self.all_ruleset_ids)

        m = _RULESET_ONE_RE.match(endpoint)
        if m:
            rid = m.group(2)
            self._ruleset_object_reads[rid] = self._ruleset_object_reads.get(rid, 0) + 1
            if (
                self._ruleset_object_reads[rid] > self.ruleset_content_change_threshold
                and rid in self.ruleset_objects_after_change
            ):
                obj = self.ruleset_objects_after_change[rid]
            else:
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

        m = _ACTIONS_SECRETS_RE.match(endpoint)
        if m:
            if self.repo_missing:
                raise gh.GhError(f"gh: HTTP 404: Not Found (.../{endpoint})\n")
            return "".join(n + "\n" for n in sorted(self._secret_names_for(None)))

        m = _ENV_SECRETS_RE.match(endpoint)
        if m:
            env = m.group(2)
            if env not in self.env_secret_names:
                raise gh.GhError(f"gh: HTTP 404: Not Found (.../{endpoint})\n")
            return "".join(n + "\n" for n in sorted(self._secret_names_for(env)))

        m = _ENV_ONE_RE.match(endpoint)
        if m:
            env = m.group(2)
            if method == "PUT":
                if env in self.env_create_fails:
                    raise gh.GhError(f"gh: could not create environment '{env}'\n")
                self.env_secret_names.setdefault(env, set())
                return ""
            if env in self.env_get_check_fails:
                raise gh.GhError(
                    "gh: HTTP 403: Resource protected by organization SAML enforcement\n"
                )
            if env in self.env_secret_names:
                return ""
            raise gh.GhError(f"gh: HTTP 404: Not Found (.../{endpoint})\n")

        if _USER_INSTALLATIONS_RE.match(endpoint):
            slug_m = _INSTALL_SLUG_JQ_RE.search(jq or "")
            owner_m = _INSTALL_OWNER_JQ_RE.search(jq or "")
            slug = slug_m.group(1) if slug_m else ""
            owner = (owner_m.group(1) if owner_m else "").lower()
            rows = [
                f"{iid}\t{sel}"
                for (s, iid, sel, acct) in self.installations
                if s == slug and acct.lower() == owner
            ]
            return "".join(r + "\n" for r in rows)

        m = _INSTALL_REPOS_RE.match(endpoint)
        if m:
            iid = m.group(1)
            names = self.install_members.get(iid, set())
            return "".join(n + "\n" for n in sorted(names))

        m = _INSTALL_REPO_ONE_RE.match(endpoint)
        if m and method == "PUT":
            iid = m.group(1)
            if iid in self.install_add_fails:
                raise gh.GhError("gh: HTTP 403: Resource not accessible by integration\n")
            return ""

        raise AssertionError(f"unexpected endpoint: {endpoint} (method={method} jq={jq})")

    def try_run(self, args):
        try:
            return True, self.run(args)
        except gh.GhError as e:
            return False, e.stderr

    def run_with_input(self, args, input_bytes):
        self.calls.append(list(args))
        if args[:2] == ["secret", "set"]:
            name = args[2]
            repo = args[args.index("--repo") + 1]
            env = args[args.index("--env") + 1] if "--env" in args else None
            if name in self.set_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            self.written_secrets.append((name, repo, env, input_bytes))
            return b""
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


def _secret_file(tmpdir, name, content=b"sekrit"):
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _run(fake, argv, isatty=False):
    """Runs `repo setup <argv>` against `fake`, returning (exit_code,
    stdout, stderr)."""
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

    def test_previewed_ruleset_id_comes_from_the_preview_itself_not_a_second_lookup(self):
        # Codex review: setup_cmd.py's real apply reads expected_fingerprint
        # directly from the preview call's own report["fingerprint"] -- the
        # exact lookup that built the plan just shown -- never a SEPARATE
        # lookup made afterward to re-derive "what was previewed". A
        # separate lookup has its own race: a swap landing between the
        # preview's internal lookup and that second call would go
        # undetected, since the second call would simply see the new
        # ruleset and report ITS id as "what was previewed" instead of the
        # one actually shown and confirmed. Asserted directly by counting
        # by-name lookups: a plain, unchanged run (needing a real update,
        # so it reaches every lookup a run can make) does exactly three --
        # the preview's own, the real apply's own first lookup, and its
        # write-time fingerprint recheck. A fourth would mean a separate
        # capture call crept back in.
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
        self.assertEqual(code, 0, err)
        self.assertEqual(fake._name_lookup_calls, 3)

    def test_refuses_to_write_when_the_ruleset_was_swapped_before_the_real_apply_started(self):
        # Codex review: the ruleset named 'merge gates' (id 7) was what the
        # --dry-run preview identified, but by the time the real apply's
        # own FIRST lookup runs, it resolves to something else entirely
        # (deleted and replaced, or reassigned) -- caught at the pre-write
        # fingerprint comparison (threshold 2, the default: the preview's
        # own lookup found "7" and reported it via report["fingerprint"],
        # but the real apply's first lookup already sees the swap), which
        # compares against expected_fingerprint (an EARLIER call's report)
        # rather than only ever comparing this call's own two internal
        # lookups against each other -- the latter would never even
        # notice a swap that already happened before this call started.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.existing_ruleset_id_after_second_lookup = None  # swapped away before the real apply starts
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
        self.assertIn("no longer matches what was previewed and", err)
        self.assertEqual(fake.puts, [])
        self.assertEqual(fake.posts, [])

    def test_refuses_to_write_when_the_ruleset_was_renamed_during_the_real_apply_itself(self):
        # The same pre-write fingerprint comparison, but for a rename
        # happening strictly DURING the real apply's own execution,
        # between its own two internal by-name lookups (threshold 3 -- the
        # preview and the real apply's own first lookup both still agree
        # at "7"; only the real apply's later write-time recheck sees the
        # rename). Distinct from the test above, which covers a swap that
        # already happened before the real apply even started -- both
        # windows are caught by the one check, but exercising them
        # separately confirms neither one is a blind spot for the other.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.existing_ruleset_id_after_second_lookup = None  # renamed away, from the write-time recheck on
        fake.existing_ruleset_id_lookup_threshold = 3
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
        self.assertIn("no longer matches what was previewed and", err)
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
        # Not a bare "master" substring check: the printed plan legitimately
        # mentions master as part of the ruleset's own hardened targeting
        # ("...on main, main and master") -- it's specifically the
        # branch-exists warning that must be absent.
        self.assertNotIn("branch literally named 'master'", err)

    def test_master_branch_check_failure_is_reported_but_not_fatal(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.master_error = "gh: HTTP 403: Resource protected by organization SAML enforcement\n"
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("could not check whether", err)
        self.assertIn("403", err)

    def test_empty_check_name_is_a_usage_error_not_a_preview_failure(self):
        # apply_ruleset validates check names before any gh call it makes;
        # that usage error must propagate directly (exit 2), not be folded
        # into "the preview above failed" (exit 1), which would misreport
        # a usage problem as a remote-state one.
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--rule", "", REPO])
        self.assertEqual(code, 2)
        self.assertNotIn("preview", err)

    def test_never_reported_check_blocks_before_any_interactive_confirmation(self):
        # Codex review: only an explicit --force may waive the
        # never-reported-check guard -- a plain "yes" to the general
        # combined-plan confirmation must not silently carry that
        # authority, since the guard exists specifically to stop someone
        # from requiring a check that will never report and so block
        # every future merge. The ruleset preview (which decides whether
        # the guard blocks or merely warns) is gated on args.force, not a
        # hardcoded True, so this refuses outright -- BEFORE the
        # interactive confirmation is ever reached, meaning there is
        # nothing for a "yes" to override in the first place.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: []}  # 'lanes' never reported
        with patch("builtins.input") as mock_input:
            code, _, err = _run(fake, ["--rule", "lanes", REPO], isatty=True)
        self.assertEqual(code, 1)
        self.assertIn("never reported", err)
        self.assertIn("Pass --force to require it anyway", err)
        mock_input.assert_not_called()
        self.assertEqual(fake.posts, [])

    def test_never_reported_check_still_applies_with_explicit_force(self):
        # The other half of the same guard: an EXPLICIT --force still
        # overrides it and applies, same as before this fix.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: []}
        code, _, err = _run(fake, ["--force", "--rule", "never-reported", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("never reported", err)
        self.assertIn("--force given", err)
        self.assertEqual(len(fake.posts), 1)

    def test_check_going_missing_between_preview_and_real_apply_is_refused_without_force(self):
        # Codex review: round 1 fixed the never-reported-check guard at the
        # PREVIEW call (dry_run=True, force=args.force), so a plain "yes"
        # can't silently waive it. But the REAL apply call (dry_run=False)
        # used to pass a hardcoded force=True -- needed so it never tries
        # to re-read a confirmation from stdin the caller's own prompt
        # already consumed -- and that hardcoded True was ALSO read by the
        # guard as "the user authorized overriding this". So a check that
        # WAS reporting when the preview ran, but had fallen out of the
        # reported set by the time the real apply's own fresh scan ran
        # (the default branch advancing to a not-yet-reported commit in
        # that window, say), got silently downgraded from "block" to "warn
        # and proceed" even though the user never passed --force and the
        # confirmed plan never showed it as missing. This simulates that
        # exact timing: _collect_reported returns the real (fully-
        # reporting) result on its first call (the preview) and an empty
        # set on its second (the real apply's own scan) -- distinct from
        # the existing tests above, which are missing from the START.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}

        real_collect_reported = rules._collect_reported
        calls = []

        def flaky_collect_reported(repo, wanted):
            calls.append(1)
            if len(calls) >= 2:
                return set()
            return real_collect_reported(repo, wanted)

        with patch("repo_lib.rules._collect_reported", side_effect=flaky_collect_reported):
            with patch("builtins.input", return_value="y"):
                code, _, err = _run(fake, ["--rule", "lanes", REPO], isatty=True)
        self.assertEqual(code, 1)
        self.assertIn("failed on:", err)
        self.assertIn("ruleset", err)
        self.assertIn("never reported", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.puts, [])

    def test_force_still_prints_the_full_plan_before_applying(self):
        # Codex review: --force skips the confirmation QUESTION, not the
        # audit trail -- notably a secret's own OVERWRITES-an-existing-
        # value warning, which a forced/unattended run must still leave
        # in its own output rather than silently applying it unseen.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.secret_names = {"TOKEN"}  # already exists -> OVERWRITES warning
            code, _, err = _run(fake, ["--force", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("OVERWRITES an existing value", err)

    def test_no_rules_alone_with_nothing_else_requested_is_a_no_op(self):
        # Codex review: no ruleset step, no secrets, no apps -- nothing
        # would be written, so there's nothing to confirm. Must not block
        # non-interactively on a question with no mutation behind it.
        fake = FakeGh()
        code, out, err = _run(fake, ["--no-rules", REPO], isatty=False)
        self.assertEqual(code, 0, err)
        self.assertNotIn("stdin is not a terminal", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.puts, [])

    def test_ruleset_already_matching_skips_confirmation_non_interactively(self):
        # Codex review: a ruleset step WAS requested, but its preview shows
        # it already matches -- nothing would actually be written, so (like
        # the --no-rules-alone case above) there's nothing to confirm.
        # Regressing this would make an idempotent, non-interactive
        # `repo setup --rule X OWNER/REPO` fail unless --force were passed
        # for no real reason, where it previously always just reported
        # "nothing to do" and exited 0.
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
        code, out, err = _run(fake, ["--rule", "lanes", REPO], isatty=False)
        self.assertEqual(code, 0, err)
        self.assertNotIn("stdin is not a terminal", err)
        self.assertIn("already matches; nothing to do", out)
        self.assertEqual(fake.puts, [])

    def test_a_rule_value_matching_the_no_op_message_does_not_fake_a_no_op(self):
        # Codex review: the no-op check used to substring-search the
        # preview's own rendered text for NO_OP_MESSAGE -- but a required
        # check name is ALSO printed as one of that same text's lines, so
        # a --rule value equal to (or containing) NO_OP_MESSAGE's exact
        # text would satisfy the search even in a genuine "would create"
        # plan, skipping confirmation for a real, unconfirmed write. No
        # existing ruleset here -- this is squarely the CREATE path, which
        # never even reaches the no-op short-circuit, yet the substring
        # search would have been fooled by the check name embedded in the
        # printed "required checks:" list.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: [rules.NO_OP_MESSAGE]}
        code, out, err = _run(fake, ["--rule", rules.NO_OP_MESSAGE, REPO], isatty=False)
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(fake.posts, [])

    def test_ruleset_content_changed_independently_is_refused_not_silently_applied(self):
        # Codex review: the ruleset step's own analog of the App-plan gap
        # above -- previewed as a no-op (so needs_confirmation may have
        # skipped asking about it entirely), but the SAME ruleset id's
        # content changes independently (someone else edits its required
        # checks) between the preview and the real apply's own first
        # (early, pre-dry_run) needs_write check. Identity alone (the id
        # staying "7" throughout) wouldn't catch this -- only comparing
        # the fingerprint's needs_write half does. Simulated by mutating
        # the fixture's ruleset content right before the SECOND
        # _build_update_body call (the first is the preview's own; the
        # second is the real apply's own early check) -- caught at the
        # pre-write fingerprint comparison, not the early one, since this
        # function no longer refuses as soon as needs_write flips; it
        # refuses once, right before the write, on whatever differs.
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

        real_build_update_body = rules._build_update_body
        calls = []

        def flaky_build_update_body(repo, existing_id, checks, ruleset_name):
            calls.append(1)
            if len(calls) == 2:
                fake.ruleset_objects["7"]["rules"][0]["parameters"]["required_status_checks"] = []
            return real_build_update_body(repo, existing_id, checks, ruleset_name)

        with patch("repo_lib.rules._build_update_body", side_effect=flaky_build_update_body):
            code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("no longer matches what was previewed and", err)
        self.assertEqual(fake.puts, [])

    def test_same_id_still_needing_a_write_but_with_different_content_is_refused(self):
        # Codex review: the residual gap the earlier fingerprint tests
        # above don't cover -- not an id swap, and not a no-op flipping to
        # a real write (both already refused), but the SAME ruleset id
        # needing a write in BOTH the preview and the real apply, where
        # the ACTUAL PAYLOAD differs because something else edited the
        # ruleset's managed content in between (here: a required check
        # re-pointed at a different GitHub App's integration_id) while the
        # write was still genuinely needed either way. needs_write alone
        # (True throughout) wouldn't catch this; only comparing the
        # fingerprint's target_body half does. 'codex' is missing from the
        # ruleset in both the "before" and "after" fixture objects, so a
        # write is needed at every read regardless of the integration_id
        # drift on 'lanes' -- isolating content drift from the
        # needs-write-boolean case the test above already covers.
        # ruleset_content_change_threshold=3 makes the swap land after the
        # preview's own three reads (ownership, scope, needs-write) and
        # before any of the real apply's, so the preview sees
        # integration_id 111 throughout and the real apply -- both its
        # near-top pass and its pre-write recheck -- consistently sees
        # 222, simulating an edit that already happened and settled while
        # the user was considering the confirmed plan.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_content_change_threshold = 3
        base_object = {
            "id": 7,
            "name": "merge gates",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [{"context": "lanes", "integration_id": 111}],
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
        fake.ruleset_objects["7"] = json.loads(json.dumps(base_object))
        changed_object = json.loads(json.dumps(base_object))
        changed_object["rules"][0]["parameters"]["required_status_checks"][0]["integration_id"] = 222
        fake.ruleset_objects_after_change["7"] = changed_object

        code, _, err = _run(fake, ["--force", "--rule", "lanes", "--rule", "codex", REPO])
        self.assertEqual(code, 1)
        self.assertIn("no longer matches what was previewed and", err)
        self.assertEqual(fake.puts, [])


class SecretSpecValidationTest(unittest.TestCase):
    """--secret NAME[@ENV]=PATH parsing and up-front validation. All of
    these must be usage errors (exit 2) with zero gh calls -- caught
    before gh.require_gh() even runs."""

    def test_malformed_spec_is_a_usage_error(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--secret", "not-a-valid-spec", REPO])
        self.assertEqual(code, 2)
        self.assertIn("NAME[@ENV]=PATH", err)
        self.assertEqual(fake.calls, [])

    def test_empty_name_is_a_usage_error(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--secret", "=path", REPO])
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])

    def test_at_sign_with_empty_env_is_a_usage_error(self):
        # NAME@=PATH must not be silently downgraded to a repository-level
        # secret -- the caller wrote the @, meaning an environment scope.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            code, _, err = _run(fake, ["--force", "--secret", f"TOKEN@={path}", REPO])
        self.assertEqual(code, 2)
        self.assertIn("empty ENV", err)
        self.assertEqual(fake.calls, [])

    def test_unreadable_path_is_a_usage_error(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--secret", "TOKEN=/does/not/exist", REPO])
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])

    def test_empty_secret_file_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt", content=b"")
            fake = FakeGh()
            code, _, err = _run(fake, ["--force", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 2)
        self.assertIn("empty", err)
        self.assertEqual(fake.calls, [])

    def test_invalid_secret_name_is_a_usage_error_before_any_gh_call(self):
        # Judgment call (see TODO.md "Decisions needing review"): the
        # porting source only discovers an invalid secret name lazily,
        # via repo-secrets' own --dry-run subprocess call, after the
        # ruleset step's harmless preview has already made gh calls. Since
        # secrets_cmd.validate_name is a real function here, not a
        # subprocess, it's validated up front instead -- zero gh calls,
        # exit 2 -- rather than reproducing that lazy discovery.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            code, _, err = _run(fake, ["--force", "--secret", f"BAD-NAME={path}", REPO])
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.posts, [])

    def test_duplicate_name_and_env_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _secret_file(tmp, "first.txt", content=b"first")
            second = _secret_file(tmp, "second.txt", content=b"second")
            fake = FakeGh()
            code, _, err = _run(
                fake,
                ["--force", "--secret", f"TOKEN={first}", "--secret", f"TOKEN={second}", REPO],
            )
        self.assertEqual(code, 2)
        self.assertIn("repeats an earlier --secret", err)
        self.assertEqual(fake.calls, [])

    def test_duplicate_is_case_insensitive_on_both_name_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _secret_file(tmp, "first.txt", content=b"first")
            second = _secret_file(tmp, "second.txt", content=b"second")
            fake = FakeGh()
            code, _, err = _run(
                fake,
                ["--force", "--secret", f"TOKEN@Prod={first}", "--secret", f"token@prod={second}", REPO],
            )
        self.assertEqual(code, 2)
        self.assertIn("repeats an earlier --secret", err)

    def test_same_name_different_env_is_not_a_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            code, _, err = _run(
                fake,
                [
                    "--dry-run",
                    "--no-rules",
                    "--secret",
                    f"TOKEN={path}",
                    "--secret",
                    f"TOKEN@lanes={path}",
                    REPO,
                ],
            )
        self.assertEqual(code, 0, err)
        self.assertNotIn("repeats an earlier --secret", err)


class AppSlugValidationTest(unittest.TestCase):
    def test_empty_slug_is_a_usage_error(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--app", "", REPO])
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])

    def test_slug_with_disallowed_characters_is_a_usage_error(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--app", "not a slug", REPO])
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])


class SecretStepTest(unittest.TestCase):
    def test_repository_level_secret_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt", content=b"sekrit")
            fake = FakeGh()
            code, out, err = _run(fake, ["--force", "--no-rules", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.written_secrets, [("TOKEN", REPO, None, b"sekrit")])
        self.assertIn(f"{REPO}: set 'TOKEN'", out)

    def test_environment_scoped_secret_creates_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt", content=b"sekrit")
            fake = FakeGh()
            code, out, err = _run(
                fake, ["--force", "--no-rules", "--secret", f"TOKEN@lanes={path}", REPO]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.written_secrets, [("TOKEN", REPO, "lanes", b"sekrit")])
        self.assertIn("lanes", fake.env_secret_names)  # environment now exists

    def test_environment_scoped_secret_does_not_recreate_an_existing_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.env_secret_names["lanes"] = set()  # already exists
            code, _, err = _run(fake, ["--force", "--no-rules", "--secret", f"TOKEN@lanes={path}", REPO])
        self.assertEqual(code, 0, err)
        env_puts = [c for c in fake.calls if c[1:3] == ["--method", "PUT"] and "environments/lanes" in c[3]]
        self.assertEqual(env_puts, [])

    def test_secret_write_failure_is_reported_and_does_not_block_other_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.set_fails = {"TOKEN"}
            code, _, err = _run(fake, ["--force", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 1)
        self.assertIn("failed on:", err)
        self.assertIn("secret:TOKEN", err)
        self.assertEqual(len(fake.posts), 1)  # the ruleset step still ran

    def test_secret_created_by_someone_else_since_the_plan_was_built_is_refused(self):
        # The preview's own list read sees TOKEN absent (state "new");
        # the recheck immediately before the write sees it now present --
        # simulating someone else creating it in between. Must refuse
        # rather than silently overwrite a value only ever confirmed as
        # a fresh write.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.secret_names_after_recheck = {"TOKEN"}
            code, _, err = _run(fake, ["--force", "--no-rules", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 1)
        self.assertIn("was created by", err)
        self.assertEqual(fake.written_secrets, [])

    def test_secret_recheck_failure_is_reported_as_that_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.fail_secret_recheck = {None}
            code, _, err = _run(fake, ["--force", "--no-rules", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 1)
        self.assertIn("secret:TOKEN", err)
        self.assertEqual(fake.written_secrets, [])

    def test_a_stale_secret_does_not_block_the_ruleset_step_that_ran_before_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.secret_names_after_recheck = {"TOKEN"}
            code, _, err = _run(fake, ["--force", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 1)
        self.assertIn("secret:TOKEN", err)
        self.assertEqual(len(fake.posts), 1)  # ruleset step still applied

    def test_secret_file_is_snapshotted_before_the_plan_is_shown(self):
        # The value used at write time is the byte snapshot taken during
        # up-front validation, not a fresh read of PATH -- proven here by
        # editing the file's on-disk content after setup_cmd has already
        # validated/snapshotted it (patching open() only for the *write*
        # step would be circular, so instead we edit the real file in
        # between two calls and confirm the ORIGINAL bytes still ship).
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt", content=b"original-value")
            fake = FakeGh()

            # Simulate an edit landing after setup_cmd's own read by
            # monkeypatching secrets_cmd._recheck_still_absent (called
            # once, right before the real write) to edit the file on its
            # way through.
            from repo_lib import secrets_cmd

            real_recheck = secrets_cmd._recheck_still_absent

            def edit_then_recheck(repo, name, env):
                with open(path, "wb") as f:
                    f.write(b"edited-after-preview")
                return real_recheck(repo, name, env)

            with patch("repo_lib.secrets_cmd._recheck_still_absent", side_effect=edit_then_recheck):
                code, _, err = _run(fake, ["--force", "--no-rules", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.written_secrets, [("TOKEN", REPO, None, b"original-value")])

    def test_a_secret_env_creation_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.env_create_fails = {"lanes"}
            code, _, err = _run(fake, ["--force", "--no-rules", "--secret", f"TOKEN@lanes={path}", REPO])
        self.assertEqual(code, 1)
        self.assertIn("secret:TOKEN", err)
        self.assertEqual(fake.written_secrets, [])


class AppStepTest(unittest.TestCase):
    def test_selected_installation_not_yet_a_member_is_added(self):
        fake = FakeGh()
        fake.installations = [("codex", "111", "selected", "owner")]
        code, out, err = _run(fake, ["--force", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: added to codex's installation", out)

    def test_selected_installation_not_yet_a_member_confirms_the_repo_exists_first(self):
        # Codex review, ported: absent from a 'selected' installation's
        # member list is ambiguous -- genuinely not-yet-added, or a
        # typo'd/inaccessible repo that would never appear in any
        # installation's member list either way. Must confirm the repo
        # itself exists before planning ADD.
        fake = FakeGh()
        fake.installations = [("codex", "111", "selected", "owner")]
        fake.repo_missing = True
        code, out, err = _run(fake, ["--dry-run", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 1)
        self.assertNotIn("would add", out)
        self.assertIn(f"could not confirm {REPO} exists", err)

    def test_disambiguates_by_account_when_the_same_app_is_installed_elsewhere_too(self):
        # user/installations lists every installation the authenticated
        # user can see, across every account -- filtering on slug alone
        # would reject an unambiguous target whenever the App is ALSO
        # installed on some other account. The repo's own owner is what
        # disambiguates it.
        fake = FakeGh()
        fake.installations = [
            ("codex", "222", "selected", "some-other-org"),
            ("codex", "111", "selected", "owner"),
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("more than one installation", err)
        self.assertIn(f"{REPO}: added to codex's installation", out)

    def test_installation_scoped_to_all_repositories_needs_nothing_added(self):
        fake = FakeGh()
        fake.installations = [("codex", "111", "all", "owner")]
        code, out, err = _run(fake, ["--dry-run", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("already covered", out)

    def test_all_repositories_installation_still_confirms_the_repo_exists(self):
        # Codex review, ported: unlike ALREADY_MEMBER (which only ever
        # reports success from a real gh api response naming the repo),
        # a bare 'all repositories' scope check alone must never claim
        # coverage of a repo that was never confirmed to exist.
        fake = FakeGh()
        fake.installations = [("codex", "111", "all", "owner")]
        fake.repo_missing = True
        code, _, err = _run(fake, ["--force", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not confirm {REPO} exists", err)
        self.assertIn("failed on:", err)
        self.assertIn("app:codex", err)

    def test_already_a_member_needs_nothing_added(self):
        fake = FakeGh()
        fake.installations = [("codex", "111", "selected", "owner")]
        fake.install_members = {"111": {REPO}}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("already a member", out)

    def test_already_member_and_already_all_apply_as_a_real_no_op(self):
        # ALREADY_MEMBER/ALREADY_ALL still re-resolve fresh at apply time
        # (see the recheck test below) -- but when nothing has actually
        # changed since the preview, that fresh resolve agrees, and the
        # observable outcome is still a no-op: no PUT, no "added to" line.
        # Two separate --app runs (each with a single App) rather than one
        # combined run, since only one slug per --app can resolve to a
        # given installation account in this fixture.
        fake = FakeGh()
        fake.installations = [("codex", "111", "selected", "owner")]
        fake.install_members = {"111": {REPO}}
        code, out, err = _run(fake, ["--force", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("added to", out)

        fake2 = FakeGh()
        fake2.installations = [("zizmor", "222", "all", "owner")]
        code, out, err = _run(fake2, ["--force", "--no-rules", "--app", "zizmor", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("added to", out)

    def test_membership_removed_during_confirmation_is_not_silently_trusted_or_silently_added(self):
        # Codex review, round 1 of this: an App's coverage seen as
        # ALREADY_MEMBER during the preview must not be trusted as a no-op
        # success at apply time without a fresh check -- membership can be
        # revoked (or the installation narrowed) while the confirmation
        # prompt is open. Simulated here by removing the fixture's
        # membership right before the SECOND plan_app_step call -- the
        # first is setup_cmd's own preview (sees ALREADY_MEMBER);
        # apply_step's fresh re-resolve is the second, and by then
        # membership is gone.
        #
        # Codex review, round 2 of this: fixing THAT by simply adding it
        # back opened a different gap -- the preview said "already a
        # member" (a no-op), so setup_cmd's own needs_confirmation may
        # have skipped asking about this App at all; silently promoting
        # that into an actual write is applying something the user was
        # never shown, let alone confirmed. So the correct outcome here
        # is neither "report the stale ALREADY_MEMBER as success" nor
        # "silently add it" -- it's refusing and asking for a fresh run.
        fake = FakeGh()
        fake.installations = [("codex", "111", "selected", "owner")]
        fake.install_members = {"111": {REPO}}

        real_plan_app_step = apps.plan_app_step
        calls = []

        def flaky_plan_app_step(repo, repo_owner, slug):
            calls.append(1)
            if len(calls) == 2:
                fake.install_members["111"] = set()  # membership revoked
            return real_plan_app_step(repo, repo_owner, slug)

        with patch("repo_lib.apps.plan_app_step", side_effect=flaky_plan_app_step):
            code, out, err = _run(fake, ["--force", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 1)
        self.assertNotIn("added to", out)
        self.assertIn("now needs owner/repo added", err)
        self.assertIn("app:codex", err)

    def test_membership_confirmed_still_present_at_apply_time_stays_a_no_op(self):
        # The other direction: previewed as ALREADY_MEMBER, and the fresh
        # re-resolution at apply time agrees -- nothing changed, so this
        # must still succeed as a no-op, same as before either fix.
        fake = FakeGh()
        fake.installations = [("codex", "111", "selected", "owner")]
        fake.install_members = {"111": {REPO}}
        code, out, err = _run(fake, ["--force", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("added to", out)

    def test_membership_check_is_case_insensitive_against_canonical_casing(self):
        # .full_name comes back in the repo's own canonical casing, which
        # can differ from however the caller typed --app's OWNER/REPO.
        fake = FakeGh()
        fake.installations = [("codex", "111", "selected", "Owner")]
        fake.install_members = {"111": {"Owner/Repo"}}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", "--app", "codex", "Owner/repo"])
        self.assertEqual(code, 0, err)
        self.assertIn("already a member", out)

    def test_no_installation_found_is_reported_as_an_error(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--no-rules", "--app", "not-installed-anywhere", REPO])
        self.assertEqual(code, 1)
        self.assertIn("no installation of an App with slug 'not-installed-anywhere' was found", err)
        self.assertIn("failed on:", err)
        self.assertIn("app:not-installed-anywhere", err)

    def test_app_dry_run_makes_no_writes(self):
        fake = FakeGh()
        fake.installations = [("codex", "111", "selected", "owner")]
        code, out, err = _run(fake, ["--dry-run", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("would add", out)

    def test_app_already_covered_skips_confirmation_non_interactively(self):
        # Codex review: an App WAS requested, but it's already a member --
        # nothing would actually be written, so there's nothing to
        # confirm. Must not block non-interactively on a question with no
        # mutation behind it.
        fake = FakeGh()
        fake.installations = [("codex", "111", "selected", "owner")]
        fake.install_members = {"111": {REPO}}
        code, out, err = _run(fake, ["--no-rules", "--app", "codex", REPO], isatty=False)
        self.assertEqual(code, 0, err)
        self.assertNotIn("stdin is not a terminal", err)
        # The plan (including "already a member") is always printed to
        # stderr, unconditionally -- see the "Codex review" comment above
        # the print loop in setup_cmd.run().
        self.assertIn("already a member", err)

    def test_app_membership_write_failure_is_reported(self):
        fake = FakeGh()
        fake.installations = [("codex", "111", "selected", "owner")]
        fake.install_add_fails = {"111"}
        code, _, err = _run(fake, ["--force", "--no-rules", "--app", "codex", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not add", err)
        self.assertIn("classic PAT", err)
        self.assertIn("app:codex", err)

    def test_app_plan_error_does_not_block_dry_run_output_of_other_steps(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", "--app", "no-such-app", REPO])
        self.assertEqual(code, 1)  # App error still makes --dry-run's own exit nonzero
        self.assertIn("would create ruleset", out)  # but the ruleset preview still shows


class CombinedPlanTest(unittest.TestCase):
    def test_combined_dry_run_shows_every_requested_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.installations = [("codex", "111", "selected", "owner")]
            code, out, err = _run(
                fake, ["--dry-run", "--secret", f"TOKEN={path}", "--app", "codex", REPO]
            )
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}:", out)
        self.assertIn("ruleset (repo-rules):", out)
        self.assertIn("secrets (repo-secrets):", out)
        self.assertIn("App installation membership:", out)
        self.assertIn("would add", out)

    def test_no_terminal_and_no_force_changes_nothing_across_every_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.installations = [("codex", "111", "selected", "owner")]
            code, _, err = _run(fake, ["--secret", f"TOKEN={path}", "--app", "codex", REPO], isatty=False)
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.written_secrets, [])

    def test_interactive_confirmation_asks_once_for_every_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.installations = [("codex", "111", "selected", "owner")]
            with patch("builtins.input", return_value="y") as mock_input:
                code, out, err = _run(
                    fake, ["--secret", f"TOKEN={path}", "--app", "codex", REPO], isatty=True
                )
        self.assertEqual(code, 0, err)
        self.assertEqual(mock_input.call_count, 1)  # one confirmation, not three
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(len(fake.written_secrets), 1)
        self.assertIn(f"{REPO}: added to codex's installation", out)

    def test_declining_the_single_confirmation_applies_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.installations = [("codex", "111", "selected", "owner")]
            with patch("builtins.input", return_value="n"):
                code, _, err = _run(
                    fake, ["--secret", f"TOKEN={path}", "--app", "codex", REPO], isatty=True
                )
        self.assertEqual(code, 1)
        self.assertIn("not confirmed", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.written_secrets, [])

    def test_every_step_is_attempted_even_when_an_earlier_one_fails(self):
        # The ruleset write itself is forced to fail, while a secret and an
        # App membership are also requested -- both must still be applied,
        # and only "ruleset" is named among the failures.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")

            class FailingRulesetWriteGh(FakeGh):
                def run_with_input(self, args, input_bytes):
                    if args[0] == "api":
                        raise gh.GhError("gh: simulated failure\n")
                    return super().run_with_input(args, input_bytes)

            fake = FailingRulesetWriteGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.installations = [("codex", "111", "selected", "owner")]
            code, _, err = _run(fake, ["--force", "--secret", f"TOKEN={path}", "--app", "codex", REPO])
        self.assertEqual(code, 1)
        self.assertIn("failed on:", err)
        self.assertIn("ruleset", err)
        self.assertNotIn("secret:TOKEN", err)
        self.assertNotIn("app:codex", err)
        self.assertEqual(fake.written_secrets, [("TOKEN", REPO, None, b"sekrit")])

    def test_a_secret_dry_run_failure_blocks_the_whole_apply_including_the_ruleset(self):
        # A secret whose plan-build read fails ("error" state) makes the
        # combined preview fail -- nothing is applied at all, not even the
        # ruleset step whose own preview succeeded.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.repo_missing = True  # makes the secret's own plan-build read fail
            code, _, err = _run(fake, ["--force", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 1)
        self.assertIn("the preview above failed", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.written_secrets, [])

    def test_an_app_plan_error_does_not_block_the_other_steps_from_applying(self):
        # Unlike a ruleset/secret preview failure, an App-plan error is an
        # independent per-step runtime outcome and must not refuse the
        # whole apply -- the ruleset and secret steps still go through.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            # No installations registered at all -> App plan ERROR.
            code, _, err = _run(
                fake, ["--force", "--secret", f"TOKEN={path}", "--app", "codex", REPO]
            )
        self.assertEqual(code, 1)
        self.assertIn("app:codex", err)
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(fake.written_secrets, [("TOKEN", REPO, None, b"sekrit")])

    def test_master_branch_check_runs_alongside_secret_and_app_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.installations = [("codex", "111", "selected", "owner")]
            fake.master_exists = True
            code, _, err = _run(fake, ["--force", "--secret", f"TOKEN={path}", "--app", "codex", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("branch literally named 'master'", err)


if __name__ == "__main__":
    unittest.main()
