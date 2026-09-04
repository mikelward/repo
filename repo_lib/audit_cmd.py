"""`repo audit` -- report whether a repository's branch rules actually hold.

Port of mikelward/scripts's repo-rules-audit (see its own header comment
for the full reasoning; repeated here only where the port changes
something, and only where the current, fully-hardened shell source --
mikelward/scripts#216 -- differs from an earlier version). Read-only: it
never writes anything, unlike repo_lib.rules (repo setup's ruleset
composer), which this module deliberately does not call into except for
check_master_branch (see below) -- the two tools ask genuinely different
questions (repo-rules composes a ruleset that satisfies a policy;
repo-rules-audit reads GitHub's own MERGED view back and reports whether
it already does), so their branch-condition-matching logic stays separate
here the same way it stays separate in the shell source, rather than
threading this module through rules.py's private, create/update-oriented
internals.

check_master_branch IS reused directly from repo_lib.rules (with a small
quiet= addition -- see its own docstring) rather than reimplemented: it is
a full, independently-tested API call + branching + messaging, not a
one-line predicate, and repo setup already established it as this
codebase's home for "does repo have an actual branch named master".
rules.DEFAULT_CHECKS is reused too, for the same reason the shell source
threads one shared variable through its own usage() and arg-parsing
default: so this can't silently drift from what repo-rules itself
requires by default.

What this checks independently of what `repo setup`'s ruleset step itself
manages (ported from the shell's own header comment, since extended):
`repo setup` sets required_status_checks, pull_request (conversation
resolution, up to date, rebase-only), required_linear_history, and
non_fast_forward through a ruleset it owns by name, and deliberately
leaves every other field of that ruleset untouched on update -- including
bypass_actors, which this module reads back and reports on every branch
ruleset whose conditions plainly cover the target branch (literal match
only -- a pattern such as "refs/heads/*" is reported unevaluated rather
than guessed at). This module checks required_linear_history and
non_fast_forward too, on its own account rather than trusting `repo
setup` wrote them: a bypass actor can defeat either regardless of what
the ruleset says (see the note `repo setup` itself now prints when one
exists), so this is independent verification of what actually holds, not
a check on a field `repo setup` has no opinion on -- unlike deletion,
which genuinely is one: a required check means nothing if the branch it
protects can be force-pushed or deleted out from under it. When
auditing the default branch (no --branch given), it flags a default
branch not literally named "main". When AUDITING THE REPOSITORY'S REAL
DEFAULT BRANCH -- determined directly via the repo's own .default_branch,
not merely by whether --branch was omitted; an explicit `--branch main`
that happens to name the actual default gets this too -- it also checks
whether every ruleset that plainly covers it also targets
refs/heads/main and refs/heads/master (see _targeting_status below for
the literal-first-then-glob-fallback nuance this took several rounds of
review to get right in the shell source). Independent of any ruleset, it
also warns whenever the repository has an actual branch named "master"
at all.

It also audits where secrets live (new here; the shell source never did).
The fleet's shared credentials -- the weekly dependency batches'
`<HUB>_PAT` (or `<HUB>_APP_ID` + `<HUB>_APP_PRIVATE_KEY` pair) and the
screenshot commit-back token -- each belong in an environment named after
the reusable workflow that reads them, because a secret passed to a
reusable workflow reaches the runner of every job in it, a batch's
untrusted update job included (repo_lib.credentials has the full
reasoning). A credential kept as a REPOSITORY secret, a consumer (one
carrying the hub's caller workflow) whose environment holds no credential
at all, and a batch credential left behind by a batch this repository no
longer runs are each reported as [FIX] -- a finding `repo setup` closes,
named with the command -- rather than [GAP]: the environment layout is
being rolled out through `repo setup`, and until every repository has
been through it these are not counted toward the exit status (TODO.md
records promoting them). Every other repository-level secret is listed
under [CHECK]: with the callers passing `secrets: inherit`, those reach
the update job as well, and each should be scoped to an environment its
one consuming job declares -- or confirmed, by hand, as genuinely
repository-wide. Names only, never values: GitHub does not return them.

Cost and reliability: free -- GitHub's REST API, inside the standard
5,000-authenticated-requests-an-hour limit. A run costs six or seven
calls per repository plus one per branch ruleset found on it, plus three
to six for the secrets audit and one per batch caller read -- a handful, in practice. The exception is the never-reported walk: on a
healthy repository every required check is found on the default head and
it stops after two calls, but where one has never reported it goes on to
scan a page of open and then closed pull requests, so the expensive path
is the one that finds a real gap. Every failure mode is loud: not authenticated (the
first call fails, exit 1), GitHub down or rate limited (same) -- a failed
read is never reported as a gap, because "could not tell" and "found a
gap" are different findings and conflating them would print a false
all-clear as easily as a false alarm.

Exit status: 0 if every check and property held, 1 if anything did not
(or could not be read), 2 for a usage error.
"""

