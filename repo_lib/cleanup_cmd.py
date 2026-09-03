"""`repo cleanup` -- delete the branches a fleet accumulates.

No shell-script counterpart; new here. It exists because GitHub's
"automatically delete head branches" setting is off across this fleet and
nothing else sweeps up: mikelward/simmo reached 192 branches, 184 of them
dead, before anybody counted.

The one thing that makes this non-trivial is that **this fleet
rebase-merges**. A rebase-merged branch's commits are rewritten, so its tip
is not an ancestor of the default branch and every ancestry test -- `git
branch --merged`, a `compare` returning ahead_by 0 -- calls it unmerged.
Judged that way, essentially nothing in this fleet is ever safe to delete.
So a branch counts as merged here if EITHER holds:

- a pull request whose head was this branch has `merged_at` set. This is
  the one that matters, and it is authoritative regardless of merge style:
  GitHub records the merge against the pull request, not against the
  rewritten commits.
- `compare/{default}...{branch}` reports `ahead_by == 0` -- the branch is
  contained in the default branch. Catches a branch merged (or
  fast-forwarded) with no pull request at all.

What that pair still cannot see is a branch whose *content* landed under
different commits and a different pull request -- patch-equivalence, which
`git cherry` finds and no GitHub API does. Those are reported as unmerged,
which is the safe direction: they are offered, never swept.

Deleting a branch is not quite reversible from here, so:

- **Merged branches are the only thing deleted without asking per branch**,
  and only after the plan is printed and confirmed (or --force given).
- **Unmerged branches are never swept.** With --include-unmerged, each one
  older than --older-than is offered individually, with its age, how many
  commits would be lost, and whether its pull request was closed without
  merging (the strongest abandoned signal available here). That flag
  refuses --force: a per-branch judgment cannot be made unattended.
- **Every deletion prints the full SHA it deleted**, with the `gh api`
  call that recreates the ref from it -- the form that works from
  anywhere, since a `git push` needs a clone that already has the object
  and the fleet invocation has no clone at all. GitHub keeps an
  unreferenced commit reachable for a while, so this is a real recovery
  path, not a gesture -- but not a permanent one.
- **Every branch's SHA and protection are re-read immediately before its
  own delete**, since the plan was built before the confirmation prompt: a
  branch that moved or became protected while the question waited is
  refused rather than deleted on a plan that no longer describes it. One
  request per branch, and deliberately no more -- see
  `_why_no_longer_safe` for what was tried, what it cost, and why the
  answer to a plan gone stale is to re-run the command.

Four kinds of branch are never offered at all, whatever their state: the
default branch, a protected branch, the head of an open pull request, and
the *base* of an open pull request -- deleting that last one closes the
child pull request, which is how a stacked pair gets destroyed by a
cleanup that only looked at heads.

Cost and reliability: free, but the request budget is worth stating
plainly, because the fleet invocation runs this once per repository
against one shared 5,000-authenticated-requests-an-hour limit. Building
the plan costs one repository read, three paginated list calls, and one
`compare` per branch no merged pull request accounted for. Deleting then
costs one branch re-read and one DELETE per branch removed, and nothing
else. So a repository with ~190 branches and ~180 merged ones costs
roughly 380 requests --
about a dozen such repositories an hour, and far more of an ordinary
one. `gh` waits out GitHub's secondary rate limit and retries; the
primary limit is reported rather than waited on, so a sweep that hits it
stops partway with the failures named. Nothing is left half-done by
that: each branch is independent and already deleted, so re-running once
the limit resets picks up exactly what remains. Exit status: 0 if every
deletion succeeded (or --dry-run), 1 if any deletion was refused by its
revalidation or failed, or any read needed to build the plan failed,
2 for a usage error.
"""

import datetime
import json
import re
import shlex
import sys
from urllib.parse import quote

from repo_lib import gh
from repo_lib.common import error, error_lines

