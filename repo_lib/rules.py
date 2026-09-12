"""The ruleset step of `repo setup` -- compose a branch protection ruleset.

Port of mikelward/scripts's repo-rules (see its own header comment for the
full reasoning; repeated here only where the port changes something).
`setup_cmd.py` shells the equivalent step of repo-setup out to a separate
`repo-rules` script; there is no separate `repo rules` subcommand here, so
this module IS that step, called directly as a library rather than a
subprocess.

Built in from the start, not ported as a later hardening (see TODO.md):
a ruleset this module CREATES targets ~DEFAULT_BRANCH together with the
literal refs/heads/main and refs/heads/master, so a branch literally
called master -- a leftover from a rename, or one that was simply never
renamed -- cannot slip past the checks this module requires. An UPDATE
leaves an existing ruleset's targeting alone, like every other field this
module does not manage. A REAL branch by the other name, beside the
default, is the branch that lock exists to close: check_sibling_branch()
warns about it independent of any ruleset, since deleting it removes the
backdoor outright, and the ruleset step holds itself while one exists
(_hold_for_sibling_branch), since a ruleset written onto it enforces
rules there nothing here can tell that branch satisfies.

A required check has to have PASSED on the repository before this module
requires it, and a check the caller says cannot be satisfied yet (its
publishing workflow is still in a pull request) waits too: either one is
DEFERRED -- named in the plan with its reason and left out of this write,
never refused and never removed if already required -- so the rest of the
ruleset lands now and a later run adds it (SPEC.md, *The ladder*). There
is no override; `--force` only skips the confirmation.

Every internal helper below either returns a plain false-y result or
raises RulesetError to signal "abort the ruleset step" -- callers decide
what that means for their own exit status. A helper that fails at one
specific API call reports it in full (including gh's own stderr) at the
point of failure; the multi-call check-reporting walk in _collect_reported
instead lets its caller print one summary message, since which of several
calls failed is not itself useful to the person reading it.

Cost and reliability: free -- GitHub's REST API, inside the standard
5,000-authenticated-requests-an-hour limit. A typical run costs a handful
of calls; a worst case (a check that has never reported, so every head the
bounded scan offers is walked) costs a few hundred. Not on a hot path --
`repo setup` is never run from a shell prompt automatically.
"""

import json
import re
import sys
import urllib.parse

from repo_lib import apps, gh
from repo_lib.common import error, error_lines, info, warn

