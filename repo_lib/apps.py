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
from repo_lib.common import error, error_lines

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
