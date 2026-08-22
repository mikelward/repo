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

What this checks that `repo setup`'s ruleset step does not manage
(ported from the shell's own header comment): repo-rules sets
required_status_checks and pull_request (conversation resolution, up to
date, rebase-only) through a ruleset it owns by name, and deliberately
leaves every other field of that ruleset untouched on update -- including
bypass_actors, which this module reads back and reports on every branch
ruleset whose conditions plainly cover the target branch (literal match
only -- a pattern such as "refs/heads/*" is reported unevaluated rather
than guessed at). It also checks non_fast_forward and deletion, two rule
types repo-rules has no opinion on: a required check means nothing if the
branch it protects can be force-pushed or deleted out from under it. When
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

Cost and reliability: free -- GitHub's REST API, inside the standard
5,000-authenticated-requests-an-hour limit. A run costs four or five
calls per repository plus one per branch ruleset found on it -- a
handful, in practice. Every failure mode is loud: not authenticated (the
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

from repo_lib import gh, rules
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
        for rule in effective_rules:
            if rule.get("type") != "required_status_checks":
                continue
            for check_entry in (rule.get("parameters") or {}).get("required_status_checks") or []:
                contexts.add(check_entry.get("context"))
        for check in checks:
            if check in contexts:
                ok(f"'{check}' is a required status check")
            else:
                gap(f"'{check}' is NOT a required status check")
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

    if gap_found[0]:
        raise SystemExit(1)
    return 0