DEFAULT_RULESET_NAME = "main"
# Names this tool used before DEFAULT_RULESET_NAME settled. A repository
# carrying one is mid-migration, not misconfigured: it is adopted and
# renamed in place rather than left beside a new one, so nothing it holds
# (bypass actors, a narrowed scope, an extra rule type) is lost.
LEGACY_RULESET_NAMES = ("merge gates",)
# This fleet's usual three checks -- the lanes docs-vs-code split, Codex's
# review verdict, zizmor's workflow-injection scan -- used when --rule was
# never given, matching repo-rules' own default.
DEFAULT_CHECKS = ["lanes", "codex", "zizmor"]
# The rule types this module manages. Everything else in a ruleset it
# updates is carried through untouched (see _build_update_body), so this
# set says what gets written, not what may be present.
# required_linear_history and non_fast_forward take no parameters, unlike
# the other two: managing them is purely a presence check.
MANAGED_RULE_TYPES = {
    "required_status_checks",
    "pull_request",
    "required_linear_history",
    "non_fast_forward",
}
# GitHub's ref-name conditions accept fnmatch-style globs; a ref containing
# one of these is not something this module's literal matching can safely
# evaluate for a merge-method conflict (see _find_merge_method_conflicts).
_GLOB_CHARS_RE = re.compile(r"[*?\[]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
# The hardened targeting: a repository's real default branch, resolved by
# GitHub at merge time, together with the two literal names a rename can
# leave stranded.
_HARDENED_INCLUDE = ["~DEFAULT_BRANCH", "refs/heads/main", "refs/heads/master"]

# Shared with setup_cmd.py, which greps for it in a preview call's captured
# stdout to tell "nothing to write" apart from a real plan -- a module-
# level constant rather than a string duplicated (and driftable) in two
# files.
NO_OP_MESSAGE = "already matches; nothing to do"

# Sentinel distinguishing "no earlier fingerprint to compare against" (the
# default, meaning "compare against my own") from a real fingerprint
# tuple, which could otherwise collide with a legitimate one -- see
# apply_ruleset's expected_fingerprint.
_NO_EXPECTATION = object()


class RulesetError(Exception):
    """Signals "abort the ruleset step"; the reason has already been
    reported via error()/error_lines() at the point of failure, except
    where the caller's own wrapper message covers it (see module
    docstring).

    `.detail` carries the failed call and gh's own stderr for the raisers
    that report nothing themselves -- without it the wrapper can only say
    "could not tell", leaving no way to distinguish a rate limit from a
    permissions problem, or to see which call failed. None where the
    raiser already reported."""

    def __init__(self, detail=None):
        super().__init__(detail or "")
        self.detail = detail


class RulesetHeld(RulesetError):
    """The ruleset cannot be written until a person acts on it -- not a
    failed read or a bad request, and not a reason for any other step to
    stop. `.detail` says what and why; apply_ruleset reports it and hands
    it back as report["held"], and setup_cmd skips only the ruleset step
    (SPEC.md, invariant 1: every other step still makes its progress)."""


def _valid_no_control_chars(value, what):
    if _CONTROL_CHAR_RE.search(value):
        error(f"{what} contains a control character (tab, newline, or similar).")
        error("Check names are compared and recorded one per line, so such a")
        error("name cannot be handled unambiguously. Refusing rather than")
        error("guessing what was meant.")
        return False
    return True


def _json_string(value):
    """A JSON string literal, for splicing into a --jq program's own text
    (not a full JSON encoder -- matches repo-rules' own json_string, whose
    caller already rejected control characters upstream)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _read_default_branch(repo):
    try:
        branch = gh.run(["api", f"repos/{repo}", "--jq", ".default_branch"]).strip()
    except gh.GhError as e:
        error_lines(f"could not read {repo}:", e.stderr)
        raise RulesetError()
    if not branch:
        error(f"could not read {repo}'s default branch")
        raise RulesetError()
    return branch


def _check_allow_rebase(repo):
    """A ruleset NARROWS what a repository permits; it cannot enable a
    method the repository has turned off. Allowing only rebase on a repo
    with rebase merging disabled would leave the intersection empty and
    nothing could merge -- refused rather than fixed, since flipping a
    repository-wide merge setting is not this module's call to make."""
    try:
        allow = gh.run(["api", f"repos/{repo}", "--jq", ".allow_rebase_merge"]).strip()
    except gh.GhError as e:
        error_lines(
            f"could not read {repo} to check whether rebase merging is still allowed:",
            e.stderr,
        )
        return False
    if allow != "true":
        error(f"{repo} has rebase merging disabled, and this ruleset allows only")
        error("rebase. A ruleset narrows what the repository permits and cannot")
        error("enable a method, so together they would leave no way to merge")
        error("anything. Enable 'Allow rebase merging' in the repository's pull")
        error("request settings first.")
        return False
    return True


def _repo_is_empty(repo):
    """True/False, or None if the read itself failed (an unknown answer,
    never to be read as "empty")."""
    try:
        count = gh.run(["api", f"repos/{repo}/branches?per_page=1", "--jq", "length"]).strip()
    except gh.GhError:
        return None
    return count == "0"


def _json_lines(output):
    names = set()
    for line in output.splitlines():
        line = line.strip()
        if line:
            names.add(json.loads(line))
    return names


def _entry_satisfied(entry, names, app_pairs):
    """Whether a required-status-check entry has ever been reported by
    something GitHub would actually accept for it.

    An entry carrying an integration_id is bound to one GitHub App, so a
    same-named check run from a different producer -- or a legacy commit
    status, which has no App at all -- does not satisfy it. Matching on
    the name alone there prints an all-clear while merges stay blocked,
    which is the failure this whole check exists to catch."""
    context, integration_id = entry
    if integration_id is None:
        return context in names
    return (context, integration_id) in app_pairs


class _Reported:
    """What this repository has reported, in two strengths: `names` and
    `app_pairs` are every context name seen anywhere and every (name, App
    id) pair seen on a check run; `passed_names` and `passed_pairs` are the
    subset that concluded `success` at least once. Requiring a check that
    has run but never passed wedges every merge exactly as one that has
    never run does (SPEC.md, *The ladder*), so setup asks the stronger
    question; audit's "required but never reported" asks the weaker."""

    def __init__(self):
        self.names = set()
        self.app_pairs = set()
        self.passed_names = set()
        self.passed_pairs = set()

    def satisfied(self, entry, passed):
        if passed:
            return _entry_satisfied(entry, self.passed_names, self.passed_pairs)
        return _entry_satisfied(entry, self.names, self.app_pairs)


def _collect_reported(repo, wanted, ref=None, passed=False, shas=None, passing=frozenset({"success"})):
    """What this repository has ever reported, as a _Reported. Walks the
    default branch head, then open pull requests, then closed ones -- each
    stage bounded to one page -- stopping as soon as every entry in
    `wanted` is satisfied: reported at all, or, with `passed`, reported
    with a `success` conclusion.

    `wanted` is a set of (context, integration_id-or-None) entries. `ref`
    is the branch whose head to scan first -- the branch whose gates are
    actually in question. None means the repository's default branch. A
    check produced only on pushes to `release` never appears on the
    default branch's head, so scanning that head while auditing `release`
    reports a working gate as never reported. `shas`, when given, is the
    whole scan instead: exactly those commits, and no walk -- for asking
    what one particular head has reported, with the same App-binding
    semantics. `passing` is the set of check-run conclusions that count
    as passed: `success` alone for deciding whether to REQUIRE a check
    (a check that has only ever been skipped has not shown it can pass),
    while a caller asking whether one head SATISFIES a requirement also
    counts `skipped` and `neutral`, as GitHub does.

    Raises RulesetError on any read failure: an incomplete answer must
    never be read as "this check has never reported", which would either
    reject a valid name or require one on the strength of a safety check
    that never finished."""
    found = _Reported()
    names = found.names
    app_pairs = found.app_pairs

    # This answers only "has the bound App reported this check" -- evidence,
    # not liveness. A check RUN carries its App's id directly (below); the App
    # publishes `lanes` as a commit STATUS, and the Statuses REST API hides the
    # creating App -- it surfaces the bot user `{slug}[bot]`, not the
    # integration_id -- so a status the App posted is recognized by matching
    # that login, resolved from the id via the owner's installs. Whether that
    # App can STILL publish here (coverage) is a separate precondition on the
    # binding (apps.app_covers_repo), kept out of this scan on purpose: four
    # coverage findings came from entangling the two (Codex, mikelward/repo#52).
    # The status resolution stays lazy -- only for a bound entry still
    # unsatisfied after the check-run scan, and only where this SHA carries a
    # status for it -- so the ordinary path costs no extra calls.
    owner = repo.split("/", 1)[0]
    bound = {(context, iid) for context, iid in wanted if iid is not None}
    login_for = {}

    def bot_login(iid):
        if iid not in login_for:
            try:
                slug = apps.app_slug_for_id(owner, iid)
            except gh.GhError as e:
                # A failed read is can't-tell, never "the App never posted":
                # the same discipline the scans below hold to. The one 403
                # worth naming is the App-token one: no rerun and no scope
                # change clears it, so say so rather than leaving an
                # operator to re-authenticate at it (see apps.APP_TOKEN_HINT).
                detail = e.stderr
                if apps.is_missing_app_token(detail):
                    detail = f"{detail}\n{apps.APP_TOKEN_HINT}"
                raise RulesetError(f"resolving the App with id {iid} on {owner}:\n{detail}")
            login_for[iid] = f"{slug}[bot]" if slug else None
        return login_for[iid]

    def scan(sha):
        try:
            # Of the repository's history, every attempt counts (a re-run
            # that failed after a pass does not unmake the pass); of a
            # pull request's head, only the latest run per check is the
            # check's current word, which is GitHub's own default listing.
            history = shas is None
            out = _cached_run(
                [
                    "api",
                    "--paginate",
                    f"repos/{repo}/commits/{sha}/check-runs" + ("?filter=all" if history else ""),
                    "--jq",
                    ".check_runs[] | [.name, .app.id, .conclusion] | @json",
                ]
            )
        except gh.GhError as e:
            raise RulesetError(f"reading check runs for {sha}:\n{e.stderr}")
        for line in out.splitlines():
            if not line.strip():
                continue
            name, app_id, conclusion = json.loads(line)
            names.add(name)
            if conclusion in passing:
                found.passed_names.add(name)
            if app_id is not None:
                app_pairs.add((name, app_id))
                if conclusion in passing:
                    found.passed_pairs.add((name, app_id))
        try:
            out = _cached_run(
                [
                    "api",
                    "--paginate",
                    f"repos/{repo}/commits/{sha}/status",
                    "--jq",
                    ".statuses[] | [.context, .state] | @json",
                ]
            )
        except gh.GhError as e:
            raise RulesetError(f"reading commit statuses for {sha}:\n{e.stderr}")
        status_contexts = set()
        for line in out.splitlines():
            if not line.strip():
                continue
            context, state = json.loads(line)
            status_contexts.add(context)
            if state == "success":
                found.passed_names.add(context)
        names.update(status_contexts)
        # Only a bound entry the check-run scan did not already satisfy, AND
        # that this SHA actually carries a status for, needs the status's
        # creator resolved. Gating on a status being present here matters: the
        # login lookup reads `user/installations`, and if that is unavailable a
        # blanket resolution would abort the whole scan -- even when a later
        # pull request's check run carries the App's id directly and would
        # satisfy the binding with no installation lookup at all (Codex,
        # mikelward/repo#52). The combined /status endpoint drops the creator;
        # the per-status list below carries it.
        # And, of the history, an UNBOUND context whose latest status is
        # not a success but which has a status at all: the combined
        # status shows only the latest per context, and a success the
        # context later replaced on the same commit is still a pass here
        # (Codex review, mikelward/repo#56). Of a head, the latest is the
        # answer, so unbound entries never reach the history there.
        pending = [
            (context, iid)
            for context, iid in wanted
            if not found.satisfied((context, iid), passed)
            and context in status_contexts
            and (iid is not None or history)
        ]
        want_logins = {iid: bot_login(iid) for _context, iid in pending if iid is not None}
        if not any(iid is None for _context, iid in pending) and not any(want_logins.values()):
            return
        try:
            out = _cached_run(
                [
                    "api",
                    "--paginate",
                    f"repos/{repo}/commits/{sha}/statuses",
                    "--jq",
                    '.[] | [.context, (.creator.login // ""), .state] | @json',
                ]
            )
        except gh.GhError as e:
            raise RulesetError(f"reading commit status creators for {sha}:\n{e.stderr}")
        # The list is every status ever posted on the SHA, newest first.
        # Asked of a pull request's head (`shas`), only the first one per
        # (context, creator) is that creator's current word: a success
        # the same App later replaced with a failure or a pending does not
        # satisfy the requirement now. Asked of the repository's history,
        # the question is whether the App has EVER passed it here, and a
        # success it later replaced on the same commit still answers yes
        # (Codex review, mikelward/repo#56, both ways).
        newest_only = shas is not None
        seen = set()
        for line in out.splitlines():
            if not line.strip():
                continue
            context, login, state = json.loads(line)
            if newest_only and (context, login) in seen:
                continue
            seen.add((context, login))
            for entry_context, iid in pending:
                if entry_context != context:
                    continue
                if iid is None:
                    if state == "success":
                        found.passed_names.add(context)
                elif login and want_logins.get(iid) == login:
                    app_pairs.add((context, iid))
                    if state == "success":
                        found.passed_pairs.add((context, iid))

    def satisfied():
        return all(found.satisfied(e, passed) for e in wanted)

    if shas is not None:
        for sha in shas:
            scan(sha)
        return found

    endpoint = f"repos/{repo}/commits?per_page=1"
    if ref is not None:
        endpoint += f"&sha={urllib.parse.quote(ref, safe='')}"
    try:
        head = gh.run(["api", endpoint, "--jq", ".[0].sha"]).strip()
        heads = [head] if head and head != "null" else []
    except gh.GhError as e:
        # A repository with no commits yet answers 409 here -- a KNOWN
        # zero-report answer, not a failed read: nothing has ever run
        # because nothing has ever been pushed.
        if _repo_is_empty(repo) is True:
            heads = []
        else:
            raise RulesetError(
                f"reading the head of {ref or 'the default branch'}:\n{e.stderr}"
            )
    for sha in heads:
        scan(sha)
    if satisfied():
        return found

    # Page by page, newest first, stopping at the first page that settles
    # every entry: a pass on a pull request older than the newest hundred
    # is still a pass here, and a listing capped at one page read it as
    # "never" on every run (Codex review, mikelward/repo#56). Paged by
    # hand rather than --paginate so a repository with a long history
    # pays only for the pages it takes to find the answer -- and bounded,
    # since each head costs two reads and a check that never passed would
    # otherwise walk the whole history on every run, spending the hour's
    # API budget on a repository with thousands of pull requests (Codex
    # review, mikelward/repo#56). Evidence older than the bound is not
    # counted: the check stays deferred, named on every run.
    for state in ("open", "closed"):
        page = 1
        while page <= _EVIDENCE_PAGES:
            try:
                out = gh.run(
                    [
                        "api",
                        f"repos/{repo}/pulls?state={state}&per_page=100&sort=updated"
                        f"&direction=desc&page={page}",
                        "--jq",
                        ".[].head.sha",
                    ]
                )
            except gh.GhError as e:
                raise RulesetError(f"listing {state} pull requests:\n{e.stderr}")
            # Not `shas`: scan() reads that name to tell the repository's
            # history (every attempt counts) from a head (newest only),
            # and rebinding it here flipped every pull request head into
            # newest-only, discarding a superseded pass (Codex review,
            # mikelward/repo#56).
            page_shas = [sha.strip() for sha in out.splitlines() if sha.strip()]
            for sha in page_shas:
                scan(sha)
                if satisfied():
                    return found
            if len(page_shas) < 100:
                break
            page += 1
    return found


def effective_rules(repo, branch):
    """Every rule GitHub enforces on `branch`, as the effective-rules
    endpoint reports them -- across every ruleset covering it, not just
    the one this module manages. Rulesets AGGREGATE, so a rule another
    (or an inherited) ruleset carries is enforced just as hard as one in
    ours, and a caller asking "can a pull request satisfy this branch"
    gets the wrong answer from the managed ruleset alone (Codex review,
    mikelward/repo#42).

    Returns the rule dicts as GitHub sent them. Raises RulesetError on a
    failed read. audit_cmd.py reads the same endpoint for much more than
    this; this is the narrow read setup_cmd.py and scaffold.py need."""
    try:
        # --paginate concatenates each page's own array rather than
        # merging them, so '.[]' unwraps per page into one rule per line
        # -- the same shape audit_cmd.py uses, and for the same reason.
        raw = gh.run(
            [
                "api",
                "--paginate",
                f"repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}",
                "--jq",
                ".[]",
            ]
        )
    except gh.GhError as e:
        raise RulesetError(f"reading {repo}'s effective rules for {branch}:\n{e.stderr}")
    found = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            rule = json.loads(line)
        except ValueError:
            raise RulesetError(f"reading {repo}'s effective rules for {branch}: unexpected response")
        if isinstance(rule, dict):
            found.append(rule)
    return found


def required_checks_in(rules):
    """The status checks the effective `rules` (see effective_rules)
    require, as (context, integration_id or None) pairs -- the same shape
    never_reported speaks, because the App a requirement is BOUND to is
    part of what it requires: an unbound entry is satisfied by any
    producer of that context, while a bound one names the single App that
    may report it. A caller reasoning about which workflow publishes a
    context must not read a same-named requirement bound to some other
    App as one of its own (Codex review, mikelward/repo#42)."""
    contexts = set()
    for rule in rules:
        if rule.get("type") != "required_status_checks":
            continue
        for entry in (rule.get("parameters") or {}).get("required_status_checks") or []:
            if entry.get("context"):
                contexts.add((entry["context"], entry.get("integration_id") or None))
    return contexts


def effective_required_checks(repo, branch):
    """Every status check `branch` actually requires: required_checks_in
    over effective_rules. Raises RulesetError on a failed read."""
    return required_checks_in(effective_rules(repo, branch))


def quoted(names):
    """Check names for a message, quoted and comma-separated. A name can
    contain spaces -- this repository has one called "Classify the diff" --
    so a space-joined list cannot be split back into names by eye, and the
    reader cannot tell one missing check from three."""
    return ", ".join(f"'{n}'" for n in names)


# How far back the evidence scan looks per pull request state: pages of
# a hundred, newest first (see _collect_reported).
_EVIDENCE_PAGES = 5

# What one run has already read about a commit's check runs and statuses,
# keyed by the exact `gh api` argv. One `repo setup` asks the evidence
# question up to five times (the preview, the binding's preview, the
# write, the binding's fingerprint, the binding's write), and the per-call
# bound above times five is a run that can spend the hour's API budget on
# a repository with a long history (Codex review, mikelward/repo#56). A
# pass on a commit does not un-happen, which is what makes a run-long
# memo of these reads safe: what the write's fresh recompute might miss
# is a pass that landed during the run, which the next run counts.
# Reset at the start of each command's run (see reset_evidence_cache).
_evidence_cache = {}


def reset_evidence_cache():
    """Forget what earlier reads in this process learned about commits'
    check runs and statuses. Called at the start of a command's run, so a
    long-lived process (the test suite) never carries one run's evidence
    into another's."""
    _evidence_cache.clear()


def _cached_run(args):
    """gh.run, memoized for this run on the exact argv (see
    _evidence_cache). Failures are not cached: a read that failed once is
    tried again where it is asked again."""
    key = tuple(args)
    if key not in _evidence_cache:
        _evidence_cache[key] = gh.run(args)
    return _evidence_cache[key]


def never_reported(repo, entries, ref=None):
    """Which of `entries` this repository has never reported, in the order
    given. A required check nothing posts blocks every merge.

    Each entry is (context, integration_id-or-None); pass None where the
    gate is not bound to a particular GitHub App. `ref` is the branch
    whose gates are in question, whose head is scanned first.

    Returns (context, integration_id, name_reported) triples.
    `name_reported` says the name itself has reported from some producer,
    which only matters for a bound gate: there the check is failing on the
    App, not the name, and a bare "never reported" reads as plainly false
    to a user who can see that check running.

    Raises RulesetError on a failed read: "never reported" and "could not
    tell" are different findings, and only the first is a gap."""
    entries = list(entries)
    found = _collect_reported(repo, set(entries), ref=ref)
    return [
        (context, integration_id, context in found.names)
        for context, integration_id in entries
        if not found.satisfied((context, integration_id), passed=False)
    ]


def never_passed(repo, entries, ref=None, shas=None, passing=frozenset({"success"})):
    """Which of `entries` this repository has never reported a `success`
    for, in the order given -- the question a ruleset write asks before
    requiring a check (SPEC.md, *The ladder*): a check that has run and
    failed blocks every merge exactly as one that has never run does.
    With `shas`, the question is asked of those commits alone (see
    _collect_reported) -- what a pull request's head still lacks.

    Returns (context, integration_id, name_reported) triples, the same
    shape as never_reported, so describe_missing and bound_to_another_app
    read both. Raises RulesetError on a failed read, for the same reason."""
    entries = list(entries)
    found = _collect_reported(repo, set(entries), ref=ref, passed=True, shas=shas, passing=passing)
    return [
        (context, integration_id, context in found.names)
        for context, integration_id in entries
        if not found.satisfied((context, integration_id), passed=True)
    ]


def bound_to_another_app(item):
    """True when a missing check's name does report, just never from the
    App its gate is bound to -- a different problem with a different fix
    from a check nothing produces at all."""
    _, integration_id, name_reported = item
    return integration_id is not None and name_reported


def describe_missing(items):
    """Missing checks for a message. Quoted like `quoted`, and for a gate
    whose name reports from the wrong producer, naming the App it is bound
    to -- otherwise the reader is told a check they can watch running has
    never reported."""
    return ", ".join(
        f"'{context}' (needs App {integration_id})"
        if bound_to_another_app((context, integration_id, name_reported))
        else f"'{context}'"
        for context, integration_id, name_reported in items
    )


def deferral_reason(item):
    """Why a check a write would have newly required is deferred instead,
    for a plan line: which of the three states it is in decides what a
    reader does about it. A check that has never run needs a pull request
    to run on; one that has run and never passed needs the failure fixed;
    a bound check the App has not published yet needs the App's
    credential to reach the publisher."""
    context, integration_id, name_reported = item
    if integration_id is not None:
        return (
            f"'{context}' has not passed here as App {integration_id} yet"
            + (" (it passes from another publisher)" if name_reported else "")
        )
    if name_reported:
        return f"'{context}' has run here but never passed"
    return f"'{context}' has never run here"


def _lookup_ruleset_ids(repo, ruleset_name, include_parents=False):
    """Every ruleset id on `repo` named `ruleset_name`, oldest first.

    A list, not one id: GitHub does not make a ruleset's name unique
    within a repository, so two can carry the same one and both apply
    (Codex review, mikelward/repo#31). Collapsing them here would let a
    run report a name handled while a second ruleset under it kept
    aggregating.

    A failed lookup must never read as "there is no ruleset": that would
    create a duplicate that ANDs with a real one this run simply couldn't
    see -- so it raises RulesetError instead."""
    try:
        out = gh.run(
            [
                "api",
                "--paginate",
                f"repos/{repo}/rulesets?includes_parents={'true' if include_parents else 'false'}",
                "--jq",
                f".[] | select(.name == {_json_string(ruleset_name)}) | .id",
            ]
        )
    except gh.GhError as e:
        error_lines(
            f"could not list {repo}'s rulesets, so cannot tell whether "
            f"'{ruleset_name}' already exists. Creating one now could duplicate "
            "it, and two rulesets AND together -- the stale one would keep "
            "requiring checks this run was meant to replace.",
            e.stderr,
        )
        raise RulesetError()
    return [line.strip() for line in out.splitlines() if line.strip()]


def _lookup_existing_ruleset(repo, ruleset_name):
    """The id of the ruleset this run will write under `ruleset_name`, or
    None. The first of several sharing the name -- which is a real
    possibility, see _lookup_ruleset_ids. The others are reported by
    find_duplicate_standard_rulesets and left alone: the standard is a
    floor (see TODO.md)."""
    ids = _lookup_ruleset_ids(repo, ruleset_name)
    return ids[0] if ids else None


def find_rulesets_named(repo, ruleset_name=DEFAULT_RULESET_NAME, include_parents=False):
    """Every ruleset id on `repo` carrying `ruleset_name`, oldest first.

    include_parents=True counts an org- or enterprise-level ruleset of the
    same name too. That is the right question for a READ: an inherited one
    aggregates with the repository's own, so leaving it out reports "just
    the one" over two that both apply (Codex review, mikelward/repo#33).
    It is the wrong question for a write, which is why this module's own
    calls leave it False -- `repo setup` can only ever adopt, update or
    delete a ruleset the repository owns.

    All of them, not just the ones after the first: none and exactly one
    are different answers, and a caller handed only the extras cannot tell
    them apart -- which is how "exactly one ruleset is named 'main'" got
    printed for a repository that has none (Codex review,
    mikelward/repo#33).

    Public for the same reason find_legacy_rulesets is: `repo audit` asks
    the same question read-only, and asking it twice from two definitions
    is how the two come to disagree."""
    return _lookup_ruleset_ids(repo, ruleset_name, include_parents=include_parents)


def find_legacy_rulesets(repo, ruleset_name=DEFAULT_RULESET_NAME):
    """[(name, id)] for every ruleset on `repo` carrying a name this tool
    used before `ruleset_name`. Raises on a failed lookup, for the same
    reason _lookup_existing_ruleset does.

    Repository-owned only (includes_parents=false, via
    _lookup_existing_ruleset): an org- or enterprise-level ruleset that
    happens to share a legacy name is not this tool's to adopt, rename or
    delete, so reporting it would only send a reader looking for something
    they cannot act on here. Public because `repo audit` reports the same
    finding read-only, and reading the list twice from two definitions is
    how the two drift."""
    found = []
    for legacy_name in LEGACY_RULESET_NAMES:
        if legacy_name == ruleset_name:
            continue
        # Every id under the name, not just the first: two rulesets can
        # share one, and reporting or deleting only one of them would
        # leave the other applying with nothing to say so (Codex review,
        # mikelward/repo#31).
        for rid in _lookup_ruleset_ids(repo, legacy_name):
            found.append((legacy_name, rid))
    return found


def _lookup_legacy_ruleset(repo, ruleset_name):
    """(name, id) of the first ruleset carrying a name this tool used
    before `ruleset_name`, or (None, None) -- what an adoption renames.

    One, deliberately: a rename can only make one of them the standard
    ruleset. Where two share a legacy name, the second is still a legacy
    ruleset on the next run -- beside the standard one this run just made
    -- and the deletion path handles it there. Converging in two runs
    beats renaming both to the same name in one."""
    for legacy_name, rid in find_legacy_rulesets(repo, ruleset_name):
        return legacy_name, rid
    return None, None


def _resolve_ruleset(repo, ruleset_name):
    """(id, adopted_legacy_name, duplicate_ids): the ruleset this run will
    write, and any others already carrying the same managed name.

    Prefers one already carrying the standard name. Failing that, adopts a
    legacy-named one -- the write renames it in place, which keeps its
    bypass actors, its scope and any rule type outside MANAGED_RULE_TYPES,
    all of which a create-a-new-one-and-leave-the-old would have stranded
    behind a second, aggregating ruleset. A falsy id means create.

    duplicate_ids comes out of the same lookup rather than a second one:
    an extra by-name lookup here was a real race once (see setup_cmd.py's
    own docstring on the fingerprint), and a report is no reason to
    reintroduce the shape of it."""
    ids = _lookup_ruleset_ids(repo, ruleset_name)
    if ids:
        return ids[0], None, ids[1:]
    legacy_name, legacy_id = _lookup_legacy_ruleset(repo, ruleset_name)
    return legacy_id, legacy_name, []


def _comparable_ruleset(body):
    """A ruleset's content as one comparable string: everything GitHub
    reports about it except the fields that identify this particular copy
    (see _VOLATILE_FIELDS) and its name.

    Whole-object, not field-by-field. "Is A at least as strict as B" was
    reimplemented per field five times over, and each round of review
    found another field the previous one missed -- an unmanaged rule type,
    a ref the other did not cover, a stricter approval count, a required
    check bound to a specific App via integration_id, a bypass actor
    present on one side only (see TODO.md). An equality test over the
    whole object cannot be one item short. What it costs is keeping a
    duplicate that differs harmlessly -- a reordered bypass-actor list,
    say -- which is the safe direction to be wrong in."""
    trimmed = {k: v for k, v in body.items() if k not in _VOLATILE_FIELDS and k != "name"}
    return json.dumps(trimmed, sort_keys=True)


def _plan_legacy_deletion(repo, ruleset_name, existing, adopted_legacy, target_body):
    """(deletable, differing): every legacy-named ruleset this run will
    delete once the standard one is written, and which of them hold
    something the standard one will not.

    A legacy name is this tool's own former name for the standard ruleset
    (LEGACY_RULESET_NAMES). Converging on one ruleset is the whole point
    of the rename, so all of them go -- not only the ones that came out
    byte-identical (maintainer, 2026-09-07).

    Identity used to be the gate, because "is A at least as strict as B"
    was reimplemented per field five times over and each round of review
    found a field the last one missed (see TODO.md), and a false "adds
    nothing" deletes a ruleset that was holding the branch up. What made
    that unrecoverable was that GitHub hands back no copy of a deleted
    ruleset. It does now: the body is recorded before the delete (see
    apply_ruleset's `record`), so restoring one is a POST of the JSON in
    the log. With the cost recoverable, the subset question stops being
    load-bearing -- and no comparison this module can get wrong decides
    anything.

    `differing` is only for what the plan SAYS. Getting it incomplete
    costs a vaguer warning, never a wrong deletion, so the whole-object
    equality test is fine here where it was not fine as a gate.

    Nothing is planned when this run is itself adopting a legacy ruleset
    (it is becoming the standard one, not being superseded by it) or when
    there is no standard ruleset yet."""
    if adopted_legacy or not existing or not target_body:
        return [], []
    wanted = _comparable_ruleset(target_body)
    deletable, differing = [], []
    for legacy_name, legacy_id in find_legacy_rulesets(repo, ruleset_name):
        try:
            raw = gh.run(["api", f"repos/{repo}/rulesets/{legacy_id}"])
        except gh.GhError as e:
            error_lines(
                f"could not read ruleset '{legacy_name}' (id {legacy_id}) on {repo} to "
                "tell what it holds. Refusing to delete a ruleset this run could not "
                "read: the recorded body is what makes the deletion reversible, and "
                "there would be nothing to record.",
                e.stderr,
            )
            raise RulesetError()
        deletable.append((legacy_name, legacy_id))
        if _comparable_ruleset(json.loads(raw)) != wanted:
            differing.append((legacy_name, legacy_id))
    return deletable, differing


def _still_deletable(repo, ruleset_name, survivor_id, legacy_name, legacy_id):
    """The legacy ruleset's body if deleting it is STILL safe, else None --
    asked immediately before the delete rather than from the plan's own
    snapshot, and returning the body so the caller records exactly what it
    is about to remove rather than a copy read a round trip earlier.

    What the fresh read has to establish is no longer "is it identical",
    which stopped deciding anything (see _plan_legacy_deletion). It is
    that the two rulesets are still the two this run reasoned about. An
    administrator renaming the duplicate to `ruleset_name` -- and the
    survivor away from it -- inside this window, which spans the
    survivor's own write and so is a network round trip wide rather than
    an instant, would otherwise have the newly canonical ruleset deleted
    and the repository left with nothing under that name at all (Codex
    review, mikelward/repo#31). Names are the whole check now, and they
    have to be read rather than assumed for exactly that reason.

    A failed read is not "unchanged": it returns None, and the caller
    reports the duplicate as kept rather than deleting on an answer it
    could not get -- and with nothing to record, which is the same
    objection _plan_legacy_deletion makes about an unreadable ruleset."""
    try:
        survivor = json.loads(gh.run(["api", f"repos/{repo}/rulesets/{survivor_id}"]))
        candidate = json.loads(gh.run(["api", f"repos/{repo}/rulesets/{legacy_id}"]))
    except gh.GhError as e:
        error_lines(
            f"could not re-read '{ruleset_name}' (id {survivor_id}) and '{legacy_name}' "
            f"(id {legacy_id}) on {repo} to confirm which is which. Not deleting it.",
            e.stderr,
        )
        return None
    if survivor.get("name") != ruleset_name or candidate.get("name") != legacy_name:
        return None
    return candidate


def _report_duplicate_standard(repo, ruleset_name, existing, extras):
    """Says so when more than one ruleset carries the managed name.

    Reported, not resolved: the standard is a floor (see TODO.md), so an
    extra is not a half-done repository.

    The message reports the name and points at the extra, claiming
    nothing about what it does -- this lookup filters by name and never
    reads enforcement, scope or rules."""
    for rid in extras:
        warn(
            f"{repo}: note -- more than one ruleset is named '{ruleset_name}'; this run "
            f"writes id {existing} and leaves id {rid} alone. Worth "
            f"reading id {rid} to see what it says."
        )


def _record_deleted_ruleset(record, repo, legacy_name, legacy_id, body):
    """Write the ruleset about to be deleted where it can be read back.

    `record` is the caller's log writer (setup_cmd's `_Log.write`), which
    keeps this out of the terminal: it is a wall of JSON, and #45's whole
    point was that the terminal says what changed while the file holds the
    record. With no log -- `--no-log`, or a caller that passes nothing --
    it goes to stdout instead, because the operator opted out of the file,
    not out of being able to undo this.

    Raises OSError if it cannot be written, which the caller turns into
    "not deleting it": an unrecorded delete is the unrecoverable one.

    The GitHub-generated fields go (_VOLATILE_FIELDS -- id, node_id,
    source, the timestamps, _links), because what is recorded has to be
    what a POST accepts: the same read-only fields the update path already
    strips before a PUT would have this rejected, so a record advertised
    as restorable would not have restored anything (Codex review,
    mikelward/repo#46). The name stays, unlike in _comparable_ruleset --
    that drops it to compare two rulesets, where this is recreating one."""
    payload = json.dumps(
        {k: v for k, v in body.items() if k not in _VOLATILE_FIELDS},
        indent=2,
        sort_keys=True,
    )
    text = (
        f"--- deleting ruleset '{legacy_name}' (id {legacy_id}) on {repo}; "
        f"POST this back to repos/{repo}/rulesets to restore it ---\n{payload}\n"
    )
    if record is None:
        # Same durability as the log's own write_now: print() can return
        # with the bytes still in Python's buffer, and a full disk or a
        # broken pipe would then surface after the ruleset is already gone
        # (Codex review, mikelward/repo#46). Any failure propagates as the
        # OSError the caller turns into "not deleting it".
        print(text, end="")
        sys.stdout.flush()
    else:
        record(text)


def _report_differing_legacy(repo, ruleset_name, differing):
    """Says so for each legacy-named ruleset that is about to be deleted
    while holding something the standard one will not.

    Deleting it is still right -- converging on one ruleset is the point,
    and rulesets aggregate, so leaving it means both apply forever. But
    "identical" and "merely superseded" are different facts about what the
    repository loses, and the operator is owed the difference. The body is
    recorded before the delete, so this points at that rather than asking
    anyone to have memorized it."""
    for legacy_name, legacy_id in differing:
        warn(
            f"{repo}: note -- '{legacy_name}' (id {legacy_id}) is NOT identical to "
            f"'{ruleset_name}'; deleting it drops whatever it held that the other does "
            "not. Its full body is recorded first, so it can be restored by POSTing "
            f"that JSON back to repos/{repo}/rulesets."
        )


def _check_ruleset_ownership(repo, ruleset_id, ruleset_name):
    """Whether a ruleset found under a name this module writes is one it
    can actually write.

    Two ways it isn't. One GitHub does not actively enforce would report a
    gate that does not gate. A tag-targeted one would take the branch
    rules added here and be rejected on PUT -- after the dry run promised
    the update, and after earlier `repo setup` steps have written (Codex
    review, mikelward/repo#29).

    Rule types outside MANAGED_RULE_TYPES are not one of those ways: an
    update edits only those four and copies the rest through untouched."""
    try:
        out = gh.run(
            ["api", f"repos/{repo}/rulesets/{ruleset_id}", "--jq", ".enforcement, .target"]
        )
    except gh.GhError as e:
        error_lines(
            f"could not read ruleset '{ruleset_name}' (id {ruleset_id}) to check "
            "what it contains. Refusing to overwrite rules that cannot be inspected.",
            e.stderr,
        )
        return False
    lines = out.splitlines()
    if len(lines) < 2:
        error(f"could not read ruleset '{ruleset_name}' (id {ruleset_id}): empty response")
        return False
    enforcement, target = lines[0].strip(), lines[1].strip()
    if target != "branch":
        error(f"ruleset '{ruleset_name}' (id {ruleset_id}) on {repo} targets '{target}',")
        error("not 'branch'. The rules this writes are branch rules; GitHub would")
        error("reject them on a ruleset of that target. Rename it, or point this run")
        error("at a different one.")
        return False
    if enforcement != "active":
        error(f"ruleset '{ruleset_name}' (id {ruleset_id}) on {repo} is '{enforcement}', not")
        error("'active', so its rules block nothing. Setting a check list on it")
        error("would report a gate that does not gate. Activate it, or point this")
        error("run at a different ruleset.")
        return False
    return True


def _widen_include(include):
    """(include, added): the ref_name include list a write will carry --
    whatever the ruleset already names, plus any of the hardened three it
    does not.

    Existing entries are kept rather than replaced, so a ruleset also
    covering some release branch keeps covering it: this only ever widens.
    A ruleset carrying ~ALL is left alone -- it already covers every
    branch, and appending three refs it subsumes would be noise.

    Exclusions are NOT touched. One naming a ref this adds would defeat
    the widening, which is why _excluded_hardened_refs reports it rather
    than this quietly deleting an exclusion somebody meant."""
    include = list(include)
    if "~ALL" in include:
        return include, []
    added = [ref for ref in _HARDENED_INCLUDE if ref not in include]
    return include + added, added


def _excluded_hardened_refs(exclude, default_branch):
    """(named, unevaluated): which of the hardened refs a ruleset's own
    exclusions carve back out of the scope a write leaves it with, and
    whether a glob exclusion makes that unanswerable.

    Widening the include list is not the whole answer to "does this
    ruleset cover all three?" -- an exclusion outranks an include, so a
    ruleset including ~ALL (or all three literally) while excluding
    refs/heads/master leaves master exactly as unprotected as before,
    and nothing about the include list says so (Codex review,
    mikelward/repo#31). ~DEFAULT_BRANCH is resolved against the real
    default branch first, since an exclusion names a ref, not a token.

    Both sides are normalized, not just the wanted one: an exclusion may
    itself be written `~DEFAULT_BRANCH`, and comparing that token against
    the resolved `refs/heads/<default>` would miss a ruleset excluding the
    very branch this protects. `~ALL` on the exclusion side carves out
    every branch, these three included. audit_cmd._branch_coverage_verdict
    already treats both tokens as effective in `exclude`, so recognizing
    them here keeps the two commands answering the same question (Codex
    review, mikelward/repo#31).

    Reported by the entry as written, not as normalized, so the ref named
    is the one to go looking for in the ruleset.

    Literal matching otherwise, and a glob is reported as unevaluated
    rather than guessed at -- the same discipline as
    _find_merge_method_conflicts, for the same reason: this module does not
    reimplement GitHub's ref matching."""
    exclude = list(exclude or [])
    wanted = set(_normalize_refs(_HARDENED_INCLUDE, default_branch))
    named = [
        ref
        for ref in exclude
        if ref == "~ALL" or _normalize_refs([ref], default_branch)[0] in wanted
    ]
    return named, _has_glob(exclude)


def _effective_scope_added(scope_added, exclude, default_branch):
    """(covered, unevaluated): which of the refs a widening adds the
    ruleset will actually govern, and whether a glob exclusion makes that
    unanswerable.

    An exclusion outranks an include, so a widening that adds
    refs/heads/master to a ruleset already excluding it changes the
    include list and protects nothing. Claiming otherwise would put the
    plan at odds with _report_excluded_hardened, which says on the same
    run that the branch stays open (Codex review, mikelward/repo#45)."""
    exclude = list(exclude or [])
    if "~ALL" in exclude:
        return [], False
    excluded = set(_normalize_refs(exclude, default_branch))
    covered = [
        ref
        for ref in scope_added
        if _normalize_refs([ref], default_branch)[0] not in excluded
    ]
    return covered, _has_glob(exclude)


def _report_excluded_hardened(repo, ruleset_name, target_body, default_branch):
    """Says so when a ruleset's exclusions still keep one of the hardened
    refs out. Reported, never fixed: deleting an exclusion somebody wrote
    is a narrowing decision this module does not make, and `repo audit`
    already counts the same state as a [GAP] (see
    audit_cmd._targeting_status). Silence was the real problem -- an
    already-matching ruleset excluding refs/heads/master reported
    "nothing to do" while master stayed open."""
    ref_name = ((target_body or {}).get("conditions") or {}).get("ref_name") or {}
    named, unevaluated = _excluded_hardened_refs(ref_name.get("exclude"), default_branch)
    if named:
        warn(
            f"{repo}: ruleset '{ruleset_name}' excludes {', '.join(named)}, so it does not "
            "protect that branch whatever its include list says. An exclusion is never edited "
            "here -- remove it by hand, or delete the branch it names. `repo audit` reports "
            "this as a gap."
        )
    if unevaluated:
        warn(
            f"{repo}: ruleset '{ruleset_name}' has a pattern in its exclusions, so whether it "
            "still covers the default branch, refs/heads/main and refs/heads/master cannot be "
            "checked here without reimplementing GitHub's ref matching. Check it by hand."
        )


def _compute_scope(repo, existing_id):
    """The branch conditions the write will target: the hardened three-ref
    set on create, and on update the existing ruleset's own conditions
    WIDENED to include them (see _widen_include).

    Post-widening, not as-found, because this is what the merge-method
    conflict scan evaluates: a write that newly brings refs/heads/master
    into scope has to be checked against the other rulesets covering
    master, not against the narrower scope it is replacing."""
    if not existing_id:
        return {"include": list(_HARDENED_INCLUDE), "exclude": []}
    try:
        out = gh.run(
            [
                "api",
                f"repos/{repo}/rulesets/{existing_id}",
                "--jq",
                "{include: [.conditions.ref_name.include[]?], "
                "exclude: [.conditions.ref_name.exclude[]?]} | @json",
            ]
        ).strip()
    except gh.GhError as e:
        error_lines(
            f"could not read ruleset (id {existing_id}) to see which branches it "
            "covers. Refusing to guess at what else applies there.",
            e.stderr,
        )
        raise RulesetError()
    scope = json.loads(out)
    scope["include"], _ = _widen_include(scope.get("include") or [])
    return scope


def _normalize_refs(refs, default_branch):
    return [f"refs/heads/{default_branch}" if r == "~DEFAULT_BRANCH" else r for r in refs]


def _has_glob(refs):
    return any(_GLOB_CHARS_RE.search(r) for r in refs)


def _find_merge_method_conflicts(repo, scope, existing_id, default_branch):
    """Rulesets AGGREGATE: a pull request must satisfy every one that
    applies to the branch, and allowed_merge_methods INTERSECTS across
    them. Returns (definite, undecidable) -- ruleset names that plainly
    exclude rebase and overlap `scope`'s branches, and ones whose scope
    uses a pattern or an exclusion this module's literal matching cannot
    safely evaluate. Both are reported rather than silently passed over:
    an unevaluated ruleset might still exclude rebase on this branch, and
    guessing wrong in either direction is worse than asking a human to
    check by hand."""
    try:
        out = gh.run(
            ["api", "--paginate", f"repos/{repo}/rulesets?includes_parents=true", "--jq", ".[].id"]
        )
    except gh.GhError as e:
        error_lines(
            f"could not read {repo}'s other rulesets, so cannot tell whether one of "
            "them rules out a rebase merge. Refusing to guess: rulesets aggregate, "
            "and a conflict would leave no way to merge anything.",
            e.stderr,
        )
        raise RulesetError()
    ids = [line.strip() for line in out.splitlines() if line.strip()]

    ours_include = _normalize_refs(scope.get("include") or [], default_branch)
    ours_exclude = scope.get("exclude") or []

    definite, undecidable = [], []
    for rid in ids:
        if not rid or rid == existing_id:
            continue
        try:
            ruleset = json.loads(gh.run(["api", f"repos/{repo}/rulesets/{rid}"]))
        except gh.GhError as e:
            error_lines(f"could not read ruleset (id {rid}) on {repo}:", e.stderr)
            raise RulesetError()
        if ruleset.get("enforcement") != "active":
            continue
        excludes_rebase = any(
            rule.get("type") == "pull_request"
            and (rule.get("parameters") or {}).get("allowed_merge_methods")
            and "rebase" not in rule["parameters"]["allowed_merge_methods"]
            for rule in ruleset.get("rules") or []
        )
        if not excludes_rebase:
            continue
        conditions = (ruleset.get("conditions") or {}).get("ref_name") or {}
        theirs_include = _normalize_refs(conditions.get("include") or [], default_branch)
        theirs_exclude = conditions.get("exclude") or []
        name = ruleset.get("name") or f"id {rid}"

        if _has_glob(ours_include + theirs_include) or ours_exclude or theirs_exclude:
            undecidable.append(name)
        elif "~ALL" in ours_include or "~ALL" in theirs_include or (
            set(ours_include) & set(theirs_include)
        ):
            definite.append(name)
    return definite, undecidable


def _validate_merge_method_scope(repo, existing_id, default_branch):
    scope = _compute_scope(repo, existing_id)
    definite, undecidable = _find_merge_method_conflicts(repo, scope, existing_id, default_branch)
    if definite:
        error(f"another active ruleset on {repo} excludes rebase from its allowed")
        error("merge methods, and covers the same branches as this one:")
        for n in definite:
            error(f"  {n}")
        error("Rulesets aggregate, so together they would leave no method that")
        error("satisfies both and nothing could merge. Reconcile them first.")
        raise RulesetError()
    if undecidable:
        error(f"another active ruleset on {repo} excludes rebase from its allowed")
        error("merge methods, on a scope this module cannot evaluate:")
        for n in undecidable:
            error(f"  {n}")
        error("Its conditions use a pattern (such as 'refs/heads/*') or an")
        error("exclusion, and this matches branch names literally rather than")
        error("reimplementing GitHub's ref matching. Check it, then narrow either")
        error("scope.")
        raise RulesetError()


def _as_entries(checks):
    """Normalize a `checks` argument into (context, integration_id-or-None)
    pairs, order preserved. Each item is either a bare context name (a name
    off the command line, unbound: any producer counts) or an already-normalized
    (context, integration_id) pair -- so this is idempotent and every internal
    consumer can normalize its own input without caring which form reached it."""
    entries = []
    for c in checks:
        if isinstance(c, str):
            entries.append((c, None))
        else:
            context, integration_id = c
            entries.append((context, integration_id or None))
    return entries


def _check_entry(context, integration_id):
    """One `required_status_checks` entry. An integration_id binds the
    requirement to a single GitHub App -- only a check that App itself
    produced satisfies it -- and is omitted entirely when None, since GitHub
    treats a present-but-null integration_id differently from an absent one."""
    entry = {"context": context}
    if integration_id is not None:
        entry["integration_id"] = integration_id
    return entry


def _context_label(context, integration_id):
    """A required check for a plan line, naming the App it is bound to when it
    is -- so restricting a check to one App reads as the enforcement change it
    is, not a bare `lanes` that the ruleset already required by name."""
    return f"{context} (App {integration_id})" if integration_id else context


def _binding_map(target_body):
    """context -> integration_id for the required checks in a ruleset body,
    so a plan line can name the App a check is being bound to."""
    mapping = {}
    for rule in (target_body or {}).get("rules") or []:
        if rule.get("type") == "required_status_checks":
            for entry in (rule.get("parameters") or {}).get("required_status_checks") or []:
                if entry.get("context"):
                    mapping[entry["context"]] = entry.get("integration_id")
    return mapping


def _create_body(ruleset_name, checks, deferred=()):
    """The body a fresh ruleset is POSTed with. `deferred` names the
    checks left out of it this time (see apply_ruleset's `defer`): a
    create requires nothing yet that nothing can satisfy, and a later
    update adds each one once it can be."""
    return {
        "name": ruleset_name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": list(_HARDENED_INCLUDE), "exclude": []}},
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        _check_entry(context, integration_id)
                        for context, integration_id in _as_entries(checks)
                        if context not in deferred
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


_VOLATILE_FIELDS = (
    "id",
    "node_id",
    "source",
    "source_type",
    "created_at",
    "updated_at",
    "_links",
    "current_user_can_bypass",
)


def _enforced_lines(target_rules, indent="  "):
    """Every managed protection the ruleset holds once this write lands,
    in the wording _describe_plan uses.

    The resulting state, not the delta -- what a create prints (there
    everything is new, so the state IS the change), what --verbose and the
    log print for an update, and what a scope widening makes newly
    effective on the refs it adds. Derived from the body about to be
    written rather than restated as a fixed list, so it stays true of a
    ruleset carrying something this module did not put there."""
    now = {r.get("type"): (r.get("parameters") or {}) for r in target_rules or []}
    sc = now.get("required_status_checks") or {}
    pr = now.get("pull_request")
    lines = []
    contexts = [
        _context_label(entry.get("context"), entry.get("integration_id"))
        for entry in sc.get("required_status_checks") or []
        if entry.get("context")
    ]
    if contexts:
        lines.append(f"{indent}required checks: " + ", ".join(contexts))
    if pr is not None and pr.get("required_review_thread_resolution"):
        lines.append(f"{indent}review conversations must be resolved")
    if sc.get("strict_required_status_checks_policy"):
        lines.append(f"{indent}the branch must be up to date with the base")
    if pr is not None and pr.get("allowed_merge_methods"):
        lines.append(f"{indent}rebase is the only merge method")
    if "required_linear_history" in now:
        lines.append(f"{indent}commit history must be linear")
    if "non_fast_forward" in now:
        lines.append(f"{indent}force pushes are blocked")
    # A rule this module does not manage -- required_signatures,
    # required_deployments, whatever else a hand-made ruleset carried --
    # is preserved by an update and becomes effective on every ref the
    # write covers, this one's widening included. Naming it without
    # describing it is the honest half: the reader needs to know it is
    # there, and this module has no wording for a rule it never writes
    # (Codex review, mikelward/repo#45).
    unmanaged = sorted(t for t in now if t and t not in MANAGED_RULE_TYPES)
    if unmanaged:
        lines.append(
            f"{indent}plus rules this tool does not manage, kept as they are: "
            + ", ".join(unmanaged)
        )
    return lines


def _newly_enforced(original_rules, target_rules):
    """The managed protections this write actually adds, in the wording
    _describe_plan uses.

    An update's plan used to recite every rule the ruleset would end up
    holding, most of which it already held. That buried the one or two
    lines that were the change (maintainer, 2026-09-07). A create still
    lists everything, because on a create everything is new.
    """
    was = {r.get("type"): (r.get("parameters") or {}) for r in original_rules or []}
    now = {r.get("type"): (r.get("parameters") or {}) for r in target_rules or []}
    added = []
    if "pull_request" in now and "pull_request" not in was:
        added.append("a pull request is required before merging")
    pr_was, pr_now = was.get("pull_request") or {}, now.get("pull_request") or {}
    if pr_now.get("required_review_thread_resolution") and not pr_was.get(
        "required_review_thread_resolution"
    ):
        added.append("review conversations must be resolved")
    if pr_now.get("allowed_merge_methods") != pr_was.get("allowed_merge_methods"):
        added.append("rebase is the only merge method")
    sc_was, sc_now = was.get("required_status_checks") or {}, now.get(
        "required_status_checks"
    ) or {}
    if sc_now.get("strict_required_status_checks_policy") and not sc_was.get(
        "strict_required_status_checks_policy"
    ):
        added.append("the branch must be up to date with the base")
    for rule_type, description in (
        ("required_linear_history", "commit history must be linear"),
        ("non_fast_forward", "force pushes are blocked"),
    ):
        if rule_type in now and rule_type not in was:
            added.append(description)
    return added


def _covers_default_branch(include, default_branch):
    """Whether a ruleset's include list is KNOWN to reach the default
    branch already: `~ALL`, `~DEFAULT_BRANCH`, or the branch by name. A
    glob is not evaluated (this module never guesses at GitHub's pattern
    semantics) and so reads as not covering it -- fail closed: the one
    caller uses "already covered" to let a widening through while the
    ruleset requires a check that cannot pass on the branch, and a glob
    that in fact misses the branch (`refs/heads/release/*`) would let
    that widening wedge every merge (Codex review, mikelward/repo#56).
    The cost of the closed reading is a refused write, said with what
    has to land first, on a ruleset whose glob did cover the branch."""
    include = list(include or [])
    if "~ALL" in include:
        return True
    return f"refs/heads/{default_branch}" in _normalize_refs(include, default_branch)


def _widening_hazards(rules, contexts):
    """What in a ruleset's existing rules could block a pull request on a
    branch the ruleset is about to be widened onto, as reasons for the
    refusal in _build_update_body: required checks (`contexts`), an
    approval requirement of any kind (this tool's own pull requests get
    no approver), and any rule type this module does not write, which it
    cannot reason about (a required signature would refuse the tool's own
    API commits; a required deployment or workflow may never run there)."""
    hazards = []
    if contexts:
        hazards.append(f"it requires {quoted(contexts)}")
    for rule in rules:
        kind = rule.get("type")
        params = rule.get("parameters") or {}
        if kind == "pull_request":
            if params.get("required_approving_review_count"):
                hazards.append(
                    f"it requires {params['required_approving_review_count']} approving review(s)"
                )
            if params.get("require_code_owner_review"):
                hazards.append("it requires a code owner's review")
            if params.get("require_last_push_approval"):
                hazards.append("it requires approval of the last push")
        elif kind not in MANAGED_RULE_TYPES:
            hazards.append(f"it carries a '{kind}' rule this tool does not manage")
    return hazards


def _build_update_body(repo, existing_id, checks, ruleset_name, deferred=(), default_branch=None):
    """UPDATE does not build a body from scratch: it fetches the existing
    ruleset and edits only the managed rules inside it (plus the ref_name
    include list, see below), since a PUT replaces the whole object and
    this module does not know every field GitHub puts there (enforcement,
    bypass_actors, and so on all stay exactly as they were). An entry's
    integration_id -- which binds a required check to a specific GitHub
    App -- is preserved by reusing the existing entry for any context that
    already has one, rather than rebuilding from names alone.

    `deferred` names checks this write must not NEWLY require (see
    apply_ruleset's `defer`). A deferred check the ruleset already
    requires stays required exactly as it is -- unbound, or bound to
    whatever App it was -- since leaving it alone changes nothing about
    what the branch enforces; one it does not require yet is left out,
    and one whose only novelty is an App binding keeps its existing entry.
    Nothing is ever removed: a check the ruleset requires that `checks`
    does not name stays (`checks_kept`), since the standard is a floor.
    The one shape refused instead (RulesetError, reported) is a ruleset
    that does not reach `default_branch` yet and carries anything that
    could block a pull request there (_widening_hazards): the widening
    would make it newly effective on the branch with nothing to say the
    branch can satisfy it -- keeping it can strand the branch, dropping
    it loosens whatever the ruleset was protecting -- so neither is
    written, and widening it is a person's call.

    The one thing an update does rewrite outside `rules` is the ref_name
    include list, widened to carry the hardened three refs (see
    _widen_include). Leaving it alone -- as this did until now -- meant a
    hand-made ruleset kept whatever narrow scope it was given while a
    freshly created one got all three, so the master backdoor this module
    exists to close stayed open on exactly the repositories that already
    had a ruleset. Widening only ever adds: an entry the ruleset already
    names, an exclusion, and every other condition key (repository
    properties, say) are untouched.

    Returns (changed, target, had_pull_request, scope_added, checks_added)
    -- target is the full object to PUT, `changed` is whether it differs
    from what's there now, scope_added lists the refs the widening
    appended, and checks_added the required checks this write would name
    that the ruleset does not require today."""
    try:
        raw = gh.run(["api", f"repos/{repo}/rulesets/{existing_id}"])
    except gh.GhError as e:
        error_lines(f"could not read ruleset '{ruleset_name}' (id {existing_id}):", e.stderr)
        raise RulesetError()
    original = json.loads(raw)
    for field in _VOLATILE_FIELDS:
        original.pop(field, None)

    entries = _as_entries(checks)
    # Contexts the ruleset requires TODAY, and the App each is bound to, so the
    # caller can be told which of `checks` this write would newly require -- a
    # distinction that matters to a caller holding back a write until something
    # that can satisfy the new requirement exists (setup_cmd.py's
    # pending-scaffold gate, Codex review, mikelward/repo#42). Binding an
    # already-required check to an App is a new requirement too: nothing may
    # have reported it AS that App yet.
    existing_contexts = set()
    # context -> the entries the ruleset carries for it, in its order, and
    # context -> the App ids they are bound to (None for an unbound one).
    # By (context, App), never by context alone: GitHub lets one name be
    # required from two Apps as two entries, and collapsing them to one
    # silently dropped the other on every update -- the one thing the
    # floor promises never happens (Codex review, mikelward/repo#56).
    have_entries = {}
    existing_bindings = {}
    has_status_checks = False
    has_pull_request = False
    has_linear_history = False
    has_non_fast_forward = False
    new_rules = []
    for rule in original.get("rules") or []:
        rule = dict(rule)
        if rule.get("type") == "required_status_checks":
            has_status_checks = True
            params = dict(rule.get("parameters") or {})
            have_entries = {}
            for h in params.get("required_status_checks") or []:
                have_entries.setdefault(h.get("context"), []).append(h)
            existing_contexts = set(have_entries)
            existing_bindings = {
                context: {(h.get("integration_id") or None) for h in hs}
                for context, hs in have_entries.items()
            }
            rule["parameters"] = params
        elif rule.get("type") == "pull_request":
            has_pull_request = True
            params = dict(rule.get("parameters") or {})
            params["required_review_thread_resolution"] = True
            params["allowed_merge_methods"] = ["rebase"]
            rule["parameters"] = params
        elif rule.get("type") == "required_linear_history":
            has_linear_history = True
        elif rule.get("type") == "non_fast_forward":
            has_non_fast_forward = True
        new_rules.append(rule)

    # Newly required: a context the ruleset does not require today, OR one it
    # requires but not yet bound to the App this write binds it to -- both are
    # requirements nothing may have satisfied AS asked yet, which is what the
    # never-reported hold reads this for.
    def _newly_required(context, integration_id):
        if context not in existing_contexts:
            return True
        return integration_id is not None and integration_id not in existing_bindings[context]

    conditions = dict(original.get("conditions") or {})
    ref_name = dict(conditions.get("ref_name") or {})
    widened, scope_added = _widen_include(ref_name.get("include") or [])
    newly_covers_default = bool(
        scope_added
        and default_branch is not None
        and not _covers_default_branch(ref_name.get("include"), default_branch)
    )
    if newly_covers_default:
        # Widening makes everything the ruleset already carries effective
        # on a branch it never applied to, and nothing this module can read
        # says the branch can satisfy it: a check's history on this
        # repository is not a history on THIS branch (a success on a
        # release pull request says nothing about main), an approval
        # requirement is one this tool's own pull requests can never meet,
        # and a rule type this module does not write is one it cannot
        # reason about. Four review rounds on mikelward/repo#56 each found
        # the previous reading one case short, so the widening is refused
        # whenever the ruleset carries anything that could block a pull
        # request on the branch, and widening it is a person's call --
        # rare (a `main`-named ruleset scoped off main), and fail-closed.
        # A ruleset carrying nothing of the kind is widened.
        hazards = _widening_hazards(original.get("rules") or [], sorted(existing_contexts))
        if hazards:
            raise RulesetHeld(
                f"not writing ruleset '{ruleset_name}' (id {existing_id}) -- widening it "
                f"onto '{default_branch}' would enforce there what nothing here can tell "
                f"'{default_branch}' can satisfy: " + "; ".join(hazards) + ". Widen the ruleset "
                "by hand once it is known to, or remove that from it first."
            )

    wanted_contexts = []
    checks_deferred = []
    # What the widening itself defers, with the reason apply_ruleset reports
    # beside the caller's own: a check this write would newly require is
    # left for the run after the one that widens, so the widening write
    # carries nothing that could block a pull request on the branch -- the
    # same rule the existing rules are held to above. A check's history
    # elsewhere on this repository is not a history on the branch (Codex
    # review, mikelward/repo#56); the next run requires it under the
    # ordinary rule, once the ruleset covers the branch.
    widening_deferred = {}
    for context, integration_id in entries:
        wanted = _check_entry(context, integration_id)
        reason = deferred[context] if context in deferred else None
        if reason is None and newly_covers_default and _newly_required(context, integration_id):
            reason = widening_deferred[context] = (
                f"'{context}' waits until ruleset '{ruleset_name}' covers '{default_branch}' -- "
                "this run widens it onto the branch, and a check's history elsewhere on this "
                "repository is not a history there"
            )
        if reason is not None:
            if context not in existing_contexts:
                # Not required today, and not to be newly required now.
                checks_deferred.append(context)
                continue
            if integration_id is not None and integration_id not in existing_bindings[context]:
                # Required today, but not as this App: the binding is the
                # new requirement, and it is what waits. The entries stay
                # as the ruleset has them.
                checks_deferred.append(context)
                wanted_contexts.extend(have_entries[context])
                continue
            widening_deferred.pop(context, None)  # required today as asked: nothing deferred
            # Required today exactly as asked, or asked unbound: nothing new
            # to defer -- deferral holds back a new requirement, never
            # loosens a standing one.
        # An explicit binding in `checks` wins (it sets or re-points the
        # App); an unbound wanted entry preserves whatever the ruleset
        # already had, so a bare command-line name never strips an
        # existing App binding off a check.
        if "integration_id" in wanted:
            # The unbound entry, if there was one, becomes this bound one
            # -- the same requirement, tightened to the App -- and an
            # entry bound to another App stays beside it: the floor.
            wanted_contexts.append(wanted)
            wanted_contexts.extend(
                h
                for h in have_entries.get(context, [])
                if (h.get("integration_id") or None) not in (None, integration_id)
            )
        else:
            wanted_contexts.extend(have_entries.get(context) or [wanted])

    # A context the ruleset requires today and `checks` does not stays
    # required, exactly as it is. The fleet standard is a floor, not a
    # ceiling (maintainer, 2026-09-06): a repository may enforce more, and
    # this module only ever adds. Dropping it used to be the one line in a
    # plan that loosened the gate, and with deferral it could have dropped
    # a working gate while every replacement was still waiting -- a
    # collaborator could then merge past it until a later run converged
    # (Codex security review, mikelward/repo#56). Kept and named, so a
    # plan still says what is required beyond the standard; removing one
    # is a person's decision, made in the ruleset by hand.
    wanted_names = {c for c, _ in entries}
    checks_kept = sorted(c for c in existing_contexts if c not in wanted_names)
    for c in checks_kept:
        wanted_contexts.extend(have_entries[c])
    for rule in new_rules:
        if rule.get("type") == "required_status_checks":
            rule["parameters"]["required_status_checks"] = wanted_contexts
            rule["parameters"]["strict_required_status_checks_policy"] = True

    if not has_status_checks:
        new_rules.append(
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": wanted_contexts,
                },
            }
        )
    if not has_pull_request:
        new_rules.append(
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": True,
                    "allowed_merge_methods": ["rebase"],
                },
            }
        )
    if not has_linear_history:
        new_rules.append({"type": "required_linear_history"})
    if not has_non_fast_forward:
        new_rules.append({"type": "non_fast_forward"})

    target = dict(original)
    target["rules"] = new_rules
    if scope_added:
        ref_name["include"] = widened
        conditions["ref_name"] = ref_name
        target["conditions"] = conditions
    # Adopting a legacy-named ruleset renames it here rather than in a
    # separate call, so the rename and the rules land in one write.
    target["name"] = ruleset_name
    checks_added = [
        c for c, iid in entries if _newly_required(c, iid) and c not in checks_deferred
    ]
    newly_enforced = _newly_enforced(original.get("rules"), new_rules)
    return (
        target != original,
        target,
        has_pull_request,
        scope_added,
        checks_added,
        checks_kept,
        newly_enforced,
        checks_deferred,
        newly_covers_default,
        widening_deferred,
    )


