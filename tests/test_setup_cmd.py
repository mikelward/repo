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
_ENV_POLICIES_RE = re.compile(r"^repos/([^/]+/[^/]+)/environments/([^/]+)/deployment-branch-policies$")
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
_SCAFFOLD_CONVENTIONS_RE = re.compile(r"^repos/mikelward/conf/contents/agents/AGENTS\.md\?ref=main$")
# Singular git/ref/... is the read (GET) route; plural git/refs/... is
# create/update/delete only and has no GET at all -- two regexes, not one,
# so the fixture can only answer a read on the route real GitHub actually
# serves it on (Codex review, mikelward/repo#14).
_SCAFFOLD_REF_READ_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/ref/heads/([^/?]+)$")
# The bootstrap step's pull-request write path. The gap branch carries a
# `/`, so it needs its own read route -- the default-branch one above
# deliberately stops at a path separator. The listing is matched ahead of
# _PULLS_RE, whose shape (state + per_page) this one also fits.
_SCAFFOLD_GAP_REF_READ_RE = re.compile(
    r"^repos/([^/]+/[^/]+)/git/ref/heads/(" + re.escape(scaffold.GAP_BRANCH_PREFIX) + r".*)$"
)
_SCAFFOLD_GAP_PULLS_RE = re.compile(
    r"^repos/([^/]+/[^/]+)/pulls\?state=open&per_page=100&base=(.+)$"
)
_SCAFFOLD_REF_CREATE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/refs$")
# GitHub's effective-rules endpoint: what a branch actually enforces
# across every ruleset covering it, which is what the scaffold step reads
# to say whether its pull request can merge on its own.
_EFFECTIVE_RULES_RE = re.compile(r"^repos/([^/]+/[^/]+)/rules/branches/(.+)$")
_SCAFFOLD_PULL_CREATE_RE = re.compile(r"^repos/([^/]+/[^/]+)/pulls$")
_SCAFFOLD_REF_WRITE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/refs/heads/([^/?]+)$")
_SCAFFOLD_COMMIT_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/commits/([^/?]+)$")
_SCAFFOLD_TREE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/trees/([^/?]+)\?recursive=1$")
_SCAFFOLD_BLOB_CREATE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/blobs$")
_SCAFFOLD_TREE_CREATE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/trees$")
_SCAFFOLD_COMMIT_CREATE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/commits$")
_SCAFFOLD_CONTENTS_PUT_RE = re.compile(r"^repos/([^/]+/[^/]+)/contents/(.+)$")

# Every path build_scaffold_files produces -- what a scaffold pull request
# adds when it covers the whole gap.
_SCAFFOLD_PATHS = (
    ".github/lanes.conf",
    ".github/workflows/ci.yml",
    ".github/workflows/codex-review-check.yml",
    ".github/workflows/codex-review-listener.yml",
    ".github/workflows/codex-review.yml",
    ".github/workflows/zizmor.yml",
    ".github/zizmor.yml",
    "AGENTS.md",
    "CLAUDE.md",
)

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
        # Which read first reports the renamed branch, counting every
        # read this run makes. 3 (the default) puts it at the bootstrap
        # step's pre-apply recheck; 5 puts it BETWEEN the credentials
        # recheck's two reads, which is the window a name checked before
        # the workflows left open (Codex, mikelward/repo#36). The
        # credentials plan makes the first two: `workflow_snapshot` reads
        # the name, reads the workflows pinned to it, and confirms the
        # name again -- the confirming read is the second, since the
        # workflows are not read through this counter.
        self.default_branch_renamed_after_read = 3
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
        # Set instead when a test needs more than one ruleset under the
        # managed name -- GitHub does not make it unique.
        self.existing_ruleset_ids = None
        # What a by-name lookup answers from existing_ruleset_id_lookup_
        # threshold on, modeling a second ruleset created under the name
        # part-way through a run.
        self.existing_ruleset_ids_later = None
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
        self.env_secrets_fail_after = {}  # env -> nth secret-names read that starts failing
        self._env_secret_reads = {}
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
        self.branch_workflows_after_recheck = {}  # branch -> its workflows from the second listing on
        self._branch_reads = {}
        self._workflow_reads = {}
        self.root_contents_error = None  # stderr for the root listing, or None to succeed
        self.deleted_secrets = []  # (name, env or None)
        self.delete_fails = set()  # secret names whose DELETE fails
        # env -> which branches may reach it: None (any branch -- GitHub's
        # default for a new environment), "protected", or a list of
        # custom policy patterns (`tag:v*` for a tag policy).
        self.env_policies = {}
        self.env_protection_rules = {}  # env -> the protection_rules the GET reports
        self.restricted = []  # (env, name) of every branch policy POSTed
        self.env_deleted_after_restrict = set()  # envs deleted right after being restricted
        self._restrict_confirmed = set()  # envs whose restriction this run has confirmed
        self.env_reopened_during_writes = set()  # envs reopened as the pair is written
        self.env_puts = []  # (env, body) of every PUT carrying a body
        self.restrict_fails = set()  # env names whose policy PUT fails
        self.policy_post_fails = set()  # env names whose branch-policy POST fails
        self.policy_post_fails_adding = {}  # env -> a pattern someone adds right before that POST fails
        # env -> can_admins_bypass an administrator changes while the run
        # sits between its policy PUT and the restore that follows a failed
        # POST, so the restore's settings snapshot is stale.
        self.admin_bypass_after_failed_post = {}
        # env -> a pattern someone adds while the settings are re-read, i.e.
        # AFTER the rollback's branch-policy listing and before its PUT.
        self.policy_added_before_restore = {}
        self._add_on_next_env_read = {}
        self.policy_added_during_post = {}  # env -> a pattern someone adds while that POST succeeds
        # env -> the policy the SECOND read on sees, modeling one set while
        # the plan waited on confirmation; absent => unchanged.
        self.env_policies_after_recheck = {}
        self.env_policies_after_read = {}  # env -> (read count, policy): switches at that read
        self._env_reads = {}
        self.env_admin_bypass = {}  # env -> can_admins_bypass as the GET reports it

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
        self.conventions_content = "# Coding\n\n- Fake shared conventions.\n"
        self.conventions_fetch_fails = False
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
        # (read_count, sha): the branch's tip changing once more than
        # `read_count` ref reads have happened -- a concurrent push
        # landing at a chosen point in the run. bootstrap_ref_sha_after_
        # first_read cannot express that where the bootstrap step makes a
        # recheck of its own (it would fire on that recheck and fail the
        # step before the thing under test is reached).
        self.bootstrap_ref_sha_after_read = None
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
        # The pull-request write path the gap-fill takes on a branch that
        # has commits. bootstrap_open_pulls models what the open-pull-
        # request listing answers -- (number, head ref, head-and-base are
        # the same repository, url) tuples, as the --jq line format the
        # step reads.
        self.bootstrap_open_pulls = []
        self.bootstrap_pulls_list_fails = False
        # What the listing answers from its SECOND call on -- a scaffold
        # pull request somebody opened during the confirmation wait, which
        # only the step's own recheck before writing can see.
        self.bootstrap_open_pulls_later = None
        self._gap_pulls_reads = 0
        # There is deliberately no route for a pull request's file list.
        # The bootstrap step never reads what an open scaffold pull
        # request contains -- nothing derived from a mutable pull request
        # stays true -- so a call for one is a regression, and with no
        # route the fixture raises rather than answering it (Codex review,
        # mikelward/repo#42).
        # The branch's effective rules, as one object per line. Empty (the
        # default) is an unprotected branch: nothing required, so nothing
        # this pull request could fail to satisfy.
        self.effective_rules = []
        self.effective_rules_read_fails = False
        self.bootstrap_ref_create_fails = False
        # The sha an already-existing ref of the gap branch's name holds,
        # for the "a rerun rebuilt the same commit" path -- None means the
        # ref genuinely isn't there.
        self.bootstrap_gap_ref_sha = None
        self.bootstrap_pull_create_fails = False
        self.bootstrap_pull_create_response = None
        self.bootstrap_pull_number = 42
        self.created_refs = []
        self.created_pulls = []
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

    def _tree_ref(self, ref):
        """`ref` as this fake stores trees under: None for one naming the
        default branch, which GitHub answers from the same tree as an
        unqualified read. `workflow_texts` names the default explicitly so
        a rename cannot silently redirect it (Codex, mikelward/repo#36),
        and the "changed while the plan waited" hooks count default-branch
        reads -- so without this they would stop firing and every one of
        those tests would pass vacuously. The rename hook only moves which
        name `.default_branch` REPORTS; the tree keeps its own name here."""
        if ref in (self.default_branch, self.default_branch_after_bootstrap_plan):
            # A rename moves the branch: the old name stops resolving and
            # the new one answers from the same tree. This fake keeps one
            # tree and moves only the name `.default_branch` reports, so
            # both names read it.
            return None
        return ref

    def _delete_after_restrict(self, env):
        """Someone deletes `env` in the window this run cannot see: after
        the restriction and its own confirming read, and before the next
        thing that touches it -- the existence check the writes used to
        make, or the write itself once that check is gone (Codex,
        mikelward/repo#36). Fires once, so a recreate stays created."""
        if env in self._restrict_confirmed:
            self._restrict_confirmed.discard(env)
            self.env_secret_names.pop(env, None)
            self.env_policies.pop(env, None)

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
            # file. The threshold counts the reads the credentials plan
            # makes first: `workflow_snapshot` reads the name, then the
            # workflows pinned to it, then the name again to confirm the
            # two are one reading (Codex, mikelward/repo#36; the first of
            # those was already ahead of the bootstrap step at
            # mikelward/repo#14 -- caught here, not by that PR comment,
            # while writing this fixture). So the bootstrap plan's own
            # read is the THIRD call, and the pre-apply recheck this
            # fixture exists to test is the fourth.
            branch = self.default_branch
            if (
                self._default_branch_reads > self.default_branch_renamed_after_read
                and self.default_branch_after_bootstrap_plan is not None
            ):
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

        m = _EFFECTIVE_RULES_RE.match(endpoint)
        if m:
            if self.effective_rules_read_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            return "".join(json.dumps(rule) + "\n" for rule in self.effective_rules)

        m = _SCAFFOLD_GAP_PULLS_RE.match(endpoint)
        if m:
            if self.bootstrap_pulls_list_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            self._gap_pulls_reads += 1
            pulls = self.bootstrap_open_pulls
            if self._gap_pulls_reads > 1 and self.bootstrap_open_pulls_later is not None:
                pulls = self.bootstrap_open_pulls_later
            return "".join(
                f"{number} {head_ref} {'true' if same_repo else 'false'} {url}\n"
                for number, head_ref, same_repo, url in pulls
            )

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
            if self.existing_ruleset_ids is not None:
                # Counted once by the line above, like every other by-name
                # lookup. Counting it twice here put the threshold behind
                # the FIRST lookup, so a test meaning "the duplicate
                # appears on the real apply's own lookup" got it from the
                # preview instead and passed without exercising the
                # transition at all (Codex review, mikelward/repo#33).
                if (
                    self._name_lookup_calls >= self.existing_ruleset_id_lookup_threshold
                    and self.existing_ruleset_ids_later is not None
                ):
                    return "".join(f"{rid}\n" for rid in self.existing_ruleset_ids_later)
                return "".join(f"{rid}\n" for rid in self.existing_ruleset_ids)
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
            ref = self._tree_ref(urllib.parse.unquote(m.group(2))) if m.group(2) else None
            if ref:
                self._branch_reads[ref] = self._branch_reads.get(ref, 0) + 1
                if self._branch_reads[ref] >= 2 and ref in self.branch_workflows_after_recheck:
                    self.branch_workflows[ref] = self.branch_workflows_after_recheck[ref]
                # `.get`, not `[]`: a rename makes the OLD default branch
                # an ordinary branch, and it has no entry here.
                files = [*(self.workflow_files or []), *self.branch_workflows.get(ref, {})]
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
            name = m.group(2)
            ref = self._tree_ref(urllib.parse.unquote(m.group(3))) if m.group(3) else None
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
            if env in self.env_secrets_fail_after:
                # Nth read onward fails: the pre-write rollback inventory is
                # a later read than the plan's own (Codex, mikelward/repo#36).
                self._env_secret_reads[env] = self._env_secret_reads.get(env, 0) + 1
                if self._env_secret_reads[env] >= self.env_secrets_fail_after[env]:
                    raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            if env not in self.env_secret_names:
                raise gh.GhError(f"gh: HTTP 404: Not Found (.../{endpoint})\n")
            return "".join(n + "\n" for n in sorted(self._secret_names_for(env)))

        m = _ENV_POLICIES_RE.match(endpoint)
        if m and method is None:
            env = m.group(2)
            policy = self.env_policies.get(env)
            if not isinstance(policy, list):
                raise AssertionError(f"branch policies listed for {env}, whose policy is {policy!r}")
            if env in self.policy_added_before_restore:
                # Arm it for the NEXT environment GET, which is the
                # rollback's settings re-read: this listing is the read the
                # refusal above is decided from, so a pattern added before
                # it is a different (already-covered) case.
                self._add_on_next_env_read[env] = self.policy_added_before_restore.pop(env)
            if env in self.env_deleted_after_restrict and [(env, p) for p in policy] == self.restricted:
                # `restrict_environment` confirms its own postcondition by
                # listing the policies; this listing IS that confirmation,
                # so arm the deletion for whatever touches the environment
                # next.
                self.env_deleted_after_restrict.discard(env)
                self._restrict_confirmed.add(env)
            return "".join(
                (f"tag {p[4:]}" if p.startswith("tag:") else f"branch {p}") + "\n" for p in policy
            )

        m = _ENV_ONE_RE.match(endpoint)
        if m:
            env = m.group(2)
            if method == "PUT":
                if env in self.env_create_fails:
                    raise gh.GhError(f"gh: could not create environment '{env}'\n")
                if env not in self.env_secret_names:
                    # A created environment has GitHub's default policy:
                    # open to every branch. Modeling it as keeping whatever
                    # the deleted one had would hide the exposure a
                    # recreate-after-restrict opens (Codex,
                    # mikelward/repo#36).
                    self.env_policies[env] = None
                self.env_secret_names.setdefault(env, set())
                return ""
            self._delete_after_restrict(env)
            if env in self._add_on_next_env_read and isinstance(self.env_policies.get(env), list):
                self.env_policies[env].append(self._add_on_next_env_read.pop(env))
            if env in self.env_get_check_fails:
                raise gh.GhError(
                    "gh: HTTP 403: Resource protected by organization SAML enforcement\n"
                )
            if env in self.env_secret_names:
                return json.dumps(self._environment(env))
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

        if _SCAFFOLD_CONVENTIONS_RE.match(endpoint) and jq == ".content":
            if self.conventions_fetch_fails:
                raise gh.GhError("gh: HTTP 404: Not Found (.../repos/mikelward/conf)\n")
            return base64.encodebytes(self.conventions_content.encode()).decode()

        if _SCAFFOLD_ZIZMOR_RE.match(endpoint) and jq == ".content":
            if self.zizmor_fetch_fails:
                raise gh.GhError("gh: HTTP 404: Not Found (.../repos/mikelward/lanes)\n")
            return base64.encodebytes(self.zizmor_workflow_content.encode()).decode()

        m = _SCAFFOLD_GAP_REF_READ_RE.match(endpoint)
        if m and method is None and jq is None:
            if self.bootstrap_gap_ref_sha is None:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            return json.dumps({"object": {"sha": self.bootstrap_gap_ref_sha}})

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
            if self.bootstrap_ref_sha_after_read is not None:
                after, later = self.bootstrap_ref_sha_after_read
                if self._scaffold_ref_reads > after:
                    sha = later
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
            "AGENTS.md",
            "CLAUDE.md",
        }

    def _text(self, name, ref):
        """A workflow's text on `ref` (None: the default branch)."""
        if ref and name in self.branch_workflows.get(ref, {}):
            return self.branch_workflows[ref][name]
        return self.workflow_texts.get(name, "")

    def _blob(self, name, ref):
        return hashlib.sha1(self._text(name, ref).encode()).hexdigest()

    def _environment(self, env):
        """The environment object GitHub's GET reports, as far as the
        branch-policy reader and the restriction's PUT read it."""
        self._env_reads[env] = self._env_reads.get(env, 0) + 1
        if self._env_reads[env] >= 2 and env in self.env_policies_after_recheck:
            self.env_policies[env] = self.env_policies_after_recheck[env]
        if env in self.env_policies_after_read and self._env_reads[env] >= self.env_policies_after_read[env][0]:
            self.env_policies[env] = self.env_policies_after_read[env][1]
        policy = self.env_policies.get(env)
        if policy is None:
            branch_policy = None
        elif policy == "protected":
            branch_policy = {"protected_branches": True, "custom_branch_policies": False}
        else:
            branch_policy = {"protected_branches": False, "custom_branch_policies": True}
        return {
            "name": env,
            "deployment_branch_policy": branch_policy,
            "protection_rules": self.env_protection_rules.get(env, []),
            "can_admins_bypass": self.env_admin_bypass.get(env, True),
        }

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
            if env is not None:
                self._delete_after_restrict(env)
                if env in self.env_reopened_during_writes:
                    # An administrator reopens the environment while the
                    # pair is going in -- after every check this move makes
                    # before its writes (Codex, mikelward/repo#36). Fires
                    # once, on the first write into it.
                    self.env_reopened_during_writes.discard(env)
                    self.env_policies[env] = None
                if env not in self.env_secret_names:
                    # An environment secret needs its environment: GitHub
                    # 404s rather than creating one. Modeling the write as
                    # always succeeding hid what a deleted environment does
                    # to a run that had already restricted it (Codex,
                    # mikelward/repo#36).
                    raise gh.GhError(f"gh: HTTP 404: Not Found (environment '{env}')\n")
                self.env_secret_names[env].add(name)
            self.written_secrets.append((name, repo, env, input_bytes))
            return b""
        assert args[0] == "api", args
        endpoint, method, _jq = _parse_api_args(args[1:])
        body = json.loads(input_bytes.decode())
        if method == "PUT":
            self.puts.append((args, body))
            m = _ENV_ONE_RE.match(endpoint)
            if m:
                env = m.group(2)
                if env in self.restrict_fails:
                    raise gh.GhError("gh: HTTP 422: Validation Failed\n")
                self.env_puts.append((env, body))
                policy = body.get("deployment_branch_policy")
                if policy is None:
                    self.env_policies[env] = None
                elif policy.get("protected_branches"):
                    self.env_policies[env] = "protected"
                else:
                    self.env_policies[env] = []
                return b""
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
                #
                # It also creates the branch: a later read has to see it,
                # or a run that bootstrapped an empty repository would
                # then fail its own post-bootstrap recheck against a ref
                # the fixture still claims doesn't exist.
                self.bootstrap_ref_missing = False
                self.bootstrap_ref_empty_409 = False
                self._scaffold_ref_current_sha = "bootstrapcommitsha"
                return json.dumps({"commit": {"sha": "bootstrapcommitsha"}}).encode()
        elif method == "POST":
            m = _ENV_POLICIES_RE.match(endpoint)
            if m:
                env = m.group(2)
                if env in self.policy_post_fails:
                    if env in self.policy_post_fails_adding and isinstance(self.env_policies.get(env), list):
                        self.env_policies[env].append(self.policy_post_fails_adding[env])
                    if env in self.admin_bypass_after_failed_post:
                        self.env_admin_bypass[env] = self.admin_bypass_after_failed_post[env]
                    raise gh.GhError("gh: HTTP 422: Validation Failed (policy)\n")
                if not isinstance(self.env_policies.get(env), list):
                    raise gh.GhError("gh: HTTP 422: custom branch policies are not enabled\n")
                name = body["name"] if body.get("type", "branch") == "branch" else f"tag:{body['name']}"
                self.env_policies[env].append(name)
                if env in self.policy_added_during_post:
                    self.env_policies[env].append(self.policy_added_during_post[env])
                self.restricted.append((env, body["name"]))
                return b""
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
            if _SCAFFOLD_REF_CREATE_RE.match(endpoint):
                self.posts.append((args, body))
                if self.bootstrap_ref_create_fails:
                    raise gh.GhError("gh: HTTP 422: Reference already exists\n")
                self.created_refs.append(body)
                return json.dumps({"ref": body["ref"]}).encode()
            m = _SCAFFOLD_PULL_CREATE_RE.match(endpoint)
            if m:
                self.posts.append((args, body))
                if self.bootstrap_pull_create_fails:
                    raise gh.GhError("gh: HTTP 422: Validation Failed\n")
                self.created_pulls.append(body)
                if self.bootstrap_pull_create_response is not None:
                    return self.bootstrap_pull_create_response
                number = self.bootstrap_pull_number
                return json.dumps(
                    {"number": number, "html_url": f"https://github.com/{m.group(1)}/pull/{number}"}
                ).encode()
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

    def test_a_second_ruleset_under_the_managed_name_is_reported(self):
        # GitHub does not make the name unique, so this run writes the
        # first and the other keeps applying -- including whatever
        # stricter rule or bypass actor it carries. Reported rather than
        # resolved: deciding between two rulesets that disagree is its own
        # change (see TODO.md), and picking one silently is the problem.
        fake = FakeGh()
        self._ruleset_with_scope(fake, list(_HARDENED_SCOPE))
        fake.legacy_ruleset_ids = []
        fake.all_ruleset_ids = ["7", "8"]
        fake.ruleset_objects["8"] = dict(fake.ruleset_objects["7"], id=8)
        fake.existing_ruleset_ids = ["7", "8"]
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("more than one ruleset is named 'main'", err)
        self.assertIn("writes id 7 and leaves id 8 alone", err)
        self.assertEqual(fake.puts, [])
        self.assertEqual(fake.deleted_rulesets, [])

    def test_a_duplicate_appearing_before_the_write_is_reported_even_when_quiet(self):
        # setup_cmd's real apply runs quiet unless --verbose, so gating
        # this report on quiet -- as the reports around it are -- would
        # suppress the only warning there is for a second ruleset created
        # since the preview ran, which is precisely the silence this is
        # for (Codex review, mikelward/repo#33).
        fake = FakeGh()
        self._ruleset_with_scope(fake, list(_HARDENED_SCOPE))
        fake.all_ruleset_ids = ["7", "8"]
        fake.ruleset_objects["8"] = dict(fake.ruleset_objects["7"], id=8)
        fake.existing_ruleset_ids = ["7", "8"]
        # No -v: the real apply is quiet, and the note still has to appear.
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("more than one ruleset is named 'main'", err)

    def test_a_duplicate_created_between_the_passes_is_reported_on_the_no_op_path(self):
        # The case the previous test could not reach: it supplied both ids
        # from the first lookup, so the preview named the duplicate. Here
        # the second ruleset appears only from the real apply's own lookup
        # (threshold 2), and that apply computes needs_write=False and
        # returns down the no-op path -- which, gated on quiet, said
        # nothing at all (Codex review, mikelward/repo#33).
        fake = FakeGh()
        self._ruleset_with_scope(fake, list(_HARDENED_SCOPE))
        fake.existing_ruleset_ids = ["7"]
        fake.existing_ruleset_ids_later = ["7", "8"]
        fake.existing_ruleset_id_lookup_threshold = 2
        fake.all_ruleset_ids = ["7", "8"]
        fake.ruleset_objects["8"] = dict(fake.ruleset_objects["7"], id=8)
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("more than one ruleset is named 'main'", err)
        self.assertEqual(fake.puts, [])

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

    def test_a_rename_in_that_window_keeps_the_duplicate_too(self):
        # Content equality deliberately ignores the name -- that is what
        # lets a duplicate be recognized as identical to a survivor called
        # something else -- so it says nothing about WHICH of the two is
        # the standard ruleset. An administrator renaming the duplicate to
        # 'main' in this window would otherwise have the newly canonical
        # one deleted (Codex review, mikelward/repo#31).
        fake = FakeGh()
        self._matching_pair(fake)
        fake.ruleset_objects_after_change["9"] = dict(fake.ruleset_objects["9"], name="main")
        fake.ruleset_content_change_threshold = 6
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted_rulesets, [])
        self.assertIn("renamed", err)

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