import json
import re
import urllib.parse

from repo_lib import credentials, gh, rules
from repo_lib.common import error, error_lines

# Same shape as setup_cmd.py's/secrets_cmd.py's own OWNER_REPO_RE -- kept
# as its own module-level copy rather than imported, matching this
# codebase's existing convention of a small, self-contained validator per
# subcommand module (secrets_cmd.py and setup_cmd.py each already carry
# their own copy of this exact regex).
OWNER_REPO_RE = re.compile(r"^(?!\.\.?/)[A-Za-z0-9._-]+/(?!\.\.?$)[A-Za-z0-9._-]+$")

# A CHECK name is printed one per line in this report (same reasoning
# rules.py's own _valid_no_control_chars gives for check/ruleset names
# there); a raw control character would make that output ambiguous.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

# Same glob-char class as repo-rules-audit's own jq `is_literal` test
# (and rules.py's _GLOB_CHARS_RE) -- GitHub's ref-name conditions accept
# fnmatch-style globs, and a ref containing one of these is not something
# this module's literal matching can safely evaluate.
_GLOB_CHARS_RE = re.compile(r"[*?\[]")

# The three refs a ruleset covering the repository's actual default
# branch must target, per repo-rules' own hardened create-time scope
# (rules.py's _HARDENED_INCLUDE) -- duplicated here as its own constant,
# not imported, for the same "small self-contained validator per module"
# reason as OWNER_REPO_RE above: this module's own literal/glob matching
# is intentionally separate from rules.py's (see module docstring), so
# the list it checks against stays local to it too.
_REQUIRED_DEFAULT_REFS = ["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master"]


def add_arguments(parser):
    parser.add_argument(
        "--branch",
        help="check this branch instead of the repository's default",
    )
    parser.add_argument("repo", metavar="OWNER/REPO")
    parser.add_argument(
        "check",
        nargs="*",
        metavar="CHECK",
        help="a required status check to look for (default: " + " ".join(rules.DEFAULT_CHECKS) + ")",
    )


def _valid_check_name(name):
    if not name:
        error("empty check name")
        return False
    if _CONTROL_CHAR_RE.search(name):
        error(f"check name '{name}' contains a control character (tab, newline, or")
        error("similar). Check names are printed one per line in this report, so")
        error("such a name cannot be handled unambiguously. Refusing rather than")
        error("guessing what was meant.")
        return False
    return True


def _is_literal(ref):
    return not _GLOB_CHARS_RE.search(ref)


def _matches_branch(ref, branch_ref, default_branch_matches):
    return ref == branch_ref or ref == "~ALL" or (ref == "~DEFAULT_BRANCH" and default_branch_matches)


def _branch_coverage_verdict(include, exclude, branch_ref, default_branch_matches):
    """One of "covers" / "excluded" / "unevaluated" / "not-covering".

    Mirrors repo-rules-audit's own jq program exactly, glob-char class
    included: literal coverage is checked FIRST -- a ruleset whose
    `include` already spells out the branch literally is fully resolved
    regardless of what other glob pattern also sits in `include` -- and a
    glob only becomes relevant as a fallback once no literal entry
    covers the branch, in which case the verdict is "unevaluated" (needs
    manual review) rather than a guessed pass or fail. A glob in
    `exclude` can only ever be reached when `include` already covers the
    branch literally (a ruleset that doesn't literally include the
    branch is already "unevaluated" or "not-covering" before `exclude`
    is even consulted) -- true by construction of the branches below, not
    asserted separately.
    """
    inc_literal = any(_matches_branch(r, branch_ref, default_branch_matches) for r in include)
    inc_pattern = any(not _is_literal(r) for r in include)
    exc_literal = any(_matches_branch(r, branch_ref, default_branch_matches) for r in exclude)
    exc_pattern = any(not _is_literal(r) for r in exclude)
    if inc_literal:
        if exc_literal:
            return "excluded"
        if exc_pattern:
            return "unevaluated"
        return "covers"
    if inc_pattern:
        return "unevaluated"
    return "not-covering"