def _plan_write(repo, existing_id, checks, ruleset_name, deferred=(), default_branch=None):
    """Returns (needs_write, target_body, introduces_pr_protection,
    scope_added, checks_added, checks_kept, newly_enforced,
    checks_deferred, newly_covers_default, widening_deferred): the
    full API body this step would PUT to `existing_id` (or POST as a new
    ruleset, when `existing_id` is falsy) to reach `checks`, whether that
    differs from what's there now, and whether writing it would be what
    FIRST makes the branch require a pull request. The third is computed
    from the EXISTING ruleset's own rules, not from whether `existing_id`
    is set -- an update-only check on `existing_id` misses a managed
    ruleset that already had linear-history/force-push rules but no
    pull_request one yet, which this call would still be the one to add
    (Codex review, mikelward/repo#14). needs_write and target_body feed
    apply_ruleset's fingerprint; introduces_pr_protection rides along in
    `report` only, since it describes the ruleset's PRIOR state rather
    than what this call is about to write. scope_added -- the refs the
    update widens the ruleset's scope by -- rides along the same way: it
    is already inside target_body, and travels separately only so the
    plan can name what changed. checks_added -- which of `checks` the
    ruleset does not require today -- rides along for the same reason, and
    is every check for a ruleset being created fresh. checks_deferred --
    which of `deferred` this write leaves out (or leaves as it was) rather
    than newly requiring; see apply_ruleset's `defer`. newly_covers_default
    -- whether the widening is what first brings the default branch into
    the ruleset's scope, so that every check it already carries becomes
    newly effective there -- refused in _build_update_body when it
    carries any. widening_deferred -- context -> reason for the checks
    that widening alone left out of this write (see _build_update_body),
    which `deferred` does not name."""
    if existing_id:
        (
            changed,
            target,
            had_pull_request,
            scope_added,
            checks_added,
            checks_kept,
            newly_enforced,
            checks_deferred,
            newly_covers_default,
            widening_deferred,
        ) = _build_update_body(
            repo, existing_id, checks, ruleset_name, deferred=deferred, default_branch=default_branch
        )
        return (
            changed,
            target,
            not had_pull_request,
            scope_added,
            checks_added,
            checks_kept,
            newly_enforced,
            checks_deferred,
            newly_covers_default,
            widening_deferred,
        )
    # A create enforces all of it for the first time, so the plan lists
    # everything rather than a diff -- None means "list them all".
    entries = _as_entries(checks)
    return (
        True,
        _create_body(ruleset_name, checks, deferred),
        True,
        [],
        [context for context, _ in entries if context not in deferred],
        [],
        None,
        [context for context, _ in entries if context in deferred],
        False,
        {},
    )


