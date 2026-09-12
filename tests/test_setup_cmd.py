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

from repo_lib import apps, gh, rules, scaffold, setup_cmd
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
_CHECK_RUNS_RE = re.compile(r"^repos/([^/]+/[^/]+)/commits/([^/]+)/check-runs(?:\?filter=all)?$")
_STATUS_RE = re.compile(r"^repos/([^/]+/[^/]+)/commits/([^/]+)/status$")
_STATUSES_LIST_RE = re.compile(r"^repos/([^/]+/[^/]+)/commits/([^/]+)/statuses$")
_WORKFLOW_RUNS_RE = re.compile(r"^repos/([^/]+/[^/]+)/actions/runs\?head_sha=([^&]+)&per_page=100$")
_PULLS_RE = re.compile(r"^repos/([^/]+/[^/]+)/pulls\?state=(open|closed)&.*$")
_RULESETS_LOOKUP_RE = re.compile(r"^repos/([^/]+/[^/]+)/rulesets\?includes_parents=false$")
_RULESETS_ALL_RE = re.compile(r"^repos/([^/]+/[^/]+)/rulesets\?includes_parents=true$")
_RULESET_ONE_RE = re.compile(r"^repos/([^/]+/[^/]+)/rulesets/([^/]+)$")
_SIBLING_REF_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/ref/heads/(main|master)$")
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
    r"^repos/([^/]+/[^/]+)/pulls\?state=open&per_page=100$"
)
_SCAFFOLD_REF_CREATE_RE = re.compile(r"^repos/([^/]+/[^/]+)/git/refs$")
# GitHub's effective-rules endpoint: what a branch actually enforces
# across every ruleset covering it, which is what the scaffold step reads
# to say whether its pull request can merge on its own.
_EFFECTIVE_RULES_RE = re.compile(r"^repos/([^/]+/[^/]+)/rules/branches/(.+)$")
_SCAFFOLD_PULL_CREATE_RE = re.compile(r"^repos/([^/]+/[^/]+)/pulls$")
# An earlier run's scaffold pull request: what apply_gaps reads to decide
# whether to merge it, and the two writes it can make to it.
_SCAFFOLD_PULL_ONE_RE = re.compile(r"^repos/([^/]+/[^/]+)/pulls/(\d+)$")
_SCAFFOLD_PULL_MERGE_RE = re.compile(r"^repos/([^/]+/[^/]+)/pulls/(\d+)/merge$")
_PULL_HEAD_RE = re.compile(r"^prhead(\d+)")
_ACTIONS_PERMISSIONS_RE = re.compile(r"^repos/([^/]+/[^/]+)/actions/permissions$")
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


