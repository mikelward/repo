import base64
import urllib.parse
import hashlib
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from repo_lib import apps, gh, rules, scaffold
from repo_lib.cli import main

REPO = "owner/repo"

# The three refs a ruleset this tool writes always targets. A fixture
# using it is one whose scope is ALREADY hardened, so the run under test
# has no widening to do -- which is what makes "already matches; nothing
# to do" a real no-op rather than a scope rewrite waiting to happen.
_HARDENED_SCOPE = ("~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master")

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
_ENVIRONMENTS_RE = re.compile(r"^repos/([^/]+/[^/]+)/environments$")
_REPO_SECRET_ONE_RE = re.compile(r"^repos/([^/]+/[^/]+)/actions/secrets/([^/]+)$")
_ENV_SECRET_ONE_RE = re.compile(r"^repos/([^/]+/[^/]+)/environments/([^/]+)/secrets/([^/]+)$")
_WORKFLOWS_DIR_RE = re.compile(r"^repos/([^/]+/[^/]+)/contents/\.github/workflows(?:\?ref=(.+))?$")
_WORKFLOW_FILE_RE = re.compile(r"^repos/([^/]+/[^/]+)/contents/\.github/workflows/([^/?]+)(?:\?ref=(.+))?$")
_ROOT_CONTENTS_RE = re.compile(r"^repos/([^/]+/[^/]+)/contents(?:\?ref=(.+))?$")
_BRANCHES_RE = re.compile(r"^repos/([^/]+/[^/]+)/branches\?per_page=100$")
_USER_INSTALLATIONS_RE = re.compile(r"^user/installations$")
_INSTALL_REPOS_RE = re.compile(r"^user/installations/([^/]+)/repositories$")
_INSTALL_REPO_ONE_RE = re.compile(r"^user/installations/([^/]+)/repositories/([^/]+)$")
_INSTALL_SLUG_JQ_RE = re.compile(r'app_slug=="([^"]*)"')
_INSTALL_OWNER_JQ_RE = re.compile(r'== \("([^"]*)" \| ascii_downcase\)')

# -- bootstrap/scaffold step: the two external template sources, and the
# target repo's own git-data-api reads/writes plan_gaps/apply_gaps make.
# Deliberately its own set, not folded into _WORKFLOW_FILE_RE above: that
# one matches ANY repo's .github/workflows/*, which happens to also match
# mikelward/lanes's zizmor.yml by accident (same path shape) -- relying on
# that would be a coincidence future edits to either regex could silently
# break, not a real fixture for "this is where the scaffold fetches from".
_SCAFFOLD_TEMPLATE_COMMIT_RE = re.compile(r"^repos/mikelward/codex-review/commits/main$")
_SCAFFOLD_TEMPLATE_RE = re.compile(r"^repos/mikelward/codex-review/contents/templates/([^/?]+)\?ref=(.+)$")
_SCAFFOLD_ZIZMOR_RE = re.compile(r"^repos/mikelward/lanes/contents/\.github/workflows/zizmor\.yml\?ref=main$")
# Singular git/ref/... is the read (GET) route; plural git/refs/... is
# create/update/delete only and has no GET at all -- two regexes, not one,
# so the fixture can only answer a read on the route real GitHub actually
# serves it on (Codex review, mikelward/repo#14).
_SCAFFOLD_REF_READ_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/ref/heads/([^/?]+)$")
_SCAFFOLD_REF_WRITE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/refs/heads/([^/?]+)$")
_SCAFFOLD_COMMIT_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/commits/([^/?]+)$")
_SCAFFOLD_TREE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/trees/([^/?]+)\?recursive=1$")
_SCAFFOLD_BLOB_CREATE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/blobs$")
_SCAFFOLD_TREE_CREATE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/trees$")
_SCAFFOLD_COMMIT_CREATE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/commits$")
_SCAFFOLD_CONTENTS_PUT_RE = re.compile(r"^repos/([^/]+/[^/]+)/contents/(.+)$")