def _targeting_status(include, exclude):
    """("complete" | "missing" | "unevaluated", [missing refs]) for a
    ruleset already known to "cover" the branch (see
    _branch_coverage_verdict) -- whether its `include`/`exclude` also
    target all of ~DEFAULT_BRANCH, refs/heads/main and refs/heads/master.

    Literal-first-then-glob-fallback again, same discipline as coverage
    above: a required ref missing from a literal `include` is only
    "unevaluated" (not "missing") when some OTHER glob in `include` might
    reach it -- this script does not reimplement GitHub's ref matching to
    find out. `~ALL` covers all three unconditionally unless `exclude`
    literally carves one back out (checked explicitly; `exclude` needs no
    glob check here because a glob in it would already have routed the
    whole ruleset to "unevaluated" in _branch_coverage_verdict before this
    function is ever called for it)."""
    if "~ALL" in include:
        missing = [w for w in _REQUIRED_DEFAULT_REFS if w in exclude]
        return ("missing", missing) if missing else ("complete", [])
    missing = [w for w in _REQUIRED_DEFAULT_REFS if w not in include or w in exclude]
    if not missing:
        return "complete", []
    if any(not _is_literal(r) for r in include):
        return "unevaluated", []
    return "missing", missing


# The weekly dependency batches, keyed by the caller workflow file a consumer
# carries. Each hub's publish job reads its credential from an environment
# of the same name (see that repository's docs/PAT.md).
def audit_duplicate_rulesets(repo, ok, note):
    """Reports a SECOND ruleset carrying the managed name.

    GitHub does not make a ruleset's name unique within a repository, so
    two can be named `main` and both apply. `repo setup` writes the first
    and says it is leaving the other alone; nothing fixes it, because what
    to do when the two disagree is undecided (see TODO.md).

    [CHECK], therefore, not [FIX] or [GAP]: [FIX] would claim `repo setup`
    closes it, which it does not, and [GAP] would fail the audit over a
    state no command here can resolve."""
    try:
        ids = rules.find_rulesets_named(repo)
    except rules.RulesetError:
        raise SystemExit(1)
    if not ids:
        # Not a gap on its own: the branch's own rules are checked above
        # from the effective-rules API, which covers an inherited or
        # differently-named ruleset too. Saying "exactly one" here would
        # be a false all-clear for a repository that has none of its own.
        ok(f"no repository ruleset is named '{rules.DEFAULT_RULESET_NAME}'")
        return
    extras = ids[1:]
    if not extras:
        ok(f"exactly one ruleset is named '{rules.DEFAULT_RULESET_NAME}'")
        return
    note(
        f"more than one ruleset is named '{rules.DEFAULT_RULESET_NAME}' -- "
        f"also id(s) {', '.join(extras)}. Rulesets aggregate, so all of them apply, and "
        "`repo setup` writes only the first. Reconcile them by hand"
    )


def audit_legacy_rulesets(repo, ok, fix):
    """Reports a ruleset still carrying a name this tool used before
    `rules.DEFAULT_RULESET_NAME`.

    `repo setup` says so on every run, which is how the fleet finds them
    -- but that means running a write command against a repository just to
    ask a read-only question, so there was no way to sweep the fleet for
    "which of these still have two?". rules.find_legacy_rulesets is
    reused rather than reimplemented, so the two commands cannot come to
    disagree about which names count.

    [FIX] rather than [GAP]: `repo setup` closes it, either by adopting
    the ruleset (when there is no standard-named one) or by deleting it
    (when it is identical to the one that survives). One that is neither
    still needs a human, and setup says which."""
    try:
        legacy = rules.find_legacy_rulesets(repo)
    except rules.RulesetError:
        # find_legacy_rulesets has already reported the failed call. Fail
        # closed, like every other ruleset read here: "could not tell"
        # must never print as "there is none".
        raise SystemExit(1)
    if not legacy:
        ok(f"no ruleset left under a name this tool used before '{rules.DEFAULT_RULESET_NAME}'")
        return
    for name, rid in legacy:
        fix(
            f"'{name}' (id {rid}) is a ruleset name this tool used before "
            f"'{rules.DEFAULT_RULESET_NAME}' -- rulesets aggregate, so it applies too. "
            "`repo setup` adopts it, or deletes it when it duplicates the one that stays"
        )


def audit_auto_merge(repo, ok, fix):
    """Whether the repository allows auto-merge. The weekly batches arm
    auto-merge on the pull requests they open, and a repository with the
    setting off leaves them parked -- quietly, since a batch's pull request
    is the one nobody is watching for. A setup pass enables it, so this is
    a [FIX] like the credential findings."""
    try:
        value = gh.run(["api", f"repos/{repo}", "--jq", ".allow_auto_merge"]).strip()
    except gh.GhError as e:
        error_lines(f"could not read whether {repo} allows auto-merge:", e.stderr)
        raise SystemExit(1)
    if value == "true":
        ok("auto-merge is allowed on the repository")
    else:
        fix(
            "auto-merge is not allowed on the repository -- the weekly batch cannot arm it on "
            f"its pull requests; `repo setup {repo}` enables it"
        )


