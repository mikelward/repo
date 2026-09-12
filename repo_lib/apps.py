"""The App-installation-membership step of `repo setup`.

Port of mikelward/scripts's repo-setup's native App-installation logic (see
its own header/inline comments for the full reasoning; repeated here only
where the port changes something). There is no standalone repo-app-* shell
script this was extracted from -- repo-setup's own comment explains why:
"the App-installation step is native, because there is no sibling script
for it yet." This module is the direct port of that native logic, given
its own file (mirroring rules.py's shape) rather than folded into
setup_cmd.py, since setup_cmd.py composes three independent steps and
shouldn't own any one of their internals.

An installation scoped to "all repositories" gives the App access to every
repository forked into that account too, the instant the fork is created,
since forking creates a new repository the grant already covers. An
explicit, reviewable --app membership list is the fix for that gap; a
"selected" installation is the one this module ever writes to.

Every function below returns a plain result (an AppPlan, a bool, or a
(id, selection) pair) rather than raising -- unlike rules.py's
RulesetError, there's no multi-call sequence here whose caller wants one
summary message for "something in this failed"; each of these calls is
already the whole story, and its own error() call already said why.

Cost and reliability: free -- a handful of GitHub REST API calls per App
per repository, well inside the 5,000-authenticated-requests-an-hour
limit.
"""

from dataclasses import dataclass
from typing import Optional

import re

from repo_lib import gh
from repo_lib.common import error, error_lines, warn_lines

# The character class this module (and the App-membership PUT endpoint it
# calls) is prepared to handle in a slug -- refusing rather than guessing
# how to URL-encode anything wider, same reasoning as secrets_cmd's
# ENV_NAME_RE.
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class AppPlan:
    """The resolved plan for one --app SLUG against one repository.

    verdict is one of:
      ADD             -- a "selected" installation exists and the repo is
                          not yet a member; install_id names it.
      ALREADY_MEMBER   -- a "selected" installation exists and the repo is
                          already a member.
      ALREADY_ALL      -- an "all repositories" installation exists (and
                          the repo itself was confirmed to exist).
      ERROR            -- could not be determined; already reported via
                          error()/error_lines() at the point of failure.
    """

    slug: str
    verdict: str
    install_id: Optional[str] = None


def _positive_int(value):
    """`value` as a positive int, or None if it is not one. Both id-keyed
    lookups below refuse to splice anything but digits into their --jq
    program, and treat a non-integer / non-positive id (nothing GitHub would
    mint) as "no such App" rather than reaching the API."""
    try:
        numeric = int(str(value))
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


# Every App read in this module goes through `user/installations`, which
# GitHub serves only to a GitHub App USER-TO-SERVER token. The token `gh
# auth login` issues belongs to an OAuth App, and a PAT to no App at all,
# so both get a 403 no matter which scopes they carry -- not a permission
# an operator can grant themselves. Worth saying in full wherever that 403
# surfaces: "403" alone sends people to re-authenticate, which cannot fix
# it (mikelward/repo, maintainer's run 2026-09-12).
APP_TOKEN_HINT = (
    "`user/installations` answers only to a GitHub App user-to-server token. "
    "The token `gh auth login` issues is an OAuth-App token, so GitHub refuses "
    "it whatever its scopes -- re-authenticating with gh will not change this."
)


def is_missing_app_token(stderr):
    """True if `stderr` is GitHub's "not a GitHub App token" 403 rather than
    some other failure of the same read. Matched on the message, since gh
    relays the API's body and not a distinguishable status: a plain 403 can
    also mean a suspended install or a blocked account, which ARE worth
    retrying and must not be reported as the hopeless case."""
    return "authorized to a GitHub App" in (stderr or "")


def installations_readable():
    """(ok, detail) for the one endpoint every read below goes through."""
    return gh.try_run(["api", "user/installations", "--jq", ".total_count"])