def _check_run(entry):
    """A FakeGh.check_runs entry as (name, app id, conclusion). A bare name
    is an unbound check that passed -- the shape every older fixture uses
    -- a pair binds it to an App, and a triple says how it concluded
    (None: still running)."""
    if isinstance(entry, tuple):
        if len(entry) == 3:
            return entry
        name, app_id = entry
        return name, app_id, "success"
    return entry, None, "success"


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
        # sha -> [(context, creator login[, state]), ...] for the plural
        # /statuses endpoint, every status ever posted, NEWEST FIRST as
        # GitHub lists them; a pair is a success.
        self.status_creators = {}
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
        # Truthier than a read count for the window that matters: the gap
        # between the plan's read of a ruleset and the delete spans the
        # survivor's own PUT, so keying the swap on that PUT says "inside
        # the window" without depending on how many reads either side of
        # it happens to make.
        self.change_rulesets_after_put = False
        self.ruleset_read_fails_after_put = set()
        self._ruleset_put_done = False
        # rid -> read count after which reads of that id fail, for the
        # "could not tell" half of a recheck (as distinct from "changed").
        self.ruleset_read_fails_after = {}
        # A real branch named main or master beside the default branch
        # (read as git/ref/heads/<name>: the branches endpoint follows a
        # rename's 301 and is not what the tool asks).
        self.sibling_exists = False
        self.sibling_error = None
        # After this many sibling reads, the branch exists: a branch
        # created while the operator was confirming.
        self.sibling_created_after_reads = None
        self._sibling_reads = 0
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
        # App id -> (slug, repository_selection) for the id-keyed lookups the
        # binding coverage precondition (app_covers_repo) and status-creator
        # resolution (app_slug_for_id) use. Absent id => not installed on the
        # owner (coverage False / slug None). A "selected" selection reads its
        # repo list from install_members[str(app_id)].
        self.app_coverage = {}
        self.app_coverage_fails = None  # gh stderr for the id-keyed installs read, or None
        # The credential preflight's two reads: gh stderr to fail them
        # with, or None for "this token works".
        self.auth_fails = None
        self.installations_read_fails = None

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
        self.bootstrap_contents_put_fails = False  # the empty-branch bootstrap write
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
        # number -> overrides for what GET pulls/{n} answers: "state",
        # "draft", "head_sha", "base_ref", "mergeable", "mergeable_state". The default
        # (see _pull) is an open, non-draft, mergeable ("clean") pull
        # request whose head is "prhead<number>" -- with no check runs on
        # that head unless a test adds them, so the step WAITS by default
        # rather than merging.
        self.pulls = {}
        # number -> overrides applied from the SECOND read of that pull
        # request on: what changed while the plan waited on confirmation.
        self.pulls_later = {}
        self._pull_reads = {}
        self.pull_read_fails = set()
        self.pull_merge_fails = set()  # numbers whose merge PUT is refused
        self.merged_pulls = []  # (number, body) of every merge PUT
        self.closed_pulls = []  # numbers PATCHed closed
        self.merged_sha = "mergedsha123"
        # The login this token acts as, which is what a scaffold pull
        # request's author has to be for the step to treat it as its own.
        self.login = "owner"
        # sha -> the Actions workflow runs from it, as (path, status,
        # conclusion) triples. A sha with no entry derives one successful
        # run per scaffold check that passed in check_runs, from its
        # publisher's path -- the shape every older merge fixture assumes.
        self.workflow_runs = {}
        self.workflow_runs_read_fails = False
        # Whether GitHub Actions is enabled on the repository -- read only
        # when a scaffold pull request has no check on it at all.
        self.actions_enabled = True
        self.actions_permissions_fails = False
        # What the review-state GraphQL read answers for a pull request:
        # GitHub's reviewDecision (None where no rule requires a review),
        # how many review threads are unresolved, and whether it fails.
        self.review_decision = None
        self.unresolved_threads = 0
        # Or the threads themselves, as isResolved flags, paged 100 at a
        # time the way GitHub pages them; None derives them from
        # unresolved_threads.
        self.review_threads = None
        self.review_state_fails = False
        self.review_state_reads = 0
        # What compare/{merged}...{default} answers after a merge: where
        # the merged commit landed relative to the default branch.
        self.merge_landed = "identical"
        # number -> paths the pull request's head tree carries BEYOND the
        # generated commit (a push onto the branch), or a full override of
        # its non-directory entries as {path: (sha, mode)}. By default a
        # head's tree is exactly the base plus the generated changes.
        self.pull_head_extra_paths = {}
        self.pull_head_trees = {}
        # number -> the head commit's parent shas; the default is the one
        # generated commit on top of the branch's tip.
        self.pull_head_parents = {}
        # number -> the head commit's message; the default is the one the
        # generator would write for the plan.
        self.pull_head_messages = {}
        # Paths in UPDATED_PATHS whose blob sha the tree reports as NOT the
        # template's -- an outdated pinned copy. Every other present path
        # reports the template's own sha, so "present" means "current".
        self.bootstrap_outdated_paths = set()
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
        # branch name -> sha: gap branches that already exist holding that
        # commit (a ref POST for one 422s, and its read answers the sha),
        # for the "occupied by another commit, take the next name" case.
        self.bootstrap_occupied_gap_refs = {}
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
        if args[1] == "graphql":
            assert any(a.startswith("query=") and "reviewDecision" in a for a in args), args
            if self.review_state_fails:
                raise gh.GhError("gh: HTTP 502: Bad Gateway\n")
            self.review_state_reads += 1
            flags = self.review_threads
            if flags is None:
                flags = [False] * self.unresolved_threads + [True]
            after = [a for a in args if a.startswith("after=")]
            start = int(after[0][len("after="):]) if after else 0
            page = flags[start : start + 100]
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewDecision": self.review_decision,
                                "reviewThreads": {
                                    "pageInfo": {
                                        "hasNextPage": start + 100 < len(flags),
                                        "endCursor": str(start + 100),
                                    },
                                    "nodes": [{"isResolved": f} for f in page],
                                },
                            }
                        }
                    }
                }
            )
        endpoint, method, jq = _parse_api_args(args[1:])
        if "/compare/" in endpoint and jq == ".status":
            return self.merge_landed + "\n"

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

        m = _WORKFLOW_RUNS_RE.match(endpoint)
        if m:
            if self.workflow_runs_read_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            sha = m.group(2)
            if sha in self.workflow_runs:
                runs = self.workflow_runs[sha]
            else:
                runs = [
                    (scaffold.CHECK_PUBLISHERS[name][0], "completed", "success")
                    for name, _app_id, conclusion in map(_check_run, self.check_runs.get(sha, []))
                    if name in scaffold.CHECK_PUBLISHERS and conclusion in scaffold._PASSING_CONCLUSIONS
                ]
            return "".join(json.dumps(list(run)) + "\n" for run in runs)

        m = _CHECK_RUNS_RE.match(endpoint)
        if m:
            if ".status" in (jq or ""):
                # The scaffold step's own read of a pull request head:
                # --jq '[.name, .status, .conclusion]'. A conclusion of None
                # models a run still in progress.
                return "".join(
                    json.dumps([name, "completed" if conclusion else "in_progress", conclusion])
                    + "\n"
                    for name, _app_id, conclusion in map(_check_run, self.check_runs.get(m.group(2), []))
                )
            # Models --jq '[.name, .app.id, .conclusion]': an entry may be
            # a bare name (no App binding, passed), a (name, app id) pair,
            # or a (name, app id, conclusion) triple -- see _check_run.
            return "".join(
                json.dumps(list(_check_run(n))) + "\n" for n in self.check_runs.get(m.group(2), [])
            )

        m = _STATUS_RE.match(endpoint)
        if m:
            # Models --jq '[.context, .state]': a bare context is a passed
            # status; a (context, state) pair says otherwise.
            return "".join(
                json.dumps(list(c) if isinstance(c, tuple) else [c, "success"]) + "\n"
                for c in self.statuses.get(m.group(2), [])
            )

        m = _STATUSES_LIST_RE.match(endpoint)
        if m:
            # Models --jq '.[] | [.context, (.creator.login // ""), .state]'.
            return "".join(
                json.dumps([entry[0], entry[1], entry[2] if len(entry) > 2 else "success"]) + "\n"
                for entry in self.status_creators.get(m.group(2), [])
            )

        m = _SCAFFOLD_PULL_ONE_RE.match(endpoint)
        if m and method is None:
            number = int(m.group(2))
            if number in self.pull_read_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            pull = self._pull(number, read=True)
            return json.dumps(
                [
                    pull["state"],
                    pull["draft"],
                    pull["head_sha"],
                    pull["base_ref"],
                    pull["mergeable"],
                    pull["mergeable_state"],
                ]
            )

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
            # A four-tuple is a pull request this token's user opened; a
            # fifth element names another author.
            return "".join(
                f"{p[0]} {p[1]} {'true' if p[2] else 'false'} "
                f"{p[4] if len(p) > 4 else self.login} {p[3]}\n"
                for p in pulls
            )

        if endpoint == "user" and jq == ".login":
            if self.auth_fails is not None:
                raise gh.GhError(self.auth_fails)
            return self.login + "\n"

        if _ACTIONS_PERMISSIONS_RE.match(endpoint) and jq == ".enabled":
            if self.actions_permissions_fails:
                raise gh.GhError("gh: HTTP 403: Resource not accessible by integration\n")
            return ("true" if self.actions_enabled else "false") + "\n"

        m = _PULLS_RE.match(endpoint)
        if m:
            shas = self.open_prs if m.group(2) == "open" else self.closed_prs
            # Paged the way GitHub pages: 100 per page, `page=` from 1.
            page_m = re.search(r"[&?]page=(\d+)", endpoint)
            page = int(page_m.group(1)) if page_m else 1
            return "".join(sha + "\n" for sha in shas[(page - 1) * 100 : page * 100])

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
            if self._ruleset_put_done and rid in self.ruleset_read_fails_after_put:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            if (
                (
                    self._ruleset_object_reads[rid] > self.ruleset_content_change_threshold
                    or (self.change_rulesets_after_put and self._ruleset_put_done)
                )
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
            if jq == ".total_count":
                # apps.installations_readable's probe: whether this token
                # may list installations at all, not which ones.
                if self.installations_read_fails is not None:
                    raise gh.GhError(self.installations_read_fails)
                return f"{len(self.installations)}\n"
            aid_m = re.search(r"app_id == (\d+)", jq or "")
            if aid_m:
                # Id-keyed lookups: app_covers_repo (projects
                # [.id, .repository_selection]) and app_slug_for_id (.app_slug).
                if self.app_coverage_fails is not None:
                    raise gh.GhError(self.app_coverage_fails)
                cover = self.app_coverage.get(int(aid_m.group(1)))
                if cover is None:
                    return ""  # not installed on the owner
                cslug, cselection = cover[0], cover[1]
                # Optional third element marks the installation suspended.
                csuspended = bool(cover[2]) if len(cover) > 2 else False
                if "repository_selection" in (jq or ""):
                    return (
                        f"{aid_m.group(1)}\t{cselection}\t"
                        f"{'true' if csuspended else 'false'}\n"
                    )
                return f"{cslug}\n"
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
            if m.group(2) in self.bootstrap_occupied_gap_refs:
                return json.dumps({"object": {"sha": self.bootstrap_occupied_gap_refs[m.group(2)]}})
            if self.bootstrap_gap_ref_sha is None:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            return json.dumps({"object": {"sha": self.bootstrap_gap_ref_sha}})

        m = _SIBLING_REF_RE.match(endpoint)
        if (
            m
            and method is None
            and jq is None
            and m.group(2) not in (self.default_branch, self.default_branch_after_bootstrap_plan)
        ):
            if self.sibling_error:
                raise gh.GhError(self.sibling_error)
            self._sibling_reads += 1
            exists = self.sibling_exists
            if (
                self.sibling_created_after_reads is not None
                and self._sibling_reads > self.sibling_created_after_reads
            ):
                exists = True
            if exists:
                return json.dumps({"object": {"sha": "5ib1150000000000000000000000000000000000"}})
            raise gh.GhError("gh: HTTP 404: Not Found\n")

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
            head = _PULL_HEAD_RE.match(m.group(2))
            if head:
                number = int(head.group(1))
                parents = self.pull_head_parents.get(number, [self.bootstrap_commit_sha])
                message = self.pull_head_messages.get(number)
                if message is None:
                    message = self._generated_message()
                return json.dumps(
                    {
                        "tree": {"sha": f"prtree{number}"},
                        "parents": [{"sha": p} for p in parents],
                        "message": message,
                    }
                )
            return json.dumps({"tree": {"sha": self.bootstrap_tree_sha}})

        m = _SCAFFOLD_TREE_RE.match(endpoint)
        if m and m.group(2).startswith("prtree"):
            number = int(m.group(2)[len("prtree"):])
            if number in self.pull_head_trees:
                entries = self.pull_head_trees[number]
            else:
                # The generated commit: the base's entries with the
                # scaffold's own content written over the missing and
                # outdated paths -- built by the real generator against
                # this fake, so the shas are the ones the step expects.
                base = self._all_scaffold_paths() if self.bootstrap_existing_paths is None else self.bootstrap_existing_paths
                entries = {p: (self._tree_sha(p), "100644") for p in base}
                for p, content in scaffold.build_scaffold_files(self.default_branch).items():
                    if p not in entries or (
                        p in scaffold.UPDATED_PATHS and p in self.bootstrap_outdated_paths
                    ):
                        entries[p] = (scaffold._blob_sha(content), "100644")
                for p in self.pull_head_extra_paths.get(number, ()):
                    entries[p] = (hashlib.sha1(f"pushed {p}".encode()).hexdigest(), "100644")
            return json.dumps(
                {
                    "tree": [
                        {"path": p, "type": "blob", "mode": mode, "sha": sha}
                        for p, (sha, mode) in entries.items()
                    ],
                    "truncated": False,
                }
            )

        m = _SCAFFOLD_TREE_RE.match(endpoint)
        if m:
            if self.bootstrap_tree_read_fails:
                raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
            paths = self._all_scaffold_paths() if self.bootstrap_existing_paths is None else self.bootstrap_existing_paths
            # mode "100644" (a real, non-executable file) -- real GitHub
            # tree entries always carry one, and plan_gaps now checks it
            # (Codex review, mikelward/repo#14), so an entry missing it
            # would silently stop matching what the fake models as present.
            entries = [{"path": p, "type": "blob", "mode": "100644", "sha": self._tree_sha(p)} for p in paths]
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

    def _pull(self, number, read=False):
        pull = {
            "state": "open",
            "draft": False,
            "head_sha": f"prhead{number}",
            "base_ref": "main",
            "mergeable": True,
            "mergeable_state": "clean",
        }
        pull.update(self.pulls.get(number, {}))
        if read:
            self._pull_reads[number] = self._pull_reads.get(number, 0) + 1
        if self._pull_reads.get(number, 0) >= 2:
            pull.update(self.pulls_later.get(number, {}))
        return pull

    def _generated_message(self):
        """The commit message the generator writes for this fake's gap:
        what a scaffold pull request's head has to carry to be merged."""
        present = self._all_scaffold_paths() if self.bootstrap_existing_paths is None else self.bootstrap_existing_paths
        files = scaffold.build_scaffold_files(self.default_branch)
        missing = {p: c for p, c in files.items() if p not in present}
        outdated = {
            p: c
            for p, c in files.items()
            if p in present and p in scaffold.UPDATED_PATHS and p in self.bootstrap_outdated_paths
        }
        return scaffold._gap_commit_message(missing, outdated)

    def _tree_sha(self, path):
        """What the tree listing reports as a present scaffold path's blob
        sha: the template's own for a current pinned copy, something else
        for one bootstrap_outdated_paths names, and a placeholder for
        every path the step never compares by content."""
        name = path.rsplit("/", 1)[-1]
        if path in scaffold.UPDATED_PATHS and path not in self.bootstrap_outdated_paths:
            return scaffold._blob_sha(self.template_contents.get(name, ""))
        return hashlib.sha1(f"present {path}".encode()).hexdigest()

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
            m = _SCAFFOLD_PULL_MERGE_RE.match(endpoint)
            if m:
                number = int(m.group(2))
                if number in self.pull_merge_fails:
                    raise gh.GhError("gh: HTTP 405: Pull Request is not mergeable\n")
                if body.get("sha") != self._pull(number)["head_sha"]:
                    raise gh.GhError("gh: HTTP 409: Head branch was modified. Review and try the merge again.\n")
                self.merged_pulls.append((number, body))
                self.pulls.setdefault(number, {})["state"] = "closed"
                # The default branch's tip is the merge now.
                self._scaffold_ref_current_sha = self.merged_sha
                return json.dumps({"merged": True, "sha": self.merged_sha, "message": "Pull Request successfully merged"}).encode()
            self.puts.append((args, body))
            if _RULESET_ONE_RE.match(endpoint):
                self._ruleset_put_done = True
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
                if self.bootstrap_contents_put_fails:
                    raise gh.GhError("gh: HTTP 500: Internal Server Error\n")
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
                if (
                    self.bootstrap_ref_create_fails
                    or body["ref"].removeprefix("refs/heads/") in self.bootstrap_occupied_gap_refs
                ):
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
            m = _SCAFFOLD_PULL_ONE_RE.match(endpoint)
            if m and body.get("state") == "closed":
                number = int(m.group(2))
                self.closed_pulls.append(number)
                self.pulls.setdefault(number, {})["state"] = "closed"
                return b"{}"
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


def _run(fake, argv, isatty=False, log_dir=None):
    """Runs `repo setup <argv>` against `fake`, returning (exit_code,
    stdout, stderr).

    The run's log goes to a temporary directory, never the real
    $XDG_STATE_HOME: a suite that wrote there would leave a file per test
    in the developer's own state directory. `log_dir` points it somewhere
    a test can then read.
    """
    out, err = StringIO(), StringIO()
    with tempfile.TemporaryDirectory() as state:
        # Point XDG_CONFIG_HOME at an empty temp dir too, so a run reads no
        # fleet config unless the test passes one with --config: a real
        # ~/.config/repo/config.yaml on the developer's machine must not
        # leak into these tests.
        with patch.dict(
            os.environ, {"XDG_STATE_HOME": log_dir or state, "XDG_CONFIG_HOME": state}
        ):
            return _run_captured(fake, argv, isatty, out, err)


def _run_captured(fake, argv, isatty, out, err):
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
            # Covers main by name already, so the widening only adds the
            # other two refs; a ruleset that does NOT reach main and carries
            # a check is refused instead (see the widening test).
            "conditions": {"ref_name": {"include": ["refs/heads/release", "refs/heads/main"], "exclude": []}},
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
            ["refs/heads/release", "refs/heads/main", "~DEFAULT_BRANCH", "refs/heads/master"],
        )
        self.assertEqual(body["conditions"]["ref_name"]["exclude"], [])
        # target and bypass_actors are still untouched -- widening the
        # include list is the ONE thing an update rewrites outside `rules`.
        self.assertEqual(body["target"], "branch")
        self.assertEqual(body["bypass_actors"], [{"actor_id": 1, "actor_type": "Team"}])
        checks_rule = next(r for r in body["rules"] if r["type"] == "required_status_checks")
        # `old-check` is not in the standard, and stays exactly as it was,
        # App binding included: the standard is a floor.
        self.assertEqual(
            checks_rule["parameters"]["required_status_checks"],
            [{"context": "lanes"}, {"context": "old-check", "integration_id": 9}],
        )
        # An already-present required_linear_history/non_fast_forward rule
        # is left alone, not duplicated -- these two take no parameters, so
        # managing them is purely a presence check.
        types = [rule["type"] for rule in body["rules"]]
        self.assertEqual(types.count("required_linear_history"), 1)
        self.assertEqual(types.count("non_fast_forward"), 1)
        self.assertIn("now also targeting ~DEFAULT_BRANCH, refs/heads/master", out)
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

    def test_the_bypass_note_survives_the_quiet_default(self):
        # The section for a step with nothing to do is dropped, and this
        # ruleset has nothing to do -- but the note is the one line here
        # that reports a gap rather than a state, so "nothing to change"
        # standing alone over a ruleset anyone on that list can override
        # is worse than the recital it replaced (Codex review,
        # mikelward/repo#45). Same fixture as the -v test above, run
        # without it.
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
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.posts, [])
        self.assertIn("1 bypass actor(s)", out)
        self.assertIn("repo audit", out)
        # The other steps are still dropped -- this keeps one section, not
        # the recital.
        self.assertNotIn("auto-merge:", out)

    def test_dry_run_makes_no_writes(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.puts, [])
        self.assertIn("would create ruleset", out)


    def test_a_check_that_has_never_run_is_deferred_not_refused(self):
        # SPEC.md, *The ladder*: a check is required once it has passed
        # here, and until then the rest of the ruleset lands without it.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: []}  # 'lanes' has never run
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("deferred, not required yet: 'lanes' has never run here", out)
        self.assertIn("a later run requires it once it has passed", out)
        self.assertNotIn("never reported", err)

        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(len(ruleset_posts), 1)
        self.assertEqual(ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"], [])
        self.assertIn("required checks: none yet", out)
        self.assertIn("deferred, not required yet: 'lanes' has never run here", out)


    def test_force_does_not_require_a_check_that_has_never_passed(self):
        # --force means "apply without asking" and nothing else (SPEC.md,
        # *Flags*): it used to waive the never-reported guard, and a guard
        # the fleet loop's own flag turns off is not a guard.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: []}
        code, out, err = _run(fake, ["--force", "--rule", "never-reported", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("--force given", err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(len(ruleset_posts), 1)
        self.assertEqual(ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"], [])
        self.assertIn("'never-reported' has never run here", out)

    def test_a_check_that_ran_but_never_passed_is_deferred_and_said_so(self):
        # A check that runs and fails blocks every merge exactly as one
        # that never runs -- and the reader needs to know which it is, since
        # one needs a pull request and the other needs a fix.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: [("zizmor", None, "failure")]}
        code, out, err = _run(fake, ["--force", "--rule", "zizmor", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("'zizmor' has run here but never passed", out)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"], [])

    def test_a_check_that_passed_on_a_closed_pull_request_counts(self):
        # "Passed" is read from the default branch head, then open and
        # closed pull request heads -- the scaffold pull request that first
        # ran `lanes` is closed (merged) by the run that requires it.
        fake = FakeGh()
        fake.check_runs = {"prhead9": ["lanes"]}
        fake.closed_prs = ["prhead9"]
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(
            ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"],
            [{"context": "lanes"}],
        )
        self.assertNotIn("deferred", out)

    def test_a_commits_evidence_is_read_once_per_run(self):
        # The evidence question is asked several times in one run (the
        # preview, the write's fresh recompute, the binding's own passes),
        # and each walk costs two reads per commit -- so a run memoizes
        # what it read about a commit, and reads it once (Codex review,
        # mikelward/repo#56). A pass does not un-happen, which is what
        # makes the memo safe for the length of one run.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", "--no-bootstrap", REPO])
        self.assertEqual(code, 0, err)
        reads = [
            c for c in fake.calls
            if c[0] == "api" and any(f"/commits/{fake.default_head_sha}/check-runs" in str(a) for a in c)
        ]
        self.assertEqual(len(reads), 1, reads)
        # And never across runs: the next run reads afresh.
        fake.check_runs = {fake.default_head_sha: ["lanes", "zizmor"]}
        fake.calls.clear()
        code, out, err = _run(fake, ["--dry-run", "--no-bootstrap", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("'codex' has never run here", out)

    def test_a_pass_on_a_pull_request_beyond_the_newest_hundred_still_counts(self):
        # The only pass is on the 101st most recently updated closed pull
        # request: the evidence scan pages past the first hundred rather
        # than reading "never" off a capped listing on every run (Codex
        # review, mikelward/repo#56).
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "zizmor"], "old-pass": ["codex"]}
        fake.closed_prs = [f"closed{n}" for n in range(100)] + ["old-pass"]
        code, out, err = _run(fake, ["--force", "--no-bootstrap", REPO])
        self.assertEqual(code, 0, err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(len(ruleset_posts), 1)
        self.assertEqual(
            [c["context"] for c in ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"]],
            ["lanes", "codex", "zizmor"],
        )
        listings = [c[1] for c in fake.calls if c[0] == "api" and "pulls?state=closed" in c[1]]
        # Two pages per scan (the preview's and the write's own): the second
        # is read, and nothing past the page that settled it.
        self.assertTrue(any("page=2" in listing for listing in listings), listings)
        self.assertFalse(any("page=3" in listing for listing in listings), listings)

        # And the walk is bounded: a pass older than the newest five hundred
        # is not counted, so a check that never passed does not spend the
        # hour's API budget on every run (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "zizmor"], "old-pass": ["codex"]}
        fake.closed_prs = [f"closed{n}" for n in range(500)] + ["old-pass"]
        code, out, err = _run(fake, ["--force", "--no-bootstrap", REPO])
        self.assertEqual(code, 0, err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(
            [c["context"] for c in ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"]],
            ["lanes", "zizmor"],
        )
        self.assertIn("'codex' has never run here", out)
        listings = [c[1] for c in fake.calls if c[0] == "api" and "pulls?state=closed" in c[1]]
        self.assertTrue(any("page=5" in listing for listing in listings), listings)
        self.assertFalse(any("page=6" in listing for listing in listings), listings)

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
        # GitHub does not make the name unique, so this run writes one
        # and leaves the other. Reported, not resolved: the standard is a
        # FLOOR (maintainer, 2026-09-06), so an extra is not a half-done
        # repository and there is nothing to reconcile. The note reports
        # what the name lookup found and points at it -- enforcement,
        # scope and rules were never read, so it claims nothing about
        # what the extra does (Codex review, mikelward/repo#44).
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
        self.assertIn("Worth reading id 8 to see what it says", err)
        self.assertNotIn("aggregate", err)
        self.assertNotIn("held to at least", err)
        self.assertNotIn("reconcile", err.lower())
        self.assertEqual(fake.puts, [])
        # Never deleted: proving an extra adds nothing needs the per-field
        # strictness comparison _comparable_ruleset exists to avoid, and
        # getting it wrong deletes a ruleset holding the branch up.
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

    def test_both_of_a_differing_pair_are_named_not_just_one(self):
        # Two rulesets can share a legacy name. Both go, and both are
        # named -- one standing in for the pair would leave a reader
        # thinking a single body was recorded when two were.
        fake = FakeGh()
        self._matching_pair(fake, legacy_rules=[{"type": "required_signatures"}])
        fake.legacy_ruleset_ids = ["9", "10"]
        fake.all_ruleset_ids = ["1", "9", "10"]
        fake.ruleset_objects["10"] = dict(fake.ruleset_objects["9"], id=10)
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(fake.deleted_rulesets), ["10", "9"])
        self.assertIn("(id 9) is NOT identical", err)
        self.assertIn("(id 10) is NOT identical", err)

    def test_a_difference_appearing_after_the_preview_is_still_reported(self):
        # The plan called this duplicate identical, and the quiet apply
        # path prints the plan's note nowhere -- so the note has to come
        # from the body actually about to be deleted (Codex review,
        # mikelward/repo#46).
        fake = FakeGh()
        self._matching_pair(fake)
        fake.ruleset_objects["1"]["rules"] = [
            r for r in fake.ruleset_objects["1"]["rules"] if r["type"] != "non_fast_forward"
        ]
        changed = dict(fake.ruleset_objects["9"])
        changed["rules"] = [*changed["rules"], {"type": "required_signatures"}]
        fake.ruleset_objects_after_change["9"] = changed
        fake.ruleset_content_change_threshold = 1 << 30
        fake.change_rulesets_after_put = True
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, ["9"])
        self.assertIn("(id 9) is NOT identical to 'main'", err)

    def test_a_dry_run_names_the_deletion_and_that_it_differs(self):
        fake = FakeGh()
        self._matching_pair(fake, legacy_rules=[{"type": "required_signatures"}])
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        plan = out + err
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, [])
        self.assertIn("would delete the superseded ruleset 'merge gates' (id 9)", plan)
        self.assertIn("NOT identical to what 'main' will hold", plan)

    def test_without_a_log_the_body_goes_to_the_terminal(self):
        # --no-log is opting out of the file, not out of being able to
        # undo the deletion -- so the record has to land somewhere.
        fake = FakeGh()
        self._matching_pair(fake, legacy_rules=[{"type": "required_signatures"}])
        code, out, err = _run(fake, ["--force", "--no-log", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, ["9"])
        self.assertIn("required_signatures", out + err)
        self.assertIn("POST this back", out + err)

    def test_a_legacy_ruleset_that_differs_is_deleted_and_recorded(self):
        # Identity used to be the gate. It no longer is: converging on one
        # ruleset is the point, so a legacy-named one goes whatever it
        # holds (maintainer, 2026-09-07). What that costs is stated rather
        # than hidden -- the note says it is NOT identical, and the body
        # goes into the record so it can be POSTed back.
        fake = FakeGh()
        self._matching_pair(fake, legacy_rules=[{"type": "required_signatures"}])
        with tempfile.TemporaryDirectory() as state:
            code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO], log_dir=state)
            logged = _only_log(state)
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, ["9"])
        self.assertIn("'merge gates' (id 9) is NOT identical to 'main'", err)
        # The body is the log's job -- a wall of JSON on the terminal is
        # what the quieting exists to prevent.
        self.assertIn("required_signatures", logged)
        self.assertIn("POST this back", logged)
        self.assertNotIn("required_signatures", out)

    def test_a_legacy_ruleset_covering_a_ref_the_survivor_does_not_is_deleted(self):
        # Scope differences are part of what "not identical" covers, and
        # the recorded body is what makes losing refs/heads/release
        # reversible rather than silent.
        fake = FakeGh()
        self._matching_pair(
            fake,
            legacy_scope={"ref_name": {"include": [*_HARDENED_SCOPE, "refs/heads/release"], "exclude": []}},
        )
        with tempfile.TemporaryDirectory() as state:
            code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO], log_dir=state)
            logged = _only_log(state)
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, ["9"])
        self.assertIn("NOT identical", err)
        self.assertIn("refs/heads/release", logged)

    def test_a_legacy_ruleset_with_a_bypass_actor_is_deleted(self):
        # Removing a bypass actor is a tightening, not a loss -- but it is
        # still a difference, so it is reported and recorded like any
        # other.
        fake = FakeGh()
        self._matching_pair(fake)
        fake.ruleset_objects["9"]["bypass_actors"] = [{"actor_id": 5, "actor_type": "Team"}]
        with tempfile.TemporaryDirectory() as state:
            code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO], log_dir=state)
            logged = _only_log(state)
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, ["9"])
        self.assertIn("NOT identical", err)
        self.assertIn('"actor_id": 5', logged)

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

    def test_a_duplicate_edited_during_the_write_is_deleted_as_it_now_is(self):
        # An edit landing in the window no longer holds the delete back --
        # every legacy-named ruleset goes (maintainer, 2026-09-07). What
        # the edit must not do is make the RECORD stale: the body written
        # down has to be the one actually removed, or restoring from it
        # would silently drop the edit.
        fake = FakeGh()
        self._matching_pair(fake)
        fake.ruleset_objects["1"]["rules"] = [
            r for r in fake.ruleset_objects["1"]["rules"] if r["type"] != "non_fast_forward"
        ]
        changed = dict(fake.ruleset_objects["9"])
        changed["rules"] = [*changed["rules"], {"type": "required_signatures"}]
        fake.ruleset_objects_after_change["9"] = changed
        # Only after the survivor's own PUT, which is what makes this the
        # window the plan's earlier read cannot cover.
        fake.ruleset_content_change_threshold = 1 << 30
        fake.change_rulesets_after_put = True
        with tempfile.TemporaryDirectory() as state:
            code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO], log_dir=state)
            logged = _only_log(state)
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.deleted_rulesets, ["9"])
        # The body recorded is the one actually removed, read fresh right
        # before the delete -- not the copy the plan saw.
        self.assertIn("required_signatures", logged)

    def test_a_rename_in_that_window_keeps_the_duplicate(self):
        # The one thing the fresh read still has to establish: an
        # administrator renaming the duplicate to 'main' in this window
        # would otherwise have the newly canonical ruleset deleted and the
        # repository left with nothing under that name (Codex review,
        # mikelward/repo#31).
        fake = FakeGh()
        self._matching_pair(fake)
        fake.ruleset_objects["1"]["rules"] = [
            r for r in fake.ruleset_objects["1"]["rules"] if r["type"] != "non_fast_forward"
        ]
        fake.ruleset_objects_after_change["9"] = dict(fake.ruleset_objects["9"], name="main")
        fake.ruleset_content_change_threshold = 1 << 30
        fake.change_rulesets_after_put = True
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted_rulesets, [])
        self.assertIn("renamed", err)

    def test_a_failed_re_read_before_the_delete_keeps_the_duplicate(self):
        # "Could not tell" is not "safe to delete" -- and with no body
        # read there is nothing to record, so the delete would be the
        # unrecoverable kind.
        fake = FakeGh()
        self._matching_pair(fake)
        fake.ruleset_objects["1"]["rules"] = [
            r for r in fake.ruleset_objects["1"]["rules"] if r["type"] != "non_fast_forward"
        ]
        fake.ruleset_read_fails_after_put.add("9")
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

    def test_no_rules_skips_the_ruleset_step_but_still_checks_for_a_sibling(self):
        fake = FakeGh()
        fake.sibling_exists = True
        code, _, err = _run(fake, ["--no-rules", "--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.puts, [])
        self.assertIn("has a branch named 'master' beside its default branch 'main'", err)

    def test_sibling_branch_holds_the_ruleset_step_for_a_person(self):
        # A real master beside main is the branch the literal targeting
        # exists to lock out; a ruleset written onto it would enforce
        # there what nothing here can tell it satisfies. Held: said in the
        # preview, no ruleset written, every other step still runs.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.sibling_exists = True
        fake.bootstrap_existing_paths = set()
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("not writing ruleset 'main' -- owner/repo has a branch named 'master' beside its default branch 'main'", err)
        self.assertIn("Delete or rename 'master'", err)

        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("has a branch named 'master' beside its default branch 'main'", err)
        self.assertIn("skipping the ruleset step -- held for a person", err)
        self.assertIn("failed on: ruleset", err)
        self.assertFalse([p for p in fake.posts if "rulesets" in p[0][3]])
        self.assertEqual(fake.puts, [])
        # Held for a person, and only that step: the scaffold pull request
        # still opens.
        self.assertIn(f"{REPO}: opened pull request #42", out)

    def test_sibling_branch_does_not_hold_an_already_compliant_ruleset(self):
        # Nothing to write means nothing to hold: the branch is still said,
        # and the run exits clean, since the ruleset is at the standard.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.sibling_exists = True
        fake.existing_ruleset_id = "7"
        fake.all_ruleset_ids = ["7"]
        fake.ruleset_objects["7"] = {
            "id": 7,
            "name": "main",
            "target": "branch",
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": ["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master"],
                    "exclude": [],
                }
            },
            "rules": _rules_with(["lanes"]),
        }
        code, out, err = _run(fake, ["--force", "-v", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("already matches; nothing to do", out)
        self.assertIn("has a branch named 'master' beside its default branch 'main'", err)
        self.assertNotIn("held for a person", err)
        self.assertEqual(fake.puts, [])

    def test_sibling_branch_check_is_read_once_per_run(self):
        # Asked by the advisory check and by each ruleset preview and
        # write, answered by one read: the answer does not change inside
        # a run (the write's own last look, below, is the one re-read).
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.sibling_exists = True
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        reads = [c for c in fake.calls if c[:2] == ["api", f"repos/{REPO}/git/ref/heads/master"]]
        self.assertEqual(len(reads), 1)

    def test_a_sibling_created_during_the_confirmation_wait_holds_the_write(self):
        # The preview saw no master; one appears before the write. The
        # write's own last look reads past the run's memo -- like every
        # other precondition it re-reads there -- and holds (Codex
        # review, mikelward/repo#56).
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.sibling_created_after_reads = 1
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertNotIn("beside its default branch", out)  # the preview saw none
        self.assertIn("not writing ruleset 'main' -- owner/repo has a branch named 'master' beside its default branch 'main'", err)
        self.assertIn("failed on: ruleset", err)
        self.assertEqual(fake.puts, [])
        self.assertFalse([p for p in fake.posts if "rulesets" in p[0][3]])

    def test_no_sibling_warning_when_master_is_absent(self):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.sibling_exists = False
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        # Not a bare "master" substring check: the printed plan legitimately
        # mentions master as part of the ruleset's own hardened targeting
        # ("...on main, main and master") -- it's specifically the
        # branch-exists warning that must be absent.
        self.assertNotIn("beside its default branch", err)
        self.assertNotIn("held for a person", err)

    def test_sibling_is_read_as_a_git_ref_never_the_redirecting_branches_endpoint(self):
        # A repository renamed master -> main keeps a 301 from the old name
        # on the branches endpoint, and gh follows it, so that endpoint
        # answers 200 with main's record -- a standing false "master
        # exists" on exactly the repositories that closed the backdoor by
        # renaming. The git ref read answers only for a ref that exists.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(["api", f"repos/{REPO}/git/ref/heads/master"], fake.calls)
        self.assertFalse([c for c in fake.calls if c[:2] == ["api", f"repos/{REPO}/branches/master"]])

    def test_a_master_default_branch_is_not_its_own_sibling(self):
        # A fork whose default branch is master has no main beside it:
        # nothing to hold, and the lock there is the literal main.
        fake = FakeGh()
        fake.default_branch = "master"
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        code, _, err = _run(fake, ["--force", "--no-bootstrap", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("beside its default branch", err)
        self.assertNotIn("held for a person", err)
        self.assertIn(["api", f"repos/{REPO}/git/ref/heads/main"], fake.calls)

    def test_sibling_branch_check_failure_fails_the_ruleset_step_only(self):
        # The advisory check says it could not tell and goes on; the
        # ruleset step, which would write onto that branch, fails rather
        # than guessing. It fails alone -- a preview that cannot finish
        # skips its own step, like a hold, and leaves every other step to
        # make its progress (SPEC.md, invariant 1).
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}
        fake.sibling_error = "gh: HTTP 403: Resource protected by organization SAML enforcement\n"
        code, _, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not check whether", err)
        self.assertIn("403", err)
        self.assertIn("its preview could not finish", err)
        self.assertIn("failed on: ruleset", err)
        self.assertNotIn("the preview above failed", err)
        self.assertNotIn("held for a person", err)
        self.assertEqual(fake.puts, [])

    def test_empty_check_name_is_a_usage_error_not_a_preview_failure(self):
        # apply_ruleset validates check names before any gh call it makes;
        # that usage error must propagate directly (exit 2), not be folded
        # into "the preview above failed" (exit 1), which would misreport
        # a usage problem as a remote-state one.
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--rule", "", REPO])
        self.assertEqual(code, 2)
        self.assertNotIn("preview", err)


    def test_a_never_passed_check_is_deferred_inside_the_confirmed_plan(self):
        # The deferral is part of the plan the person confirms -- the
        # ruleset they say yes to is the one written, with the check left
        # out and named -- not a guard a "yes" could waive.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: []}  # 'lanes' has never run
        with patch("builtins.input", return_value="y") as mock_input:
            code, out, err = _run(fake, ["--rule", "lanes", REPO], isatty=True)
        self.assertEqual(code, 0, err)
        mock_input.assert_called_once()
        self.assertIn("deferred, not required yet: 'lanes' has never run here", err)  # the plan
        # The names themselves, not a repr of the records they arrive in.
        self.assertNotIn("None", err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"], [])




    def test_a_check_falling_out_of_the_passed_set_before_the_real_apply_is_refused(self):
        # The preview saw `lanes` passed and planned to require it; by the
        # real apply's own fresh scan nothing has (the default branch
        # advancing to a commit nothing has reported on yet, say). The
        # fresh plan defers `lanes`, so its body differs from the one
        # confirmed, and the fingerprint refuses rather than writing
        # either body unconfirmed.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes"]}

        real_collect_reported = rules._collect_reported
        calls = []

        def flaky_collect_reported(repo, wanted, **kwargs):
            calls.append(1)
            if len(calls) >= 2:
                return rules._Reported()  # nothing reported
            return real_collect_reported(repo, wanted, **kwargs)

        with patch("repo_lib.rules._collect_reported", side_effect=flaky_collect_reported):
            with patch("builtins.input", return_value="y"):
                code, _, err = _run(fake, ["--rule", "lanes", REPO], isatty=True)
        self.assertEqual(code, 1)
        self.assertIn("failed on:", err)
        self.assertIn("ruleset", err)
        self.assertIn("no longer matches what was previewed", err)
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

        def flaky_build_update_body(repo, existing_id, checks, ruleset_name, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                fake.ruleset_objects["7"]["rules"][0]["parameters"]["required_status_checks"] = []
            return real_build_update_body(repo, existing_id, checks, ruleset_name, **kwargs)

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


# GitHub's own refusal when a token that is not a GitHub App user token
# asks for installations -- the wording apps.is_missing_app_token keys on.
_APP_TOKEN_403 = (
    "gh: You must authenticate with an access token authorized to a GitHub App "
    "in order to list installations (HTTP 403)\n"
)


class CredentialPreflightTest(unittest.TestCase):
    def test_a_run_whose_credentials_are_refused_touches_nothing(self):
        fake = FakeGh()
        fake.auth_fails = "gh: To get started with GitHub CLI, please run: gh auth login\n"
        code, _, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 2)
        self.assertIn("gh auth login", err)
        # The preflight read itself, and nothing after it: no repository
        # read, and above all no write.
        self.assertEqual(fake.calls, [["api", "user", "--jq", ".login"]])

    def test_an_expired_token_is_a_refusal_too(self):
        fake = FakeGh()
        fake.auth_fails = "gh: HTTP 401: Bad credentials (https://api.github.com/user)\n"
        code, _, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [["api", "user", "--jq", ".login"]])

    def test_a_probe_failure_that_is_not_a_refusal_does_not_stop_the_run(self):
        # A 500 says nothing about the token (Codex, mikelward/repo#60).
        # Stopping here would cost every step of every repository in the
        # fleet over one unlucky read -- exactly what invariant 1 forbids.
        fake = FakeGh()
        fake.auth_fails = "gh: HTTP 500: Internal Server Error (https://api.github.com/user)\n"
        fake.allow_auto_merge = "false"
        code, _, err = _run(fake, ["--force", "--no-rules", "--no-bootstrap", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("could not check this gh token", err)
        self.assertIn("500", err)
        self.assertTrue(fake.patches, "the run went on to do its work")

    def test_working_credentials_cost_one_read(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--no-rules", "--no-bootstrap", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.calls.count(["api", "user", "--jq", ".login"]), 1)

    def test_app_without_installation_access_stops_the_run(self):
        fake = FakeGh()
        fake.installations_read_fails = _APP_TOKEN_403
        code, _, err = _run(fake, ["--force", "--app", "some-app", REPO])
        self.assertEqual(code, 2)
        self.assertIn("--app some-app", err)
        # The hopeless case says so: re-authenticating cannot grant this.
        self.assertIn("GitHub App user-to-server token", err)
        self.assertIn("Drop --app", err)
        self.assertNotIn(["api", "--method", "PUT"], [c[:3] for c in fake.calls])

    def test_an_ordinary_installations_failure_leaves_the_run_going(self):
        # Same reasoning as the auth probe: only "this token never may" is
        # the request being impossible. A 500 leaves the App step to fail
        # on its own, with every other step still making its progress.
        fake = FakeGh()
        fake.installations_read_fails = "gh: HTTP 500: Internal Server Error\n"
        code, _, err = _run(fake, ["--force", "--no-rules", "--no-bootstrap", "--app", "some-app", REPO])
        self.assertNotEqual(code, 2)
        self.assertNotIn("GitHub App user-to-server token", err)
        self.assertIn("could not list this account's App installations", err)

    def test_the_app_token_403_is_explained_where_the_binding_evidence_needs_it(self):
        # The status-creator resolution reads the same endpoint, and there
        # it fails only its own step. It still has to say WHY: a bare 403
        # reads as something a rerun or a scope change fixes, and this one
        # is neither.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["codex", "zizmor"]}
        fake.statuses = {fake.default_head_sha: [("lanes", "success")]}
        fake.status_creators = {fake.default_head_sha: [("lanes", "lanes-app[bot]")]}
        fake.app_coverage_fails = _APP_TOKEN_403
        with patch("repo_lib.gh.run", fake.run), patch("repo_lib.gh.try_run", fake.try_run):
            rules.reset_evidence_cache()
            with self.assertRaises(rules.RulesetError) as caught:
                rules.never_passed(REPO, [("lanes", 12345)])
        self.assertIn("GitHub App user-to-server token", str(caught.exception))

    def test_without_app_an_unreadable_installations_list_holds_only_its_own_step(self):
        # Invariant 1 (SPEC.md): the binding evidence needs the same
        # endpoint, but the scaffold, credential and settings steps do not,
        # so the run makes their progress rather than stopping.
        fake = FakeGh()
        fake.installations_read_fails = _APP_TOKEN_403
        fake.app_coverage_fails = _APP_TOKEN_403
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        fake.allow_auto_merge = "false"
        code, out, err = _run(fake, ["--force", "--no-bootstrap", REPO])
        self.assertEqual(fake.calls.count(["api", "user/installations", "--jq", ".total_count"]), 0)
        self.assertIn("auto-merge", (out + err).lower())
        self.assertTrue(fake.patches, "the auto-merge step still applied")


def _config_file(tmpdir, text):
    path = os.path.join(tmpdir, "config.yaml")
    with open(path, "w") as f:
        f.write(text)
    return path


class SetupConfigFileTest(unittest.TestCase):
    def test_no_config_ignores_a_named_config(self):
        # --no-config wins even over an explicit --config: the config's
        # rules are never read, so the default checks are used instead.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config_file(tmp, "rules:\n  - lanes\n")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            code, out, err = _run(
                fake,
                ["--force", "--no-bootstrap", "--no-config", "--config", cfg, REPO],
            )
        self.assertEqual(code, 0, err)
        contexts = [
            c["context"]
            for rule in fake.posts[0][1]["rules"]
            if rule["type"] == "required_status_checks"
            for c in rule["parameters"]["required_status_checks"]
        ]
        self.assertEqual(contexts, ["lanes", "codex", "zizmor"])  # defaults, not config

    def test_config_force_lets_the_bare_command_apply_without_confirming(self):
        # force: true in the config is what makes `repo setup <repo>` (no
        # --force) the convergence invocation.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config_file(tmp, "force: true\n")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            code, out, err = _run(fake, ["--no-bootstrap", "--config", cfg, REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.posts), 1)  # the ruleset was created, unprompted

    def test_no_force_overrides_config_force_keeping_the_rest(self):
        # --no-force cancels a config force: true for a one-off confirmation,
        # without --no-config discarding the rest of the fleet config. With
        # no terminal to confirm at, the run applies nothing.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config_file(tmp, "force: true\n")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            code, _, err = _run(fake, ["--no-force", "--no-bootstrap", "--config", cfg, REPO])
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(fake.posts, [])  # nothing applied

    def test_config_rules_replace_the_default_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config_file(tmp, "rules:\n  - lanes\n  - zizmor\n")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "zizmor"]}
            code, out, err = _run(fake, ["--force", "--no-bootstrap", "--config", cfg, REPO])
        self.assertEqual(code, 0, err)
        contexts = [
            c["context"]
            for rule in fake.posts[0][1]["rules"]
            if rule["type"] == "required_status_checks"
            for c in rule["parameters"]["required_status_checks"]
        ]
        self.assertEqual(contexts, ["lanes", "zizmor"])

    def test_config_rules_are_ignored_under_no_rules(self):
        # --no-rules turns the step off; config rules must not resurrect it
        # or trip the --no-rules/--rule contradiction check.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config_file(tmp, "rules:\n  - lanes\n")
            fake = FakeGh()
            code, out, err = _run(
                fake, ["--force", "--no-rules", "--no-bootstrap", "--config", cfg, REPO]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.posts, [])  # no ruleset written

    def test_no_app_overrides_configured_apps(self):
        # --no-app drops the config's apps for one run without --no-config
        # discarding the rest. Proven via the App-token precheck: with a
        # configured app it would exit 2, and --no-app skips it.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config_file(tmp, "apps:\n  - some-app\n")
            fake = FakeGh()
            fake.installations_read_fails = _APP_TOKEN_403
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            code, _, err = _run(
                fake, ["--force", "--no-bootstrap", "--no-app", "--config", cfg, REPO]
            )
        self.assertNotEqual(code, 2, err)
        self.assertNotIn("GitHub App user-to-server token", err)

    def test_configured_apps_without_no_app_still_hit_the_precheck(self):
        # The control for the test above: the configured app IS applied when
        # --no-app is absent, so the failing installations read stops the run.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config_file(tmp, "apps:\n  - some-app\n")
            fake = FakeGh()
            fake.installations_read_fails = _APP_TOKEN_403
            code, _, err = _run(fake, ["--force", "--no-bootstrap", "--config", cfg, REPO])
        self.assertEqual(code, 2)
        self.assertIn("GitHub App user-to-server token", err)

    def test_no_app_and_app_are_contradictory(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--no-app", "--app", "some-app", REPO])
        self.assertEqual(code, 2)
        self.assertIn("contradictory", err)

    def test_a_missing_named_config_is_a_usage_error(self):
        fake = FakeGh()
        code, _, err = _run(fake, ["--force", "--config", "/no/such/config.yaml", REPO])
        self.assertEqual(code, 2)
        self.assertIn("not found", err)
        self.assertEqual(fake.calls, [])

    def test_a_config_credential_path_that_cannot_open_is_a_usage_error(self):
        # A NUL ("embedded null byte" -> ValueError) or a lone surrogate
        # ("\uD800" -> UnicodeEncodeError) makes open() raise a ValueError,
        # not an OSError; the credential read must still exit 2, not
        # traceback (Codex, mikelward/repo#62).
        for path_literal in ('"/tmp/\\0/x"', '"\\uD800"'):
            with tempfile.TemporaryDirectory() as tmp:
                cfg = _config_file(tmp, f"credentials:\n  LANES_APP_ID: {path_literal}\n")
                fake = FakeGh()
                code, _, err = _run(
                    fake, ["--force", "--no-rules", "--no-bootstrap", "--config", cfg, REPO]
                )
            self.assertEqual(code, 2, path_literal)
            self.assertIn("cannot read", err)

    def test_a_config_naming_a_non_fleet_credential_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = _secret_file(tmp, "v.txt")
            cfg = _config_file(tmp, f"credentials:\n  NOT_A_FLEET_CRED: {key}\n")
            fake = FakeGh()
            code, _, err = _run(fake, ["--force", "--config", cfg, REPO])
        self.assertEqual(code, 2)
        self.assertIn("fleet credential", err)


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

    def test_a_secret_dry_run_failure_does_not_block_the_other_steps(self):
        # A secret whose plan-build read fails ("error" state) fails its
        # own step and nothing else. SPEC.md invariant 1: a step that
        # cannot finish does not hold back the parts that can, which is
        # what makes one fleet-wide invocation converge the repositories
        # it can rather than abort on whichever step is unhappy.
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.repo_missing = True  # makes the secret's own plan-build read fail
            code, _, err = _run(fake, ["--force", "--secret", f"TOKEN={path}", REPO])
        self.assertEqual(code, 1)
        self.assertIn("secret:TOKEN", err)
        self.assertNotIn("the preview above failed", err)
        self.assertEqual(fake.written_secrets, [])

    def test_a_ruleset_preview_failure_does_not_block_the_other_steps(self):
        # The case a fleet run actually hits: one unreadable precondition
        # of the ruleset write (here a 403 on the sibling-branch read; in
        # the field, a check-runs or App read the token cannot make) used
        # to abort the whole run, so a repository with one unreadable
        # thing converged on nothing -- run after run, since nothing about
        # it changes on its own. The secret step still writes; the ruleset
        # step alone is skipped and counted (SPEC.md, invariant 1).
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes"]}
            fake.sibling_error = "gh: HTTP 403: Resource protected by organization SAML enforcement\n"
            code, _, err = _run(
                fake, ["--force", "--rule", "lanes", "--secret", f"TOKEN={path}", REPO]
            )
        self.assertEqual(code, 1)
        self.assertIn("its preview could not finish", err)
        self.assertIn("failed on: ruleset", err)
        self.assertNotIn("secret:TOKEN", err)
        self.assertEqual(fake.written_secrets, [("TOKEN", REPO, None, b"sekrit")])
        # And the ruleset itself is untouched -- skipped, not guessed at.
        self.assertEqual(fake.puts, [])

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

    def test_sibling_branch_check_runs_alongside_secret_and_app_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            fake.installations = [("codex", "111", "selected", "owner")]
            fake.sibling_exists = True
            code, _, err = _run(fake, ["--force", "--secret", f"TOKEN={path}", "--app", "codex", REPO])
        self.assertEqual(code, 1, err)
        self.assertIn("has a branch named 'master' beside its default branch 'main'", err)
        self.assertIn("skipping the ruleset step -- held for a person", err)


def _rules_with(contexts):
    """A ruleset body already holding every managed rule, requiring
    `contexts` -- so a plan against it has at most one thing to say."""
    return [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [{"context": c} for c in contexts],
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


class UpdatePlanTest(unittest.TestCase):
    """An update's plan names what the write adds, not everything the
    ruleset will end up holding."""

    def _existing(self, contexts):
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        fake.existing_ruleset_id = "1"
        fake.all_ruleset_ids = ["1"]
        fake.ruleset_objects["1"] = {
            "id": 1,
            "name": "main",
            "enforcement": "active",
            "target": "branch",
            "conditions": {
                "ref_name": {
                    "include": ["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master"],
                    "exclude": [],
                }
            },
            "rules": _rules_with(contexts),
        }
        return fake

    def test_an_update_names_only_the_added_check(self):
        fake = self._existing(("lanes", "codex"))
        code, out, err = _run(
            fake,
            ["--dry-run", "--rule", "lanes", "--rule", "codex", "--rule", "zizmor", REPO],
        )
        plan = out + err
        self.assertIn("would newly require: zizmor", plan)
        # Rules the ruleset already holds are not restated.
        self.assertNotIn("review conversations must be resolved", plan)
        self.assertNotIn("force pushes are blocked", plan)
        self.assertNotIn("commit history must be linear", plan)
        self.assertNotIn("rebase is the only merge method", plan)

    def test_an_update_names_a_protection_it_adds(self):
        # Same ruleset, but without the linear-history rule: that one line
        # is the change, and it is the line that shows.
        fake = self._existing(("lanes", "codex", "zizmor"))
        fake.ruleset_objects["1"]["rules"] = [
            r for r in fake.ruleset_objects["1"]["rules"]
            if r["type"] != "required_linear_history"
        ]
        code, out, err = _run(
            fake,
            ["--dry-run", "--rule", "lanes", "--rule", "codex", "--rule", "zizmor", REPO],
        )
        plan = out + err
        self.assertIn("commit history must be linear", plan)
        self.assertNotIn("force pushes are blocked", plan)
        self.assertNotIn("would newly require", plan)

    def test_an_update_keeps_both_bindings_of_a_check_required_from_two_apps(self):
        # GitHub lets one name be required from two Apps as two entries.
        # Collapsing them by name dropped one on every update -- the one
        # thing the floor promises never happens (Codex review,
        # mikelward/repo#56). Kept by (context, App), whether the name is
        # beyond the standard or asked for unbound.
        fake = self._existing(("lanes", "zizmor"))
        rules_ = fake.ruleset_objects["1"]["rules"]
        rules_[0]["parameters"]["required_status_checks"] = [
            {"context": "lanes", "integration_id": 111},
            {"context": "lanes", "integration_id": 222},
            {"context": "zizmor"},
        ]
        fake.ruleset_objects["1"]["rules"] = [
            r for r in rules_ if r["type"] != "required_linear_history"
        ]  # something else to write, so a write happens at all
        fake.check_runs = {fake.default_head_sha: [("lanes", 111), ("lanes", 222), "zizmor"]}
        code, out, err = _run(fake, ["--force", "--rule", "zizmor", REPO])
        self.assertEqual(code, 0, err)
        checks_rule = next(r for r in fake.puts[-1][1]["rules"] if r["type"] == "required_status_checks")
        self.assertEqual(
            checks_rule["parameters"]["required_status_checks"],
            [
                {"context": "zizmor"},
                {"context": "lanes", "integration_id": 111},
                {"context": "lanes", "integration_id": 222},
            ],
        )

        fake = self._existing(("lanes", "zizmor"))
        rules_ = fake.ruleset_objects["1"]["rules"]
        rules_[0]["parameters"]["required_status_checks"] = [
            {"context": "lanes", "integration_id": 111},
            {"context": "lanes", "integration_id": 222},
            {"context": "zizmor"},
        ]
        fake.ruleset_objects["1"]["rules"] = [
            r for r in rules_ if r["type"] != "required_linear_history"
        ]
        fake.check_runs = {fake.default_head_sha: [("lanes", 111), ("lanes", 222), "zizmor"]}
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        checks_rule = next(r for r in fake.puts[-1][1]["rules"] if r["type"] == "required_status_checks")
        self.assertEqual(
            checks_rule["parameters"]["required_status_checks"],
            [
                {"context": "lanes", "integration_id": 111},
                {"context": "lanes", "integration_id": 222},
                {"context": "zizmor"},
            ],
        )

    def test_an_update_keeps_a_check_beyond_the_standard_and_says_so(self):
        # The standard is a floor: a check the ruleset requires that this
        # run does not name stays required, exactly as it was. Dropping it
        # used to be the one plan line that loosened the gate, and with
        # deferral it could have dropped a working gate while every
        # replacement was still waiting (Codex security review,
        # mikelward/repo#56). Named, so the plan still says what the
        # ruleset requires beyond the standard.
        fake = self._existing(("lanes", "old-check"))
        fake.ruleset_objects["1"]["rules"] = [
            r for r in fake.ruleset_objects["1"]["rules"]
            if r["type"] != "required_linear_history"
        ]  # something else to write, so a plan is shown at all
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        plan = out + err
        self.assertIn("keeps requiring, beyond the standard: old-check", plan)
        self.assertNotIn("NO LONGER", plan)

        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [e["context"] for e in fake.puts[0][1]["rules"][0]["parameters"]["required_status_checks"]],
            ["lanes", "old-check"],
        )

    def test_a_deferred_replacement_never_drops_a_working_gate(self):
        # The security finding itself: every standard check is deferred
        # (their workflows are in the scaffold pull request), and the
        # ruleset requires an App-bound gate of its own. That gate stays,
        # bound as it was, rather than the write leaving the branch with
        # no required check at all.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: []}
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
                        "required_status_checks": [{"context": "security-scan", "integration_id": 99}],
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
        self.assertEqual(len(fake.puts), 1)
        self.assertEqual(
            fake.puts[0][1]["rules"][0]["parameters"]["required_status_checks"],
            [{"context": "security-scan", "integration_id": 99}],
        )
        self.assertIn("'lanes' waits for .github/workflows/ci.yml", out)

    def test_verbose_expands_an_update_to_every_rule(self):
        # The terminal gets the delta; --verbose and the log are supposed
        # to hold the whole picture, and the captured short plan cannot be
        # expanded back into it -- so the full rendering comes from the
        # report (Codex review, mikelward/repo#45).
        fake = self._existing(("lanes", "codex"))
        code, out, err = _run(
            fake,
            ["--dry-run", "-v", "--rule", "lanes", "--rule", "codex", "--rule", "zizmor", REPO],
        )
        plan = out + err
        self.assertIn("would update ruleset 'main'", plan)
        self.assertIn("required checks: lanes, codex, zizmor", plan)
        self.assertIn("review conversations must be resolved", plan)
        self.assertIn("commit history must be linear", plan)
        self.assertIn("force pushes are blocked", plan)

    def test_widening_the_scope_names_what_becomes_enforced_there(self):
        # The rules do not change, so the delta is empty -- but master
        # starts being protected all the same, and "also targeting
        # master" alone does not say by what (Codex review,
        # mikelward/repo#45).
        fake = self._existing(("lanes",))
        fake.ruleset_objects["1"]["conditions"]["ref_name"]["include"] = ["refs/heads/main"]
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        plan = out + err
        self.assertIn("scope: also targeting", plan)
        self.assertIn("newly effective on", plan)
        self.assertIn("required checks: lanes", plan)
        self.assertIn("force pushes are blocked", plan)
        # Still an update, and still no delta to report about the rules.
        self.assertNotIn("would newly require", plan)

    def test_the_full_plan_still_names_a_check_kept_beyond_the_standard(self):
        # The full rendering is the short plan PLUS the resulting state,
        # not the state instead of it -- swapping one for the other
        # dropped the short plan's own lines from --verbose and from the
        # log (Codex review, mikelward/repo#45).
        fake = self._existing(("lanes", "old-check"))
        fake.ruleset_objects["1"]["rules"] = [
            r for r in fake.ruleset_objects["1"]["rules"]
            if r["type"] != "required_linear_history"
        ]  # something to write, so there is a plan to render at all
        code, out, err = _run(fake, ["--dry-run", "-v", "--rule", "lanes", REPO])
        plan = out + err
        self.assertIn("keeps requiring, beyond the standard: old-check", plan)
        # ...and the resulting state is there too, under its own heading,
        # with the kept check in it.
        self.assertIn("after this write the ruleset holds:", plan)
        self.assertIn("required checks: lanes, old-check", plan)

    def test_a_preserved_unmanaged_rule_is_named_in_the_scope_block(self):
        # An update keeps a rule type this module never writes, and a
        # widening makes it effective on the refs it adds -- so the plan
        # has to say it is there. Named, not described: this module has no
        # wording for a rule it does not manage (Codex review,
        # mikelward/repo#45).
        fake = self._existing(("lanes",))
        fake.ruleset_objects["1"]["conditions"]["ref_name"]["include"] = ["refs/heads/main"]
        fake.ruleset_objects["1"]["rules"].append({"type": "required_signatures"})
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        plan = out + err
        self.assertIn("newly effective on", plan)
        self.assertIn("plus rules this tool does not manage", plan)
        self.assertIn("required_signatures", plan)

    def test_an_excluded_ref_is_not_claimed_as_newly_protected(self):
        # An exclusion outranks an include, so widening the include list
        # to cover master protects nothing while master is excluded --
        # and _report_excluded_hardened says exactly that on the same
        # run (Codex review, mikelward/repo#45).
        fake = self._existing(("lanes",))
        fake.ruleset_objects["1"]["conditions"]["ref_name"] = {
            "include": ["refs/heads/main"],
            "exclude": ["refs/heads/master"],
        }
        code, out, err = _run(fake, ["--dry-run", "--rule", "lanes", REPO])
        plan = out + err
        self.assertIn("scope: also targeting", plan)
        self.assertIn("excludes refs/heads/master", plan)
        # ~DEFAULT_BRANCH is still genuinely newly covered; master is not.
        self.assertIn("newly effective on ~DEFAULT_BRANCH:", plan)
        self.assertNotIn("newly effective on ~DEFAULT_BRANCH, refs/heads/master", plan)

    def test_a_create_still_lists_every_rule(self):
        # On a create all of it is new, so the list *is* the change.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        plan = out + err
        self.assertIn("would create ruleset 'main'", plan)
        self.assertIn("review conversations must be resolved", plan)
        self.assertIn("force pushes are blocked", plan)


def _only_log(state):
    """The single log file a run wrote under `state`, as text."""
    directory = os.path.join(state, "repo")
    names = os.listdir(directory)
    assert len(names) == 1, names
    with open(os.path.join(directory, names[0]), encoding="utf-8") as f:
        return f.read()


class RunLogTest(unittest.TestCase):
    """The terminal says what changed; the log keeps the full record."""

    def _read_log(self, state):
        return _only_log(state)

    def test_the_log_keeps_the_full_plan_the_terminal_leaves_out(self):
        with tempfile.TemporaryDirectory() as state:
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            code, out, err = _run(fake, ["--force", REPO], log_dir=state)
            self.assertEqual(code, 0, err)
            logged = self._read_log(state)
        # The terminal got what changed, not the plan.
        self.assertIn(f"{REPO}: created ruleset", out)
        self.assertNotIn("ruleset (repo-rules):", err)
        # The log got both, plus a header naming the run.
        self.assertIn(f"# repo setup {REPO} at ", logged)
        self.assertIn("ruleset (repo-rules):", logged)
        self.assertIn(f"{REPO}: created ruleset", logged)
        # ...including the sections the terminal drops as idle.
        self.assertIn("auto-merge:", logged)
        self.assertIn("full record: ", err)

    def test_a_run_that_changes_nothing_writes_no_log(self):
        # Over a fleet most runs find the repository in shape. A file per
        # repository per sweep, each saying nothing happened, is the same
        # noise in a different place.
        with tempfile.TemporaryDirectory() as state:
            fake = FakeGh()
            fake.workflow_files = ["ci.yml"]
            code, out, err = _run(fake, ["--no-rules", REPO], log_dir=state)
            self.assertEqual(code, 0, err)
            self.assertEqual(err, "")
            self.assertFalse(os.path.exists(os.path.join(state, "repo")))

    def test_a_declined_run_writes_no_log(self):
        # The plan is printed before the question, and printing it goes
        # through the log. A run answered "no" changed nothing, so it must
        # leave no file and claim no record (Codex review,
        # mikelward/repo#45).
        with tempfile.TemporaryDirectory() as state:
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            # Not a terminal and no --force: refused after showing the plan.
            code, _, err = _run(fake, [REPO], log_dir=state)
            self.assertEqual(code, 1)
            self.assertNotIn("full record", err)
            self.assertFalse(os.path.exists(os.path.join(state, "repo")))
            self.assertEqual(fake.posts, [])

    def test_a_refused_write_still_writes_the_log(self):
        # The arm gate is "a mutation is planned", not "a mutation
        # succeeded". A step that refuses its own write at the last
        # instant -- here a secret someone else created since the plan was
        # built -- changed nothing, and the record of WHY is exactly what
        # the file is for (Codex review, mikelward/repo#45).
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "value.txt")
            fake = FakeGh()
            fake.secret_names_after_recheck = {"TOKEN"}
            code, _, err = _run(
                fake,
                ["--force", "--no-rules", "--secret", f"TOKEN={path}", REPO],
                log_dir=state,
            )
            self.assertEqual(code, 1)
            self.assertEqual(fake.written_secrets, [])
            logged = self._read_log(state)
        self.assertIn("refusing to overwrite", logged)
        self.assertIn("full record: ", err)

    def test_an_interactive_run_still_logs_the_full_plan(self):
        # The terminal gets the short plan to answer; teeing that alone
        # would leave the log inheriting the terminal's omissions, which is
        # not the full record this advertises.
        with tempfile.TemporaryDirectory() as state:
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            with patch("builtins.input", return_value="y"):
                code, out, err = _run(fake, [REPO], isatty=True, log_dir=state)
            self.assertEqual(code, 0, err)
            logged = self._read_log(state)
        # The terminal was asked with the short plan...
        self.assertNotIn("auto-merge:", err)
        # ...and the log kept the full one anyway.
        self.assertIn("--- full plan ---", logged)
        self.assertIn("auto-merge:", logged)

    def test_a_verbose_no_op_run_writes_no_log(self):
        # -v buffers the progress markers and the full plan whether or not
        # anything follows, so arming unconditionally at Apply wrote a file
        # for a run that changed nothing (Codex review, mikelward/repo#45).
        with tempfile.TemporaryDirectory() as state:
            fake = FakeGh()
            fake.workflow_files = ["ci.yml"]
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            # The ruleset step is ON and already compliant, so the run goes
            # all the way to Apply with nothing to do -- the case a
            # `--no-rules` early return would never reach.
            fake.existing_ruleset_id = "1"
            fake.all_ruleset_ids = ["1"]
            fake.ruleset_objects["1"] = {
                "id": 1,
                "name": "main",
                "enforcement": "active",
                "target": "branch",
                "conditions": {
                    "ref_name": {
                        "include": [
                            "~DEFAULT_BRANCH",
                            "refs/heads/main",
                            "refs/heads/master",
                        ],
                        "exclude": [],
                    }
                },
                "rules": _rules_with(("lanes", "codex", "zizmor")),
            }
            code, out, err = _run(fake, ["--force", "-v", REPO], log_dir=state)
            self.assertEqual(code, 0, err)
            self.assertEqual(fake.puts, [])
            self.assertEqual(fake.posts, [])
            self.assertNotIn("full record", err)
            self.assertFalse(os.path.exists(os.path.join(state, "repo")))

    def test_a_log_that_cannot_be_written_is_not_called_full(self):
        # /dev/full accepts the open and fails the write, and the failure
        # can surface as late as close(), where the buffer is flushed.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, _, err = _run(fake, ["--force", "--log", "/dev/full", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("stopped short", err)
        self.assertNotIn("full record", err)

    def test_no_log_writes_nothing(self):
        with tempfile.TemporaryDirectory() as state:
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            code, _, err = _run(fake, ["--force", "--no-log", REPO], log_dir=state)
            self.assertEqual(code, 0, err)
            self.assertNotIn("full record", err)
            self.assertFalse(os.path.exists(os.path.join(state, "repo")))

    def test_a_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as state:
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            _run(fake, ["--dry-run", REPO], log_dir=state)
            self.assertFalse(os.path.exists(os.path.join(state, "repo")))


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

    # A consumer still on the ambient gate: it runs the lanes action but hands
    # it no `app-id`, so nothing publishes the status as the App.
    AMBIENT = (
        "jobs:\n  classify:\n    steps:\n      - uses: mikelward/lanes@main\n"
        "        with:\n          mode: classify\n"
    )

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

    def test_a_supplied_pair_is_placed_even_with_no_app_publisher(self):
        # Provisioning ahead of the workflow migration: the consumer still
        # runs the ambient gate (no `app-id` anywhere), so nothing publishes
        # as the App yet -- but --credential is an explicit instruction to
        # place the pair so the init/finalize jobs can authenticate once the
        # migrated workflow lands. It must be set and the environment
        # restricted, never routed to the unused path (Codex,
        # mikelward/repo#50).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher(self.AMBIENT)
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
        self.assertIn(
            "lanes: no workflow here publishes the lanes status as the App yet; placing the "
            "supplied credential in the 'lanes' environment for the migration",
            err,
        )
        self.assertIn("lanes: set LANES_APP_ID in environment 'lanes' (new)", err)
        self.assertIn("lanes: set LANES_APP_PRIVATE_KEY in environment 'lanes' (new)", err)
        self.assertEqual(
            [w[:3] for w in fake.written_secrets],
            [("LANES_APP_ID", REPO, "lanes"), ("LANES_APP_PRIVATE_KEY", REPO, "lanes")],
        )
        self.assertEqual(fake.restricted, [("lanes", "main")])
        # Never the unused path: not declined, not deleted.
        self.assertNotIn("nothing uses it", err)
        self.assertNotIn("not set", err)

    def test_a_config_supplied_pair_is_placed_like_a_command_line_one(self):
        # The pair read from the fleet config provisions exactly as the
        # typed --credential pair does -- this is what lets the convergence
        # loop drop the two --credential flags.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            cfg = _config_file(
                tmp,
                f"credentials:\n  LANES_APP_ID: {app_id}\n  LANES_APP_PRIVATE_KEY: {key}\n",
            )
            fake = self._publisher(self.AMBIENT)
            code, out, err = _run(fake, ["--force", "--no-rules", "--config", cfg, REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [w[:3] for w in fake.written_secrets],
            [("LANES_APP_ID", REPO, "lanes"), ("LANES_APP_PRIVATE_KEY", REPO, "lanes")],
        )

    def test_a_command_line_credential_overrides_the_config_for_the_same_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_id = _secret_file(tmp, "cfg_id.txt", b"11111")
            cli_id = _secret_file(tmp, "cli_id.txt", b"22222")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            cfg = _config_file(
                tmp,
                f"credentials:\n  LANES_APP_ID: {cfg_id}\n  LANES_APP_PRIVATE_KEY: {key}\n",
            )
            fake = self._publisher(self.AMBIENT)
            code, out, err = _run(
                fake,
                [
                    "--force", "--no-rules", "--config", cfg,
                    "--credential", f"LANES_APP_ID={cli_id}",
                    REPO,
                ],
            )
        self.assertEqual(code, 0, err)
        written_id = [v for name, _r, _e, v in fake.written_secrets if name == "LANES_APP_ID"]
        self.assertEqual(written_id, [b"22222"])  # the command line's value, not the config's

    def test_a_supplied_pair_already_in_the_environment_is_kept_not_deleted(self):
        # conf's case: the pair was provisioned into the restricted lanes
        # environment weeks ago, the workflow is not migrated yet, and
        # re-running setup with --credential must KEEP it. Deleting a value
        # GitHub never returns as "unused" is the foot-gun this fixes
        # (Codex, mikelward/repo#50).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher(self.AMBIENT)
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
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
        self.assertNotIn(("LANES_APP_ID", "lanes"), fake.deleted_secrets)
        self.assertNotIn(("LANES_APP_PRIVATE_KEY", "lanes"), fake.deleted_secrets)
        self.assertNotIn("nothing uses it", err)
        self.assertNotIn("not set", err)

    def test_a_supplied_pair_is_held_when_a_reusable_call_could_forward_it(self):
        # An external reusable call with `secrets: inherit` may forward the
        # pair to a workflow that reads THIS repository's repo/org secrets, so
        # supplying --credential must NOT delete those repository copies to
        # move the pair into the environment -- the cannot-read guard holds
        # even for a supplied pair (Codex, mikelward/repo#51).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = FakeGh()
            fake.workflow_files = ["ci.yml"]
            fake.workflow_texts = {
                "ci.yml": "jobs:\n  ci:\n    uses: some-org/shared/.github/workflows/ci.yml@main\n    secrets: inherit\n"
            }
            fake.secret_names = set(self.PAIR)
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
            "calls a reusable workflow this cannot read, which may be what publishes", err
        )
        self.assertIn(
            "LANES_APP_ID not set -- a reusable workflow this cannot read may forward the pair", err
        )
        # The repository copies feeding that workflow are kept, and nothing
        # is written into the environment.
        self.assertEqual(fake.deleted_secrets, [])
        self.assertEqual(fake.written_secrets, [])

    def test_a_supplied_pair_provisions_beside_a_forwarding_call_with_no_copy(self):
        # A forwarding reusable call, but the repository and environment hold
        # neither secret: there is nothing to strand, so a supplied pair is
        # placed and the environment restricted rather than held (Codex,
        # mikelward/repo#51).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = FakeGh()
            fake.workflow_files = ["ci.yml"]
            fake.workflow_texts = {
                "ci.yml": "jobs:\n  ci:\n    uses: some-org/shared/.github/workflows/ci.yml@main\n    secrets: inherit\n"
            }
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
        self.assertEqual(
            [w[:3] for w in fake.written_secrets],
            [("LANES_APP_ID", REPO, "lanes"), ("LANES_APP_PRIVATE_KEY", REPO, "lanes")],
        )
        self.assertEqual(fake.restricted, [("lanes", "main")])
        self.assertNotIn("left as is", err)
        self.assertNotIn("not set", err)

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
        code, out, err = _run(fake, ["--dry-run", "-v", REPO])
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

    def test_a_publisher_only_on_a_branch_is_not_seen_so_the_pair_is_cleaned_up(self):
        # setup reasons about the default branch alone (main-only): a publisher
        # that exists only on a feature branch is not read, so nothing on the
        # default branch uses the pair and it is cleaned up -- repository and
        # environment copies both. Recoverable: a rerun with --credential
        # re-places it once the publisher is on the default branch (maintainer:
        # main-only + delete, 2026-09-10).
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.branch_workflows = {"feature": {"ci.yml": self.PUBLISHER}}
        fake.secret_names = set(self.PAIR)
        fake.env_secret_names = {"lanes": set(self.PAIR)}
        code, out, err = _run(fake, ["--force", "--no-rules", "-v", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("nothing uses it", out + err)
        # The feature branch is never read -- setup only touches the repo and
        # its default branch.
        self.assertFalse(any("ref=feature" in " ".join(c) for c in fake.calls))
        # The environment copy is removed too, not just the repository one.
        self.assertIn(("LANES_APP_ID", "lanes"), fake.deleted_secrets)
        self.assertIn(("LANES_APP_PRIVATE_KEY", "lanes"), fake.deleted_secrets)

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

    def _ruleset_requiring(self, fake, status_checks):
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
                        "required_status_checks": status_checks,
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
                {"type": "required_linear_history", "parameters": {}},
                {"type": "non_fast_forward", "parameters": {}},
            ],
        }

    def test_a_supplied_id_binds_the_lanes_check_once_the_app_publishes(self):
        # The default branch publishes `lanes` as the App (the init step holds
        # the pair), the pair is already usable in the environment, and
        # --credential hands the id -- so the ruleset requires `lanes` FROM that
        # App, not any producer of the name.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            # Pair already placed and the environment already restricted, so the
            # credential step is idle and only the ruleset write happens.
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
            # The App has posted `lanes` (here as a check run carrying its id),
            # so requiring it from the App does not wedge the merge. It is also
            # installed and covers this repo -- the binding's coverage
            # precondition (app_covers_repo), which --force does not override.
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {12345: ("lanes-app", "all")}
            code, out, err = _run(
                fake,
                [
                    "--force",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 0, err)
        # The binding is a second ruleset write, after the credential is
        # settled -- it is the last ruleset PUT this run makes.
        checks_rule = next(
            r for r in fake.puts[-1][1]["rules"] if r["type"] == "required_status_checks"
        )
        self.assertEqual(
            checks_rule["parameters"]["required_status_checks"],
            [
                {"context": "lanes", "integration_id": 12345},
                {"context": "codex"},
                {"context": "zizmor"},
            ],
        )

    def test_a_binding_held_by_a_sibling_branch_holds_the_ruleset_step(self):
        # The main preview finds the ruleset compliant (nothing to write);
        # the binding preview would write, and meets a real master beside
        # main. That hold is the ruleset step's, not something to skip
        # silently with exit 0 forever (Codex review, mikelward/repo#56).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {12345: ("lanes-app", "all")}
            fake.sibling_exists = True
            code, out, err = _run(
                fake,
                [
                    "--force",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1)
        self.assertIn("has a branch named 'master' beside its default branch 'main'", err)
        self.assertIn("skipping the ruleset step -- held for a person", err)
        self.assertIn("failed on: ruleset", err)
        self.assertEqual(fake.puts, [])

    def test_force_does_not_bind_to_an_app_that_does_not_cover_the_repo(self):
        # The coverage precondition is NOT --force-overridable: binding `lanes`
        # to an App that is not installed on / does not cover this repo would
        # wedge every merge, and no amount of --force makes an uninstalled App
        # able to report. The documented fleet command always passes --force,
        # so this is the path that matters (Codex, mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {}  # App 12345 is not installed on the owner
            code, out, err = _run(
                fake,
                [
                    "--force",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertNotEqual(code, 0, out)
        self.assertIn("is not installed on", out + err)
        # No ruleset write bound `lanes` to the App -- it stays required but
        # unbound, --force notwithstanding.
        for _url, body in fake.puts:
            checks_rule = next(
                (r for r in body["rules"] if r["type"] == "required_status_checks"), None
            )
            if checks_rule is None:
                continue
            for entry in checks_rule["parameters"]["required_status_checks"]:
                self.assertNotIn("integration_id", entry)

    def test_a_suspended_installation_does_not_count_as_coverage(self):
        # A suspended installation keeps its `repository_selection` but cannot
        # act, so it can never publish `lanes` -- binding to it would wedge every
        # merge. The coverage precondition must treat it as not covering, exactly
        # like an absent install, rather than reading "all repositories" and
        # binding anyway (Codex, mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            # Installed "all repositories" but SUSPENDED -> cannot publish here.
            fake.app_coverage = {12345: ("lanes-app", "all", True)}
            code, out, err = _run(
                fake,
                [
                    "--force",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertNotEqual(code, 0, out)
        self.assertIn("is not installed on", out + err)
        # No ruleset write bound `lanes` to the suspended App.
        for _url, body in fake.puts:
            checks_rule = next(
                (r for r in body["rules"] if r["type"] == "required_status_checks"), None
            )
            if checks_rule is None:
                continue
            for entry in checks_rule["parameters"]["required_status_checks"]:
                self.assertNotIn("integration_id", entry)

    def test_binding_skipped_when_coverage_cannot_be_read(self):
        # A failed installations read during the coverage precondition is
        # can't-tell: skip the binding and fail, never bind on unconfirmed
        # coverage -- the same fail-closed discipline the fingerprint capture
        # keeps (Codex, mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage_fails = "gh: HTTP 500: boom\n"
            code, out, err = _run(
                fake,
                [
                    "--force",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertNotEqual(code, 0, out)
        for _url, body in fake.puts:
            checks_rule = next(
                (r for r in body["rules"] if r["type"] == "required_status_checks"), None
            )
            if checks_rule is None:
                continue
            for entry in checks_rule["parameters"]["required_status_checks"]:
                self.assertNotIn("integration_id", entry)

    def test_dry_run_flags_a_binding_to_an_app_that_does_not_cover(self):
        # --dry-run must mirror the real run's coverage precondition: if the
        # App does not cover the repo, the binding cannot be written, so the
        # dry run says so and exits nonzero rather than approving a plan the
        # real run would reject (Codex, mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {}  # App 12345 is not installed on the owner
            code, out, err = _run(
                fake,
                [
                    "--dry-run",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertNotEqual(code, 0, out)
        self.assertIn("App binding CANNOT be written", out)
        self.assertEqual(fake.puts, [])  # a dry run writes nothing

    def test_dry_run_counts_a_planned_app_install_as_coverage(self):
        # The coverage precondition is enforced AFTER the --app step, so an App
        # this run will add to the repo covers it by binding time. The dry-run
        # preview must count that planned addition, not flag a plan that would
        # succeed (Codex, mikelward/repo#52). App 12345 is installed on the
        # owner as a "selected" install NOT yet including this repo, and --app
        # will add it.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            # Installed "selected", not yet covering this repo -> app_covers_repo
            # is False now, but --app lanes-app will ADD it.
            fake.app_coverage = {12345: ("lanes-app", "selected")}
            fake.installations = [("lanes-app", "88", "selected", REPO.split("/", 1)[0])]
            fake.install_members = {"88": set()}  # repo not a member yet -> ADD
            code, out, err = _run(
                fake,
                [
                    "--dry-run",
                    "--app", "lanes-app",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        # Planned coverage counts, so the binding is NOT flagged uncovered.
        self.assertNotIn("App binding CANNOT be written", out)

    def test_dry_run_does_not_flag_coverage_for_an_already_bound_check(self):
        # Coverage is a precondition for a binding this run would WRITE, so both
        # the dry-run preview and the real run gate the check on
        # binding_needs_write. An already-bound `lanes` whose App has since lost
        # coverage is no longer a binding this run makes -- the real run writes
        # nothing and never checks coverage, so the dry-run must not flag it
        # either (they were asymmetric before). Standing drift like this is
        # `repo audit`'s to report, not `setup`'s to fail on (Codex,
        # mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            # A fully compliant ruleset -- hardened scope, and `lanes` ALREADY
            # bound to App 12345 -- so there is no ruleset write this run and
            # binding_needs_write is False (Codex's narrow case: a ruleset that
            # already matches, binding included).
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
                                {"context": "lanes", "integration_id": 12345}
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
                    {"type": "required_linear_history"},
                    {"type": "non_fast_forward"},
                ],
            }
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345)]}
            fake.app_coverage = {}  # App 12345 no longer covers the repo
            code, out, err = _run(
                fake,
                [
                    "--dry-run", "--rule", "lanes",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("App binding CANNOT be written", out)
        self.assertEqual(fake.puts, [])  # a dry run writes nothing

    def test_no_binding_when_the_pair_move_fails(self):
        # The binding is written only after the credential move settles the
        # pair. If the move fails (here the environment restriction), the check
        # must be left required but UNBOUND -- binding it to an App whose
        # credential did not land would block every merge (Codex,
        # mikelward/repo#52). A later run binds it once the pair is settled.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set(self.PAIR)  # repo-level copies, being placed this run
            fake.restrict_fails = {"lanes"}  # the pair's move cannot settle
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            code, out, err = _run(
                fake,
                [
                    "--force",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1, err)
        # No ruleset PUT this run binds lanes to the App.
        for _url, body in fake.puts:
            for rule in body.get("rules") or []:
                if rule["type"] == "required_status_checks":
                    for c in rule["parameters"]["required_status_checks"]:
                        if c["context"] == "lanes":
                            self.assertNotIn("integration_id", c)

    def test_binding_deferred_until_the_app_has_published(self):
        # The App publishes `lanes` on the default branch and the pair is in
        # place, but the App has not actually posted a `lanes` status yet (only
        # some other producer has). Binding now would require a check the App
        # has never reported, so it is deferred -- not a failure: the pair is
        # placed, `lanes` stays required (unbound), and a later run binds it.
        # This is the two-run cadence's run 1.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
            # `lanes` reports (so the unbound requirement holds), but from
            # App 7, not the 12345 the binding would name. App 12345 is
            # installed and covers this repo (so it's the deferral, not a
            # coverage failure) -- it just has not posted a `lanes` status yet.
            fake.check_runs = {fake.default_head_sha: [("lanes", 7), "codex", "zizmor"]}
            fake.app_coverage = {12345: ("lanes-app", "all")}
            code, out, err = _run(
                fake,
                [
                    "--dry-run",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        # Deferral is not a failure: the pair is placed and the check stays
        # required (unbound) until a later run binds it.
        self.assertEqual(code, 0, err)
        self.assertIn("App binding waits", out)
        self.assertEqual(fake.puts, [])

    _EFFECTIVE_LANES_BOUND_55 = [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "lanes", "integration_id": 55},
                    {"context": "codex"},
                    {"context": "zizmor"},
                ]
            },
        }
    ]

    def test_dry_run_flags_a_refused_re_point(self):
        # setup does not re-point an existing binding to a different App
        # (descope): the effective rules already bind `lanes` to App 55 and this
        # run supplies App 12345, so --dry-run says the switch is HELD and exits
        # nonzero rather than approving a switch the real run refuses (Codex
        # E/F/H, mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes", "integration_id": 55}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.effective_rules = self._EFFECTIVE_LANES_BOUND_55
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {12345: ("lanes-app", "all")}
            code, out, err = _run(
                fake,
                [
                    "--dry-run",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertNotEqual(code, 0, out)
        self.assertIn("HELD", out)
        self.assertIn("App 55", out)
        self.assertIn("does not re-point", out)
        self.assertEqual(fake.written_secrets, [])

    def test_already_bound_to_the_same_app_is_not_a_re_point(self):
        # Idempotent: when `lanes` is already bound to the SAME App this run
        # supplies (12345), it is not a re-point -- the run is not refused
        # (descope, Codex #52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes", "integration_id": 12345}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.effective_rules = [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [
                            {"context": "lanes", "integration_id": 12345},
                            {"context": "codex"},
                            {"context": "zizmor"},
                        ]
                    },
                }
            ]
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {12345: ("lanes-app", "all")}
            code, out, err = _run(
                fake,
                [
                    "--force",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 0, err)
        self.assertNotIn("does not re-point", out + err)
        self.assertNotIn("not switched", err)

    def test_a_re_point_to_a_different_app_is_refused(self):
        # setup does not re-point: `lanes` is already bound to App 55 and this
        # run supplies App 12345, so the run REFUSES -- it does not switch the
        # credential and does not bind, leaving App 55 in place. Re-point/rotate
        # is a tracked follow-up (descope; Codex E/F/H, mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes", "integration_id": 55}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.effective_rules = self._EFFECTIVE_LANES_BOUND_55
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {12345: ("lanes-app", "all")}
            with patch("builtins.input", return_value="y"):
                code, out, err = _run(
                    fake,
                    [
                        "--credential", f"LANES_APP_ID={app_id}",
                        "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                        REPO,
                    ],
                    isatty=True,
                )
        self.assertNotEqual(code, 0, out)
        self.assertIn("not switched", err)
        # The credential is NOT rotated to 12345, and lanes is NOT re-bound to it.
        written = [w[:3] for w in fake.written_secrets]
        self.assertNotIn(("LANES_APP_ID", REPO, "lanes"), written)
        for _url, body in fake.puts:
            checks_rule = next(
                (r for r in body["rules"] if r["type"] == "required_status_checks"), None
            )
            if checks_rule is None:
                continue
            for entry in checks_rule["parameters"]["required_status_checks"]:
                if entry.get("context") == "lanes":
                    self.assertNotEqual(entry.get("integration_id"), 12345)

    def test_an_unreadable_current_binding_refuses_the_switch(self):
        # If the effective-rules read fails, setup cannot tell a first bind from
        # a re-point, so it REFUSES conservatively -- it does not switch the
        # credential or bind (descope; Codex E/F/H, mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {12345: ("lanes-app", "all")}
            fake.effective_rules_read_fails = True
            with patch("builtins.input", return_value="y"):
                code, out, err = _run(
                    fake,
                    [
                        "--credential", f"LANES_APP_ID={app_id}",
                        "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                        REPO,
                    ],
                    isatty=True,
                )
        self.assertNotEqual(code, 0, out)
        self.assertIn("not switched", err)
        written = [w[:3] for w in fake.written_secrets]
        self.assertNotIn(("LANES_APP_ID", REPO, "lanes"), written)

    def test_a_re_point_appearing_only_at_apply_time_holds_the_switch(self):
        # Finding J: the plan-time refuse_repoint snapshot reads `lanes` as a
        # first bind, but between the ruleset apply and the credential move an
        # admin binds it to App 55. The apply-time recheck immediately before
        # the switch catches that transition and holds the credential move --
        # the switch is otherwise unprotected while the binding it depends on is
        # fingerprinted only against the target App, not the source (Codex J,
        # mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
            )
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
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {12345: ("lanes-app", "all")}
            calls = []

            def staged(repo, target):
                # Plan time: unbound (first bind). Apply time: App 55 appeared.
                calls.append(target)
                return (None, False) if len(calls) == 1 else (55, False)

            with patch("repo_lib.setup_cmd._lanes_repoint_state", side_effect=staged):
                with patch("builtins.input", return_value="y"):
                    code, out, err = _run(
                        fake,
                        [
                            "--credential", f"LANES_APP_ID={app_id}",
                            "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                            REPO,
                        ],
                        isatty=True,
                    )
        # The apply-time recheck must run: called at plan time and again before
        # the move.
        self.assertGreaterEqual(len(calls), 2)
        self.assertNotEqual(code, 0, out)
        self.assertIn("not switched", err)
        self.assertIn("App 55", err)
        written = [w[:3] for w in fake.written_secrets]
        self.assertNotIn(("LANES_APP_ID", REPO, "lanes"), written)

    def test_no_rules_still_refuses_a_re_pointing_credential_switch(self):
        # Finding K: --no-rules skips the ruleset writes but still runs the
        # credential move. When `lanes` is bound to App 55 and this run supplies
        # App 12345, switching the credential would leave the workflow publishing
        # as 12345 while the untouched requirement still names 55 -- wedging every
        # merge. The re-point safety is a property of the credential move, not of
        # the ruleset step, so it must hold the switch under --no-rules too (Codex
        # K, mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes", "integration_id": 55}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.effective_rules = self._EFFECTIVE_LANES_BOUND_55
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {12345: ("lanes-app", "all")}
            with patch("builtins.input", return_value="y"):
                code, out, err = _run(
                    fake,
                    [
                        "--no-rules",
                        "--credential", f"LANES_APP_ID={app_id}",
                        "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                        REPO,
                    ],
                    isatty=True,
                )
        self.assertNotEqual(code, 0, out)
        self.assertIn("not switched", err)
        self.assertIn("App 55", err)
        written = [w[:3] for w in fake.written_secrets]
        self.assertNotIn(("LANES_APP_ID", REPO, "lanes"), written)

    def test_dry_run_no_rules_flags_a_re_pointing_credential_switch(self):
        # The --no-rules re-point hold (Finding K) is previewed by --dry-run too,
        # so the dry run's exit status matches the real run rather than approving
        # a switch it would refuse.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            self._ruleset_requiring(
                fake,
                [{"context": "lanes", "integration_id": 55}, {"context": "codex"}, {"context": "zizmor"}],
            )
            fake.effective_rules = self._EFFECTIVE_LANES_BOUND_55
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            fake.app_coverage = {12345: ("lanes-app", "all")}
            code, out, err = _run(
                fake,
                [
                    "--dry-run",
                    "--no-rules",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertNotEqual(code, 0, out)
        self.assertIn("HELD", out)
        self.assertEqual(fake.written_secrets, [])

    def test_binding_skipped_when_the_fingerprint_capture_fails(self):
        # If the ruleset state can't be captured after the main apply (a
        # transient read failure), the deferred binding write must be SKIPPED,
        # not run unpinned -- an unpinned write is the race the capture exists
        # to close. It fails the run and waits for a rerun (Codex,
        # mikelward/repo#52).
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.env_secret_names = {"lanes": set(self.PAIR)}
            fake.env_policies = {"lanes": ["main"]}
            # Missing zizmor, so the main apply does a real PUT; the App (12345)
            # has published, so the binding is wanted.
            self._ruleset_requiring(fake, [{"context": "lanes"}, {"context": "codex"}])
            fake.check_runs = {fake.default_head_sha: [("lanes", 12345), "codex", "zizmor"]}
            # Ruleset reads fail after the main PUT -> the capture fails.
            fake.ruleset_read_fails_after_put = {"7"}
            code, out, err = _run(
                fake,
                [
                    "--force",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 1, err)
        self.assertIn("App binding was not written", err)
        for _url, body in fake.puts:
            for rule in body.get("rules") or []:
                if rule["type"] == "required_status_checks":
                    for c in rule["parameters"]["required_status_checks"]:
                        if c["context"] == "lanes":
                            self.assertNotIn("integration_id", c)

    def test_a_non_numeric_app_id_is_refused_not_silently_unbound(self):
        # A malformed LANES_APP_ID (a mis-pointed file, say) must fail the run,
        # not be written as the credential while the binding is silently
        # skipped (Codex, mikelward/repo#52). The pair is left as is.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"not-a-number")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher()
            fake.secret_names = set(self.PAIR)
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
        self.assertIn("not a positive integer App id", err)
        self.assertEqual(fake.written_secrets, [])

    def test_no_app_publisher_leaves_the_lanes_check_unbound(self):
        # The consumer still runs the ambient gate (classify only, no `app-id`),
        # so nothing publishes `lanes` as the App yet. The pair is placed for
        # the coming migration, but the check must stay unbound -- binding it to
        # an App that is not yet the producer would wedge every merge.
        with tempfile.TemporaryDirectory() as tmp:
            app_id = _secret_file(tmp, "id.txt", b"12345")
            key = _secret_file(tmp, "key.pem", b"-----BEGIN RSA PRIVATE KEY-----")
            fake = self._publisher(self.AMBIENT)
            fake.secret_names = set(self.PAIR)
            # Missing zizmor, so a ruleset write happens -- and it must add
            # zizmor while leaving `lanes` unbound.
            self._ruleset_requiring(fake, [{"context": "lanes"}, {"context": "codex"}])
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            code, out, err = _run(
                fake,
                [
                    "--force",
                    "--credential", f"LANES_APP_ID={app_id}",
                    "--credential", f"LANES_APP_PRIVATE_KEY={key}",
                    REPO,
                ],
            )
        self.assertEqual(code, 0, err)
        checks_rule = next(
            r for r in fake.puts[0][1]["rules"] if r["type"] == "required_status_checks"
        )
        self.assertEqual(
            checks_rule["parameters"]["required_status_checks"],
            [{"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}],
        )


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
        code, out, err = _run(fake, ["--dry-run", "-v", REPO])
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

    def test_a_dry_run_still_reports_a_credential_nothing_uses(self):
        # The sibling test above covers the real run, which prints this
        # from the Apply section. A --dry-run returns before Apply, so the
        # combined plan is the only place the line can appear -- and the
        # step has no move, so it reads idle and its section was dropped
        # (Codex review, mikelward/repo#45).
        with tempfile.TemporaryDirectory() as tmp:
            path = _secret_file(tmp, "pat.txt")
            fake = FakeGh()
            fake.workflow_files = ["ci.yml"]
            code, out, err = _run(
                fake,
                ["--dry-run", "--no-rules", "--credential", f"NPM_UPDATE_PAT={path}", REPO],
            )
        plan = out + err
        self.assertEqual(code, 0, err)
        self.assertIn(
            "npm-update: NPM_UPDATE_PAT not set -- no workflow here calls mikelward/npm-update",
            plan,
        )
        self.assertNotIn("nothing to change", plan)
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

    def test_a_caller_on_another_branch_is_not_read(self):
        # setup reads the default branch only, so a caller that exists only on
        # a feature branch is not read: it never fails the run with "passes its
        # secrets by name" (a throwaway variant on a feature branch must not
        # block a fleet-wide run), and the branch is not fetched at all (no
        # per-branch quota cost). With no caller on the default branch the
        # credential reads as unused and is cleaned up -- recoverable by a
        # rerun once the caller is on the default branch (maintainer: main-only
        # + delete, 2026-09-10). `repo audit`, which does read every branch,
        # is where such a branch is surfaced.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}
        fake.branch_workflows = {"feature": {"ci.yml": "jobs: {}\n", "weekly.yml": self.NAMING}}
        fake.secret_names = {"GRADLE_UPDATE_PAT"}
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("passes its secrets by name", out + err)
        # No branch is read.
        self.assertFalse(any("ref=feature" in " ".join(c) for c in fake.calls))
        # Unused on the default branch -> the repository and environment copies
        # are removed.
        self.assertIn(("GRADLE_UPDATE_PAT", None), fake.deleted_secrets)
        self.assertIn(("GRADLE_UPDATE_PAT", "gradle-update"), fake.deleted_secrets)

    def test_a_credential_already_in_place_is_left_alone(self):
        fake = self._consumer()
        fake.env_secret_names = {"gradle-update": {"GRADLE_UPDATE_PAT"}}
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", "-v", REPO])
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

    def test_a_default_branch_repoint_between_plan_and_recheck_holds_the_delete(self):
        # setup reads the default branch only, so a repoint between the plan
        # and the pre-delete recheck would otherwise let the recheck read the
        # FORMER default and delete a credential a caller on the NEW default
        # uses. The recheck re-reads through a consistent snapshot and refuses
        # when the default has moved since the plan, holding the delete (Codex,
        # mikelward/repo#55). The all-branch scan used to cover this
        # incidentally; default-only catches it explicitly.
        fake = FakeGh()
        fake.workflow_files = ["ci.yml"]
        fake.workflow_texts = {"ci.yml": "jobs: {}\n"}  # no caller on main -> plan deletes the PAT
        fake.secret_names = {"NPM_UPDATE_PAT"}
        # main for the plan's two name reads (snapshot: name + confirm), trunk
        # for the recheck's snapshot.
        fake.default_branch_after_bootstrap_plan = "trunk"
        fake.default_branch_renamed_after_read = 2
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("the default branch is now 'trunk', not 'main'", err)
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
        code, out, err = _run(fake, ["--dry-run", "-v", REPO])
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
        code, out, err = _run(fake, ["--dry-run", "-v", REPO])
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
        self.assertIn("open a pull request writing 7 file(s) (add missing fleet ci scaffold files):", out)
        self.assertIn("already present, untouched: 2 file(s)", out)

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42 writing 7 fleet CI scaffold file(s)", out)
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


    def test_a_scaffold_still_in_a_pull_request_defers_only_the_checks_it_publishes(self):
        # The gap-fill goes in as a pull request, so its files are NOT on
        # the branch when the ruleset step runs. The ruleset is written
        # anyway -- pull-request protection, linear history, force-push
        # protection -- with the checks the missing files publish deferred
        # by name, since requiring them would block the very pull request
        # installing them (SPEC.md, *The ladder*, rung 1).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertNotIn("skipping the ruleset step", err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(len(ruleset_posts), 1)
        self.assertEqual(ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"], [])
        self.assertEqual({r["type"] for r in ruleset_posts[0]["rules"]}, {
            "required_status_checks", "pull_request", "required_linear_history", "non_fast_forward",
        })
        self.assertIn(
            "deferred, not required yet: 'lanes' waits for .github/workflows/ci.yml to be on "
            "'main' -- the pull request this run opens is adding it",
            out,
        )
        self.assertIn("'codex' waits for .github/workflows/codex-review-check.yml", out)
        self.assertIn("'zizmor' waits for .github/workflows/zizmor.yml", out)

    def test_an_outdated_publisher_being_replaced_defers_its_check_too(self):
        # Every scaffold file is present, but the pinned copy that
        # publishes `codex` differs from the template, so the pull request
        # replaces it. A copy this tool is replacing is not one it vouches
        # for: `codex` waits for the replacement to land, however the old
        # copy reported, while `lanes` and `zizmor`, whose publishers are
        # current, are required now (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS)
        fake.bootstrap_outdated_paths = {".github/workflows/codex-review-check.yml"}
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(len(ruleset_posts), 1)
        self.assertEqual(
            [e["context"] for e in ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"]],
            ["lanes", "zizmor"],
        )
        self.assertIn(
            "'codex' waits for .github/workflows/codex-review-check.yml to be on 'main' -- "
            "the pull request this run opens is replacing it",
            out,
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


    def test_a_failed_gap_that_would_leave_a_check_unpublished_still_defers_it(self):
        # No pull request was opened, so `lanes` has nowhere to run from
        # until ci.yml lands: deferred, while `codex` and `zizmor` -- whose
        # workflows are on the branch -- are required now.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {".github/workflows/ci.yml"}
        fake.bootstrap_pull_create_fails = True
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)  # the bootstrap step did fail
        self.assertIn("failed on: bootstrap", err)
        self.assertNotIn("skipping the ruleset step", err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(
            ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"],
            [{"context": "codex"}, {"context": "zizmor"}],
        )
        self.assertIn("'lanes' waits for .github/workflows/ci.yml", out)

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


    def test_a_missing_codex_workflow_defers_only_codex(self):
        # `codex`'s publisher is among the missing files, and GitHub reads
        # it from the base branch, so no pull request -- this one included
        # -- can make it report: deferred until the scaffold merges. The
        # other two are required now.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {
            ".github/workflows/codex-review-check.yml"
        }
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(
            ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"],
            [{"context": "lanes"}, {"context": "zizmor"}],
        )
        self.assertIn("'codex' waits for .github/workflows/codex-review-check.yml", out)

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
        fake.pulls = {11: {"mergeable_state": "blocked"}}  # as GitHub reports a required check unmet
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        # Nothing runs `codex` on this head and nothing is pending: held.
        self.assertIn("needs a person", err)
        self.assertNotIn("pull request #11 waits", err)
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
        # deriving one (Codex review, mikelward/repo#42). What IS read is
        # the pull request's head, mergeability and checks -- to decide
        # whether to merge it, never what the branch holds -- and the
        # fixture has no route for its file list, so a call would raise;
        # this checks the calls themselves too, so the guarantee survives
        # a fixture that grows one back.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = [
            (27, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/27")
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #27 waits", err)
        self.assertFalse(
            [c for c in fake.calls if any("/pulls/27/files" in str(a) for a in c)],
            "the step read an open pull request's contents",
        )


    def test_a_gap_containing_a_publisher_defers_the_check_even_with_a_pull_request_open(self):
        # The gap contains `ci.yml`, so the BRANCH has no way to report
        # `lanes` -- and whether the open pull request would supply it is
        # exactly the question this step stopped asking, because no answer
        # read out of a pull request stays true (Codex review,
        # mikelward/repo#42). `lanes` waits for the merge; the rest lands.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {".github/workflows/ci.yml"}
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        fake.bootstrap_open_pulls = [
            (20, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/20")
        ]
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(
            ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"],
            [{"context": "codex"}, {"context": "zizmor"}],
        )
        self.assertIn(
            "'lanes' waits for .github/workflows/ci.yml to be on 'main' -- pull request #20 is "
            "adding it",
            out,
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
        # The one shape deferral cannot express: the checks are already in
        # the ruleset, so leaving them alone keeps them, and the widening
        # makes them required on a branch that cannot publish them.
        # Dropping them would loosen `release`. Refused.
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 1)
        self.assertIn("widening it onto 'main' would enforce there what nothing here can tell 'main' can satisfy: it requires 'codex', 'lanes', 'zizmor'", err)

        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("widening it onto 'main'", err)
        self.assertIn("Widen the ruleset by hand", err)
        self.assertIn("skipping the ruleset step -- held for a person", err)
        self.assertIn("failed on: ruleset", err)
        self.assertEqual(fake.puts, [])
        # Held for a person, and only that step: the scaffold pull request
        # still opens, and the settings still land (Codex review,
        # mikelward/repo#56).
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertEqual(len(fake.created_pulls), 1)

        # A glob in the include list is not evaluated, so it does not count
        # as covering the branch: `refs/heads/release/*` misses `main`, and
        # reading any glob as coverage would have let this widening wedge
        # every merge (Codex review, mikelward/repo#56).
        fake.ruleset_objects["7"]["conditions"]["ref_name"]["include"] = ["refs/heads/release/*"]
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("widening it onto 'main'", err)
        self.assertEqual(fake.puts, [])

        # And evidence does not lift it: every check has passed on this
        # repository, but a check's history here is not a history on THIS
        # branch (a success on a release pull request says nothing about
        # main), and three review rounds each found the previous reading
        # one case short -- so a ruleset that carries any check at all is
        # never widened onto the branch by this tool (Codex review,
        # mikelward/repo#56).
        fake.bootstrap_existing_paths = None  # scaffold complete: nothing deferred
        fake.ruleset_objects["7"]["rules"][0]["parameters"]["required_status_checks"] = [
            {"context": "lanes"}, {"context": "codex"}, {"context": "zizmor"}, {"context": "release-only"},
        ]
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor", "release-only"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("it requires 'codex', 'lanes', 'release-only', 'zizmor'", err)
        self.assertIn("Widen the ruleset by hand", err)
        self.assertEqual(fake.puts, [])

        # Nor is a check the only thing that can block the tool's own pull
        # requests on the branch: an approval requirement, or a rule type
        # this tool does not write, refuses the widening the same way.
        fake.ruleset_objects["7"]["rules"][0]["parameters"]["required_status_checks"] = []
        fake.check_runs = {fake.default_head_sha: []}
        fake.ruleset_objects["7"]["rules"][1]["parameters"]["required_approving_review_count"] = 1
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("it requires 1 approving review(s)", err)
        self.assertEqual(fake.puts, [])
        fake.ruleset_objects["7"]["rules"][1]["parameters"]["required_approving_review_count"] = 0
        fake.ruleset_objects["7"]["rules"].append({"type": "required_signatures"})
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("it carries a 'required_signatures' rule this tool does not manage", err)
        self.assertEqual(fake.puts, [])

        # A ruleset carrying nothing of the kind is widened: nothing on it
        # can wedge.
        fake.ruleset_objects["7"]["rules"].pop()
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        self.assertEqual(fake.puts[0][1]["rules"][0]["parameters"]["required_status_checks"], [])

        # And the widening write adds no check of its own either, whatever
        # has passed on the repository: a success on a release pull request
        # is not a history on main, so the checks this write would newly
        # require wait for the run after, once the ruleset covers the
        # branch (Codex review, mikelward/repo#56).
        fake.puts.clear()
        fake.ruleset_objects["7"]["conditions"]["ref_name"]["include"] = ["refs/heads/release"]
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor", "release-scan"]}
        code, out, err = _run(fake, ["--force", "--rule", "release-scan", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.puts), 1)
        self.assertEqual(fake.puts[0][1]["rules"][0]["parameters"]["required_status_checks"], [])
        self.assertIn(
            "'release-scan' waits until ruleset 'main' covers 'main' -- this run widens it onto "
            "the branch",
            out,
        )
        self.assertIn("required checks: none yet", out)

    def test_a_scaffold_pull_request_defers_the_checks_a_ruleset_would_add(self):
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
        # The dry run previews the same deferral and the same exit status.
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("'codex' waits for .github/workflows/codex-review-check.yml", out)
        self.assertIn("'zizmor' waits for .github/workflows/zizmor.yml", out)
        self.assertNotIn("would newly require", out)

        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertNotIn("skipping the ruleset step", err)
        self.assertEqual(len(fake.puts), 1)  # linear history and force-push protection land now
        self.assertEqual(
            fake.puts[0][1]["rules"][0]["parameters"]["required_status_checks"],
            [{"context": "lanes"}],
        )


    def test_a_never_reported_repository_gets_bootstrapped_and_a_ruleset_with_every_check_deferred(self):
        # The exact case this exists for: a fresh (or never-fully-
        # scaffolded) repository, `repo setup OWNER/REPO` with no flags at
        # all. Its required checks have never run -- there's been no CI to
        # run them -- so none can be required yet. Everything else lands:
        # the scaffold pull request, and the ruleset with pull-request
        # protection and no checks, each deferred one named (SPEC.md,
        # *The ladder*, rung 1).
        fake = FakeGh()
        fake.bootstrap_existing_paths = {".github/zizmor.yml"}  # real gaps to fill
        # fake.check_runs defaults to {} -- nothing has ever reported.
        with patch("builtins.input", return_value="y"):
            code, out, err = _run(fake, [REPO], isatty=True)
        self.assertEqual(code, 0, err)
        self.assertNotIn("skipping the ruleset step", err)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        blob_posts = [body for _args, body in fake.posts if "encoding" in body]
        self.assertTrue(blob_posts, "no scaffold blobs were written")
        self.assertEqual(len(fake.created_pulls), 1)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(len(ruleset_posts), 1)
        self.assertEqual(ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"], [])
        self.assertIn("'lanes' waits for .github/workflows/ci.yml", out)
        self.assertIn("'codex' waits for .github/workflows/codex-review-check.yml", out)
        self.assertIn("'zizmor' waits for .github/workflows/zizmor.yml", out)

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


    def test_a_scope_blocked_scaffold_previews_the_same_deferrals(self):
        # A missing `workflow` scope leaves the scaffold off the branch
        # just as surely as a pending pull request does, so the checks it
        # would install are deferred with that reason -- in the preview and
        # in the run alike (Codex review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.token_scopes = ("gist", "read:org", "repo")
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 1)  # the bootstrap step is skipped, and that is a failure
        self.assertIn(
            "'lanes' waits for .github/workflows/ci.yml to be on 'main' -- this gh token cannot "
            "write it",
            out,
        )

        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("skipping the bootstrap step", err)
        self.assertNotIn("skipping the ruleset step", err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"], [])
        self.assertIn("this gh token cannot write it", out)

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


    def test_a_bootstrap_failure_defers_every_check_but_still_creates_the_ruleset(self):
        # A plan that failed cannot say what is missing, so no check may be
        # newly required on its account -- but pull-request protection,
        # linear history and force-push protection block nothing a later
        # run cannot fix, and they land now (SPEC.md, invariant 1).
        fake = FakeGh()
        fake.template_fetch_fails = {"codex-review.yml"}  # a plan-time bootstrap failure
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("failed on: bootstrap", err)
        self.assertNotIn("skipping the ruleset step", err)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(len(ruleset_posts), 1)
        self.assertEqual(ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"], [])
        self.assertIn("'lanes' waits until the fleet CI scaffold can be planned", out)
        self.assertEqual([b for a, b in fake.posts if "encoding" in b], [])  # no scaffold blobs

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
        failed_line = next(line for line in err.splitlines() if line.startswith("error: failed on:"))
        self.assertNotIn("ruleset", failed_line)


    def test_a_bootstrap_failure_does_not_block_an_update_that_newly_adds_pull_request_protection(self):
        # The ruleset only carries required_linear_history/non_fast_forward
        # -- no pull_request rule yet. Pull-request protection does not
        # block the scaffold's own pull request (a pull request is what it
        # is), so the update lands; `lanes`, which the plan failure leaves
        # unaccounted for, is deferred rather than newly required.
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
        self.assertNotIn("skipping the ruleset step", err)
        failed_line = next(line for line in err.splitlines() if line.startswith("error: failed on:"))
        self.assertNotIn("ruleset", failed_line)
        self.assertEqual(len(fake.puts), 1)
        self.assertEqual(fake.puts[0][1]["rules"][-2]["parameters"]["required_status_checks"], [])
        self.assertIn("'lanes' waits until the fleet CI scaffold can be planned", out)

    def test_pull_request_protection_re_added_during_the_wait_is_written_when_it_strands_nothing(self):
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

        # Nothing here strands the branch: `lanes` stays required exactly as
        # it was, and pull-request protection blocks no pull request. The
        # confirmed body is what gets written.
        code, out, err = _run(fake, ["--force", "--rule", "lanes", REPO])
        self.assertEqual(code, 1)  # the bootstrap plan failure, and only that
        failed_line = next(line for line in err.splitlines() if line.startswith("error: failed on:"))
        self.assertNotIn("ruleset", failed_line)
        self.assertEqual(len(fake.puts), 1)
        self.assertIn("pull_request", {r["type"] for r in fake.puts[0][1]["rules"]})

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

    def test_a_failed_bootstrap_plan_on_an_empty_branch_skips_the_ruleset_step(self):
        # The template fetch fails before the plan reads whether the branch
        # has any commits, so the plan cannot say -- and a ruleset that
        # first requires a pull request on an empty branch strands it just
        # as --no-bootstrap would (Codex review, mikelward/repo#56). Read
        # here instead, previewed the same by --dry-run.
        for flags in (["--dry-run"], ["--force"]):
            fake = FakeGh()
            fake.bootstrap_ref_missing = True
            fake.template_fetch_fails = {"codex-review.yml"}
            code, out, err = _run(fake, flags + [REPO])
            self.assertEqual(code, 1, err)
            self.assertIn("could not be planned", out + err)
            self.assertIn("branch has no commits yet", out + err)
            self.assertEqual([a for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"], [])
        self.assertIn("skipping the ruleset step", err)

    def test_a_bootstrap_that_left_the_branch_empty_skips_the_ruleset_step(self):
        # The plan read an empty branch and this run was to fill it first;
        # the write failed, so the branch is as empty as it was and the
        # ruleset write behind it is refused the same way (Codex review,
        # mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_ref_missing = True
        fake.bootstrap_contents_put_fails = True
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 1)
        self.assertIn("skipping the ruleset step", err)
        self.assertIn("this run's bootstrap step did not add one", err)
        self.assertIn("failed on: bootstrap ruleset", err)
        self.assertEqual([a for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"], [])

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


    def test_dry_run_previews_the_deferrals_accurately(self):
        # --dry-run must show the same deferrals and exit status the real
        # run would, not report the ruleset as creatable with every check.
        fake = FakeGh()
        fake.bootstrap_existing_paths = {".github/zizmor.yml"}
        # fake.check_runs defaults to {} -- nothing has ever reported.
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("deferred, not required yet", out)
        # The publisher's absence is the reason that gets said: it holds
        # whatever the check has reported meanwhile.
        self.assertIn("'lanes' waits for .github/workflows/ci.yml", out)
        self.assertIn("- add .github/workflows/ci.yml", out)  # bootstrap's own plan still shown
        self.assertEqual(fake.posts, [])  # dry run: no writes at all regardless

    def test_a_check_removed_during_the_wait_is_not_silently_re_added(self):
        # The preview sees all three checks already required -- so the
        # deferral for the pending scaffold leaves them as they are -- but
        # a write still needed for the linear-history rule; an
        # administrator then removes `codex` during the confirmation wait.
        # The fresh recompute now DEFERS `codex` (it is no longer required,
        # and its publisher is still inside the scaffold pull request), so
        # the body differs from the one confirmed and the fingerprint
        # refuses -- the write would otherwise have re-added a check
        # nothing can report (Codex review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()  # every publisher is still in the pull request
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
        self.assertIn("no longer matches what was previewed", err)
        self.assertEqual(fake.puts, [])


    def test_an_open_pull_request_that_only_waits_is_not_something_to_confirm(self):
        # With a scaffold pull request already open and nothing to do to it
        # yet, the bootstrap step has no write to ask about: the plan says
        # what it waits on and the run moves on (Codex review,
        # mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = [
            (18, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/18")
        ]
        code, out, err = _run(fake, ["--no-rules", REPO])  # no --force, stdin is not a terminal
        self.assertEqual(code, 0, err)
        self.assertNotIn("stdin is not a terminal", err)
        self.assertIn("pull request #18 waits: 'lanes', 'zizmor' has not succeeded as a run of its own workflow from it yet", err)
        self.assertEqual(fake.puts, [])
        self.assertEqual(fake.created_pulls, [])


    def test_dry_run_previews_the_pending_scaffold_deferrals_accurately(self):
        # With files missing and the checks already reporting, --dry-run
        # shows the ruleset as creatable WITHOUT the checks the scaffold
        # pull request is still adding, exactly as the real run writes it
        # (Codex review, mikelward/repo#42).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 0, err)
        self.assertNotIn("SKIPPED", out)
        self.assertIn("'lanes' waits for .github/workflows/ci.yml", out)
        self.assertIn("open a pull request writing 9 file(s)", out)  # bootstrap's own plan still shown
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
        self.assertIn("pull request #11 is carrying the scaffold", out)
        self.assertIn("pull request #11 waits: 'lanes', 'zizmor' has not succeeded as a run of its own workflow from it yet", out)
        self.assertIn("still absent from the default branch: 9 file(s)", out)
        self.assertNotIn("open a pull request writing", out)

        code, out, err = _run(fake, ["--no-rules", REPO])  # no --force, no terminal
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #11 waits: 'lanes', 'zizmor' has not succeeded as a run of its own workflow from it yet", err)
        self.assertIn("a later run looks again", err)
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
        self.assertIn("open a pull request writing 9 file(s)", err)  # what the plan showed
        self.assertIn("pull request #12 waits", err)
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
        listings = [c for c in fake.calls if any(a.endswith("/pulls?state=open&per_page=100") for a in c)]
        self.assertTrue(listings, "the open-pull-request listing was never made")
        self.assertIn(".head.repo.id == .base.repo.id", listings[0][listings[0].index("--jq") + 1])
        # And no base filter: a retargeted pull request of this tool's own
        # has to be found to be replaced (Codex review, mikelward/repo#56).
        self.assertFalse([c for c in fake.calls if any("&base=" in a for a in c)])

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
        self.assertIn("pull request #15 is carrying the scaffold", out)
        self.assertNotIn("workflow", err)

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #15 waits", err)
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
            any("/pulls?state=open" in c[1] for c in fake.calls if c[0] == "api" and len(c) > 1),
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

    def test_a_gap_branch_holding_something_else_is_left_and_the_next_name_taken(self):
        # The other side of the same check: this module never overwrites
        # what is already there, a ref included -- a branch of an earlier
        # pull request of this tool's that somebody pushed to is left as
        # it is, and the replacement takes the next name, so it can
        # always open (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        taken = f"{scaffold.GAP_BRANCH_PREFIX}-newscaf"
        fake.bootstrap_occupied_gap_refs = {taken: "somebody-elses-commit"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42", out)
        self.assertEqual([p["head"] for p in fake.created_pulls], [f"{taken}-2"])
        self.assertEqual([r["ref"] for r in fake.created_refs], [f"refs/heads/{taken}-2"])
        # The occupied ref was not touched.
        self.assertFalse([p for p in fake.patches if "git/refs" in p[0][3]])

    def test_every_gap_branch_name_occupied_is_refused_not_moved(self):
        # Five names all holding other commits is not a leftover; said,
        # and nothing force-moved.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_ref_create_fails = True
        fake.bootstrap_gap_ref_sha = "somebody-elses-commit"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("all exist and hold other commits", err)
        self.assertIn(f"'{scaffold.GAP_BRANCH_PREFIX}-newscaf-5'", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.created_pulls, [])

    def test_a_gap_branch_create_failure_that_is_not_an_occupied_ref_is_reported(self):
        # A ref POST failing with nothing there under the name is a real
        # failure, reported as such, not a name to skip.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_ref_create_fails = True
        fake.bootstrap_gap_ref_sha = None
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

    _OPEN_11 = [(11, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/11")]

    def test_a_complete_scaffold_closes_a_leftover_pull_request_of_its_own(self):
        # Every scaffold file is on the branch, and a pull request of this
        # tool's own is still open -- a concurrent run opened it after the
        # merging run took its look, or the files landed by hand. Looked
        # for on every run, complete branch included, and closed: left
        # alone it would be open forever (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_open_pulls = self._OPEN_11
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("close pull request #11: this tool's own, and the scaffold is complete", out)
        self.assertEqual(fake.closed_pulls, [])

        # A write, agreed to like any other: not confirmed, not made.
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(fake.closed_pulls, [])
        with patch("builtins.input", return_value="n"):
            code, out, err = _run(fake, ["--no-rules", REPO], isatty=True)
        self.assertEqual(code, 1)
        self.assertIn("not confirmed", err)
        self.assertEqual(fake.closed_pulls, [])

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.closed_pulls, [11])
        self.assertIn("closed pull request #11 -- this tool's own, and the scaffold is complete on 'main'", out + err)
        self.assertEqual(fake.created_pulls, [])
        self.assertEqual(fake.merged_pulls, [])

    def test_a_second_scaffold_pull_request_from_an_overlapping_run_is_closed_first(self):
        # Two runs overlapping each found none open and opened their own.
        # The lower-numbered one is acted on (every run picks the same);
        # the other is closed first, since a scaffold that is complete
        # afterwards never looks for it again (Codex review,
        # mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = [
            (12, "repo-setup/fleet-ci-scaffold-def5678", True, "https://github.com/owner/repo/pull/12"),
            *self._OPEN_11,
        ]
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("merge pull request #11: 'lanes', 'zizmor' passed on it; close #12 first", out)
        self.assertEqual(fake.closed_pulls, [])

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.closed_pulls, [12])
        self.assertEqual([n for n, _ in fake.merged_pulls], [11])

    def test_closing_a_duplicate_beside_a_waiting_pull_request_is_confirmed_too(self):
        # The one acted on waits (a check still running), so nothing else
        # here writes -- but the duplicate beside it is closed, and that
        # is a write to agree to like any other (Codex review,
        # mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = [
            (12, "repo-setup/fleet-ci-scaffold-def5678", True, "https://github.com/owner/repo/pull/12"),
            *self._OPEN_11,
        ]
        fake.check_runs = {"prhead11": ["lanes", ("zizmor", None, None)]}
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(fake.closed_pulls, [])
        with patch("builtins.input", return_value="n"):
            code, out, err = _run(fake, ["--no-rules", REPO], isatty=True)
        self.assertEqual(code, 1)
        self.assertIn("not confirmed", err)
        self.assertEqual(fake.closed_pulls, [])

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.closed_pulls, [12])
        self.assertIn("pull request #11 waits: 'zizmor' still running on it", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_duplicate_that_appeared_after_the_plan_is_left_for_a_later_run(self):
        # The plan saw one pull request, waiting; by the time this run
        # acts, an overlapping run has opened a second. Closing it was
        # never shown or agreed to, so it is left, said, for a later run
        # whose plan carries it (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.bootstrap_open_pulls_later = [
            *self._OPEN_11,
            (12, "repo-setup/fleet-ci-scaffold-def5678", True, "https://github.com/owner/repo/pull/12"),
        ]
        fake.check_runs = {"prhead11": ["lanes", ("zizmor", None, None)]}
        code, out, err = _run(fake, ["--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #12 -- a second scaffold pull request this tool opened -- appeared after this run planned", err)
        self.assertEqual(fake.closed_pulls, [])
        self.assertEqual(fake.merged_pulls, [])

    def test_a_lower_numbered_pull_request_that_appeared_after_the_plan_fails_the_step(self):
        # The one to act on is the lowest-numbered, and a lower one
        # appeared since the plan: this run planned against another, so
        # nothing is done to either and a rerun plans afresh.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.bootstrap_open_pulls_later = [
            (10, "repo-setup/fleet-ci-scaffold-0123456", True, "https://github.com/owner/repo/pull/10"),
            *self._OPEN_11,
        ]
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("pull request #10 is this tool's own and open now, where this run planned against #11", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.closed_pulls, [])
        self.assertEqual(fake.merged_pulls, [])

    def test_a_scaffold_pull_request_whose_checks_passed_is_merged_by_a_later_run(self):
        # SPEC.md, *The ladder*, rung 2: the run finds its own pull request
        # green and mergeable, and merges it -- rebase, with the head sha it
        # read as the merge's precondition.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", ("zizmor", None, "skipped")]}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("merge pull request #11: 'lanes', 'zizmor' passed on it", out)

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: merged pull request #11", out)
        self.assertIn("is on 'main' now", out)
        self.assertEqual(fake.merged_pulls, [(11, {"merge_method": "rebase", "sha": "prhead11"})])
        self.assertEqual(fake.created_pulls, [])
        # What was read to decide: the pull request, its head's check runs
        # and statuses -- never its file list.
        self.assertFalse([c for c in fake.calls if any("/files" in str(a) for a in c)])
        # delete-branch-on-merge was already on, so nothing was written for
        # it before the merge.
        self.assertFalse([b for a, b in fake.patches if "delete_branch_on_merge" in b])

    def test_delete_branch_on_merge_is_turned_on_before_merging_the_scaffold(self):
        # The settings step enables it later in the same run -- too late
        # for this merge, which GitHub sweeps only if the setting is on
        # when it happens. So it is turned on first, and the merged branch
        # goes with the merge itself rather than by a separate delete that
        # would race a push to the branch (Codex review, mikelward/repo#56,
        # three rounds).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.delete_branch_on_merge = "false"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: merged pull request #11", out)
        self.assertEqual(fake.delete_branch_on_merge, "true")
        order = [
            "patch" if "PATCH" in c and c[3] == f"repos/{REPO}" else "merge"
            for c in fake.calls
            if ("PATCH" in c and c[3] == f"repos/{REPO}") or any("/pulls/11/merge" in str(a) for a in c)
        ]
        self.assertEqual(order[:2], ["patch", "merge"], order)

        # On the plan's word only, never a read of its own: the settings
        # step's read failed, so nothing planned turning it on, and the
        # merge leaves it alone (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.fail_delete_branch_on_merge = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertIn(f"{REPO}: merged pull request #11", out)
        self.assertIn("could not read whether owner/repo deletes branches on merge", err)
        self.assertFalse([b for a, b in fake.patches if "delete_branch_on_merge" in b])

        # Read as on at plan time: the merge makes no read of its own --
        # an administrator turning it off meanwhile is a change the plan
        # never showed, not one to undo here -- and no write.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.delete_branch_on_merge = "true"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: merged pull request #11", out)
        self.assertFalse([b for a, b in fake.patches if "delete_branch_on_merge" in b])
        reads = [c for c in fake.calls if c[-1] == ".delete_branch_on_merge"]
        self.assertEqual(len(reads), 1, reads)  # the settings step's own, at plan time

        # A failure to turn it on is said, not fatal: the merge stands and
        # `repo cleanup` sweeps the branch.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.delete_branch_on_merge = "false"
        fake.patch_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertIn(f"{REPO}: merged pull request #11", out)
        self.assertIn("could not enable delete-branch-on-merge before merging pull request #11", err)
        self.assertEqual(len(fake.merged_pulls), 1)

    def test_merging_is_a_write_that_needs_confirming(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        code, out, err = _run(fake, ["--no-rules", REPO])  # no --force, stdin is not a terminal
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertIn("merge pull request #11", err)  # the plan it would have asked about
        self.assertEqual(fake.merged_pulls, [])

    def test_a_merged_scaffold_still_defers_its_checks_this_run(self):
        # The merge puts the files on the branch, but this run's answer to
        # "which checks can the branch publish" was read before it, from
        # the tree it compared against -- and what the pull request
        # contained is not something this run reads. Deferred once more;
        # the next run reads the branch as it is and requires them.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {".github/workflows/ci.yml"}
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {fake.default_head_sha: ["codex", "zizmor"], "prhead11": ["lanes"]}
        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: merged pull request #11", out)
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(
            ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"],
            [{"context": "codex"}, {"context": "zizmor"}],
        )
        self.assertIn("'lanes' waits for .github/workflows/ci.yml", out)

    def test_a_pull_request_that_nothing_will_ever_report_on_needs_a_person(self):
        # With Actions disabled, the scaffold's checks will never report on
        # the pull request, so waiting would repeat forever with exit 0
        # (Codex review, mikelward/repo#56): held for a person instead --
        # whether nothing has reported, or only something unrelated has
        # (an external status), which must not read as mergeable.
        for statuses in ({}, {"prhead11": ["external-scan"]}):
            fake = FakeGh()
            fake.bootstrap_existing_paths = set()
            fake.bootstrap_open_pulls = self._OPEN_11
            fake.actions_enabled = False
            fake.statuses = statuses
            code, out, err = _run(fake, ["--force", "--no-rules", REPO])
            self.assertEqual(code, 1)
            self.assertIn("needs a person: GitHub Actions is disabled on this repository", err)
            self.assertIn("failed on: bootstrap", err)
            self.assertEqual(fake.merged_pulls, [])

        # But a gap that adds no workflow to run from the head -- AGENTS.md
        # alone -- has nothing to wait for, and merges on GitHub's word
        # whether or not Actions is enabled (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {"AGENTS.md", "CLAUDE.md"}
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.actions_enabled = False
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: merged pull request #11", out)
        self.assertFalse([c for c in fake.calls if any("actions/permissions" in str(a) for a in c)])

    def test_an_unreadable_actions_setting_fails_the_step_rather_than_merging(self):
        # "Could not tell" is not "enabled", and not a wait either: a wait
        # exits 0 on every run for as long as a token cannot read this
        # (Codex review, mikelward/repo#56, twice).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.actions_permissions_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not read whether Actions is enabled on owner/repo", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_unreadable_branch_rules_on_a_blocked_pull_request_fail_the_step(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules_read_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("GitHub reports pull request #11 blocked, and what 'main' requires could not be read", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_an_unrelated_passing_check_does_not_stand_in_for_the_scaffolds_own(self):
        # The pull request adds ci.yml and zizmor.yml; an external status
        # passed on its head before Actions registered them. "Something
        # passed" is not "the scaffold ran": it waits for `lanes` and
        # `zizmor` (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.statuses = {"prhead11": ["external-scan"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "pull request #11 waits: 'lanes', 'zizmor' has not succeeded as a run of its own "
            "workflow from it yet",
            err,
        )
        self.assertEqual(fake.merged_pulls, [])

        # Nor does a report merely NAMED `lanes`: a status anyone with
        # write access can post, or another App's check run. What counts
        # is a successful Actions run of the workflow the pull request
        # adds, by path (Codex review, mikelward/repo#56).
        for reports in (
            {"statuses": {"prhead11": ["lanes", "zizmor"]}},
            {"check_runs": {"prhead11": [("lanes", 7), ("zizmor", 7)]}},
        ):
            fake = FakeGh()
            fake.bootstrap_existing_paths = set()
            fake.bootstrap_open_pulls = self._OPEN_11
            for attr, value in reports.items():
                setattr(fake, attr, value)
            fake.workflow_runs = {"prhead11": [(".github/workflows/zizmor.yml", "completed", "success")]}
            code, out, err = _run(fake, ["--force", "--no-rules", REPO])
            self.assertEqual(code, 0, err)
            self.assertIn("'lanes' has not succeeded as a run of its own workflow from it yet", err)
            self.assertEqual(fake.merged_pulls, [])

        # A run still going is not a success either: a wait.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.workflow_runs = {
            "prhead11": [
                (".github/workflows/ci.yml", "in_progress", None),
                (".github/workflows/zizmor.yml", "completed", "success"),
            ]
        }
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("'lanes' has not succeeded as a run of its own workflow", err)
        self.assertEqual(fake.merged_pulls, [])

        # A run that completed without success -- a failure, a timeout, a
        # startup failure that left no failed check run -- is held for a
        # person: waiting changes nothing (Codex review, mikelward/repo#56).
        # A re-run supersedes the attempt it re-ran, newest first.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.workflow_runs = {
            "prhead11": [
                (".github/workflows/ci.yml", "completed", "success"),
                (".github/workflows/ci.yml", "completed", "failure"),  # the older attempt
                (".github/workflows/zizmor.yml", "completed", "startup_failure"),
            ]
        }
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("needs a person: 'zizmor''s own workflow ran from it and did not succeed", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.merged_pulls, [])

        # An unreadable run listing fails the step rather than guessing.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.workflow_runs_read_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not read the workflow runs from prhead1", err)
        self.assertEqual(fake.merged_pulls, [])

        # A gap with no head-published workflow in it expects nothing
        # beyond what reported -- and nothing reporting at all is not a
        # wait either: a gap of only AGENTS.md on a repository whose own
        # ci.yml does not run for that diff would otherwise wait forever
        # (Codex review, mikelward/repo#56). GitHub's mergeability decides.
        for check_runs in ({"prhead11": ["lanes", "zizmor"]}, {}):
            fake = FakeGh()
            fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {"AGENTS.md", "CLAUDE.md"}
            fake.bootstrap_open_pulls = self._OPEN_11
            fake.check_runs = check_runs
            code, out, err = _run(fake, ["--force", "--no-rules", REPO])
            self.assertEqual(code, 0, err)
            self.assertEqual(len(fake.merged_pulls), 1)
        self.assertIn("merged pull request #11", out)

    def test_a_pull_request_with_a_check_still_running_waits(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", ("zizmor", None, None)]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #11 waits: 'zizmor' still running on it", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_pending_status_on_the_head_waits_too(self):
        # `codex` is a commit status, not a check run, and a pending one is
        # the sweep still reading.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.statuses = {"prhead11": [("codex", "pending")]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("'codex' still running on it", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_pull_request_with_a_failed_check_needs_a_person_and_fails_the_run(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", ("zizmor", None, "failure")]}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("pull request #11 needs a person: 'zizmor' failed on it", out)

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("pull request #11 needs a person: 'zizmor' failed on it", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_conflicting_or_draft_pull_request_needs_a_person(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable": False, "mergeable_state": "dirty"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("it conflicts with its base branch", err)
        self.assertEqual(fake.merged_pulls, [])

        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"draft": True}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("converted to a draft", err)
        self.assertEqual(fake.merged_pulls, [])

        # A draft that is also stale is replaced, not held: marking it
        # ready would only have the next run replace it (Codex review,
        # mikelward/repo#56).
        for staleness in ({"base_ref": "release"}, {}):
            fake = FakeGh()
            fake.bootstrap_existing_paths = set()
            fake.bootstrap_open_pulls = self._OPEN_11
            fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
            fake.pulls = {11: {"draft": True, **staleness}}
            if not staleness:
                fake.pull_head_parents = {11: ["a-pushed-commit"]}
            code, out, err = _run(fake, ["--force", "--no-rules", REPO])
            self.assertEqual(code, 0, err)
            self.assertIn("in place of the stale #11", out)
            self.assertNotIn("converted to a draft", err)
            self.assertEqual(fake.closed_pulls, [11])

    def test_a_pull_request_whose_head_is_not_the_generated_commit_is_replaced(self):
        # Provenance by content, not by name (Codex review,
        # mikelward/repo#56): a head that is not exactly the base plus the
        # generated changes -- here, a file pushed onto the branch -- is
        # never merged, whatever its checks say. It is stale, so the run
        # closes it and opens a fresh one. A base that moved and a template
        # that moved upstream read the same way.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pull_head_extra_paths = {11: {"src/pushed.py"}}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("close pull request #11 and open a fresh one: its head is not the commit", out)
        self.assertIn("e.g. src/pushed.py", out)

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.closed_pulls, [11])
        self.assertEqual(fake.merged_pulls, [])
        self.assertEqual(len(fake.created_pulls), 1)
        self.assertIn(f"{REPO}: opened pull request #42 in place of the stale #11, now closed,", out)

    def test_a_head_with_the_right_tree_but_more_history_is_stale(self):
        # A push and its revert leave the expected tree, and a rebase merge
        # would replay both commits onto the branch -- so the head has to
        # be the one generated commit on top of the tip, not merely reach
        # the right tree (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pull_head_parents = {11: ["a-pushed-commit"]}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("close pull request #11 and open a fresh one: its head is not a single commit", out)

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("in place of the stale #11", out)
        self.assertEqual(fake.closed_pulls, [11])
        self.assertEqual(fake.merged_pulls, [])

    def test_a_pull_request_retargeted_off_the_default_branch_is_stale(self):
        # Listed against 'main', retargeted since: the merge lands wherever
        # the base points now, and the head sha pins only the head (Codex
        # review, mikelward/repo#56). The base is read fresh with the head
        # right before the merge, and a moved one is replaced, not merged.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"base_ref": "release"}}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("close pull request #11 and open a fresh one: it now targets 'release', not 'main'", out)

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("in place of the stale #11", out)
        self.assertEqual(fake.closed_pulls, [11])
        self.assertEqual(fake.merged_pulls, [])

    def test_a_retargeted_pull_request_is_still_found_and_replaced(self):
        # Listed without a base filter, so one retargeted before this run
        # is found rather than left open beside a second one; found, its
        # base reads wrong and it is replaced (Codex review,
        # mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"base_ref": "release"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("in place of the stale #11", out)
        self.assertEqual(fake.closed_pulls, [11])
        self.assertEqual(len(fake.created_pulls), 1)
        listing = next(c for c in fake.calls if any(a.endswith("/pulls?state=open&per_page=100") for a in c))
        self.assertFalse(any("base=" in a for a in listing))

    def test_a_base_retargeted_during_the_wait_is_not_merged(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls_later = {11: {"base_ref": "release"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #11 is not what this run planned against", err)
        self.assertEqual(fake.merged_pulls, [])
        self.assertEqual(fake.closed_pulls, [])

    def test_a_head_with_the_right_tree_but_another_message_is_stale(self):
        # The rebase merge replays the message onto the branch as surely as
        # the tree, and the tree check cannot see it: a replaced commit with
        # the generated tree and someone's own message (user data, say) is
        # stale and replaced, not merged (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pull_head_messages = {11: "Add the scaffold\n\nTOKEN=abc1234"}
        code, out, err = _run(fake, ["--dry-run", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "close pull request #11 and open a fresh one: its head's commit message is not the "
            "one this run would generate",
            out,
        )

        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("in place of the stale #11", out)
        self.assertEqual(fake.closed_pulls, [11])
        self.assertEqual(fake.merged_pulls, [])

    def test_a_pull_request_by_someone_else_under_the_prefix_is_not_this_tools(self):
        # The branch prefix is a name anyone with push access can use, and
        # a later run merges what it finds under it -- so only a pull
        # request this token's user opened counts (Codex review,
        # mikelward/repo#56). Another author's is said and left alone, and
        # this tool opens its own beside it.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = [
            (11, "repo-setup/fleet-ci-scaffold-abc1234", True, "https://github.com/owner/repo/pull/11", "someone-else")
        ]
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #11 carries this tool's branch prefix but was opened by someone-else, not owner", err)
        self.assertEqual(fake.merged_pulls, [])
        self.assertEqual(fake.closed_pulls, [])
        self.assertIn(f"{REPO}: opened pull request #42", out)

    def test_an_unreadable_head_tree_fails_the_step_rather_than_merging(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.bootstrap_commit_read_fails = True  # the head commit read, after the plan's own
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)  # plan_gaps's own commit read fails first here
        self.assertEqual(fake.merged_pulls, [])

    def test_a_blocked_pull_request_is_held_on_a_required_check_with_nothing_running(self):
        # Every check this run can see passed, nothing is running, yet
        # GitHub calls it blocked: `codex` is required and nothing has
        # posted it on this head -- not even the sweep's pending status,
        # which would be a wait (test_a_pending_status_on_the_head_waits_
        # too). Nothing says it will ever report, so a person looks,
        # rather than a wait forever with exit 0 (Codex review,
        # mikelward/repo#56). Never merged past.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "lanes"}, {"context": "codex"}]},
            }
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("needs a person", err)
        self.assertIn(
            "required check 'codex' has not passed on its head and nothing is running there",
            err,
        )
        self.assertIn("Re-run the workflow on the pull request", err)
        self.assertNotIn("waits:", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_blocked_pull_request_is_held_on_a_foreign_required_check_that_never_ran(self):
        # A check beyond the standard is required, kept as the floor, and
        # nothing has posted it on this head: its workflow may have a path
        # filter this diff does not match, and nothing here says it will
        # ever run -- so held for a person, not waited on forever (Codex
        # review, mikelward/repo#56). The same rule as the test above:
        # the check's name proves nothing about whether it runs.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "lanes"}, {"context": "deploy-preview"}]
                },
            }
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("needs a person", err)
        self.assertIn(
            "required check 'deploy-preview' has not passed on its head and nothing is running "
            "there, so nothing says it will report",
            err,
        )
        self.assertNotIn("waits:", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_blocked_pull_request_is_held_for_the_bound_app_not_a_same_named_check(self):
        # `lanes` is required from App 12345, and the head carries a green
        # `lanes` from App 7: by name it has reported, by binding it has
        # not, and nothing is running -- a head that already ran under App
        # 7 does not run again under App 12345 on its own. Held, naming
        # the App, not a review block and not a wait forever (Codex
        # review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": [("lanes", 7), "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "lanes", "integration_id": 12345}]},
            }
        ]
        fake.app_coverage = {12345: ("lanes-app", "all")}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("needs a person", err)
        self.assertIn("required check 'lanes' (needs App 12345) has not passed on its head", err)
        self.assertIn("a check bound to an App needs that App's own report", err)
        self.assertNotIn("waits:", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_check_required_both_bound_and_unbound_is_classified_not_crashed(self):
        # Two rulesets require `lanes`, one bound to App 12345 and one to
        # no App: the pair sorts without comparing None to an int, and the
        # bound one is what the head still lacks (Codex review,
        # mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "lanes"}]},
            },
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "lanes", "integration_id": 12345}]},
            },
        ]
        fake.app_coverage = {12345: ("lanes-app", "all")}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("needs a person", err)
        self.assertIn("required check 'lanes' (needs App 12345) has not passed on its head", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_config_app_logins_do_not_outlive_the_setup_run(self):
        # The pairing is process-global; a setup run must clear it on exit so
        # a later command in the same interpreter can't read it (Codex,
        # mikelward/repo#63).
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config_file(tmp, 'app_logins:\n  "4650916": mikelward-lanes\n')
            fake = FakeGh()
            fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
            code, _, err = _run(fake, ["--force", "--no-bootstrap", "--config", cfg, REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(apps._known_slugs, {})  # cleared on exit

    def test_a_config_login_pairing_verifies_a_bound_status_without_installations(self):
        # 2b: with the operator's App id->slug pairing (config `app_logins`,
        # registered via apps.register_known_slugs), a bound `lanes` status is
        # matched by its `{slug}[bot]` creator WITHOUT the user/installations
        # read -- so the evidence scan works on a token that can't call it
        # (the yaml-lite 403). The id-keyed installations read is wired to
        # fail here to prove it is never reached.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["codex", "zizmor"]}
        fake.statuses = {fake.default_head_sha: [("lanes", "success")]}
        fake.status_creators = {fake.default_head_sha: [("lanes", "lanes-app[bot]")]}
        fake.app_coverage_fails = _APP_TOKEN_403
        apps.register_known_slugs({"12345": "lanes-app"})
        try:
            with patch("repo_lib.gh.run", fake.run), patch("repo_lib.gh.try_run", fake.try_run):
                rules.reset_evidence_cache()
                self.assertEqual(rules.never_passed(REPO, [("lanes", 12345)]), [])
        finally:
            apps.register_known_slugs({})

    def test_coverage_prediction_ignores_the_config_login_pairing(self):
        # The pairing is EVIDENCE only: whether an App covers a repo is ground
        # truth to read, not an operator assertion. use_known=False makes
        # app_slug_for_id skip the registry, so a mis-paired slug can't make a
        # planned ADD for a different App look like coverage (Codex,
        # mikelward/repo#63).
        apps.register_known_slugs({"12345": "wrong-slug"})
        try:
            fake = FakeGh()
            fake.app_coverage = {}  # id 12345 not installed on the owner
            with patch("repo_lib.gh.run", fake.run), patch("repo_lib.gh.try_run", fake.try_run):
                self.assertIsNone(
                    apps.app_slug_for_id("owner", 12345, use_known=False)
                )
                # The default path still honors the assertion (evidence use).
                self.assertEqual(apps.app_slug_for_id("owner", 12345), "wrong-slug")
        finally:
            apps.register_known_slugs({})

    def test_without_the_pairing_the_bound_status_read_still_needs_installations(self):
        # The control: no pairing, and the id-keyed installations read fails,
        # so the same scan is a can't-tell (RulesetError), which is the 403
        # the pairing exists to avoid.
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["codex", "zizmor"]}
        fake.statuses = {fake.default_head_sha: [("lanes", "success")]}
        fake.status_creators = {fake.default_head_sha: [("lanes", "lanes-app[bot]")]}
        fake.app_coverage_fails = _APP_TOKEN_403
        apps.register_known_slugs({})
        with patch("repo_lib.gh.run", fake.run), patch("repo_lib.gh.try_run", fake.try_run):
            rules.reset_evidence_cache()
            with self.assertRaises(rules.RulesetError):
                rules.never_passed(REPO, [("lanes", 12345)])

    def test_a_bound_apps_superseded_success_still_counts_as_ever_passed(self):
        # The same status history asked two ways. Of the repository, the
        # question is whether the App has EVER passed `lanes` here: it did,
        # even though it later replaced that with a failure on the same
        # commit, so the evidence stands and the binding may be written. Of
        # a pull request's head, the question is whether the requirement is
        # satisfied NOW: the App's newest word is the failure, so it is not
        # (Codex review, mikelward/repo#56, both ways).
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["codex", "zizmor"]}
        fake.statuses = {fake.default_head_sha: [("lanes", "failure")]}
        fake.status_creators = {
            fake.default_head_sha: [("lanes", "lanes-app[bot]", "failure"), ("lanes", "lanes-app[bot]")]
        }
        fake.app_coverage = {12345: ("lanes-app", "all")}
        with patch("repo_lib.gh.run", fake.run), patch("repo_lib.gh.try_run", fake.try_run):
            rules.reset_evidence_cache()
            self.assertEqual(rules.never_passed(REPO, [("lanes", 12345)]), [])
            rules.reset_evidence_cache()
            self.assertEqual(
                rules.never_passed(REPO, [("lanes", 12345)], shas=[fake.default_head_sha]),
                [("lanes", 12345, True)],
            )
            # The same for an unbound context: the combined status shows
            # only its latest state, so the history is read for it too --
            # of the repository, never of a head (Codex review,
            # mikelward/repo#56).
            rules.reset_evidence_cache()
            self.assertEqual(rules.never_passed(REPO, [("lanes", None)]), [])
            rules.reset_evidence_cache()
            self.assertEqual(
                rules.never_passed(REPO, [("lanes", None)], shas=[fake.default_head_sha]),
                [("lanes", None, True)],
            )

    def test_a_superseded_success_on_a_pull_request_head_still_counts_in_the_history(self):
        # The only pass is on a pull request's head, later replaced by a
        # failure on the same commit. Of the repository's history every
        # attempt counts, on a pull request head as on the default
        # branch's -- the page of heads must not flip the scan into the
        # newest-only reading a head gets (Codex review,
        # mikelward/repo#56).
        fake = FakeGh()
        fake.check_runs = {fake.default_head_sha: ["codex", "zizmor"]}
        fake.open_prs = ["prsha1"]
        fake.statuses = {"prsha1": [("lanes", "failure")]}
        fake.status_creators = {"prsha1": [("lanes", "someone", "failure"), ("lanes", "someone")]}
        with patch("repo_lib.gh.run", fake.run), patch("repo_lib.gh.try_run", fake.try_run):
            rules.reset_evidence_cache()
            self.assertEqual(rules.never_passed(REPO, [("lanes", None)]), [])

    def test_a_bound_apps_superseded_success_is_not_a_pass(self):
        # The bound App posted `lanes` success, then failure, on this head,
        # and another producer's success is what the combined status shows.
        # The status history lists newest first, and only the App's newest
        # word counts -- so the requirement is unmet and the pull request
        # waits, rather than the stale success reading as satisfied and the
        # block as a review's (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {"AGENTS.md", "CLAUDE.md"}
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["zizmor"]}
        fake.statuses = {"prhead11": ["lanes"]}
        fake.status_creators = {
            "prhead11": [
                ("lanes", "other-bot[bot]"),
                ("lanes", "lanes-app[bot]", "failure"),
                ("lanes", "lanes-app[bot]"),
            ]
        }
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "lanes", "integration_id": 12345}]},
            }
        ]
        fake.app_coverage = {12345: ("lanes-app", "all")}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("needs a person", err)
        self.assertIn("required check 'lanes' (needs App 12345) has not passed on its head", err)
        self.assertIn("a check bound to an App needs that App's own report", err)
        self.assertNotIn("waits:", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_required_check_that_skipped_satisfies_the_blocked_head_as_github_counts_it(self):
        # GitHub counts a skipped or neutral required check as satisfied,
        # and the merge assessment does too -- so the blocked classification
        # must not read one as "has not passed" and wait forever where a
        # review block needs a person (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": [("lanes", None, "skipped"), "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "lanes"}]},
            }
        ]
        fake.review_decision = "REVIEW_REQUIRED"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("needs a person: GitHub blocks it on a review it requires and does not have", err)

    def test_a_pull_request_blocked_by_a_review_needs_a_person(self):
        # Every required check has passed and GitHub still calls it blocked,
        # and asked live, GitHub says a review rule holds it -- a review it
        # lacks, one requesting changes, or an unresolved conversation --
        # which no later run can settle: held, not waited on forever (Codex
        # review, mikelward/repo#56). Read live, not inferred from the rules
        # present, because a rule on the branch is not a rule unsatisfied.
        cases = [
            ("REVIEW_REQUIRED", 0, "a review it requires and does not have"),
            ("CHANGES_REQUESTED", 0, "a review requesting changes"),
            (None, 2, "2 unresolved conversation(s)"),
            ("REVIEW_REQUIRED", 1, "a review it requires and does not have and 1 unresolved conversation(s)"),
        ]
        for decision, unresolved, expected in cases:
            fake = FakeGh()
            fake.bootstrap_existing_paths = set()
            fake.bootstrap_open_pulls = self._OPEN_11
            fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
            fake.pulls = {11: {"mergeable_state": "blocked"}}
            fake.effective_rules = [
                {
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": "lanes"}]},
                },
                {"type": "pull_request", "parameters": {"required_review_thread_resolution": True}},
                # A rule that settles on its own is ALSO on the branch, and
                # must not turn a live review block into a wait.
                {"type": "required_deployments", "parameters": {}},
            ]
            fake.review_decision = decision
            fake.unresolved_threads = unresolved
            code, out, err = _run(fake, ["--force", "--no-rules", REPO])
            self.assertEqual(code, 1, (decision, unresolved, err))
            self.assertIn(f"needs a person: GitHub blocks it on {expected}, which this tool cannot settle", err)
            self.assertIn("failed on: bootstrap", err)
            self.assertEqual(fake.merged_pulls, [])

    def test_an_unresolved_conversation_blocks_only_where_a_rule_makes_it(self):
        # No rule requires conversations resolved, so an unresolved one is
        # not what blocks the pull request -- and with nothing else to
        # name, the block is held as one this tool cannot identify.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "lanes"}]},
            }
        ]
        fake.unresolved_threads = 1
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("needs a person: GitHub blocks it on something this tool cannot identify", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_signature_rule_holds_the_generated_commit_for_a_person(self):
        # The git-data API commit is unsigned, so a required_signatures
        # rule on the branch is what blocks it, and only a person settles
        # that.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [{"type": "required_signatures"}]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "needs a person: 'main' carries a 'required_signatures' rule, which the generated "
            "commit cannot satisfy",
            err,
        )
        self.assertEqual(fake.merged_pulls, [])

    def test_review_threads_are_read_to_the_last_page(self):
        # A page boundary is not a blocker: 150 resolved threads are not
        # "more conversations than this tool reads", and an unresolved one
        # on the second page is found (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [
            {"type": "pull_request", "parameters": {"required_review_thread_resolution": True}}
        ]
        fake.review_threads = [True] * 150
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("needs a person: GitHub blocks it on something this tool cannot identify", err)
        self.assertNotIn("conversation", err)
        self.assertEqual(fake.review_state_reads, 4)  # two pages, at plan time and again before the write

        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [
            {"type": "pull_request", "parameters": {"required_review_thread_resolution": True}}
        ]
        fake.review_threads = [True] * 100 + [False]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("needs a person: GitHub blocks it on 1 unresolved conversation(s)", err)

    def test_a_merge_that_landed_elsewhere_is_reported_not_returned(self):
        # The merge API pins the head, not the base: a retarget in the
        # window after the fresh assessment lands the commit on another
        # branch. Confirmed afterwards, and reported as such rather than
        # returned as the default branch's new tip (Codex review,
        # mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.merge_landed = "diverged"
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("merged pull request #11 on owner/repo, but 'main' does not contain the merged commit", err)
        self.assertIn("failed on: bootstrap", err)
        self.assertNotIn("is on 'main' now", out)
        self.assertEqual(len(fake.merged_pulls), 1)

    def test_an_unreadable_review_state_fails_the_step_rather_than_guessing(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = []
        fake.review_state_fails = True
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("GitHub reports pull request #11 blocked, and its review state could not be read", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_rule_that_settles_on_its_own_is_a_hint_in_the_hold_not_a_wait(self):
        # Every required check has passed and GitHub still calls it
        # blocked, but the branch carries a required_deployments rule: a
        # deployment finishing settles that without a person, so it is a
        # wait, not a hold (Codex review, mikelward/repo#56).
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pulls = {11: {"mergeable_state": "blocked"}}
        fake.effective_rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "lanes"}]},
            },
            {
                "type": "required_deployments",
                "parameters": {"required_deployment_environments": ["staging"]},
            },
        ]
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn(
            "pull request #11 needs a person: GitHub blocks it on something this tool cannot "
            "identify (the branch carries a 'required_deployments' rule, which may settle on its "
            "own",
            err,
        )
        self.assertEqual(fake.merged_pulls, [])

    def test_a_head_that_moved_during_the_wait_is_not_merged(self):
        # The plan read one head; by the write a push has replaced it. What
        # was confirmed was the merge of the head that was read, so the
        # fresh state is reported and left for a later run.
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"], "prhead11-moved": ["lanes", "zizmor"]}
        fake.pulls_later = {11: {"head_sha": "prhead11-moved"}}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn("pull request #11 is not what this run planned against", err)
        self.assertEqual(fake.merged_pulls, [])

    def test_a_merge_github_refuses_fails_the_step_and_nothing_else(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set()
        fake.bootstrap_open_pulls = self._OPEN_11
        fake.check_runs = {"prhead11": ["lanes", "zizmor"]}
        fake.pull_merge_fails = {11}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 1)
        self.assertIn("could not merge pull request #11", err)
        self.assertIn("failed on: bootstrap", err)

    def test_an_outdated_pinned_template_is_updated_through_the_pull_request(self):
        # codex-review's three workflow files are byte-pinned in every
        # consumer, so a copy that differs is never a customization: the
        # pull request replaces it with the current template (SPEC.md,
        # *What is updated, and what is only added*). Nothing else present
        # is touched, and an outdated copy still publishes its check, so
        # nothing is deferred on its account.
        fake = FakeGh()
        fake.bootstrap_outdated_paths = {".github/workflows/codex-review-listener.yml"}
        fake.check_runs = {fake.default_head_sha: ["lanes", "codex", "zizmor"]}
        code, out, err = _run(fake, ["--dry-run", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(
            "open a pull request writing 1 file(s) (update fleet ci scaffold files to the current "
            "templates):",
            out,
        )
        self.assertIn("- update .github/workflows/codex-review-listener.yml to the current template", out)
        self.assertIn("already present, untouched: 8 file(s)", out)
        self.assertNotIn("deferred", out)

        code, out, err = _run(fake, ["--force", REPO])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{REPO}: opened pull request #42 writing 1 fleet CI scaffold file(s)", out)
        tree_posts = [body for _args, body in fake.posts if "base_tree" in body]
        self.assertEqual(
            [e["path"] for e in tree_posts[0]["tree"]], [".github/workflows/codex-review-listener.yml"]
        )
        commit_posts = [body for _args, body in fake.posts if "parents" in body]
        self.assertTrue(
            commit_posts[0]["message"].startswith("Update fleet CI scaffold files to the current templates\n"),
            commit_posts[0]["message"],
        )
        self.assertEqual(fake.created_pulls[0]["title"], "Update fleet CI scaffold files to the current templates")
        self.assertIn("- update `.github/workflows/codex-review-listener.yml`", fake.created_pulls[0]["body"])
        ruleset_posts = [b for a, b in fake.posts if a[3] == f"repos/{REPO}/rulesets"]
        self.assertEqual(
            [e["context"] for e in ruleset_posts[0]["rules"][0]["parameters"]["required_status_checks"]],
            ["lanes", "codex", "zizmor"],
        )

    def test_a_missing_and_an_outdated_file_ride_one_pull_request(self):
        fake = FakeGh()
        fake.bootstrap_existing_paths = set(_SCAFFOLD_PATHS) - {"AGENTS.md"}
        fake.bootstrap_outdated_paths = {".github/workflows/codex-review.yml"}
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        commit_posts = [body for _args, body in fake.posts if "parents" in body]
        self.assertTrue(commit_posts[0]["message"].startswith("Add and update fleet CI scaffold files\n"))
        self.assertIn("- add AGENTS.md", commit_posts[0]["message"])
        self.assertIn("- update .github/workflows/codex-review.yml to the current template", commit_posts[0]["message"])
        # A workflow rides the code lane, so no docs prefix even though
        # AGENTS.md alone would have taken one.
        self.assertFalse(commit_posts[0]["message"].startswith("docs:"))

    def test_a_present_unpinned_file_that_differs_is_left_alone(self):
        # ci.yml, the zizmor files, lanes.conf and the conventions files may
        # carry a project's own decisions: present is enough, whatever they
        # hold. (The fixture reports a placeholder sha for each of them, so
        # every one differs from the scaffold's content.)
        fake = FakeGh()
        code, out, err = _run(fake, ["--force", "--no-rules", REPO])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.created_pulls, [])
        self.assertNotIn("update", out + err)


class LogOpenFailureTest(unittest.TestCase):
    def test_an_unwritable_log_warns_and_carries_on(self):
        # Opening the setup log can fail (an unwritable directory), and the
        # run carries on and can still succeed -- so this is a warning, not
        # an error, and it must not raise.
        with tempfile.TemporaryDirectory() as d:
            not_a_dir = os.path.join(d, "afile")
            open(not_a_dir, "w").close()
            log = setup_cmd._Log(os.path.join(not_a_dir, "sub", "setup.log"), "header\n")
            err = StringIO()
            with redirect_stderr(err):
                log.arm()
                log.write("a line")  # best-effort: must not raise
                log.close()
            msg = err.getvalue()
        self.assertIn("warning: could not open the log", msg)
        self.assertNotIn("error:", msg)


if __name__ == "__main__":
    unittest.main()