def audit_delete_branch_on_merge(repo, ok, fix):
    """Whether the repository deletes a pull request's head branch once it
    merges. Off, nothing sweeps the branches a merged pull request leaves
    behind -- this is the setting `repo cleanup`'s own docstring names as
    the reason that command has to exist at all. GitHub applies it from
    the merge event itself, so it fires correctly on a rebase-merged
    branch too, unlike an ancestry check. A setup pass enables it, so this
    is a [FIX] like the auto-merge finding above."""
    try:
        value = gh.run(["api", f"repos/{repo}", "--jq", ".delete_branch_on_merge"]).strip()
    except gh.GhError as e:
        error_lines(f"could not read whether {repo} deletes branches on merge:", e.stderr)
        raise SystemExit(1)
    if value == "true":
        ok("a merged pull request's head branch is deleted automatically")
    else:
        fix(
            f"{repo} does not delete a merged pull request's head branch automatically -- "
            f"branches accumulate until `repo cleanup` sweeps them; `repo setup {repo}` enables it"
        )


def audit_secrets(repo, ok, fix):
    """Reports where the fleet credentials live, and names every other
    repository-level secret. See the module docstring for the reasoning.

    A failed read exits 1 rather than reporting a finding: "could not
    tell" and "found a gap" are different findings (same rule as the rest
    of this module). The reads, and the one 404 they tolerate, are
    repo_lib.credentials' -- the same ones `repo setup` fixes from."""
    try:
        repo_secrets = credentials.repository_secrets(repo)
        environments = credentials.environments(repo)
    except credentials.ReadError as e:
        error_lines(e.message, e.detail)
        raise SystemExit(1)

    def environment_secrets(hub):
        try:
            return credentials.environment_secrets(repo, environments, hub)
        except credentials.ReadError as e:
            error_lines(e.message, e.detail)
            raise SystemExit(1)

    def move_command(names):
        flags = " ".join(f"--credential {name}=PATH" for name in names)
        return f"`repo setup {flags} {repo}`"

    # Every workflow is read, on every branch: a batch's caller is
    # whichever file calls it from a job -- the fleet names it `<hub>.yml`,
    # but GitHub runs any name, from any branch a push lands on -- and a
    # file that mentions the batch in a shape the reader cannot resolve is
    # "cannot tell", never "does not run". Read as `repo setup` reads it,
    # so the two agree.
    try:
        texts = credentials.workflow_texts(repo)
    except credentials.ReadError as e:
        error_lines(e.message, e.detail)
        raise SystemExit(1)
    found = {hub: credentials.callers(texts, credentials.hub_workflow(hub)) for hub in credentials.BATCH_HUBS}
    unread = {hub: credentials.unread_mentions(texts, credentials.hub_workflow(hub)) for hub in credentials.BATCH_HUBS}
    # A hub with an unread mention is "cannot tell" even beside a readable
    # caller: `repo setup` refuses every move for it, so an [ok] or a move
    # here would contradict the command the audit points at.
    unknown = [hub for hub in credentials.BATCH_HUBS if unread[hub]]
    hubs = [hub for hub in credentials.BATCH_HUBS if found[hub] and hub not in unknown]
    for hub in unknown:
        fix(
            f"{hub}: {credentials.workflow_labels(unread[hub])} mentions "
            f"{credentials.hub_workflow(hub)} in a shape "
            f"this cannot read as a caller -- whether the batch runs there cannot be told; write "
            f"the caller as a job-level `uses:` (`repo setup` moves or deletes nothing of its "
            f"until then)"
        )
    for hub in hubs:
        pat, app_id, app_key = credentials.batch_credentials(hub)
        environment, env_secrets = environment_secrets(hub)
        in_environment = credentials.usable(env_secrets, hub)
        as_repository = [name for name in (pat, app_id, app_key) if name in repo_secrets]
        # The callers' shape decides whether an environment credential can
        # reach the batch at all: only `secrets: inherit` carries one into
        # a called workflow, and one caller naming its secrets is the
        # finding however the others read. Read as `repo setup` reads
        # them, so the two agree -- an [ok] here for a credential setup
        # calls NOT FIXED would be a false all-clear.
        inherits = True
        for caller, verdict in sorted(found[hub].items()):
            if not verdict:
                inherits = False
                fix(
                    f"{hub}: {credentials.workflow_label(caller)} passes its secrets by name, "
                    f"so a credential in the '{hub}' "
                    f"environment never reaches the batch -- convert the caller to `secrets: inherit`"
                )
        if as_repository:
            # Reported even when the environment already holds a copy: the
            # repository one is what still reaches the update job.
            fix(
                f"{hub}: {', '.join(as_repository)} is a repository secret, which reaches "
                f"every job of every workflow, the batch's untrusted update job included -- "
                f"{move_command(as_repository)} moves it into the '{hub}' environment"
            )
        if in_environment:
            if not as_repository and inherits is True:
                ok(f"{hub}: the batch credential lives in the '{hub}' environment")
        elif not credentials.usable(set(repo_secrets) | set(env_secrets), hub):
            # No usable credential anywhere. A whole credential at repository
            # level still opens the batch's pull requests (through `inherit`),
            # and so does an App pair split across the two scopes -- the
            # called workflow sees both halves -- so there the move above is
            # the one fix; half an App pair with no partner opens nothing, so
            # it gets this finding as well as the move.
            half = (
                f", and {', '.join(as_repository)} is only half of the App pair"
                if as_repository
                else ""
            )
            fix(
                f"{hub}: environment '{hub}' holds no batch credential ({pat}, or {app_id} "
                f"and {app_key}){half} -- the batch opens its pull requests as GITHUB_TOKEN; "
                f"{move_command([pat])} sets one"
            )

    expected = {name for hub in (*hubs, *unknown) for name in credentials.batch_credentials(hub)}
    every_batch_name = {
        name for hub in credentials.BATCH_HUBS for name in credentials.batch_credentials(hub)
    }
    stale = sorted(name for name in repo_secrets if name in every_batch_name and name not in expected)
    if stale:
        fix(
            f"batch credential(s) for a batch this repository does not run: "
            f"{', '.join(stale)} -- `repo setup {repo}` deletes them"
        )
    # A batch whose caller was removed can leave its credential behind in
    # the hub's environment -- the place this audit steers it to -- where
    # the loop above never looks. An active credential nothing uses is the
    # same finding wherever it sits.
    for hub in credentials.BATCH_HUBS:
        if hub in hubs or hub in unknown:
            continue
        environment, env_secrets = environment_secrets(hub)
        left_behind = sorted(
            name for name in env_secrets if name in credentials.batch_credentials(hub)
        )
        if left_behind:
            fix(
                f"batch credential(s) for a batch this repository does not run, in the "
                f"'{environment}' environment: {', '.join(left_behind)} -- "
                f"`repo setup {repo}` deletes them"
            )

    # The commit-back workflow, audited the way its batch siblings are: by
    # what calls it, with `repo setup`'s reading, so the two agree on a
    # missing token, a token behind a caller naming its secrets, and a
    # token nothing uses.
    token = credentials.COMMIT_ARTIFACT_TOKEN
    label = credentials.COMMIT_ARTIFACT_ENV
    prefix = credentials.COMMIT_ARTIFACT_WORKFLOW
    callers = credentials.callers(texts, prefix)
    unread = credentials.unread_mentions(texts, prefix)
    environment, env_secrets = environment_secrets(label)
    if unread:
        fix(
            f"{label}: {credentials.workflow_labels(unread)} mentions {prefix} in a shape "
            f"this cannot read as a "
            f"caller -- whether the token is used there cannot be told; write the caller as a "
            f"job-level `uses:` (`repo setup` moves or deletes nothing of its until then)"
        )
    elif not callers:
        where = []
        if token in repo_secrets:
            where.append("as a repository secret")
        if token in env_secrets:
            where.append(f"in the '{environment}' environment")
        if where:
            fix(
                f"{token} {' and '.join(where)}, but no workflow here calls "
                f"{prefix.rstrip('/')} -- `repo setup {repo}` deletes it"
            )
    else:
        inherits = True
        for caller, verdict in sorted(callers.items()):
            if not verdict:
                inherits = False
                fix(
                    f"{label}: {credentials.workflow_label(caller)} passes its secrets by name, "
                    f"so a token in the '{label}' "
                    f"environment never reaches the commit-back workflow -- convert the caller to "
                    f"`secrets: inherit`"
                )
        if token in repo_secrets:
            # Reported even when the environment already holds a copy: the
            # repository one is what reaches every job of every workflow.
            fix(
                f"{token} is a repository secret, which reaches every job of every workflow -- "
                f"{move_command([token])} moves it into the '{label}' environment"
            )
        if token in env_secrets:
            if token not in repo_secrets and inherits:
                ok(f"{label}: the token lives in the '{label}' environment")
        elif token not in repo_secrets:
            fix(
                f"{label}: environment '{label}' holds no {token} -- the commit-back workflow "
                f"pushes as GITHUB_TOKEN, and a push it authors starts no workflow run, so a "
                f"pull request with drift wedges on checks that never arrive; "
                f"{move_command([token])} sets one"
            )

    other = sorted(name for name in repo_secrets if name not in credentials.FLEET_CREDENTIALS)
    if other:
        print(
            "  [CHECK] repository secrets -- these reach every job of every workflow, "
            "and every job of a reusable workflow called with `secrets: inherit` (the "
            "dependency batches are); scope each to an environment its one consuming "
            "job declares, or confirm it must be repository-wide:"
        )
        for name in other:
            print(f"    {name}")
    elif repo_secrets:
        ok("no repository-level secrets beyond the fleet credentials reported above")
    else:
        ok("no repository-level secrets")


