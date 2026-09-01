# repo

Fleet-management CLI for GitHub repositories: `repo list`, `repo secrets`,
`repo setup`, `repo audit`. A Python rewrite of the
`repo-list`/`repo-secrets`/`repo-setup`/`repo-rules-audit` shell scripts in
[mikelward/scripts](https://github.com/mikelward/scripts), which stay in
place unchanged -- this is a fresh implementation, not a migration, and the
shell versions remain the source of truth for behavior until this catches up.

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

Python 3 (standard library only -- no dependencies to install) and the `gh`
CLI, authenticated.

## Usage

Run `./repo` directly from a checkout; nothing to install.

```
repo list [--owner OWNER] [--include-forks] [--include-archived]
repo secrets --name NAME [--env ENV] --file PATH [--force] OWNER/REPO...
repo setup [--dry-run] [--force] [--no-rules] [--rule CHECK]...
           [--secret NAME[@ENV]=PATH]... [--credential NAME=PATH]...
           [--app SLUG]... OWNER/REPO
repo audit [--branch NAME] OWNER/REPO [CHECK...]
```

See `AGENTS.md` for testing and contribution conventions.

## Status

`repo list`, `repo secrets`, `repo setup`, and `repo audit` are all
implemented -- every shell tool in mikelward/scripts now has a Python
equivalent here. `repo setup` composes three steps -- the required-checks
branch ruleset (plus a standalone warning when a repository has an actual
`master` branch), fanning a secret out via the same logic `repo secrets`
uses, and ensuring GitHub App installation membership -- behind one
combined plan and a single confirmation for the whole repository. `repo
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
batches arm it on their pull requests. `[FIX]` findings do not fail the audit
batches arm it on their pull requests. `[FIX]` findings do not fail the audit
yet (see `TODO.md`): the layout is being rolled out through `repo setup`.

`repo setup` makes the move, and enables auto-merge on the repository where it
is off. Its fleet-credentials step is always on: for
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
See `TODO.md` for where the port deliberately diverges
from the shell porting source.