_OWNERSHIP_JQ = ".enforcement, .target"


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
    single ruleset id are distinguished (the enforcement check vs. the
    scope fetch) -- there is no third, so matching on the enforcement JQ
    string and falling back to "scope fetch" for any other is safe.
    """

    def __init__(self):
        self.calls = []
        # -- ruleset step state (unchanged) --
        self.default_branch = "main"
        self._default_branch_reads = 0
        self.default_branch_after_bootstrap_plan = None
        self.allow_rebase = "true"
        self.allow_auto_merge = "true"
        self.fail_allow_auto_merge = False
        self.delete_branch_on_merge = "true"
        self.fail_delete_branch_on_merge = False
        self.patches = []
        self.patch_fails = False
        self.branch_count = "1"
        self.default_head_sha = "abc123"
        self.default_head_fails = False
        self.check_runs = {}
        self.statuses = {}
        self.open_prs = []
        self.closed_prs = []
        self.existing_ruleset_id = None
        # Set by the migration tests: the id a lookup for a legacy ruleset
        # name resolves to, or None for "there isn't one".
        self.legacy_ruleset_id = None
        # Set instead when a test needs more than one ruleset sharing a
        # legacy name -- GitHub does not make the name unique.
        self.legacy_ruleset_ids = None
        self.deleted_rulesets = []
        self.ruleset_delete_fails = set()
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
        # rid -> read count after which reads of that id fail, for the
        # "could not tell" half of a recheck (as distinct from "changed").
        self.ruleset_read_fails_after = {}
        self.master_exists = False
        self.master_error = None
        self.master_redirect_name = None  # branch a renamed master redirects to
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

        # -- fleet-credentials step state --
        # None models a repository with no .github/workflows at all (404);
        # a list is the file names the directory holds, each read back
        # from workflow_texts (absent => empty file).
        self.workflow_files = None
        self.workflow_texts = {}
        # From the second listing / second read of a file on: the state a
        # recheck sees, modeling a change made while the plan waited on
        # confirmation. None => unchanged; "error" (listing only) => the
        # second listing fails.
        self.workflow_files_after_recheck = None
        # Other branches: name -> {workflow name: text}. A branch lists the
        # default branch's workflows plus these; a text equal to the
        # default's has the same blob sha and is not re-read.
        self.branch_workflows = {}
        self.workflow_texts_after_recheck = {}
        self._workflow_reads = {}
        self.root_contents_error = None  # stderr for the root listing, or None to succeed
        self.deleted_secrets = []  # (name, env or None)
        self.delete_fails = set()  # secret names whose DELETE fails

        # -- App-installation step state --
        self.installations = []  # (slug, id, selection, account)
        self.install_members = {}  # install_id -> set(full_name)
        self.install_add_fails = set()  # install_ids whose membership PUT fails

        # -- bootstrap/scaffold step state --
        # Fake content for the two external template sources -- what it
        # says doesn't matter to plan_gaps (only which PATHS exist), and
        # default_branch is "main" above so _branches_line's own rewrite
        # of zizmor.yml is always a no-op here regardless of content.
        self.template_contents = {name: f"# fake {name}\n" for name in scaffold.TEMPLATE_FILES}
        self.zizmor_workflow_content = "# fake zizmor.yml\n"
        self.template_fetch_fails = set()  # TEMPLATE_FILES names whose fetch 404s
        self.template_resolve_fails = False  # codex-review main->sha resolve fails
        # The fixed sha every template fetch must be pinned to once
        # _resolve_commit_sha resolves codex-review's main -- asserted
        # below rather than just accepted, so a regression back to each
        # fetch resolving "main" independently (Codex review,
        # mikelward/repo#14) fails loudly instead of passing by accident.
        self.template_commit_sha = "faketemplateshaabc123"
        self.zizmor_fetch_fails = False
        # Whether default_branch has any commits yet. False (the ordinary
        # case) means the branch's git/refs/heads ref exists;
        # bootstrap_ref_missing models a repository with none yet (`repo
        # create --no-scaffold`, or one otherwise still empty) via the
        # HTTP 404 shape ("this ref specifically doesn't exist"); a
        # genuinely brand-new, wholly-empty repository -- zero git objects
        # at all -- gets HTTP 409 ("Git Repository is empty") from this
        # same endpoint instead, which bootstrap_ref_empty_409 models
        # (Codex review, mikelward/repo#14).
        self.bootstrap_ref_missing = False
        self.bootstrap_ref_empty_409 = False
        # A 409 that ISN'T "Git Repository is empty" -- some other conflict
        # against a branch that has commits -- must NOT be read as "safe to
        # bootstrap": treating any 409 as empty would let a caller write
        # straight onto an existing branch (Codex review, mikelward/repo#14).
        self.bootstrap_ref_ambiguous_409 = False
        self.bootstrap_ref_fails = False  # a non-404/409 failure reading the ref
        self._scaffold_ref_reads = 0
        self.bootstrap_ref_sha_after_first_read = None
        self.bootstrap_commit_sha = "deadbeefcommit"
        # Tracks the scaffold ref's tip across a successful gap-fill PATCH
        # (see run_with_input's PATCH branch), so a read that follows a
        # real write sees the new commit rather than the stale value --
        # otherwise every write-then-recheck test would see a spurious
        # mismatch (Codex review, mikelward/repo#14).
        self._scaffold_ref_current_sha = self.bootstrap_commit_sha
        self._scaffold_ref_patched = False
        # Models a concurrent push landing AFTER apply_gaps's own write
        # already succeeded -- distinct from bootstrap_ref_sha_after_
        # first_read, which fires on read count and so would also catch
        # apply_gaps's own pre-PATCH recheck, never letting the write
        # happen at all (Codex review, mikelward/repo#14).
        self.bootstrap_ref_sha_after_own_write = None
        self.bootstrap_tree_sha = "deadbeeftree"
        self.bootstrap_commit_read_fails = False
        self.bootstrap_tree_read_fails = False
        self.bootstrap_tree_truncated = False
        # None (the default) means "every scaffold path is already
        # present" -- the harmless, no-op state every OTHER test in this
        # file implicitly relies on, since the bootstrap step is always
        # on. Set to an explicit set of paths to model a partially- or
        # un-scaffolded repository.
        self.bootstrap_existing_paths = None
        # path -> "tree" | "commit": a non-blob entry a test plants at (or
        # as an ancestor of) a scaffold path, modeling a path collision
        # plan_gaps must refuse rather than silently replace.
        self.bootstrap_occupied_entries = {}
        self.bootstrap_blob_fails = False
        self.bootstrap_tree_create_fails = False
        self.bootstrap_commit_create_fails = False
        self.bootstrap_ref_update_fails = False
        # The gh token's own OAuth scopes, as scaffold._missing_workflow_
        # scope reads them (via gh.token_scopes -> X-OAuth-Scopes). None
        # (the default) models a token this can't tell the scopes of at
        # all (a fine-grained PAT/GitHub App token) -- never blocks. Pass
        # a tuple missing "workflow" to model mikelward/repo#18's real
        # cause: a tree-create referencing a .github/workflows/* path
        # 404ing no matter how long a caller waits.
        self.token_scopes = None
        self._bootstrap_blob_seq = 0

    # -- repo_lib.gh.run/try_run/run_with_input replacements --------------

    def _secret_names_for(self, env):
        key = env
        self._list_calls[key] = self._list_calls.get(key, 0) + 1
        count = self._list_calls[key]
        # The fleet-credentials step lists the repository secrets once, up
        # front, before any --secret plan is built -- so at the repository
        # level the --secret step's own plan read is the second call and its
        # write-time recheck the third. Environment lists are unaffected:
        # that step reads only the fleet environments (the three hubs and
        # ci-commit-artifact), which no --secret test here targets.
        recheck = 3 if env is None else 2
        if count >= recheck and key in self.fail_secret_recheck:
            raise gh.GhError("gh: simulated failure on revalidation\n")
        if count >= recheck:
            if env is None and self.secret_names_after_recheck is not None:
                return self.secret_names_after_recheck
            if env is not None and env in self.env_secret_names_after_recheck:
                return self.env_secret_names_after_recheck[env]
        return self.secret_names if env is None else self.env_secret_names.get(env, set())

    def run(self, args):
        self.calls.append(list(args))
        assert args[0] == "api", args
        if args == ["api", "-i", "user"]:
            # gh.token_scopes()'s own read -- raw headers, a blank line,
            # then a body, same shape `gh api -i` really prints.
            header = (
                f"X-OAuth-Scopes: {', '.join(self.token_scopes)}\n" if self.token_scopes is not None else ""
            )
            return f"HTTP/2.0 200 OK\n{header}\n{{}}"
        endpoint, method, jq = _parse_api_args(args[1:])

        m = _DEFAULT_BRANCH_RE.match(endpoint)
        if m and jq == ".default_branch":
            if self.fail_default_branch:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            self._default_branch_reads += 1
            # default_branch_after_bootstrap_plan models an administrative
            # rename between the bootstrap plan's own read and setup_cmd's
            # pre-apply recheck -- None (the default) means every read
            # sees the same branch, matching every other test in this
            # file. The threshold is 2, not 1: _plan_credentials's own
            # unconditional credentials.workflow_texts call reads
            # .default_branch before the bootstrap step ever does (Codex
            # review, mikelward/repo#14 -- caught here, not by that PR
            # comment, while writing this fixture), so the bootstrap
            # plan's own read is the SECOND call, and the pre-apply
            # recheck this fixture exists to test is the third.
            branch = self.default_branch
            if self._default_branch_reads > 2 and self.default_branch_after_bootstrap_plan is not None:
                branch = self.default_branch_after_bootstrap_plan
            return branch + "\n"
        if m and jq == ".allow_auto_merge":
            if self.fail_allow_auto_merge or self.repo_missing:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            return self.allow_auto_merge + "\n"
        if m and jq == ".delete_branch_on_merge":
            if self.fail_delete_branch_on_merge or self.repo_missing:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            return self.delete_branch_on_merge + "\n"
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
            # Models --jq '[.name, .app.id]': an entry may be a bare name
            # (no App binding) or a (name, app id) pair.
            return "".join(
                json.dumps(list(n) if isinstance(n, tuple) else [n, None]) + "\n"
                for n in self.check_runs.get(m.group(2), [])
            )

        m = _STATUS_RE.match(endpoint)
        if m:
            contexts = self.statuses.get(m.group(2), [])
            return "".join(json.dumps(c) + "\n" for c in contexts)

        m = _PULLS_RE.match(endpoint)
        if m:
            shas = self.open_prs if m.group(2) == "open" else self.closed_prs
            return "".join(sha + "\n" for sha in shas)

        if _RULESETS_LOOKUP_RE.match(endpoint):
            # A lookup for a legacy name asks a different question, and
            # fixtures here model one ruleset -- the standard one. Answer
            # "no such ruleset" without counting it, so the swap thresholds
            # below stay about the lookups they were written for.
            match = re.search(r"select\(\.name == (\".*?\")\)", jq or "")
            if match and json.loads(match.group(1)) in rules.LEGACY_RULESET_NAMES:
                if self.legacy_ruleset_ids is not None:
                    return "".join(f"{rid}\n" for rid in self.legacy_ruleset_ids)
                return f"{self.legacy_ruleset_id}\n" if self.legacy_ruleset_id else ""
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
        if m and method == "DELETE":
            rid = m.group(2)
            if rid in self.ruleset_delete_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            self.deleted_rulesets.append(rid)
            self.ruleset_objects.pop(rid, None)
            if self.legacy_ruleset_id == rid:
                self.legacy_ruleset_id = None
            self.all_ruleset_ids = [r for r in self.all_ruleset_ids if r != rid]
            return ""

        m = _RULESET_ONE_RE.match(endpoint)
        if m:
            rid = m.group(2)
            self._ruleset_object_reads[rid] = self._ruleset_object_reads.get(rid, 0) + 1
            if self._ruleset_object_reads[rid] > self.ruleset_read_fails_after.get(rid, 1 << 30):
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            if (
                self._ruleset_object_reads[rid] > self.ruleset_content_change_threshold
                and rid in self.ruleset_objects_after_change
            ):
                obj = self.ruleset_objects_after_change[rid]
            elif rid not in self.ruleset_objects:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            else:
                obj = self.ruleset_objects[rid]
            if jq == _OWNERSHIP_JQ:
                # Fixtures predate the target check and are all branch
                # rulesets; default rather than making every one restate it.
                return obj.get("enforcement", "") + "\n" + obj.get("target", "branch") + "\n"
            if jq:
                ref_name = obj.get("conditions", {}).get("ref_name", {})
                return json.dumps(
                    {"include": ref_name.get("include", []), "exclude": ref_name.get("exclude", [])}
                ) + "\n"
            return json.dumps(obj)

        if _MASTER_BRANCH_RE.match(endpoint):
            if self.master_error:
                raise gh.GhError(self.master_error)
            if self.master_redirect_name:
                # GitHub 301s a renamed branch's old name to the new one and
                # gh follows it, so the call succeeds -- reporting the name
                # it landed on, not the one asked for.
                return f"{self.master_redirect_name}\n"
            if self.master_exists:
                return "master\n"
            raise gh.GhError("gh: HTTP 404: Not Found\n")

        m = _ACTIONS_SECRETS_RE.match(endpoint)
        if m:
            if self.repo_missing:
                raise gh.GhError(f"gh: HTTP 404: Not Found (.../{endpoint})\n")
            return "".join(n + "\n" for n in sorted(self._secret_names_for(None)))

        m = _REPO_SECRET_ONE_RE.match(endpoint)
        if m and method == "DELETE":
            name = m.group(2)
            if name in self.delete_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            self.secret_names = {n for n in self.secret_names if n.upper() != name.upper()}
            self.deleted_secrets.append((name, None))
            return ""

        m = _ENV_SECRET_ONE_RE.match(endpoint)
        if m and method == "DELETE":
            env, name = m.group(2), m.group(3)
            if name in self.delete_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            self.env_secret_names[env] = {
                n for n in self.env_secret_names.get(env, set()) if n.upper() != name.upper()
            }
            self.deleted_secrets.append((name, env))
            return ""

        if _ENVIRONMENTS_RE.match(endpoint) and jq == ".environments[].name":
            if self.repo_missing:
                raise gh.GhError(f"gh: HTTP 404: Not Found (.../{endpoint})\n")
            return "".join(n + "\n" for n in self.env_secret_names)

        if _BRANCHES_RE.match(endpoint):
            return "".join(n + "\n" for n in [self.default_branch, *self.branch_workflows])

        m = _WORKFLOWS_DIR_RE.match(endpoint)
        if m:
            ref = urllib.parse.unquote(m.group(2)) if m.group(2) else None
            if ref:
                files = [*(self.workflow_files or []), *self.branch_workflows[ref]]
                return "".join(f"{n} {self._blob(n, ref)}\n" for n in dict.fromkeys(files))
            self._workflow_reads["/"] = self._workflow_reads.get("/", 0) + 1
            files = self.workflow_files
            if self._workflow_reads["/"] >= 2 and self.workflow_files_after_recheck is not None:
                if self.workflow_files_after_recheck == "error":
                    raise gh.GhError("gh: HTTP 500: boom\n")
                files = self.workflow_files_after_recheck
            if files is None:
                raise gh.GhError(f"gh: HTTP 404: Not Found (.../{endpoint})\n")
            return "".join(f"{n} {self._blob(n, None)}\n" for n in files)

        m = _WORKFLOW_FILE_RE.match(endpoint)
        if m and jq == ".content":
            name, ref = m.group(2), (urllib.parse.unquote(m.group(3)) if m.group(3) else None)
            if ref:
                return base64.encodebytes(self._text(name, ref).encode()).decode()
            self._workflow_reads[name] = self._workflow_reads.get(name, 0) + 1
            if self._workflow_reads[name] >= 2 and name in self.workflow_texts_after_recheck:
                text = self.workflow_texts_after_recheck[name]
            else:
                text = self.workflow_texts.get(name, "")
            return base64.encodebytes(text.encode()).decode()

        if _ROOT_CONTENTS_RE.match(endpoint):
            if self.root_contents_error is not None:
                raise gh.GhError(self.root_contents_error)
            return "3\n"

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

        if _SCAFFOLD_TEMPLATE_COMMIT_RE.match(endpoint) and jq == ".sha":
            if self.template_resolve_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            return self.template_commit_sha + "\n"

        m = _SCAFFOLD_TEMPLATE_RE.match(endpoint)
        if m and jq == ".content":
            name, ref = m.group(1), m.group(2)
            if ref != self.template_commit_sha:
                raise AssertionError(
                    f"template fetch for {name} used ref={ref!r}, expected the resolved "
                    f"{self.template_commit_sha!r} (Codex review, mikelward/repo#14)"
                )
            if name in self.template_fetch_fails:
                raise gh.GhError(f"gh: HTTP 404: Not Found (.../{endpoint})\n")
            return base64.encodebytes(self.template_contents.get(name, "").encode()).decode()

        if _SCAFFOLD_ZIZMOR_RE.match(endpoint) and jq == ".content":
            if self.zizmor_fetch_fails:
                raise gh.GhError("gh: HTTP 404: Not Found (.../repos/mikelward/lanes)\n")
            return base64.encodebytes(self.zizmor_workflow_content.encode()).decode()

        m = _SCAFFOLD_REF_READ_RE.match(endpoint)
        if m and method is None and jq is None:
            if self.bootstrap_ref_missing:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            if self.bootstrap_ref_empty_409:
                raise gh.GhError("gh: HTTP 409: Git Repository is empty.\n")
            if self.bootstrap_ref_ambiguous_409:
                raise gh.GhError("gh: HTTP 409: Conflict\n")
            if self.bootstrap_ref_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            self._scaffold_ref_reads += 1
            # bootstrap_ref_sha_after_first_read models the branch moving
            # (a race, or a deliberate reset) between plan_gaps's own read
            # (the first) and apply_gaps's pre-PATCH recheck (the second
            # and any later one) -- None (the default) means every read
            # sees the same tip, matching every other test in this file.
            sha = self._scaffold_ref_current_sha
            if self._scaffold_ref_reads > 1 and self.bootstrap_ref_sha_after_first_read is not None:
                sha = self.bootstrap_ref_sha_after_first_read
            if self._scaffold_ref_patched and self.bootstrap_ref_sha_after_own_write is not None:
                sha = self.bootstrap_ref_sha_after_own_write
            return json.dumps({"object": {"sha": sha}})

        m = _SCAFFOLD_COMMIT_RE.match(endpoint)
        if m and method is None and jq is None:
            if self.bootstrap_commit_read_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            return json.dumps({"tree": {"sha": self.bootstrap_tree_sha}})

        m = _SCAFFOLD_TREE_RE.match(endpoint)
        if m:
            if self.bootstrap_tree_read_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            paths = self._all_scaffold_paths() if self.bootstrap_existing_paths is None else self.bootstrap_existing_paths
            # mode "100644" (a real, non-executable file) -- real GitHub
            # tree entries always carry one, and plan_gaps now checks it
            # (Codex review, mikelward/repo#14), so an entry missing it
            # would silently stop matching what the fake models as present.
            entries = [{"path": p, "type": "blob", "mode": "100644"} for p in paths]
            # bootstrap_occupied_entries lets a test plant a non-regular-
            # file (or an ancestor-of-a-scaffold-path) entry instead of /
            # alongside the blob one -- a bare kind string (e.g.
            # {".github/zizmor.yml": "tree"}, a directory sitting where the scaffold
            # file would go) or a (kind, mode) pair (e.g.
            # {".github/zizmor.yml": ("blob", "120000")}, a symlink).
            for path, kind in self.bootstrap_occupied_entries.items():
                kind, mode = kind if isinstance(kind, tuple) else (kind, None)
                entries = [e for e in entries if e["path"] != path]
                entry = {"path": path, "type": kind}
                if mode is not None:
                    entry["mode"] = mode
                entries.append(entry)
            return json.dumps({"tree": entries, "truncated": self.bootstrap_tree_truncated})

        raise AssertionError(f"unexpected endpoint: {endpoint} (method={method} jq={jq})")

    def _all_scaffold_paths(self):
        """Every path build_scaffold_files("main") produces -- kept as its
        own small, static list rather than calling the real function (that
        would make this fake's "already complete" default state depend on
        network access, defeating the point of a fake)."""
        return {f".github/workflows/{name}" for name in scaffold.TEMPLATE_FILES} | {
            ".github/workflows/zizmor.yml",
            ".github/zizmor.yml",
            ".github/lanes.conf",
            ".github/workflows/ci.yml",
        }

    def _text(self, name, ref):
        """A workflow's text on `ref` (None: the default branch)."""
        if ref and name in self.branch_workflows.get(ref, {}):
            return self.branch_workflows[ref][name]
        return self.workflow_texts.get(name, "")

    def _blob(self, name, ref):
        return hashlib.sha1(self._text(name, ref).encode()).hexdigest()

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
        endpoint, method, _jq = _parse_api_args(args[1:])
        body = json.loads(input_bytes.decode())
        if method == "PUT":
            self.puts.append((args, body))
            m = _RULESET_ONE_RE.match(endpoint)
            if m:
                # A successful PUT replaces the stored ruleset, so a later
                # read sees what was written -- which is what a re-read
                # right before deleting a duplicate is asking about
                # (Codex review, mikelward/repo#31). Modeling the write as
                # invisible would make that check compare the survivor's
                # PRE-write body and never delete anything.
                stored = dict(body)
                stored.setdefault("id", int(m.group(2)) if m.group(2).isdigit() else m.group(2))
                self.ruleset_objects[m.group(2)] = stored
            if _SCAFFOLD_CONTENTS_PUT_RE.match(endpoint):
                # push_initial_commit's own bootstrap write, for a
                # repository whose branch has no commits yet -- the one
                # write GitHub allows there, and the only PUT that parses
                # its own response body (`.commit.sha`).
                return json.dumps({"commit": {"sha": "bootstrapcommitsha"}}).encode()
        elif method == "POST":
            if _SCAFFOLD_BLOB_CREATE_RE.match(endpoint):
                if self.bootstrap_blob_fails:
                    raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
                self.posts.append((args, body))
                self._bootstrap_blob_seq += 1
                return json.dumps({"sha": f"blobsha{self._bootstrap_blob_seq}"}).encode()
            if _SCAFFOLD_TREE_CREATE_RE.match(endpoint):
                if self.bootstrap_tree_create_fails:
                    raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
                self.posts.append((args, body))
                return json.dumps({"sha": "newscaffoldtreesha"}).encode()
            if _SCAFFOLD_COMMIT_CREATE_RE.match(endpoint):
                if self.bootstrap_commit_create_fails:
                    raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
                self.posts.append((args, body))
                return json.dumps({"sha": "newscaffoldcommitsha"}).encode()
            self.posts.append((args, body))
        elif method == "PATCH":
            if _SCAFFOLD_REF_WRITE_RE.match(endpoint) and self.bootstrap_ref_update_fails:
                raise gh.GhError("gh: HTTP 422: Reference update failed\n")
            if self.patch_fails:
                raise gh.GhError("gh: HTTP 403: Must have admin rights to Repository.\n")
            self.patches.append((args, body))
            if _SCAFFOLD_REF_WRITE_RE.match(endpoint) and "sha" in body:
                self._scaffold_ref_current_sha = body["sha"]
                self._scaffold_ref_patched = True
            self.allow_auto_merge = "true" if body.get("allow_auto_merge") else self.allow_auto_merge
            self.delete_branch_on_merge = (
                "true" if body.get("delete_branch_on_merge") else self.delete_branch_on_merge
            )
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
        types = [rule["type"] for rule in body["rules"]]
        self.assertIn("required_linear_history", types)
        self.assertIn("non_fast_forward", types)
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

    def test_update_widens_the_scope_and_leaves_unmanaged_fields_alone(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "42"
        fake.all_ruleset_ids = ["42"]
        fake.ruleset_objects["42"] = {
            "id": 42,
            "name": "main",
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
                {"type": "required_linear_history"},
                {"type": "non_fast_forward"},
            ],
        }
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        body = fake.puts[0][1]
        # The scope is WIDENED, never replaced: whatever the ruleset
        # already covered it still covers, plus the hardened three. A
        # ruleset named 'main' that covers only some release branch is
        # not protecting the default branch at all, which is the whole
        # thing this step exists to do.
        self.assertEqual(
            body["conditions"]["ref_name"]["include"],
            ["refs/heads/release", *_HARDENED_SCOPE],
        )
        self.assertEqual(body["conditions"]["ref_name"]["exclude"], [])
        # target and bypass_actors are still untouched -- widening the
        # include list is the ONE thing an update rewrites outside `rules`.
        self.assertEqual(body["target"], "branch")
        self.assertEqual(body["bypass_actors"], [{"actor_id": 1, "actor_type": "Team"}])
        checks_rule = next(r for r in body["rules"] if r["type"] == "required_status_checks")
        self.assertEqual(
            checks_rule["parameters"]["required_status_checks"], [{"context": "lanes"}]
        )
        # An already-present required_linear_history/non_fast_forward rule
        # is left alone, not duplicated -- these two take no parameters, so
        # managing them is purely a presence check.
        types = [rule["type"] for rule in body["rules"]]
        self.assertEqual(types.count("required_linear_history"), 1)
        self.assertEqual(types.count("non_fast_forward"), 1)
        self.assertIn("now also targeting ~DEFAULT_BRANCH, refs/heads/main, refs/heads/master", out)
        self.assertIn("scope: also targeting ~DEFAULT_BRANCH", err)
        # A preserved bypass actor overrides every rule above, old and new
        # alike -- the plan says so rather than reading as an unqualified
        # guarantee. repo audit is what actually reports who they are. The
        # note lands in the combined plan setup_cmd.py prints to stderr
        # (the dry-run preview pass), not the real apply's own stdout.
        self.assertIn("1 bypass actor(s)", err)
        self.assertIn("repo audit", err)

    def _ruleset_with_scope(self, fake, include, exclude=()):
        """A minimal already-compliant ruleset differing only in scope, so
        a test asserting what the widening does isn't also asserting what
        the rule edits do."""
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "main",
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": list(include), "exclude": list(exclude)}},
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
                {"type": "required_linear_history"},
                {"type": "non_fast_forward"},
            ],
        }

    def test_a_partly_hardened_scope_gains_only_the_refs_it_lacks(self):
        # The common shape across the fleet: a hand-made ruleset naming
        # the literal default branch and nothing else, so refs/heads/
        # master -- the backdoor the wider targeting exists to close --
        # was never covered.
        fake = FakeGh()
        self._ruleset_with_scope(fake, ["refs/heads/main"])
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        self.assertEqual(
            fake.puts[0][1]["conditions"]["ref_name"]["include"],
            ["refs/heads/main", "~DEFAULT_BRANCH", "refs/heads/master"],
        )
        self.assertIn("now also targeting ~DEFAULT_BRANCH, refs/heads/master", out)

    def test_a_scope_of_all_branches_is_left_exactly_as_it_is(self):
        # ~ALL already covers every branch these three name, so appending
        # them would be noise -- and noise that rewrites somebody else's
        # ruleset for no gain. Nothing else about the fixture needs a
        # write, so this stays a genuine no-op.
        fake = FakeGh()
        self._ruleset_with_scope(fake, ["~ALL"])
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.puts, [])
        self.assertIn("already matches; nothing to do", out)

    def test_an_exclusion_that_defeats_the_widening_is_named(self):
        # This module never edits an exclusion -- deleting one somebody
        # wrote is a different decision from adding a ref to an include
        # list. So a ruleset excluding the very ref just added still
        # excludes it, and a plan that just said "also targeting
        # refs/heads/master" would promise coverage the write does not
        # deliver.
        fake = FakeGh()
        self._ruleset_with_scope(fake, ["~DEFAULT_BRANCH"], exclude=["refs/heads/master"])
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        plan = out + err
        self.assertIn("scope: also targeting refs/heads/main, refs/heads/master", plan)
        self.assertIn("excludes refs/heads/master, so it does not protect that branch", plan)
        self.assertEqual(fake.puts, [])

    def test_all_branches_with_one_excluded_is_not_reported_as_complete(self):
        # Codex review, mikelward/repo#31: ~ALL short-circuits the
        # widening because it already covers every branch -- but an
        # exclusion outranks an include, so a ruleset including ~ALL and
        # excluding refs/heads/master leaves master exactly as
        # unprotected as before. Nothing else here needs a write, so
        # without this the run reported "nothing to do" and exited 0 over
        # the very backdoor the widening exists to close.
        fake = FakeGh()
        self._ruleset_with_scope(fake, ["~ALL"], exclude=["refs/heads/master"])
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("already matches; nothing to do", out)
        self.assertIn("excludes refs/heads/master, so it does not protect that branch", err)
        self.assertIn("repo audit", err)

    def test_an_exclusion_of_the_default_branch_by_name_is_named_too(self):
        # An exclusion names a ref, never the ~DEFAULT_BRANCH token, so
        # the comparison resolves the token first -- otherwise excluding
        # the repository's actual default branch by name would read as
        # excluding nothing this cares about.
        fake = FakeGh()
        self._ruleset_with_scope(fake, list(_HARDENED_SCOPE), exclude=["refs/heads/main"])
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("already matches; nothing to do", out)
        self.assertIn("excludes refs/heads/main", err)

    def test_the_exclusion_finding_is_reported_once_not_once_per_pass(self):
        # setup_cmd previews the ruleset step and then applies it, so a
        # finding printed on both passes reads as two separate problems.
        # The real apply runs quiet (absent --verbose, which asks for the
        # full audit trail of both passes on purpose) for exactly this
        # reason.
        fake = FakeGh()
        self._ruleset_with_scope(fake, ["refs/heads/main"], exclude=["refs/heads/master"])
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        self.assertEqual((out + err).count("excludes refs/heads/master"), 1)

    def test_the_default_branch_token_in_the_exclusions_is_recognized(self):
        # An exclusion may itself be written ~DEFAULT_BRANCH, and
        # comparing that token against the resolved refs/heads/<default>
        # would miss a ruleset excluding the very branch this protects
        # (Codex review, mikelward/repo#31).
        fake = FakeGh()
        self._ruleset_with_scope(fake, list(_HARDENED_SCOPE), exclude=["~DEFAULT_BRANCH"])
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("already matches; nothing to do", out)
        # Named as written, not as resolved, so the ref reported is the
        # one to go looking for in the ruleset.
        self.assertIn("excludes ~DEFAULT_BRANCH", err)

    def test_excluding_all_branches_is_recognized_too(self):
        fake = FakeGh()
        self._ruleset_with_scope(fake, list(_HARDENED_SCOPE), exclude=["~ALL"])
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("excludes ~ALL", err)

    def test_a_pattern_in_the_exclusions_is_reported_as_unevaluated(self):
        # Literal matching only, here as everywhere else in this module:
        # a glob might or might not carve out a hardened ref, and
        # answering would mean reimplementing GitHub's ref matching.
        fake = FakeGh()
        self._ruleset_with_scope(fake, list(_HARDENED_SCOPE), exclude=["refs/heads/mast*"])
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("has a pattern in its exclusions", err)
        self.assertIn("Check it by hand", err)

    def test_widening_is_checked_against_the_merge_methods_on_the_refs_it_adds(self):
        # The scan that stops two rulesets from intersecting to no
        # permitted merge method has to see the scope the write will
        # PRODUCE, not the narrower one it is replacing: this widening is
        # exactly what brings refs/heads/master into range of the other
        # ruleset below, and evaluating the as-found scope would wave it
        # through and leave master unable to merge anything.
        fake = FakeGh()
        self._ruleset_with_scope(fake, ["refs/heads/main"])
        fake.all_ruleset_ids = ["7", "8"]
        fake.ruleset_objects["8"] = {
            "id": 8,
            "name": "no rebase on master",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/master"], "exclude": []}},
            "rules": [
                {"type": "pull_request", "parameters": {"allowed_merge_methods": ["squash"]}}
            ],
        }
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("no rebase on master", err)
        self.assertEqual(fake.puts, [])

    def test_no_bypass_actor_note_when_the_ruleset_has_none(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("bypass actor", err)

    def test_update_adds_linear_history_and_block_force_pushes_when_missing(self):
        # An existing ruleset created before this module managed these two
        # rule types gets them appended on its next update, same as a
        # missing required_status_checks/pull_request rule would.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "42"
        fake.all_ruleset_ids = ["42"]
        fake.ruleset_objects["42"] = {
            "id": 42,
            "name": "main",
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
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        types = [rule["type"] for rule in fake.puts[0][1]["rules"]]
        self.assertIn("required_linear_history", types)
        self.assertIn("non_fast_forward", types)

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
            "name": "main",
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
            "name": "main",
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
            "name": "main",
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
            "name": "main",
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
            "name": "main",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": list(_HARDENED_SCOPE), "exclude": []}},
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
                {"type": "required_linear_history"},
                {"type": "non_fast_forward"},
            ],
        }
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.puts, [])
        self.assertIn("already matches; nothing to do", out)

    def test_unchanged_ruleset_with_a_bypass_actor_still_notes_it(self):
        # Codex review: the no-op path returns before _describe_plan is
        # ever called, so a ruleset that already matches -- rules and
        # all -- but carries a bypass actor used to report "matches" with
        # no caveat at all, even though that actor can override every one
        # of those rules. Same fixture as the no-op test above, plus
        # bypass_actors.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "main",
            "enforcement": "active",
            "bypass_actors": [{"actor_id": 1, "actor_type": "Team"}],
            "conditions": {"ref_name": {"include": list(_HARDENED_SCOPE), "exclude": []}},
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
                {"type": "required_linear_history"},
                {"type": "non_fast_forward"},
            ],
        }
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.puts, [])
        self.assertIn("already matches; nothing to do", out)
        self.assertIn("1 bypass actor(s)", out)
        self.assertIn("repo audit", out)

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

    def test_an_unmanaged_rule_type_is_adopted_and_survives(self):
        # This used to be refused, on the reasoning that overwriting the
        # ruleset would delete the rule. An update never rebuilds the body
        # -- every existing rule is copied through and only the four
        # managed types are edited -- so refusing was guarding against a
        # write this module does not make, and it left exactly the
        # hand-made ruleset `repo setup` most needs to adopt sitting beside
        # a second one it created instead.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "3"
        fake.all_ruleset_ids = ["3"]
        fake.ruleset_objects["3"] = {
            "id": 3,
            "name": "main",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "commit_message_pattern", "parameters": {"pattern": "^x"}},
                {"type": "required_signatures"},
            ],
        }
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        written = fake.puts[0][1]
        by_type = {r["type"]: r for r in written["rules"]}
        # The unmanaged rules are still there, parameters and all.
        self.assertEqual(by_type["commit_message_pattern"]["parameters"], {"pattern": "^x"})
        self.assertIn("required_signatures", by_type)
        # ...and the fleet's four gates were added alongside them.
        for managed in rules.MANAGED_RULE_TYPES:
            self.assertIn(managed, by_type)

    def test_the_managed_set_matches_what_a_create_actually_writes(self):
        # MANAGED_RULE_TYPES is the module's stated contract -- the types it
        # writes, and by omission the ones it carries through untouched. It
        # is written down in one place and enforced in another (the if/elif
        # chain in _build_update_body), so pin them together: adding a rule
        # to the create body without adding it here would silently widen
        # what an update overwrites.
        created = {rule["type"] for rule in rules._create_body("main", ["lanes"])["rules"]}
        self.assertEqual(created, rules.MANAGED_RULE_TYPES)

    def test_a_tag_targeted_ruleset_of_the_same_name_is_refused(self):
        # Accepting extra rule types opened this: a tag ruleset holding
        # only required_signatures now passes the rule-type test, and
        # _build_update_body would preserve target "tag" while adding
        # branch-only rules -- so the dry run promises an update GitHub
        # rejects on PUT, after earlier steps have already written (Codex
        # review, mikelward/repo#29).
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "3"
        fake.all_ruleset_ids = ["3"]
        fake.ruleset_objects["3"] = {
            "id": 3,
            "name": "main",
            "target": "tag",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
            "rules": [{"type": "required_signatures"}],
        }
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("targets 'tag'", err)
        self.assertEqual(fake.puts, [])

    def test_a_legacy_named_ruleset_is_renamed_rather_than_rivalled(self):
        # The migration's whole point: `repo setup` created 'merge gates'
        # before the standard name settled. Creating a second ruleset and
        # leaving that one behind would strand its bypass actors and its
        # scope behind a ruleset that AND-s with the new one, so it is
        # adopted and renamed in the same write instead.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = None
        fake.legacy_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "merge gates",
            "enforcement": "active",
            "bypass_actors": [{"actor_id": 5, "actor_type": "Team"}],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [],
        }
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        self.assertEqual(fake.puts[0][0][3], f"repos/{REPO}/rulesets/7")
        self.assertEqual(fake.posts, [])
        written = fake.puts[0][1]
        self.assertEqual(written["name"], "main")
        self.assertEqual(written["bypass_actors"], [{"actor_id": 5, "actor_type": "Team"}])
        self.assertIn("adopted the ruleset named 'merge gates'", out)

    def test_the_plan_names_the_rename_rather_than_an_update(self):
        # With only the legacy ruleset present there is no 'main' to
        # update, so a plan saying so would name something that does not
        # exist and hide the rename.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = None
        fake.legacy_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "merge gates",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [],
        }
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        plan = out + err
        self.assertIn("would adopt ruleset 'merge gates' (id 7) and rename it 'main'", plan)
        self.assertNotIn("would update ruleset 'main'", plan)
        self.assertEqual(fake.puts, [])

    def _matching_pair(self, fake, legacy_rules=None, legacy_scope=None):
        """A repository carrying both the standard ruleset and a legacy-
        named one. They are identical unless a test says otherwise, which
        is the case worth deleting."""
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "1"
        fake.legacy_ruleset_id = "9"
        fake.all_ruleset_ids = ["1", "9"]
        rules_body = [
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
            {"type": "required_linear_history"},
            {"type": "non_fast_forward"},
        ]
        scope = {"ref_name": {"include": list(_HARDENED_SCOPE), "exclude": []}}
        fake.ruleset_objects["1"] = {
            "id": 1,
            "name": "main",
            "target": "branch",
            "enforcement": "active",
            "conditions": scope,
            "rules": rules_body,
        }
        fake.ruleset_objects["9"] = {
            "id": 9,
            "name": "merge gates",
            "target": "branch",
            "enforcement": "active",
            "conditions": legacy_scope if legacy_scope is not None else scope,
            "rules": legacy_rules if legacy_rules is not None else rules_body,
        }

    def test_an_identical_legacy_ruleset_is_deleted(self):
        # Rulesets aggregate, so a duplicate is not broken -- but
        # allowed_merge_methods INTERSECTS, so a pair that ever drifts
        # apart there leaves nothing able to merge at all. One identical
        # to the survivor removes nothing by going.
        fake = FakeGh()
        self._matching_pair(fake)
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, ["9"])
        # Nothing was written to the survivor: it already matched, and the
        # whole change was removing the duplicate.
        self.assertEqual(fake.puts, [])
        self.assertEqual(fake.posts, [])
        self.assertIn("deleted the superseded ruleset 'merge gates' (id 9)", out)

    def test_a_deletion_with_nothing_else_to_do_is_still_planned_and_confirmed(self):
        # The steady state this has to work in is an already-correct
        # ruleset, so a deletion that only ran on the write path would
        # never run at all. It is a mutation like any other: shown in
        # --dry-run, and asked about before it happens.
        fake = FakeGh()
        self._matching_pair(fake)
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        plan = out + err
        self.assertIn("would delete the superseded ruleset 'merge gates' (id 9)", plan)
        self.assertEqual(fake.deleted_rulesets, [])

        fake = FakeGh()
        self._matching_pair(fake)
        code, out, err = _run(fake, ["--rule", "lanes", REPO], isatty=False)
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(fake.deleted_rulesets, [])

    def test_two_rulesets_sharing_a_legacy_name_are_both_handled(self):
        # GitHub does not make a ruleset's name unique within a
        # repository, so two can carry the same one and both apply.
        # Handling only the first would let a run report the name dealt
        # with while the second kept aggregating (Codex review,
        # mikelward/repo#31).
        fake = FakeGh()
        self._matching_pair(fake)
        fake.legacy_ruleset_ids = ["9", "10"]
        fake.all_ruleset_ids = ["1", "9", "10"]
        fake.ruleset_objects["10"] = dict(fake.ruleset_objects["9"], id=10)
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(fake.deleted_rulesets), ["10", "9"])

    def test_a_second_ruleset_sharing_a_legacy_name_is_still_reported(self):
        # The kept half of the same case: neither is deleted, and both
        # are named rather than one standing in for the pair.
        fake = FakeGh()
        self._matching_pair(fake, legacy_rules=[{"type": "required_signatures"}])
        fake.legacy_ruleset_ids = ["9", "10"]
        fake.all_ruleset_ids = ["1", "9", "10"]
        fake.ruleset_objects["10"] = dict(fake.ruleset_objects["9"], id=10)
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, [])
        self.assertIn("(id 9) is still there", err)
        self.assertIn("(id 10) is still there", err)

    def test_a_legacy_ruleset_that_differs_is_reported_not_deleted(self):
        # The whole reason this is an equality test and not a
        # field-by-field "is the survivor at least as strict": an
        # unmanaged rule type the survivor does not carry would be lost,
        # and so would four other things each found only after the
        # previous was fixed (see TODO.md).
        fake = FakeGh()
        self._matching_pair(fake, legacy_rules=[{"type": "required_signatures"}])
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, [])
        self.assertIn("'merge gates' (id 9) is still there beside 'main'", err)
        self.assertIn("not identical", err)

    def test_a_legacy_ruleset_covering_a_ref_the_survivor_does_not_is_kept(self):
        # Scope is part of the comparison, not just the rules: a legacy
        # ruleset reaching a branch the standard one does not is
        # protecting something, whatever its rules say.
        fake = FakeGh()
        self._matching_pair(
            fake,
            legacy_scope={"ref_name": {"include": [*_HARDENED_SCOPE, "refs/heads/release"], "exclude": []}},
        )
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, [])
        self.assertIn("is still there beside 'main'", err)

    def test_a_legacy_ruleset_with_a_bypass_actor_the_survivor_lacks_is_kept(self):
        # Deleting this one would let that actor past every remaining
        # gate, which is the opposite of what removing a duplicate is
        # supposed to do.
        fake = FakeGh()
        self._matching_pair(fake)
        fake.ruleset_objects["9"]["bypass_actors"] = [{"actor_id": 5, "actor_type": "Team"}]
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, [])
        self.assertIn("is still there beside 'main'", err)

    def test_the_deletion_happens_after_the_write_that_makes_it_safe(self):
        # What makes the duplicate safe to delete is that the SURVIVOR
        # holds everything it held -- true only once this run's own write
        # has landed. Deleting first would leave a window, and a failed
        # write would leave the repository with neither.
        fake = FakeGh()
        self._matching_pair(fake)
        # The survivor is missing a rule the legacy one already has, so it
        # is identical to it only after this run's own write.
        fake.ruleset_objects["1"] = dict(fake.ruleset_objects["1"])
        fake.ruleset_objects["1"]["rules"] = [
            r for r in fake.ruleset_objects["1"]["rules"] if r["type"] != "non_fast_forward"
        ]
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        self.assertEqual(fake.deleted_rulesets, ["9"])
        put_index = next(
            i for i, c in enumerate(fake.calls) if "PUT" in c and f"repos/{REPO}/rulesets/1" in c
        )
        delete_index = next(
            i for i, c in enumerate(fake.calls) if "DELETE" in c and c[-1].endswith("/rulesets/9")
        )
        self.assertLess(put_index, delete_index)

    def test_a_duplicate_edited_during_the_write_is_not_deleted(self):
        # The plan reads the duplicate before the survivor's own write, so
        # the window between that read and the delete is a network round
        # trip wide -- an administrator editing the duplicate inside it
        # would otherwise have it deleted on a reading that no longer
        # holds (Codex review, mikelward/repo#31). Simulated by swapping
        # the duplicate's content in after its first read.
        fake = FakeGh()
        self._matching_pair(fake)
        fake.ruleset_objects["1"]["rules"] = [
            r for r in fake.ruleset_objects["1"]["rules"] if r["type"] != "non_fast_forward"
        ]
        changed = dict(fake.ruleset_objects["9"])
        changed["rules"] = [*changed["rules"], {"type": "required_signatures"}]
        fake.ruleset_objects_after_change["9"] = changed
        # The seventh read of the duplicate is the one _still_superseded
        # makes, right before the delete; everything before it -- the
        # plan, the merge-method scans, the fingerprint recompute -- sees
        # the unchanged object, so this lands in exactly the window the
        # earlier checks cannot cover.
        fake.ruleset_content_change_threshold = 6
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted_rulesets, [])
        self.assertIn("no longer identical", err)
        # The survivor's own write still happened -- the duplicate going
        # is the part that was unsafe, not the protection being written.
        self.assertEqual(len(fake.puts), 1)

    def test_a_failed_re_read_before_the_delete_keeps_the_duplicate(self):
        # "Could not tell" is not "unchanged": a read this cannot make
        # must never be the reason a ruleset is deleted.
        fake = FakeGh()
        self._matching_pair(fake)
        fake.ruleset_read_fails_after["9"] = 6
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted_rulesets, [])

    def test_a_failed_deletion_fails_the_step(self):
        fake = FakeGh()
        self._matching_pair(fake)
        fake.ruleset_delete_fails = {"9"}
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not delete the superseded ruleset 'merge gates'", err)

    def test_an_unreadable_legacy_ruleset_refuses_rather_than_guessing(self):
        fake = FakeGh()
        self._matching_pair(fake)
        del fake.ruleset_objects["9"]
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted_rulesets, [])

    def test_an_inactive_ruleset_is_still_refused(self):
        # The other half of the check, and the half that stays: a ruleset
        # GitHub does not enforce would report a gate that does not gate.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "3"
        fake.all_ruleset_ids = ["3"]
        fake.ruleset_objects["3"] = {
            "id": 3,
            "name": "main",
            "enforcement": "evaluate",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [],
        }
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("not", err)
        self.assertIn("'active'", err)
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

    def test_renamed_master_does_not_warn(self):
        # gh follows GitHub's 301 off a renamed master, so the lookup
        # succeeds with main's record -- not a master branch to warn about.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.master_redirect_name = "main"
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
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
        # The names themselves, not a repr of the records they arrive in.
        self.assertIn("'lanes'", err)
        self.assertNotIn("None", err)
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
        # result on its second (the real apply's own scan) -- distinct from
        # the existing tests above, which are missing from the START.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}

        real_collect_reported = rules._collect_reported
        calls = []

        def flaky_collect_reported(repo, wanted, ref=None):
            calls.append(1)
            if len(calls) >= 2:
                return set(), set()  # (names, app_pairs): nothing reported
            return real_collect_reported(repo, wanted, ref=ref)

        with patch("repo_lib.rules._collect_reported", side_effect=flaky_collect_reported):
            with patch("builtins.input", return_value="y"):
                code, _, err = _run(fake, ["--rule", "lanes", REPO], isatty=True)
        self.assertEqual(code, 1)
        self.assertIn("failed on:", err)
        self.assertIn("ruleset", err)
        self.assertIn("never reported", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.puts, [])

    def test_force_does_not_print_the_full_plan_by_default(self):
        # --force skips the confirmation QUESTION and, by default, the
        # full audit trail too -- notably a secret's own OVERWRITES-an-
        # existing-value warning: a forced/unattended run over a fleet
        # stays quiet unless it changed something (see the real "set
        # 'TOKEN'" print below) or -v asked for the full plan.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.secret_names = {"TOKEN"}  # already exists -> OVERWRITES warning
            code, out, err = _run(fake, ["--force", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("OVERWRITES an existing value", err)
        self.assertIn(f"{REPO}: set 'TOKEN'", out)

    def test_verbose_prints_the_full_plan_even_under_force(self):
        # -v restores the full audit trail -- notably a secret's own
        # OVERWRITES-an-existing-value warning -- even though --force
        # means there's no question left to answer about it.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.secret_names = {"TOKEN"}  # already exists -> OVERWRITES warning
            code, _, err = _run(fake, ["--force", "-v", "--secret", f"TOKEN={path}", REPO])
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
            "name": "main",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": list(_HARDENED_SCOPE), "exclude": []}},
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
                {"type": "required_linear_history"},
                {"type": "non_fast_forward"},
            ],
        }
        code, out, err = _run(fake, ["--rule", "lanes", "-v", REPO], isatty=False)
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
            "name": "main",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": list(_HARDENED_SCOPE), "exclude": []}},
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
                {"type": "required_linear_history"},
                {"type": "non_fast_forward"},
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
            "name": "main",
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
        code, out, err = _run(fake, ["--no-rules", "--app", "codex", "-v", REPO], isatty=False)
        self.assertEqual(code, 0, err)
        self.assertNotIn("stdin is not a terminal", err)
        # -v shows the plan (including "already a member") unconditionally
        # -- see setup_cmd.run()'s show_plan comment.
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
        # Required checks collapse onto one line, not one line each.
        self.assertIn("required checks: lanes, codex, zizmor", out)

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


