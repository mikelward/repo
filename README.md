# repo

Fleet-management CLI for GitHub repositories: `repo list`, `repo secrets`,
`repo setup`. A Python rewrite of the `repo-list`/`repo-secrets`/`repo-setup`
shell scripts in [mikelward/scripts](https://github.com/mikelward/scripts),
which stay in place unchanged -- this is a fresh implementation, not a
migration, and the shell versions remain the source of truth for behavior
until this catches up.

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
           [--secret NAME[@ENV]=PATH]... [--app SLUG]... OWNER/REPO
```

See `AGENTS.md` for testing and contribution conventions.

## Status

`repo list` is implemented. `repo secrets` and `repo setup` are still stubs
that exit with "not yet implemented" -- each is a follow-up PR of similar
size, porting behavior from the shell scripts.