# App id -> slug pairings the operator supplies (config `app_logins`), so a
# bound check's status creator can be matched without reading
# `user/installations` -- the endpoint GitHub serves only to a GitHub App
# user-to-server token (see app_slug_for_id / APP_TOKEN_HINT). Populated once
# per run from the config; empty otherwise, in which case resolution falls
# back to the API exactly as before.
_known_slugs = {}


def register_known_slugs(mapping):
    """Replace the operator-supplied App id -> slug pairings. Keys coerce to
    positive ints; a non-positive/non-integer key or an empty slug is dropped
    (config validation already rejects those, so this is only belt-and-braces).
    Call with {} to clear -- one run's pairings must never leak into another."""
    _known_slugs.clear()
    for app_id, slug in (mapping or {}).items():
        numeric = _positive_int(app_id)
        if numeric is not None and slug:
            _known_slugs[numeric] = slug


def require_installations_readable(slugs):
    """Exit 2 before the run touches anything when `--app` was passed and
    this token can NEVER list App installations.

    `--app` has nothing to fall back on -- listing installations is how a
    slug becomes an installation id -- so with the wrong kind of token it
    fails that step on every repository in the fleet, one repository at a
    time, and no rerun changes that. Said once, up front, instead.

    Only for the token refusal, which is the request being impossible: any
    other failure of the same read may be this read's bad luck, so it is
    reported and the run goes on to make its other progress, the App step
    failing alone (SPEC.md, invariant 1). The `lanes` binding reads this
    endpoint too and is never checked here -- it has other evidence to fall
    back on, so it holds its own step either way.
    """
    ok, detail = installations_readable()
    if ok:
        return
    if is_missing_app_token(detail):
        error_lines(
            f"--app {' '.join(slugs)} needs to list this account's App installations, "
            "and this token may not:",
            detail,
        )
        error(APP_TOKEN_HINT)
        error("Drop --app to run everything else, or supply a GitHub App user token.")
        raise SystemExit(2)
    warn_lines(
        f"could not list this account's App installations, which --app {' '.join(slugs)} "
        "needs (continuing -- the App step reports its own failure):",
        detail,
    )