def _bypass_actor_note(bypass_actors):
    """None, or one line saying a preserved bypass actor can override
    every rule this ruleset states -- old ones and the two this module
    just added alike. Shared between _describe_plan (the would-write
    path) and apply_ruleset's own no-op message (Codex review,
    mikelward/repo#14: the no-op path returns before _describe_plan is
    ever called, so an already-compliant ruleset with a bypass actor
    reported "matches" with no caveat at all -- the exact case this note
    exists for). This module never adds or removes bypass_actors on an
    UPDATE (see _build_update_body's own doc); `repo audit` is what
    actually checks who they are and reports the gap, so this stays a
    pointer rather than a re-derivation of that logic here."""
    if not bypass_actors:
        return None
    return (
        f"  note: {len(bypass_actors)} bypass actor(s) on this ruleset can override "
        "all of the above -- see `repo audit`"
    )


def _scope_line(scope_added):
    """The one line saying what an update does to the ruleset's targeting
    -- widened by the refs _widen_include appended, or unchanged. What an
    exclusion still keeps out is _report_excluded_hardened's job: it is
    true on every path, including the ones with no scope change to
    describe."""
    if not scope_added:
        return "  scope unchanged"
    return "  scope: also targeting " + ", ".join(scope_added)


def _scope_result(scope_added):
    """How a completed write describes what it did to the targeting --
    past tense, for the success line."""
    if not scope_added:
        return "its scope is unchanged"
    return "now also targeting " + ", ".join(scope_added)


