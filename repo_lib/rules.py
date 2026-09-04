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
module does not manage. check_master_branch() is independent of any
ruleset: it warns whenever the repository has an actual branch named
"master" at all, since deleting it removes the backdoor outright.

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

from repo_lib import gh
from repo_lib.common import error, error_lines

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


def _collect_reported(repo, wanted, ref=None):
    """What this repository has ever reported, as (names, app_pairs):
    every context name seen anywhere, and every (name, App id) pair seen
    on a check run. Walks the default branch head, then open pull
    requests, then closed ones -- each stage bounded to one page --
    stopping as soon as every entry in `wanted` is satisfied.

    `wanted` is a set of (context, integration_id-or-None) entries. `ref`
    is the branch whose head to scan first -- the branch whose gates are
    actually in question. None means the repository's default branch. A
    check produced only on pushes to `release` never appears on the
    default branch's head, so scanning that head while auditing `release`
    reports a working gate as never reported.

    Raises RulesetError on any read failure: an incomplete answer must
    never be read as "this check has never reported", which would either
    reject a valid name or, with --force, require one on the strength of a
    safety check that never finished."""
    names = set()
    app_pairs = set()

    def scan(sha):
        try:
            out = gh.run(
                [
                    "api",
                    "--paginate",
                    f"repos/{repo}/commits/{sha}/check-runs",
                    "--jq",
                    ".check_runs[] | [.name, .app.id] | @json",
                ]
            )
        except gh.GhError as e:
            raise RulesetError(f"reading check runs for {sha}:\n{e.stderr}")
        for line in out.splitlines():
            if not line.strip():
                continue
            name, app_id = json.loads(line)
            names.add(name)
            if app_id is not None:
                app_pairs.add((name, app_id))
        try:
            out = gh.run(
                [
                    "api",
                    "--paginate",
                    f"repos/{repo}/commits/{sha}/status",
                    "--jq",
                    ".statuses[].context | @json",
                ]
            )
        except gh.GhError as e:
            raise RulesetError(f"reading commit statuses for {sha}:\n{e.stderr}")
        names.update(_json_lines(out))

    def satisfied():
        return all(_entry_satisfied(e, names, app_pairs) for e in wanted)

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
        return names, app_pairs

    for state in ("open", "closed"):
        try:
            out = gh.run(
                [
                    "api",
                    f"repos/{repo}/pulls?state={state}&per_page=100&sort=updated&direction=desc",
                    "--jq",
                    ".[].head.sha",
                ]
            )
        except gh.GhError as e:
            raise RulesetError(f"listing {state} pull requests:\n{e.stderr}")
        for sha in out.splitlines():
            sha = sha.strip()
            if not sha:
                continue
            scan(sha)
            if satisfied():
                return names, app_pairs
        if satisfied():
            return names, app_pairs
    return names, app_pairs


