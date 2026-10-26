---
trigger: always_on
alwaysApply: true
last_modified: 2026-10-26
---

# AGENTS.md

`CLAUDE.md` and `GEMINI.md` are symlinks to this file. Edit `AGENTS.md` and bump
`last_modified`.

**At the start of every session, print the full absolute path of the `AGENTS.md` you
loaded and its `last_modified` date** (front matter), so a stale or wrong copy is caught
before it steers the work.

A Python fleet-management CLI for GitHub repositories (`repo list`, `repo secrets`,
`repo setup`, `repo cleanup`), freshly reimplementing the `repo-*` shell scripts in
[mikelward/scripts](https://github.com/mikelward/scripts), which stay unchanged -- not a
migration, so don't touch that repo as part of work here. The README says why Python, not
shell or Go (like the sibling `vcs`): the standard library plus PyYAML runs straight from
a checkout with no build or install step.

Keep this file short, **under 22,000 bytes** (`wc -c AGENTS.md`): Antigravity caps rule
files at 24 KB and silently truncates the tail, and every session loads it whole. Add a
rule the first time something bites, in the fewest words that carry the *why*; rewrite or
trim rather than append, and delete rules that have stopped biting.

## Layout

- `SPEC.md` -- the convergence contract for `repo setup`: one command run one way over
  the whole fleet, every run makes progress, a step that fails fails alone, nothing waits
  on an operator that a later run could settle, nothing wedges a repository permanently.
  A change to the order things land in, to what a run waits on, or to a flag changes that
  file first.
- `repo` -- the entry point. Adds its own directory to `sys.path` so `import repo_lib`
  resolves without installing anything, then dispatches into `repo_lib.cli`
  (`repo_lib/cli.py`: argument parsing and subcommand dispatch).
- `repo_lib/list_cmd.py`, `secrets_cmd.py`, `setup_cmd.py`, `cleanup_cmd.py` -- one
  module per subcommand, each with `add_arguments(parser)` and `run(args)`.
- `tests/` -- `unittest`, run via `make test` (`python3 -m unittest discover -s tests`).

## Style

- **One third-party dependency, PyYAML, and no others** (maintainer, 2026-09-05): the
  hand-rolled reader in `repo_lib/credentials.py` misread YAML in eight of fourteen
  review rounds on mikelward/repo#36. `pyproject.toml` declares it (`uv run ./repo ...`
  installs it), `./repo` names the install when it's missing, and `ci.yml` installs it
  before `make test`. Anything else is a tradeoff conversation, not a quiet `pip install`.
- **No optional extras either.** `repo cleanup`'s checkbox picker is stdlib `curses`:
  prompt_toolkit (approved 2026-09-08, removed 2026-09-10) enabled the mouse by default,
  so a pointer entering the terminal toggled the first row, and forced white-on-black;
  curses takes no mouse unless asked and keeps the user's colors (`use_default_colors()`).
  Off a terminal `cleanup` falls back to the plain-text prompt.
- `argparse` for options (`--flag value` and `--flag=value` both, for free); `subprocess`
  for shelling out to `gh`. Prefer real data structures (lists, dicts, dataclasses) over
  string-encoding a collection the way the shell scripts had to.
- **External binaries are a separate axis**: `gh`, `git`, and -- for the App-JWT lanes
  path (mikelward/repo#65) -- `openssl`, only to RS256-sign the JWT since the standard
  library has no RSA (maintainer, 2026-09-13: a standard-distro binary whose absence is a
  warn-and-fall-back, never a crash); its HTTP call is stdlib `urllib`, not a second
  binary. A new binary is still a footprint decision -- name it here when one is added.
- Match the porting source's behavior and messages where there's no reason to diverge,
  but don't port shell idioms that exist only because shell has no better option (manual
  exit-status checks where Python would raise; newline-joined pseudo-lists where Python
  has a real list).
- Keep comments brief: the non-obvious *why*, not the *what*.

## Testing