def _deferred_lines(deferred):
    """One line per check a write leaves for a later run, with its reason
    -- the same words in the plan, in the log and beside the write, since
    it is the same fact in each (SPEC.md, *The ladder*)."""
    return [
        f"deferred, not required yet: {reason} -- a later run requires it once it has passed"
        for _context, reason in deferred
    ]


def _describe_plan(
    repo,
    existing_id,
    default_branch,
    checks,
    ruleset_name,
    bypass_actors=(),
    adopted_legacy=None,
    scope_added=(),
    target_body=None,
    needs_write=True,
    deletions=(),
    checks_added=(),
    checks_kept=(),
    newly_enforced=None,
    full=False,
    differing=(),
    deferred=(),
):
    lines = []
    if existing_id and not needs_write:
        # Reached only with a deletion to do: the ruleset itself is
        # already right, and saying "would update" it would name a write
        # that is not going to happen.
        lines.append(
            f"{repo}: ruleset '{ruleset_name}' (id {existing_id}) {NO_OP_MESSAGE} itself"
        )
    elif existing_id and adopted_legacy:
        # Saying "update ruleset 'main'" here would name a ruleset that
        # does not exist yet and hide the rename, which is the change the
        # reader most needs to see (Codex review, mikelward/repo#30).
        lines.append(
            f"{repo}: would adopt ruleset '{adopted_legacy}' (id {existing_id}) and rename "
            f"it '{ruleset_name}'"
        )
        lines.append(_scope_line(scope_added))
    elif existing_id:
        lines.append(f"{repo}: would update ruleset '{ruleset_name}' (id {existing_id})")
        lines.append(_scope_line(scope_added))
    else:
        lines.append(f"{repo}: would create ruleset '{ruleset_name}' on {default_branch}, main and master")
    if needs_write and newly_enforced is None:
        # A create: all of it is new, so listing it is the change.
        lines += _enforced_lines((target_body or {}).get("rules"))
    elif needs_write:
        # An update: only what this write actually adds. Reciting rules the
        # ruleset already holds buried the one or two lines that were the
        # change (maintainer, 2026-09-07).
        if checks_added:
            binding = _binding_map(target_body)
            lines.append(
                "  would newly require: "
                + ", ".join(_context_label(c, binding.get(c)) for c in checks_added)
            )
        if checks_kept:
            lines.append(
                "  keeps requiring, beyond the standard: " + ", ".join(checks_kept)
            )
        lines += [f"  {added}" for added in newly_enforced]
        if scope_added:
            # Widening the scope enforces the rules the ruleset ALREADY
            # holds on refs they did not cover before, so nothing appears
            # in the delta above and the branches become protected all the
            # same. "also targeting master" alone does not say that master
            # is about to start requiring these (Codex review,
            # mikelward/repo#45).
            ref_name = ((target_body or {}).get("conditions") or {}).get("ref_name") or {}
            covered, unevaluated = _effective_scope_added(
                scope_added, ref_name.get("exclude"), default_branch
            )
            if covered:
                # Only the refs the exclusions leave alone: a ref added to
                # the include list and excluded in the same ruleset gets
                # nothing, and _report_excluded_hardened says so.
                lines.append(
                    "  newly effective on "
                    + ", ".join(covered)
                    + (
                        " (unless a pattern in the exclusions covers them)"
                        if unevaluated
                        else ""
                    )
                    + ":"
                )
                lines += _enforced_lines((target_body or {}).get("rules"), indent="    ")
        if full:
            # The full rendering is the short plan PLUS the resulting
            # state, never the state instead of it. Swapping one for the
            # other silently dropped the kept-beyond-the-standard line from
            # --verbose and from the log, which is where an operator is
            # most likely to be reading (Codex review, mikelward/repo#45).
            lines.append("  after this write the ruleset holds:")
            lines += _enforced_lines((target_body or {}).get("rules"), indent="    ")
    lines += [f"  {line}" for line in _deferred_lines(deferred)]
    note = _bypass_actor_note(bypass_actors)
    if note:
        lines.append(note)
    differing_ids = {legacy_id for _n, legacy_id in differing}
    for legacy_name, legacy_id in deletions:
        lines.append(
            f"  would delete the superseded ruleset '{legacy_name}' (id {legacy_id}) -- "
            + (
                f"NOT identical to what '{ruleset_name}' will hold; its full body is "
                "recorded first"
                if legacy_id in differing_ids
                else f"identical to what '{ruleset_name}' will hold"
            )
        )
    return lines