def app_slug_for_id(owner, app_id, use_known=True):
    """The slug of the GitHub App whose numeric id is `app_id`, as installed
    on `owner`'s account, or None if no installation of it is visible there.

    This answers only "which App is this id", for matching a status's
    `{slug}[bot]` creator back to the bound `integration_id` -- the Statuses
    REST API surfaces the bot user, not the id, so evidence that the bound App
    posted a status is recognized by that login. It is deliberately NOT a
    coverage check: whether the App can still publish to a given repo is a
    separate, current-state question (`app_covers_repo`), kept out of the
    evidence scan so "has it reported" and "can it still publish" don't get
    entangled -- four review rounds of coverage findings came from entangling
    them (Codex, mikelward/repo#52).

    Filters on the target account, not the id alone: `user/installations`
    spans every account the caller can see, and the same App installed on
    another account must not answer here. Lets a gh read failure propagate
    rather than reporting and returning None: the caller must tell "not
    installed here" (a real gap) from "could not read" (can't-tell), and only
    a raised error carries the second. A non-integer / non-positive `app_id`
    returns None without reaching the API."""
    numeric = _positive_int(app_id)
    if numeric is None:
        return None
    if use_known and numeric in _known_slugs:
        # The operator asserted this id's slug (config `app_logins`), so the
        # match needs no `user/installations` read -- and the assertion is
        # account-agnostic, so it stands ahead of the owner filter below. Only
        # for EVIDENCE (matching a status creator): coverage prediction passes
        # use_known=False, since whether an App covers a repo is ground truth
        # to read, not an operator assertion (Codex, mikelward/repo#63).
        return _known_slugs[numeric]
    if not SLUG_RE.match(owner or ""):
        # `owner` is spliced into the --jq text below; anything outside the
        # slug character class cannot be, so refuse rather than guess -- a
        # fail-closed "not resolvable", never a wrong match.
        return None
    jq = (
        f".installations[] | select(.app_id == {numeric} and "
        '(.account.login | ascii_downcase) == '
        f'("{owner}" | ascii_downcase)) | .app_slug'
    )
    out = gh.run(["api", "user/installations", "--paginate", "--jq", jq])
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def app_covers_repo(owner, app_id, repo):
    """True if the App with numeric id `app_id`, installed on `owner`'s
    account, can currently act on `repo` -- an ACTIVE installation that is
    either "all repositories" or a "selected" one whose member list includes it.
    False if it is not installed on `owner` at all, a selected install excludes
    `repo`, or the installation is suspended.

    A suspended installation keeps its `repository_selection` but cannot act, so
    it can never publish the status -- counting it as coverage would let setup
    bind `lanes` to an App that then blocks every merge, and audit report the
    binding as healthy, so `suspended_at` is checked explicitly (Codex,
    mikelward/repo#52).

    This is the liveness/coverage question the binding precondition turns on,
    kept separate from the evidence scan (see app_slug_for_id). Binding a
    required check to an App that cannot publish here would wedge every merge,
    so setup refuses it -- and, unlike "has not reported yet", that refusal is
    NOT `--force`-overridable, since no amount of forcing makes an uninstalled
    App able to report (Codex, mikelward/repo#52).

    Lets a gh read failure propagate (can't-tell); the caller must tell that
    from a definite "not covered". A non-integer / non-positive id returns
    False."""
    numeric = _positive_int(app_id)
    if numeric is None:
        return False
    if not SLUG_RE.match(owner or ""):
        return False
    jq = (
        f".installations[] | select(.app_id == {numeric} and "
        '(.account.login | ascii_downcase) == '
        f'("{owner}" | ascii_downcase)) | '
        "[.id, .repository_selection, (.suspended_at != null)] | @tsv"
    )
    out = gh.run(["api", "user/installations", "--paginate", "--jq", jq])
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        install_id, selection, suspended = line.split("\t", 2)
        if suspended == "true":
            # Suspended: it holds a repository_selection but cannot act, so it
            # never publishes here and does not count as coverage.
            return False
        if selection != "selected":
            # "all repositories" (any non-selected scope) covers every repo in
            # the account, this one included.
            return True
        return _installation_covers(install_id, repo)
    return False


def _installation_covers(install_id, repo):
    """True if `repo` is among the repositories a "selected" installation
    (`install_id`) can act on.

    Lets a gh read failure propagate rather than returning False:
    app_slug_for_id's caller must tell "not covered here" -- a real gap --
    from "the member list could not be read", which is can't-tell, and only a
    raised error carries the second, exactly as app_slug_for_id itself does.
    Same case-insensitive full-name compare plan_app_step uses."""
    if not str(install_id).isdigit():
        # `install_id` is GitHub's own numeric installation id, spliced into
        # the URL below; anything else cannot be, so fail closed rather than
        # guess -- never a wrong match.
        return False
    out = gh.run(
        [
            "api",
            "--paginate",
            f"user/installations/{install_id}/repositories",
            "--jq",
            ".repositories[].full_name",
        ]
    )
    members = [line.strip() for line in out.splitlines() if line.strip()]
    return any(member.lower() == (repo or "").lower() for member in members)


def resolve_installation(slug, repo_owner):
    """The (install_id, repository_selection) of the one installation of
    `slug` on `repo_owner`'s account, or None if it could not be resolved
    unambiguously (already reported).

    user/installations lists every installation the AUTHENTICATED USER can
    see, across every account they belong to -- personal and every org
    they are a member of. Filtering on app_slug alone would therefore
    reject a perfectly unambiguous target whenever the same App happens to
    be installed on more than one of those accounts (a personal install
    and an org install, say). repo_owner -- the target repository's own
    account -- is what disambiguates it, the same way the account itself
    disambiguates it on GitHub's own UI. Compared case-insensitively,
    since GitHub account names are.

    Only ever called with a slug/repo_owner that have already passed
    SLUG_RE / the OWNER/REPO character check, so splicing them raw into
    the --jq program's own text carries no quote-breaking risk -- neither
    character class can contain `"` or a backslash.
    """
    jq = (
        f'.installations[] | select(.app_slug=="{slug}" and '
        '(.account.login | ascii_downcase) == '
        f'("{repo_owner}" | ascii_downcase)) | [.id, .repository_selection] | @tsv'
    )
    try:
        out = gh.run(["api", "user/installations", "--paginate", "--jq", jq])
    except gh.GhError as e:
        error_lines(f"could not list App installations (needed for --app {slug}):", e.stderr)
        return None
    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        error(f"no installation of an App with slug '{slug}' was found on {repo_owner}'s account")
        return None
    if len(lines) > 1:
        error(f"more than one installation matched App slug '{slug}' on {repo_owner}'s")
        error("account -- refusing to guess which one")
        return None
    install_id, selection = lines[0].split("\t", 1)
    return install_id, selection