# The lookaheads reject `.` and `..` components: made of allowed
# characters, but as path segments spliced into `repos/{repo}/...` they
# would address a different endpoint than the one that was validated.
OWNER_REPO_RE = re.compile(r"^(?!\.\.?/)[A-Za-z0-9._-]+/(?!\.\.?$)[A-Za-z0-9._-]+$")

DEFAULT_OLDER_THAN_DAYS = 7

# Why each branch was left alone. Ordered most-specific first; the first
# match wins, so a protected default branch reports as the default branch.
SKIP_DEFAULT = "the default branch"
SKIP_PROTECTED = "protected"
SKIP_OPEN_PR_HEAD = "head of open PR #{}"
SKIP_OPEN_PR_BASE = "base of open PR #{} -- deleting it would close that PR"
SKIP_UNSAFE_NAME = "name is not one this can safely address as a ref path"


def add_arguments(parser):
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan; delete nothing"
    )
    parser.add_argument(
        "--force", action="store_true", help="delete merged branches without asking"
    )
    parser.add_argument(
        "--older-than",
        type=int,
        default=DEFAULT_OLDER_THAN_DAYS,
        metavar="DAYS",
        help=(
            "how old an unmerged branch's last commit must be before "
            f"--include-unmerged offers it (default {DEFAULT_OLDER_THAN_DAYS}). "
            "Merged branches are not filtered by age -- merged is merged."
        ),
    )
    parser.add_argument(
        "--include-unmerged",
        action="store_true",
        help=(
            "also offer each unmerged branch older than --older-than, one at "
            "a time. Cannot be combined with --force"
        ),
    )
    parser.add_argument("repo", metavar="OWNER/REPO")


def _ref_path(name):
    """`name`, encoded for splicing into an API URL PATH segment.

    `safe="/"` on purpose: an embedded `/` stays a path separator, because
    GitHub's ref endpoints expect a multi-segment branch name
    (`release/1.0`) as literal segments, matching how git names the ref.
    Same encoding, and the same reason, as `scaffold._branch_ref_path`;
    kept local rather than imported because `credentials` and `scaffold`
    already each carry their own for their own endpoint shape.

    Encoding rather than refusing matters: `release#1` and `release%231`
    are legal branch names, and an earlier version of this rejected them
    outright, which quietly put them beyond cleanup's reach forever (Codex
    review, mikelward/repo#20).
    """
    return quote(name, safe="/")


def _safe_ref_name(name):
    """True if `name` can address one branch and no other once encoded.

    Only what encoding cannot fix is refused: a name whose path segments
    would traverse (`.`, `..`, empty) addresses a different endpoint than
    the one that was validated, and no escaping changes that. Everything
    else -- `#`, `%`, a space -- goes through `_ref_path`.
    """
    if not name or name.startswith("/") or name.endswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def _parse_json_lines(output):
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def _read_repo(repo):
    """(canonical full_name, default branch) for `repo`.

    The canonical name matters beyond tidiness. A renamed or transferred
    repository still answers to its old name -- GitHub redirects the API
    call -- but every pull request it returns reports the CANONICAL
    `head.repo.full_name`. Comparing those against the caller's spelling
    would make every local head look like a fork, so no open pull
    request's head branch would be protected, and one already contained
    in the default branch would be swept and its pull request closed. The
    same spelling feeds the per-branch `head=<owner>:<ref>` filter, which
    would match nothing for the same reason. Resolving once here costs no
    extra request -- this response already carries it (Codex review,
    mikelward/repo#20).
    """
    try:
        output = gh.run(
            ["api", f"repos/{repo}", "--jq", "{full_name, default_branch}"]
        )
    except gh.GhError as e:
        error_lines(f"could not read {repo}:", e.stderr)
        raise SystemExit(1)
    data = json.loads(output) if output.strip() else {}
    return (data.get("full_name") or repo), (data.get("default_branch") or "")


def _read_branches(repo):
    try:
        output = gh.run(
            [
                "api",
                "--paginate",
                f"repos/{repo}/branches",
                "--jq",
                ".[] | {name, sha: .commit.sha, protected}",
            ]
        )
    except gh.GhError as e:
        error_lines(f"could not list {repo}'s branches:", e.stderr)
        raise SystemExit(1)
    return _parse_json_lines(output)