def _confirm(repo, ruleset_name, plan_lines):
    """True if applying was confirmed. Prints the plan and asks only when
    stdin is a terminal; otherwise refuses rather than blocking on a
    question nobody can answer or silently applying an unconfirmed
    change."""
    if not sys.stdin.isatty():
        error("stdin is not a terminal and --force was not given, so leaving")
        error(f"{repo}'s ruleset '{ruleset_name}' unchanged rather than either blocking")
        error("on a question nobody can answer or silently applying a change nobody")
        error("confirmed. Pass --force to apply it non-interactively, or run this")
        error("from a terminal.")
        return False
    for line in plan_lines:
        print(line, file=sys.stderr)
    print(f"Apply this change to {repo}? [y/N] ", file=sys.stderr, end="")
    try:
        answer = input()
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        info(f"not confirmed; leaving {repo}'s ruleset '{ruleset_name}' unchanged.")
        return False
    return True


def apply_ruleset(
    repo,
    checks,
    dry_run,
    force,
    ruleset_name=DEFAULT_RULESET_NAME,
    expected_fingerprint=_NO_EXPECTATION,
    report=None,
    skip_confirm=False,
    defer=None,
    refuse_if_newly_effective=False,
    verify_scaffold_before_requiring_checks=None,
    quiet=False,
    record=None,
):
    """Runs the whole repo-rules port against `repo`. Returns 0 on success
    (including "nothing to change" and a clean --dry-run), 1 if any step
    failed, 2 for a usage error (an empty or control-character check
    name).

    A "fingerprint" is `(existing_id, adopted_legacy, needs_write,
    target_body, deletions)`: which ruleset (or None, meaning create) this
    call is about to act on, whether it would actually write anything, the
    exact API body it would PUT/POST if so, and which legacy-named
    rulesets it would delete afterwards. adopted_legacy distinguishes
    "this id is the standard ruleset" from "this id is a legacy one about
    to be renamed into it", which target_body cannot, since a rename makes
    them identical. Computed once per pass (see `_plan_write` and
    `_plan_legacy_deletion`), exposed via report["fingerprint"], and what
    expected_fingerprint compares against. Every part matters --
    target_body alone can coincidentally match an earlier no-op's even
    when a write is newly needed (content removed, then re-added
    identically), so needs_write has to travel with it rather than being
    inferred from it; and deletions is a mutation of its own, so a
    duplicate that stopped being identical while this waited has to drop
    out of the plan rather than be deleted on a stale reading.

    A deletion is planned whenever a legacy-named ruleset's content is
    identical to what the standard one will hold (see
    _plan_legacy_deletion), including when nothing else needs writing --
    an already-correct ruleset is the steady state, so a deletion that
    only ran on the write path would never run. It happens after the
    write, never before, and a failed delete fails the call.

    expected_fingerprint: when given (not the _NO_EXPECTATION default),
    the fresh fingerprint computed immediately before the real write must
    match it exactly, or this refuses rather than writing -- covers an
    identity swap, a rename, a previewed no-op needing a write after all,
    or the same ruleset's own managed content changing (a required
    check's integration binding, say), in any combination, in the window
    since an earlier call captured it. Left at the default, a call
    compares against its OWN earlier-in-this-call fingerprint instead, so
    every real write still protects itself against drift during its own
    execution with no caller-supplied expectation. See TODO.md's
    "Decisions needing review" for the history of why this exists and
    what it replaced.

    Ownership (enforcement, unmanaged rule types) and the other-rulesets
    merge-method scope conflict are re-verified separately, immediately
    before the fingerprint recheck, not folded into it: both can fail for
    reasons a generic mismatch message would explain badly, and scope
    isn't about this ruleset's own content in the first place.

    report: when given a dict, records structured facts back to the
    caller rather than leaving them to re-derive from rendered text
    (which a --rule value matching NO_OP_MESSAGE could otherwise fool):
    report["checks_added"] -- which of `checks` the ruleset does not
    require today, so a caller can hold back a write whose new requirement
    nothing can satisfy yet. report["scope_added"] -- the refs the write
    would newly target, since a ruleset's existing rules become newly
    effective on a branch its scope newly covers. report["bypass_note"] --
    set on the no-op return, the one thing an already-compliant ruleset
    still has to say, so a caller that hides idle steps does not hide it.
    report["needs_write"], report["deletions"], report["existing_id"]
    (also fingerprint[0]), report["fingerprint"]. report["held"] -- set
    with the reason when the ruleset cannot be written until a person
    acts (RulesetHeld: a widening that would enforce on the branch what
    nothing here can tell it satisfies), so a caller skips this step and
    no other. report["deferred"] --
    (context, reason) pairs for the checks this write leaves for a later
    run, see `defer` below -- is set on every path that got as far as
    planning, so a caller can say what is waiting even when nothing else
    needs writing.

    defer: {context: reason} -- checks the caller knows cannot be newly
    required yet, whatever they have reported (setup_cmd.py passes the
    ones whose publishing workflow its own scaffold pull request is still
    adding). Joined here with the checks that have never PASSED on this
    repository (never_passed): requiring either would block every merge
    with nothing able to satisfy it, which is the permanent wedge SPEC.md
    forbids. A deferred check is not refused and not removed: the write
    goes ahead with everything else, a check the ruleset already requires
    stays required as it is, and the plan names each deferred one with its
    reason so a later run's requiring it is no surprise. There is no
    override -- `force` used to waive the never-reported guard, and a
    guard the fleet loop's own flag turns off is not a guard (SPEC.md,
    *Flags*).

    record: where the body of a ruleset this deletes is written before it
    goes, so the deletion can be undone (see _record_deleted_ruleset). A
    callable taking one string -- setup_cmd hands it the run's log writer.
    None sends it to stdout instead.

    skip_confirm: True skips this function's own interactive _confirm()
    unconditionally, independent of `force`. `force` is the command-line
    "apply without asking"; skip_confirm is for a caller (setup_cmd.py's
    real apply) whose own confirmation already happened, with nothing
    left on stdin to answer a second one.

    refuse_if_newly_effective: refuse, from the FRESH recompute right
    before the real write, a write that would make this ruleset's rules
    newly effective on the branch in any of the three ways -- introducing
    pull-request protection for the first time, newly requiring a check,
    or widening the ruleset's scope onto the branch. setup_cmd.py passes
    this for a branch with no commits that nothing this run will push
    one to: pull-request protection there can never be satisfied (no
    direct push may create the branch, and no pull request can target a
    branch that does not exist), and the preview's answer is a snapshot
    -- an administrator could remove the existing pull_request rule or a
    required check during the confirmation wait, and _build_update_body
    would rebuild the same target body regardless, passing the
    fingerprint check since that compares WHAT would be written, not why
    (Codex review, mikelward/repo#14, #42). Refusing here, from the same
    fresh recompute the fingerprint check itself uses, is what actually
    closes that window rather than narrowing it.

    verify_scaffold_before_requiring_checks: when given, called with the
    FRESH default_branch (the same re-read this function's own fresh
    recompute already did) exactly when a real write would introduce
    pull-request protection for the first time OR name a check the
    ruleset does not require today, and the matching refusal above didn't
    already refuse it -- the return value is an error message (refuses and
    prints it) or None (proceeds). Both, not just the first: adding a
    required check wedges a branch that already requires pull requests
    just as surely, so the caller's "is the branch still what my answer
    was computed against" check has to cover it (Codex review,
    mikelward/repo#42). This module knows nothing about the fleet CI scaffold;
    the callback is how setup_cmd.py verifies that concern against the
    branch this call is ACTUALLY about to protect, not a snapshot from
    before this function re-read the default branch -- an administrator
    changing the default branch itself, or introduces_pr_protection only
    turning true here (an existing pull_request rule removed during the
    wait, see the parameter above), both need the scaffold checked
    against THIS branch, not whichever one an earlier snapshot named
    (Codex review, mikelward/repo#14).

    quiet: suppresses the "nothing to do" no-op report (and its bypass-
    actor note) -- setup_cmd.py's real apply call passes this when the
    caller wants only what changed, not an audit trail of everything this
    checked. Never suppresses a real write's own report, a dry-run
    preview, or anything from `error()`: quiet means "nothing happened
    here", not "don't say what did"."""
    checks = list(checks) if checks else list(DEFAULT_CHECKS)
    entries = _as_entries(checks)

    for context, _integration_id in entries:
        if not context:
            error("empty check name")
            return 2
        if not _valid_no_control_chars(context, f"check name '{context}'"):
            return 2
    if not _valid_no_control_chars(ruleset_name, "the ruleset name"):
        return 2

    try:
        default_branch = _read_default_branch(repo)
    except RulesetError:
        return 1

    if not _check_allow_rebase(repo):
        return 1

    try:
        # A bound entry is satisfied only by the App it names, an unbound one by
        # any producer of that context -- never_passed and _collect_reported
        # read the binding, so an App-bound `lanes` that nothing has yet posted
        # AS that App is deferred here exactly like a name nothing has passed.
        missing = never_passed(repo, entries)
    except RulesetError as e:
        error_lines(f"could not read which checks have passed on {repo}:", e.detail)
        error("Refusing to guess: an incomplete answer here either defers a check")
        error("that is fine or requires one on the strength of a safety check that")
        error("did not finish.")
        return 1
    # The caller's reasons first: a check whose publisher is not on the
    # branch waits for that whichever way its runs went, and one reason per
    # check is enough for a reader.
    deferred = dict(defer or {})
    for item in missing:
        deferred.setdefault(item[0], deferral_reason(item))

    try:
        existing, adopted_legacy, duplicates = _resolve_ruleset(repo, ruleset_name)
    except RulesetError:
        return 1

    if report is not None:
        report["existing_id"] = existing
        report["adopted_legacy"] = adopted_legacy

    if existing and not _check_ruleset_ownership(repo, existing, ruleset_name):
        return 1

    try:
        _validate_merge_method_scope(repo, existing, default_branch)
    except RulesetError:
        return 1

    try:
        (
            needs_write,
            target_body,
            introduces_pr_protection,
            scope_added,
            checks_added,
            checks_kept,
            newly_enforced,
            checks_deferred,
            _newly_covers_default,
            widening_deferred,
        ) = _plan_write(repo, existing, entries, ruleset_name, deferred, default_branch)
    except RulesetHeld as e:
        # A person's call, not a failure of the request: said, handed back,
        # and the caller's other steps go on (SPEC.md, invariant 1).
        error(f"{repo}: {e.detail}")
        if report is not None:
            report["held"] = e.detail
        return 1
    except RulesetError:
        return 1
    # Only the checks this write actually leaves out, in the requested
    # order: one the ruleset already requires is not deferred, whatever
    # its runs say, since leaving it alone changes nothing.
    reasons = {**widening_deferred, **deferred}
    deferred_now = [(context, reasons[context]) for context in checks_deferred]

    try:
        deletions, differing = _plan_legacy_deletion(
            repo, ruleset_name, existing, adopted_legacy, target_body
        )
    except RulesetError:
        return 1

    fingerprint = (existing, adopted_legacy, needs_write, target_body, tuple(deletions))
    if report is not None:
        report["needs_write"] = needs_write
        report["deletions"] = list(deletions)
        report["fingerprint"] = fingerprint
        report["introduces_pr_protection"] = introduces_pr_protection
        report["checks_added"] = list(checks_added)
        report["scope_added"] = list(scope_added)
        report["deferred"] = deferred_now

    if not needs_write and not deletions:
        # Outside the quiet guard, like the one on the write path below and
        # for the same reason: this is the pass that would notice a second
        # ruleset created since the preview ran, and an already-correct
        # ruleset is the steady state, so the no-op return is where a real
        # apply usually ends up (Codex review, mikelward/repo#33).
        _report_duplicate_standard(repo, ruleset_name, existing, duplicates)
        note = _bypass_actor_note((target_body or {}).get("bypass_actors") or [])
        if report is not None:
            # Computed outside the quiet guard and reported structurally:
            # a caller that drops a step's section when the step has
            # nothing to do would otherwise drop this note with it,
            # leaving "nothing to change" standing alone over a ruleset
            # anyone on that list can override (Codex review,
            # mikelward/repo#45).
            report["bypass_note"] = note
        if not quiet:
            print(f"{repo}: ruleset '{ruleset_name}' (id {existing}) {NO_OP_MESSAGE}")
            _report_excluded_hardened(repo, ruleset_name, target_body, default_branch)
            if note:
                print(note)
        # Not gated on quiet: a check still waiting is the one thing an
        # otherwise-idle step has to say, on every run until it lands
        # (SPEC.md: a run says what a later run will do and what that is
        # waiting on).
        for line in _deferred_lines(deferred_now):
            print(f"{repo}: {line}")
        return 0

    # A real branch named main or master beside the default holds the write
    # (a person's call: delete it, or widen by hand), after the no-op return
    # above since an already-compliant ruleset has nothing to hold.
    try:
        _hold_for_sibling_branch(repo, default_branch, ruleset_name)
    except RulesetHeld as e:
        error(f"{repo}: {e.detail}")
        if report is not None:
            report["held"] = e.detail
        return 1
    except RulesetError:
        return 1

    plan_lines = _describe_plan(
        repo,
        existing,
        default_branch,
        checks,
        ruleset_name,
        target_body.get("bypass_actors") or [],
        adopted_legacy,
        scope_added,
        target_body,
        needs_write,
        deletions,
        checks_added,
        checks_kept,
        newly_enforced,
        differing=differing,
        deferred=deferred_now,
    )
    if report is not None:
        # The same plan rendered in full, for a caller that shows the
        # abbreviated one on the terminal and keeps the complete one for
        # --verbose and its log. Rendered here rather than reconstructed
        # there: plan_lines is already the delta, and nothing downstream
        # can recover the rules it left out (Codex review,
        # mikelward/repo#45).
        report["plan_lines_full"] = _describe_plan(
            repo,
            existing,
            default_branch,
            checks,
            ruleset_name,
            target_body.get("bypass_actors") or [],
            adopted_legacy,
            scope_added,
            target_body,
            needs_write,
            deletions,
            checks_added,
            checks_kept,
            newly_enforced,
            full=True,
            differing=differing,
            deferred=deferred_now,
        )

    if dry_run:
        for line in plan_lines:
            print(line)
        _report_excluded_hardened(repo, ruleset_name, target_body, default_branch)
        _report_duplicate_standard(repo, ruleset_name, existing, duplicates)
        _report_differing_legacy(repo, ruleset_name, differing)
        return 0

    if not (force or skip_confirm) and not _confirm(repo, ruleset_name, plan_lines):
        return 1

    # Everything below is re-verified fresh, right before writing -- the
    # repository or the ruleset could have changed in whatever time an
    # interactive user spent deciding, or (for setup_cmd.py's real apply,
    # which never actually waits here -- skip_confirm=True) in the
    # earlier confirmation this call didn't itself show. Ownership and
    # scope get their own specific rechecks (see apply_ruleset's own doc
    # for why); everything else -- identity, and the exact content about
    # to be written -- collapses into the one fingerprint comparison
    # below, against expected_fingerprint.
    try:
        default_branch = _read_default_branch(repo)
    except RulesetError:
        return 1
    if not _check_allow_rebase(repo):
        return 1
    try:
        fresh_existing, fresh_adopted_legacy, fresh_duplicates = _resolve_ruleset(
            repo, ruleset_name
        )
    except RulesetError:
        return 1
    if fresh_existing and not _check_ruleset_ownership(repo, fresh_existing, ruleset_name):
        return 1
    try:
        _validate_merge_method_scope(repo, fresh_existing, default_branch)
    except RulesetError:
        return 1

    try:
        (
            fresh_needs_write,
            fresh_target_body,
            fresh_introduces_pr_protection,
            fresh_scope_added,
            fresh_checks_added,
            _fresh_checks_kept,
            _fresh_newly_enforced,
            _fresh_checks_deferred,
            _fresh_newly_covers_default,
            _fresh_widening_deferred,
        ) = _plan_write(repo, fresh_existing, entries, ruleset_name, deferred, default_branch)
    except RulesetHeld as e:
        error(f"{repo}: {e.detail}")
        if report is not None:
            report["held"] = e.detail
        return 1
    except RulesetError:
        error(f"could not re-read ruleset '{ruleset_name}' to write it")
        return 1

    if fresh_needs_write:
        # Read again, past the run's memo: a branch created during the
        # confirmation wait is exactly what this last look is for.
        try:
            _hold_for_sibling_branch(repo, default_branch, ruleset_name, fresh=True)
        except RulesetHeld as e:
            error(f"{repo}: {e.detail}")
            if report is not None:
                report["held"] = e.detail
            return 1
        except RulesetError:
            return 1

    if refuse_if_newly_effective:
        newly = []
        if fresh_introduces_pr_protection:
            newly.append("now introduce pull-request protection")
        if fresh_checks_added:
            newly.append(f"now newly require {quoted(fresh_checks_added)}")
        if fresh_scope_added:
            newly.append(
                f"now widen its scope to also target {', '.join(fresh_scope_added)}, making its "
                "existing rules newly effective there"
            )
        if newly:
            error(
                f"ruleset '{ruleset_name}' on {repo} would {' and '.join(newly)} -- the caller "
                "asked to refuse exactly that (the branch has no commits for it to be satisfied "
                "on), and the ruleset was edited while this was waiting if the preview did not "
                "say so. Not writing it. Rerun to re-check."
            )
            return 1
    if (
        fresh_introduces_pr_protection or fresh_checks_added or fresh_scope_added
    ) and verify_scaffold_before_requiring_checks is not None:
        problem = verify_scaffold_before_requiring_checks(default_branch)
        if problem is not None:
            error(problem)
            return 1

    try:
        fresh_deletions, fresh_differing = _plan_legacy_deletion(
            repo, ruleset_name, fresh_existing, fresh_adopted_legacy, fresh_target_body
        )
    except RulesetError:
        return 1

    fresh_fingerprint = (
        fresh_existing,
        fresh_adopted_legacy,
        fresh_needs_write,
        fresh_target_body,
        tuple(fresh_deletions),
    )
    want = fingerprint if expected_fingerprint is _NO_EXPECTATION else expected_fingerprint
    if fresh_fingerprint != want:
        error(f"ruleset '{ruleset_name}' on {repo} no longer matches what was previewed and")
        error("confirmed -- either its identity changed (it was created, deleted, or")
        error("replaced by something else under the same name) or its own managed")
        error("content did (a required check re-pointed at a different integration, say),")
        error("while this was waiting. Refusing to write state nobody actually confirmed.")
        error("Rerun to re-check.")
        return 1

    required_now = ", ".join(_binding_map(fresh_target_body)) or "none yet"
    if not fresh_needs_write:
        # Deletion-only: the ruleset already holds everything it should,
        # and the whole change is removing the duplicate beside it.
        pass
    elif fresh_existing:
        try:
            gh.run_with_input(
                ["api", "--method", "PUT", f"repos/{repo}/rulesets/{fresh_existing}", "--input", "-"],
                json.dumps(fresh_target_body).encode(),
            )
        except gh.GhError as e:
            error_lines(f"could not update ruleset '{ruleset_name}' on {repo}:", e.stderr)
            return 1
        if fresh_adopted_legacy:
            print(
                f"{repo}: adopted the ruleset named '{fresh_adopted_legacy}' and renamed "
                f"it '{ruleset_name}' ({_scope_result(fresh_scope_added)}; its bypass actors "
                f"and any other rules are unchanged); required checks: {required_now}"
            )
        else:
            print(
                f"{repo}: updated ruleset '{ruleset_name}' ({_scope_result(fresh_scope_added)}); "
                f"required checks: {required_now}"
            )
    else:
        try:
            gh.run_with_input(
                ["api", "--method", "POST", f"repos/{repo}/rulesets", "--input", "-"],
                json.dumps(fresh_target_body).encode(),
            )
        except gh.GhError as e:
            error_lines(f"could not create ruleset '{ruleset_name}' on {repo}:", e.stderr)
            return 1
        print(
            f"{repo}: created ruleset '{ruleset_name}' on {default_branch}, main and master; "
            f"required checks: {required_now}"
        )
    # What a later run will add, said beside the write it was left out of
    # -- and not gated on quiet, since it is the step's own change in
    # progress, not a recital of state.
    for line in _deferred_lines(deferred_now):
        print(f"{repo}: {line}")

    # After the write, never before: what makes a legacy ruleset safe to
    # delete is that the SURVIVING one holds everything it held, and until
    # the write lands that is only true of the target body. Deleting first
    # and then failing the write would leave the repository protected by
    # neither.
    # quiet here for the same reason the no-op report has it: setup_cmd's
    # preview pass has already printed both of these from its own dry run,
    # and the real apply repeating them is noise, not a second finding.
    if not quiet:
        _report_excluded_hardened(repo, ruleset_name, fresh_target_body, default_branch)

    delete_failed = False
    for legacy_name, legacy_id in fresh_deletions:
        body = _still_deletable(repo, ruleset_name, fresh_existing, legacy_name, legacy_id)
        if body is None:
            error(
                f"{repo}: not deleting '{legacy_name}' (id {legacy_id}) -- it or "
                f"'{ruleset_name}' (id {fresh_existing}) has been renamed since this run "
                "planned the deletion, or could not be re-read. Nothing is unprotected; "
                "rerun to re-check."
            )
            delete_failed = True
            continue
        # The body goes down BEFORE the DELETE, and a failure to record it
        # cancels the delete. GitHub hands back no copy of a deleted
        # ruleset, so this record is the only thing that makes the
        # deletion reversible -- and deleting without it would be exactly
        # the unrecoverable loss that kept this to identical rulesets
        # until now (see _plan_legacy_deletion).
        if _comparable_ruleset(body) != _comparable_ruleset(fresh_target_body):
            # From the body about to go, not the plan's `differing`: an
            # administrator adding a rule to an until-then identical
            # duplicate after the preview would otherwise have it deleted
            # under a plan that called it identical, and the quiet apply
            # path suppresses the plan's own note anyway (Codex review,
            # mikelward/repo#46). Not gated on quiet, for the same reason
            # _report_duplicate_standard is not: this reads state the
            # preview could not have seen.
            _report_differing_legacy(repo, ruleset_name, [(legacy_name, legacy_id)])
        try:
            _record_deleted_ruleset(record, repo, legacy_name, legacy_id, body)
        except OSError as e:
            error(
                f"{repo}: not deleting '{legacy_name}' (id {legacy_id}) -- its body could "
                f"not be recorded ({e}), and deleting it unrecorded is not reversible."
            )
            delete_failed = True
            continue
        try:
            gh.run(["api", "--method", "DELETE", f"repos/{repo}/rulesets/{legacy_id}"])
        except gh.GhError as e:
            error_lines(
                f"could not delete the superseded ruleset '{legacy_name}' (id {legacy_id}) "
                f"on {repo}. '{ruleset_name}' is in place, so the branch is protected -- but "
                "the duplicate is still there and still applies. Delete it by hand, or rerun.",
                e.stderr,
            )
            delete_failed = True
            continue
        print(
            f"{repo}: deleted the superseded ruleset '{legacy_name}' (id {legacy_id}) -- "
            f"'{ruleset_name}' is the one ruleset now"
        )

    # NOT gated on quiet, unlike the two around it. quiet means "the
    # preview already said this about state that has not changed" -- but
    # this list comes from the fresh lookup, and a second ruleset created
    # since the preview ran is exactly what the preview could not have
    # named. Since setup_cmd's real apply runs quiet unless --verbose,
    # gating it here would suppress the only warning there is (Codex
    # review, mikelward/repo#33). The cost is saying it twice on a
    # repository that already had one, which is a repository that needs
    # the reminder anyway.
    _report_duplicate_standard(repo, ruleset_name, fresh_existing, fresh_duplicates)
    if delete_failed:
        return 1
    return 0


