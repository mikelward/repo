# repo

Fleet-management CLI for GitHub repositories: `repo list`, `repo create`,
`repo secrets`, `repo setup`, `repo audit`, `repo cleanup`. Four of the six
are a Python rewrite of the `repo-list`/`repo-secrets`/`repo-setup`/`repo-rules-audit`
shell scripts in [mikelward/scripts](https://github.com/mikelward/scripts),
which stay in place unchanged -- this is a fresh implementation, not a
migration, and the shell versions remain the source of truth for behavior
until this catches up. `repo create` and `repo cleanup` have no shell-script
counterparts; they're new here.

`repo setup` is a convergence loop, and one command run one way: the same
invocation, with the same flags, over every repository --

    repo list | xargs -n1 repo setup --force --credential NAME=PATH ...

-- repeated until nothing is left deferred or held. Every repository ends
at the standard, fully set up and with every protection in place, with
nothing waiting on a person that a later run could settle and nothing left
in a state a later run cannot fix. The command is never tailored per
repository; a `--credential` for a reusable workflow this repository does
not call is reported unused rather than written. (`--secret` is not a
convergence flag -- it writes its value to every repository it is given --
and the lanes App pair is placed ahead of its workflow by design. Re-pointing
an existing lanes binding to a different App is the one hold a rerun does not
clear; it is done by hand, and the rest of the run still lands. See
`SPEC.md`.) `SPEC.md` is that contract -- what the standard
is, the order things land in, what a run defers and why -- and the place a
change to any of that goes first.

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

Python 3.9+, [PyYAML](https://pyyaml.org/) (the one dependency, declared
in `pyproject.toml`: `uv run ./repo ...` installs it into a project
environment on its own, or install it yourself with `uv pip install pyyaml`,
`python3 -m pip install pyyaml`, or your distribution's package -- `python3-yaml`
on Debian and Ubuntu; `./repo` says so if it is missing) and the `gh` CLI,
authenticated. The floor is `argparse.BooleanOptionalAction`
(`repo create`'s `--scaffold`/`--no-scaffold`), added in 3.9; every
subcommand's parser is built up front regardless of which one is invoked,
so an older interpreter fails on any command, not just that one (Codex
review, mikelward/repo#14).

The fleet's stable `repo setup` flags -- which credentials exist and where
each value is read from, plus `rules`/`apps`/`force` -- can live in a config
file at `$XDG_CONFIG_HOME/repo/config.yaml` (or `~/.config/repo/config.yaml`),
read by default, so they are not retyped every run; a command-line flag
overrides it and `--no-config` ignores it. It holds paths, never secret
values. See `SPEC.md`.

`repo setup` checks those credentials before it touches a repository: a
token GitHub refuses stops the run at the top naming `gh auth login`,
rather than failing once per step. `--app` is checked there too, because
listing an account's App installations answers only to a GitHub App
user-to-server token -- the token `gh auth login` issues is refused
whatever its scopes. Either check failing for any other reason (a 500, a
rate limit) says nothing about the token, so it is reported and the run
carries on. Everything not about Apps still converges on an ordinary
token; only `--app` and verifying a `lanes` binding whose evidence is a
commit status need that read.

No optional extras: `repo cleanup`'s interactive checkbox picker uses the
standard library's `curses`, so on a terminal you get a checkbox list (space
toggles, `a`/`n` select all/none, enter confirms, `q`/esc cancels), with
everything preselected. Off a terminal -- or where `curses` cannot drive
the terminal -- `cleanup` falls back to a plain-text prompt (all / select
one-by-one / none) and says so. Either way there is nothing extra to
install; the tool stays PyYAML-only.

## Usage

Run `./repo` directly from a checkout; nothing to install.

```
repo list [--owner OWNER] [--include-forks] [--include-archived]
repo create (--private|--public) [--no-scaffold] OWNER/REPO
repo secrets --name NAME [--env ENV] --file PATH [--force] OWNER/REPO...
repo setup [--dry-run] [--force|--no-force] [-v|--verbose] [--no-rules]
           [--no-bootstrap] [--config FILE] [--no-config] [--no-app]
           [--log FILE] [--no-log] [--rule CHECK]...
           [--secret NAME[@ENV]=PATH]... [--credential NAME=PATH]...
           [--app SLUG]... OWNER/REPO
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
that says how to replace it. It also writes `AGENTS.md` -- this account's
shared agent conventions, fetched from `mikelward/conf`, under a generated
header whose two sections (what the project is, what a contributor runs)
are left as TODOs -- and a `CLAUDE.md` that imports it, so a brand-new
repository is not the one place an agent works with no conventions at
all. `lanes` and `zizmor` report from the scaffold's own CI run; `codex`
needs a pull request whose BASE branch already carries its workflow,
since its status-writing sweep runs under `pull_request_target` (Codex
review, mikelward/repo#14) -- so on the first pull request opened after
the scaffold lands, not on the one adding it. Only the placeholder's replacement with real project
jobs is left undone (see `repo_lib/scaffold.py`'s own docstring for the
full split between what's generated and what isn't).
`repo setup` is quiet: the terminal gets what is changing and what is
wrong, not a recital of everything already in place. A step with nothing
to do is not mentioned, and an update to an existing ruleset names only
the checks and protections it adds -- a create still lists all of them,
because there everything is new. `--verbose` restores the full plan and
per-step progress markers. A run that changes something also writes the
full record -- every section, including the ones the terminal drops -- to
a timestamped file under `$XDG_STATE_HOME/repo` (`--log FILE` to choose
the path, `--no-log` to skip it), and names that path at the end -- plus
the full body of any ruleset it deletes, since GitHub hands back no copy
of one and that record is what makes the deletion reversible. A run
that changes nothing writes no file: over a fleet, a log per repository
per sweep saying nothing happened is the same noise somewhere else.

`repo setup` composes four steps -- the required-checks
branch ruleset (named `main`, with linear history required, force pushes
blocked, and held for a person while a repository has a real `main` or
`master` branch beside its default branch, which is also warned about on
its own; a ruleset already carrying that name, or a name this tool
used before it, is adopted and updated in place -- renamed where needed --
rather than gaining a second one beside it, since rulesets aggregate and
two of them are only ever confusing, and where both names exist the older
one is deleted -- whatever it holds, with its full body written to the
run's log first so it can be POSTed back, and with a note when it was not
identical to what survives; an existing ruleset's own scope is
widened to cover all three refs, never narrowed or replaced -- and an
exclusion that carves one of them back out is reported rather than
deleted, since removing one somebody wrote is a narrowing decision this
does not make),
fanning a secret out via the same logic `repo secrets`
uses, ensuring GitHub App installation membership, and (always on, like
the fleet-credentials and auto-merge steps below; `--no-bootstrap` skips
it) adding whichever of the fleet's own CI scaffold files an
already-existing repository is still missing -- reusing `repo create
--scaffold`'s own generated files, never overwriting one already there
except codex-review's three workflow files, which every consumer pins
byte for byte and which are brought up to the current template when they
differ -- as one commit on a branch of its own, opened as a pull request against
the default branch (or, for a repository whose branch has no commits yet
and so has no base for a pull request to target, the same two-commit
bootstrap `repo create --scaffold` uses, written directly). A pull
request rather than a direct push for two reasons that point the same
way: it is the only write a branch an earlier run already protected
accepts at all, and it is what makes the checks the scaffold installs
actually run -- `lanes` and `zizmor` report on that pull request, and a
ruleset cannot require a check that has never passed. A scaffold pull
request an earlier run left open -- its own, by branch prefix and by
author -- is never reopened beside itself: a later run merges it once its
head is exactly the commit this run would generate (the branch's tree plus
the generated files, compared by blob sha), every check on it has passed
and GitHub reports it mergeable (rebase, conditional on the head it read);
replaces it when its head is anything else (the base moved, a template
moved, something was pushed to it); and otherwise says what it is waiting
on -- or, for a failed check, a conflict, a draft or a review block, that
it needs a person. So this
stays safe to run over a fleet on a loop, and the loop closes on its own.
The ruleset step never waits for the scaffold as a whole: it writes what is
safe now and DEFERS, by name and with the reason, each check that cannot be
newly required yet -- one that has never passed on this repository (run,
or run and failed, which the run tells apart), or one whose publishing
workflow is still inside the scaffold pull request. The second question is
asked of the branch, never of the pending pull request: a publisher among
the missing paths is one the branch cannot report from, and whether some
pull request would supply it is not a question with a durable answer --
a pull request is editable by anyone at any moment. So a gap containing
`ci.yml`, `zizmor.yml` or codex-review's check workflow defers that check
until the pull request has merged, and a gap of only `AGENTS.md` defers
nothing. A check the ruleset already requires is never dropped -- not by a
deferral, and not by its absence from the standard: the standard is a
floor, and this tool only adds requirements. Where the branch ALREADY requires a check
whose publisher is one of the missing files, the repository is wedged
before this tool arrives -- nothing can merge -- and `repo setup` says
so rather than leaving it to be discovered. This is what makes `repo setup --force`
fix a repository regardless of its starting state: a
brand-new repo, one only partway set up, and one already complete all
converge on the same result -- behind one combined plan and a single
confirmation for the whole repository -- with `--force` meaning only
"apply without asking"; it waives no guard. By default it prints only what it
actually changed, so `repo list | xargs -n1 repo setup --force` stays
quiet across an already-in-shape fleet; `-v`/`--verbose` shows the full
plan (what a repository already has, not just what moved) and a
progress line per step, for a closer look at one repository or a run
you're debugging. Converging a repository that was missing scaffold
files takes a few runs, one rung each (see SPEC.md's ladder): the pull
request opens, a later run merges it, a later run requires the checks it
publishes once they have passed, and `codex` follows from the first pull
request the sweep sees with its workflow on the default branch. `repo
audit` is the read-only counterpart: it reports whether a branch's rules
(required checks, conversation resolution, up-to-date merges, force-push
and deletion protection, bypass actors, and -- when auditing the
repository's real default branch -- whether every covering ruleset also
targets `refs/heads/main` and `refs/heads/master`, whether a ruleset under
a name this tool used before `main` is still there, and whether more than
one ruleset carries the name `main` at all) already hold, without writing
anything. It also audits where secrets live. The fleet's shared credentials -- the
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
every other repository-level secret is listed by name for review. The lanes
App pair (`LANES_APP_ID` + `LANES_APP_PRIVATE_KEY`, which mikelward/lanes's
`init`, `attest` and `gate` modes publish the required `lanes` status with)
is audited the same way with one substitution the action's shape forces: a
step reads no `secrets: inherit`, so the environment secret reaches it only
through the job's own `environment: lanes` declaration, and a publishing job
(one handing the action `app-id`) without that declaration is the `[FIX]`
instead of a caller naming its secrets. The `lanes` environment itself is
held to admitting only the default branch -- a custom policy naming exactly
it; "protected branches only" is not that, since GitHub reads it as every
branch while no branch-protection rule exists, and a ruleset is not one: the
App publishes the status the ruleset requires, so an environment any branch
can reach hands a same-repo pull request's push-triggered run the same reach
-- an open one is a `[FIX]` that `repo setup` closes, a policy someone set
to something else is a `[FIX]` to close by hand. A repository that does not allow auto-merge is a `[FIX]` too: the weekly
batches arm it on their pull requests. So is one that does not delete a merged
pull request's head branch automatically -- without it nothing sweeps the
branches a merge leaves behind (see `repo cleanup` below). `[FIX]` findings do
not fail the audit yet (see `TODO.md`): the layout is being rolled out through
`repo setup`.

`repo setup` makes the move, and enables auto-merge and delete-branch-on-merge
on the repository where either is off. Its fleet-credentials step is always on: for
each batch the repository runs (by whichever workflows call it from a job,
whatever they are named, on the default branch) and for the
commit-back workflow (by a job calling it), a value passed as
`--credential NAME=PATH` is set in the environment the name belongs in, the
repository-level copy is deleted once the environment holds a usable
credential, and a copy for a workflow the repository does not use is deleted
wherever it sits. "Uses" is read from the default branch alone -- setup only
ever touches the repository and its default branch -- so a caller that exists
only on a non-default branch counts as not-used, and its credential is
cleaned up (a rerun with `--credential` restores it once the workflow is on
the default branch; `repo audit` still reads every branch, and is where such
a branch is surfaced). A value for a workflow the repository does not use is left
unset, which is the difference from `--secret` (which refuses a fleet
credential's name outright, whatever scope it names: `--credential` is the
one flag that places it). It refuses -- and says so, as
`NOT FIXED`, exiting 1 -- while the caller still passes its secrets by name
(an environment secret reaches a called workflow only through
`secrets: inherit`, so the move would hand the workflow nothing) or when the
environment holds nothing and no value was given (GitHub never returns a
secret's value, so a move needs it handed in). The lanes App pair is the
exception to *left unset*: a consumer cannot publish the status as the App
until the pair is already in its environment, so a supplied `--credential`
places the pair even when no workflow publishes as the App yet -- provisioning
ahead of the workflow migration. (A lanes pair present but *not* supplied on
this run, with no publisher, is still deleted as unused, like any other
credential nothing uses.) A publishing job that does not declare the
environment holds the move back; once the pair is settled, an open `lanes`
environment is restricted to the default branch (re-sending its wait timer and
reviewers, which the API would otherwise reset), and a policy someone set to
anything else is reported, never rewritten.

Once the default branch actually publishes the `lanes` status as the App --
an `init`/`gate`/`attest` step holding the pair -- and the run is handed
`LANES_APP_ID`, `repo setup` requires the `lanes` check *from that App* (an
`integration_id` binding) rather than from any producer of the name, so a
same-repo pull request cannot mint its own `lanes` status through the ambient
token and satisfy the gate. The id is read from the supplied `LANES_APP_ID`
value -- the same number the workflow authenticates with, so there is no
wrong-id risk and no App lookup. The binding is written by a **second ruleset
update, after this run's credential move has settled the pair**: the ordinary
ruleset step (which runs first) requires `lanes` unbound -- preserving any
binding already there, never stripping it -- and only once the move succeeds
is the binding applied, so a failed move never leaves `lanes` required from an
App the workflow cannot authenticate as. This makes it a **two-run
migration, the runs any time apart**: one run places the pair (the check
stays unbound), and a later run -- once the App has actually published `lanes`
-- binds it. A run where the App has not published yet defers the binding and
says so rather than failing; the check stays required, unbound, until then.
Re-pointing an existing binding to a *different* App (one is already bound and
a different `LANES_APP_ID` is supplied) is **not** automated: `repo setup`
refuses it -- switching neither the credential nor the binding, leaving the
working App in place -- rather than opening a window where the publisher and
the requirement disagree. Move it by hand for now (re-point the ruleset entry,
then rotate the credential); a safe automated re-point/rotation, where the
switch and the binding commit together, is a tracked follow-up. The deferred
first-bind write is pinned to the state captured right after the main update,
so an edit during the credential move is refused rather than silently
rewritten.
`repo audit` verifies a bound `lanes` the same way GitHub enforces it: the
Statuses API hides the creating App, so it matches the status's `{slug}[bot]`
creator, resolving the slug from the ruleset's `integration_id` through the
account's App installations (a check run carries the id directly). Whether that
App can still publish here -- installed on the owner, and covering this repo --
is a separate precondition, kept out of that evidence scan: `repo setup` refuses
to bind `lanes` to an App that does not cover the repo -- a hard failure
`--force` does NOT override, since binding to an App that cannot report would
wedge every merge -- and `repo audit` reports such a binding as its own gap.
Across a fleet:
`repo list | xargs -n1 repo setup --force --credential NPM_UPDATE_PAT=pat.txt --credential GRADLE_UPDATE_PAT=pat.txt --credential RUST_UPDATE_PAT=pat.txt --credential CI_COMMIT_ARTIFACT_TOKEN=token.txt --credential LANES_APP_ID=app-id.txt --credential LANES_APP_PRIVATE_KEY=app.pem`.
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
