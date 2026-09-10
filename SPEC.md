# SPEC

What `repo setup` promises, written down so the promise is not re-derived
from the code -- or contradicted by it -- one review finding at a time. A
change that alters the order things land in, adds something a run waits
on, or adds a flag is a change to this file first.

## The contract

`repo setup` is a convergence loop, not a one-shot configurator. Run it on
a loop over the fleet (`repo list | xargs -n1 repo setup --force`, or a
cron) and, after a few runs, every repository is at the fleet standard.
Two invariants hold on every run, and they outrank every other rule here:

1. **Every run makes progress.** Whatever a repository is missing, the run
   does the part it can verify is safe now, and says exactly what a later
   run will do and what that is waiting on. A step that cannot finish this
   run does not hold back the parts of it that can: a ruleset write goes
   in with the checks that have passed and defers the rest; a scaffold
   pull request goes in with what is missing today. "Rerun once X has
   happened" is a valid outcome; "skipped" with nothing changed is not,
   unless nothing at all is safe.
2. **No run leaves a repository in a state a later run cannot fix.**
   Nothing wedges merges permanently: a check is never required before it
   has passed on this repository, or while its publishing workflow is
   absent from the default branch. Nothing is deleted without its body in
   the run's record. Nothing is overwritten unless it is byte-pinned
   upstream (see *What is updated*). A wait a later run resolves on its
   own is fine; a wait only a person can resolve is reported as such, with
   the one thing to do.

Nothing waits on an operator except what only an operator can settle (see
*What still needs a person*). Merging the pull requests this tool opens is
not on that list: it merges its own, and closes its own where the scaffold
is complete without them, so none outlives what it was opened for.

**Open branches may break temporarily.** A run may leave an open pull
request unable to merge for a while -- a newly required check its head
predates and has never run, a stricter rule it does not satisfy yet.
That is acceptable, on one condition: it works again after another run of
`repo setup`, or after the branch is rebased onto the latest default
branch, with no other action. What a run must never do is leave a branch
that no rerun and no rebase can bring back -- that is the permanent wedge
invariant 2 forbids.

## The standard

The end state a repository converges to. Each item is one step of `repo
setup`, always on unless a flag opts the repository out:

- **The scaffold**: codex-review's three workflow files at the current
  template, `zizmor.yml` and its `.github/zizmor.yml` policy, a `ci.yml`
  running lanes' classify/gate pair with `.github/lanes.conf`, and
  `AGENTS.md` with a `CLAUDE.md` that imports it. `--no-bootstrap` opts
  out.
- **One branch ruleset named `main`**, targeting `~DEFAULT_BRANCH`,
  `refs/heads/main` and `refs/heads/master`: pull requests required,
  rebase the only merge method, conversation resolution required, linear
  history, no force pushes, and the required checks `lanes`, `codex` and
  `zizmor` with branches required up to date -- `lanes` bound to the lanes
  App once that App publishes it. The standard is a floor: a check the
  ruleset already requires beyond it stays required, since this tool only
  ever adds a requirement, and removing one is a person's decision made
  in the ruleset by hand. A ruleset that does not reach the default branch
  yet is widened onto it only when it carries nothing that could block a
  pull request there -- no required check, no approval requirement, no
  rule type this tool does not write: a check's history on this
  repository says nothing about this branch, and this tool's own pull
  requests get no approver, so widening such a ruleset is refused and
  left to a person. The widening write adds no check of its own for the
  same reason; the checks it would have added wait for the run after,
  once the ruleset covers the branch. The literal `main` and `master`
  are a lock against renaming the default branch out from under the
  ruleset, and they land whatever the default is called. A real branch
  by the other name beside the default is the branch that lock exists
  to close, and a ruleset written onto it would enforce there what
  nothing here can tell that branch satisfies (a check that never runs
  there blocks every pull request into it, and no rerun or rebase clears
  that), so while one exists the ruleset step is held for a person --
  delete or rename the branch, or widen by hand -- and every other step
  still runs. Read as a Git ref, never through the branches endpoint,
  which follows a rename's redirect and reports the renamed branch under
  its old name. A legacy-named duplicate is adopted or
  deleted (recorded first). `--no-rules` opts out; `--rule` names a
  different check set to add.
- **Fleet credentials in their environments**, each restricted to the
  default branch, with no repository-level copy left behind.
- **Auto-merge** and **delete branch on merge** enabled.
- **The GitHub Apps** named with `--app` installed on the repository.

## The ladder

A required check has to exist before it can be required, and the thing
that makes it exist is a pull request. So a repository climbs in steps,
one run each, and every rung is a state the repository can sit in safely:

0. A branch with no commits gets the whole scaffold pushed directly,
   before anything else; the ruleset is not written past that push
   failing, or past a run that could not plan the scaffold and so cannot
   say whether the branch is empty: pull-request protection on a branch
   nothing can push to is the one wedge no rerun undoes.
1. The scaffold's missing (or outdated) files go in as a pull request.
   The ruleset is written in the same run with everything that is safe
   now: pull-request protection, linear history, force-push protection,
   and whichever standard checks have already passed here and whose
   publisher is on the default branch. The rest is deferred, by name, with
   the reason.
2. A later run finds that pull request, sees its head is exactly the
   commit it would generate today, its checks green and the pull request
   mergeable, and merges it -- rebase, with the head sha as a precondition
   so an edit since the read is refused rather than merged. A head that is
   not that commit is replaced with a fresh pull request.
3. A later run sees the scaffold on the default branch and the checks it
   publishes passed, and requires them. `lanes` and `zizmor` pass on the
   scaffold pull request itself; `codex` reports from the first pull
   request the sweep sees once its workflow is on the default branch --
   the next scaffold update, a dependency batch, anything.
