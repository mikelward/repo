# repo

Fleet-management CLI for GitHub repositories: `repo list`, `repo create`,
`repo secrets`, `repo setup`, `repo audit`, `repo cleanup`. Four of the six
are a Python rewrite of the `repo-list`/`repo-secrets`/`repo-setup`/`repo-rules-audit`
shell scripts in [mikelward/scripts](https://github.com/mikelward/scripts),
which stay in place unchanged -- this is a fresh implementation, not a
migration, and the shell versions remain the source of truth for behavior
until this catches up. `repo create` and `repo cleanup` have no shell-script
counterparts; they're new here.

## Why Python, not another shell rewrite

`repo-setup` (the most complex of the three) had grown past what shell makes
safe to express: multi-step plan/confirm/revalidate/apply flows, subprocess
composition, and signal handling. Two rounds of real bugs came directly from
shell semantics rather than logic -- POSIX sh's `grep` exit-status ambiguity
and newline-delimited pseudo-arrays, then (even after a bash rewrite) a
non-obvious `dash` behavior where a trap for a signal is not honored until
the foreground child it's blocked on completes, which silently broke a test
harness's own SIGTERM handling. Python's `subprocess`, real data structures,
and `signal` module remove that whole class of problem.

Go (the language of the sibling [`vcs`](https://github.com/mikelward/vcs)
tool) was considered and set aside: `vcs` needs to be fast because it runs on
every shell prompt, and nothing here runs anywhere near that hot path -- it's
invoked interactively, by a human, occasionally. Without that constraint,
Python's ecosystem for exactly this shape of program (wrap a CLI, build a
plan, test it thoroughly) wins on development speed without giving up
anything that matters here.

## Requirements

Python 3.9+ (standard library only -- no dependencies to install) and the
`gh` CLI, authenticated. The floor is `argparse.BooleanOptionalAction`
(`repo create`'s `--scaffold`/`--no-scaffold`), added in 3.9; every
subcommand's parser is built up front regardless of which one is invoked,
so an older interpreter fails on any command, not just that one (Codex
review, mikelward/repo#14).

## Usage

Run `./repo` directly from a checkout; nothing to install.

```
repo list [--owner OWNER] [--include-forks] [--include-archived]
repo create (--private|--public) [--no-scaffold] OWNER/REPO
repo secrets --name NAME [--env ENV] --file PATH [--force] OWNER/REPO...
repo setup [--dry-run] [--force] [-v|--verbose] [--no-rules] [--no-bootstrap]
           [--rule CHECK]... [--secret NAME[@ENV]=PATH]...
           [--credential NAME=PATH]... [--app SLUG]... OWNER/REPO
repo audit [--branch NAME] OWNER/REPO [CHECK...]
repo cleanup [--dry-run] [--force] [--older-than DAYS]
             [--log FILE] OWNER/REPO
```

See `AGENTS.md` for testing and contribution conventions.

## Status

`repo list`, `repo create`, `repo secrets`, `repo setup`, `repo audit`, and
`repo cleanup` are all implemented -- every shell tool in mikelward/scripts
now has a Python equivalent here, plus `repo create` and `repo cleanup`,
which have none. `repo create`
creates an empty repository and, unless `--no-scaffold` is given, pushes
everything mechanically safe to generate as its first commits (two --
GitHub's API needs an existing commit before it will create a branch ref
at all, so a genuinely empty repository can't take this as one write; see
`push_initial_commit`'s own docstring): codex-review's three workflow
files (fetched live from `mikelward/codex-review`,
always current rather than a vendored copy going stale), `zizmor.yml`
(from `mikelward/lanes`, its self-identified pilot), the `.github/zizmor.yml`
exceptions policy and `.github/lanes.conf` docs/code split those imply, and a
`ci.yml` wiring `mikelward/lanes`'s classify+gate job pair with a trivial
placeholder standing in for the project's real jobs, carrying the comment
that says how to replace it. `lanes` and `zizmor` report from the scaffold's
own CI run; `codex` needs a pull request first, since its status-writing
sweep triggers on pull-request activity, not a bare push (Codex review,
mikelward/repo#14). Only the placeholder's replacement with real project
jobs is left undone (see `repo_lib/scaffold.py`'s own docstring for the
full split between what's generated and what isn't).
`repo setup` composes four steps -- the required-checks
branch ruleset (named `main`, with linear history required, force pushes
blocked, plus a standalone warning when a repository has an actual
`master` branch; a ruleset already carrying that name, or a name this tool
used before it, is adopted and updated in place -- renamed where needed --
rather than gaining a second one beside it, since rulesets aggregate and
two of them are only ever confusing),
fanning a secret out via the same logic `repo secrets`
uses, ensuring GitHub App installation membership, and (always on, like
the fleet-credentials and auto-merge steps below; `--no-bootstrap` skips
it) adding whichever of the fleet's own CI scaffold files an
already-existing repository is still missing -- reusing `repo create
--scaffold`'s own generated files, never overwriting one already there,
as one commit on top of the branch's current tip (or, for a repository
whose branch has no commits yet, the same two-commit bootstrap `repo
create --scaffold` uses). This is what makes `repo setup --force`
fix a repository regardless of its starting state in the common case: a
brand-new repo, one only partway set up, and one already complete all
converge on the same result -- behind one combined plan and a single
confirmation for the whole repository. By default it prints only what it
actually changed, so `repo list | xargs -n1 repo setup --force` stays
quiet across an already-in-shape fleet; `-v`/`--verbose` shows the full
plan (what a repository already has, not just what moved) and a
progress line per step, for a closer look at one repository or a run
you're debugging. One case still doesn't converge
(see TODO.md): a repository a PRIOR `repo setup` run already protected
with a pull-request-requiring ruleset has no scaffold fix here yet --
the direct ref update this step makes is exactly what that ruleset
blocks, and closing it needs a branch-and-PR write path this tool
doesn't have. `repo
audit` is the read-only counterpart: it reports whether a branch's rules
(required checks, conversation resolution, up-to-date merges, force-push
and deletion protection, bypass actors, and -- when auditing the
repository's real default branch -- whether every covering ruleset also
targets `refs/heads/main` and `refs/heads/master`) already hold, without
writing anything. It also audits where secrets live. The fleet's shared credentials -- the
weekly dependency batches' `<HUB>_PAT` (or `<HUB>_APP_ID` +
`<HUB>_APP_PRIVATE_KEY` pair, for mikelward/npm-update, gradle-update and
rust-update) and mikelward/ci-commit-artifact's `CI_COMMIT_ARTIFACT_TOKEN`
-- each belong in an environment named after the reusable workflow that
reads them, because a secret passed to a reusable workflow, or inherited
by one, reaches every job in it, a batch's untrusted update job included.
A reusable workflow's callers are whichever workflows call it from a job,
whatever they are named and on whichever branch (a workflow that differs
from the default branch's copy is read on its own branch too, since a push
there runs it). A credential kept as a repository secret, a caller
that passes its secrets by name (an environment credential never reaches
it), a consumer whose environment holds none, a workflow that mentions the
reusable workflow in a shape the audit cannot read as a caller ("cannot
tell"), and a credential left behind by a workflow nothing here calls --
for the batches and the commit-back workflow alike -- are each reported as
`[FIX]` -- a finding `repo setup` closes, named with the command -- and
every other repository-level secret is listed by name for review. A repository that does not allow auto-merge is a `[FIX]` too: the weekly
batches arm it on their pull requests. So is one that does not delete a merged
pull request's head branch automatically -- without it nothing sweeps the
branches a merge leaves behind (see `repo cleanup` below). `[FIX]` findings do
not fail the audit yet (see `TODO.md`): the layout is being rolled out through
`repo setup`.

`repo setup` makes the move, and enables auto-merge and delete-branch-on-merge
on the repository where either is off. Its fleet-credentials step is always on: for
each batch the repository runs (by whichever workflows call it from a job,
whatever they are named, on any branch) and for the
commit-back workflow (by a job calling it), a value passed as
`--credential NAME=PATH` is set in the environment the name belongs in, the
repository-level copy is deleted once the environment holds a usable
credential, and a copy for a workflow the repository does not use is deleted
wherever it sits. A value for a workflow the repository does not use is left
unset, which is the difference from `--secret` (which refuses a fleet
credential's name outright, whatever scope it names: `--credential` is the
one flag that places it). It refuses -- and says so, as
`NOT FIXED`, exiting 1 -- while the caller still passes its secrets by name
(an environment secret reaches a called workflow only through
`secrets: inherit`, so the move would hand the workflow nothing) or when the
environment holds nothing and no value was given (GitHub never returns a
secret's value, so a move needs it handed in). Across a fleet:
`repo list | xargs -n1 repo setup --force --credential NPM_UPDATE_PAT=pat.txt --credential GRADLE_UPDATE_PAT=pat.txt --credential RUST_UPDATE_PAT=pat.txt --credential CI_COMMIT_ARTIFACT_TOKEN=token.txt`.
`repo cleanup` deletes the branches a repository has finished with. It exists
because this fleet used to leave GitHub's "automatically delete head branches"
off with nothing else sweeping up -- mikelward/simmo reached 192 branches, 184
of them dead. `repo setup`/`repo audit` now enable and check that setting (see
above), which handles a branch going forward, from the moment its pull request
merges, with no per-repository invocation needed; `repo cleanup` remains for
the backlog a repository already accumulated before that, and for what the
setting cannot see at all -- an unmerged branch, or a merge with no pull
request. The hard part is that the fleet **rebase-merges**, so a merged branch's
commits are rewritten and every ancestry test calls it unmerged; judged that
way almost nothing here is ever deletable. So a branch counts as merged when a
pull request whose head it was has `merged_at` set **and targeted the default
branch** (authoritative whatever the merge style, since GitHub records the
merge against the pull request, not the rewritten commits), or when `compare`
reports it already contained in the default branch (which catches one merged
with no pull request at all). A pull request merged into some other branch
proves only that the commits reached *that* branch -- the upper half of a
stack merges into the lower one -- so it falls through to the comparison,
which is swept when the base did land and only offered when it did not.

Merged branches are the only ones swept, behind the same printed-plan-then-
confirm flow `repo secrets` uses. The plan prints -- it is what the question is
about, and a confirmation nobody can check is one they can only answer by
guessing. What does not print is the deletion stream after it: two lines a
branch, hundreds of them on a repository with a weekly dependency batch, and
scrolled past, none of it is a record. So the run writes the whole account --
the plan, every deletion with the command that restores it, every refusal and
every error -- to a timestamped file under `$XDG_STATE_HOME/repo`
(`~/.local/state/repo` by default), or to `--log FILE`, and the terminal gets a
carriage-returned `deleting 12/166...` counter in its place. State rather than
cache, because those restore commands are the only record of what a run
deleted. The file is opened *before* anything is deleted, and each branch's
restore command is written and fsynced before its own delete request -- a
flush alone only reaches the kernel, which a power loss or a late writeback
error still discards; a run that cannot open or write it deletes nothing and
says so, since sweeping with
nowhere to write the way back is sweeping with no way back. It is appended to,
never truncated, so pointing `--log` at an earlier run's file -- or two
default-path runs starting inside the same second -- cannot destroy that run's
restore commands. `--dry-run` writes no log: it changes
nothing, a file appearing where none was asked for is a change, and its plan is
the whole of its output. Unmerged branches are never swept: they are
offered, on a terminal -- stdin and stderr both, since that is where the
question is asked and answered, so `2>file` gets the plan and no prompt rather
than an invisible one -- and never under `--force`, showing their age, how many
commits would be lost, and whether their own pull request was closed without
merging. Which ones are offered turns on that last point. A branch whose pull
request was **closed without merging against the default branch** is offered
whatever its age -- somebody has already said it is finished with, and age is
only a proxy for exactly that. Only against the default branch, because GitHub
closes a pull request automatically when its base is deleted: in a stack, that
happens to the upper one the moment the lower branch goes, so its closure says
nothing about whether the work is live. Nobody deletes the default branch, so a
closure against it was a person's decision.
A branch with **no pull request** is offered once its last commit is
`--older-than` days old (7 by default), since a date is the only evidence there
is and recent work should not be asked about. `--older-than` itself is just a
threshold and combines with anything; it is the unmerged *stage* that never
runs under `--force`, because a per-branch judgment cannot be made
unattended. Every deletion
logs the full SHA it removed alongside the `gh api` call that recreates the
ref from it -- shell-quoted, since a branch name may legally contain `$(...)`
and the line is meant to be pasted, and in the API form because a `git push`
needs a clone that already holds the object. Each branch's SHA and
protection are re-read immediately before its own delete, because the plan is
built before the confirmation prompt: one that moved or became protected while
the question waited is refused rather than deleted on a plan that no longer
describes it, and the run exits non-zero saying which. That is one request per
branch and deliberately the whole of it -- earlier revisions also re-read the
repository's open pull requests and re-ran the default-branch comparison here,
which grew to about a quarter of the module. A plan that has gone stale in any
other way is answered by re-running the command, which reclassifies from
scratch.

Four kinds of branch are never touched whatever their state: the default
branch, a protected branch, the head of an open pull request, and the *base* of
an open pull request -- deleting that last one closes the child pull request,
which is how a stacked pair gets destroyed by a sweep that only looked at
heads. The repository's canonical name is resolved once up front, so invoking a
renamed or transferred repository by a name it merely redirects from still
matches its pull requests -- which report canonically -- rather than reading
every one of them as a fork and leaving open pull requests' head branches
unprotected.

What it cannot see is a branch whose content landed under different
commits and a different pull request (patch-equivalence, which `git cherry`
finds and no GitHub API does); those report as unmerged, which is the safe
direction.

The plan groups branches by the prefix they share, so a fleet-sized listing
prints `claude/` once with 166 topics under it rather than 166 times. A prefix
only one branch carries gets no heading -- that would cost more than the
repetition it saves -- so those print in full alongside any unprefixed name.

Across a fleet: `repo list | xargs -n1 repo cleanup --force`. Budget for
that: a repository with ~190 branches, ~180 of them merged, costs roughly
380 requests against the shared 5,000-an-hour limit -- about a dozen such
repositories an hour. A sweep that does hit the limit stops partway with
the failures named, and re-running once it resets picks up what remains,
since each deletion is independent.

See `TODO.md` for where the port deliberately diverges
from the shell porting source.