def _read_pulls(repo, state):
    try:
        output = gh.run(
            [
                "api",
                "--paginate",
                f"repos/{repo}/pulls?state={state}",
                "--jq",
                ".[] | {number, merged_at, head_ref: .head.ref, "
                "head_sha: .head.sha, head_repo: .head.repo.full_name, "
                "base_ref: .base.ref}",
            ]
        )
    except gh.GhError as e:
        error_lines(f"could not list {repo}'s {state} pull requests:", e.stderr)
        raise SystemExit(1)
    return _parse_json_lines(output)


def _head_is_local(pull, repo):
    """True if this pull request's HEAD is a branch of `repo` itself.

    A pull request from a fork names a branch in the FORK, whose ref can
    collide with an unrelated branch of the same name here -- so a fork's
    head must never mark a local branch merged, or protect one, on a
    coincidence of naming. Its BASE is a different matter and is not
    filtered by this: the base always names a branch in this repository,
    fork or not, and deleting it would close that still-open pull request
    (Codex review, mikelward/repo#20).

    Compared lowercased because GitHub names are case-insensitive: an exact
    compare against however the caller spelled OWNER/REPO drops EVERY pull
    request when the two differ in case, and a dropped merged pull request
    reads as "not merged" -- the plan would then offer live branches
    instead of sweeping dead ones. head.repo is null once a fork is
    deleted, which is correctly not-local.
    """
    return (pull.get("head_repo") or "").lower() == repo.lower()


def _read_commit_date(repo, sha):
    """(committer date, error_text) for one commit."""
    ok, output = gh.try_run(
        ["api", f"repos/{repo}/commits/{sha}", "--jq", ".commit.committer.date"]
    )
    if not ok:
        return None, output
    return output.strip() or None, None


def _compare_to_default(repo, default_branch, branch, sha):
    """(ahead_by, last_commit_date, error_text). ahead_by is None when the
    compare could not be read -- the caller reports that branch rather than
    guessing at it in either direction.

    The date is the BRANCH TIP's, which is not always the last entry of the
    compare's `commits`: GitHub caps that array at 250, so a branch further
    ahead than that would otherwise be dated by its 250th commit and a
    long-lived branch updated yesterday could be offered as months old
    (Codex review, mikelward/repo#20). The common case still costs nothing
    extra -- the array is only re-read from the tip when it was truncated.
    """
    ok, output = gh.try_run(
        [
            "api",
            f"repos/{repo}/compare/{_ref_path(default_branch)}...{_ref_path(branch)}",
            "--jq",
            "{ahead_by, count: (.commits | length), "
            "last: (.commits | last | .commit.committer.date)}",
        ]
    )
    if not ok:
        return None, None, output
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return None, None, f"could not parse the compare result for {branch}\n"
    ahead_by = parsed.get("ahead_by")
    last = parsed.get("last")
    if ahead_by and parsed.get("count") != ahead_by:
        last, err = _read_commit_date(repo, sha)
        if err is not None:
            return None, None, err
    return ahead_by, last, None


def _now():
    """A seam so a test can pin "now" -- an age assertion against the real
    clock drifts the moment the fixture ages past its own threshold."""
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_timestamp(text):
    if not text:
        return None
    try:
        # GitHub returns Zulu time; fromisoformat only learned to parse the
        # 'Z' suffix in 3.11, and the floor here is 3.9.
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_days(timestamp, now):
    if timestamp is None:
        return None
    return (now - timestamp).days