def sibling_branch(repo, default_branch, fresh=False):
    """Which hardened literal ref names a REAL branch that is not the
    default: ("exists", name) | ("absent", None) | ("error", detail).

    The standard targets `refs/heads/main` and `refs/heads/master` by name
    whatever the default branch is called, as a lock against renaming the
    default out from under a ruleset scoped to it. A real branch by the
    other name, beside the default, is the branch that lock exists to
    close -- and a ruleset written onto it enforces rules there that
    nothing here can tell it satisfies (SPEC.md, *The standard*).

    Read with the singular git/ref/heads/{name}, which answers only for a
    ref that exists. The branches endpoint does not: GitHub 301-redirects
    a renamed branch's old name to its new one, and `gh api` follows
    redirects, so `repos/{repo}/branches/master` answers 200 with main's
    record on every repository renamed master -> main -- a standing false
    "master exists" on exactly the repositories that closed the backdoor.
    Memoized for the run (see _evidence_cache): the ruleset step asks up
    to four times per run and the answer rarely changes inside one. A
    404 is the answer, not a failure, so it is memoized too; only another
    error is tried again where it is asked again. `fresh` reads past the
    memo and replaces it: the check right before the write asks again,
    like every other precondition apply_ruleset re-reads there, since a
    branch created while the operator was confirming would otherwise be
    widened onto on the strength of the preview's 404 (Codex review,
    mikelward/repo#56)."""
    for ref in _HARDENED_INCLUDE:
        if not ref.startswith("refs/heads/"):
            continue
        name = ref[len("refs/heads/"):]
        if name == default_branch:
            continue
        key = ("sibling", repo, name)
        if fresh or key not in _evidence_cache:
            ok, raw = gh.try_run(["api", f"repos/{repo}/git/ref/heads/{name}"])
            if not ok and "HTTP 404" not in raw and "Git Repository is empty" not in raw:
                return "error", raw
            _evidence_cache[key] = ok
        if _evidence_cache[key]:
            return "exists", name
    return "absent", None