class VerbosityTest(unittest.TestCase):
    """Quiet by default (only what changed); -v restores the full plan
    audit trail and per-step progress markers. See _progress's docstring
    and the show_plan comment in setup_cmd.run()."""

    def test_quiet_by_default_shows_only_what_changed(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        # What changed: on stdout, unconditionally.
        self.assertIn(f"{REPO}: created ruleset", out)
        # The full plan audit trail: not shown by default.
        self.assertNotIn("ruleset (repo-rules):", err)
        self.assertNotIn("would create ruleset", err)
        self.assertNotIn("checking rules", err)

    def test_verbose_shows_the_full_plan_and_progress_markers(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", "-v", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: created ruleset", out)
        self.assertIn("ruleset (repo-rules):", err)
        self.assertIn(f"{REPO}: checking rules", err)
        self.assertIn(f"{REPO}: checking fleet credentials", err)
        self.assertIn(f"{REPO}: checking auto-merge", err)
        self.assertIn(f"{REPO}: checking the fleet CI scaffold", err)

    def test_verbose_long_flag_is_equivalent_to_short(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", "--verbose", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("ruleset (repo-rules):", err)

    def test_progress_markers_for_secrets_and_apps_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.installations = [("codex", "111", "selected", "owner")]
            code, _, err = _run(
                fake, ["--force", "-v", "--secret", f"TOKEN={path}", "--app", "codex", REPO]
            )
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: checking secrets", err)
        self.assertIn(f"{REPO}: checking App installation membership", err)

        fake2 = FakeGh()
        fake2.check_runs = {fake2.default_head_sha: ["lanes", "codex", "zizmor"]}
        code2, _, err2 = _run(fake2, ["--force", "-v", REPO])
        self.assertEqual(code2, 0, err2)
        self.assertNotIn("checking secrets", err2)
        self.assertNotIn("checking App installation", err2)


class CredentialsStepTest(unittest.TestCase):
    """The fleet-credentials step: always on, it sets a supplied credential
    in the environment it belongs in, deletes the copies that leaves
    redundant and the ones nothing uses, and refuses to act under a caller
    that still names its secrets. The combined plan goes to stderr (as the
    rest of setup's does); a dry run prints it to stdout."""

    INHERITING = (
        "jobs:\n"
        "  update:\n"
        "    uses: mikelward/gradle-update/.github/workflows/gradle-update.yml@main\n"
        "    permissions:\n"
        "      contents: write\n"
        "    # the reason\n"
        "    secrets: inherit\n"
        "    with:\n"
        "      extra-repositories: ''\n"
    )
    NAMING = INHERITING.replace(
        "    secrets: inherit\n", "    secrets:\n      token: ${{ secrets.GRADLE_UPDATE_PAT }}\n"
    )
    RUST_INHERITING = INHERITING.replace("gradle-update", "rust-update")
    SYNC_INHERITING = (
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: mikelward/lanes@main\n"
        "  sync:\n"
        "    needs: build\n"
        "    uses: mikelward/ci-commit-artifact/.github/workflows/commit-artifact.yml@main\n"
        "    with:\n"
        "      artifact-name: rendered\n"
        "    secrets: inherit\n"
    )
    SYNC_NAMING = SYNC_INHERITING.replace(
        "    secrets: inherit\n", "    secrets:\n      push-token: ${{ secrets.CI_COMMIT_ARTIFACT_TOKEN }}\n"
    )

    def _consumer(self, text=INHERITING, hub="gradle-update"):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", f"{hub}.yml"]
        fake.workflow_texts = {f"{hub}.yml": text}
        return fake

    def _order(self, fake):
        """Indexes of the environment creation, the first secret write and
        the first delete in the fake's call log (None where absent)."""

        def first(predicate):
            return next((i for i, c in enumerate(fake.calls) if predicate(c)), None)

        return (
            first(lambda c: c[1:3] == ["--method", "PUT"] and "/environments/" in c[3]),
            first(lambda c: c[:2] == ["secret", "set"]),
            first(lambda c: c[1:3] == ["--method", "DELETE"]),
        )

    def test_a_repository_in_shape_needs_nothing(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        # Nothing to plan, so the no-rules early return still applies:
        # no plan printed, no confirmation asked of a non-terminal.
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        # Alongside another step, the section says so.
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("  fleet credentials:\n    nothing to do\n", out)
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_supplied_credential_moves_into_the_hub_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt", b"ghp_example")
            fake = self._consumer()
            fake.secret_names = {"GRADLE_UPDATE_PAT"}
            code, out, err = _run(
                fake, ["--force", "-v", "--no-rules", "--credential", f"GRADLE_UPDATE_PAT={path}", REPO]
            )
        self.assertEqual(code, 0, err)
        self.assertIn("gradle-update: set GRADLE_UPDATE_PAT in environment 'gradle-update' (new)", err)
        self.assertIn(
            "gradle-update: delete repository secret GRADLE_UPDATE_PAT -- the 'gradle-update' "
            "environment holds the credential once set",
            err,
        )
        self.assertEqual(
            fake.written_secrets, [("GRADLE_UPDATE_PAT", REPO, "gradle-update", b"ghp_example")]
        )
        self.assertEqual(fake.deleted_secrets, [("GRADLE_UPDATE_PAT", None)])
        # The environment is created before the write, and the repository
        # copy deleted only after it: the copy is what keeps the batch
        # working until the environment holds the credential.
        env_put, write, delete = self._order(fake)
        self.assertIsNotNone(env_put)
        self.assertLess(env_put, write)
        self.assertLess(write, delete)
        self.assertIn(f"{REPO}: deleted 'GRADLE_UPDATE_PAT'", out)

    def test_a_repository_copy_is_deleted_once_the_environment_already_holds_one(self):
        fake = self._consumer()
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "-v", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "gradle-update: delete repository secret GRADLE_UPDATE_PAT -- the 'gradle-update' "
            "environment holds the credential\n",
            err,
        )
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [("GRADLE_UPDATE_PAT", None)])

    def test_a_caller_with_the_yaml_extension_is_read_as_the_batch(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "gradle-update.yaml"]
        fake.workflow_texts = {"gradle-update.yaml": self.INHERITING}
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_secrets, [("GRADLE_UPDATE_PAT", None)])
        self.assertNotIn("nothing uses it", err)

    def test_every_caller_file_of_a_batch_must_inherit(self):
        # `<hub>.yml` and `<hub>.yaml` can both exist, and GitHub runs both;
        # the one that names its secrets would be stranded by the delete the
        # other justifies.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "gradle-update.yml", "gradle-update.yaml"]
        fake.workflow_texts = {"gradle-update.yml": self.INHERITING, "gradle-update.yaml": self.NAMING}
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("gradle-update: gradle-update passes its secrets by name", out + err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_second_caller_added_while_the_plan_waited_keeps_the_repository_copy(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "gradle-update.yml"]
        fake.workflow_texts = {"gradle-update.yml": self.INHERITING, "gradle-update.yaml": self.NAMING}
        fake.workflow_files_after_recheck = ["ci.yml", "gradle-update.yml", "gradle-update.yaml"]
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "GRADLE_UPDATE_PAT kept: the callers changed since the plan was built "
            "(gradle-update, gradle-update now)",
            err,
        )
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_caller_naming_the_repository_in_another_case_is_the_caller(self):
        # Owner and repository names are case-insensitive on GitHub, so
        # this is the commit-back workflow's caller and its token is in
        # use: the repository copy moves, the environment copy stays.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        assert "mikelward/ci-commit-artifact" in self.SYNC_INHERITING
        fake.workflow_texts = {
            "ci.yml": self.SYNC_INHERITING.replace("mikelward/ci-commit-artifact", "MikelWard/CI-Commit-Artifact")
        }
        fake.secret_names = {"CI_COMMIT_ARTIFACT_TOKEN"}
        fake.env_secret_names = {"ci-commit-artifact": {"CI_COMMIT_ARTIFACT_TOKEN"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_secrets, [("CI_COMMIT_ARTIFACT_TOKEN", None)])

    def test_a_caller_under_another_name_holds_the_move_back(self):
        # GitHub runs a workflow whatever it is named; a second caller that
        # names its secrets would be stranded by the delete the named
        # caller justifies.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "gradle-update.yml", "weekly.yml"]
        fake.workflow_texts = {"gradle-update.yml": self.INHERITING, "weekly.yml": self.NAMING}
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("gradle-update: weekly passes its secrets by name", out + err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_credential_removed_from_the_environment_while_the_plan_waited_keeps_the_copy(self):
        # The plan deletes the repository copy because the environment
        # already holds the credential; by apply time it does not.
        fake = self._consumer()
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        fake.env_secret_names_after_recheck = {"gradle-update": set()}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "GRADLE_UPDATE_PAT kept: the 'gradle-update' environment no longer holds the credential", err
        )
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_supplied_credential_nothing_uses_is_still_reported(self):
        # Nothing to move and no ruleset step used to take the silent early
        # return, leaving no trace of the value the user handed in.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            fake = FakeGh()
            fake.workflow_files = ["ci.yml"]
            code, out, err = _run(
                fake, ["--no-rules", "--credential", f"NPM_UPDATE_PAT={path}", REPO], isatty=False
            )
        self.assertEqual(code, 0, err)
        self.assertIn(
            "npm-update: NPM_UPDATE_PAT not set -- no workflow here calls mikelward/npm-update, "
            "so nothing uses it",
            out + err,
        )
        self.assertEqual(fake.written_secrets, [])

    def test_a_second_caller_the_reader_cannot_resolve_in_a_read_file_holds_the_move_back(self):
        fake = self._consumer(
            self.INHERITING
            + "  weekly: {uses: mikelward/gradle-update/.github/workflows/gradle-update.yml@main, "
            "secrets: {token: x}}\n"
        )
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        # "not fixed: ..." is Apply's own unconditional report of a real
        # failure -- printed whether or not --verbose showed the plan too.
        self.assertIn(
            "not fixed: gradle-update: gradle-update mentions mikelward/gradle-update/ in a shape "
            "this cannot read as a caller",
            err,
        )
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_caller_on_another_branch_is_read_too(self):
        # A push to `feature` runs its workflows from `feature`, so a caller
        # that exists only there still needs the credential: not unused,
        # and held to the same `inherit` test as one on the default branch.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.branch_workflows = {"feature": {"ci.yml": "jobs: {}\n", "weekly.yml": self.NAMING}}
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("gradle-update: weekly on feature passes its secrets by name", out + err)
        self.assertEqual(fake.deleted_secrets, [])
        # The unchanged ci.yml is not re-read on the branch: same blob.
        self.assertFalse(any("ci.yml?ref=feature" in " ".join(c) for c in fake.calls))
        # A branch name with URL metacharacters reaches the API encoded;
        # sent raw, `feature/x#1` would read as `feature/x`.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.branch_workflows = {"feature/x": {}, "feature/x#1": {"weekly.yml": self.NAMING}}
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("gradle-update: weekly on feature/x#1 passes its secrets by name", out + err)
        self.assertTrue(any("?ref=feature%2Fx%231" in " ".join(c) for c in fake.calls))
        self.assertEqual(fake.deleted_secrets, [])
        # Inheriting there, the caller lets the move go ahead.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.branch_workflows = {"feature": {"weekly.yml": self.INHERITING}}
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_secrets, [("GRADLE_UPDATE_PAT", None)])
        self.assertNotIn("nothing uses it", out + err)

    def test_a_credential_already_in_place_is_left_alone(self):
        fake = self._consumer()
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("gradle-update: the credential lives in the 'gradle-update' environment", out)
        self.assertNotIn("NOT FIXED", out)

    def test_without_a_value_the_move_is_reported_as_not_fixed(self):
        fake = self._consumer()
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: gradle-update: environment 'gradle-update' holds no credential -- pass "
            "--credential GRADLE_UPDATE_PAT=PATH to set one; GRADLE_UPDATE_PAT stays a "
            "repository secret until then",
            err,
        )
        self.assertIn("failed on: credential:gradle-update", err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_caller_naming_its_secrets_blocks_the_move(self):
        # An environment secret reaches a called workflow only through
        # `secrets: inherit`; a caller still passing it by name would be
        # handed nothing once the repository copy is gone.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            fake = self._consumer(self.NAMING)
            fake.secret_names = {"GRADLE_UPDATE_PAT"}
            code, out, err = _run(
                fake, ["--force", "--no-rules", "--credential", f"GRADLE_UPDATE_PAT={path}", REPO]
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: gradle-update: gradle-update passes its secrets by name, so a "
            "credential in the 'gradle-update' environment would never reach it -- convert the "
            "caller to `secrets: inherit` first; GRADLE_UPDATE_PAT left as is",
            err,
        )
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_caller_naming_its_secrets_keeps_the_repository_copy(self):
        # Even with the environment already holding one: the copy is the
        # one the caller actually passes.
        fake = self._consumer(self.NAMING)
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("passes its secrets by name", err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_caller_naming_its_secrets_leaves_a_scoped_credential_idle(self):
        # Nothing to write or delete, but the credential the environment
        # holds can never reach a caller that names its secrets -- the
        # batch runs as GITHUB_TOKEN, quietly. Not "in shape".
        fake = self._consumer(self.NAMING)
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: gradle-update: gradle-update passes its secrets by name, so a "
            "credential in the 'gradle-update' environment would never reach it -- convert the "
            "caller to `secrets: inherit` first\n",
            err,
        )
        self.assertNotIn("left as is", err)
        self.assertNotIn("lives in the", err)

    def test_a_caller_changed_while_the_plan_waited_keeps_the_repository_copy(self):
        # The plan saw an inheriting caller; by apply time it names its
        # secrets again. The write would be idle and the delete would
        # strand the batch, so the move is re-checked before either.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            fake = self._consumer()
            fake.workflow_texts_after_recheck = {"gradle-update.yml": self.NAMING}
            fake.secret_names = {"GRADLE_UPDATE_PAT"}
            code, out, err = _run(
                fake, ["--force", "--no-rules", "--credential", f"GRADLE_UPDATE_PAT={path}", REPO]
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "GRADLE_UPDATE_PAT kept: gradle-update no longer passes `secrets: inherit`", err
        )
        self.assertIn(
            "GRADLE_UPDATE_PAT not set: gradle-update no longer passes `secrets: inherit`", err
        )
        self.assertIn("failed on: credential:gradle-update", err)
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [])
        self.assertIn("GRADLE_UPDATE_PAT", fake.secret_names)

    def test_a_move_with_nothing_to_delete_is_re_validated_too(self):
        # A new credential with no repository copy is writes only; the
        # caller going back to naming its secrets makes that write idle,
        # which is not a success.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            fake = self._consumer()
            fake.workflow_texts_after_recheck = {"gradle-update.yml": self.NAMING}
            code, out, err = _run(
                fake, ["--force", "--no-rules", "--credential", f"GRADLE_UPDATE_PAT={path}", REPO]
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "GRADLE_UPDATE_PAT not set: gradle-update no longer passes `secrets: inherit`", err
        )
        self.assertIn("failed on: credential:gradle-update", err)
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_caller_added_while_the_plan_waited_keeps_a_stale_copy(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_files_after_recheck = ["ci.yml", "npm-update.yml"]
        fake.workflow_texts = {"npm-update.yml": self.INHERITING.replace("gradle-update", "npm-update")}
        fake.secret_names = {"NPM_UPDATE_PAT"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("NPM_UPDATE_PAT kept: npm-update appeared since the plan was built", err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_delete_that_cannot_be_re_validated_does_not_happen(self):
        fake = self._consumer()
        fake.workflow_files_after_recheck = "error"
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            f"GRADLE_UPDATE_PAT kept: could not list {REPO}'s workflows (the plan could not be re-validated)",
            err,
        )
        self.assertEqual(fake.deleted_secrets, [])

    def test_the_commit_back_token_delete_is_re_validated_too(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": self.SYNC_INHERITING}
        fake.workflow_texts_after_recheck = {"ci.yml": self.SYNC_NAMING}
        fake.secret_names = {"CI_COMMIT_ARTIFACT_TOKEN"}
        fake.env_secret_names = {"ci-commit-artifact": {"CI_COMMIT_ARTIFACT_TOKEN"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("CI_COMMIT_ARTIFACT_TOKEN kept: ci no longer passes `secrets: inherit`", err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_caller_file_that_calls_nothing_is_no_caller(self):
        # What a workflow calls decides, not what it is named: a
        # `gradle-update.yml` with no job calling the batch means the batch
        # does not run here, so the value is left unset and the repository
        # copy goes as unused.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            fake = self._consumer("name: gradle update\non: push\njobs: {}\n")
            fake.secret_names = {"GRADLE_UPDATE_PAT"}
            code, out, err = _run(
                fake, ["--force", "--no-rules", "--credential", f"GRADLE_UPDATE_PAT={path}", REPO]
            )
        self.assertEqual(code, 0, err)
        self.assertIn(
            "gradle-update: GRADLE_UPDATE_PAT not set -- no workflow here calls mikelward/gradle-update, "
            "so nothing uses it",
            err,
        )
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [("GRADLE_UPDATE_PAT", None)])

    def test_stale_credentials_are_deleted_wherever_they_sit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            fake = FakeGh()
            fake.workflow_files = ["ci.yml"]
            fake.secret_names = {"NPM_UPDATE_PAT", "OTHER"}
            fake.env_secret_names = {"rust-update": {"RUST_UPDATE_PAT", "KEEP"}, "lanes": {"LANES_TOKEN"}}
            code, out, err = _run(
                fake, ["--force", "-v", "--no-rules", "--credential", f"NPM_UPDATE_PAT={path}", REPO]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            fake.deleted_secrets, [("NPM_UPDATE_PAT", None), ("RUST_UPDATE_PAT", "rust-update")]
        )
        self.assertEqual(fake.written_secrets, [])
        self.assertIn(
            "npm-update: delete repository secret NPM_UPDATE_PAT -- no workflow here calls "
            "mikelward/npm-update, so nothing uses it",
            err,
        )
        self.assertIn(
            "rust-update: delete RUST_UPDATE_PAT from environment 'rust-update' -- no workflow "
            "here calls mikelward/rust-update, so nothing uses it",
            err,
        )
        # The supplied value is not set on a repository that runs no such
        # batch -- that is the whole difference from --secret.
        self.assertIn("npm-update: NPM_UPDATE_PAT not set -- no workflow here calls mikelward/npm-update", err)
        self.assertIn("OTHER", fake.secret_names)
        self.assertIn("KEEP", fake.env_secret_names["rust-update"])
        self.assertEqual(fake.env_secret_names["lanes"], {"LANES_TOKEN"})

    def test_the_commit_back_token_moves_like_a_batch_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "token.txt", b"github_pat_example")
            fake = FakeGh()
            fake.workflow_files = ["ci.yml"]
            fake.workflow_texts = {"ci.yml": self.SYNC_INHERITING}
            fake.secret_names = {"CI_COMMIT_ARTIFACT_TOKEN"}
            code, out, err = _run(
                fake,
                ["--force", "-v", "--no-rules", "--credential", f"CI_COMMIT_ARTIFACT_TOKEN={path}", REPO],
            )
        self.assertEqual(code, 0, err)
        self.assertIn(
            "ci-commit-artifact: set CI_COMMIT_ARTIFACT_TOKEN in environment 'ci-commit-artifact' (new)",
            err,
        )
        self.assertEqual(
            fake.written_secrets,
            [("CI_COMMIT_ARTIFACT_TOKEN", REPO, "ci-commit-artifact", b"github_pat_example")],
        )
        self.assertEqual(fake.deleted_secrets, [("CI_COMMIT_ARTIFACT_TOKEN", None)])

    def test_the_commit_back_token_stays_while_any_caller_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "token.txt")
            fake = FakeGh()
            fake.workflow_files = ["ci.yml", "nightly.yml"]
            fake.workflow_texts = {"ci.yml": self.SYNC_INHERITING, "nightly.yml": self.SYNC_NAMING}
            fake.secret_names = {"CI_COMMIT_ARTIFACT_TOKEN"}
            code, out, err = _run(
                fake,
                ["--force", "--no-rules", "--credential", f"CI_COMMIT_ARTIFACT_TOKEN={path}", REPO],
            )
        self.assertEqual(code, 1)
        self.assertIn("not fixed: ci-commit-artifact: nightly passes its secrets by name", err)
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_mention_the_reader_cannot_parse_blocks_the_stale_delete(self):
        # Flow style (or any shape the fleet does not write) is not a
        # caller the reader can see, but it IS a mention -- and "unused"
        # must mean absent from the text, not unparsed. Nothing deleted.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "batch.yml"]
        fake.workflow_texts = {
            "ci.yml": "jobs: {sync: {uses: mikelward/ci-commit-artifact/.github/workflows/commit-artifact.yml@main, secrets: inherit}}\n",
            "batch.yml": "jobs: {update: {uses: mikelward/npm-update/.github/workflows/npm-update.yml@main, secrets: inherit}}\n",
        }
        fake.secret_names = {"CI_COMMIT_ARTIFACT_TOKEN", "NPM_UPDATE_PAT"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: ci-commit-artifact: ci mentions mikelward/ci-commit-artifact/ in a shape "
            "this cannot read as a caller -- whether it is used there cannot be told, so nothing is deleted",
            err,
        )
        self.assertIn(
            "not fixed: npm-update: batch mentions mikelward/npm-update/ in a shape this cannot "
            "read as a caller",
            err,
        )
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.secret_names, {"CI_COMMIT_ARTIFACT_TOKEN", "NPM_UPDATE_PAT"})

    def test_a_mention_appearing_while_the_plan_waited_keeps_a_stale_copy(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs:\n  build:\n    steps:\n      - run: true\n"}
        fake.workflow_texts_after_recheck = {
            "ci.yml": "jobs: {sync: {uses: mikelward/ci-commit-artifact/.github/workflows/commit-artifact.yml@main}}\n"
        }
        fake.secret_names = {"CI_COMMIT_ARTIFACT_TOKEN"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "CI_COMMIT_ARTIFACT_TOKEN kept: a workflow now mentions mikelward/ci-commit-artifact/", err
        )
        self.assertEqual(fake.deleted_secrets, [])

    def test_an_unused_commit_back_token_is_stale(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs:\n  build:\n    steps:\n      - uses: mikelward/lanes@main\n"}
        fake.secret_names = {"CI_COMMIT_ARTIFACT_TOKEN"}
        fake.env_secret_names = {"ci-commit-artifact": {"CI_COMMIT_ARTIFACT_TOKEN"}}
        code, out, err = _run(fake, ["--force", "-v", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "ci-commit-artifact: delete repository secret CI_COMMIT_ARTIFACT_TOKEN -- no workflow "
            "here calls mikelward/ci-commit-artifact, so nothing uses it",
            err,
        )
        self.assertEqual(
            fake.deleted_secrets,
            [("CI_COMMIT_ARTIFACT_TOKEN", None), ("CI_COMMIT_ARTIFACT_TOKEN", "ci-commit-artifact")],
        )

    def test_half_an_app_pair_in_the_environment_asks_for_the_other_half(self):
        fake = self._consumer(self.RUST_INHERITING, hub="rust-update")
        fake.secret_names = {"RUST_UPDATE_APP_PRIVATE_KEY"}
        fake.env_secret_names = {"rust-update": {"RUST_UPDATE_APP_ID"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "pass --credential RUST_UPDATE_APP_PRIVATE_KEY=PATH to set one; "
            "RUST_UPDATE_APP_PRIVATE_KEY stays a repository secret until then",
            err,
        )
        self.assertEqual(fake.deleted_secrets, [])
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "key.pem", b"-----BEGIN EXAMPLE-----")
            fake = self._consumer(self.RUST_INHERITING, hub="rust-update")
            fake.secret_names = {"RUST_UPDATE_APP_PRIVATE_KEY"}
            fake.env_secret_names = {"rust-update": {"RUST_UPDATE_APP_ID"}}
            code, out, err = _run(
                fake,
                ["--force", "--no-rules", "--credential", f"RUST_UPDATE_APP_PRIVATE_KEY={path}", REPO],
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            fake.written_secrets,
            [("RUST_UPDATE_APP_PRIVATE_KEY", REPO, "rust-update", b"-----BEGIN EXAMPLE-----")],
        )
        self.assertEqual(fake.deleted_secrets, [("RUST_UPDATE_APP_PRIVATE_KEY", None)])
        # The environment already existed, so it was not re-PUT (which
        # would reset its protection settings).
        env_put, write, delete = self._order(fake)
        self.assertIsNone(env_put)

    def test_an_environment_listed_in_another_case_is_written_under_that_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            fake = self._consumer()
            fake.secret_names = {"gradle_update_pat"}
            fake.env_secret_names = {"Gradle-Update": set()}
            code, out, err = _run(
                fake, ["--force", "--no-rules", "--credential", f"gradle_update_pat={path}", REPO]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.written_secrets, [("GRADLE_UPDATE_PAT", REPO, "Gradle-Update", b"sekrit")])
        self.assertEqual(fake.deleted_secrets, [("GRADLE_UPDATE_PAT", None)])
        self.assertEqual(fake.secret_names, set())

    def test_a_failed_write_keeps_the_repository_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            fake = self._consumer()
            fake.secret_names = {"GRADLE_UPDATE_PAT"}
            fake.set_fails = {"GRADLE_UPDATE_PAT"}
            code, out, err = _run(
                fake, ["--force", "--no-rules", "--credential", f"GRADLE_UPDATE_PAT={path}", REPO]
            )
        self.assertEqual(code, 1)
        self.assertIn("GRADLE_UPDATE_PAT kept: the write it waited on failed", err)
        self.assertIn("failed on: credential:GRADLE_UPDATE_PAT", err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_failed_delete_fails_the_run(self):
        fake = self._consumer()
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        fake.delete_fails = {"GRADLE_UPDATE_PAT"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not delete 'GRADLE_UPDATE_PAT' from {REPO}:", err)
        self.assertIn("failed on: credential:GRADLE_UPDATE_PAT", err)

    def test_a_failed_read_fails_this_step_alone(self):
        # A hidden workflows directory (a token without Contents access
        # reads as 404, and so does the root listing) is not an answer, so
        # nothing is deleted as stale -- but a --secret asked for in the
        # same run still goes through, as every other step does past a
        # failed one.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.secret_names = {"NPM_UPDATE_PAT"}
            fake.root_contents_error = "gh: HTTP 404: Not Found\n"
            code, out, err = _run(fake, ["--force", "--no-rules", "--secret", f"TOKEN@lanes={path}", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not tell whether {REPO} has workflows", err)
        self.assertIn("failed on: credentials", err)
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.written_secrets, [("TOKEN", REPO, "lanes", b"sekrit")])

    def test_a_dry_run_shows_the_plan_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            fake = self._consumer()
            fake.secret_names = {"GRADLE_UPDATE_PAT"}
            code, out, err = _run(
                fake,
                ["--dry-run", "--no-rules", "--credential", f"GRADLE_UPDATE_PAT={path}", REPO],
            )
            self.assertEqual(code, 0, err)
            self.assertIn("gradle-update: set GRADLE_UPDATE_PAT in environment 'gradle-update' (new)", out)
            self.assertEqual(fake.written_secrets, [])
            self.assertEqual(fake.deleted_secrets, [])
            # A NOT FIXED line is an exit 1 in a dry run too, as the real
            # run's would be.
            fake = self._consumer()
            fake.secret_names = {"GRADLE_UPDATE_PAT"}
            code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("NOT FIXED: gradle-update:", out)

    def test_a_delete_needs_confirmation(self):
        fake = self._consumer()
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_fleet_credential_under_secret_is_refused(self):
        # --secret NPM_UPDATE_PAT=PATH writes the repository copy this step
        # removes, and the plan was built before that write, so the copy
        # would survive a clean exit; in its own environment it is a write
        # the plan does not know about either, so the step would still
        # report the credential missing and keep the repository copy.
        # Refused up front whatever scope it names, with the flag that does
        # the whole move.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            for spec in [
                f"NPM_UPDATE_PAT={path}",
                f"npm_update_pat={path}",
                f"NPM_UPDATE_PAT@production={path}",
                f"NPM_UPDATE_PAT@npm-update={path}",
            ]:
                for extra in ([], ["--credential", f"NPM_UPDATE_PAT={path}"]):
                    fake = FakeGh()
                    code, out, err = _run(fake, ["--force", "--no-rules", "--secret", spec, *extra, REPO])
                    self.assertEqual(code, 2, (spec, extra, err))
                    self.assertIn("names the fleet credential NPM_UPDATE_PAT", err)
                    self.assertIn("Use --credential NPM_UPDATE_PAT=PATH instead", err)
                    self.assertEqual(fake.calls, [])

    def test_credential_specs_are_validated_up_front(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            empty = _secret_file(tmp, "empty.txt", b"")
            cases = [
                ([f"RANDOM_TOKEN={path}"], "is not a fleet credential"),
                ([f"GRADLE_UPDATE_PAT{path}"], "missing '='"),
                ([f"={path}"], "empty NAME"),
                (["GRADLE_UPDATE_PAT="], "empty PATH"),
                ([f"GRADLE_UPDATE_PAT={path}", f"gradle_update_pat={path}"], "repeats an earlier"),
                ([f"GRADLE_UPDATE_PAT={tmp}/missing.txt"], "cannot read"),
                ([f"GRADLE_UPDATE_PAT={empty}"], "is empty"),
            ]
            for raws, message in cases:
                fake = FakeGh()
                argv = ["--force", "--no-rules"]
                for raw in raws:
                    argv += ["--credential", raw]
                code, out, err = _run(fake, argv + [REPO])
                self.assertEqual(code, 2, (raws, err))
                self.assertIn(message, err)
                self.assertEqual(fake.calls, [], raws)
        # The rejection names what the flag is for.
        fake = FakeGh()
        code, out, err = _run(fake, ["--force", "--no-rules", "--credential", "RANDOM_TOKEN=x", REPO])
        self.assertIn("CI_COMMIT_ARTIFACT_TOKEN (environment 'ci-commit-artifact')", err)
        self.assertIn("--secret NAME[@ENV]=PATH sets it where you say", err)


class AutoMergeStepTest(unittest.TestCase):
    """Always on, like the fleet-credentials step: the setting has one right
    value, and the weekly batches depend on it."""

    def test_an_allowed_repository_needs_nothing(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(fake.patches, [])
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("  auto-merge:\n    already allowed\n", out)

    def test_auto_merge_is_enabled_after_confirmation(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.allow_auto_merge = "false"
        # A repository setting change, so it is a mutation the gate asks about.
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(fake.patches, [])
        code, out, err = _run(fake, ["--force", "-v", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "enable auto-merge on the repository (the weekly batches arm it on their pull requests)",
            err,
        )
        self.assertEqual(len(fake.patches), 1)
        args, body = fake.patches[0]
        self.assertEqual(args[:4], ["api", "--method", "PATCH", f"repos/{REPO}"])
        self.assertEqual(body, {"allow_auto_merge": True})
        self.assertIn(f"{REPO}: enabled auto-merge", out)

    def test_a_dry_run_reports_without_enabling(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.allow_auto_merge = "false"
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("  auto-merge:\n    enable auto-merge on the repository", out)
        self.assertEqual(fake.patches, [])

    def test_a_failed_enable_fails_the_step(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.allow_auto_merge = "false"
        fake.patch_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not enable auto-merge on {REPO}:", err)
        self.assertIn("failed on: auto-merge", err)

    def test_a_failed_read_fails_this_step_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.workflow_files = ["ci.yml"]
            fake.fail_allow_auto_merge = True
            code, out, err = _run(fake, ["--force", "--no-rules", "--secret", f"TOKEN@lanes={path}", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not read whether {REPO} allows auto-merge:", err)
        self.assertIn("failed on: auto-merge", err)
        self.assertEqual(fake.patches, [])
        self.assertEqual(fake.written_secrets, [("TOKEN", REPO, "lanes", b"sekrit")])


class DeleteBranchOnMergeStepTest(unittest.TestCase):
    """Always on, like the auto-merge step above and for the same reason:
    the setting has one right value, and it's what `repo cleanup` exists
    to make unnecessary going forward."""

    def test_an_allowed_repository_needs_nothing(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(fake.patches, [])
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("  delete-branch-on-merge:\n    already allowed\n", out)

    def test_delete_branch_on_merge_is_enabled_after_confirmation(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.delete_branch_on_merge = "false"
        # A repository setting change, so it is a mutation the gate asks about.
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(fake.patches, [])
        code, out, err = _run(fake, ["--force", "-v", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "delete a pull request's head branch automatically once it merges",
            err,
        )
        self.assertEqual(len(fake.patches), 1)
        args, body = fake.patches[0]
        self.assertEqual(args[:4], ["api", "--method", "PATCH", f"repos/{REPO}"])
        self.assertEqual(body, {"delete_branch_on_merge": True})
        self.assertIn(f"{REPO}: enabled delete-branch-on-merge", out)

    def test_a_dry_run_reports_without_enabling(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.delete_branch_on_merge = "false"
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "  delete-branch-on-merge:\n    delete a pull request's head branch automatically",
            out,
        )
        self.assertEqual(fake.patches, [])

    def test_a_failed_enable_fails_the_step(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.delete_branch_on_merge = "false"
        fake.patch_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not enable delete-branch-on-merge on {REPO}:", err)
        self.assertIn("failed on: delete-branch-on-merge", err)

    def test_a_failed_read_fails_this_step_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.workflow_files = ["ci.yml"]
            fake.fail_delete_branch_on_merge = True
            code, out, err = _run(fake, ["--force", "--no-rules", "--secret", f"TOKEN@lanes={path}", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not read whether {REPO} deletes branches on merge:", err)
        self.assertIn("failed on: delete-branch-on-merge", err)
        self.assertEqual(fake.patches, [])
        self.assertEqual(fake.written_secrets, [("TOKEN", REPO, "lanes", b"sekrit")])


class BootstrapStepTest(unittest.TestCase):
    """Always on, like credentials and auto-merge: the fleet's scaffold
    files have one right state (present), and this only ever adds what's
    missing -- never touches a path already there."""

    def test_a_fully_scaffolded_repository_needs_nothing(self):
        fake = FakeGh()
        # bootstrap_existing_paths defaults to None -- "everything present"
        # -- so this is the every-other-test-in-this-file baseline.
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.patches, [])

    def test_scaffold_ref_reads_use_the_singular_endpoint_writes_the_plural_one(self):
        # GitHub's Git References API has no GET route on the plural
        # git/refs/{ref} path at all -- only git/ref/{ref} (singular)
        # reads; git/refs/{ref} is create/update/delete. Every read call
        # here used the plural form until this was caught, which 404'd
        # against every populated branch and made plan_gaps/apply_gaps
        # misclassify it as empty (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_existing_paths = {".github/zizmor.yml"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        ref_reads = [c[1] for c in fake.calls if c[0] == "api" and "/git/ref" in c[1]]
        self.assertTrue(ref_reads, "no ref read calls were made at all")
        for endpoint in ref_reads:
            self.assertIn(
                "/git/ref/heads/",
                endpoint,
                f"read call {endpoint!r} used the plural, write-only git/refs/... route",
            )
        self.assertEqual(len(fake.patches), 1)
        self.assertIn("/git/refs/heads/", fake.patches[0][0][3])

    def test_a_partially_scaffolded_repository_adds_only_what_is_missing(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = {
            ".github/workflows/codex-review-listener.yml",
            ".github/lanes.conf",
        }
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("  bootstrap (fleet CI scaffold):", out)
        self.assertIn("add .github/workflows/codex-review.yml", out)
        self.assertIn("add .github/workflows/zizmor.yml", out)
        self.assertIn("add .github/zizmor.yml", out)
        self.assertIn("add .github/workflows/ci.yml", out)
        self.assertNotIn("add .github/workflows/codex-review-listener.yml", out)
        self.assertNotIn("add .github/lanes.conf", out)
        self.assertIn("already present, untouched: 2 file(s)", out)

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: added 5 fleet CI scaffold file(s)", out)
        blob_paths = {body["encoding"] for _args, body in fake.posts if "encoding" in body}
        self.assertEqual(blob_paths, {"utf-8"})
        blob_posts = [body for _args, body in fake.posts if "encoding" in body]
        self.assertEqual(len(blob_posts), 5)  # one blob per missing file, none for the two present
        tree_posts = [body for _args, body in fake.posts if "base_tree" in body]
        self.assertEqual(len(tree_posts), 1)
        self.assertEqual(tree_posts[0]["base_tree"], fake.bootstrap_tree_sha)
        self.assertEqual(
            {e["path"] for e in tree_posts[0]["tree"]},
            {
                ".github/workflows/codex-review.yml",
                ".github/workflows/codex-review-check.yml",
                ".github/workflows/zizmor.yml",
                ".github/zizmor.yml",
                ".github/workflows/ci.yml",
            },
        )
        commit_posts = [body for _args, body in fake.posts if "parents" in body]
        self.assertEqual(len(commit_posts), 1)
        self.assertEqual(commit_posts[0]["parents"], [fake.bootstrap_commit_sha])
        self.assertEqual(len(fake.patches), 1)
        self.assertEqual(fake.patches[0][1], {"sha": "newscaffoldcommitsha", "force": False})

    def test_bootstrap_applies_before_the_ruleset_step(self):
        # A ruleset requiring pull requests blocks apply_gaps's own direct
        # ref update for anyone not a configured bypass actor -- which the
        # ruleset step never configures the caller to be. Applying the
        # scaffold before this run creates that ruleset is what keeps a
        # fresh repository's first-ever `repo setup --force` working
        # (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)

        def first(predicate):
            return next(i for i, c in enumerate(fake.calls) if predicate(c))

        bootstrap_call = first(lambda c: len(c) > 3 and c[3] == f"repos/{REPO}/git/blobs")
        ruleset_call = first(
            lambda c: len(c) > 3 and c[2] == "POST" and c[3] == f"repos/{REPO}/rulesets"
        )
        self.assertLess(bootstrap_call, ruleset_call)

    def test_a_never_reported_repository_still_gets_bootstrapped_and_only_skips_the_ruleset(self):
        # The exact case this exists for: a fresh (or never-fully-
        # scaffolded) repository, `repo setup OWNER/REPO` with no flags at
        # all. Its required checks have never reported -- there's been no
        # CI to report them -- so the ruleset step can't safely proceed.
        # Before this, that made the WHOLE run refuse to change anything,
        # bootstrap included, even though bootstrap's own plan was fine on
        # its own. Now it applies everything it safely can (bootstrap) and
        # skips only the ruleset step, saying what unblocks it.
        fake = FakeGh()
        fake.bootstrap_existing_paths = {".github/zizmor.yml"}  # real gaps to fill
        # fake.check_runs defaults to {} -- nothing has ever reported.
        with patch("builtins.input", return_value="y"):
            code, out, err = _run(fake, [REPO], isatty=True)
        self.assertEqual(code, 1)
        self.assertIn("failed on:", err)
        self.assertIn("ruleset", err)
        self.assertIn("skipping the ruleset step", err)
        self.assertIn("never reported", err)
        self.assertIn("--force", err)
        # Bootstrap's writes actually landed...
        self.assertIn(f"{REPO}: added", out)
        blob_posts = [body for _args, body in fake.posts if "encoding" in body]
        self.assertTrue(blob_posts, "no scaffold blobs were written")
        self.assertEqual(len(fake.patches), 1)
        # ...but no ruleset was ever created.
        ruleset_posts = [(a, b) for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(ruleset_posts, [])

    def test_missing_workflow_scope_skips_the_bootstrap_step_before_any_write(self):
        # Reported directly (mikelward/repo#18): `repo setup OWNER/REPO
        # --no-rules --force` on a repo with real gaps 404'd on the
        # gap-fill's tree-create, every time, on two separate days -- not
        # a timing window at all. Root cause: a gh token missing the
        # `workflow` OAuth scope, which blocks writing anything under
        # .github/workflows/ -- checked up front now, before any write is
        # attempted, rather than discovered as an opaque, unretryable 404.
        fake = FakeGh()
        fake.bootstrap_existing_paths = {".github/zizmor.yml"}  # real gaps to fill
        fake.token_scopes = ("gist", "read:org", "repo")
        code, out, err = _run(fake, ["--no-rules", "--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("skipping the bootstrap step", err)
        self.assertIn("workflow", err)
        self.assertIn("gh auth refresh", err)
        self.assertNotIn(f"{REPO}: added", out)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.patches, [])

    def test_missing_workflow_scope_message_counts_only_workflow_files(self):
        # Codex review, mikelward/repo#18: the skip message counted
        # len(plan.missing) -- every still-missing scaffold file, not just
        # the ones under .github/workflows/ -- so on a repo missing a
        # non-workflow file too (here, .github/lanes.conf, alongside the
        # scaffold's five workflow files) it overstated how many files the
        # missing scope actually blocks.
        fake = FakeGh()
        fake.bootstrap_existing_paths = {".github/zizmor.yml"}  # only .github/zizmor.yml already present
        fake.token_scopes = ("gist", "read:org", "repo")
        code, out, err = _run(fake, ["--no-rules", "--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("5 file(s) under .github/workflows/", err)
        self.assertNotIn("6 file(s)", err)

    def test_unknown_token_scopes_do_not_block_the_gap_fill(self):
        # A fine-grained PAT or GitHub App token carries no OAuth scopes
        # at all -- "can't tell" must not read as "missing", or every
        # such token would be refused a gap-fill it could actually write.
        fake = FakeGh()
        fake.bootstrap_existing_paths = {".github/zizmor.yml"}  # real gaps to fill
        fake.token_scopes = None
        code, out, err = _run(fake, ["--no-rules", "--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: added", out)
        self.assertEqual(len(fake.patches), 1)

    def test_a_concurrent_push_after_bootstrap_blocks_activating_the_ruleset(self):
        # A concurrent push landing between apply_gaps's own write
        # finishing and the ruleset step activating protection doesn't
        # touch the ruleset's own fingerprint, so nothing else here would
        # notice before locking in protection over a scaffold that may no
        # longer be complete (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        fake.bootstrap_ref_sha_after_own_write = "a-later-concurrent-push-sha"
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "'main' changed after the bootstrap step finished verifying the scaffold "
            "(a concurrent push)",
            err,
        )
        self.assertIn("failed on: ruleset", err)
        self.assertFalse(
            any(len(c) > 3 and c[2] == "POST" and c[3] == f"repos/{REPO}/rulesets" for c in fake.calls)
        )

    def test_a_concurrent_push_is_caught_even_when_the_preview_saw_no_new_protection(self):
        # The scaffold-tip guard used to be computed once, gated on the
        # PREVIEW's own introduces_pr_protection -- here the existing
        # ruleset already has pull_request at preview time, so that
        # snapshot says False and the old precomputed guard would never
        # have run at all. Losing pull_request during the wait (an
        # ordinary ruleset edit, unrelated to bootstrap) makes the FRESH
        # recompute say True instead, and only a check anchored to THAT
        # fresh answer -- not the preview's -- can still catch the
        # concurrent push (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_content_change_threshold = 3
        base_object = {
            "id": 7,
            "name": "main",
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
        fake.ruleset_objects["7"] = json.loads(json.dumps(base_object))
        changed_object = json.loads(json.dumps(base_object))
        changed_object["rules"] = [changed_object["rules"][0]]  # pull_request removed
        fake.ruleset_objects_after_change["7"] = changed_object
        fake.bootstrap_ref_sha_after_own_write = "a-later-concurrent-push-sha"

        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertNotIn("skipping the ruleset step", err)  # no precomputed gate caught this
        self.assertIn(
            "'main' changed after the bootstrap step finished verifying the scaffold "
            "(a concurrent push)",
            err,
        )
        self.assertEqual(fake.puts, [])

    def test_a_bootstrap_failure_blocks_creating_a_new_ruleset(self):
        # Activating pull-request protection while the scaffold failed
        # would leave the repository permanently stuck the way TODO.md
        # describes -- apply_gaps has no path past that protection once
        # it exists (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.template_fetch_fails = {"codex-review.yml"}  # a plan-time bootstrap failure
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("failed on:", err)
        self.assertIn("bootstrap", err)
        self.assertIn("ruleset", err)
        self.assertIn("skipping the ruleset step", err)
        self.assertEqual(fake.posts, [])  # no rulesets POST, no scaffold blobs either

    def test_a_bootstrap_failure_does_not_block_updating_an_existing_ruleset(self):
        # The branch is already protected either way here -- from a run
        # that scaffolded it successfully, or one already stuck -- so an
        # UPDATE to that existing ruleset (this PR's own
        # required_linear_history/non_fast_forward rollout, say) still
        # has legitimate work to do and must not be held hostage to an
        # unrelated bootstrap failure.
        fake = FakeGh()
        fake.template_fetch_fails = {"codex-review.yml"}
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "main",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": list(_HARDENED_SCOPE), "exclude": []}},
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
                {"type": "required_linear_history"},
                {"type": "non_fast_forward"},
            ],
        }
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("bootstrap", err)
        self.assertNotIn("skipping the ruleset step", err)
        self.assertIn("already matches; nothing to do", out)
        failed_line = next(line for line in err.splitlines() if line.startswith("repo: failed on:"))
        self.assertNotIn("ruleset", failed_line)

    def test_a_bootstrap_failure_blocks_an_update_that_newly_adds_pull_request_protection(self):
        # An existing_id-only check misses this: the ruleset already
        # exists, but only carries required_linear_history/
        # non_fast_forward -- no pull_request rule yet. This update would
        # still be the one that first makes the branch require a pull
        # request, so it's exactly as dangerous, paired with a failed
        # bootstrap, as creating a fresh ruleset would be (Codex review,
        # mikelward/repo#14).
        fake = FakeGh()
        fake.template_fetch_fails = {"codex-review.yml"}
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "main",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [{"type": "required_linear_history"}, {"type": "non_fast_forward"}],
        }
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("skipping the ruleset step", err)
        self.assertIn("require a pull request for the first time", err)
        failed_line = next(line for line in err.splitlines() if line.startswith("repo: failed on:"))
        self.assertIn("ruleset", failed_line)
        self.assertEqual(fake.puts, [])  # no PUT to the ruleset either

    def test_pull_request_protection_added_during_the_wait_is_refused_even_though_the_preview_missed_it(self):
        # The preview's own introduces_pr_protection reflects a SNAPSHOT:
        # here the existing ruleset already has pull_request when
        # setup_cmd's own preview reads it, so the early external gate
        # (computed from that preview) lets this call happen. If the
        # ruleset then loses its pull_request rule during the
        # confirmation wait -- while still needing a write for an
        # unrelated reason (missing required_linear_history/
        # non_fast_forward, same as any ordinary update) --
        # _build_update_body would silently reconstruct the very same
        # target body regardless, passing the ordinary fingerprint check
        # untouched. Only apply_ruleset's own FRESH recompute, right
        # before the real write, catches this (Codex review,
        # mikelward/repo#14).
        fake = FakeGh()
        fake.template_fetch_fails = {"codex-review.yml"}
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_content_change_threshold = 3
        base_object = {
            "id": 7,
            "name": "main",
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
        fake.ruleset_objects["7"] = json.loads(json.dumps(base_object))
        changed_object = json.loads(json.dumps(base_object))
        changed_object["rules"] = [changed_object["rules"][0]]  # pull_request removed
        fake.ruleset_objects_after_change["7"] = changed_object

        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertNotIn("skipping the ruleset step", err)  # the early gate missed it
        self.assertIn("would now introduce pull-request protection", err)
        self.assertEqual(fake.puts, [])

    def test_pull_request_protection_added_during_the_wait_is_refused_on_an_empty_no_bootstrap_branch(self):
        # Same race as the test above, but reached from the OTHER
        # direction: bootstrap_failed stays False the whole time here --
        # --no-bootstrap means bootstrap never even ran -- so it can't be
        # what enables apply_ruleset's fresh recheck. empty_branch_would_
        # be_stranded has to be computed independently of the preview's
        # own (here also False, since the existing ruleset already has
        # pull_request) introduces_pr_protection, or this exact case slips
        # through unguarded: the branch has zero commits, --no-bootstrap
        # means nothing will ever add one, and an administrator strips
        # pull_request from the ruleset during the confirmation wait
        # (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_ref_missing = True
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_content_change_threshold = 3
        base_object = {
            "id": 7,
            "name": "main",
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
        fake.ruleset_objects["7"] = json.loads(json.dumps(base_object))
        changed_object = json.loads(json.dumps(base_object))
        changed_object["rules"] = [changed_object["rules"][0]]  # pull_request removed
        fake.ruleset_objects_after_change["7"] = changed_object

        code, out, err = _run(fake, ["--force", "--no-bootstrap", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertNotIn("skipping the ruleset step", err)  # the early gate missed it too
        self.assertIn("would now introduce pull-request protection", err)
        self.assertEqual(fake.puts, [])

    def test_no_bootstrap_skips_the_step_entirely(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()  # would otherwise add everything
        code, out, err = _run(fake, ["--no-rules", "--no-bootstrap", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")  # still a no-op: nothing else was requested either
        self.assertEqual(fake.posts, [])
        # Force the combined plan to actually print (an unrelated pending
        # change -- auto-merge -- so this doesn't hit the same early no-op
        # return as above) and confirm the bootstrap section is absent
        # from it entirely, not just empty.
        fake.allow_auto_merge = "false"
        code, out, err = _run(fake, ["--dry-run", "--no-rules", "--no-bootstrap", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("  auto-merge:", out)
        self.assertNotIn("bootstrap", out)
        self.assertEqual(fake.posts, [])

    def test_no_bootstrap_on_an_empty_branch_skips_the_ruleset_step(self):
        # --no-bootstrap means nothing here will ever add an initial
        # commit -- unlike a bootstrap FAILURE, which at least attempted
        # one -- so this needs its own check: a ruleset that first makes
        # the branch require a pull request would strand a repository
        # with zero commits just as surely as one this run failed to
        # scaffold. No direct push could create the branch once that rule
        # is in force, and no pull request can target a branch that
        # doesn't exist yet to use as a base (Codex review,
        # mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_ref_missing = True
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", "--no-bootstrap", REPO])
        self.assertEqual(code, 1)
        self.assertIn("skipping the ruleset step", err)
        self.assertIn("branch has no commits yet", err)
        self.assertIn("--no-bootstrap", err)
        self.assertEqual(fake.posts, [])  # no rulesets POST

    def test_dry_run_previews_the_no_bootstrap_ruleset_skip_accurately(self):
        # A --dry-run never reaches the Apply section, where an earlier
        # version of this check ran -- so it printed the ruleset as if it
        # would be created and exited 0, while the equivalent real run
        # (the test above) skips the ruleset and exits 1. Whether the
        # branch has any commits is knowable from a read alone, so the
        # dry run's own preview and exit status must already reflect it
        # (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_ref_missing = True
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", "--no-bootstrap", REPO])
        self.assertEqual(code, 1)
        self.assertIn("SKIPPED", out)
        self.assertIn("branch has no commits yet", out)
        self.assertEqual(fake.posts, [])  # dry run: no writes at all regardless

    def test_dry_run_previews_the_never_reported_ruleset_skip_accurately(self):
        # Same accuracy requirement as the no-commits-yet case above, for
        # the never-reported-checks skip: --dry-run must show the same
        # SKIPPED verdict and exit status the real run would, not report
        # the ruleset as creatable when the real run would skip it.
        fake = FakeGh()
        fake.bootstrap_existing_paths = {".github/zizmor.yml"}
        # fake.check_runs defaults to {} -- nothing has ever reported.
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 1)
        self.assertIn("SKIPPED", out)
        self.assertIn("never reported", out)
        self.assertIn("add .github/workflows/ci.yml", out)  # bootstrap's own plan still shown
        self.assertEqual(fake.posts, [])  # dry run: no writes at all regardless

    def test_no_bootstrap_emptiness_check_failure_fails_closed(self):
        # Can't tell whether the branch has commits or not -- fail closed
        # the same as every other "can't verify, so refuse" check in this
        # module, rather than guessing it's safe to proceed.
        fake = FakeGh()
        fake.bootstrap_ref_fails = True
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", "--no-bootstrap", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not check whether", err)
        self.assertIn("has any commits yet", err)
        self.assertIn("skipping the ruleset step", err)
        self.assertEqual(fake.posts, [])

    def test_no_bootstrap_ambiguous_409_is_not_read_as_empty(self):
        # A 409 that isn't specifically "Git Repository is empty" -- some
        # other conflict against a branch that actually has commits --
        # must be treated as a genuine read failure, not silently folded
        # into "the branch is empty." Treating every 409 as empty would
        # tell the ruleset step it's safe to activate pull-request
        # protection on a branch that was never actually at risk of being
        # stranded (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_ref_ambiguous_409 = True
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", "--no-bootstrap", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not check whether", err)
        self.assertIn("has any commits yet", err)
        self.assertIn("skipping the ruleset step", err)
        self.assertEqual(fake.posts, [])

    def test_no_bootstrap_on_a_non_empty_branch_still_creates_the_ruleset(self):
        # The ordinary case --no-bootstrap exists for: a repository that
        # genuinely doesn't want the fleet CI scaffold but already has its
        # own initial commit. Nothing strands it, so the ruleset step must
        # not be held back.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", "--no-bootstrap", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("skipping the ruleset step", err)
        ruleset_posts = [
            (a, b) for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"
        ]
        self.assertEqual(len(ruleset_posts), 1)

    def test_an_empty_repository_bootstraps_via_the_contents_api(self):
        # No commits on the branch yet -- the same two-commit bootstrap
        # `repo create --scaffold` uses, not a gap-fill on top of a tree
        # that doesn't exist.
        fake = FakeGh()
        fake.bootstrap_ref_missing = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: added 7 fleet CI scaffold file(s)", out)
        self.assertEqual(len(fake.puts), 1)  # the Contents-API bootstrap write
        self.assertEqual(fake.puts[0][1]["branch"], "main")

    def test_a_wholly_empty_repository_bootstraps_via_the_contents_api_too(self):
        # Same as the test above, but the ref read fails with HTTP 409
        # ("Git Repository is empty") rather than 404 -- the shape GitHub's
        # Get a reference endpoint documents for a repository with zero
        # git objects at all, the exact state right after `repo create`.
        # Treating only 404 as "no commits yet" would misclassify this as
        # an unexpected failure and refuse to bootstrap the repository
        # `repo create --scaffold` most needs to (Codex review,
        # mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_ref_empty_409 = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: added 7 fleet CI scaffold file(s)", out)
        self.assertEqual(len(fake.puts), 1)  # the Contents-API bootstrap write
        self.assertEqual(fake.puts[0][1]["branch"], "main")

    def test_a_directory_occupying_a_scaffold_path_is_refused_not_replaced(self):
        # A "tree" entry at .github/zizmor.yml -- a directory, not a file -- must
        # never be silently replaced by our blob (Codex review,
        # mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()  # everything else genuinely missing
        fake.bootstrap_occupied_entries = {".github/zizmor.yml": "tree"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("cannot add .github/zizmor.yml to the scaffold: .github/zizmor.yml already exists and is not a "
                       "regular file (tree); add it by hand", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.puts, [])

    def test_a_symlink_occupying_a_scaffold_path_is_refused_not_treated_as_present(self):
        # Git stores a symlink as type "blob" too (mode "120000"), so type
        # alone can't tell it apart from the real file -- it may point
        # anywhere and isn't the scaffold content just because the tree
        # entry says "blob" (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()  # everything else genuinely missing
        fake.bootstrap_occupied_entries = {".github/zizmor.yml": ("blob", "120000")}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("cannot add .github/zizmor.yml to the scaffold: .github/zizmor.yml already exists and is not a "
                       "regular file (blob, mode 120000); add it by hand", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.puts, [])

    def test_a_file_occupying_a_scaffold_directory_component_is_refused(self):
        # ".github" existing as a blob (a file) means nothing can live
        # under it -- not just the exact path colliding.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_occupied_entries = {".github": "blob"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("cannot add .github/lanes.conf to the scaffold: .github already exists and "
                       "is not a directory (blob); add it by hand", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.posts, [])

    def test_a_failed_template_fetch_fails_the_step_alone(self):
        fake = FakeGh()
        fake.template_fetch_fails = {"codex-review.yml"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "could not fetch mikelward/codex-review@faketemplateshaabc123:templates/"
            "codex-review.yml",
            err,
        )
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.posts, [])

    def test_a_failed_template_commit_resolve_fails_the_step_alone_before_any_fetch(self):
        # Resolving codex-review's main to a commit sha is the first call
        # build_scaffold_files makes for the three templates -- a failure
        # there must stop before any of the three fetches, not just fail
        # one of them (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.template_resolve_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not resolve mikelward/codex-review@main to a commit", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.posts, [])

    def test_a_truncated_tree_fails_closed_rather_than_guessing(self):
        fake = FakeGh()
        fake.bootstrap_tree_truncated = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("too large to list in one call", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.posts, [])

    def test_ref_moving_since_the_plan_was_built_is_refused_not_overwritten(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_ref_update_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not update main to the scaffold gap-fill's commit", err)
        self.assertIn("moved since the plan was built, or a ruleset already blocks a direct push", err)
        # Neither cause is diagnosable from GitHub's own response, so the
        # error names the way past both -- including the flag that gets
        # the rest of `repo setup` through a branch this step cannot write
        # to at all, which its rejection ("Changes must be made through a
        # pull request") says nothing about.
        self.assertIn("`--no-bootstrap` skips this step", err)
        self.assertIn("adding by hand", err)
        self.assertIn("failed on: bootstrap", err)

    def test_a_branch_reset_backward_is_refused_rather_than_silently_restored(self):
        # force: False alone only guarantees a fast-forward -- an
        # ancestry check, not "the ref hasn't moved". If the branch was
        # reset backward to an ancestor of plan.base_commit_sha while
        # this waited, that ancestor still passes a fast-forward check
        # against the commit this plan built (it descends from exactly
        # that ancestor), so a bare force:False PATCH would silently
        # restore whatever commits the reset just removed. The explicit
        # equality recheck right before the PATCH must catch this even
        # though a plain fast-forward check would not (Codex review,
        # mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_ref_sha_after_first_read = "an-earlier-ancestor-sha"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("no longer points at the commit this plan was built from", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.patches, [])  # the PATCH itself never ran

    def test_a_default_branch_rename_is_refused_rather_than_scaffolding_the_old_one(self):
        # An administrative rename of the default branch (repo settings,
        # not a push) between plan_gaps's own read and this step's apply
        # must not land the scaffold on a branch that's no longer
        # current while reporting success (Codex review,
        # mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.default_branch_after_bootstrap_plan = "trunk"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("default branch changed from 'main' to 'trunk'", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.patches, [])

    def test_a_default_branch_rename_is_refused_even_when_the_old_branch_had_nothing_missing(self):
        # Same race as the test above, but the plan itself found nothing
        # to add (the old branch was already fully scaffolded) -- so the
        # recheck used to live inside `elif bootstrap_plan.missing:` and
        # never ran at all, letting a no-op plan report success without
        # ever inspecting the renamed branch, which a later ruleset step
        # could then go on to protect while wholly unscaffolded (Codex
        # review, mikelward/repo#14).
        fake = FakeGh()
        # bootstrap_existing_paths left at its default (None -- everything
        # present), so plan_gaps finds nothing missing on the old branch.
        fake.default_branch_after_bootstrap_plan = "trunk"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("default branch changed from 'main' to 'trunk'", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.patches, [])

    def test_a_concurrent_push_is_refused_for_a_no_op_plan_too(self):
        # Same branch NAME, but its TIP moved: a concurrent push could
        # have deleted or replaced a scaffold file since plan_gaps read
        # the tree, and a no-op plan used to skip apply_gaps entirely
        # (its own exact-sha recheck lives inside the write path), so
        # this would previously exit 0 having never re-verified anything
        # beyond the branch's name (Codex review, mikelward/repo#14).
        fake = FakeGh()
        # bootstrap_existing_paths left at its default (None -- everything
        # present), so plan_gaps finds nothing missing.
        fake.bootstrap_ref_sha_after_first_read = "a-different-tip-sha"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("no longer points at the commit this plan was built from", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.patches, [])

    def test_a_dry_run_makes_no_writes(self):
        # A genuine, fixable gap is not an error state -- like the ruleset
        # step's own "would create ruleset" preview, a dry run reports it
        # and still exits 0; only a bootstrap_plan.error (a read that
        # actually failed) exits 1 under --dry-run.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("  bootstrap (fleet CI scaffold):", out)
        self.assertIn("add .github/zizmor.yml", out)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.patches, [])


if __name__ == "__main__":
    unittest.main()