def _classify(
    repo, branches, default_branch, open_pulls, closed_pulls, now, older_than
):
    """Sort every branch into (deletable, offerable, kept, skipped, failed).

    deletable/offerable/kept are lists of dicts with at least name, sha and
    a `why` string for the plan; skipped is (name, reason); failed is
    (name, gh's own error text).
    """
    open_heads = {
        p["head_ref"]: p["number"] for p in open_pulls if _head_is_local(p, repo)
    }
    # Every open pull request, fork heads included: a fork's base still names
    # a branch here, and deleting it closes that pull request.
    open_bases = {}
    for pull in open_pulls:
        open_bases.setdefault(pull["base_ref"], pull["number"])

    # ref -> {head sha: pull number}. Keyed by SHA, not just by name,
    # because a merged pull request only proves the branch landed as it
    # stood AT THAT SHA. This fleet reuses and resets branch names -- a
    # merged `claude/<topic>` gets new commits pushed onto it -- so matching
    # by name alone would read "merged once" as "merged now" and delete the
    # new work (Codex review, mikelward/repo#20). A tip that matches no
    # merged pull request falls through to the compare instead, which
    # reports it unmerged and therefore only ever offers it.
    #
    # Only a pull request merged INTO THE DEFAULT BRANCH counts. Merged
    # anywhere else it proves the commits reached that base, which is not
    # the same claim: `AGENTS.md` documents stacking, where the upper pull
    # request targets the lower branch, so an upper one merges while its
    # base is still an ordinary branch. Abandon that base -- close its pull
    # request, delete or rewind it -- and the upper branch is the only ref
    # left to those commits while carrying a merged pull request that says
    # otherwise. Falling through to the compare asks the question that
    # actually matters, "is this in the default branch now": a stack whose
    # base did land answers yes and is still swept; one that never landed
    # answers no and is only ever offered (Codex review, mikelward/repo#20).
    merged_pr = {}
    unmerged_pr = {}
    for pull in closed_pulls:
        if not _head_is_local(pull, repo):
            continue
        ref = pull["head_ref"]
        if pull.get("merged_at"):
            if pull.get("base_ref") == default_branch:
                merged_pr.setdefault(ref, {}).setdefault(
                    pull.get("head_sha"), pull["number"]
                )
        else:
            # Keyed by SHA for the same reason the merged map is, and it
            # matters even though this one gates no deletion: it is the
            # offer prompt's strongest abandonment signal, so attaching a
            # reused name's OLD closed pull request to today's commits
            # would talk a person into deleting live work. `AGENTS.md`
            # documents resetting and reusing a pinned branch name, so
            # this is the workflow rather than a naming coincidence (Codex
            # review, mikelward/repo#20).
            unmerged_pr.setdefault(ref, {}).setdefault(
                pull.get("head_sha"), pull["number"]
            )

    deletable, offerable, kept, skipped, failed = [], [], [], [], []

    for branch in sorted(branches, key=lambda b: b["name"]):
        name = branch["name"]
        sha = branch["sha"]

        if name == default_branch:
            skipped.append((name, SKIP_DEFAULT))
            continue
        if branch.get("protected"):
            skipped.append((name, SKIP_PROTECTED))
            continue
        if name in open_heads:
            skipped.append((name, SKIP_OPEN_PR_HEAD.format(open_heads[name])))
            continue
        if name in open_bases:
            skipped.append((name, SKIP_OPEN_PR_BASE.format(open_bases[name])))
            continue
        if not _safe_ref_name(name):
            skipped.append((name, SKIP_UNSAFE_NAME))
            continue

        merged_number = merged_pr.get(name, {}).get(sha)
        if merged_number is not None:
            deletable.append(
                {
                    "name": name,
                    "sha": sha,
                    # A merged pull request is durable SHORT OF REWRITING
                    # the default branch: GitHub keeps `merged_at` set even
                    # if a later force-push drops the merge commit, so the
                    # evidence would outlive the commits (Codex review,
                    # mikelward/repo#20). Not defended against, deliberately
                    # -- confirming it would cost a compare on every merged
                    # branch, ~180 more requests on a fleet-sized sweep, to
                    # rule out something `repo setup`'s own ruleset blocks
                    # (`non_fast_forward` on the default branch) and
                    # `AGENTS.md` forbids. TODO.md carries the trade-off.
                    "why": f"merged by PR #{merged_number}",
                }
            )
            continue

        # No merged pull request accounts for it. Only now is a compare
        # worth a call -- this is what keeps the call count proportional to
        # the interesting branches rather than to the whole repository.
        ahead_by, last_commit, err = _compare_to_default(
            repo, default_branch, name, sha
        )
        if ahead_by is None:
            failed.append((name, err))
            continue
        if ahead_by == 0:
            deletable.append(
                {
                    "name": name,
                    "sha": sha,
                    # Containment is a fact about the DEFAULT BRANCH, not
                    # about this one, so unlike a merged pull request it can
                    # stop being true while this branch sits unchanged --
                    # rechecked before the delete (Codex review,
                    # mikelward/repo#20).
                    "why": f"already contained in {default_branch}",
                }
            )
            continue

        age = _age_days(_parse_timestamp(last_commit), now)
        entry = {
            "name": name,
            "sha": sha,
            "ahead_by": ahead_by,
            "age_days": age,
            "closed_pr": unmerged_pr.get(name, {}).get(sha),
        }
        entry["why"] = _describe_unmerged(entry)
        if age is None or age < older_than:
            # Too recent, or no readable age to compare against
            # --older-than. Either way it is kept, never offered: the flag
            # exists to hold back recent work, and "no idea how old" is not
            # "old enough".
            kept.append(entry)
        else:
            offerable.append(entry)

    return deletable, offerable, kept, skipped, failed