def _hold_for_sibling_branch(repo, default_branch, ruleset_name, fresh=False):
    """RulesetHeld when `repo` has a branch named `main` or `master` beside
    its default branch; RulesetError (reported) when that cannot be read.
    Called only once a write is planned: an already-compliant ruleset has
    nothing to hold, and the branch is said by check_sibling_branch.
    `fresh` is the pre-write check's re-read (sibling_branch)."""
    status, value = sibling_branch(repo, default_branch, fresh=fresh)
    if status == "error":
        error_lines(
            f"could not check whether {repo} has a branch named 'main' or 'master' beside "
            f"'{default_branch}':",
            value,
        )
        raise RulesetError()
    if status == "exists":
        raise RulesetHeld(
            f"not writing ruleset '{ruleset_name}' -- {repo} has a branch named '{value}' beside "
            f"its default branch '{default_branch}'. The ruleset targets '{value}' by name, as a "
            f"lock against renaming '{default_branch}' out from under it, so writing it would "
            f"enforce on '{value}' rules nothing here can tell that branch satisfies, and a pull "
            f"request into '{value}' could be blocked by a check that never runs there. Delete or "
            f"rename '{value}' -- it is the branch the lock exists to close -- or widen the "
            "ruleset by hand, then rerun."
        )


def check_sibling_branch(repo, default_branch=None, quiet=False):
    """Warns on stderr if `repo` has a real branch named `main` or `master`
    beside its default branch -- the backdoor the hardened targeting
    exists to close, worth flagging independent of any ruleset since
    deleting the branch removes it outright (and, while it exists, the
    ruleset step is held: _hold_for_sibling_branch). Advisory here: never
    raises, and a read failure is reported but does not fail the rest of
    `repo setup`. The default branch is read when not given.

    Returns ("exists" | "absent" | "error", detail) -- detail is the
    branch name for "exists", gh's raw stderr for "error", else None.
    `repo setup` (quiet=False, the default) relies on this function's own
    stderr reporting. `repo audit` (quiet=True) decides for itself whether
    an unreadable check fails the whole audit closed rather than merely
    warns, so it takes the outcome back and reports the finding in its own
    [ok]/[GAP] format -- quiet=True suppresses this function's printing so
    the two reports don't word the same finding differently."""
    if default_branch is None:
        try:
            default_branch = _read_default_branch(repo)
        except RulesetError:
            return "error", f"could not read {repo}'s default branch\n"
    status, value = sibling_branch(repo, default_branch)
    if status == "exists":
        if not quiet:
            warn(f"{repo} has a branch named '{value}' beside its default branch '{default_branch}'")
            warn(f"-- the branch the ruleset's targeting of '{value}' by name exists to lock out.")
            warn(f"The ruleset step is held while it exists: delete or rename '{value}'.")
        return "exists", value
    if status == "error":
        if not quiet:
            warn(f"could not check whether {repo} has a branch named 'main' or 'master' beside '{default_branch}':")
            for line in value.splitlines():
                warn(f"  {line}")
        return "error", value
    return "absent", None