def run(args):
    if not OWNER_REPO_RE.match(args.repo):
        error(f"'{args.repo}' is not OWNER/REPO")
        raise SystemExit(2)

    checks = args.check if args.check else list(rules.DEFAULT_CHECKS)
    for check in checks:
        if not _valid_check_name(check):
            raise SystemExit(2)

    if args.branch is not None and not args.branch:
        error("--branch needs a non-empty name")
        raise SystemExit(2)

    gh.require_gh()

    repo = args.repo
    asked_default = args.branch is None

    if asked_default:
        try:
            branch = gh.run(["api", f"repos/{repo}", "--jq", ".default_branch"]).strip()
        except gh.GhError as e:
            error_lines(f"could not read {repo}:", e.stderr)
            raise SystemExit(1)
        if not branch:
            error(f"could not read {repo}'s default branch")
            raise SystemExit(1)
    else:
        branch = args.branch

    gap_found = [False]

    def ok(message):
        print(f"  [ok] {message}")

    def gap(message):
        print(f"  [GAP] {message}")
        gap_found[0] = True

    def fix(message):
        # A finding `repo setup` closes, reported but not yet counted: the
        # environment layout for the fleet credentials is being rolled out
        # through `repo setup`, and failing every repository until it has
        # been run would make the exit status say nothing else. TODO.md
        # records promoting these to [GAP] once the fleet has been through
        # it.
        print(f"  [FIX] {message}")

    def note(message):
        # Neither ok nor a gap: something a human has to look at that no
        # command here resolves, so it neither passes nor fails the audit.
        # Same tier the unevaluated-ruleset-pattern report already uses.
        print(f"  [CHECK] {message}")

    print(f"{repo} (@{branch})")

    # Flagged, not just noted (matching the shell source's own reasoning):
    # this fleet's tooling and docs both say "main" in the singular, so a
    # repo answering something else is a real inconsistency to fix, not a
    # preference to leave alone -- and it's invisible from the branch's
    # own rules, which look identical under any name. Only checked when no
    # --branch was given: an explicit `--branch main` naming the actual
    # default is a different question (see default_branch_matches below),
    # and an explicit `--branch release` isn't claiming to BE the default
    # at all.
    if asked_default:
        if branch == "main":
            ok("the default branch is named 'main'")
        else:
            gap(f"the default branch is '{branch}', not 'main'")

    branch_ref = f"refs/heads/{branch}"
    encoded_branch = urllib.parse.quote(branch, safe="")
    try:
        # --paginate concatenates each page's own JSON array rather than
        # merging them into one, so a single json.loads over the raw output
        # breaks once a branch's effective rules span more than one page.
        # --jq '.[]' unwraps each page's array into one rule object per
        # line -- applied per page, so it stays correct across any number
        # of pages -- matching the line-oriented pattern already used below
        # for the ruleset id listing.
        rule_lines = gh.run(
            [
                "api",
                "--paginate",
                f"repos/{repo}/rules/branches/{encoded_branch}",
                "--jq",
                ".[]",
            ]
        ).splitlines()
    except gh.GhError as e:
        error_lines(f"could not read {repo}'s effective rules for {branch}:", e.stderr)
        raise SystemExit(1)
    effective_rules = [json.loads(line) for line in rule_lines if line.strip()]

    def any_rule(rule_type, extra=None):
        for rule in effective_rules:
            if rule.get("type") != rule_type:
                continue
            if extra is None or extra(rule):
                return True
        return False

    if any_rule("pull_request"):
        ok("a pull request is required before merging")
    else:
        gap(f"no pull_request rule -- direct pushes to {branch} are allowed")

    if any_rule(
        "pull_request",
        lambda r: (r.get("parameters") or {}).get("required_review_thread_resolution") is True,
    ):
        ok("review conversations must be resolved")
    else:
        gap("conversation resolution is not required")

    if any_rule("required_status_checks"):
        contexts = set()
        # Every context the branch enforces, with the App it is bound to,
        # deduplicated but in the order the rules list them.
        required_entries = []
        for rule in effective_rules:
            if rule.get("type") != "required_status_checks":
                continue
            for check_entry in (rule.get("parameters") or {}).get("required_status_checks") or []:
                context = check_entry.get("context")
                contexts.add(context)
                entry = (context, check_entry.get("integration_id") or None)
                if entry not in required_entries:
                    required_entries.append(entry)
        for check in checks:
            if check in contexts:
                ok(f"'{check}' is a required status check")
            else:
                gap(f"'{check}' is NOT a required status check")
        # Listed in the ruleset is only half of it. Asked of every context
        # the branch enforces, not just the named ones: a stale gate nobody
        # asked about blocks merges exactly as hard, and is likelier to be
        # the one nothing produces.
        if required_entries:
            try:
                unseen = rules.never_reported(repo, required_entries, ref=branch)
            except rules.RulesetError as e:
                error_lines(
                    f"could not tell which of {repo}'s checks have ever reported:",
                    e.detail,
                )
                raise SystemExit(1)
            # Two different faults with two different fixes. `repo setup`
            # reuses an existing entry by context (_build_update_body), so
            # it carries a stale integration_id straight through -- naming
            # it as the remedy for a wrong-App gate sends the user to a
            # command that completes and changes nothing.
            wrong_app = [i for i in unseen if rules.bound_to_another_app(i)]
            absent = [i for i in unseen if not rules.bound_to_another_app(i)]
            if absent:
                gap(
                    f"required but never reported: {rules.describe_missing(absent)} -- "
                    "add the check, or run `repo setup`"
                )
            if wrong_app:
                gap(
                    "required but never reported by the App it is bound to: "
                    f"{rules.describe_missing(wrong_app)} -- repoint the ruleset "
                    "entry; `repo setup` preserves the existing binding"
                )
            if not unseen:
                ok(
                    "every required check has reported: "
                    + rules.quoted(c for c, _ in required_entries)
                )

        if any_rule(
            "required_status_checks",
            lambda r: (r.get("parameters") or {}).get("strict_required_status_checks_policy")
            is True,
        ):
            ok("the branch must be up to date with the base before merging")
        else:
            gap(
                "branches do NOT need to be up to date before merging "
                "(a stale-base pass could still merge)"
            )
    else:
        gap(f"no required_status_checks rule at all -- named: {' '.join(checks)}")

    if any_rule("required_linear_history"):
        ok("commit history must be linear")
    else:
        gap("merge commits are allowed (history can bypass what a required check saw)")

    if any_rule("non_fast_forward"):
        ok("force pushes are blocked")
    else:
        gap("force pushes are allowed (a required check's history can be rewritten out from under it)")

    if any_rule("deletion"):
        ok("branch deletion is blocked")
    else:
        gap("branch deletion is allowed")

    # Bypass actors are a property of the ruleset that carries a rule, not
    # of the effective rule itself, so they're read back from the
    # rulesets directly -- every branch ruleset that reaches this
    # repository, org- or enterprise-owned included (includes_parents=
    # true, so an inherited bypass actor isn't silently missed), restricted
    # to ones actually enforced and covering $BRANCH, matched literally
    # (same discipline as repo-rules itself -- see _branch_coverage_verdict).
    real_default = [None]

    def default_branch_matches():
        # Only meaningful (and only fetched) for a ruleset whose
        # conditions actually reference ~DEFAULT_BRANCH -- lazily, and
        # cached, so a repo whose rulesets never use the token costs no
        # extra call. Directly whether $BRANCH IS the repository's real
        # default, not merely whether --branch was omitted: an explicit
        # `--branch main` naming the actual default must still get this.
        if asked_default:
            return True
        if real_default[0] is None:
            try:
                value = gh.run(["api", f"repos/{repo}", "--jq", ".default_branch"]).strip()
            except gh.GhError as e:
                error_lines(
                    f"could not read {repo}'s default branch (needed to resolve a "
                    "~DEFAULT_BRANCH ruleset scope):",
                    e.stderr,
                )
                raise SystemExit(1)
            if not value:
                error(f"could not read {repo}'s default branch")
                raise SystemExit(1)
            real_default[0] = value
        return real_default[0] == branch

    try:
        ruleset_ids = [
            line.strip()
            for line in gh.run(
                [
                    "api",
                    "--paginate",
                    f"repos/{repo}/rulesets?includes_parents=true",
                    "--jq",
                    '.[] | select(.target == "branch" and .enforcement == "active") | .id',
                ]
            ).splitlines()
            if line.strip()
        ]
    except gh.GhError as e:
        error_lines(f"could not list {repo}'s rulesets to check for bypass actors:", e.stderr)
        raise SystemExit(1)

    bypassers = []
    unevaluated = []
    undertargeted = []
    targeting_unevaluated = []

    for rid in ruleset_ids:
        try:
            raw = gh.run(["api", f"repos/{repo}/rulesets/{rid}"])
        except gh.GhError as e:
            error_lines(f"could not read ruleset {rid} on {repo}:", e.stderr)
            raise SystemExit(1)
        ruleset = json.loads(raw)
        name = ruleset.get("name") or f"id {rid}"
        conditions = (ruleset.get("conditions") or {}).get("ref_name") or {}
        include = conditions.get("include") or []
        exclude = conditions.get("exclude") or []

        # Only resolved when some ruleset's conditions actually reference
        # ~DEFAULT_BRANCH -- a raw substring check over the ruleset's own
        # JSON text, matching repo-rules-audit's own `grep -q
        # '~DEFAULT_BRANCH'` gate on the same call.
        dm = default_branch_matches() if "~DEFAULT_BRANCH" in raw else False

        verdict = _branch_coverage_verdict(include, exclude, branch_ref, dm)

        if verdict == "covers":
            for actor in ruleset.get("bypass_actors") or []:
                actor_id = actor.get("actor_id")
                actor_id_display = "-" if actor_id is None else actor_id
                bypassers.append(
                    f"  {name}: {actor.get('actor_type')} {actor_id_display} "
                    f"({actor.get('bypass_mode')})"
                )
            # Only meaningful when $BRANCH IS the repository's real
            # default (default_branch_matches(), not asked_default -- see
            # its own docstring): the three-ref convention is repo-rules'
            # own widened targeting for the default branch specifically,
            # not something a ruleset scoped to some other branch has any
            # reason to carry.
            if default_branch_matches():
                status, missing = _targeting_status(include, exclude)
                if status == "missing":
                    undertargeted.append(f"  {name}: does not target {', '.join(missing)}")
                elif status == "unevaluated":
                    unevaluated.append(
                        f"  {name} (targeting not evaluated: another ref pattern is present)"
                    )
                    # Tracked separately from the shared `unevaluated`
                    # bucket above: this one gates whether the
                    # targeting-complete [ok] summary below may print at
                    # all, not just whether this ruleset gets a [CHECK]
                    # line -- an unresolved ruleset must never sit
                    # silently under a claim that every ruleset's
                    # targeting was confirmed.
                    targeting_unevaluated.append(f"  {name}")
        elif verdict == "unevaluated":
            pattern = ", ".join(include)
            unevaluated.append(f"  {name} (matches: {pattern})")
            # A SEPARATE unevaluated case from the one inside `covers`
            # above: here the branch-coverage verdict itself couldn't be
            # determined at all (a glob in include, or a glob exclusion
            # over an otherwise-complete include) -- not "covers, but a
            # required ref might be behind a glob". Either way this
            # script cannot tell whether the ruleset covers $BRANCH, so
            # the targeting-complete [ok] summary must stay suppressed on
            # this ruleset too -- same bucket, same gate, for the same
            # reason: a claim that every covering ruleset's targeting was
            # confirmed cannot be made while one ruleset's coverage of the
            # branch itself remains unknown.
            targeting_unevaluated.append(f"  {name}")
        # "excluded" / "not-covering": nothing to report.

    if bypassers:
        gap(f"bypass actors configured on a ruleset covering {branch}:")
        for line in bypassers:
            print(line)
    else:
        ok(f"no bypass actor on any ruleset that plainly covers {branch}")

    if unevaluated:
        print("  [CHECK] rulesets with a pattern not evaluated automatically -- check by hand:")
        for line in unevaluated:
            print(line)

    if default_branch_matches():
        if undertargeted:
            gap(
                f"a ruleset covering {branch} does not target all of the default "
                "branch, refs/heads/main and refs/heads/master:"
            )
            for line in undertargeted:
                print(line)
        elif targeting_unevaluated:
            # Neither ok nor gap: a ruleset's targeting genuinely could
            # not be confirmed (a glob that might or might not reach a
            # missing ref), so claiming "every ruleset ... targets ..."
            # here would be a false all-clear over an audit that was
            # never completed. The [CHECK] section above already named it
            # for manual review.
            pass
        else:
            ok(
                f"every ruleset covering {branch} targets the default branch, "
                "refs/heads/main and refs/heads/master"
            )

    # Independent of any ruleset: a branch literally named "master" left
    # over from a rename (or never renamed at all) is the backdoor
    # repo-rules' wider targeting exists to close. quiet=True: this
    # module reports the finding itself, in its own [ok]/[GAP] format,
    # rather than duplicating rules.check_master_branch's own stderr
    # wording.
    status, detail = rules.check_master_branch(repo, quiet=True)
    if status == "exists":
        gap(
            "a branch literally named 'master' exists -- delete it, or confirm "
            f"every ruleset that protects {branch} also targets refs/heads/master"
        )
    elif status == "absent":
        ok("no branch literally named 'master'")
    else:
        error_lines(f"could not check whether {repo} has a branch named 'master':", detail)
        raise SystemExit(1)

    audit_duplicate_rulesets(repo, ok, note)
    audit_legacy_rulesets(repo, ok, fix)
    audit_auto_merge(repo, ok, fix)
    audit_delete_branch_on_merge(repo, ok, fix)
    audit_secrets(repo, ok, fix)

    if gap_found[0]:
        raise SystemExit(1)
    return 0