def _confirm_repo_exists(repo, context):
    """True if `repo` was confirmed, via a real gh api call, to exist and
    be accessible. `context` is folded into the error message when it
    isn't -- used both for the ALREADY_ALL case (an installation's own
    scope alone never confirms the repo itself) and for the not-yet-a-
    member case (absent from a "selected" installation's member list is
    ambiguous the same way -- a real, not-yet-added repo, or a typo'd one
    that would never appear in any installation's list either)."""
    ok, out = gh.try_run(["api", f"repos/{repo}", "--jq", ".id"])
    if ok:
        return True
    error_lines(f"could not confirm {repo} exists and is accessible (needed before {context}):", out)
    return False


def plan_app_step(repo, repo_owner, slug):
    """Resolves the plan for one --app SLUG against `repo`. Never raises;
    an unresolvable step is reported (via error()/error_lines() at the
    point of failure) and returned as AppPlan(slug, "ERROR")."""
    resolved = resolve_installation(slug, repo_owner)
    if resolved is None:
        return AppPlan(slug, "ERROR")
    install_id, selection = resolved

    if selection != "selected":
        # ALREADY_ALL is reported on the strength of the installation's
        # own scope alone ONLY once $repo itself is independently
        # confirmed to exist -- otherwise an App-only run against a
        # typo'd or inaccessible repo would exit 0 claiming coverage
        # nothing was ever checked.
        if _confirm_repo_exists(
            repo, f"treating it as already covered by {slug}'s 'all repositories' scope"
        ):
            return AppPlan(slug, "ALREADY_ALL")
        return AppPlan(slug, "ERROR")

    ok, out = gh.try_run(
        ["api", "--paginate", f"user/installations/{install_id}/repositories", "--jq", ".repositories[].full_name"]
    )
    if not ok:
        error_lines(f"could not list {slug}'s installed repositories:", out)
        return AppPlan(slug, "ERROR")
    # Case-insensitive: GitHub repository names are case-insensitive to
    # resolve, but .full_name is returned in the repo's own canonical
    # casing, which can differ from however the caller typed `repo`. An
    # exact-case miss here would plan ADD for a repo that's already a
    # member, which can then fail outright for a credential allowed to
    # list an installation's repositories but not modify them, reporting
    # failure on a state that already held.
    members = [line.strip() for line in out.splitlines() if line.strip()]
    if any(member.lower() == repo.lower() for member in members):
        return AppPlan(slug, "ALREADY_MEMBER")

    # Absent from the list is ambiguous the same way ALREADY_ALL's bare
    # scope check was: genuinely not-yet-added, or a typo'd/inaccessible
    # repo that would never appear in ANY installation's member list. The
    # real apply's ADD case already resolves the repo's id before adding
    # it and would catch a typo there, but planning ADD purely on "not in
    # the list" would report success for a step that would actually fail
    # at apply time.
    if _confirm_repo_exists(repo, f"planning to add it to {slug}'s installation"):
        return AppPlan(slug, "ADD", install_id)
    return AppPlan(slug, "ERROR")