4. The lanes App binding follows the same rule: the check is bound to the
   App only once the App has published it here, and the credential is in
   place, in a run after the one that placed the credential.

"Passed" means a `success` conclusion (or status) on this repository --
on the default branch's head, an open pull request's head, or a closed
one's, looking back through the newest five hundred of each: a bound,
since a check that never passed would otherwise walk the whole history on
every run, and older evidence is not counted. A check that has run and failed is not required until it has
passed once, and the run says that it has run but never passed, which is
different from never having run. A check whose workflow this run's own
scaffold pull request is still adding is deferred until that pull request
has merged, whatever it has reported meanwhile: what a pull request
contains is not evidence about the branch, and the branch is what the
ruleset protects.

## What is updated, and what is only added

The scaffold **adds** any file that is missing and **never touches** one
that is present, with one class of exception: files that are byte-pinned
upstream. codex-review's three templates are compared byte for byte by
`codex-review-check` in every consumer, so a local copy that differs is
either a superseded shape (the migration the update performs) or drift
that already fails that check; the current template is the right content
in both cases, and the update pull request is how a template change
reaches the fleet. Everything else -- `ci.yml`, both zizmor files,
`lanes.conf`, `AGENTS.md`, `CLAUDE.md` -- may carry a project's own
decisions, and is left exactly as found.

## Its own pull requests

`repo setup` opens pull requests for the scaffold and merges them itself on
a later run. It recognizes its own by branch prefix in this repository (a
fork's branch of the same name is not its own) AND by author (the user
this token acts as; a pull request's author is the one thing about it
nobody can change afterwards), looks for them whatever branch they target
now (one retargeted since it was opened is found and replaced, not left
open beside a second), never opens a second one beside an open one of
its own, and never reads what an open one contains to reason about the
branch. Whether one is safe to merge is answered by content,
not by name: the generated commit is deterministic, so the head is
merged only when it is exactly one commit whose parent is the default
branch's current tip and whose tree is that tip's plus the generated
changes, carrying the generated commit message -- compared by blob sha,
no file read. A head that is anything
else (the base moved, a template moved upstream, something was pushed to
it, even a push that was reverted, since a rebase merge would replay it)
is stale: it is closed and a fresh one is opened. So is one that no
longer targets the default branch: the merge lands wherever the base
points now, and the head sha pins only the head, so the base is read
again with the head right before merging, and the default branch is
read afterwards to confirm the merged commit is on it -- a merge that
landed elsewhere is reported, not counted. It merges only when every check
run on the head has completed successfully (skipped and neutral count as
not failed), no commit status is pending or failed, each workflow the
pull request itself adds has a successful Actions run from its head
(asked by workflow path, since a check merely named like one can come
from anywhere; one whose newest run completed without success is held
for a person, since waiting changes nothing), the pull request is not a
draft and GitHub reports it
mergeable -- a pull request nothing runs on at all, a gap of only
`AGENTS.md` on a repository whose own workflows skip that diff, merges on
GitHub's word alone rather than waiting forever -- and it merges by API with
the head sha it read, so a head that moved is a refusal, not a merge --
and delete-branch-on-merge is turned on before the merge where it is
off, so GitHub deletes the merged branch as part of the merge itself: the
settings step would enable it later in the same run, too late for this
merge, and a delete afterwards would be a separate request with no sha
precondition, racing anyone pushing to the branch. A pull request GitHub blocks on a required check that has not passed on
its head, with nothing running there (a run still going, or the codex
sweep's pending status, is a wait), is reported as needing a person,
whatever the check is called: nothing says it will ever report -- a
workflow whose path filter the diff does not match never runs on it,
the tool's own or a project's customized copy alike, and a check bound
to an App needs that App's own report, which a head that already ran
under another does not get again -- and no rerun or rebase changes that.
One blocked with every required check passed is asked,
live, what holds it: a review it lacks or an unresolved conversation is
reported as needing a person, as is a branch requiring signed commits
(the generated commit is not one); and a block none of those explains is
reported as needing a person to look, never waited on forever -- naming,
as a hint only, any rule on the branch that may settle on its own (a
required deployment, a required workflow, code scanning), since a rule's
presence is not a reason and reading it as one would let it mask a block
only a person can clear. A hold on a deployment still pending costs one
red run that clears itself. It never merges, closes or updates a pull
request it did not open.

## Flags

`--force` means "apply without asking" and nothing else. It does not waive
a guard: there is no flag that requires a check before it has passed, and
none is wanted -- a guard a fleet loop turns off with the flag it always
passes is not a guard. `--dry-run` previews the same decisions, the same
deferrals and the same exit status as the run would have. `--no-rules`,
`--no-bootstrap`, `--rule`, `--secret`, `--credential` and `--app` shape
what the standard means for one repository; none of them changes the
contract above.

## What still needs a person

Every one of these is reported in the run's output with the one thing to
do, and none of them is caused by a run:

- A check that runs here and fails (`zizmor` finding a real problem in a
  workflow, say): the ruleset defers it until it passes, and passing it
  is a code change.
- A scaffold pull request whose checks fail, that conflicts with the base,
  that somebody converted to a draft, that GitHub blocks on a review
  requirement, an unresolved conversation, a signature rule or something
  this tool cannot name, or whose own workflows will never run because
  Actions is disabled on the repository (a pull request adding no
  workflow has nothing to run, and merges on GitHub's word).
- A scaffold path occupied by something that is not a plain file.
- A gh token without the `workflow` scope, which cannot write workflows.
- A legacy-named ruleset whose merge methods conflict with rebase.
- A branch that already requires a check whose publisher is missing from
  it (a state that predates the tool): every pull request there is stuck
  until someone who can bypass the rule lands the workflow.