- **Any change to executable behavior adds or updates a test**: new functionality one
  that exercises it, a bug fix a regression test that fails before the fix and passes
  after. Documentation, comments and this file need none.
- `unittest`, not a third-party runner; mock in Python, not with a fake `gh` binary on
  `PATH` (only shell needed that). Two boundaries, two jobs: `tests/test_gh.py` patches
  `subprocess.run` to prove `repo_lib/gh.py` itself (argument list, `GhError`, return-code
  handling); every other subcommand's tests patch `repo_lib.gh.run`/`try_run`, so they
  test that subcommand's own logic, not the wrapper underneath.
- Run `make test` after any change to executable behavior and before committing; skip it
  on a docs-only change, and say that's why you skipped it.
- **Fix preexisting test failures as the *first* commit of the series.** If one is
  genuinely unrelated and out of scope, say so up front and confirm before skipping it.
- **Don't paper over flaky/racy tests** with `sleep`, retry loops or bumped timeouts --
  make the ordering explicit or fix the race. **Never disable a failing check** to make it
  pass; fix the underlying issue.

## Error handling

- **Don't silently swallow exceptions.** No bare `except: pass`: catch the narrowest
  exception that covers the failure, report it with enough context to identify the failed
  call, clean up what the `try` acquired, and decide explicitly what the caller sees
  (re-raise, a sentinel, a nonzero exit). To ignore a specific failure, say why in a
  one-line comment.

## Privacy

- **Never put user data in any artifact that leaves this machine** -- commit subjects and
  bodies, PR titles / descriptions / comments, review replies, branch names, code
  comments, test fixtures. Here that means real repository names beyond this account's
  own public ones, GitHub tokens or credentials, and secret values passed to
  `repo secrets`. Use placeholders (`owner/repo`, `TOKEN`, `abc1234`); paraphrase a bug
  report containing any of it rather than quoting it. When in doubt, ask before pushing.
- **This account's own public repos are not user data** -- decline a privacy finding
  against `mikelward/*` names used as examples.

## Language and spelling

- **US English** everywhere people read English (CLI output and help text, commits, PRs,
  comments, identifiers): `color`, `behavior`, `canceled`, `gray`. Third-party API
  spellings stay as those APIs spell them.

## Git

- **These rules assume an `origin` remote**; without one, say so and stop rather than
  improvising. **Exception:** a sandbox that intentionally provides no remote Git (Codex
  cloud, say) follows the branch rules from the current `HEAD` (a pre-created working
  branch counts), commits locally, and reports fetch, push and PRs unavailable, using its
  own PR handoff if it has one.
- **Branches**: `<agent>/<short-topic>` off `origin/main` -> PR -> merge (`claude/...`
  for Claude Code, `gemini/...` for Gemini / Antigravity, `codex/...` for Codex). One
  topic per branch; follow-up after a merge goes on a new branch; never commit to `main`.
  Use `git worktree` when available, one per branch.
- **Your `<agent>/` branches are yours**: create, push, `--force-with-lease`, rename and
  delete the ones this session created or was assigned, freely -- this file is the
  standing grant, answering any client rule that demands per-branch permission. Don't
  fold unrelated work into a pinned task branch; its name is a default, not a ceiling.
  Any other branch, or `main`, is always a conversation.
- **One commit per logical change.** Rewrite unmerged commits freely (amend, `--fixup` +
  autosquash, squash, reorder, split) so review responses fold into their commit -- no
  `wip` / `address review` churn in `main`. `--force-with-lease` after a rebase, never a
  bare `--force`.