def _describe_unmerged(entry):
    commits = entry["ahead_by"]
    parts = [f"{commits} commit{'s' if commits != 1 else ''} not on the default branch"]
    age = entry["age_days"]
    parts.append(f"last commit {age}d ago" if age is not None else "age unknown")
    if entry["closed_pr"] is not None:
        parts.append(f"PR #{entry['closed_pr']} closed without merging")
    return ", ".join(parts)


def _describe_plan(repo, plan, older_than, include_unmerged):
    deletable, offerable, kept, skipped, failed = plan
    lines = []

    if deletable:
        lines.append(f"Merged branches on {repo}, to delete ({len(deletable)}):")
        for entry in deletable:
            lines.append(f"  {entry['name']}: {entry['why']}")
    else:
        lines.append(f"No merged branches to delete on {repo}.")

    if offerable:
        lines.append("")
        verb = "to offer one at a time" if include_unmerged else "NOT deleted"
        lines.append(
            f"Unmerged, last commit at least {older_than}d ago, "
            f"{verb} ({len(offerable)}):"
        )
        for entry in offerable:
            lines.append(f"  {entry['name']}: {entry['why']}")
        if not include_unmerged:
            lines.append("  (pass --include-unmerged to be asked about each of these)")

    if kept:
        lines.append("")
        lines.append(f"Unmerged and too recent to offer ({len(kept)}):")
        for entry in kept:
            lines.append(f"  {entry['name']}: {entry['why']}")

    if skipped:
        lines.append("")
        lines.append(f"Left alone ({len(skipped)}):")
        for name, reason in skipped:
            lines.append(f"  {name}: {reason}")

    if failed:
        lines.append("")
        lines.append(f"COULD NOT CLASSIFY ({len(failed)}) -- left alone:")
        for name, _ in failed:
            lines.append(f"  {name}")

    return lines


def _confirm(count):
    """True if the user confirmed deleting the merged branches. False means
    "stop, nothing was deleted" -- the caller has already had the plan
    printed."""
    if not sys.stdin.isatty():
        error("stdin is not a terminal and --force was not given, so no branches")
        error("were deleted rather than either blocking on a question nobody can")
        error("answer or silently deleting unconfirmed. Pass --force to delete")
        error("non-interactively, or run this from a terminal.")
        return False
    print(
        f"Delete the {count} merged branch{'es' if count != 1 else ''} above? [y/N] ",
        file=sys.stderr,
        end="",
    )
    try:
        answer = input()
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        error("not confirmed; no branches were deleted.")
        return False
    return True