def describe_plan(repo, plans):
    """Plain-text lines describing every AppPlan in `plans` -- no leading
    indentation of their own; the caller (setup_cmd's combined plan)
    indents them to fit its own nesting."""
    lines = []
    for plan in plans:
        if plan.verdict == "ADD":
            lines.append(f"{plan.slug}: would add {repo}")
        elif plan.verdict == "ALREADY_MEMBER":
            lines.append(f"{plan.slug}: already a member")
        elif plan.verdict == "ALREADY_ALL":
            lines.append(f"{plan.slug}: installed with 'All repositories' -- already covered")
        else:
            lines.append(f"{plan.slug}: could not determine (see error above)")
    return lines


_VERDICT_LABEL = {
    "ALREADY_MEMBER": "already a member",
    "ALREADY_ALL": "installed with 'All repositories'",
    "ERROR": "could not be determined",
}


def apply_step(repo, repo_owner, previewed_plan):
    """Applies one AppPlan for real. Returns True on success (including
    the ALREADY_MEMBER/ALREADY_ALL no-ops). ERROR always returns False.

    The plan is re-resolved here, fresh, via a full plan_app_step call --
    not trusted from whatever `previewed_plan` (built earlier, for the
    preview) saw. Codex review: the confirmation prompt (or an earlier
    step's own duration) can sit for an arbitrary amount of real time, and
    membership can change in that window -- a "selected" installation
    losing the repo, an "all repositories" installation narrowing, or the
    App being uninstalled outright -- so treating a previewed
    ALREADY_MEMBER/ALREADY_ALL as a no-op success without rechecking could
    report success for a repo the App no longer actually covers.
    Re-resolving is cheap (a handful of API calls, well inside the rate
    limit -- see the module docstring) and gives every verdict here the
    same fresh-state-at-write-time guarantee rules.apply_ruleset's own
    real (non-preview) call gets for free from being an independent call
    each time; there is no reason a direct function call here should
    trust a stale snapshot when a fresh read is this cheap.

    Codex review, one level deeper: re-resolving fixed trusting a STALE
    no-op as success, but blindly acting on whatever the fresh
    resolution says opened a DIFFERENT gap -- if `previewed_plan` was
    itself NOT "ADD" (a no-op, or an ERROR), setup_cmd.run's own
    needs_confirmation may have skipped asking about this App ENTIRELY,
    on the strength of "nothing to do here". If the fresh resolution then
    finds it needs ADD after all, silently performing that write would
    apply something the user was never shown, let alone confirmed --
    refused below instead, same principle as the needs_write half of
    rules.apply_ruleset's own fingerprint: a previewed no-op turning out
    to need a write is never silently promoted into one."""
    plan = plan_app_step(repo, repo_owner, previewed_plan.slug)
    if plan.verdict == "ERROR":
        return False
    if plan.verdict != "ADD":
        return True
    if previewed_plan.verdict != "ADD":
        error(f"{previewed_plan.slug}'s installation now needs {repo} added, but the plan")
        error(f"shown and confirmed said otherwise ({_VERDICT_LABEL[previewed_plan.verdict]}) --")
        error("something changed while this was waiting. Refusing to add it without a")
        error("fresh plan and confirmation covering that change. Rerun to re-check.")
        return False

    ok, out = gh.try_run(["api", f"repos/{repo}", "--jq", ".id"])
    if not ok:
        error_lines(f"could not resolve {repo}'s numeric id (needed to add it to {plan.slug}):", out)
        return False
    repo_id = out.strip()

    ok, out = gh.try_run(
        ["api", "--method", "PUT", f"user/installations/{plan.install_id}/repositories/{repo_id}"]
    )
    if ok:
        print(f"{repo}: added to {plan.slug}'s installation")
        return True
    error_lines(f"could not add {repo} to {plan.slug}'s installation:", out)
    error("if this looks like a permission problem rather than a 'not found', a")
    error("classic PAT is the documented fallback -- these endpoints predate")
    error("fine-grained ones and may not accept them (unverified against GitHub's")
    error("own docs; see repo-setup's own header comment in mikelward/scripts).")
    return False