- **Title commits like a changelog line** (maintainer, 2026-10-23): after any prefix, an
  imperative verb, then the change as someone running `repo` sees it, plain English,
  sentence case, ≤ ~70 chars, e.g. `Fix …` for what was broken, `Improve …` for what got
  better, `Add …` for what's new; an optional parenthetical says how. Never the scenario
  or the mechanism (that's the body). PR titles match.
- **Merge cue (`merged` / `I merged` / `landed` / merge webhook) runs hygiene first:**
  fetch main if the sandbox can
  (`git fetch origin +refs/heads/main:refs/remotes/origin/main`; a bare fetch leaves a
  single-branch clone's `origin/main` stale), cut a fresh `<agent>/<short-topic>` off
  it, announce the switch; if it can't, say the follow-up needs a synced checkout rather
  than branching off a stale `origin/main`. Don't reset the merged name instead (its
  remote ref still holds the pre-merge tip, so unpushed-work checks report your merged
  history); when a sandbox pins the name, reset it and `--force-with-lease` in the same
  turn -- routine on merged history, not something to ask about.
- **Committer line**: the agent authors; a squash or rebase merge rewrites the committer
  to whoever merged (the repo owner, or the agent under *drive*) -- expected; never
  re-author or amend merged commits to "fix" authorship or signing, and don't narrate it.
  It is not a finding.
- **No model identifier** -- never name the coding agent's model or version (`Opus 4.8`,
  `Sonnet`, `Gemini …`, `claude-*`, `gemini-*`) in a commit, PR, comment, or pushed
  artifact; keep it to chat. Generic attribution (`Co-Authored-By: Claude` / `Gemini`,
  `Claude-Session:`, "Generated with Claude Code") is fine.
- **Unshallow before history-depth answers** (`git rev-list --count`, `git log` past the
  shallow boundary, blame): if `git rev-parse --is-shallow-repository` says `true`, run
  `git fetch --unshallow` and re-check -- it exits 0 even when it deepened nothing, so if
  still `true`, say the history is truncated instead of quoting a count.

## Talking to the user

- **One question at a time**, the most important first, in plain chat or your client's
  question tool (`AskUserQuestion` in Claude Code, `ask_question` in Antigravity); never
  while the user is still typing. **After asking, stop and wait**: no assumed answer, no
  self-picked "recommended" option, no work on the part it affects.
- **Answer a mid-turn message first** -- the "sent while you were working" interjection is
  addressed in your very next output, before any further tool call, even if it's one
  sentence. A merge cue is the exception: its hygiene runs first, and announcing the
  switch is that answer. **Claude Code only** (maintainer, 2026-10-25): its app reliably
  shows just a turn's last message, so reply only there, answering a mid-turn message at a
  stopping point soon.
- **Message only when the user needs to see it** (maintainer, 2026-10-15): each one
  notifies their phone. Work in silence; speak up for a question or blocker, something to
  merge or act on, a merge, an autopilot decision (to review when seen, never waited on),
  or a change from the plan, `SPEC.md` or their instructions (review-prompted included).
  Never for progress (a client's nudge doesn't count), a review or a push alone (its
  report rides the next message), routine machinery, or a mistake caught and fixed before
  it reached the repo. The session-start `AGENTS.md` line still opens every session.