class LanesCredentialStepTest(unittest.TestCase):
    """The lanes App pair in the fleet-credentials step: the same move as
    the batches', held back by a publishing job that does not declare the
    environment rather than by a caller naming its secrets, and followed
    by restricting the environment to the default branch when any branch
    can reach it."""

    PUBLISHER = (
        "jobs:\n  init:\n    runs-on: ubuntu-latest\n    environment: lanes\n"
        "    steps:\n      - uses: mikelward/lanes@main\n        with:\n"
        "          mode: init\n          app-id: ${{ secrets.LANES_APP_ID }}\n"
        "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n"
    )
    PAIR = {"LANES_APP_ID", "LANES_APP_PRIVATE_KEY"}

    def _publisher(self, text=PUBLISHER):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": text}
        return fake

    def test_a_supplied_pair_moves_into_a_new_restricted_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set(self.PAIR)
            code, out, err = _run(
                fake,
                [
                    "--force", "-v", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 0, err)
        self.assertIn("lanes: set LANES_APP_ID in environment 'lanes' (new)", err)
        self.assertIn("lanes: set LANES_APP_PRIVATE_KEY in environment 'lanes' (new)", err)
        self.assertIn(
            "lanes: delete repository secret LANES_APP_ID -- the 'lanes' environment holds the credential once set",
            err,
        )
        self.assertIn(
            "lanes: restrict environment 'lanes' to branch 'main' -- it can be reached from any branch, so a "
            "same-repo pull request's push-triggered workflow reads the App credential too",
            err,
        )
        self.assertEqual(
            [w[:3] for w in fake.written_secrets],
            [("LANES_APP_ID", REPO, "lanes"), ("LANES_APP_PRIVATE_KEY", REPO, "lanes")],
        )
        self.assertEqual(sorted(fake.deleted_secrets), [("LANES_APP_ID", None), ("LANES_APP_PRIVATE_KEY", None)])
        self.assertEqual(fake.restricted, [("lanes", "main")])
        self.assertEqual(fake.env_policies["lanes"], ["main"])
        self.assertIn(f"{REPO}: restricted environment 'lanes' to branch 'main'", out)
        # The environment exists, is shut, and only then holds a secret.
        # The restriction lands BEFORE the writes: after them, a failure
        # would leave the pair in an environment any branch can enter --
        # the exposure this placement exists to close, created by the run
        # closing it (Codex, mikelward/repo#36).
        put_env = next(i for i, c in enumerate(fake.calls) if c[1:3] == ["--method", "PUT"] and c[3].endswith("/environments/lanes"))
        write = next(i for i, c in enumerate(fake.calls) if c[:2] == ["secret", "set"])
        restrict = next(i for i, c in enumerate(fake.calls) if c[1:3] == ["--method", "POST"] and "deployment-branch-policies" in c[3])
        delete = next(
            i for i, c in enumerate(fake.calls)
            if c[1:3] == ["--method", "DELETE"] and "/actions/secrets/" in c[3]
        )
        self.assertLess(put_env, restrict)
        self.assertLess(restrict, write)
        self.assertLess(write, delete)

    def test_an_open_environment_already_holding_the_pair_is_restricted(self):
        fake = self._publisher()
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_protection_rules = {
            "lanes": [
                {"type": "wait_timer", "wait_timer": 5},
                {
                    "type": "required_reviewers",
                    "prevent_self_review": True,
                    "reviewers": [{"type": "User", "reviewer": {"id": 7}}],
                },
            ]
        }
        fake.env_admin_bypass = {"lanes": False}
        code, out, err = _run(fake, ["--force", "-v", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(fake.deleted_secrets), [("LANES_APP_ID", None), ("LANES_APP_PRIVATE_KEY", None)])
        self.assertEqual(fake.restricted, [("lanes", "main")])
        # The PUT re-sends every protection setting GitHub would otherwise
        # reset, alongside the new policy.
        [(env, body)] = fake.env_puts
        self.assertEqual(env, "lanes")
        self.assertEqual(
            body,
            {
                "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
                "wait_timer": 5,
                "reviewers": [{"type": "User", "id": 7}],
                "prevent_self_review": True,
                "can_admins_bypass": False,
            },
        )

    def test_a_failed_policy_write_puts_the_open_policy_back(self):
        # The restriction is two writes; the second failing after the
        # first would leave custom-policy mode naming no branch, which
        # admits nothing -- worse than the open state it started from.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_admin_bypass = {"lanes": False}
        fake.policy_post_fails = {"lanes"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not restrict environment 'lanes' on {REPO}:", err)
        self.assertIn("Validation Failed (policy)", err)
        self.assertEqual([env for env, _ in fake.env_puts], ["lanes", "lanes"])
        restore = fake.env_puts[1][1]
        self.assertIsNone(restore["deployment_branch_policy"])
        # The settings the first PUT carried ride the restore too.
        self.assertIs(restore["can_admins_bypass"], False)
        self.assertIsNone(fake.env_policies["lanes"])
        self.assertEqual(fake.restricted, [])

    def test_a_policy_added_while_the_settings_are_re_read_is_left_in_place(self):
        # The refusal above rests on a listing taken before the settings
        # re-read, and that read is a round trip: a policy added across it
        # was deleted by the restore PUT and the environment reopened to
        # every branch, with the App pair inside it (Codex,
        # mikelward/repo#36). The list is confirmed last now, immediately
        # before the write that acts on it.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.policy_post_fails = {"lanes"}
        fake.policy_added_before_restore = {"lanes": "release/*"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "a policy someone set meanwhile -- 'release/*' -- was left in place rather than reopened over; "
            "restrict the environment to 'main' by hand",
            err,
        )
        # Theirs, kept; and no second PUT, so the environment is not open.
        self.assertEqual(fake.env_policies["lanes"], ["release/*"])
        self.assertEqual([env for env, _ in fake.env_puts], ["lanes"])

    def test_a_policy_added_before_the_failed_write_is_left_in_place(self):
        # The restore is over an empty list only: a pattern someone added
        # between the two writes is theirs, and putting the open policy back
        # would drop it and reopen the environment to every branch (Codex,
        # mikelward/repo#36). Left as it is, with the reason, for a hand.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.policy_post_fails = {"lanes"}
        fake.policy_post_fails_adding = {"lanes": "release/*"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("Validation Failed (policy)", err)
        self.assertIn(
            "a policy someone set meanwhile -- 'release/*' -- was left in place rather than reopened over; "
            "restrict the environment to 'main' by hand",
            err,
        )
        self.assertEqual([env for env, _ in fake.env_puts], ["lanes"])
        self.assertEqual(fake.env_policies["lanes"], ["release/*"])
        # Exactly the default branch added meanwhile is the wanted state:
        # done, whatever the failed write said.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.policy_post_fails = {"lanes"}
        fake.policy_post_fails_adding = {"lanes": "main"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "restricted environment 'lanes' to branch 'main' (the branch policy write failed, and the "
            "branch was added meanwhile)",
            out,
        )
        self.assertEqual([env for env, _ in fake.env_puts], ["lanes"])
        self.assertEqual(fake.env_policies["lanes"], ["main"])

    def test_the_restore_resends_the_settings_the_environment_has_now(self):
        # The snapshot the restore would otherwise reuse was taken before
        # the policy PUT, and the branch-policy re-read between them sees
        # no protection settings at all -- so an administrator's change in
        # that window was silently reverted by the restore (Codex,
        # mikelward/repo#36).
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_admin_bypass = {"lanes": False}
        fake.policy_post_fails = {"lanes"}
        fake.admin_bypass_after_failed_post = {"lanes": True}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertEqual([env for env, _ in fake.env_puts], ["lanes", "lanes"])
        first, restore = fake.env_puts[0][1], fake.env_puts[1][1]
        self.assertIs(first["can_admins_bypass"], False)
        self.assertIs(restore["can_admins_bypass"], True)
        self.assertIsNone(restore["deployment_branch_policy"])
        self.assertIsNone(fake.env_policies["lanes"])

    def test_a_policy_mode_set_before_the_restore_is_left_alone(self):
        # The branch-policy list says nothing about the mode, so an
        # administrator switching to protected mode between that read and
        # the restore's own would have been reopened over by a PUT that
        # came for the settings and ignored the mode it was handed (Codex,
        # mikelward/repo#36).
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.policy_post_fails = {"lanes"}
        # The fifth environment read is the restore's own.
        fake.env_policies_after_read = {"lanes": (5, "protected")}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "the environment's policy mode was set to 'protected' meanwhile, so the open policy was "
            "not restored over it; restrict the environment to 'main' by hand",
            err,
        )
        # Only this run's own PUT into custom mode; no restore over theirs.
        self.assertEqual([env for env, _ in fake.env_puts], ["lanes"])
        self.assertEqual(fake.env_policies["lanes"], "protected")

    def test_a_policy_mode_found_after_the_failed_write_is_named_whole(self):
        # `now` is a mode here, not a list of patterns, and joining over it
        # spelled it one character at a time.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.policy_post_fails = {"lanes"}
        fake.env_policies_after_read = {"lanes": (4, "protected")}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("a policy someone set meanwhile -- 'protected' -- was left in place", err)

    def test_an_unreadable_policy_holds_this_runs_moves(self):
        # `plan.failed` is recorded at the end of the apply and the moves
        # run first, so a forced run wrote the pair into an environment
        # nobody had established was shut, and deleted the repository
        # copies behind it (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_get_check_fails = {"lanes"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not read owner/repo's 'lanes' environment:", err)
        self.assertIn(
            "lanes: LANES_APP_ID, LANES_APP_PRIVATE_KEY stays a repository secret until the policy "
            "can be read",
            err,
        )
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.restricted, [])
        self.assertEqual(fake.env_puts, [])

    def test_a_half_done_policy_finished_before_the_failed_write_is_done(self):
        # Custom mode naming no branch writes no PUT, so it had no restore
        # and took no post-failure re-read either -- and reported failure
        # over a policy another run had just installed (Codex,
        # mikelward/repo#36).
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": []}
        fake.policy_post_fails = {"lanes"}
        fake.policy_post_fails_adding = {"lanes": "main"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "restricted environment 'lanes' to branch 'main' (the branch policy write failed, and the "
            "branch was added meanwhile)",
            out,
        )
        # No PUT: this run never left custom mode, so there is nothing to
        # restore and nothing of the environment's settings to rewrite.
        self.assertEqual(fake.env_puts, [])
        self.assertEqual(fake.env_policies["lanes"], ["main"])

    def test_a_half_done_policy_still_empty_after_the_failed_write_fails(self):
        # The other direction: the re-read is not an excuse to pass.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": []}
        fake.policy_post_fails = {"lanes"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("Validation Failed (policy)", err)
        self.assertEqual(fake.env_puts, [])
        self.assertEqual(fake.env_policies["lanes"], [])

    def test_a_restriction_left_half_done_is_completed(self):
        # Custom-policy mode naming no branch: nobody sets it, and it is
        # what a failed restore above would leave. Completed with the
        # second write alone -- no PUT, so nothing else is touched.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": []}
        code, out, err = _run(fake, ["--force", "-v", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("lanes: restrict environment 'lanes' to branch 'main' -- it admits no branch at all", err)
        self.assertEqual(fake.env_puts, [])
        self.assertEqual(fake.restricted, [("lanes", "main")])
        self.assertEqual(fake.env_policies["lanes"], ["main"])

    def test_a_publisher_that_went_away_while_the_plan_waited_is_not_restricted(self):
        # The environment already holds the pair and nothing is deleted, so
        # the restriction is the only apply-time action -- and it is held
        # to the same recheck a delete is (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.workflow_texts_after_recheck = {"ci.yml": "jobs: {}\n"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "environment 'lanes' not restricted: the publishing workflows changed since the plan was built (none now)",
            err,
        )
        self.assertEqual(fake.env_puts, [])
        self.assertEqual(fake.restricted, [])

    def test_a_default_branch_renamed_while_the_plan_waited_is_not_restricted(self):
        # The restriction names the default branch; restricting to the old
        # name would shut the new trusted branch out and let the stale one
        # in (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.default_branch_after_bootstrap_plan = "trunk"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("environment 'lanes' not restricted: the default branch is now 'trunk', not 'main'", err)
        self.assertEqual(fake.restricted, [])

    def test_a_deleted_environment_is_not_recreated_open_by_the_writes(self):
        # `env_exists` on each write is a PLAN-TIME snapshot, so the writes
        # re-ran `_ensure_environment` on the environment this run had just
        # restricted -- and a deletion in that window is a 404 there, which
        # CREATES it again with GitHub's default open policy. The pair then
        # went in and the repository copies came out, into an environment
        # any branch can enter: the run closed the door, somebody removed
        # the door, and the run rebuilt it open and put the credential
        # behind it, reporting success (Codex, mikelward/repo#36).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set(self.PAIR)
            fake.env_deleted_after_restrict = {"lanes"}
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        # Nothing written, nothing deleted, and no environment recreated
        # for the pair to sit in.
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [])
        self.assertNotIn("lanes", fake.env_secret_names)
        self.assertIn("could not set 'LANES_APP_ID' on owner/repo (environment lanes)", err)
        # The repository copies are the fallback, and they stay.
        self.assertIn("LANES_APP_ID kept: the write it waited on failed", err)

    def test_an_environment_reopened_while_the_pair_is_written_keeps_the_copies(self):
        # Every other check this move makes happens BEFORE its writes, so
        # the writes' own window was the last one open: an administrator
        # reopening the environment there had the pair written into it and
        # the repository copies deleted behind it, and the run exited 0
        # with the credential reachable from an untrusted branch (Codex,
        # mikelward/repo#36). The deletes are the irreversible half, so
        # they are what the post-write confirmation gates -- and the halves
        # this run WROTE are taken back out, since reporting alone left the
        # credential sitting in an environment every branch could now read
        # (Codex, mikelward/repo#36 again). The repository copies are the
        # fallback and stay, so the run leaves the state it found.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set(self.PAIR)
            fake.env_reopened_during_writes = {"lanes"}
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        # Only this run's own environment writes are undone; the repository
        # copies -- the irreversible half and the working fallback -- stay.
        self.assertEqual(
            sorted(fake.deleted_secrets),
            [("LANES_APP_ID", "lanes"), ("LANES_APP_PRIVATE_KEY", "lanes")],
        )
        self.assertEqual(fake.env_secret_names["lanes"], set())
        self.assertEqual(fake.secret_names, set(self.PAIR))
        self.assertIn("undid the write of 'LANES_APP_ID' (environment 'lanes')", out)
        self.assertIn(
            "LANES_APP_ID kept: environment 'lanes' can be reached from any branch after the "
            "credential was written",
            err,
        )
        # Nothing is rewritten: the policy is left as whoever changed it
        # left it, and the next run re-plans against what it finds.
        self.assertEqual(fake.restricted, [("lanes", "main")])
        self.assertIsNone(fake.env_policies["lanes"])

    def test_a_first_write_into_a_reopened_environment_is_taken_back_out(self):
        # The pair is placed into an environment that holds neither half,
        # and an administrator reopens it mid-write. Both halves are this
        # run's, so both come back out: leaving them reported the exposure
        # while creating it, with a credential the operator had just handed
        # in readable from every branch (Codex, mikelward/repo#36). There
        # are no repository copies here, so the state restored is the one
        # the run found -- nothing anywhere.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set()
            fake.env_secret_names = {"lanes": set()}
            fake.env_policies = {"lanes": ["main"]}
            fake.env_reopened_during_writes = {"lanes"}
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        self.assertEqual(fake.env_secret_names["lanes"], set())
        self.assertEqual(
            sorted(fake.deleted_secrets),
            [("LANES_APP_ID", "lanes"), ("LANES_APP_PRIVATE_KEY", "lanes")],
        )
        self.assertIn("undid the write of 'LANES_APP_PRIVATE_KEY' (environment 'lanes')", out)
        self.assertIn(
            "lanes: environment 'lanes' can be reached from any branch after the credential was written",
            err,
        )

    def test_a_rotation_with_nothing_to_delete_still_confirms_the_environment(self):
        # A rotation into an environment that already holds the pair
        # deletes nothing, so gating the post-write confirmation on the
        # deletes skipped it exactly where the run had just put a FRESH
        # credential somewhere any branch could read it -- and then
        # reported the repository in shape (Codex, mikelward/repo#36).
        # Nothing can be UNDONE on this path -- both halves are overwrites,
        # whose old values are gone, and deleting them would leave no
        # credential at all -- so the run names them instead of taking them
        # back out (Codex, mikelward/repo#36).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set()  # nothing at repository level to delete
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            fake.env_reopened_during_writes = {"lanes"}
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted_secrets, [])
        self.assertIn(
            "lanes: environment 'lanes' can be reached from any branch after the credential was written",
            err,
        )
        self.assertIn(
            "LANES_APP_ID holds the new value in environment 'lanes', which can be reached from any "
            "branch -- it cannot be taken back out without leaving no credential at all",
            err,
        )

    def test_a_half_written_pair_is_rolled_back_rather_than_shadowing(self):
        # An environment secret shadows the repository copy of the same
        # name for every job declaring that environment. So a half that
        # lands while its partner's write fails leaves such a job
        # authenticating with one new half and one old one -- a pair that
        # worked before the run, broken by it (Codex, mikelward/repo#36).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set(self.PAIR)  # both halves, and they work
            fake.env_secret_names = {"lanes": set()}
            fake.env_policies = {"lanes": ["main"]}
            fake.set_fails = {"LANES_APP_PRIVATE_KEY"}
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        # The half that landed is undone, so the working repository pair is
        # what every job sees again.
        self.assertEqual(fake.env_secret_names["lanes"], set())
        self.assertEqual(fake.deleted_secrets, [("LANES_APP_ID", "lanes")])
        self.assertEqual(fake.secret_names, set(self.PAIR))
        self.assertIn("undid the write of 'LANES_APP_ID' (environment 'lanes')", out)
        self.assertIn("LANES_APP_ID kept: the write it waited on failed", err)

    def test_the_first_failed_write_stops_the_rest(self):
        # The inverse ordering of the partial rotation below: when the
        # FIRST half's write fails, carrying on wrote the second over a
        # pair that was working, breaking it in the one direction nothing
        # can undo -- where stopping leaves the credential exactly as the
        # run found it (Codex, mikelward/repo#36).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set()
            fake.env_secret_names = {"lanes": set(self.PAIR)}  # a working pair
            fake.env_policies = {"lanes": ["main"]}
            fake.set_fails = {"LANES_APP_ID"}
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        # The second write never happens, so nothing is mismatched and the
        # run has nothing to report about a half it left behind.
        self.assertEqual(
            [c[2] for c in fake.calls if c[:2] == ["secret", "set"]], ["LANES_APP_ID"]
        )
        self.assertNotIn("now holds the new value", err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_partial_rotation_says_the_pair_it_left_mismatched(self):
        # Rotating overwrites both halves, so neither is one this run
        # created and the rollback has nothing to undo: the environment is
        # left with one new half and one old one, and the App cannot
        # authenticate (Codex, mikelward/repo#36). Nothing can put it back
        # -- GitHub never returns a secret's value -- so the run says what
        # it left rather than reporting a bare write failure.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set()
            fake.env_secret_names = {"lanes": set(self.PAIR)}  # a working pair
            fake.env_policies = {"lanes": ["main"]}
            fake.set_fails = {"LANES_APP_PRIVATE_KEY"}
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "LANES_APP_ID now holds the new value in environment 'lanes' while its other half does "
            "not, so the App cannot authenticate until both are set",
            err,
        )
        # Not deleted: that would lose the credential outright rather than
        # leave it mismatched, and the old value is not recoverable either.
        self.assertEqual(fake.deleted_secrets, [])

    def test_an_unreadable_rollback_inventory_stops_the_writes(self):
        # The rollback needs to know which halves this run creates. Reading
        # that can fail, and carrying on with an empty inventory made the
        # read failure silently disable the very thing that keeps a
        # half-written pair from shadowing a working repository one (Codex,
        # mikelward/repo#36). So the writes do not start.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set(self.PAIR)
            fake.env_secret_names = {"lanes": set()}
            fake.env_policies = {"lanes": ["main"]}
            fake.env_secrets_fail_after = {"lanes": 2}  # the plan's read lands; this one does not
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [])
        self.assertIn(
            "LANES_APP_ID not set: the environment's secrets could not be read first", err
        )

    def test_a_repoint_while_the_workflows_are_read_refuses_the_whole_plan(self):
        # Pinning the workflow reads to the default branch turns a RENAME
        # into a 404, but a repoint to a branch that still exists answers
        # happily from the old one. The plan then judged the environment's
        # policy against a name that had stopped being the default -- and
        # where nothing needed doing it queued no move, so no apply-time
        # recheck ever ran and the run reported the repository healthy
        # while the real default branch was shut out of its own
        # environment (Codex, mikelward/repo#36). The name is confirmed
        # after the texts, so the two are one reading.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": ["main"]}
        fake.default_branch_after_bootstrap_plan = "trunk"
        fake.default_branch_renamed_after_read = 1  # between the two reads of the name
        # --no-bootstrap so the credentials plan is the step under test:
        # with the scaffold step running, IT catches the repoint first and
        # the run never reaches the reading this pins.
        code, out, err = _run(fake, ["--force", "--no-rules", "--no-bootstrap", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "default branch changed from 'main' to 'trunk' while its workflows were being read",
            err,
        )
        self.assertEqual(fake.restricted, [])
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_rename_between_the_recheck_s_two_reads_is_caught(self):
        # The window a name checked BEFORE the workflows left open: the
        # rename lands after that check and before the workflow read, so
        # the name agrees while the state read describes the new branch's
        # copies. Identical states then passed the comparison and the run
        # restricted the environment to the old name, reporting success
        # (Codex, mikelward/repo#36). Confirming the name after the read
        # catches it, since the read is what the rename precedes.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.default_branch_after_bootstrap_plan = "trunk"
        fake.default_branch_renamed_after_read = 5
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("environment 'lanes' not restricted: the default branch is now 'trunk', not 'main'", err)
        self.assertEqual(fake.restricted, [])
        self.assertEqual(fake.env_puts, [])

    def test_a_rename_holds_back_the_delete_of_an_unused_pair(self):
        # The no-publisher path deletes, and which copies count as the
        # default branch's is what "no publisher" was read from -- so the
        # rename refuses there too. One comparison for both, rather than a
        # second check beside it (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.secret_names = set(self.PAIR)
        fake.default_branch_after_bootstrap_plan = "trunk"
        fake.default_branch_renamed_after_read = 5
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("the default branch is now 'trunk', not 'main'", err)
        self.assertEqual(fake.deleted_secrets, [])

    COMPOSITE = (
        "name: ci\non: pull_request_target\njobs:\n  init:\n    runs-on: ubuntu-latest\n"
        "    environment: lanes\n    steps:\n      - uses: ./.github/actions/lanes-init\n"
        "        with:\n          app-id: ${{ secrets.LANES_APP_ID }}\n"
        "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n"
    )

    MIXED = (
        "name: ci\non: pull_request_target\njobs:\n"
        "  init:\n    runs-on: ubuntu-latest\n    environment: lanes\n    steps:\n"
        "      - uses: mikelward/lanes@main\n        with:\n          mode: init\n"
        "          app-id: ${{ secrets.LANES_APP_ID }}\n"
        "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n"
        "  extra:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: ./.github/actions/lanes-extra\n        with:\n"
        "          app-id: ${{ secrets.LANES_APP_ID }}\n"
        "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n"
    )

    def test_a_readable_publisher_does_not_vouch_for_the_rest_of_its_file(self):
        # One job publishes readably, another hands the pair to a local
        # composite action. Asking only whether the FILE had a lanes step
        # excluded it whole, and setup then moved and deleted the pair on
        # the strength of the publisher, breaking the consumer it never saw
        # (Codex, mikelward/repo#36). The accounting is per reference: a
        # mention counts unless a resolved step was handed it.
        fake = self._publisher(self.MIXED)
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted_secrets, [])
        self.assertIn("with no mikelward/lanes step this can read taking them", err)

    def test_a_pair_named_with_no_readable_step_is_never_deleted(self):
        # A workflow can hand the pair to a local composite action, and the
        # `mikelward/lanes` reference then lives in that action's own
        # `action.yml` -- a file this reads none of. So every reader came
        # back empty, the pair read as used by nothing, and all FOUR copies
        # were deleted: the publisher broken and the values gone, since
        # GitHub never gives a secret back (Codex, mikelward/repo#36).
        fake = self._publisher(self.COMPOSITE)
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.written_secrets, [])
        self.assertIn(
            "names LANES_APP_ID or LANES_APP_PRIVATE_KEY with no mikelward/lanes step this can "
            "read taking them",
            err,
        )

    def test_a_policy_set_while_the_plan_waited_fails_the_run(self):
        # The plan saw an open environment; by apply time someone set a
        # policy of their own. Left alone -- and reported as a failure, not
        # as a done step (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies_after_recheck = {"lanes": ["release/*"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: environment 'lanes' is restricted to 'release/*', not to 'main' alone -- set "
            "since the plan was built, and a policy someone set is not rewritten; restrict it to 'main' by hand",
            err,
        )
        self.assertEqual(fake.env_puts, [])
        self.assertEqual(fake.restricted, [])

    def test_a_policy_set_between_the_two_reads_fails_the_run(self):
        # The restriction reads the policy, then the environment again for
        # the settings its PUT must resend; a policy set between the two
        # is refused off the second read, never overwritten (Codex,
        # mikelward/repo#36). The plan's read is the first, the apply-time
        # policy recheck the second, the settings read the third.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies_after_read = {"lanes": (3, "protected")}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("changed its deployment branch policy between two reads of it", err)
        self.assertEqual(fake.restricted, [])
        self.assertEqual(fake.env_puts, [])

    def test_a_pattern_added_before_the_half_done_completion_fails_the_run(self):
        # Custom mode naming no branch is completed with one POST, and the
        # policy list is re-read right before it: a pattern someone added
        # meanwhile would otherwise gain the default branch beside it
        # (Codex, mikelward/repo#36). Reads: the plan's, the apply-time
        # recheck, the re-list before the POST.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": []}
        fake.env_policies_after_read = {"lanes": (3, ["release/*"])}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("changed its deployment branch policy between two reads of it", err)
        self.assertEqual(fake.restricted, [])
        self.assertEqual(fake.env_puts, [])

    def test_a_half_done_completion_finished_meanwhile_is_done(self):
        # Another run (or a hand) added exactly the default branch between
        # the two reads: the environment is in the wanted state, which is
        # done, not a conflict (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": []}
        fake.env_policies_after_read = {"lanes": (3, ["main"])}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("environment 'lanes' was restricted to branch 'main' since the plan was built", out)
        self.assertEqual(fake.restricted, [])
        self.assertEqual(fake.env_puts, [])

    def test_a_restricted_environment_needs_nothing(self):
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": ["main"]}
        # Alongside another step, so the combined plan is shown at all.
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("lanes: the credential lives in the 'lanes' environment", out)
        self.assertIn("lanes: environment 'lanes' admits only the trusted base branch", out)
        self.assertEqual(fake.restricted, [])
        self.assertEqual(fake.env_puts, [])

    def test_an_uncredentialed_gate_is_reported_and_the_pair_still_placed(self):
        # `init` holds the pair, so the classify-only finding stays quiet
        # while the step posting the required verdict has no credential and
        # falls back to the ambient check-run in silence (Codex,
        # mikelward/repo#36). The pair is still placed and the environment
        # still restricted -- the credentialed steps need both.
        mixed = self.PUBLISHER + """  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: mikelward/lanes@main
        with:
          mode: gate
"""
        fake = self._publisher(mixed)
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: ci runs mikelward/lanes in `gate` mode with no App credential",
            err,
        )
        self.assertNotIn("only to steps that publish no status", err)
        # Placed and shut regardless: this is a report, not a hold.
        self.assertEqual(fake.restricted, [("lanes", "main")])
        self.assertEqual(sorted(name for name, _e in fake.deleted_secrets), sorted(self.PAIR))

    def test_a_policy_someone_set_is_reported_not_rewritten(self):
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": ["release/*"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: environment 'lanes' is restricted to 'release/*', not to 'main' alone -- "
            "restrict it to 'main' in the environment's settings; a policy someone set is not rewritten",
            err,
        )
        self.assertEqual(fake.env_puts, [])
        self.assertEqual(fake.restricted, [])

    def test_a_policy_opened_while_the_plan_waited_holds_the_move(self):
        # The policy was right when the plan was shown, so nothing queued a
        # restriction and nothing else re-read the door -- the move's own
        # recheck asks what the environment holds, not who may enter it
        # (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": ["main"]}
        # Opened between the plan's read and the apply's.
        fake.env_policies_after_read = {"lanes": (2, None)}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("environment 'lanes' now can be reached from any branch", err)
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.env_puts, [])

    def test_a_trusted_policy_still_trusted_at_apply_time_lets_the_move_run(self):
        # The other direction: the re-read is a gate, not a refusal.
        fake = self._publisher()
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            sorted(name for name, _env in fake.deleted_secrets), sorted(self.PAIR)
        )

    def test_a_policy_someone_set_holds_the_repository_copies(self):
        # The environment is shut before the credential goes into it, and a
        # policy someone set is never rewritten -- so this environment
        # cannot be shut, and deleting the repository copies would leave
        # the pair only where a branch the policy admits can read it
        # (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": ["release/*"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: environment 'lanes' is restricted to 'release/*', not to 'main' alone -- "
            "restrict it to 'main' in the environment's settings; a policy someone set is not "
            "rewritten; LANES_APP_ID, LANES_APP_PRIVATE_KEY stays a repository secret until then",
            err,
        )
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.restricted, [])
        self.assertEqual(fake.env_puts, [])
        # The plan never offered the deletes it is not going to make.
        self.assertNotIn("delete repository secret LANES_APP_ID", out + err)

    def test_a_policy_someone_set_holds_a_supplied_pair(self):
        # Same hold on the writes, and the declined names are said whatever
        # the verbosity: placing the pair here would create the exposure
        # the finding is about (Codex, mikelward/repo#36).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set()}
            fake.env_policies = {"lanes": "protected"}
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        self.assertIn("admits protected branches", err)
        for name in self.PAIR:
            self.assertIn(
                f"lanes: {name} not set -- the 'lanes' environment admits a branch this cannot "
                f"trust, and the credential is not placed where such a branch reads it",
                err,
            )
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.restricted, [])
        self.assertEqual(fake.env_puts, [])
        self.assertNotIn("set LANES_APP_ID in environment", out + err)

    def test_a_publisher_without_the_environment_holds_the_move_back(self):
        fake = self._publisher(self.PUBLISHER.replace("    environment: lanes\n", ""))
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: ci publishes the lanes status as the App from a job that does not declare "
            "`environment: lanes`, so a credential in the 'lanes' environment would never reach it -- "
            "declare the environment on every job that takes `app-id` first; LANES_APP_ID, "
            "LANES_APP_PRIVATE_KEY left as is",
            err,
        )
        self.assertEqual(fake.deleted_secrets, [])
        # The environment is not touched while the credential is stranded.
        self.assertEqual(fake.restricted, [])

    def test_a_branch_copy_without_the_environment_does_not_hold_the_move_back(self):
        # The default branch's publisher declares the environment; a copy
        # on a branch does not. That copy runs from its branch, which the
        # restricted environment shuts out either way, so it loses the
        # credential when the repository copy moves -- and keeping the
        # repository copy for its sake would leave the pair exposed to
        # exactly that branch's push-triggered run (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.branch_workflows = {"feature": {"ci.yml": self.PUBLISHER.replace("    environment: lanes\n", "")}}
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(fake.deleted_secrets), [("LANES_APP_ID", None), ("LANES_APP_PRIVATE_KEY", None)])
        self.assertNotIn("not fixed", err)
        # And the default branch's own publisher still does hold it back,
        # whatever a branch copy declares.
        fake = self._publisher(self.PUBLISHER.replace("    environment: lanes\n", ""))
        fake.branch_workflows = {"feature": {"ci.yml": self.PUBLISHER}}
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("not fixed: lanes: ci publishes the lanes status", err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_publisher_only_on_a_branch_keeps_the_pair_and_says_so(self):
        # The pull request adopting lanes: nothing on the default branch
        # publishes yet, the branch copy will once merged. The pair moves
        # and the environment is restricted -- what that merge needs -- and
        # the plan says no publisher reaches it yet (Codex, mikelward/repo#36).
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.branch_workflows = {"feature": {"ci.yml": self.PUBLISHER}}
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        code, out, err = _run(fake, ["--force", "--no-rules", "-v", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "lanes: no workflow on the default branch publishes the lanes status as the App; ci on "
            "feature does from a branch, which reaches the environment once merged -- the pair is "
            "kept for it",
            out + err,
        )
        self.assertNotIn("nothing uses it", out + err)
        self.assertEqual(sorted(fake.deleted_secrets), [("LANES_APP_ID", None), ("LANES_APP_PRIVATE_KEY", None)])
        self.assertEqual(len(fake.restricted), 1)

    def test_a_branch_only_publisher_that_went_away_holds_the_apply_back(self):
        # The plan rested on the branch copy alone; gone while the prompt
        # sat, the apply would otherwise leave a pair nothing uses (Codex,
        # mikelward/repo#36).
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.branch_workflows = {"feature": {"ci.yml": self.PUBLISHER}}
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.branch_workflows_after_recheck = {"feature": {"ci.yml": "jobs: {}\n"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("the publishing workflows changed since the plan was built (none now)", err)
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.restricted, [])

    def test_half_the_pair_handed_to_the_action_holds_everything(self):
        # Neither unused nor a publisher: the pair stays where it is, and the
        # environment is not touched, until the step takes both inputs.
        fake = self._publisher(self.PUBLISHER.replace("          app-id: ${{ secrets.LANES_APP_ID }}\n", ""))
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: ci hands mikelward/lanes one of `app-id` and `app-private-key` without the "
            "other, so the step cannot authenticate as the App -- hand it both first; LANES_APP_ID, "
            "LANES_APP_PRIVATE_KEY left as is",
            err,
        )
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.restricted, [])

    def test_a_pair_nothing_publishes_with_is_deleted(self):
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {
            "ci.yml": "jobs:\n  classify:\n    steps:\n      - uses: mikelward/lanes@main\n        with:\n          mode: classify\n"
        }
        fake.secret_names = {"LANES_APP_ID"}
        fake.env_secret_names = {"lanes": {"LANES_APP_PRIVATE_KEY"}}
        code, out, err = _run(fake, ["--force", "-v", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "lanes: delete repository secret LANES_APP_ID -- no workflow here publishes the lanes status as "
            "the App (a mikelward/lanes step taking `app-id`), so nothing uses it",
            err,
        )
        self.assertEqual(sorted(fake.deleted_secrets), [("LANES_APP_ID", None), ("LANES_APP_PRIVATE_KEY", "lanes")])
        self.assertEqual(fake.restricted, [])

    def test_a_reusable_workflow_call_this_cannot_read_holds_the_delete(self):
        # The called workflow's own file is not among the texts, so a lanes
        # step in it is invisible: the caller names neither the action nor
        # either secret, every reader came back empty, and both copies were
        # deleted with their values (Codex, mikelward/repo#36).
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {
            "ci.yml": "jobs:\n  ci:\n    uses: some-org/shared/.github/workflows/ci.yml@main\n    secrets: inherit\n"
        }
        fake.secret_names = {"LANES_APP_ID"}
        fake.env_secret_names = {"lanes": {"LANES_APP_PRIVATE_KEY"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: ci calls a reusable workflow this cannot read, which may be what "
            "publishes; LANES_APP_ID, LANES_APP_PRIVATE_KEY left as is",
            err,
        )
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_reusable_workflow_call_is_silent_with_no_pair_to_lose(self):
        # Every repository here calls some reusable workflow, so raising it
        # where the pair is not present would report on the many to protect
        # the few -- and there is nothing for `unused` to delete anyway.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {
            "ci.yml": "jobs:\n  ci:\n    uses: some-org/shared/.github/workflows/ci.yml@main\n    secrets: inherit\n"
        }
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("reusable workflow this cannot read", err)

    def test_a_local_reusable_workflow_call_is_read_directly(self):
        # Its file is among the texts, so its lanes step publishes on its
        # own account and the pair moves as usual.
        fake = self._publisher()
        fake.workflow_files = ["ci.yml", "call.yml"]
        fake.workflow_texts = dict(fake.workflow_texts)
        fake.workflow_texts["call.yml"] = (
            "jobs:\n  ci:\n    uses: ./.github/workflows/ci.yml\n    secrets: inherit\n"
        )
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("reusable workflow this cannot read", err)

    def test_a_reusable_call_beside_a_publisher_still_moves(self):
        # The other half of the audit's parity test: the reading is
        # consulted only where nothing publishes readably, so an unrelated
        # call does not hold back a repository whose own workflow publishes.
        fake = self._publisher()
        fake.workflow_files = list(fake.workflow_files) + ["call.yml"]
        fake.workflow_texts = dict(fake.workflow_texts)
        fake.workflow_texts["call.yml"] = (
            "jobs:\n  ci:\n    uses: some-org/shared/.github/workflows/ci.yml@main\n    secrets: inherit\n"
        )
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": ["main"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("reusable workflow this cannot read", err)
        self.assertEqual(sorted(fake.deleted_secrets), [("LANES_APP_ID", None), ("LANES_APP_PRIVATE_KEY", None)])

    def test_a_shape_the_reader_cannot_resolve_deletes_nothing(self):
        # A document PyYAML rejects resolves no step, so the mention is
        # "cannot tell" and holds everything back.
        fake = self._publisher("jobs: {init: {steps: [{uses: mikelward/lanes@main, with: {app-id: x}}]}\n")
        fake.secret_names = set(self.PAIR)
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("not fixed: lanes: ci mentions mikelward/lanes in a shape this cannot read as a step", err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_the_pair_reaching_only_classify_is_not_fixed(self):
        # `classify` takes the credential for the generated lane, so the
        # pair is placed and the environment restricted either way -- but
        # nothing publishes the required status as the App, which is the
        # silent fallback to the ambient check-run mikelward/lanes's README
        # warns about, so the run says so and fails (Codex,
        # mikelward/repo#36).
        fake = self._publisher(self.PUBLISHER.replace("          mode: init\n", "          mode: classify\n"))
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: ci hands mikelward/lanes the App credential only to steps that publish "
            "no status -- `classify`, or a `mode` the action refuses to start on -- so the required "
            "`lanes` status is still the ambient check-run a pull request's own workflow produces "
            "-- hand the pair to the `init` and gate steps too",
            err,
        )
        # The move and the restriction still happen: classify needs both.
        self.assertEqual(sorted(fake.deleted_secrets), [("LANES_APP_ID", None), ("LANES_APP_PRIVATE_KEY", None)])
        self.assertEqual(fake.restricted, [("lanes", "main")])

    def test_a_step_wired_to_another_app_holds_the_pair(self):
        fake = self._publisher(
            self.PUBLISHER.replace("secrets.LANES_APP_ID", "secrets.OTHER_ID").replace(
                "secrets.LANES_APP_PRIVATE_KEY", "secrets.OTHER_KEY"
            )
        )
        fake.secret_names = set(self.PAIR)
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: ci hands mikelward/lanes `app-id` and `app-private-key` from secrets "
            "that are not LANES_APP_ID and LANES_APP_PRIVATE_KEY, so it publishes as an App this "
            "tool does not manage; LANES_APP_ID, LANES_APP_PRIVATE_KEY left as is",
            err,
        )
        # Neither moved nor deleted: a placement here would change nothing,
        # and a deletion is the destructive half of the same misreading.
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.restricted, [])

    def test_a_branch_copy_running_init_does_not_answer_for_the_default_branch(self):
        # The default branch hands the pair only to `classify` while a
        # feature branch's copy runs `init`. That copy publishes from a
        # branch the restricted environment shuts out, so it cannot stand
        # in for the trusted base -- reading it as one hid the finding
        # (Codex, mikelward/repo#36).
        fake = self._publisher(self.PUBLISHER.replace("          mode: init\n", "          mode: classify\n"))
        fake.branch_workflows = {"feature": {"ci.yml": self.PUBLISHER}}
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: ci hands mikelward/lanes the App credential only to steps that publish "
            "no status -- `classify`, or a `mode` the action refuses to start on --",
            err,
        )

    def test_a_branch_only_publisher_raises_no_classify_finding(self):
        # Nothing on the default branch publishes at all, which the plan
        # already says; the branch copy's mode is its own pull request's
        # business until it merges.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.branch_workflows = {
            "feature": {"ci.yml": self.PUBLISHER.replace("          mode: init\n", "          mode: classify\n")}
        }
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        code, out, err = _run(fake, ["--force", "-v", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("lanes: no workflow on the default branch publishes the lanes status as the App", out + err)
        self.assertNotIn("only to steps that publish no status", out + err)

    def test_a_publisher_that_stopped_publishing_a_status_while_the_plan_waited_holds_it_back(self):
        # Same workflow, same job, one word changed: `init` to `classify`
        # while the prompt sat. The names the recheck compares are
        # identical, so comparing them alone applied the plan and exited
        # clean while the required status had gone back to the ambient
        # check-run (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.workflow_texts_after_recheck = {
            "ci.yml": self.PUBLISHER.replace("          mode: init\n", "          mode: classify\n")
        }
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "environment 'lanes' not restricted: the workflows publishing the `lanes` status as the "
            "App changed since the plan was built (none now)",
            err,
        )
        self.assertEqual(fake.restricted, [])

    def test_a_policy_added_while_the_restriction_ran_fails_the_run(self):
        # Both writes land, which is not the same as the environment being
        # restricted: someone adding another custom policy between them
        # leaves it admitting an untrusted branch, and reporting success
        # over that says the run left the repository in shape (Codex,
        # mikelward/repo#36). The postcondition is what this function is
        # for, so it is read rather than inferred from two exit codes.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_policies = {"lanes": []}
        fake.policy_added_during_post = {"lanes": "release/*"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "not fixed: lanes: environment 'lanes' still admits 'main', 'release/*' after adding "
            "'main' -- a policy set while this ran, left in place rather than deleted; remove it by "
            "hand so only 'main' remains",
            err,
        )
        # Left in place: someone set it, and this deletes nobody's policy.
        self.assertEqual(fake.env_policies["lanes"], ["main", "release/*"])

    def test_a_finding_appearing_while_the_plan_waited_holds_the_apply_back(self):
        # The recheck compares everything the workflows say, as one value:
        # enumerating the facts by hand is what has to be got right every
        # time, and twice was not (Codex, mikelward/repo#36). Here a second
        # job wired to another App appears beside the publisher the plan
        # rested on -- the publisher list is unchanged, so a comparison of
        # names alone would apply the plan and exit clean.
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.workflow_texts_after_recheck = {
            "ci.yml": self.PUBLISHER + self.PUBLISHER.replace("jobs:\n  init:", "  second:").replace(
                "secrets.LANES_APP_ID", "secrets.OTHER_ID"
            ).replace("secrets.LANES_APP_PRIVATE_KEY", "secrets.OTHER_KEY")
        }
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "environment 'lanes' not restricted: ci now hands mikelward/lanes an App credential "
            "that is not LANES_APP_ID and LANES_APP_PRIVATE_KEY",
            err,
        )
        self.assertEqual(fake.restricted, [])

    def test_a_publisher_appearing_while_the_plan_waited_keeps_the_pair(self):
        # The other direction of the same comparison: the plan rested on
        # nothing publishing, so the delete it authorized is refused once
        # something does.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.workflow_texts_after_recheck = {"ci.yml": self.PUBLISHER}
        fake.secret_names = set(self.PAIR)
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("LANES_APP_ID kept: the publishing workflows changed since the plan was built", err)
        self.assertEqual(fake.deleted_secrets, [])
    def test_half_a_supplied_pair_is_not_written_at_all(self):
        # The repository holds one half and only the other is supplied, so
        # the run ends with the credential still unusable. Writing the
        # supplied half anyway put it in an environment the caller then
        # returned early without restricting -- readable by any branch,
        # beside the half still at repository level, which together are the
        # whole App credential (Codex, mikelward/repo#36).
        with tempfile.TemporaryDirectory() as tmp:
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = {"LANES_APP_ID"}
            code, out, err = _run(
                fake,
                ["--force", "--no-rules", "--credential", f"LANES_APP_PRIVATE_KEY={key}", REPO],
            )
        self.assertEqual(code, 1)
        self.assertIn("environment 'lanes' holds no credential", err)
        self.assertIn(
            "lanes: LANES_APP_PRIVATE_KEY not set -- half a credential is not one, and the rest "
            "of it has to arrive in the same run",
            err,
        )
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.restricted, [])
        # The complete pair in the same run is written, so the guard is on
        # "unusable after this run", not on "supplied".
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            both = self._publisher()
            both.secret_names = {"LANES_APP_ID"}
            code, out, err = _run(
                both,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 0, err)
        self.assertEqual([w[0] for w in both.written_secrets], ["LANES_APP_ID", "LANES_APP_PRIVATE_KEY"])
        self.assertEqual(both.restricted, [("lanes", "main")])

    def test_the_half_already_in_the_environment_is_re_read_before_the_write(self):
        # Completing a half the environment already holds queues a write
        # and no delete, and the destination re-read was gated on there
        # being a delete -- so it skipped exactly the case where the other
        # half is the thing that might be gone. Setup wrote one secret,
        # restricted the environment, and exited 0 with a credential that
        # cannot authenticate (Codex, mikelward/repo#36).
        with tempfile.TemporaryDirectory() as tmp:
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": {"LANES_APP_ID"}}
            fake.env_secret_names_after_recheck = {"lanes": set()}
            code, out, err = _run(
                fake,
                ["--force", "--no-rules", "--credential", f"LANES_APP_PRIVATE_KEY={key}", REPO],
            )
        self.assertEqual(code, 1)
        self.assertIn("no longer holds the credential", err)
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.restricted, [])

    def test_a_pair_removed_while_the_plan_waited_holds_the_restriction_back(self):
        # The restriction-only move writes and deletes nothing, so `move`'s
        # own destination re-read never runs for it. Asking only what the
        # workflows say restricted an environment an administrator had
        # emptied meanwhile and exited 0, while a fresh plan and the audit
        # both report no usable credential (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.env_secret_names_after_recheck = {"lanes": {"LANES_APP_ID"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "environment 'lanes' not restricted: the 'lanes' environment no longer holds the credential",
            err,
        )
        self.assertEqual(fake.restricted, [])
        self.assertEqual(fake.env_puts, [])

    def test_a_failed_restriction_holds_back_the_writes_and_the_deletes(self):
        # The restriction is not a step beside the move, it is the door the
        # move goes through. Failing it used to fail the run and delete the
        # repository copies anyway, leaving the pair only in an environment
        # any branch can enter -- and with a supplied value, writing it
        # there in the first place (Codex, mikelward/repo#36).
        fake = self._publisher()
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        fake.restrict_fails = {"lanes"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"could not restrict environment 'lanes' on {REPO}:", err)
        self.assertIn("LANES_APP_ID kept: the environment was not restricted", err)
        self.assertEqual(fake.deleted_secrets, [])

    def test_a_failed_restriction_writes_no_supplied_value(self):
        # The same door, on the path that would otherwise CREATE the
        # exposure: a supplied pair written into an environment whose
        # restriction then failed is a credential every branch can read,
        # where before the run there was none there at all.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set(self.PAIR)
            fake.restrict_fails = {"lanes"}
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        self.assertIn("LANES_APP_ID not set: the environment was not restricted", err)
        self.assertEqual(fake.written_secrets, [])
        self.assertEqual(fake.deleted_secrets, [])


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

    def test_a_second_mention_no_caller_resolves_in_a_read_file_holds_the_move_back(self):
        # The readable caller does not vouch for a mention beside it that
        # resolves to no caller -- one that is not a job's `uses:`.
        fake = self._consumer(
            self.INHERITING
            + "  weekly:\n    runs-on: ubuntu-latest\n"
            "    env:\n      HUB: mikelward/gradle-update/.github/workflows/gradle-update.yml@main\n"
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
            f"GRADLE_UPDATE_PAT kept: could not list {REPO}'s workflows on branch main (the plan could not "
            "be re-validated)",
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
        # A document PyYAML rejects is not a caller the reader can see, but
        # it IS a mention -- and "unused" must mean absent from the text,
        # not unparsed. Nothing deleted.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml", "batch.yml"]
        fake.workflow_texts = {
            "ci.yml": "jobs: {sync: {uses: mikelward/ci-commit-artifact/.github/workflows/commit-artifact.yml@main, secrets: inherit}\n",
            "batch.yml": "jobs: {update: {uses: mikelward/npm-update/.github/workflows/npm-update.yml@main, secrets: inherit}\n",
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
            "ci.yml": "jobs: {sync: {uses: mikelward/ci-commit-artifact/.github/workflows/commit-artifact.yml@main}\n"
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
        # The write is a ref CREATE -- the gap branch the pull request is
        # opened from -- which is a POST to the plural collection itself,
        # not to a ref path under it.
        self.assertEqual(len(fake.created_refs), 1)
        self.assertTrue(
            fake.created_refs[0]["ref"].startswith(f"refs/heads/{scaffold.GAP_BRANCH_PREFIX}"),
            fake.created_refs[0],
        )

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
        self.assertIn("open a pull request adding 7 file(s):", out)
        self.assertIn("already present, untouched: 2 file(s)", out)

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42 adding 7 fleet CI scaffold file(s)", out)
        self.assertIn("https://github.com/owner/repo/pull/42", out)
        blob_paths = {body["encoding"] for _args, body in fake.posts if "encoding" in body}
        self.assertEqual(blob_paths, {"utf-8"})
        blob_posts = [body for _args, body in fake.posts if "encoding" in body]
        self.assertEqual(len(blob_posts), 7)  # one blob per missing file, none for the two present
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
                "AGENTS.md",
                "CLAUDE.md",
            },
        )
        commit_posts = [body for _args, body in fake.posts if "parents" in body]
        self.assertEqual(len(commit_posts), 1)
        self.assertEqual(commit_posts[0]["parents"], [fake.bootstrap_commit_sha])
        # The commit goes on a branch of its own and the pull request is
        # opened from it -- nothing is written to the default branch, so
        # a ruleset requiring pull requests cannot block any of this.
        self.assertEqual(
            fake.created_refs,
            [{"ref": "refs/heads/repo-setup/fleet-ci-scaffold-newscaf", "sha": "newscaffoldcommitsha"}],
        )
        self.assertEqual(len(fake.created_pulls), 1)
        self.assertEqual(fake.created_pulls[0]["base"], "main")
        self.assertEqual(fake.created_pulls[0]["head"], "repo-setup/fleet-ci-scaffold-newscaf")
        self.assertEqual(fake.created_pulls[0]["title"], "Add missing fleet CI scaffold files")
        self.assertIn("`.github/workflows/ci.yml`", fake.created_pulls[0]["body"])
        self.assertEqual(fake.patches, [])

    def test_a_symlinked_claude_md_counts_as_present_not_occupied(self):
        # Most of this fleet points CLAUDE.md at AGENTS.md with a symlink,
        # which is the scaffold's content by another route. Treating it as
        # an occupied path -- the rule for every other non-regular file --
        # would fail the whole bootstrap step on those repositories over a
        # file that is already exactly right.
        fake = FakeGh()
        fake.bootstrap_occupied_entries = {"CLAUDE.md": ("blob", "120000")}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("cannot add CLAUDE.md", err)
        self.assertNotIn("add CLAUDE.md", out)

    def test_a_symlink_anywhere_else_is_still_an_occupied_path(self):
        # The exemption is one named path, not "a symlink is fine": one at
        # ci.yml could point anywhere, and silently replacing it is what
        # this step promises never to do.
        fake = FakeGh()
        fake.bootstrap_occupied_entries = {".github/workflows/ci.yml": ("blob", "120000")}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("cannot add .github/workflows/ci.yml", err)

    def test_the_scaffolded_conventions_carry_the_shared_ones_and_the_placeholders(self):
        # A freshly created repository is the one place in the fleet an
        # agent works with no conventions loaded at all, which is exactly
        # when it is most likely to invent some. So the file carries the
        # shared rules rather than deferring to a user-level file that a
        # remote session may not load, with the repository-specific parts
        # left as explicit TODOs.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        blobs = {}
        tree_posts = [body for _args, body in fake.posts if "base_tree" in body]
        blob_posts = [body for _args, body in fake.posts if "encoding" in body]
        for entry, body in zip(tree_posts[0]["tree"], blob_posts):
            blobs[entry["path"]] = body["content"]
        # The fake answers blobs in the order they were created, which is
        # sorted by path -- assert that rather than trusting it.
        self.assertEqual(
            [e["path"] for e in tree_posts[0]["tree"]], sorted(blobs), "blob order assumption"
        )
        agents = blobs["AGENTS.md"]
        self.assertIn("TODO: one paragraph", agents)
        self.assertIn("Fake shared conventions", agents)
        self.assertEqual(blobs["CLAUDE.md"], "@AGENTS.md\n")

    def test_an_unreadable_conventions_source_fails_the_scaffold(self):
        # Fail-closed like every other template source: a scaffold missing
        # its conventions is not a scaffold this tool wrote.
        fake = FakeGh()
        fake.conventions_fetch_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("mikelward/conf", err)
        self.assertIn("failed on: bootstrap", err)

    def test_bootstrap_applies_before_the_ruleset_step(self):
        # The one branch the scaffold still lands on directly -- one with
        # no commits, which has no base for a pull request to target -- is
        # written before this run's own ruleset takes effect, since a
        # ruleset requiring pull requests blocks that write for anyone not
        # a configured bypass actor, which the ruleset step never
        # configures the caller to be (Codex review, mikelward/repo#14).
        fake = FakeGh()
        fake.bootstrap_ref_missing = True
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)

        def first(predicate):
            return next(i for i, c in enumerate(fake.calls) if predicate(c))

        bootstrap_call = first(
            lambda c: len(c) > 3 and c[2] == "PUT" and c[3].startswith(f"repos/{REPO}/contents/")
        )
        ruleset_call = first(
            lambda c: len(c) > 3 and c[2] == "POST" and c[3] == f"repos/{REPO}/rulesets"
        )
        self.assertLess(bootstrap_call, ruleset_call)

    def test_a_scaffold_still_in_a_pull_request_holds_back_new_pr_protection(self):
        # The gap-fill's own ordering answer, now that it goes in as a
        # pull request: the files are NOT on the branch when the ruleset
        # step runs, so a ruleset that would first require pull requests
        # -- and with them `lanes`, `codex` and `zizmor` -- would block
        # the very pull request installing those checks. It waits for the
        # merge instead.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertIn("skipping the ruleset step", err)
        self.assertIn("still only in pull request #42", err)
        self.assertIn("Merge it, then rerun.", err)
        self.assertIn("failed on: ruleset", err)
        self.assertFalse(
            any(len(c) > 3 and c[2] == "POST" and c[3] == f"repos/{REPO}/rulesets" for c in fake.calls)
        )

    def test_a_scaffold_pull_request_does_not_hold_back_an_already_protecting_ruleset(self):
        # Only a ruleset that would introduce pull-request protection for
        # the FIRST time waits: where the branch already requires one,
        # this run changes nothing about the pull request's own odds, so
        # the ruleset step runs as it always would.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
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
                        # All three already required: the ruleset step adds
                        # no check here, so nothing about it needs the
                        # pending scaffold to have landed. (Only linear
                        # history and force-push blocking are missing, so
                        # there is still a write to make.)
                        "required_status_checks": [
                            {"context": "lanes"},
                            {"context": "codex"},
                            {"context": "zizmor"},
                        ],
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
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertNotIn("skipping the ruleset step", err)
        self.assertEqual(len(fake.puts), 1)  # the ruleset update itself

    def test_a_docs_only_gap_does_not_hold_back_the_ruleset(self):
        # Every workflow is already on the branch and only AGENTS.md is
        # missing, so every requested check can already report -- the
        # scaffold pull request is docs, and holding the ruleset back for
        # it defers a safe write over a gap that blocks nothing. This is
        # most of the fleet, since the conventions files joined the
        # scaffold long after the workflows did (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {"AGENTS.md", "CLAUDE.md"}
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertNotIn("skipping the ruleset step", err)
        ruleset_posts = [(a, b) for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(len(ruleset_posts), 1)

    def test_a_docs_only_gap_that_fails_to_open_still_lets_the_ruleset_through(self):
        # The bootstrap step failed, but what it failed to add was
        # AGENTS.md -- which publishes no check, so every requested check
        # is still reachable and the ruleset write has nothing to wait
        # for. Treating any bootstrap failure as "hold everything" skipped
        # an unrelated, safe write (Codex review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {"AGENTS.md", "CLAUDE.md"}
        fake.bootstrap_pull_create_fails = True
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)  # the bootstrap step did fail
        self.assertIn("failed on: bootstrap", err)
        self.assertNotIn("skipping the ruleset step", err)
        ruleset_posts = [(a, b) for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(len(ruleset_posts), 1)

    def test_a_failed_gap_that_would_leave_a_check_unpublished_still_holds_back(self):
        # The other side: no pull request was opened, so a HEAD-published
        # check is as unreachable as a base-published one -- `lanes` has
        # nowhere to run from if ci.yml never landed.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {".github/workflows/ci.yml"}
        fake.bootstrap_pull_create_fails = True
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("skipping the ruleset step", err)
        self.assertEqual(
            [(a, b) for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"], []
        )

    def test_a_push_after_a_nonblocking_scaffold_pull_request_blocks_the_ruleset(self):
        # A docs-only gap lets the ruleset through, which means the run
        # goes on to require checks on the strength of a gap assessment
        # made before the pull request was opened. A push landing after
        # that -- one dropping a publisher, say -- invalidates it, so the
        # tip is re-verified right before the write even though nothing
        # was written to the branch (Codex review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {"AGENTS.md", "CLAUDE.md"}
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        # Reads: plan_gaps, open_gap_pull_request's own recheck, then the
        # pre-write verification -- so the third read is the first to see
        # the push.
        fake.bootstrap_ref_sha_after_read = (2, "a-later-concurrent-push-sha")
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertIn("changed after the bootstrap step read it (a concurrent push)", err)
        self.assertIn("failed on: ruleset", err)
        self.assertEqual(
            [(a, b) for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"], []
        )

    def test_a_missing_codex_workflow_still_holds_back_the_ruleset(self):
        # The other side of the same cut: `codex`'s publisher is among
        # the missing files, and GitHub reads it from the base branch, so
        # no pull request -- this one included -- can make it report.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {
            ".github/workflows/codex-review-check.yml"
        }
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("skipping the ruleset step", err)
        self.assertEqual(
            [(a, b) for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"], []
        )

    def test_a_check_already_required_that_this_pull_request_cannot_report_is_flagged(self):
        # The repository is already wedged -- its ruleset requires `codex`
        # while the workflow that publishes `codex` is missing from the
        # branch -- so the pull request this opens cannot merge on its own
        # either. Reported rather than left to be discovered (Codex
        # review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {
            ".github/workflows/codex-review-check.yml"
        }
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
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
                        "required_status_checks": [
                            {"context": "lanes"},
                            {"context": "codex"},
                            {"context": "zizmor"},
                        ],
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
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "lanes"},
                        {"context": "codex"},
                        {"context": "zizmor"},
                    ]
                },
            }
        ]
        code, out, err = _run(fake, ["--force", REPO])
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertIn("this branch already requires 'codex'", err)
        self.assertIn("needs someone who can bypass the rule", err)

    def test_the_dry_run_says_the_branch_is_wedged_too(self):
        # --dry-run returns long before the Apply section, so the read has
        # to happen at plan time -- otherwise the preview tells someone to
        # open a scaffold pull request without mentioning that the
        # branch's own gate makes it unmergeable (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {
            ".github/workflows/codex-review-check.yml"
        }
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "codex"}]},
            }
        ]
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertIn("HEADS UP:", out)
        self.assertIn("this branch already requires 'codex'", out)
        self.assertIn("needs someone who can bypass the rule", out)

    def test_a_check_required_by_another_ruleset_is_flagged_too(self):
        # Rulesets aggregate, so a check some OTHER (or inherited) ruleset
        # requires blocks this pull request just as hard -- and reading
        # only the ruleset this tool manages missed exactly that case
        # (Codex review, mikelward/repo#42). Here the managed ruleset does
        # not exist at all, so nothing in `checks_added` would have
        # revealed it.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {
            ".github/workflows/codex-review-check.yml"
        }
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "codex"}]},
            }
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertIn("this branch already requires 'codex'", err)

    def test_a_required_check_outside_the_requested_rules_is_flagged_too(self):
        # `--rule lanes` says what this run would write; it says nothing
        # about what the branch already enforces. A `codex` requirement
        # from another ruleset blocks this pull request whether or not
        # this run mentions `codex` (Codex review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {
            ".github/workflows/codex-review-check.yml"
        }
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "codex"}]},
            }
        ]
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertIn("this branch already requires 'codex'", err)

    def test_the_wedge_advisory_claims_nothing_about_a_reused_pull_request(self):
        # `missing` proves the publisher is absent from the BRANCH and
        # nothing more. A pull request an earlier run left open may not
        # carry it -- GapPullRequest deliberately holds no evidence either
        # way -- so the advisory must not say it does, nor that merging it
        # lifts the wedge (Codex review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {
            ".github/workflows/codex-review-check.yml"
        }
        fake.bootstrap_open_pulls = [
            (
                11,
                "repo-setup/fleet-ci-scaffold-abc1234",
                True,
                "https://github.com/owner/repo/pull/11",
            )
        ]
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "codex"}]},
            }
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertIn("pull request #11 is adding the fleet CI scaffold", out)
        self.assertIn("this branch already requires 'codex'", err)
        # The wedge is real either way, and stated about the branch.
        self.assertIn("missing from 'main'", err)
        self.assertIn("needs someone who can bypass the rule", err)
        self.assertNotIn("this pull request adds", err)
        self.assertNotIn("merging it", err)

    def test_a_head_published_check_the_pull_request_carries_is_not_flagged(self):
        # `lanes` is published by ci.yml, which GitHub reads from the pull
        # request's OWN head -- so the pull request adding ci.yml runs it,
        # `lanes` reports, and the pull request merges on its own. Only
        # `codex`, read from the base branch, wedges the repository.
        # Flagging a head-published check sent someone looking for a bypass
        # actor they don't need (Codex review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {".github/workflows/ci.yml"}
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "lanes"}]},
            }
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertNotIn("already requires", err)

    def test_a_base_published_check_is_still_flagged_beside_a_head_published_one(self):
        # Both are required and both publishers are missing, so the
        # narrowing has to name `codex` and stay silent about `lanes`
        # rather than reporting all or nothing (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {
            ".github/workflows/ci.yml",
            ".github/workflows/codex-review-check.yml",
        }
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "lanes"}, {"context": "codex"}]
                },
            }
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertIn("this branch already requires 'codex'", err)
        self.assertNotIn("lanes", err)

    def test_a_check_bound_to_an_app_is_not_attributed_to_the_scaffold(self):
        # A requirement bound to a specific App names that App as the only
        # one allowed to report it, and nothing here knows which App the
        # fleet's own workflow publishes as -- so a same-named context
        # bound elsewhere says nothing about the scaffold's missing
        # workflow. Silence beats a wrong claim in an advisory (Codex
        # review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {
            ".github/workflows/codex-review-check.yml"
        }
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "codex", "integration_id": 99}]
                },
            }
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertNotIn("already requires", err)

    def test_an_empty_branch_previews_the_added_check_refusal_too(self):
        # --no-bootstrap on a branch with no commits, whose ruleset already
        # requires pull requests but not every check: the real run refuses
        # the check addition (an empty branch has never run a workflow, so
        # nothing can satisfy it), and the preview has to say the same
        # rather than reporting the update and exiting 0 (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_ref_missing = True
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
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
            ],
        }
        code, out, err = _run(fake, ["--dry-run", "--no-bootstrap", REPO])
        self.assertEqual(code, 1)
        self.assertIn("SKIPPED", out)
        self.assertIn("branch has no commits yet", out)

        code, out, err = _run(fake, ["--force", "--no-bootstrap", REPO])
        self.assertEqual(code, 1)
        self.assertIn("skipping the ruleset step", err)
        self.assertEqual(fake.puts, [])

    def test_an_unreadable_effective_rules_read_does_not_fail_the_step(self):
        # The warning is advisory: it says whether the pull request can
        # merge on its own, and a read that failed only means this run
        # cannot say.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.effective_rules_read_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertIn("could not check which checks", err)

    def test_no_run_reads_what_an_open_scaffold_pull_request_contains(self):
        # The point of the redesign, asserted rather than left implicit:
        # ten review findings came from deriving an answer out of a pull
        # request anyone can change at any moment, and the fix was to stop
        # deriving one (Codex review, mikelward/repo#42). The fixture has
        # no route for a pull request's file list, so a call would raise --
        # this checks the calls themselves too, so the guarantee survives
        # a fixture that grows one back.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = [
            (27, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/27")
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #27 is adding the fleet CI scaffold", out)
        self.assertFalse(
            [c for c in fake.calls if any("/pulls/" in str(a) for a in c)],
            "the step read an open pull request's contents",
        )

    def test_a_gap_containing_a_publisher_holds_back_even_with_a_pull_request_open(self):
        # The gap contains `ci.yml`, so the BRANCH has no way to report
        # `lanes` -- and whether the open pull request would supply it is
        # exactly the question this step stopped asking, because no answer
        # read out of a pull request stays true (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {".github/workflows/ci.yml"}
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        fake.bootstrap_open_pulls = [
            (20, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/20")
        ]
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("skipping the ruleset step", err)
        self.assertEqual(
            [(a, b) for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"], []
        )

    def test_a_ruleset_widened_onto_this_branch_is_held_back_too(self):
        # A ruleset can newly impose its rules on this branch without
        # changing a rule at all: it already carries pull_request and all
        # three checks but targets only refs/heads/release, and the write
        # widens its scope to cover the default branch. Both the
        # introduces-protection and the added-check predicates answer
        # False, so nothing held it back while `codex`'s publisher was
        # still inside the scaffold pull request (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "main",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/release"], "exclude": []}},
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [
                            {"context": "lanes"},
                            {"context": "codex"},
                            {"context": "zizmor"},
                        ],
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
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 1)
        self.assertIn("SKIPPED: the fleet CI scaffold will not be on the branch", out)

        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("newly target", err)
        self.assertIn("skipping the ruleset step", err)
        self.assertEqual(fake.puts, [])

    def test_a_scaffold_pull_request_holds_back_a_ruleset_that_adds_a_check(self):
        # The other half of the same hazard, and the one a first-time-
        # pull-request-protection test cannot reach: the branch ALREADY
        # requires pull requests, so introduces_pr_protection is False,
        # but the ruleset requires only `lanes` and this write would add
        # `codex` and `zizmor`. `codex` publishes under
        # `pull_request_target` from the BASE branch's copy, which is
        # still inside the scaffold pull request -- so nothing could
        # report it, nothing could merge, the scaffold's own pull request
        # included, and only an administrator could undo it (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
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
            ],
        }
        # The dry run previews the same skip and the same exit status.
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 1)
        self.assertIn("SKIPPED: the fleet CI scaffold will not be on the branch", out)
        self.assertIn("would newly require 'codex', 'zizmor'", out)

        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertIn("skipping the ruleset step", err)
        self.assertIn("newly require 'codex', 'zizmor'", err)
        self.assertIn("failed on: ruleset", err)
        self.assertEqual(fake.puts, [])  # the ruleset was never written

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
        self.assertIn(f"{REPO}: opened pull request #42", out)
        blob_posts = [body for _args, body in fake.posts if "encoding" in body]
        self.assertTrue(blob_posts, "no scaffold blobs were written")
        self.assertEqual(len(fake.created_pulls), 1)
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

    def test_a_scope_blocked_scaffold_previews_the_ruleset_skip_too(self):
        # A missing `workflow` scope leaves the scaffold off the branch
        # just as surely as a pending pull request does, and the Apply
        # section skips the ruleset for it -- so the preview has to say so
        # too, rather than promising a ruleset the real run holds back and
        # asking to confirm a write that will not happen (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.token_scopes = ("gist", "read:org", "repo")
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 1)
        self.assertIn("SKIPPED: the fleet CI scaffold will not be on the branch", out)

        # And the real run agrees, without a confirmation: the only
        # mutation left is one it is about to skip.
        code, out, err = _run(fake, [REPO])  # no --force, stdin is not a terminal
        self.assertEqual(code, 1)
        self.assertNotIn("stdin is not a terminal", err)
        self.assertIn("skipping the bootstrap step", err)
        self.assertIn("skipping the ruleset step", err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.puts, [])

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
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertEqual(len(fake.created_pulls), 1)

    def test_a_concurrent_push_after_bootstrap_blocks_activating_the_ruleset(self):
        # A concurrent push landing between the bootstrap step finishing
        # and the ruleset step activating protection doesn't touch the
        # ruleset's own fingerprint, so nothing else here would notice
        # before locking in protection over a scaffold that may no longer
        # be complete (Codex review, mikelward/repo#14). The scaffold is
        # already complete here (the default fixture), which is what
        # leaves the ruleset step free to run at all: the push lands after
        # the bootstrap step's own recheck confirmed it.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        fake.bootstrap_ref_sha_after_read = (2, "a-later-concurrent-push-sha")
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "'main' changed after the bootstrap step read it (a concurrent push)",
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
        fake.bootstrap_ref_sha_after_read = (2, "a-later-concurrent-push-sha")

        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertNotIn("skipping the ruleset step", err)  # no precomputed gate caught this
        self.assertIn(
            "'main' changed after the bootstrap step read it (a concurrent push)",
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
            # Hardened already, so this write widens no scope: the
            # early gate must have nothing of its own to fire on,
            # or it would catch this before the fresh recompute
            # the test is about.
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
            # Hardened already, so this write widens no scope: the
            # early gate must have nothing of its own to fire on,
            # or it would catch this before the fresh recompute
            # the test is about.
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

    def test_a_check_removed_during_the_wait_is_not_silently_re_added(self):
        # The other half of the window the introduces_pr_protection
        # refusal already closes. The preview sees all three checks
        # already required (so nothing is held back) but a write still
        # needed for the linear-history rule; an administrator then
        # removes `codex` during the confirmation wait. The fresh
        # recompute rebuilds the SAME target body -- an entry with no
        # integration_id reconstructs byte for byte -- so the fingerprint
        # passes, and without this refusal the write would silently
        # re-add a check whose workflow is still inside the pending
        # scaffold pull request (Codex review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()  # scaffold pending, so the caller refuses
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        base_object = {
            "id": 7,
            "name": "main",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": list(_HARDENED_SCOPE), "exclude": []}},
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [
                            {"context": "lanes"},
                            {"context": "codex"},
                            {"context": "zizmor"},
                        ],
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
        changed = json.loads(json.dumps(base_object))
        changed["rules"][0]["parameters"]["required_status_checks"] = [
            {"context": "lanes"},
            {"context": "zizmor"},
        ]
        fake.ruleset_objects_after_change["7"] = changed
        fake.ruleset_content_change_threshold = 3

        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("would now newly require 'codex'", err)
        self.assertIn("Not writing it", err)
        self.assertEqual(fake.puts, [])

    def test_a_held_back_ruleset_write_is_not_something_to_confirm(self):
        # With a scaffold pull request already open there is nothing for
        # the bootstrap step to write, and the ruleset write is one the
        # Apply section is already going to skip -- so there is no
        # question to ask. Before this, a non-interactive run without
        # --force refused at the stdin check and never reached the step
        # that reports the open pull request (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        fake.bootstrap_open_pulls = [
            (18, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/18")
        ]
        code, out, err = _run(fake, [REPO])  # no --force, stdin is not a terminal
        self.assertEqual(code, 1)
        self.assertNotIn("stdin is not a terminal", err)
        self.assertIn("pull request #18 is adding the fleet CI scaffold", out)
        self.assertIn("skipping the ruleset step", err)
        self.assertEqual(fake.puts, [])
        self.assertEqual(fake.created_pulls, [])

    def test_dry_run_previews_the_pending_scaffold_ruleset_skip_accurately(self):
        # Same accuracy requirement again, for the skip the pull-request
        # write path introduces: with files missing and the checks already
        # reporting, --dry-run reached neither the Apply section's gate nor
        # the never-reported one, so it printed the ruleset as creatable
        # and exited 0 while the equivalent real run
        # (test_a_scaffold_still_in_a_pull_request_holds_back_new_pr_
        # protection) skips it and exits 1 (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 1)
        self.assertIn("SKIPPED: the fleet CI scaffold will not be on the branch", out)
        self.assertIn("open a pull request adding 9 file(s):", out)  # bootstrap's own plan still shown
        self.assertEqual(fake.posts, [])  # dry run: no writes at all regardless

    def test_dry_run_does_not_claim_the_skip_where_protection_already_exists(self):
        # Only a ruleset that would introduce pull-request protection for
        # the first time waits on the scaffold, so the preview must not
        # announce a skip the real run wouldn't make either.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
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
                        # All three already required -- this write adds no
                        # check, so it needs nothing from the pending
                        # scaffold (the test below is the other half).
                        "required_status_checks": [
                            {"context": "lanes"},
                            {"context": "codex"},
                            {"context": "zizmor"},
                        ],
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
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("SKIPPED: the fleet CI scaffold will not be on the branch", out)

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
        self.assertIn(f"{REPO}: added 9 fleet CI scaffold file(s)", out)
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
        self.assertIn(f"{REPO}: added 9 fleet CI scaffold file(s)", out)
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

    def test_a_pull_request_that_cannot_be_opened_says_where_the_commit_went(self):
        # The commit and its branch are already pushed by the time the
        # pull request itself is attempted, so a failure here leaves
        # something behind -- said plainly, rather than reported as if
        # nothing had happened. It also names the flag that gets the rest
        # of `repo setup` through a repository this step cannot finish on.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_pull_create_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not open a pull request adding the scaffold", err)
        self.assertIn("repo-setup/fleet-ci-scaffold-newscaf", err)
        self.assertIn("`--no-bootstrap`", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(len(fake.created_refs), 1)
        self.assertEqual(fake.created_pulls, [])

    def test_a_scaffold_pull_request_already_open_is_reported_not_reopened(self):
        # `repo setup` runs unattended across a fleet, so a step that
        # cannot see its own earlier pull request opens a second one on
        # every run. It is reported instead -- the one thing standing
        # between this repository and a scaffold -- and nothing is written.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = [
            (11, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/11")
        ]
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #11 is adding the scaffold", out)
        self.assertIn("still absent from the default branch: 9 file(s)", out)
        self.assertNotIn("open a pull request adding", out)

        code, out, err = _run(fake, ["--no-rules", REPO])  # no --force, no terminal
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #11 is adding the fleet CI scaffold", out)
        self.assertIn("merge it, then rerun", out)
        # Nothing to confirm and nothing to write: not a blob, not a
        # branch, not a second pull request.
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.created_refs, [])
        self.assertEqual(fake.created_pulls, [])

    def test_a_scaffold_pull_request_that_closed_during_the_wait_is_not_replaced(self):
        # Finding one open is what takes the bootstrap write out of the
        # confirmation, so nobody agreed to opening one. If it closes
        # before the recheck, opening another would be a write the preview
        # said would not happen -- on a non-interactive run that refuses
        # unconfirmed changes for exactly this reason (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = [
            (
                11,
                "repo-setup/fleet-ci-scaffold-abc1234",
                True,
                "https://github.com/owner/repo/pull/11",
            )
        ]
        fake.bootstrap_open_pulls_later = []
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("pull request #11", err)
        self.assertIn("no longer open", err)
        self.assertIn("Rerun to plan", err)
        self.assertIn("failed on: bootstrap", err)
        # Nothing was written in its place.
        self.assertEqual(fake.created_refs, [])
        self.assertEqual(fake.created_pulls, [])

    def test_a_moved_base_is_refused_even_when_a_pull_request_is_being_reused(self):
        # Reusing an open pull request writes nothing, but it still hands
        # the caller `plan.missing` -- and every answer built on that (what
        # is still absent, and so which checks the gap keeps from
        # reporting) is wrong if the branch has moved since. A branch that
        # lost a base-published workflow during the wait would otherwise
        # leave the run concluding the gap blocks nothing (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = [
            (19, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/19")
        ]
        fake.bootstrap_ref_sha_after_first_read = "a-branch-that-moved"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("no longer points at the commit this plan was built from", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.created_pulls, [])
        self.assertNotIn("already adds the fleet CI scaffold", out)

    def test_one_opened_during_the_confirmation_wait_is_found_before_writing(self):
        # The plan's answer is a snapshot, and the combined plan waits on
        # a confirmation for as long as the person takes. The listing is
        # read again immediately before the write for exactly that reason.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls_later = [
            (12, "repo-setup/fleet-ci-scaffold-def5678", True, "https://github.com/owner/repo/pull/12")
        ]
        with patch("builtins.input", return_value="y"):
            code, out, err = _run(fake, ["--no-rules", REPO], isatty=True)
        self.assertEqual(code, 0, err)
        self.assertIn("open a pull request adding 9 file(s):", err)  # what the plan showed
        self.assertIn("pull request #12 is adding the fleet CI scaffold", out)
        self.assertEqual(fake.created_refs, [])
        self.assertEqual(fake.created_pulls, [])

    def test_a_pull_request_from_a_fork_branch_of_the_same_name_does_not_count(self):
        # A fork's branch can be named anything at all; one that happens
        # to carry this prefix is not a pull request this tool opened, and
        # treating it as one would leave the repository unscaffolded
        # forever on somebody else's say-so. Asked of GitHub as head and
        # base sharing a repository id, so a caller that typed the
        # repository's name in another casing (or a name it has since been
        # renamed away from) doesn't read every one of its own pull
        # requests as a fork's.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = [
            (13, "repo-setup/fleet-ci-scaffold-abc1234", False, "https://example.invalid/13"),
            (14, "some-unrelated-branch", True, "https://example.invalid/14"),
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertEqual(len(fake.created_pulls), 1)
        # Pinned, because the alternative -- comparing the head's
        # canonical full_name against the name the caller typed -- is
        # wrong in a way nothing else here would catch: it reads every one
        # of this tool's own pull requests as a fork's the moment a caller
        # types the repository in another casing, or under a name GitHub
        # has since renamed away from.
        listings = [c for c in fake.calls if any("&base=" in a for a in c)]
        self.assertTrue(listings, "the open-pull-request listing was never made")
        self.assertIn(".head.repo.id == .base.repo.id", listings[0][listings[0].index("--jq") + 1])

    def test_an_open_pull_request_is_reported_even_without_the_workflow_scope(self):
        # The scope only matters for a write. With a scaffold pull request
        # already open there is no write to make, so failing the step over
        # a token that could not have made one reports a problem this
        # repository does not have -- and buries the pull request that is
        # the actual next step (Codex review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.token_scopes = ("gist", "read:org", "repo")
        fake.bootstrap_open_pulls = [
            (15, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/15")
        ]
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #15 is adding the scaffold", out)
        self.assertNotIn("workflow", err)

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #15 is adding the fleet CI scaffold", out)
        self.assertEqual(fake.posts, [])

    def test_an_unreadable_pull_request_listing_fails_the_step_closed(self):
        # "Could not tell" must not read as "there is none": that opens a
        # second pull request beside one already there.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_pulls_list_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not list", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.posts, [])

    def test_a_complete_repository_never_lists_its_pull_requests(self):
        # The listing only ever answers "what would this run open", so a
        # repository with nothing missing -- every one of them, once a
        # fleet has converged -- must not pay for it.
        fake = FakeGh()
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertFalse(
            any("&base=" in c[1] for c in fake.calls if c[0] == "api" and len(c) > 1),
            [c for c in fake.calls if c[0] == "api"],
        )

    def test_the_docs_lane_reading_matches_the_lanes_conf_the_scaffold_writes(self):
        # The predicate and the config are two statements of one rule, and
        # a commit prefix is wrong the moment they disagree. `*.md` and
        # `**/docs/*.md` (maintainer, 2026-09-06) -- so root markdown, and
        # markdown directly inside a docs/ directory at any depth.
        conf = scaffold._LANES_CONF
        self.assertIn("\ndocs *.md\n", conf)
        self.assertIn("\ndocs **/docs/*.md\n", conf)

        for path in ("README.md", "AGENTS.md", "docs/guide.md", "packages/ui/docs/api.md"):
            self.assertTrue(scaffold._docs_lane_only({path}), path)
        for path in (
            "docs/guide/setup.md",  # **/docs/*.md is one level, not a tree
            "src/main.py",
            ".github/workflows/ci.yml",
            "notes.txt",
            "docsy/thing.md",  # a directory merely starting with "docs"
        ):
            self.assertFalse(scaffold._docs_lane_only({path}), path)

        # Every path has to ride it, and an empty gap rides nothing.
        self.assertFalse(scaffold._docs_lane_only({"AGENTS.md", "src/main.py"}))
        self.assertFalse(scaffold._docs_lane_only(set()))

    def test_a_docs_only_gap_carries_the_docs_prefix_its_own_lanes_conf_requires(self):
        # The lanes gate fails a docs-only diff whose commit subject
        # carries no docs prefix -- and the scaffold's own lanes.conf is
        # what puts root markdown on that lane. A pull request adding only
        # AGENTS.md and CLAUDE.md would fail the very check it installs.
        fake = FakeGh()
        fake.bootstrap_existing_paths = {
            ".github/workflows/codex-review.yml",
            ".github/workflows/codex-review-check.yml",
            ".github/workflows/codex-review-listener.yml",
            ".github/workflows/zizmor.yml",
            ".github/workflows/ci.yml",
            ".github/zizmor.yml",
            ".github/lanes.conf",
        }
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        commit_posts = [body for _args, body in fake.posts if "parents" in body]
        self.assertEqual(len(commit_posts), 1)
        self.assertTrue(
            commit_posts[0]["message"].startswith("docs: Add missing fleet CI scaffold files"),
            commit_posts[0]["message"],
        )

    def test_a_mixed_gap_carries_no_prefix(self):
        # Anything outside the docs lane makes it a code-lane diff, which
        # the gate does not ask a prefix of.
        fake = FakeGh()
        fake.bootstrap_existing_paths = {".github/zizmor.yml"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        commit_posts = [body for _args, body in fake.posts if "parents" in body]
        self.assertTrue(
            commit_posts[0]["message"].startswith("Add missing fleet CI scaffold files"),
            commit_posts[0]["message"],
        )

    def test_a_gap_branch_that_already_holds_this_exact_commit_is_reused(self):
        # The branch name carries the commit's own sha, so a ref already
        # there under that name and pointing at that commit is a rerun
        # that rebuilt an identical commit -- not somebody else's branch.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_ref_create_fails = True
        fake.bootstrap_gap_ref_sha = "newscaffoldcommitsha"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertEqual(len(fake.created_pulls), 1)

    def test_a_gap_branch_holding_something_else_is_refused_not_moved(self):
        # The other side of the same check: this module never overwrites
        # what is already there, a ref included.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_ref_create_fails = True
        fake.bootstrap_gap_ref_sha = "somebody-elses-commit"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not create the branch", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.created_pulls, [])

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