def quoted(names):
    """Check names for a message, quoted and comma-separated. A name can
    contain spaces -- this repository has one called "Classify the diff" --
    so a space-joined list cannot be split back into names by eye, and the
    reader cannot tell one missing check from three."""
    return ", ".join(f"'{n}'" for n in names)


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
    names, app_pairs = _collect_reported(repo, set(entries), ref=ref)
    return [
        (context, integration_id, context in names)
        for context, integration_id in entries
        if not _entry_satisfied((context, integration_id), names, app_pairs)
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


def _lookup_existing_ruleset(repo, ruleset_name):
    """The id of the ruleset named `ruleset_name` on `repo`, or None. A
    failed lookup must never read as "there is no ruleset": that would
    create a duplicate that ANDs with a real one this run simply couldn't
    see -- so it raises RulesetError instead."""
    try:
        out = gh.run(
            [
                "api",
                "--paginate",
                f"repos/{repo}/rulesets?includes_parents=false",
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
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    return ids[0] if ids else None


def _lookup_legacy_ruleset(repo, ruleset_name):
    """(name, id) of the first ruleset carrying a name this tool used
    before `ruleset_name`, or (None, None). Raises on a failed lookup, for
    the same reason _lookup_existing_ruleset does."""
    for legacy_name in LEGACY_RULESET_NAMES:
        if legacy_name == ruleset_name:
            continue
        found = _lookup_existing_ruleset(repo, legacy_name)
        if found:
            return legacy_name, found
    return None, None


def _resolve_ruleset(repo, ruleset_name):
    """(id, adopted_legacy_name): the ruleset this run will write.

    Prefers one already carrying the standard name. Failing that, adopts a
    legacy-named one -- the write renames it in place, which keeps its
    bypass actors, its scope and any rule type outside MANAGED_RULE_TYPES,
    all of which a create-a-new-one-and-leave-the-old would have stranded
    behind a second, aggregating ruleset. (None, None) means create."""
    existing = _lookup_existing_ruleset(repo, ruleset_name)
    if existing:
        return existing, None
    legacy_name, legacy_id = _lookup_legacy_ruleset(repo, ruleset_name)
    return legacy_id, legacy_name


def _note_legacy_ruleset(repo, ruleset_name, existing, adopted_legacy):
    """Says so when a legacy-named ruleset sits beside the standard one.

    Read-only on purpose. Removing it is the obvious next step and is not
    taken here: deciding whether one ruleset is superseded by another means
    comparing rule types, ref scope, every managed parameter, each required
    check's App binding, and the bypass actors -- five levels, each found
    only after the previous was fixed. That belongs in its own change with
    its own review (see TODO.md), not bolted onto a rename."""
    if adopted_legacy or not existing:
        return
    try:
        legacy_name, legacy_id = _lookup_legacy_ruleset(repo, ruleset_name)
    except RulesetError:
        return
    if legacy_id:
        error(
            f"{repo}: note -- '{legacy_name}' (id {legacy_id}) is still there beside "
            f"'{ruleset_name}'. Rulesets aggregate, so both apply; delete it by hand "
            "once you have checked it carries nothing the other does not."
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


def _compute_scope(repo, existing_id):
    """The branch conditions the write will target: the existing ruleset's
    own conditions on update (unchanged), or the hardened three-ref set on
    create."""
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
    return json.loads(out)


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


def _create_body(ruleset_name, checks):
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
                    "required_status_checks": [{"context": c} for c in checks],
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


def _build_update_body(repo, existing_id, checks, ruleset_name):
    """UPDATE does not build a body from scratch: it fetches the existing
    ruleset and edits only the two managed rules inside it, since a PUT
    replaces the whole object and this module does not know every field
    GitHub puts there (conditions, enforcement, bypass_actors, and so on
    all stay exactly as they were). An entry's integration_id -- which
    binds a required check to a specific GitHub App -- is preserved by
    reusing the existing entry for any context that already has one,
    rather than rebuilding from names alone.

    Returns (changed, target) -- target is the full object to PUT,
    `changed` is whether it differs from what's there now."""
    try:
        raw = gh.run(["api", f"repos/{repo}/rulesets/{existing_id}"])
    except gh.GhError as e:
        error_lines(f"could not read ruleset '{ruleset_name}' (id {existing_id}):", e.stderr)
        raise RulesetError()
    original = json.loads(raw)
    for field in _VOLATILE_FIELDS:
        original.pop(field, None)

    wanted_contexts = [{"context": c} for c in checks]
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
            have_by_context = {
                h.get("context"): h for h in params.get("required_status_checks") or []
            }
            params["required_status_checks"] = [
                have_by_context.get(w["context"], dict(w)) for w in wanted_contexts
            ]
            params["strict_required_status_checks_policy"] = True
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
    # Adopting a legacy-named ruleset renames it here rather than in a
    # separate call, so the rename and the rules land in one write.
    target["name"] = ruleset_name
    return target != original, target, has_pull_request


def _plan_write(repo, existing_id, checks, ruleset_name):
    """Returns (needs_write, target_body, introduces_pr_protection): the
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
    than what this call is about to write."""
    if existing_id:
        changed, target, had_pull_request = _build_update_body(repo, existing_id, checks, ruleset_name)
        return changed, target, not had_pull_request
    return True, _create_body(ruleset_name, checks), True


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


def _describe_plan(
    repo, existing_id, default_branch, checks, ruleset_name, bypass_actors=(), adopted_legacy=None
):
    lines = []
    if existing_id and adopted_legacy:
        # Saying "update ruleset 'main'" here would name a ruleset that
        # does not exist yet and hide the rename, which is the change the
        # reader most needs to see (Codex review, mikelward/repo#30).
        lines.append(
            f"{repo}: would adopt ruleset '{adopted_legacy}' (id {existing_id}) and rename "
            f"it '{ruleset_name}'; scope unchanged"
        )
    elif existing_id:
        lines.append(f"{repo}: would update ruleset '{ruleset_name}' (id {existing_id}); scope unchanged")
    else:
        lines.append(f"{repo}: would create ruleset '{ruleset_name}' on {default_branch}, main and master")
    lines.append("  required checks: " + ", ".join(checks))
    lines.append("  review conversations must be resolved")
    lines.append("  the branch must be up to date with the base")
    lines.append("  rebase is the only merge method")
    lines.append("  commit history must be linear")
    lines.append("  force pushes are blocked")
    note = _bypass_actor_note(bypass_actors)
    if note:
        lines.append(note)
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
        error(f"not confirmed; leaving {repo}'s ruleset '{ruleset_name}' unchanged.")
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
    refuse_if_introduces_pr_protection=False,
    verify_scaffold_before_introducing_pr_protection=None,
    quiet=False,
):
    """Runs the whole repo-rules port against `repo`. Returns 0 on success
    (including "nothing to change" and a clean --dry-run), 1 if any step
    failed, 2 for a usage error (an empty or control-character check
    name).

    A "fingerprint" is `(existing_id, adopted_legacy, needs_write,
    target_body)`: adopted_legacy distinguishes "this id is the standard
    ruleset" from "this id is a legacy one about to be renamed into it",
    which target_body cannot, since a rename makes them identical. Which
    ruleset (or None, meaning create) this call is about to act on,
    whether it would actually write anything, and the exact API body it
    would PUT/POST if so. Computed once per pass (see `_plan_write`),
    exposed via report["fingerprint"], and what expected_fingerprint
    compares against. All three parts matter -- target_body alone can
    coincidentally match an earlier no-op's even when a write is newly
    needed (content removed, then re-added identically), so needs_write
    has to travel with it rather than being inferred from it.

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
    report["needs_write"], report["existing_id"] (also fingerprint[0]),
    report["fingerprint"]. report["never_reported"] is set instead, and
    the other three left absent, when this refuses over the never-
    reported-check guard without force -- the one refusal reason that
    isn't a real problem with the request, only something to wait out.

    skip_confirm: True skips this function's own interactive _confirm()
    unconditionally, independent of `force`. The two are different
    things: `force` authorizes overriding the never-reported-check guard
    above; skip_confirm only says "don't ask a question here" -- for a
    caller (setup_cmd.py's real apply) whose own confirmation already
    happened, with nothing left on stdin to answer a second one.

    refuse_if_introduces_pr_protection: checked against the FRESH
    recompute right before the real write, not the earlier preview --
    setup_cmd.py's own bootstrap-failure gate already skips calling this
    function at all when the PREVIEW says a write would introduce
    pull-request protection, but a preview-time answer is a snapshot: an
    administrator could edit the existing ruleset during the confirmation
    wait (removing its pull_request rule from one that otherwise still
    needed a write) such that _plan_write's fresh call now answers True
    where the preview said False, and _build_update_body would silently
    reconstruct the same target body regardless -- passing the ordinary
    fingerprint check, since that compares WHAT would be written, not why.
    Refusing this here, from the same fresh recompute the fingerprint
    check itself uses, is what actually closes that window rather than
    narrowing it (Codex review, mikelward/repo#14).

    verify_scaffold_before_introducing_pr_protection: when given, called
    with the FRESH default_branch (the same re-read this function's own
    fresh recompute already did) exactly when a real write would
    introduce pull-request protection for the first time and
    refuse_if_introduces_pr_protection didn't already refuse it -- the
    return value is an error message (refuses and prints it) or None
    (proceeds). This module knows nothing about the fleet CI scaffold;
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

    for check in checks:
        if not check:
            error("empty check name")
            return 2
        if not _valid_no_control_chars(check, f"check name '{check}'"):
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
        # Names off the command line carry no App binding, so every entry
        # is unbound: any producer of that context counts.
        missing = never_reported(repo, [(c, None) for c in checks])
    except RulesetError as e:
        error_lines(f"could not read which checks have reported on {repo}:", e.detail)
        error("Refusing to guess: an incomplete answer here either rejects a valid")
        error("check or, with --force, requires one on the strength of a safety")
        error("check that did not finish.")
        return 1
    if missing:
        error(f"never reported on {repo}: {describe_missing(missing)}")
        if force:
            error("(--force given; a merge will block until each one reports)")
        else:
            error("Add the check first. Pass --force to require it anyway.")
            # Distinct from every other reason this function refuses: there
            # is nothing wrong with the repository or the request, only a
            # check that hasn't run yet -- recoverable by waiting, not by
            # anything the caller can fix now. setup_cmd.py reads this back
            # to tell that apart from a genuine preview failure, so it can
            # skip only the ruleset step rather than refusing to apply
            # anything else this run could otherwise finish.
            if report is not None:
                report["never_reported"] = missing
            return 1

    try:
        existing, adopted_legacy = _resolve_ruleset(repo, ruleset_name)
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
        needs_write, target_body, introduces_pr_protection = _plan_write(
            repo, existing, checks, ruleset_name
        )
    except RulesetError:
        return 1

    fingerprint = (existing, adopted_legacy, needs_write, target_body)
    if report is not None:
        report["needs_write"] = needs_write
        report["fingerprint"] = fingerprint
        report["introduces_pr_protection"] = introduces_pr_protection

    if not needs_write:
        if not quiet:
            print(f"{repo}: ruleset '{ruleset_name}' (id {existing}) {NO_OP_MESSAGE}")
            _note_legacy_ruleset(repo, ruleset_name, existing, adopted_legacy)
            note = _bypass_actor_note((target_body or {}).get("bypass_actors") or [])
            if note:
                print(note)
        return 0

    plan_lines = _describe_plan(
        repo,
        existing,
        default_branch,
        checks,
        ruleset_name,
        target_body.get("bypass_actors") or [],
        adopted_legacy,
    )

    if dry_run:
        for line in plan_lines:
            print(line)
        _note_legacy_ruleset(repo, ruleset_name, existing, adopted_legacy)
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
        fresh_existing, fresh_adopted_legacy = _resolve_ruleset(repo, ruleset_name)
    except RulesetError:
        return 1
    if fresh_existing and not _check_ruleset_ownership(repo, fresh_existing, ruleset_name):
        return 1
    try:
        _validate_merge_method_scope(repo, fresh_existing, default_branch)
    except RulesetError:
        return 1

    try:
        fresh_needs_write, fresh_target_body, fresh_introduces_pr_protection = _plan_write(
            repo, fresh_existing, checks, ruleset_name
        )
    except RulesetError:
        error(f"could not re-read ruleset '{ruleset_name}' to write it")
        return 1

    if fresh_introduces_pr_protection and refuse_if_introduces_pr_protection:
        error(
            f"ruleset '{ruleset_name}' on {repo} would now introduce pull-request protection "
            "(it didn't when this was last checked -- its existing pull_request rule was "
            "removed, or the ruleset itself, while this was waiting), and the caller asked to "
            "refuse exactly that. Not writing it. Rerun to re-check."
        )
        return 1
    if fresh_introduces_pr_protection and verify_scaffold_before_introducing_pr_protection is not None:
        problem = verify_scaffold_before_introducing_pr_protection(default_branch)
        if problem is not None:
            error(problem)
            return 1

    fresh_fingerprint = (
        fresh_existing,
        fresh_adopted_legacy,
        fresh_needs_write,
        fresh_target_body,
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

    if fresh_existing:
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
                f"it '{ruleset_name}' (its scope, bypass actors and any other rules are "
                f"unchanged); required checks: {', '.join(checks)}"
            )
        else:
            print(
                f"{repo}: updated ruleset '{ruleset_name}' (its scope is unchanged); "
                f"required checks: {', '.join(checks)}"
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
            f"required checks: {', '.join(checks)}"
        )
    return 0


def check_master_branch(repo, quiet=False):
    """Warns on stderr if `repo` has an actual branch literally named
    'master' -- the backdoor the hardened ~DEFAULT_BRANCH/main/master
    targeting above exists to close, worth flagging independent of any
    ruleset since deleting the branch removes it outright. Advisory, not a
    precondition for the ruleset write: never raises, and a read failure
    here is reported but does not fail the rest of `repo setup`.

    Returns ("exists" | "absent" | "error", detail) -- detail is gh's raw
    stderr text for "error", else None. `repo setup` (quiet=False, the
    default) ignores the return value and relies on this function's own
    stderr reporting, unchanged. `repo audit` (quiet=True) needs to decide
    for itself whether an unreadable check should fail the whole audit
    closed rather than merely warn, so it takes the outcome and detail
    back and reports the finding in its own [ok]/[GAP] format instead --
    quiet=True suppresses this function's own printing so the two reports
    don't duplicate (and word) the same finding differently."""
    # Ask for the branch's own name back, and require it to be literally
    # "master" -- a 200 is not enough. GitHub 301-redirects a renamed
    # branch's old name to its new one, and `gh api` follows redirects, so
    # on a repository renamed master -> main this endpoint answers 200 with
    # main's record. Reading only the exit status turns every such rename
    # into a standing false "master exists" -- the exact backdoor finding
    # that is supposed to mean something, reported on repositories that
    # closed it by renaming. The name in the response settles it whatever
    # the redirect did: only a real master branch answers to that name.
    ok, result = gh.try_run(["api", f"repos/{repo}/branches/master", "--jq", ".name"])
    if ok:
        name = result.strip()
        if name == "master":
            if not quiet:
                error(f"{repo} has a branch literally named 'master' -- this can bypass a")
                error("ruleset scoped only to the default branch. Delete it, or confirm the")
                error("ruleset above also targets refs/heads/master.")
            return "exists", None
        if name:
            # A different name means the request was redirected off a
            # renamed master, so there is no master branch to report.
            return "absent", None
        # 200 with no name is neither -- "could not tell" is its own
        # finding here, never quietly folded into a clean result.
        detail = f"gh: repos/{repo}/branches/master returned no branch name\n"
        if not quiet:
            error(f"could not check whether {repo} has a branch named 'master':")
            error(f"  {detail.strip()}")
        return "error", detail
    if "HTTP 404" in result:
        return "absent", None
    if not quiet:
        error(f"could not check whether {repo} has a branch named 'master':")
        for line in result.splitlines():
            error(f"  {line}")
    return "error", result