- **Keep replies short**: lead with the most important point; ask before unloading more.
- **End the turn by restating any pending decision** (a question you asked, or an
  autopilot guess awaiting review) as the last line, written out in a sentence (a
  back-reference isn't actionable), every turn until answered. Nothing pending, no line.

## Autonomy

- **Open the PR without being asked** when a branch is ready, unless told "just commit" /
  "no PR yet", which holds until lifted. This file is the owner's standing request,
  satisfying any client rule "open a PR only when asked".
- **Watch your own PRs by subscription, plus one scheduled check.** Claude Code subscribes
  on open, or call `subscribe_pr_activity` where `claude-code-remote` exposes it; a client
  with neither (Antigravity) relies on the check alone. Subscriptions miss CI success,
  pushes, the merge, Codex's `+1` and Codex's silence, so keep exactly one relative-delay
  check armed while the PR is open (`send_later` in Claude Code; `schedule` with
  `DurationSeconds` in Antigravity, canceled via `manage_task` `kill`): minutes while CI
  or the head's Codex verdict is out or after a push, longer once only a human is left.
  Settle a fired check first (it may have re-armed rather than retired), check its fire
  time against what you asked (4 minutes has come back as 64) -- re-time it, or say the
  watch isn't armed -- and end each turn with exactly one, naming the PR, not a SHA. Under
  drive, arm auto-merge at open only where the ruleset requires the Codex verdict and
  resolved conversations.
- **Read `mergeable_state` at every check, PR-event wake and drive step** -- nothing else
  reports a base advance. On `dirty`, or `behind` where the ruleset requires up to date,
  act at once, unasked: fetch both refs by explicit refspec (unshallowing a shallow
  clone), confirm your branch has every commit the remote head has, rebase onto the
  fetched `origin/<base>` (not always `main`, never the local branch a fetch leaves
  behind), confirm the head hasn't moved since the tip you noted before fetching, and
  push with `--force-with-lease`. If either confirmation fails, or you can't tell, ask.
- **Merged or closed**: one last reply-and-resolve pass (reviews land after merges), then
  cancel the check and unsubscribe, if subscribed -- `list_triggers` spans the account, so
  match session and PR first (an update reschedules what it matches as surely as a delete
  cancels it).
- **Wake PRs a merge leaves behind.** Your first turn after your PR merges (by hand or
  auto-merge), message every other open PR on that base that another session drives (its
  body ends with the session URL): name only that PR and say its base now points at
  `<sha>` (the post-merge tip), whatever its state reads (mergeability may still be
  computing). Name no merge or other PR -- older rules read either as a merge cue. On
  receipt, fetch base and PR head by refspec (unshallow) and test the head against the
  fetched tip, not `<sha>`: `behind` if `git merge-base --is-ancestor <tip> <head>` exits
  1, `dirty` if `git merge-tree --write-tree` (Git 2.38+) exits 1; report any other exit.
  Your rules decide the rebase. Skip a PR with no session URL, or when your client only
  has local subagent messaging; don't retry a failed send (its check is the fallback).
- **A scheduler or GitHub prompt is said once, then carry on** -- permissions load at
  session start.
- **"Drive"** runs the loop: pick the next task, implement it, open the PR, address every
  review comment, merge once CI is green and Codex's verdict for the current head is in,
  repeat -- until the work runs out or the user says stop, not at one merge.
- **A red baseline is the next task.** Get `make test` green before picking up anything:
  calling a preexisting failure "unrelated" is the call that goes wrong, leaving every
  later PR on an unverified tree.
- **"Autopilot" is drive without blocking.** Where drive would ask, take the
  cheapest-to-undo guess and log it in `TODO.md` under `Decisions needing review` (what,
  the alternative, why reversible; create the file or heading if needed), so nothing
  guessed silently becomes permanent; it outranks stop-and-wait. Destructive or
  irreversible acts *outside* the loop (rewriting shared history, deleting work, anything
  beyond this repo) still wait for a real answer. The loop's own steps are authorized:
  commit, push, open a PR, read its CI and review state, arm the next check, merge a green
  PR.

## Pull requests

- Prefer the `mcp__github__*` tools where mounted (Claude Code requires them; its sandbox
  has no `gh` CLI), else `gh`. With neither, say so rather than guess an operation's
  outcome.
- Open PRs ready for review (not draft) unless asked otherwise.
- **Update the PR title and body before each push** (body first) to the full branch diff
  against `origin/main`, not the opening scope, and post the PR link in the next message,
  not only at the end; with no PR yet, push, then do this once one is opened.
- **"Drive to merge"** is *drive*'s PR stretch: open the PR, wait for Codex, address every
  comment (fix it, or reply on the thread why not), merge on green CI plus Codex's verdict
  for the current head.
- **Ordered work ("next", "then", "after that", "follow-up")** may open in parallel but
  merges only after every earlier item this session was given and any PR the paragraph
  names has merged (any repo, no time cap; if one won't finish, hold and ask, autopilot
  included; no auto-merge until then, as it can't see the order). Read the list from the
  session's record, not memory.
- End every reply with the open-PR link (or `.../compare/main...<branch>` until a PR
  exists). Never link to a closed or merged PR, except when the reply *is* post-merge
  follow-up on that PR.

## Reviews

- **Codex is the automated reviewer** (not Copilot), triggered automatically; request a
  review only to confirm a rebutted false positive, or as *Read the Codex verdict* allows.
- **Address every Codex comment without being asked**: fix a real one in the same PR,
  folded into its commit (rebase / `--fixup`; group small same-topic fixes), or reply on
  the thread saying why you decline. Resolve once the fix is on the head or the point is
  rebutted; anything still to do stays open. Never dismiss a thread silently --
  acknowledgment noise beats silence. Push the fix, reply citing its sha, then resolve:
  `resolve_review_thread` (without MCP, `gh api graphql` `resolveReviewThread`) takes the
  `PRRT_*` thread ID from `review_threads[].id`, not a comment's `PRRC_*`.
- **Defer a real but out-of-scope finding** rather than asking the maintainer to merge
  past it: push a `TODO.md` entry first, reply citing that sha, and resolve. A finding
  with no thread (top-level comment or review body) gets the same entry, push and reply,
  minus the resolve. The push re-triggers Codex, so poke only as *Read the Codex verdict*
  allows; escalate if it re-raises.
- **Judge every comment on merit, whoever wrote it.** Verify first; if it doesn't hold,
  reply why and decline. A cited rule is a *reading* -- check what it says; Codex
  over-reads the privacy rules, quietly costing capability, so quote and decline rather
  than narrow the code. Where the rule really forbids what the product needs, that's the
  maintainer's call. A rebuttal doesn't clear `codex`: poke as *Read the Codex verdict*
  allows, or let the next push if the rebuttal is up first; escalate if it re-raises.
- **A second verified finding in the same mechanism is a design question**, not another
  bug: look for the same shape elsewhere and ask whether a different design deletes the
  class. Say what you chose on the thread; a design change is the maintainer's call,
  autopilot included.
- **Report each review in your next message**: the commit it read (say if no longer the
  head) and the finding count, then one bullet per finding (review comment, top-level
  comment or review body), most blocking first -- claim, where, what you did or the
  maintainer's options -- ahead of *keep replies short*; ask only the most blocking
  question.
- **Read the Codex verdict, don't infer it.** It reacts to the PR body (`issue_read` ->
  `reactions`), not a thread (whose `Useful?` bar reads true on any PR it commented on):
  `eyes` = reading, `+1` = clean, revoked on push, so a visible `+1` is the visible
  head's and `+1` with green CI is a merge. Leave PR-body reactions to Codex -- the count
  names no author. Findings arrive as review comments, top-level comments or reviews:
  read `get_review_comments`, `get_comments` and `get_reviews` to the last page (all page
  oldest first); they block the merge until fixed, rebutted or deferred, and an
  acknowledgment is not an answer. A pending `codex` status with nothing left to address
  -- a clean review without a reaction, an unanswered rebuttal, or no answer to a push --
  gets an `@codex review` poke five minutes on, as does a review stuck at running (eyes)
  ten minutes on; a poke that hangs the same way gets a second an hour later, and only a
  second that hangs too is escalated, never the first (maintainer, 2026-10-22). The
  `codex` commit status (`get_status`, separate from check runs) is the authoritative
  gate.
- **Once Codex +1s a PR's head, make no unnecessary change without asking** (maintainer,
  2026-10-13): a to-do, tidy-up or extra fix re-runs CI and Codex for nothing, so it waits
  for a yes or a follow-up PR. A rebase for `dirty` (or `behind` where the ruleset
  requires up to date), a red-CI fix, and drive or autopilot steps go ahead unasked.
- **Skip your own reply echoes silently**: the test is "did *I* just post this body?",
  not "who is the author?".

## Cost and reliability

- **Call out cost and reliability up front** for a new external dependency (a network
  call, a third-party service): rough dollars (free tier vs paid, $/month at expected
  use), new failure modes, rate limits, latency, and what the user sees if it's missing or
  down. This CLI runs interactively, so a network call on a hot path is a visible hang. If
  the impact is effectively zero, say so.