def _offer(entry):
    """Ask about one unmerged branch. True to delete it."""
    print(
        f"Delete {entry['name']}? ({entry['why']}) [y/N] ",
        file=sys.stderr,
        end="",
    )
    try:
        answer = input()
    except EOFError:
        # No more input: treat the rest as declined rather than as blanket
        # agreement, and stop asking questions nobody is there to answer.
        return None
    return answer.strip().lower() in ("y", "yes")


def _read_branch_state(repo, name):
    """(sha, protected, error_text) for one branch, read fresh. One call
    answers both facts, which is why this is the branches endpoint rather
    than git/ref."""
    ok, output = gh.try_run(
        [
            "api",
            f"repos/{repo}/branches/{_ref_path(name)}",
            "--jq",
            "{sha: .commit.sha, protected}",
        ]
    )
    if not ok:
        return None, None, output
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return None, None, f"could not parse the branch state for {name}\n"
    return parsed.get("sha"), parsed.get("protected"), None


def _why_no_longer_safe(repo, entry):
    """None if `entry` is still the branch the plan described, else the
    reason it is not -- read immediately before the write, because the plan
    is built before the confirmation prompt and someone could have pushed to
    or protected the branch while it waited for an answer (Codex review,
    mikelward/repo#20).

    One request per branch, answering both questions at once. It is a
    re-read, not an atomic compare-and-set -- GitHub's ref DELETE takes no
    expected SHA -- so a change landing between this read and the DELETE is
    still possible; refusing on what it can see is the reachable bar.

    Deliberately NOT a full re-derivation of the plan. Earlier revisions
    also re-read the repository's open pull requests and re-ran the
    default-branch comparison here, which grew into roughly a quarter of
    this module and a bounded-staleness cache to make it affordable. The
    maintainer cut it back to this (2026-09-03): the extra machinery
    defended a window of seconds against changes only this operator could
    make, and every round of review on it found a defect in the round
    before. A branch that gained a pull request, stopped being contained,
    or whose default branch moved during the prompt is now caught the same
    way any other stale plan is -- by re-running the command, which
    reclassifies from scratch.
    """
    sha, protected, err = _read_branch_state(repo, entry["name"])
    if err is not None:
        return f"its current state could not be re-read:\n  {err.strip()}"
    if sha != entry["sha"]:
        return (
            f"it moved to {sha} since the plan was built, so the plan "
            "described different commits"
        )
    if protected:
        return "it is protected now, though it was not when the plan was built"
    return None


def _delete(repo, entry):
    """Delete one branch. Returns None on success, gh's error text on
    failure. The full SHA is printed on success: once the ref is gone this
    line is the only record of it, and an abbreviation is no use for the
    API restore below -- which is the form that works from anywhere, unlike
    a `git push` needing a clone that already has the object."""
    ok, output = gh.try_run(
        [
            "api",
            "--method",
            "DELETE",
            f"repos/{repo}/git/refs/heads/{_ref_path(entry['name'])}",
        ]
    )
    if not ok:
        return output
    print(f"deleted {entry['name']} (was {entry['sha']})")
    # Printed for EVERY deletion, not just the offered ones -- `--force` is
    # the path that deletes the most branches and it was the one omitting
    # the recovery line it advertises (Codex review, mikelward/repo#20).
    # Emitted here so the two can never drift apart again.
    print(_restore_hint(repo, entry))
    return None


def _restore_hint(repo, entry):
    """A command the user pastes into a shell, so every interpolated value
    is shell-quoted. A branch name may legally contain `;`, `$(...)`, or a
    backtick -- git forbids none of those -- and whoever pushed the branch
    chose the name, so an unquoted one here is arbitrary code in somebody
    else's terminal (Codex review, mikelward/repo#20)."""
    return "  restore: gh api --method POST {} -f ref={} -f sha={}".format(
        shlex.quote(f"repos/{repo}/git/refs"),
        shlex.quote(f"refs/heads/{entry['name']}"),
        shlex.quote(entry["sha"]),
    )


