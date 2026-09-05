# AGENTS.md

Conventions for AI agents working in this repository.

`CLAUDE.md` is a symlink to this file, so every agent reads the same
conventions. Edit `AGENTS.md`.

This repository is a Python fleet-management CLI for GitHub repositories:
`repo list`, `repo secrets`, `repo setup`, `repo cleanup`. It is a fresh
implementation of the `repo-list`/`repo-secrets`/`repo-setup` shell scripts in
[mikelward/scripts](https://github.com/mikelward/scripts), which stay in
place unchanged for now -- not a migration, so don't touch that repo as part
of work here. See the README for why this is Python rather than another
shell rewrite or Go (matching the sibling `vcs` tool): no dependencies,
`argparse`/`subprocess`/`unittest` from the standard library only, run
directly from a checkout with no build or install step.

Keep this file as short as it can be and still work. Every session loads it
whole, so each rule costs context on every turn: add one the first time
something bites, say it once in the fewest words that carry the *why*,
rewrite or trim an existing rule rather than appending beside it, and delete
one that has stopped biting.

## Layout

- `repo` -- the executable entry point. Adds its own directory to
  `sys.path` so `import repo_lib` resolves without installing anything, then
  dispatches into `repo_lib.cli`.
- `repo_lib/cli.py` -- argument parsing and subcommand dispatch.
- `repo_lib/list_cmd.py`, `secrets_cmd.py`, `setup_cmd.py`, `cleanup_cmd.py`
  -- one module per subcommand, each with `add_arguments(parser)` and
  `run(args)`.
- `tests/` -- `unittest`, run via `make test` (`python3 -m unittest discover
  -s tests`).

## Style

- No third-party dependencies. Standard library only -- what a fresh
  `python3` gets you is what this runs on, no `pip install` step for a
  contributor or CI to get wrong. If something genuinely needs one, that's a
  conversation about the tradeoff, not a quiet `pip install`.
- `argparse` for option parsing (handles `--flag value` and `--flag=value`
  both, for free). `subprocess` for shelling out to `gh`. Prefer real data
  structures (lists, dicts, dataclasses) over string-encoding a collection
  the way the shell scripts had to.
- Match the porting source's behavior and messages where there's no reason
  to diverge, but don't port shell idioms that exist only because shell has
  no better option (manual exit-status checks where Python would raise;
  newline-joined pseudo-lists where Python has a real list).
- Keep comments brief. Explain the non-obvious *why*, not the *what*.

## Testing

- **Any change to executable behavior adds or updates a test.** New
  functionality gets a test that exercises its behavior; a bug fix gets a
  regression test that fails before the fix and passes after. Changes with
  no behavior to exercise -- documentation, comments, this file -- add no
  test and don't need `make test` run over them.
- `unittest`, not a third-party test runner -- consistent with the
  no-dependencies rule above. Mock, in Python, rather than shelling out to a
  fake `gh` binary on `PATH`; that was necessary in the shell scripts
  because shell has nothing better, and Python does. Two boundaries, two
  jobs: `tests/test_gh.py` patches `subprocess.run` directly to prove
  `repo_lib/gh.py` itself does what it claims (argument list, `GhError`,
  return-code handling); every other subcommand's tests patch
  `repo_lib.gh.run`/`try_run` instead, so a `list`/`secrets`/`setup` test is
  about that subcommand's own logic, not the wrapper underneath it.
- Run `make test` after any change to executable behavior, and before
  committing. Skip it on a docs-only change, and say that's why you skipped
  it.
- **Fix any preexisting test failures as the *first* commit of the series.**
  Don't stack new work on a red baseline. If the failure is genuinely
  unrelated and out of scope, say so up front and confirm before skipping
  it.
- **Don't paper over flaky/racy tests** with `sleep`, retry loops, or
  bumped timeouts. Make the ordering explicit, or fix the underlying race.
  A test that passes "most of the time" is broken.
- **Don't disable a failing check** to make it pass -- fix the underlying
  issue.

## Error handling

- **Don't silently swallow exceptions.** A bare `except: pass` hides real
  failures and burns hours when something eventually breaks. Catch the
  narrowest exception that covers the failure, report it with enough
  context to identify the failed call, clean up anything the `try` block
  acquired, and decide explicitly what the caller sees (re-raise, a
  sentinel, a nonzero exit) rather than letting control fall through
  silently. To ignore a specific failure, say why in a one-line comment.

## Privacy

- **Never put user data in any artifact that leaves this machine** --
  commit subjects and bodies, PR titles / descriptions / comments, review
  replies, branch names, code comments, or test fixtures. For this repo
  that means real repository names beyond this account's own public ones,
  GitHub tokens or credentials, and secret values passed to `repo secrets`.
  Use generic placeholders (`owner/repo`, `TOKEN`, `abc1234`) in examples
  and fixtures. If a bug report contains any of it, paraphrase in the
  commit / PR -- don't quote verbatim. When in doubt, ask before pushing.
- **This account's own public repos are not user data** -- decline a
  privacy finding against `mikelward/*` names used as examples.

## Language and spelling

- Use **US English** everywhere people read English: CLI output and help
  text, commit subjects and bodies, PR titles and descriptions, comments,
  and identifiers -- `color` not `colour`, `behavior` not `behaviour`,
  `canceled` not `cancelled`, `gray` not `grey`. Third-party API spellings
  stay as those APIs spell them.

## Git

- Use `git worktree` when it's available. Give each branch its own worktree
  instead of switching branches in place, so work in progress on one branch
  isn't disturbed by work on another.
- **These rules assume an `origin` remote.** Without one you can't fetch,
  branch from `origin/main`, push, or open a PR -- say so and stop rather
  than improvising a local substitute. **Exception:** in a sandbox that
  intentionally provides no remote Git support (Codex cloud, say), follow
  the normal branch rules from the current `HEAD` -- a pre-created working
  branch counts -- commit locally, and report that fetch, push, and pull
  requests are unavailable, using the sandbox's own PR handoff if it has
  one.
- **Branch naming.** Feature branches are prefixed with the agent's own
  short name: `<agent>/<short-topic>` (`claude/...` for Claude Code,
  `codex/...` for Codex, and so on). The placeholder `<agent>` stands in for
  whichever prefix you use -- don't hard-code `claude/` unless you *are*
  Claude Code.
- **Workflow.** `<agent>/<short-topic>` branch off `origin/main` -> PR ->
  merge. One topic per branch. Follow-up work after a merge goes on a new
  branch. Never commit to `main`.
- **One commit per logical change.** Rewrite unmerged commits freely --
  amend, `git commit --fixup` + autosquash, squash, reorder, split -- so
  each commit that lands is one coherent change, with fix-ups and review
  responses folded into the commit they belong to. `wip` / `address review`
  churn doesn't survive into `main`.
- `git push --force-with-lease` to your own live feature branch after a
  rebase is routine hygiene -- don't ask. Never a bare `--force`.
- **Merge cue (`merged` / `I merged` / `landed` / merge webhook) runs
  hygiene *before* engaging with the rest of the message:** `git fetch
  origin`, cut a fresh `<agent>/<short-topic>` branch off `origin/main`,
  announce the switch.
- **After a merge, take a fresh `<agent>/<short-topic>`** -- don't reset the
  merged name onto the new base. Its remote ref still points at the
  pre-merge tip, so `origin/<branch>..HEAD` keeps spanning the merged
  commits and unpushed-work checks report your own merged history back at
  you. When a sandbox pins the branch name, reset it and
  `--force-with-lease` in the same turn -- that's routine on merged
  history, not something to ask about.
- **Branches under your own `<agent>/` prefix are yours.** Create, push,
  `--force-with-lease` and rename them freely -- no permission, no
  announcement, no per-branch confirmation. Only a branch outside that
  prefix, or `main` itself, is a conversation. Deleting is the one the
  prefix can't settle: it doesn't say which session made the branch, so
  delete the ones this session created and ask about the rest.
- **The agent authors; whoever merges takes over the committer line.** A
  squash or rebase merge rewrites the committer to the person who pressed
  the button -- the repo owner normally, the agent itself when it merges
  under *drive* (see **Autonomy**). That's expected either way -- never
  re-author or amend already-merged commits to "fix" authorship or signing,
  and don't narrate it: no note in the reply, no offer to correct it. It is
  not a finding.
- **Unshallow before answering anything that depends on git history
  depth.** The sandbox clones shallow, so `git rev-list --count`, `git log`
  past the shallow boundary, and blame return wrong answers without
  warning. If `git rev-parse --is-shallow-repository` says `true`, run
  `git fetch --unshallow` first, then re-check -- it exits 0 even when it
  deepened nothing, so if `--is-shallow-repository` is still `true`, say
  the history is truncated instead of quoting a count.

## Talking to the user

- **One question at a time.** Never stack multiple questions in a single
  turn -- ask the most important one, wait for the answer, then ask the
  next if you still need it. A wall of bundled questions is harder to
  answer than a short back-and-forth.
- **Don't interrupt.** Never fire off a question while the user is still
  typing. Let them finish; a half-typed message isn't an invitation to jump
  in.
- **Don't narrate routine machinery.** A check run flipping, a re-run, a scheduled check
  re-arming, a webhook echo, a resolved thread -- act on those silently; the noise buries
  the one line that matters. Reports another rule requires stand (the Codex SHA and
  comment count, a CI timing regression).
- **Don't report your own caught-and-fixed mistakes.** A wrong turn you
  noticed and corrected before it reached anything is not news -- no "one
  thing worth flagging", no narration of the recovery. Say it only when it
  left something the user has to act on: work actually lost, a bad push
  someone may have pulled, a decision they would make differently knowing
  it.
- **Keep replies short -- don't dump a full page.** Lead with the single
  most important point and stop. If there's more, say the first point and
  ask whether they're ready for the next one rather than emptying
  everything at once.
- **End the turn by restating any pending decision.** If you're waiting on
  an answer -- a question you asked, or a guess autopilot recorded for
  review -- the last line of the reply is that question, written out in
  about a sentence. A back-reference ("as asked above") isn't actionable
  when the question is pages back or was never actually put into words;
  restate it every turn until it's answered. Nothing pending, no line.

## Asking questions

- **Ask in chat, never with `AskUserQuestion`.** That's Claude Code's
  multiple-choice question prompt, and it's broken in the Claude mobile
  app -- a question asked through it may be unanswerable. Plain chat also
  keeps the question, its context, and the answer in one readable thread.
- **After asking, stop and wait for the answer.** Don't proceed on an
  assumed answer, pick a "recommended" option yourself, or keep working on
  the part the question affects.

## Autonomy

- **Open the PR without being asked.** Pushing a finished branch and
  opening its pull request are one step, not two -- don't park a branch
  waiting for "please open a PR." The exception is an explicit instruction
  not to ("just commit", "no PR yet"), which holds until the user lifts it.
  This file is the repo owner's standing request for that PR, so a
  client-level rule reading "open a PR only when the user explicitly asks"
  is already satisfied -- the ask is here, and it doesn't need repeating
  per branch.
- **Watch your own PRs by subscription, plus one scheduled check.** Have a
  subscription -- Claude Code makes one when you open a PR; where a client
  doesn't, call `subscribe_pr_activity`. It delivers reviews, comments and
  CI failures. It cannot deliver CI *success*, a push, the merge, Codex's
  clean verdict (a reaction), or Codex never answering at all -- so keep
  exactly one check armed for as long as the PR is open (each event and
  each check costs a model turn). Under drive, arm auto-merge at PR open
  too -- but only where the ruleset makes the Codex verdict a required
  check AND requires conversations resolved: where CI is the only
  requirement it merges before Codex has answered, and an open review
  comment holds nothing back on its own.
  - Settle the fired trigger first thing in the turn, not last. It may
    have silently re-armed rather than retired -- update the one that
    survived, replace the one that didn't, and end the turn with exactly
    one pending.
  - Check the fire time you got against the one you asked for -- a
    4-minute request has come back as 64. Prefer a relative delay: the
    scheduler's clock is not this container's, so an absolute time
    computed here can be rejected as already past. Re-time it, or say
    the watch isn't armed.
  - A few minutes out while CI or the current head's Codex verdict is
    outstanding; longer once only a human is left; short again after a
    push.
  - A PR reading `dirty` -- always -- or `behind` where the ruleset
    requires branches up to date, needs a rebase onto its base and a
    lease-guarded force-push. Nothing reports a base advance, so only this
    check catches it. Fetch both refs by explicit refspec, unshallow a
    shallow clone, and rebase onto the fetched `origin/<base>` -- not
    always `main`, never the local branch a fetch leaves behind. Confirm
    before you rebase that your branch has every commit the remote head
    has, and before you push that the head has not moved since the tip you
    noted before fetching. If either fails, or you can't tell, stop and
    ask.
  - Name the PR, and say what to re-read rather than what you read. A SHA
    or a list of which PRs are open goes stale before it fires; one PR
    number does not, and the trigger has to be matchable to it.
  - Merged or closed, take one last reply-and-resolve pass -- a review can
    land after the merge -- then cancel it and unsubscribe. `list_triggers`
    spans the account, so match this session and this PR before updating
    or deleting one; an update reschedules whatever it matches as surely
    as a delete cancels it.
- **If a scheduler or GitHub call prompts, say so once and carry on.**
  Permissions load at session start, so writing a settings file mid-session
  can't fix the session you're in.
- **"Drive" means run the loop automatically**: pick the next task,
  implement it, open the PR, wait for the automatic Codex review, address
  every comment, merge once CI is green and Codex's verdict for the
  current head is in -- then pick the next actionable item and go around
  again. Driving ends when the work runs out or the user says stop, not
  when one PR merges.
- **A red baseline is the next task.** Before picking up any task, run
  `make test` and get it green. A preexisting failure is work to do, not a
  thing to classify as "unrelated" and step around -- deciding it's out of
  scope is exactly the call that goes wrong, and the cost is every later PR
  merged onto an unverified tree. Fix it first, then pick the task.
- **"Autopilot" is drive without blocking on the user.** Wherever drive
  would stop and ask, autopilot takes its best guess and keeps going,
  preferring the option that is cheapest to undo or change later. Record
  each guess in `TODO.md` under a `Decisions needing review` heading --
  what was decided, what the alternative was, and why it's reversible --
  creating the file or heading if it isn't there, so nothing guessed
  silently becomes permanent. While autopilot is in effect it outranks
  *Asking questions*' "after asking, stop and wait for the answer." The
  carve-out is for destructive or irreversible actions *outside* the loop
  -- rewriting shared history, deleting work, anything reaching a system
  beyond this repo -- which still wait for a real answer. The loop's own
  steps don't count: committing, pushing, opening a PR, reading its CI and
  review state, arming the next scheduled check, and merging a green PR
  are authorized here.

## Pull requests

- Prefer the `mcp__github__*` MCP tools for GitHub operations; the `gh` CLI
  is not installed in the sandbox. If your client exposes neither, say so
  rather than guessing at the outcome of an operation you couldn't perform.
- Open PRs ready for review (not draft) unless asked otherwise.
- **Update the PR title and body with the push, not after it** -- same
  step, so they describe the full, latest state of the branch, not the
  scope it had when it was opened. Re-read the diff against
  `origin/main` and patch whatever drifted, then post the PR link in the
  chat reply for that push, not only at the end of the conversation.
- **"Drive to merge"** is the PR stretch of *drive* (see **Autonomy**
  above): open the PR, wait for the automatic Codex review, address every
  review comment -- fix it if you agree, reply on the thread saying why if
  you don't -- and merge once CI is green and Codex's verdict for the
  current head is in.
- End every reply with the open-PR link (or `.../compare/main...<branch>`
  until a PR exists). Never link to a closed or merged PR -- except when
  the reply *is* post-merge follow-up on that PR.

## Reviews

- **Codex is the automated reviewer on this repo** -- not Copilot. Its
  reviews are triggered automatically; you don't request them, except when
  nothing has come back five minutes after a push -- that means it never
  picked the push up.
- **Address Codex comments automatically -- don't wait to be asked.** Read
  each one, decide whether it's a real issue or a false positive, and if
  it's real, fix it in the same PR. Fold the fix into the commit it
  belongs to (rebase / `--fixup`) rather than tacking on an "address
  review" commit. Group several small fixes into one commit when they
  share a topic.
- **Judge every review comment on merit, whoever wrote it.** Verify the
  claim before acting; if it doesn't hold up, reply saying why and
  decline. A comment citing a rule is a *reading* of that rule, not the
  rule -- check what the rule actually says. Codex misreads the privacy
  rules especially, and in one direction: stricter always feels safer,
  so an over-strict finding quietly costs capability the product needs.
  Quote the rule and decline rather than narrowing the code to satisfy
  it; where the rule really does forbid what the product needs, that
  conflict is the maintainer's call, not one to settle either way
  yourself.
- **A second verified finding in the same mechanism is evidence about the
  design, not another bug.** Before fixing it, look for the same shape
  elsewhere and ask whether a different design would delete the class rather
  than the instance. Say what you chose on the thread; a design change is the
  maintainer's call, autopilot included.
- **Never leave a review comment thread silently dismissed.** Answer on
  the thread, then resolve it once the fix is on the head or the point is
  rebutted -- a disagreement is an answer, so say why and resolve;
  anything still to do stays open. When you think a comment is a false
  positive, say *why* on the thread (one or two sentences). Acknowledgment
  noise is fine and preferred over silence.
- **`resolve_review_thread` works -- pass the `PRRT_*` thread node ID**
  from `pull_request_read` / `get_review_comments`
  (`review_threads[].id`) as `threadId`. A comment's `PRRC_*` node ID
  fails; they're different objects. Order of operations: push the fix
  commit first, then reply citing the new sha, then resolve.
- **Report when Codex finishes reviewing a fresh push** -- a one-liner
  naming the SHA and comment count, e.g. `Codex reviewed 87d9f02 -- 0
  comments`. Tie it to the *latest* pushed SHA so a stale review of a
  superseded commit isn't conflated with the current state.
- **Read the Codex verdict, don't infer it.** It reacts to the PR body
  (`issue_read` -> `reactions`), not to a review thread, whose `Useful?`
  bar reads true on any PR it has commented on. `eyes` means reading, `+1`
  means clean, and Codex revokes it on push -- so a visible one belongs to
  the visible head, and `+1` with green CI is a merge. The count names no
  author, so leave PR-body reactions to Codex: nobody else's is revoked,
  and a review is the attributable form, naming the commit it read.
  Findings arrive as review comments, as a top-level comment, or as a
  review -- read `get_review_comments`, `get_comments` and `get_reviews`
  to the last page, since all three page oldest first -- and they block
  the merge until fixed or rebutted; an acknowledgment is not an answer.
  Nothing from Codex since the push, five minutes on, means it never
  picked it up -- comment `@codex review`, once. The `codex` commit status
  (`get_status`, a separate surface from check runs) is the authoritative
  gate; if it's still `pending` a while after a finding was resolved with
  no unresolved threads left, a single `@codex review` nudge is
  reasonable before assuming something's stuck.
- **Skip echo events silently.** Replies posted via the GitHub MCP come
  back moments later as webhook events authored by the same identity; if
  the body matches a comment you just posted, it's your own echo --
  continue without comment. The test is "did *I* just post this body?",
  not "who is the author?".

## Cost and reliability

- **Call out cost and reliability up front** when recommending a new
  external dependency (a network call, a third-party service). Include a
  rough dollar figure -- free-tier vs. paid thresholds and $/month at
  expected use -- and note reliability implications: new failure modes,
  rate limits, added latency, and what the user sees if the dependency is
  missing or down. This CLI runs interactively, so a network call on a hot
  path is a visible hang. If the impact is effectively zero, say so rather
  than omitting the note.