def run(args):
    repo = args.repo
    if not OWNER_REPO_RE.match(repo):
        error(f"'{repo}' is not an OWNER/REPO name GitHub allows")
        raise SystemExit(2)
    if args.older_than < 0:
        error("--older-than cannot be negative")
        raise SystemExit(2)
    if args.include_unmerged and args.force:
        # --force means "do not ask", and --include-unmerged is nothing but
        # asking. Silently dropping either one would be the dangerous
        # reading (sweeping unmerged work unattended), so refuse instead.
        error("--include-unmerged asks about each branch individually and --force")
        error("suppresses the asking, so the two cannot be combined. Drop --force")
        error("to be asked, or drop --include-unmerged to sweep merged branches")
        error("only.")
        raise SystemExit(2)
    if args.include_unmerged and not args.dry_run and not sys.stdin.isatty():
        error("--include-unmerged needs a terminal to ask on; stdin is not one.")
        error("Run it from a terminal, or drop the flag to sweep merged branches")
        error("only.")
        raise SystemExit(2)

    gh.require_gh()

    # Every later call uses the canonical name, not the caller's spelling:
    # pull requests report canonically, so a renamed repository invoked by
    # its old name would read every local head as a fork.
    repo, default_branch = _read_repo(repo)
    if not default_branch:
        error(f"{repo} reported an empty default branch")
        raise SystemExit(1)

    branches = _read_branches(repo)
    open_pulls = _read_pulls(repo, "open")
    closed_pulls = _read_pulls(repo, "closed")

    now = _now()
    plan = _classify(
        repo, branches, default_branch, open_pulls, closed_pulls, now, args.older_than
    )
    deletable, offerable, _kept, _skipped, failed = plan

    lines = _describe_plan(repo, plan, args.older_than, args.include_unmerged)

    def report_classification_failures():
        # The plan names an unclassifiable branch but not WHY, so this says
        # whether it was auth, a rate limit, or something else. It is called
        # ONCE, here, before anything below can exit: three separate exits
        # had each skipped a relay that lived at the end of this function --
        # the dry run, the "nothing to delete" return, and declining the
        # confirmation -- and patching them one at a time kept missing the
        # next one (Codex reviews, mikelward/repo#20).
        for name, err in failed:
            error_lines(f"could not classify {name}, so it was left alone:", err)

    if args.dry_run:
        for line in lines:
            print(line)
        report_classification_failures()
        raise SystemExit(1 if failed else 0)

    # Printed unconditionally -- including under --force -- so an
    # unattended run still leaves the full account of what it touched and
    # what it left alone in its own output. --force skips the question, not
    # the record.
    for line in lines:
        print(line, file=sys.stderr)

    # Relayed before the confirmation gate, so declining it -- or any later
    # exit -- cannot swallow them.
    report_classification_failures()

    errors = []
    refused = []

    if deletable:
        if not args.force and not _confirm(len(deletable)):
            raise SystemExit(1)

    def delete_checked(entry):
        reason = _why_no_longer_safe(repo, entry)
        if reason is not None:
            refused.append((entry["name"], reason))
            return
        err = _delete(repo, entry)
        if err is not None:
            errors.append((entry["name"], err))

    if deletable:
        for entry in deletable:
            delete_checked(entry)

    if args.include_unmerged and offerable:
        print("", file=sys.stderr)
        error("Now asking about each unmerged branch. Each deletion prints the full")
        error("SHA and the command that recreates the branch from it.")
        for entry in offerable:
            answer = _offer(entry)
            if answer is None:
                error("no more input; leaving the remaining branches alone.")
                break
            if not answer:
                continue
            delete_checked(entry)

    for name, reason in refused:
        error(f"{name} was NOT deleted -- {reason}")
        error("  It was left alone rather than deleted on a plan that no longer")
        error("  describes it. Re-run to see it classified as it now stands.")
    for name, err in errors:
        error_lines(f"could not delete {name}:", err)

    return 1 if (errors or failed or refused) else 0
