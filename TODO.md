# TODO

## repo audit

- [x] Port `repo-rules-audit` (mikelward/scripts#216's current, fully-
      hardened version) as a new read-only `repo audit` subcommand
      (`repo_lib/audit_cmd.py`). This was the last shell tool without a
      Python equivalent. Ported: every effective-rule check (pull request
      required, conversation resolution, per-named-check required status
      check + strict/up-to-date policy, force-push and deletion
      protection), the bypass-actor scan over rulesets that plainly cover
      the audited branch, and -- when the audited branch is directly
      confirmed to be the repository's real default (not merely "no
      --branch was given") -- the targeting-completeness check: whether
      every such ruleset also targets `refs/heads/main` and
      `refs/heads/master`, with the literal-first-then-glob-fallback
      ordering and the two separate "unevaluated suppresses the success
      summary" paths preserved exactly (see `audit_cmd._branch_coverage_
      verdict`/`_targeting_status`'s own docstrings). `rules.
      check_master_branch` is reused directly (with a new `quiet=`
      parameter and a `(status, detail)` return, additive and ignored by
      `repo setup`'s existing call site) rather than reimplemented, and
      `rules.DEFAULT_CHECKS` is reused for the same default-check list
      `repo setup` uses, so the two can't drift.

- [ ] **Warn about branches that could expose a fleet credential.** `repo
      setup` now reads the default branch only (see "repo setup: fleet
      credentials"), so the branch scan lives only in `repo audit`, which
      already reads every branch. Have audit name a non-default branch whose
      workflow could actually reach a fleet credential -- a caller/publisher
      on that branch while the credential is reachable from it (a repository-
      level copy, or an environment whose policy admits the branch) -- as an
      exposure warning, distinct from setup's own credential-state report.
      This is the visibility half of the main-only setup change (maintainer,
      2026-09-10): setup acts on `main`, audit surfaces branch risk. Own PR.
      Part of the same follow-up (Codex, mikelward/repo#55): move audit's
      credential-state analysis/remediation to the default-branch scope so it
      agrees with setup -- a credential reached only from a non-default branch
      reads as unused / any `repo setup` recommendation matches what setup
      would do (default-only), rather than an `[ok]` or a "rerun setup" that
      setup would treat as unused and delete. Audit keeps reading every
      branch, but that scan feeds the exposure warning, not the used/unused
      verdict.

## repo setup

- [x] Port `repo-rules`'s ruleset composition from mikelward/scripts, with
      the hardened branch targeting built in from the start rather than
      the old single-target shape: a ruleset this creates targets
      `~DEFAULT_BRANCH`, `refs/heads/main`, AND `refs/heads/master`
      together (`repo_lib/rules.py`, exercised via `repo setup`), and
      `check_master_branch` warns on stderr — without failing the run —
      when the repo has an actual `master` branch. Ported: create vs.
      update, the ownership check (refuses a same-named ruleset GitHub
      does not actively enforce; it also refused one holding an unmanaged
      rule type until that was found to be guarding against a write this
      module never makes), the never-reported-check guard, the
      merge-method conflict scan against a repo's other active rulesets,
      and the confirm/re-validate-before-write flow.

- [x] Port repo-setup's other two steps: fanning a secret out (reusing
      `secrets_cmd.py`'s own plan/write functions directly, not
      reimplementing them) and ensuring GitHub App installation membership
      (`repo_lib/apps.py`, native — no sibling Python implementation
      existed to port from, matching the porting source's own native
      shell implementation). Both are wired into `repo setup`'s combined
      plan/single-confirm/apply-every-step-regardless-of-earlier-failures
      flow, alongside the ruleset step. See "Decisions needing review"
      below for where this diverges from the porting source and why.

- [x] **Fleet config file for `repo setup`.** The convergence flags
      (`credentials`, `rules`, `apps`, `force`) are fleet constants, so they
      live in `$XDG_CONFIG_HOME/repo/config.yaml` (read by default) instead
      of being retyped every run; the command line overrides it, `--no-config`
      ignores it (`repo_lib/config.py`). YAML because it is the one existing
      dependency; TOML's `tomllib` is 3.11+ and the floor is 3.9. safe_load
      + strict schema (unknown key / wrong type = usage error). Holds paths,
      never secret values, never `--secret`.
- [ ] **Config value resolution beyond a path (secret manager).** A
      credential entry is currently a path the value is read from, same as
      `--credential NAME=PATH`. The maintainer expects to want a real secret
      store; the pluggable form is to let a value be sourced from a **command**
      whose stdout is the secret (`op read ...`, `pass ...`, `aws
      secretsmanager get-secret-value ...`) -- the escape hatch to any manager
      with no new dependency. Cost/reliability to weigh first (AGENTS.md): a
      command resolver runs an external process, and a network-backed one is a
      visible pause on this interactive CLI's hot path, plus a new failure mode
      if the manager is down. Design the shape so `NAME: path` stays the simple
      case and `NAME: {command: [...]}` (or similar) is the opt-in. Still an
      open conversation, not a decided build.
- [ ] **Reach the App-installation facts without `user/installations`.**
      Every App read in `repo_lib/apps.py` -- `app_slug_for_id`,
      `app_covers_repo`, `resolve_installation`, and the `--app` membership
      writes -- goes through `user/installations`, which GitHub serves only
      to a GitHub App user-to-server token. `gh auth login` issues an
      OAuth-App token and a PAT belongs to no App, so both are refused with
      a 403 no matter their scopes (maintainer's own run, 2026-09-12: "You
      must authenticate with an access token authorized to a GitHub App").
      So on an ordinary gh login `--app` cannot run at all, and a `lanes`
      binding whose only evidence is a commit status cannot be verified --
      the Statuses API hides the creating App, so the id has to be resolved
      to a `{slug}[bot]` login somehow. The preflight added with this entry
      makes both fail early and say why; it does not make them work. Worth
      finding out what a gh-obtainable token CAN answer: a check run
      carries `app.id` directly (already used), `GET /apps/{slug}` is public
      but takes a slug rather than an id, and the installation endpoints
      that take a repo need an App JWT. If nothing closes the gap, the
      honest fix may be to state that App membership and bound-status
      verification need their own token, and let the rest converge without
      them.
      *Investigated 2026-09-12:* `GET /apps/{slug}` (public, no installation
      access) resolves a public App's slug->id and would replace the
      `user/installations` lookup on the evidence path -- but the fleet's
      lanes App is PRIVATE, so it 404s for it, and making it public is a real
      change (anyone could install it; its scopes become readable). The
      private-App path is **2b**: pass the App's `id <-> {slug}[bot]` pairing
      (a fleet constant) so the bound-status check is a local compare against
      the configured login -- no `user/installations`, no network. With the
      config file landed that pairing is one more entry, not an extra flag, so
      2b is the leading option; `--app` membership WRITES still need a real
      App token (option 4) and stay parked.
      *2b DONE (mikelward/repo#63, chosen 2026-09-12):* config `app_logins`
      (App id -> bot slug) registered via `apps.register_known_slugs`, which
      `app_slug_for_id` consults before the `user/installations` read -- so a
      bound `lanes` check's `{slug}[bot]` status creator is verified with no
      installations call. Wired into `repo setup` only (it reads the config).
      **Remaining:** (a) `repo audit` does not read the config, so its own
      bound-`lanes` verification still hits `user/installations` -- wire the
      same pairing in when audit grows a config read; (b) BINDING a lanes
      check still runs the coverage precondition (`apps.app_covers_repo`,
      also `user/installations`), so first-bind/rebind on a gh-auth token
      still can't proceed -- that needs option 4 (a real App token) and the
      pairing does not address it. Coverage cannot be asserted from config
      the way a slug can: it is current-state ("can this App act here now"),
      not a stable fact.

## repo audit and repo setup: the fleet credentials

- [x] Audit where the fleet credentials live (`repo_lib/credentials.py`,
      shared with `repo setup`): a `[FIX]` for a repository-level copy, a
      batch consumer with no credential, a stale copy for a batch the
      repository does not run, and a repository-level
      `CI_COMMIT_ARTIFACT_TOKEN`; every other repository secret listed
      under `[CHECK]`.
- [x] `repo setup --credential NAME=PATH`: the fleet-credentials step,
      always on -- sets a supplied value in its environment where the
      repository uses the workflow, deletes the repository copy once the
      environment holds a usable credential, deletes stale copies, and
      refuses under a caller that still names its secrets.
- [x] Enable auto-merge on the repository from `repo setup`, and report it
      off as a `[FIX]` in `repo audit` (maintainer, 2026-09-01: it was off on
      readmo and "probably is for a few repos"; the weekly batches arm it on
      their pull requests).
- [x] Enable GitHub's "automatically delete head branches" setting
      (`delete_branch_on_merge`) from `repo setup`, and report it off as a
      `[FIX]` in `repo audit` (maintainer, 2026-09-04: the user asked how to
      stop having to run `repo cleanup` by hand after every merge). This is
      the setting `repo cleanup`'s own docstring already named as the reason
      it has to exist; the fleet never turned it on, so branches kept
      accumulating until someone ran the sweep. Fires from the merge event
      itself, so it is unaffected by this fleet rebase-merging (unlike an
      ancestry check). `repo cleanup` still owns the backlog every repository
      already has, plus what this setting can't see: a merge with no pull
      request, or a branch still unmerged.
- [ ] **Promote `[FIX]` to `[GAP]` after the next setup pass.** `[FIX]`
      findings are reported but do not fail `repo audit` (maintainer,
      2026-09-01: "keep it lax enough to accept the current standard ...
      and keep a Todo to tighten it after the next setup pass"). Once
      every repository has been through `repo setup --credential ...`,
      route them through `gap()` instead of `fix()` in
      `audit_cmd.audit_secrets`, `audit_auto_merge`,
      `audit_delete_branch_on_merge`, and `audit_legacy_rulesets`, and
      flip the `assertEqual(code, 0, ...)` assertions in
      `SecretsAuditTest`, `AutoMergeAuditTest`,
      `DeleteBranchOnMergeAuditTest`, and `LegacyRulesetAuditTest` -- they
      are written to have to change. The legacy-ruleset one has its own
      condition on top: `repo setup` closes it only when the duplicate is
      identical to the survivor, so promoting it before the fleet has been
      swept would fail every repository holding one that genuinely differs
      and needs a human. Until then the hubs and every converted
      caller read a repository-level credential through `inherit`, so
      nothing is broken by the lax reading, only less isolated than it
      will be.

## repo setup: fleet CI scaffold

- [x] `repo setup` fills in whichever of the fleet's own CI scaffold
      files an already-existing repository is still missing, always on
      like the fleet-credentials and auto-merge steps (`--no-bootstrap`
      to skip it): builds the same file set `repo create --scaffold`
      generates, diffs it against the target's current tree, and adds
      whatever's missing as one commit -- never overwriting a path
      already occupied by anything, file or directory. Applies BEFORE
      the ruleset step in the same run, which is what the one remaining
      direct write (a branch with no commits yet) needs: a ruleset
      requiring pull requests blocks it (Codex review, mikelward/repo#14).
      Everything else goes in as a pull request -- see below.
- [x] **The scaffold no longer writes a `TODO.md`, and that stands**
      (maintainer, 2026-09-04: "i'm not sure if we need that"; kept
      removed under autopilot, 2026-09-04 -- see *Decisions needing
      review*). The file carried exactly one item -- replace ci.yml's
      placeholder job with the project's real jobs -- and ci.yml already
      states the same thing in a comment directly above that placeholder,
      so the scaffold was writing a second copy of a single instruction
      into a second file, where the two could drift and where a project
      that keeps its own TODO.md would find the path taken. Removed;
      `build_scaffold_files` now produces nine files, and the instruction
      lives only where the work is. Bring it back only with something to
      say that ci.yml cannot say in place -- a fleet-wide checklist a new
      repository should work through, say -- not as a restatement of the
      placeholder comment.

- [x] **Populate `CLAUDE.md` and `AGENTS.md` from a template**
      (maintainer, 2026-09-04). A freshly created repository was the one
      place in the fleet an agent worked with no conventions loaded at
      all, which is exactly when it is most likely to invent some. The
      scaffold now writes both. See *Decisions needing review* for the
      four calls this took -- where the template lives, what it holds, how
      `CLAUDE.md` gets made, and what happens where one already exists as
      a symlink.

- [x] **The bootstrap failure names `--no-bootstrap`.** GitHub's own
      rejection, relayed verbatim, says nothing about what to do next, so
      the failure names the way past it. The cause that prompted this --
      a ruleset blocking the direct ref update -- is gone with the write
      path itself (the item below, which the maintainer had deferred on
      2026-09-04: "branch-and-PR write path is a TODO for later"); what
      the line now covers is a pull request this step could not open,
      where `--no-bootstrap` gets the rest of `repo setup` through
      meanwhile and the branch it already pushed is named so the pull
      request can be opened by hand.

- [x] **Which `lanes.conf` docs rule `repo setup` generates: settled.**
      The fleet was split: eight repositories including `mikelward/lanes`
      use the shorthand `docs **/*.md`, while `mikelward/lanes`'s own
      README documents `docs *.md` + `docs docs/**/*.md` as the standard.
      Maintainer, 2026-09-06: `*.md` and `**/docs/*.md` -- "it can expand
      itself later as needed". So neither of the two forms already in the
      fleet, and chosen on that reasoning rather than by counting them: a
      fresh repository should start with the smallest pair that covers
      where it actually puts prose, and widen when its own layout calls
      for it, which is cheap; starting broad and discovering later that
      code has been riding the docs lane is not. Note the difference from
      the README's second rule, which is deliberate: `**/docs/*.md` is
      markdown sitting directly in any `docs/` directory at any depth,
      where `docs/**/*.md` is the whole tree under a top-level one.
      `_LANES_CONF` in `scaffold.py` now writes it, and `_docs_lane_only`
      reads the same two rules so the gap commit's subject prefix agrees
      with the config it ships beside.

      Keeping the one argument worth keeping from the autopilot guess this
      replaces (removed from *Decisions needing review*, since it is no
      longer a guess): the shorthand `docs **/*.md` is the wrong direction
      for a repository nobody has looked at yet, because it routes
      markdown at ANY depth down the docs lane -- a README sitting beside
      code included -- so it can skip a code lane on a diff that needed
      one. Both rules chosen here can only cost a docs-only change a full
      CI run, which is the error worth making.

- [ ] **The fleet still needs converting to that rule.** Eight
      repositories carry `docs **/*.md` and the rest the README's narrow
      pair; nothing here changes either. `mikelward/lanes`'s own `TODO.md`
      carries the matching entry, and its README documents the old pair as
      the standard, so that wants updating too.

- [x] **The gap-fill goes in as a pull request, not a direct push.** Two
      problems, one fix. A repository a PRIOR run already protected had
      no path to a scaffold fix at all -- the direct
      `git/refs/heads/{branch}` PATCH is exactly what a pull-request rule
      blocks for a non-bypass caller, which `repo setup` never configures
      itself to be. And a direct push does not run the checks the
      scaffold exists to install, so `lanes`, `zizmor` and `codex` stayed
      never-reported and the ruleset step could never require them.
      `scaffold.open_gap_pull_request` now pushes the missing files to a
      `repo-setup/fleet-ci-scaffold-<sha>` branch and opens a pull
      request against the default branch; `lanes` and `zizmor` report on
      it. One an earlier run left open is found (by that branch prefix,
      in this repository only -- a fork's branch of the same name is not
      ours) and reported rather than reopened beside itself, read once at
      plan time and again right before the write. A branch with no
      commits still bootstraps directly: there is no base for a pull
      request to target. Where every missing path rides the docs lane the
      commit subject takes the `docs` prefix its own lanes.conf requires,
      so the pull request does not fail the gate it is installing. Until
      that pull request merges, the ruleset step holds back a ruleset
      write that would need it to have landed: one requiring pull requests
      for the first time, or -- on a branch that already requires them --
      one newly requiring a check. Narrowed to the gap that actually
      blocks a check: `scaffold.CHECK_PUBLISHERS` says which file
      publishes each check and whether GitHub reads it from the pull
      request's head or the base branch. Where a pull request was opened,
      a base-published publisher (`codex`) is beyond it, as is any
      publisher that pull request does not itself carry; where none was --
      the step failed, or the token could not write the workflows -- any
      missing publisher blocks, since there is nothing to report on. The
      warning about a check the branch ALREADY requires reads GitHub's
      effective rules, not the managed ruleset, since rulesets
      aggregate. A gap of only `AGENTS.md` holds nothing back either way, even
      when adding it failed. `--dry-run` previews the same skip, and the
      branch tip the whole assessment rests on is re-verified immediately
      before the ruleset write. (Superseded 2026-09-10: the hold is now a
      per-check deferral inside the ruleset write, and the pull request is
      merged by a later run -- see *Converging takes a few runs* below.)

- [ ] **Key the scaffold branch name to the paths it adds.** Reusing an
      open scaffold pull request is matched by branch prefix -- an
      IDENTITY test ("this tool opened it") answering a CONTENTS question
      ("does it add what is missing now?"). The gap between the two is
      bridged by a separate `pulls/{n}/files` read (`_uncovered_by`), and
      six review findings on mikelward/repo#42 were all that one shape:
      the canonical-name comparison, the workflow-scope gate reading
      around it, the contents check itself, the uncovered-workflow gate, a
      file the pull request DELETES counted as coverage, and a cached
      coverage answer going stale across the confirmation wait. Naming the
      branch `repo-setup/fleet-ci-scaffold-<hash of the sorted paths>`
      would make the name itself the answer: a matching name covers
      exactly this gap by construction, a changed gap takes a different
      name, and there is nothing left to read, misparse or cache. Hash the
      PATHS, not their bytes -- the templates are fetched live, so hashing
      content would report a mismatch every time upstream moves. Not free:
      a pull request opened by today's code carries the old sha-keyed
      name, so the files read stays as a fallback while any such pull
      request is open, and the prefix lookup stays either way (it is what
      keeps a second, conflicting pull request from being opened, which is
      why "one branch per gap, several open at once" is NOT the answer --
      a changed gap almost always overlaps the old one). Maintainer,
      2026-09-06: keep the current shape for now, revisit if the reuse
      path produces another finding.

      Revisited the same day, on the seventh finding, and the case is
      weaker than it first looked: a name says what this step OPENED, not
      what the branch holds now. A branch edited after the fact -- which
      is exactly that seventh finding, a scaffold pull request altered to
      delete a workflow still present on the default branch -- defeats a
      name-derived answer precisely as it defeats a cached one. So the
      contents read cannot be deleted, only narrowed: content-keying
      would remove the read for the untouched case and nothing more.
      Whatever the branch name says, the trustworthy answer comes from
      reading what the pull request actually does -- and the step no
      longer reads it at all (see the item below), so content-keying now
      buys nothing but a tidier name. Left here only because the name is
      still an identity match, and a future feature that DOES need to know
      what a scaffold pull request contains would face the same choice
      again.

      Revisited again on the eighth finding, which is a different and
      much better argument for the same change: two `repo setup` runs
      overlapping on one repository. Both list open pull requests, both
      find none, and both build a commit -- a second apart, so the
      commit-keyed names differ, GitHub accepts a scaffold pull request
      from each, and the "one scaffold pull request per repository"
      promise is broken. Keyed to the paths, both runs pick the SAME
      branch name, GitHub grants that ref to exactly one of them, and the
      loser finds the winner's pull request instead of opening a second.
      That is the name as an atomic CLAIM, not as an answer to a contents
      question -- which is why the objection above doesn't touch it:
      nothing is concluded from the name, both runs only have to pick the
      same one. It needs two more things beside the rename: a ref that
      already exists must be accepted whatever it points at (under a
      stable name the shas always differ, so today's equality check would
      refuse exactly the race the name settles), and a pull-request create
      that GitHub refuses because one already exists for that head must
      re-list and report it rather than fail. Maintainer, 2026-09-06:
      descoped from mikelward/repo#42 -- the race is real but narrow (it
      needs two concurrent runs on one repository), today's behavior fails
      loudly rather than silently, and this is a design change that
      deserves its own branch.

- [x] **Stopped vouching for a pending scaffold pull request.** The
      bootstrap step used to READ an open scaffold pull request -- what it
      adds, deletes, renames away, whether its head moved since -- so the
      ruleset step could decide the gap blocked no check and proceed. Ten
      review findings on mikelward/repo#42 were that read being wrong in a
      new way: a deleted path counted as coverage, a renamed-away path
      invisible, an answer cached across the confirmation wait, a head
      force-pushed after the read, a branch edited into something the step
      never opened, a pull request closed or retargeted while its diff
      still read the same. A pull request is mutable by anyone at any
      moment, so no answer derived from one stays true, and each fix bought
      one instance. Now nothing is derived: `checks_a_gap_leaves_
      unpublished` asks only of the BRANCH -- is a publisher among the
      missing paths? -- and a gap that contains one holds the ruleset back
      until the pull request merges. Deleted `_read_coverage`, the
      `uncovered`/`removes` fields, three re-reads and every staleness
      guard around them. The cost is real and bounded: a repository whose
      gap contains `ci.yml` waits one merge for its ruleset, where before
      the run could reason that the pull request would supply it. A gap of
      only `AGENTS.md` -- most of this fleet -- contains no publisher and
      defers nothing, which is the case an earlier round of that review
      was right to protect.

- [x] **The scaffold updates the byte-pinned files, and only those.**
      A scaffold file already on a branch used to be left exactly as it
      was, template drift included, on the reasoning that the difference
      might be a project's own customization. That is true of `ci.yml`,
      both zizmor files, `lanes.conf`, `AGENTS.md` and `CLAUDE.md`, and
      they are still add-only. It is never true of codex-review's three
      workflow files: `codex-review-check` compares them byte for byte in
      every consumer, so a differing copy is a superseded shape (the
      migration codex-review's own TODO asks every consumer to make) or
      drift that already fails that check, and the current template is
      the right content either way. `plan_gaps` now compares those three
      by blob sha against the fetched template -- no content read -- and
      the gap pull request replaces an outdated copy alongside whatever is
      missing (`GapPlan.outdated`, `scaffold.UPDATED_PATHS`; SPEC.md,
      *What is updated*). No flag and no separate confirmation: it rides
      the same pull request, and a pull request is the review.

- [x] **Converging takes a few runs, one rung each, and no person.**
      Two changes, both from the convergence contract in SPEC.md
      (maintainer, 2026-09-10: "running repo setup should make progress
      each time ... it's fine for it to set a new workflow, then the next
      run can check it ran successfully and add the ruleset"). First, the
      ruleset step no longer skips itself: a check that has not PASSED
      here, or whose publishing workflow is still inside the scaffold pull
      request, is deferred by name inside the write, and everything else
      -- pull-request protection, linear history, force-push protection,
      the checks that have passed -- lands now (`rules.never_passed`,
      `apply_ruleset(defer=...)`). Second, `repo setup` merges its own
      scaffold pull request on a later run once every check on it passed
      and GitHub reports it mergeable, rebase, conditional on the head it
      read (`scaffold.assess_gap_pull_request`, `merge_gap_pull_request`)
      -- and only when its head is exactly the commit this run would
      generate, which is what makes a pull request known only by name safe
      to merge; a stale one is closed and replaced. `codex` still reports
      only from a pull request the sweep sees after its workflow is on the
      default branch -- the next scaffold update, a dependency batch,
      anything -- and is deferred until then, which costs nothing else a
      wait. `--force` no longer waives any guard: it means "apply without
      asking", and the fleet loop passes it on every run.

- [ ] **A scaffold pull request the codex sweep never reads waits forever.**
      The sweep sets `codex` pending when the pull request opens, and Codex
      reacts only once it reviews; when it never picks the pull request up
      (seen on mikelward/repo#56, where `@codex review` had to be posted by
      hand more than once), `repo setup` reads the pending status as a wait
      on every run and exits 0. A run that finds the status still pending
      from the run before could post `@codex review` once, and hold after
      that -- the one nudge the reviewer documents, made by the tool that
      is waiting on it.

## repo setup: one ruleset per repository

- [x] **Converge on a single branch ruleset named `main`.** The name was
      `merge gates`, chosen before there was a fleet to be consistent
      with; half the fleet then had a hand-made ruleset called `main` and
      the other half gained a second one beside it (maintainer,
      2026-09-04). Rulesets AGGREGATE -- a pull request must satisfy every
      one covering the branch -- so a duplicate is not broken, just
      confusing, except that `allowed_merge_methods` INTERSECTS and a
      genuine conflict there leaves nothing able to merge at all. Now:
      `DEFAULT_RULESET_NAME` is `main`, and a ruleset found under a legacy
      name (`LEGACY_RULESET_NAMES`) is adopted and renamed in the same
      write, so its bypass actors, scope and any extra rule type survive.
      `adopted_legacy` joins the fingerprint, since a rename makes the two
      rulesets identical in `target_body` and nothing else distinguishes
      "this is the standard one" from "this is about to become it".

- [x] **Delete EVERY legacy-named ruleset, not only an identical one.**
      Converging on one ruleset is the point of the rename, so a
      leftover under a legacy name goes whatever it holds (maintainer,
      2026-09-07); rulesets aggregate, so leaving it means both apply
      forever. Its full body is written to the run's log immediately
      before the delete (`_record_deleted_ruleset`), which is what makes
      this safe: GitHub hands back no copy of a deleted ruleset, so
      recording it turns the one unrecoverable outcome into a POST of
      the JSON in the log. A failure to record cancels that deletion.
      The plan and the run both say when the one going is NOT identical
      to what survives, so what is being dropped is stated rather than
      implied.

      **A duplicate that rules out rebase still refuses the run**, as it
      did before: `_find_merge_method_conflicts` sees it, the write is
      declined, and the deletion that would have fixed it never happens.
      Waiving the scan for a ruleset about to be deleted was tried and
      taken out again -- the waiver is only sound if the delete succeeds,
      so it needs the write undone when the delete fails, and that
      compensating write is a transaction GitHub does not offer: it can
      be stale, can fail itself, and cannot un-delete a ruleset that
      already went when a second one fails (Codex review,
      mikelward/repo#46). Delete the duplicate by hand and rerun. Worth
      revisiting only with an approach that needs no rollback.

      **Two narrower gaps in the recovery record, also deferred** (Codex
      review, mikelward/repo#46). An edit landing between
      `_still_deletable`'s read and the DELETE leaves the record one
      revision stale: the DELETE names an id with no version
      precondition, and GitHub's rulesets API offers no conditional
      delete to bind it to the body that was read. And `write_now`
      fsyncs the log file but not its directory entry, so a power loss
      between GitHub accepting the DELETE and that metadata landing
      could take a newly created log with it. Both leave the record
      slightly weaker than "exactly what was deleted, always"; neither
      touches the case it exists for, which is an operator wanting the
      ruleset back. The directory fsync is a few lines if it ever
      matters; the stale-snapshot window needs an API feature that does
      not exist.

      The equality test it replaced (`_comparable_ruleset`) survives as
      the source of that "not identical" note, where being one field
      short costs a vaguer message rather than a wrong deletion. It was
      the gate rather than "is A at least as strict as B", which had
      been reimplemented field by
      field and found one item short five times in a row -- an unmanaged
      rule type, a ref the other did not cover, a stricter managed
      parameter, a required check bound to a specific App via
      `integration_id`, a bypass actor on one side only. Equality cannot
      be one item short; what it costs is keeping a duplicate that
      differs harmlessly, which is the safe direction. The deletion is a
      planned mutation: shown in `--dry-run`, counted by
      `setup_cmd.run`'s confirmation gate (`ruleset_needs_mutation` reads
      `deletions` as well as `needs_write`), part of the fingerprint, and
      recomputed fresh before it happens. It runs on the no-write path
      too, since an already-correct ruleset is the steady state. It
      happens AFTER the write, because what makes the duplicate safe to
      delete is that the survivor holds everything it held -- true only
      once the write has landed -- and a failed delete fails the step.
      Immediately before each delete, `_still_deletable` re-reads both
      rulesets and returns the candidate's body, which is what gets
      recorded -- so the record is the ruleset actually removed, not the
      copy the plan read a network round trip earlier (Codex review,
      mikelward/repo#31). What that fresh read establishes is no longer
      "still identical", which decides nothing now, but that the two are
      still the two this run reasoned about: a rename landing in that
      window -- the duplicate becoming `main`, the survivor becoming
      something else -- would otherwise delete whichever had just become
      canonical and leave nothing under the name. A read it cannot make
      keeps the duplicate, with the same reasoning as an unreadable one
      in the plan: no body read means nothing to record, so the delete
      would be the unrecoverable kind.

- [x] **`repo audit` reports a surviving legacy ruleset.** `repo setup`
      notes one on every run, which is how the fleet found them, but that
      meant running a write command against a repository to ask a
      read-only question. `audit_legacy_rulesets` reports a `[FIX]` for
      each ruleset named in `LEGACY_RULESET_NAMES`, reusing
      `rules.find_legacy_rulesets` (repository-owned only -- an org-level
      ruleset sharing the name is not this tool's to touch) so the two
      commands cannot come to disagree about which names count.

- [x] **Widening `main`'s own scope to the hardened three refs.** An
      update used to leave an existing ruleset's conditions alone, so a
      hand-made `main` kept whatever narrow scope it was given -- while a
      freshly created one got `~DEFAULT_BRANCH`, `refs/heads/main` and
      `refs/heads/master`. Now `_widen_include` appends whichever of the
      three the ruleset does not already name, and it only ever widens:
      existing entries stay (a ruleset also covering a release branch
      keeps covering it), `~ALL` is left alone since it already subsumes
      all three, and exclusions are never edited -- the plan names one
      instead, since a ruleset excluding `refs/heads/master` still
      excludes it after the include list gains it. `_compute_scope` now
      returns the POST-widening scope, so the merge-method conflict scan
      evaluates the refs the write actually brings into range rather than
      the narrower ones it replaces.

- [x] **Two rulesets under the STANDARD name: settled, and the answer is
      that there was less to do than it looked.** GitHub does not make a
      ruleset's name unique within a repository, and
      `_lookup_existing_ruleset` writes the first id under `main`, leaving
      any others alone. The open question was what should happen when the
      two disagree.

      Maintainer, 2026-09-06: **the fleet standard is a FLOOR** -- a
      repository must enforce at least it, and may enforce more. That
      settles it without a reconcile step, because aggregation only ever
      ADDS constraints: a second ruleset can make the branch stricter,
      never looser, and its bypass actors excuse nobody from the rules the
      managed one carries. So once `repo setup` has written the ruleset the
      repository owns, the extra cannot lower what the branch enforces --
      the repository is not half-done, and the extra is something to know
      about rather than something blocking. `repo setup`'s note and `repo
      audit`'s `[CHECK]` now say that, instead of sending the reader off
      to reconcile something that is not broken.

      Both notes report what was found and point at it, and claim
      nothing about what the extras DO. Seven review rounds went into
      arriving there, each finding one more claim the check could not
      support: it is a lookup by NAME, and it never reads enforcement,
      scope or rules. So it cannot say the branch is at or above the
      standard (a ruleset excluding the default branch protects nothing
      on it), nor that the extras apply at all (a disabled or
      evaluate-mode one does not), nor what `repo setup` will write
      instead on the inherited-only path (it may create one or ADOPT an
      owned legacy-named one). Each of those has reporting elsewhere that
      did the read: `_report_excluded_hardened`,
      `_find_merge_method_conflicts`, `audit_legacy_rulesets`, and `repo
      audit`'s own effective-rules checks. Neither claims the branch is therefore at or above the
      standard: that is a question about coverage, and a ruleset
      excluding the default branch protects nothing on it whatever its
      rules hold -- `_report_excluded_hardened` would contradict the
      claim in the same run. Coverage is reported where it can be
      grounded: that function in `repo setup`, and `repo audit`'s
      covering-main checks read from the effective-rules API.

      One way an extra can genuinely break the branch is scanned for:
      `_find_merge_method_conflicts` catches `allowed_merge_methods`
      intersecting down to nothing mergeable, across inherited rulesets
      too. It is the failure mode currently scanned, not the only one --
      it reads `pull_request.allowed_merge_methods` and nothing else, so
      an extra carrying an unmanaged rule (`lock_branch`, a required
      deployment or workflow that cannot complete) can leave the branch
      just as unmergeable and go unreported (Codex review,
      mikelward/repo#44).

      **Deleting an extra -- one under a name this tool never used -- is
      still NOT done**, and it is a different question from the legacy
      path below. A legacy name is this tool's own former name, so
      deleting one is finishing a rename it started; an extra is
      somebody's ruleset, and nothing here knows what it is for. The
      legacy delete no longer needs a strictness comparison at all (the
      body is recorded first, so it is reversible), but that does not
      transfer: recording something before deleting it makes an intended
      deletion undoable, not an unintended one right. If it is ever
      revisited, the safe slice is an extra whose *scope* provably
      excludes the default branch -- no strictness comparison needed.

- [ ] **A widening can name a ref the ruleset already covers.**
      `_widen_include` compares refs literally (`ref not in include`), so
      a ruleset including `refs/heads/main` on a repository whose default
      branch *is* `main` still gets `~DEFAULT_BRANCH` appended, and the
      inverse when the token is there and the literal ref is added. Two
      separable consequences (Codex review, mikelward/repo#45):

      The **plan overstates**: `scope_added` carries that token, and
      `_effective_scope_added` filters it against the ruleset's own
      exclusions but not against what the include list already reaches,
      so `newly effective on ~DEFAULT_BRANCH` can name a branch already
      protected. Display only, and the fix is contained -- normalize the
      ORIGINAL include list and drop from `covered` anything it already
      covers.

      The **write is redundant**: the appended token makes `target !=
      original`, so `needs_write` goes true and a PUT happens for a scope
      that is unchanged in effect. That predates the plan work and
      changes what the tool WRITES rather than what it prints, so it
      wants its own change and its own tests -- including what an
      already-`~DEFAULT_BRANCH` ruleset should do when the default branch
      is later renamed, where the literal entry is not redundant at all.

## repo cleanup

- [ ] **`repo audit` cannot see an unmergeable branch.**
      `allowed_merge_methods` INTERSECTS across rulesets, so two active
      ones covering `main` -- one allowing only rebase, another only
      squash -- leave nothing mergeable at all. `repo setup` detects
      exactly that in `_find_merge_method_conflicts`, across inherited
      rulesets too, but `repo audit` never inspects the field, so a
      fleet sweep exits 0 over a branch nothing can merge into. Surfaced
      against the duplicate-ruleset note, which is why that note says
      outright that this audit does not read the extras' rules, rather
      than reading as an all-clear (Codex review, mikelward/repo#44). Closing it means
      reading every ruleset body rather than just names and ids -- the
      audit's duplicate check is a name lookup today -- so it has a real
      per-repository read cost and wants its own change. `_find_merge_
      method_conflicts` is the logic; the work is calling it from a
      read-only path and deciding the severity ([GAP]: nothing can merge,
      and `repo setup` will not delete a ruleset to fix it).

- [ ] **Patch-equivalence is invisible to `repo cleanup`, so those branches
      are reported as unmerged forever.** A branch whose *content* landed
      under different commits and a different pull request -- reworked and
      relanded under a new name, or superseded by a hand-written
      equivalent -- has no merged pull request of its own and is not
      contained in the default branch, so it reports as unmerged and is
      only ever offered, never swept. `git cherry` finds exactly these (it
      compares patch ids), and no GitHub API does, so closing this needs a
      clone -- which every other subcommand here avoids, deliberately: the
      whole tool is `gh api` calls against a repository it never checks
      out. Measured on mikelward/simmo, the gap is real but small: of 184
      dead branches, the merged-pull-request check accounted for all but
      about a dozen. Not worth a clone yet; revisit if the offered list
      routinely fills with branches that have obviously landed.
- [ ] **A merged pull request stays `merged_at` even if the default branch
      is later force-pushed past the merge**, so `repo cleanup` would sweep
      a branch that is now the only ref to those commits, without ever
      running a comparison. Real, and unlike the prompt-window findings this
      state can exist before cleanup starts -- the plan itself would be
      wrong, not merely stale. Deferred (maintainer, 2026-09-03): confirming it
      needs one `compare` per merged branch, which is ~180 extra requests on
      a ~190-branch sweep, taking it from ~380 to ~560 against a shared
      5,000-an-hour limit -- and it would rule out something this fleet
      structurally prevents. `repo setup` writes a `non_fast_forward` rule on
      the default branch (so GitHub itself rejects the push), `repo audit`
      reports its absence as a `[FIX]`, and every repo's `AGENTS.md` says
      `main` is never force-pushed. Worth revisiting if `repo cleanup` is
      ever pointed at repositories outside this fleet, where neither the
      ruleset nor the convention holds.

- [x] **Revalidation was cut back to a SHA-and-protection re-read**
      (maintainer, 2026-09-03). Before the delete, `repo cleanup` re-reads
      each branch and refuses one that moved or became protected since the
      plan was built. It used to also re-read the repository's open pull
      requests and re-run the default-branch comparison, behind a
      bounded-staleness cache (`INDEX_MAX_AGE_SECONDS`) added to make the
      per-branch version affordable. That reached ~281 of ~1010 lines --
      about a quarter of the module -- and six consecutive rounds of review
      each found a defect in the machinery added by the round before.
      What it defended was a window of seconds, against changes only this
      operator could make, on a fleet where nobody else pushes to
      `claude/*` branches. Removed; the answer to a plan that has gone
      stale in some other way is to re-run the command, which reclassifies
      from scratch. **If you want any of it back, weigh it against that
      history first** -- the individual findings were each correct, and the
      trouble was that the design kept needing another layer to hold them.
- [ ] **Only the merged sweep is fleet-safe; the offers are one repository
      at a time.** `repo list | xargs -n1 repo cleanup --force` works
      because `--force` refuses `--include-unmerged`. There is no
      equivalent for the unmerged offers, and there should not be a
      `--force`-shaped one -- but a fleet-wide *report* (every repository's
      stale unmerged branches in one listing, nothing deleted) would be
      useful and is not yet possible without running `--dry-run` per
      repository and reading past the merged section each time.

## Decisions needing review

- **The convergence contract's edges, as autopilot drew them** (2026-09-10,
  from the maintainer's principle in chat -- progress every run, no operator
  intervention, never a permanent wedge, open branches may break temporarily
  if a rerun or a rebase onto main fixes them -- written into `SPEC.md`).
  Each of these is one function or one predicate, and each is reversible:
  - *"Passed" gates a requirement, not "reported".* A check that has run
    and failed is deferred like one that never ran, with a message that
    tells the two apart. The alternative -- require on any report, as
    before -- would put a failing `zizmor` in front of every merge on a
    repository the moment its scaffold lands. Reversible: `never_passed`
    is one function beside `never_reported`, and `apply_ruleset` calls
    one or the other.
  - *`--force` waives nothing.* It used to waive the never-reported guard,
    and the fleet loop passes it on every run, so the guard was off
    exactly where it mattered. Requiring a check before it has passed is
    now not possible from the command line at all. Reversible: the
    waiver was three lines in `apply_ruleset`.
  - *The run that merges the scaffold pull request still defers the checks
    it publishes.* The deferral is computed from the tree the plan read,
    before the merge, and what the pull request contained is not read; the
    next run reads the branch as it is and requires them. Costs one run
    on a repository that is converging; the alternative infers the
    branch's contents from "our pull request merged", which is the kind of
    reasoning ten findings on mikelward/repo#42 were about. Reversible:
    `defer` in `setup_cmd._run` could be emptied on a merged outcome.
  - *Merge only when every check run passed (skipped and neutral count as
    not failed), no status is pending or failed, not a draft, and GitHub
    reports it mergeable; `dirty`, a failed check, and a `blocked` whose
    required checks have all passed (a review requirement or an unresolved
    conversation, which no run can settle) are HELD for a person and exit
    1; a `blocked` with a required check still unreported waits.* The
    review-block split was Codex's finding on mikelward/repo#56: waiting
    there repeated forever with exit 0. Whether a required check is
    satisfied on the head is asked with the requirement's App binding and
    with `skipped`/`neutral` counting as GitHub counts them (two more
    rounds), so neither a same-named check from another App nor a
    skipped required check misreads a review block.
  - *"This tool's own" is prefix AND author; "safe to merge" is the head
    tree.* Codex (mikelward/repo#56) found the prefix alone let a
    collaborator's same-repository branch be merged once green. A pull
    request's author is durable, so one another user opened is passed
    over (and this tool opens its own beside it); what a head HOLDS is
    checked by content -- the generated commit is deterministic, so the
    head's tree must equal the branch's current tree plus the generated
    changes, by blob sha -- and anything else is stale: closed, and a
    fresh one opened. That subsumes "behind" (a moved base fails the
    same comparison), so the update-branch path went. The head must also
    be exactly ONE commit whose parent is the current tip: a push and its
    revert leave the right tree, and a rebase merge would replay both
    onto the branch (Codex, round 3). Closing was chosen
    over holding because a stale one of the tool's own is regenerable and
    closing is reversible; a signature check was NOT used because commits
    made through the git-data API are not GitHub-signed, so committer
    identity there is whatever email the creator typed. Reversible: the
    author test is one line in `find_open_gap_pull_request`; the tree
    comparison is `_head_is_the_generated_commit`.
  - *An update never removes a required check.* A check the ruleset
    requires that the run does not name used to be dropped, with a "would
    NO LONGER require" plan line as the safeguard; Codex's security review
    on mikelward/repo#56 found that with deferral the write could drop a
    working App-bound gate while every standard check was still waiting,
    leaving the branch with none. Rather than "keep it only while a
    replacement is deferred, then swap", the standard is treated as the
    floor it was declared to be (maintainer, 2026-09-06): extras stay,
    named in the plan as "keeps requiring, beyond the standard", and
    `--rule` can only add. Removing a check -- the rename sequence lanes'
    README describes, say -- is done in the ruleset by hand. Reversible:
    one line in `_build_update_body`.
  - *A scaffold pull request is merged by API with the head sha as the
    merge's precondition*, and only when its fresh verdict and head match
    what the plan showed -- a change during the confirmation wait is
    reported and left for a later run, never acted on unconfirmed.
  - *A `blocked` pull request with every required check passed asks
    GitHub live what holds it* (`reviewDecision` and unresolved threads,
    one GraphQL read) rather than inferring a review block from the rules
    present -- four Codex rounds each found one more rule type the
    inference misread. What remains is ordered: a signature rule is held
    (the API commit is unsigned), and anything else is held as
    unidentified rather than waited on forever, naming any rule on the
    branch that settles on its own (deployments, required workflows,
    code scanning) as a hint. That hint was a wait until the fifth round
    showed a settled deployment rule masking a block only a person can
    clear; so a deployment still pending now costs one red run that
    clears itself, rather than reading live deployment and code-scanning
    state -- more endpoints for a corner nothing in this fleet has.
    Reversible: the order and the two frozensets in
    `assess_gap_pull_request`.
  - *The update set is codex-review's three workflow files and nothing
    else* (`scaffold.UPDATED_PATHS`). `zizmor.yml` is hand-pinned to a
    zizmor release per consumer and a bump is a decision to re-read the
    findings, so it stays add-only even though most copies are identical.
    Reversible: one frozenset.
  - *A ruleset that does not yet reach the default branch and already
    requires a deferred check is refused, not deferred.* Widening it onto
    the branch would make that check required where nothing can publish
    it; dropping the check would loosen whatever it protected. The run
    says what has to land first. Rare (a `main` ruleset scoped to
    `release`), and the one whole-write refusal the deferral design keeps
    beside the empty-branch one. Three Codex rounds sharpened it into
    one rule: a glob include is unevaluated and so counts as NOT covering
    the branch (fail closed), and a ruleset that does not reach the
    branch is widened onto it only when it carries nothing that could
    block a pull request there (`_widening_hazards`: a required check, an
    approval requirement of any kind, a rule type this tool does not
    write). Evidence was tried in between -- widen once every kept check
    has passed here -- and the third round showed that a check's history
    on the repository is not a history on this branch (a success on a
    release pull request says nothing about main), so the reading was
    dropped rather than narrowed again; the fourth round added approval
    requirements, which the tool's own pull requests can never meet.
    The refusal is a HOLD of the ruleset step alone (`RulesetHeld`,
    `report["held"]`), not a preview failure: every other step still
    runs, since a ruleset a person has to widen is no reason to leave
    the scaffold unopened (fifth round). Reversible:
    `_covers_default_branch`, `_widening_hazards` and the guard in
    `_build_update_body`.
  - *A real branch named `main` or `master` beside the default holds the
    ruleset step* (maintainer, 2026-09-11, chosen in chat over
    hazard-checking the literal refs one write at a time: "for my repos
    it would be a bug to have a master branch"). Codex's sixth round
    read the literals the write adds as a newly targeted branch the
    hazard check skips; a hazard check there answers one write, since
    the run after adds the checks to the sibling as they pass on the
    repository, and the tool cannot know what the sibling is for. So the
    whole step holds while one exists -- every other step still runs,
    a person deletes or renames the branch or widens by hand -- and the
    lock loses nothing meanwhile, since nobody can rename into a name
    that is taken. A fork whose default IS `master` has no sibling and
    is not held. Read as a Git ref (`git/ref/heads/<name>`), never
    through `branches/<name>`, which follows a rename's 301 and reported
    a renamed `master` as present. Reversible: `sibling_branch` and
    `_hold_for_sibling_branch` in `rules.py`.

- **Binding the `lanes` check to the App: how the verify side recognizes it, and
  what gates the bind** (maintainer-directed, 2026-09-09 -- "make repo setup do
  these steps", approach A chosen in chat). `repo setup` now requires `lanes`
  from the LANES App (an `integration_id` on the ruleset entry), so a same-repo
  pull request cannot forge a `lanes` status via the ambient token. Three
  choices, each reversible:
  - *Verify via installations, not a constant.* The Statuses REST API hides the
    App that created a status (it surfaces the `{slug}[bot]` user, not the id),
    so a bound status check cannot be recognized from `app.id` the way a check
    run can. `_collect_reported` resolves the bound id -> slug through the
    owner's App installations (`apps.app_slug_for_id`) and matches the creator
    login. The alternative was a hard-coded `LANES_APP_SLUG` constant (no
    network call, but the App's identity baked into the tool); approach A keeps
    the App's identity out of the source, consistent with the id only ever
    arriving from the credential. Reversible: `app_slug_for_id` is one function
    and the lazy block in `_collect_reported` is where a constant lookup would
    go instead. Cost: one `user/installations` read (and one per-status read)
    per bound entry still unsatisfied after the check-run scan -- lazy, so the
    ordinary path pays nothing; well inside the API rate limit.
  - *Gate the bind on a default-branch status publisher, id from the credential
    value.* setup binds only once `credentials.lanes_status_publishers` sees the
    default branch publishing the status as the App, and only when handed
    `LANES_APP_ID` (its value is the id). So the cutover self-sequences: an
    ambient consumer stays unbound (not wedged), a run after the migrated
    workflow lands binds. The alternative -- bind as soon as the pair is placed
    -- would wedge every merge until the App became the producer. Reversible:
    the decision is the one block before `_plan_credentials`'s `return plan`,
    and `_bind_checks` in setup_cmd.
  - *Write the binding AFTER the credential move settles the pair* (maintainer
    chose option A, 2026-09-09, over reordering the whole apply). The ordinary
    ruleset step requires `lanes` unbound (preserving any existing binding); a
    SECOND `apply_ruleset` adds the binding, run only once this run's credential
    move has succeeded. Codex found (mikelward/repo#52, twice in the same
    mechanism) that binding in the ruleset step -- which runs before the moves
    -- leaves `lanes` bound to an unusable credential if a move then fails. With
    the write deferred, and apply_ruleset's never-reported guard holding it
    until the App has actually posted, this is the intended two-run migration:
    run 1 places the pair, a later run binds. A binding held because the App has
    not published yet is not a failure -- exit 0, the check stays unbound, rerun
    later. Reversible: the binding preview/apply are two self-contained blocks in
    setup_cmd's `_run`; removing them falls back to requiring `lanes` unbound.
    The alternative (B, reorder the apply so moves precede the ruleset write)
    was one ruleset write but touched the fingerprint contract.
  - *Re-point converges, it does not refuse* (maintainer, 2026-09-09: "run
    `repo setup` and it eventually lands correct, safely, with as few manual
    steps as necessary"). Codex round 3 (mikelward/repo#52) found that
    re-pointing to a *different* App B before B publishes overwrites the
    credential to B while the ruleset still requires A, blocking merges.
    Refusing it (making re-point a manual migration) was rejected as against
    the standing goal; instead the run proceeds -- it converges by being rerun
    once B publishes (the same two-run shape) -- and warns loudly that merges
    are BLOCKED in the window, since that is not benign like a first bind's
    deferral. The block is inherent (B cannot publish until its key is placed,
    and placing it is what opens the window), so the honest answer is a clear
    warning, not a refusal or a deadlocking hold. Reversible: `repoint_from`
    detection is one read and one plan line. The same round's P2 (the deferred
    write omitted `expected_fingerprint`, a race across the credential-move
    window) is fixed by capturing the binding's fingerprint right after the
    main apply and pinning the deferred write to it.
    Round 4 (mikelward/repo#52) added the one exception to "converges, does not
    refuse": a re-point to an App that does NOT cover the repo can never converge
    (B cannot publish until it is installed, and a bare rerun cannot install it),
    so that case HOLDS the credential move rather than switching -- App A keeps
    publishing and stays bound, no wedge -- and says to add the App
    (`repo setup --app <slug>`) then rerun. The convergence promise stands; the
    hold is what keeps a switch that cannot converge from wedging every merge.
    Reversible: `repoint_to_uncovered` is one coverage read and one guard at the
    top of the credential-move loop (`repoint_to_uncovered` in setup_cmd's `_run`).
    Round 5 (mikelward/repo#52) extended the hold to `repoint_unknown` (the
    effective-rules read failed, so the bind MIGHT be a re-point; holding an
    uncovered App there costs only a rerun, never a wedge).
  - *Declined: multi-ruleset conflicts.* Codex round 4 (mikelward/repo#52) also
    flagged that a `lanes` binding in a ruleset `repo setup` does not manage
    (org-level or inherited) would survive our rebind and keep the old App
    required (P1), and that `repo audit` recommends `repo setup` for such an
    entry it cannot edit (P2). Declined: this account has no organization and
    cannot create org- or user-level rulesets (maintainer, 2026-09-10), so every
    `lanes` binding lives in the per-repo ruleset setup manages -- no foreign
    ruleset survives the rebind or misdirects audit. GitHub's effective-rules
    endpoint also does not attribute an entry to a ruleset, so a reliable
    foreign-binding detector is not possible without heuristics; and audit's
    wrong-App guidance already offers "or repoint the ruleset entry by hand" for
    exactly the case setup cannot fix. Revisit if org-level rulesets ever enter
    the fleet. Reversible: nothing was built.
  - *Declined: absent-vs-selected install in the coverage message* (Codex round 5
    P2, mikelward/repo#52). Audit's/setup's coverage-gap line says "add the App
    to <repo> (`repo setup --app <slug>`)", which cannot repair a wholly-absent
    owner installation (`--app` adds a repo to an existing installation; it does
    not install the App). Declined: the LANES App is installed on all of the
    owner's repos in one setting (maintainer, 2026-09-10), so the line does not
    fire here, and the reachable case would be "installed, this repo excluded
    from a `selected` install", for which `--app` is exactly right. Distinguishing
    absent from selected needs an extra "installed on the owner at all" signal
    threaded through `app_covers_repo`. Revisit if the fleet ever uses a selected
    or per-repo install.
  - *Maintainer's call: bind while a lanes hardening gap is open?* (Codex round 5
    P2, mikelward/repo#52). `lanes_credential_failed` holds the binding whenever
    ANY lanes-labeled failure or `unfixed` is open, not only an actual pair-move
    failure. Codex noted a case where that is over-broad: a job publishing
    `lanes` as the App (pair settled) beside a separate ambient `gate` step
    records a `lanes` `unfixed` ("hand that step the pair too"), which then holds
    a binding whose pair is in place. Narrowing it to bind-despite-a-
    hardening-gap was TRIED and reverted: keying off the absence of a failure
    tag bound `lanes` when a HELD move (a protected-env policy that drops its
    writes) left the pair unplaced -- a wedge (Codex round 5 P1). A correct
    narrow rule needs a positive signal that THIS App's pair is in the
    environment, which GitHub's APIs cannot give (secret values are never
    returned; the Statuses API hides the creating App). So the conservative
    hold stands -- bind only once the lanes state is fully clean -- and whether
    to bind while a hardening gap remains is the maintainer's call, not one to
    settle by inference. Reversible either way: it is the one
    `lanes_credential_failed` predicate in setup_cmd's `_run`.
  - *Descoped (follow-up): Re-point / rotate the lanes App binding* (maintainer
    chose "descope then follow up", 2026-09-10; Codex rounds 4-6 P1s F/H,
    mikelward/repo#52). The credential switch (the `lanes` environment move) and
    the check binding (the ruleset `integration_id`) are two separate writes,
    and setup cannot verify which App's pair the environment holds (GitHub never
    returns a secret's value; the Statuses API hides the creating App). So
    automating a RE-POINT -- moving an existing binding from App A to App B --
    kept opening windows where "publisher = B, requirement = A" until a rerun:
    a failed post-apply fingerprint capture; an over-optimistic `--app` coverage
    preview; a suspended install read as covering. Codex advised against patching
    each as an isolated exception, and one such patch regressed a held-move
    case. So re-point is descoped OUT of this PR: when `lanes` is already bound
    to a DIFFERENT App -- or its current binding can't be read -- setup REFUSES,
    switching neither the credential nor the binding, leaving the working App in
    place (`refuse_repoint` in setup_cmd's `_run`). First bind (currently
    unbound) and an idempotent rerun (already bound to the same App) are
    unaffected. **Follow-up:** re-add re-point/rotation with a design where the
    credential switch and the binding commit together, so the publisher never
    leads the requirement (bind only after positively confirming this run wrote
    the pair, or gate the switch on a captured binding transaction). Reversible:
    the follow-up replaces `refuse_repoint` and re-adds the switch path.
  - *Accepted: rerun-converging windows in the split credential/binding
    mechanism* (maintainer, 2026-09-10; mikelward/repo#52). One narrow window
    remains on the FIRST-bind path and is documented rather than fixed: a run
    supplying only `LANES_APP_ID` while the env already holds the old key
    overwrites just the id and still records the binding (setup_cmd
    `_plan_credentials`, at `plan.lanes_binding`) -- a mismatched pair the App
    can't authenticate with until a rerun supplies both halves. setup can't
    detect it (no secret values), it takes an operator supplying half a
    credential, and it converges on a rerun with the full pair (the standing
    "fine to require multiple runs" rule). The class-deleting fix rides with the
    re-point follow-up above. A second window of the same class (Codex round,
    2026-09-10, L): the deferred binding write is pinned to a fingerprint of the
    computed TARGET (App B) body, which `_build_update_body` produces identically
    whether `lanes` is currently unbound or bound to some App A -- so an admin
    binding `lanes` to A after the pre-move reread but before the deferred write
    is not caught, and a first-bind run overwrites A with B (ending consistent:
    publisher B, requirement B) or, if that write then fails, lands in the
    already-accepted failed-capture window. It needs a concurrent admin bind
    during the run and converges on a rerun. The robust fix -- capture the
    observed SOURCE binding and make it part of the deferred transaction, so the
    write aborts if the source drifted -- is the same atomic switch+bind
    redesign as the re-point follow-up above, not a separate patch.
  - *Deferred:* `repo audit` does not yet actively flag "the default branch
    publishes `lanes` as the App but the ruleset requires it unbound" as a
    `[FIX]` -- it verifies a binding that exists, but does not nag to add one.
    Cheap to add later (the audit already reads both the ruleset bindings and
    the workflow publishers); left out of this change to keep it focused.

- **Whether a half-failed restriction should restore the open policy at all**
  (flagged for the maintainer, 2026-09-05). `restrict_environment` is a PUT
  (switch to custom mode) then a POST (name the default branch). When the POST
  fails, the environment is left in custom mode naming NO branch, which admits
  nothing -- so the code restores the open policy, and that restore is a write
  whose inputs can go stale. Three findings have now landed on exactly that:
  the settings snapshot reverting somebody's protection change, the policy list
  going stale across a round trip, and the mode changing underneath. Each is
  fixed, and the remaining window is one API call wide, which is the floor for
  any confirm-then-write.
  Codex's suggestion is to stop restoring: leave the environment closed and
  report. That deletes the class outright -- no write, no staleness, nothing to
  clobber -- and `restrictable` already treats custom-mode-naming-nothing as a
  half-done restriction the next `repo setup` run completes, which several
  error paths here already rely on. What it costs is the case where the
  environment already HELD the pair: closed means the publisher cannot read it,
  so the required `lanes` status stops until someone re-runs. Restoring open
  keeps the publisher working and leaves the credential exactly as exposed as
  the run found it.
  So it is a real trade-off between an outage and an exposure that predates the
  run, not an obvious win either way, and it is a behavior decision about what
  state a repository is left in -- the maintainer's call. Reversible: the
  restore is one `try` block in `restrict_environment`, and deleting it leaves
  the surrounding refusals intact.

- **A paired credential write is three separate guards, not one unit**
  (autopilot, 2026-09-05; flagged for the maintainer). Four findings on
  mikelward/repo#36 landed in one mechanism -- writing two secrets that have to
  move together, over an API with no atomic two-key write. The rollback
  inventory could fail and silently disable the undo; a rotation's overwrites
  left a mismatched pair with nothing to undo; a reopened environment kept what
  the run had just written; and the first failed write did not stop the second,
  breaking a pair that had been working. Each is fixed where it was found:
  `unreadable` refuses before any write, `created`/`overwrote` split what can be
  undone from what cannot, the reopened branch undoes what it created, and the
  write loop now breaks on the first failure.
  What that does not do is make the pair one object. The invariant -- both
  halves move or neither does -- is currently enforced by four guards in
  sequence in `_apply_credentials`, and the fourth was missing for four rounds
  without any of the other three noticing. A `write_pair` that owned the whole
  sequence (inventory, write, undo what it created, name what it overwrote,
  confirm the policy after) would make the invariant one thing to get right
  rather than four, and would make a fifth window a compile-time-shaped question
  instead of a review finding. That is a design change on the apply path, so it
  is the maintainer's call, not autopilot's: the behavior is correct as it
  stands, and the cost of the refactor is a rewrite of the most-reviewed code on
  this branch. Reversible either way -- the guards are all in one function.

- **A `uses:` the reader cannot follow holds the unused DELETE, not the
  move** (autopilot, 2026-09-05). Three findings on mikelward/repo#36 were
  the same shape -- a local composite action, an anchored `with:` shared
  between two steps, and now a job-level call to an external reusable
  workflow -- each a file this reader does not read and might hold the lanes
  step that publishes. What distinguishes the third is that a called
  workflow can reach a secret WITHOUT the caller naming it, through `secrets:
  inherit` or the called job's own `environment: lanes`; a step-level action
  gets only what its `with:`/`env:` hands it, which is a mention this reader
  already sees. So the blind spots enumerate: files handed the secret (a
  mention catches those) and files that can take it unhanded (only a called
  reusable workflow). `lanes_called_workflows` covers the second.
  It holds the unused delete alone. A move made on a wrong reading can be
  undone from the value the operator handed in; a delete cannot, since
  GitHub never returns a secret. Holding the move too -- the safer-sounding
  reading -- would keep the credential out of every repository in the fleet,
  since they all call something. The alternative considered and not taken was
  reading the called workflow, which recurses without a floor and makes every
  level another chance to conclude "unused" in the deleting direction.
  Reversible: `lanes_called_workflows` is one function and the planner
  consults it in one place; widening it to hold the move is two lines, and
  narrowing it to callers that pass secrets is one condition.
- **The lanes App pair's home is the `lanes` environment, by name**
  (autopilot, 2026-09-05). mikelward/lanes's README says the environment may
  be named anything, but this fleet names each credential's environment
  after what reads it, and every consumer so far declares `lanes`, so the
  audit and the move hold a publishing job to exactly that name. The
  alternative -- reading each job's own `environment:` and checking that
  one -- would let two publishing jobs disagree about where the pair lives
  and make "where does the credential belong" a per-job question `repo
  setup --credential` cannot answer. Reversible: `LANES_ENV` in
  `repo_lib/credentials.py` is one string, and `_declares_environment` is
  the one comparison.
- **`repo setup` restricts a `lanes` environment only when it is open to
  every branch** (autopilot, 2026-09-05); a policy set to anything else --
  another branch, a tag pattern beside the default branch -- is reported as
  a `[FIX]` to close by hand. The alternative, rewriting the policy to the
  default branch alone, would delete deployment-branch policies someone
  chose on purpose, and the PUT that carries the change resets every
  protection setting it omits (wait timer, reviewers, self-review), which is
  the trap `secrets_cmd._ensure_environment` already refuses. The re-sent
  settings cover what the environment GET reports; a setting the API adds
  later would still be reset. "Protected branches only" is reported as a
  policy to fix by hand rather than accepted: GitHub reads it as every branch
  while the repository has no branch-protection rule, and this fleet protects
  `main` with a ruleset, which is not one (Codex, mikelward/repo#36).
  Reversible: `restrict_environment` is the one writer, and the plan's
  `else` branch is where a rewrite would go.
- **A branch copy of a lanes publisher does not hold the credential move
  back; a branch copy of a batch caller still does** (autopilot,
  2026-09-05). Codex found, on mikelward/repo#36, that a publishing job on a
  non-default branch without `environment: lanes` vetoed moving the App pair
  off repository level -- leaving it exposed to exactly that branch's
  push-triggered run, for a copy the restricted environment shuts out
  whether or not it declares the environment. Fixed for lanes: only the
  default branch's publishers veto, and a branch copy is a `[CHECK]` line.
  The batch callers (`settle`) have the same shape -- a branch copy naming
  its secrets vetoes the move, per mikelward/repo#13 -- and were left as
  they are: the same argument applies (a dispatch of a branch copy cannot
  reach the restricted environment either), but changing a behavior a
  reviewed PR chose on purpose is the maintainer's call, not a fix to fold
  into this one. Reversible either way: `on_default` in the lanes block is
  the one filter, and `settle`'s `failing` is where the batch path would
  take the same one.
- **A lanes publisher that exists only on a branch keeps the App pair,
  and both commands say no trusted publisher reaches it yet** (autopilot,
  2026-09-05). Codex asked (mikelward/repo#36) for that case to read as
  unused, or to be reported; reported was chosen. The branch-only publisher
  is ordinarily the pull request adopting lanes, and the pair it will need
  once merged is what `repo setup --credential` places ahead of the merge --
  deleting it as unused would undo the step the adoption just took, and a
  branch-only batch caller keeps its credential for the same reason
  (mikelward/repo#13). What it costs is a pair sitting in an environment
  nothing reaches; the environment is restricted, so a stale branch cannot
  reach it either. Reversible: the `if not on_default(publishers)` branch
  in the lanes block is where the unused path would be taken instead.
- **A plan-time read is confirmed after the reading it belongs to, not
  before it** (autopilot, 2026-09-05). The lanes work has now taken five
  corrections of one shape: a fact the plan rested on was read once, and
  something moved before the run acted on it. Four were closed by
  re-reading at apply time (the whole-state comparison, the branch policy
  on a queued move, the environment's own contents, the default branch);
  this one is closed at the read instead -- `workflow_snapshot` confirms
  the default branch AFTER the workflows it pinned to that name, so the
  two are one reading, because on a plan with nothing to do there is no
  apply-time recheck to reach. That is the same design question already
  open below: one snapshot of every plan-time read, re-derived and
  compared whole, would delete the class rather than the instance, and is
  the maintainer's call. What the current shape costs is a read per fact
  and a rule -- confirm the name last -- that each new fact has to be
  remembered by. Reversible: `credentials.workflow_snapshot` is the one
  function, and both commands call it in one line each.
- **The unread backstop counts references, not the name in prose --
  and the counting model has now moved three times** (autopilot,
  2026-09-05). `unread_mentions` and `lanes_unread` exist to refuse a
  delete where the reader cannot tell whether a credential is used: they
  compare how often a file names the workflow against how many uses of it
  the walk resolved. What "names" means started as a raw substring, became
  a substring in a parsed string (a comment calls nothing), then gained a
  name boundary (`mikelward/lanes-helper` is not lanes), and is now the
  whole scalar -- so a workflow titled after the action is a title, while
  `mikelward/lanes@main` sitting somewhere the walk cannot reach is still
  a reference and still holds the delete back. Each of those was a real
  finding, but three corrections to one predicate is evidence about the
  model rather than three bugs: the reader is inferring "could this be a
  `uses:`?" from a string, where the structural walk already knows which
  positions are executable. Deriving the count from the walk's own
  refusals -- every `uses:` it saw, plus every position it could not read
  at all -- would delete the class instead of the instance, and is the
  maintainer's call per this file's rule that a design change is not
  autopilot's to make. What the current shape costs is a scalar that is
  a reference in neither direction: a `uses:` split across a YAML
  concatenation would read as prose, and a bare quoted `mikelward/lanes`
  in a `with:` value reads as a reference. Reversible: `_is_reference`
  and `_reference_count` in `repo_lib/credentials.py` are the two
  functions, and `_lanes_jobs` already fills a `resolved` set the other
  side of the comparison could come from.

  **Codex has now asked twice for the other half of this** (mikelward/repo#36,
  rounds 37 and 40): count only structurally valid `uses` positions. Declined
  both times, because taken alone it inverts the error into the destructive
  direction -- a `uses:` in a document the walk cannot descend (a `jobs:` that
  is not a mapping, a `steps:` that is not a list) would count as neither
  referenced nor resolved, so the credential reads as unused and setup
  DELETES it, in exactly the case the reader admits it cannot see. Whereas
  an over-count only refuses a move, loudly, with the copies left where they
  are. The model that satisfies both is one sentence long and is the design
  change above, stated concretely: count the `uses:` values the walk reached,
  PLUS whole-scalar references in any part of the document it could not
  descend, falling back to the raw text for a rejected one. That needs
  `_lanes_jobs` to record where it refused as well as what it resolved --
  perhaps fifteen lines. It is a design change, so per this file's own rule
  it is the maintainer's to take, not autopilot's; the instance Codex names
  (a scalar that is exactly `owner/repo@ref` but is not a `uses:`) stays
  unfixed until then, and costs a repository that its credential move is
  refused rather than mis-applied.
- **A lanes step or job carrying an `if:` is read as one that runs**
  (autopilot, 2026-09-05). Codex asked (mikelward/repo#36) for statically
  disabled steps -- `if: false` on the step or its job -- to be excluded
  from the publishers, in the shared workflow-state model rather than as
  another predicate. Declined for now, for the reason the `mode` expression
  took: only the literal is decidable, and `${{ false }}`, `'false'`,
  `github.ref == ...` and this fleet's own `needs.classify.outputs.lane ==
  'code'` all need an Actions expression evaluator, which is the
  one-more-case failure the hand-written YAML reader died of. The direction
  also matters: reading a disabled step as running keeps the credential,
  while reading it as not running makes the pair unused and lets setup
  delete it -- so the naive fix turns a missed advisory line into a
  destructive act on a step somebody disabled for an afternoon. What it
  costs is that the classify-only `[FIX]` is not raised where the only
  status publisher cannot run; the missing `lanes` status announces itself
  on the next pull request, since the gate never reports. Splitting the two
  questions -- disabled steps still hold the credential but do not publish
  a status -- is coherent and is the maintainer's call, per this file's own
  rule that a design change is not autopilot's to make. Reversible: the
  step and job mappings are already in hand in `_lanes_jobs`, so an `if:`
  test has one place to go.

- **The scaffolded `AGENTS.md` is `mikelward/conf`'s own
  `agents/AGENTS.md`, under a generated header** (autopilot, 2026-09-04).
  Four calls, none of them settled beforehand:
  - **Where the template lives.** `mikelward/conf@main:agents/AGENTS.md`,
    the fleet's shared conventions, rather than a new
    `conf/templates/AGENTS.md` the maintainer had named as the candidate:
    that would be a second copy of a file that already exists, kept in
    step by hand, which is the arrangement this repository exists to
    replace. Tracked at `main` like `zizmor.yml`, not pinned to a resolved
    sha like `TEMPLATE_FILES` -- those are pinned because they are a set
    that has to come from one revision, and this is a single file. The
    cost is a sixth external read on every `repo setup`, and a scaffold
    that fails when `conf` cannot be read (fail-closed, like every other
    template source).
  - **What it holds.** The shared rules, not a near-empty stub. A stub
    deferring to the user-level conventions assumes those load, and a
    remote session does not necessarily load them -- this session's own
    context carried each repository's `CLAUDE.md` and no user-level file
    at all. The generated header on top carries the two things no template
    can know (what the project is, what a contributor runs) as explicit
    TODOs. The cost: the fetched text is written in the first person to
    the maintainer ("Talking to me"), which reads oddly in a repository
    file and would be worth rewording if this stays.
  - **How `CLAUDE.md` gets made.** A one-line `@AGENTS.md` import, which
    is what `mikelward/conf`'s own `CLAUDE.md` does, rather than the
    `120000` symlink blob most of the fleet uses. The Git Data API can
    write one, but `push_initial_commit`'s bootstrap goes through the
    Contents API, which cannot -- so a symlink would work for a gap-fill
    and fail for a brand-new repository, which is the case this is most
    for.
  - **A symlink at `CLAUDE.md` counts as present.** `plan_gaps` treats any
    non-regular file at a scaffold path as occupied and fails the whole
    step, so without this every fleet repository whose `CLAUDE.md` is a
    symlink would have its bootstrap fail over a file that is already
    exactly right. Narrow on purpose: one named path
    (`SYMLINK_IS_PRESENT_PATHS`), because a symlink at `ci.yml` could point
    anywhere and refusing is right there. The item this closes said
    "`plan_gaps` already refuses to overwrite one" as though that settled
    it -- refusing means erroring the whole repository, not skipping.

  All four are reversible: the source is two constants, the header one
  string, and the symlink exemption one frozenset.

- **A ruleset exclusion that keeps a hardened ref out is reported, not
  failed and not deleted** (autopilot, 2026-09-04, answering Codex's P1 on
  mikelward/repo#31). An exclusion outranks an include, so a ruleset
  including `~ALL` -- or all three refs literally -- while excluding
  `refs/heads/master` leaves master exactly as unprotected as before, and
  `repo setup` used to report "nothing to do" over it. It now names the
  ref on every path. What it does not do is either of the two stronger
  answers. **Deleting the exclusion** is out because this module never
  edits one: that is a narrowing decision about a rule somebody wrote, and
  the whole design here is that an update only ever widens. **Failing the
  step** was the closer call, since the fleet-credentials precedent below
  says a clean exit should mean the repository is in shape -- but the
  ruleset step's non-zero return travels through `setup_cmd`'s
  `ruleset_preview_failed`, which aborts the ENTIRE run before any other
  step applies, so one exclusion would refuse the whole repository. The
  gap is already counted where a gap belongs: `audit_cmd._targeting_status`
  reports it as a `[GAP]` and fails `repo audit`. Reversible in either
  direction -- the reporting is one function, and failing it would need
  `setup_cmd` to tell "this step found something it cannot fix" apart from
  "this step could not run" first.

- **The scaffold does not write a `TODO.md`, and autopilot left it that
  way** (2026-09-04; the maintainer's own note was "i'm not sure if we
  need that"). The file it used to write carried one item -- replace
  `ci.yml`'s placeholder job with the project's real jobs -- which
  `ci.yml` already says in a comment directly above that placeholder, so
  the scaffold was writing a second copy of a single instruction into a
  second file, where the two could drift and where a project that keeps
  its own `TODO.md` would find the path taken. The alternative was
  bringing it back with something to say that `ci.yml` cannot say in
  place, such as a fleet-wide checklist for a new repository; nobody has
  written one. Reversible: `build_scaffold_files` gains an eighth entry,
  and `plan_gaps` already refuses to overwrite a path that exists.

- **`repo setup` fails (exit 1) on a fleet credential it cannot move,
  rather than reporting it and exiting 0.** "Fixes everything" was the
  ask, so a clean exit has to mean the repository is in shape: a
  `NOT FIXED` line (no value given, or a caller still naming its secrets)
  is counted in the failure summary, and a dry run exits 1 on one too.
  The alternative -- advisory only, exit 0 -- would let a fleet-wide
  `xargs` run finish green with credentials left where they were.
  Reversible by dropping the two `failed.append` calls for `unfixed` in
  `setup_cmd.run` (the plan lines stay).
- **A failed read in the fleet-credentials step fails that step alone.**
  Like an App-plan error, not like a ruleset or `--secret` preview
  failure: the other steps still apply. A read this step cannot make
  (a token without Contents access hides the workflows directory) says
  nothing about whether the ruleset needs writing, and holding the
  ruleset write hostage to it would make `repo setup` refuse whole
  repositories over a listing. Reversible by adding
  `credentials_plan.failed` to the preview gate.

- **An invalid `--secret` NAME (or the value file being empty) is now a
  usage error caught up front, before any gh call runs — not discovered
  lazily via a failed dry-run partway through, the way the shell porting
  source's `repo-secrets --dry-run` subprocess call discovers it.** The
  shell version has no choice: repo-rules and repo-secrets are separate
  processes, so a bad secret name is invisible to repo-setup until it
  shells out and the child's own validation fails. Here, `repo setup`
  calls `secrets_cmd.validate_name`/`validate_env` directly as functions —
  the exact same rule repo-secrets itself enforces, not a looser
  re-derivation of it — so there's no reason to defer the check to a
  later, gh-touching step just to mirror the shell's process boundary.
  Same reasoning for an empty secret value: reading the file's bytes
  (needed anyway, as the up-front readability check doubles as the
  snapshot — see below) makes emptiness free to catch at the same point.
  Net effect: `repo setup --secret BAD-NAME=path OWNER/REPO` now exits 2
  with zero gh calls, where the shell version would run the ruleset
  step's own harmless preview first, then exit 1 after repo-secrets'
  child process reported the problem. Reversible by moving the name/env/
  emptiness checks out of the up-front validation pass and back into the
  per-secret preview loop, if a stricter "match the shell's own staging,
  not just its outcome" reading of the porting source is wanted instead.
- **The shell's `reject_newline` (a literal newline inside one --rule/
  --secret/--app value being read as two entries) was deliberately NOT
  ported, and that part of the original reasoning holds.** It exists to
  work around a hazard specific to shell's subprocess-composition model
  (word-splitting on a newline-delimited pseudo-array) that doesn't exist
  here — see `setup_cmd.py`'s own module docstring. Per AGENTS.md's
  "don't port shell idioms that exist only because shell has no better
  option." Not reversible in the sense of "add it back" being meaningful;
  revisit only if a future refactor reintroduces an actual subprocess
  boundary between `repo setup` and the ruleset/secrets logic.

- **The OTHER half of that original decision — that the ruleset step's
  byte-for-byte "did the dry-run text change since it was shown"
  staleness recheck was ALSO safe to drop, on the reasoning that
  `apply_ruleset()`'s own re-read-and-revalidate-before-writing already
  covered the same ground — was wrong, and stayed wrong through several
  rounds of Codex review before the real shape of the gap became clear.**
  A same-call-only recheck cannot catch a ruleset having been deleted and
  a DIFFERENT one created under the same name in the window between an
  earlier preview call and a later real apply's own start; it cannot
  catch the SAME id needing a write at real-apply time that its own
  preview never showed (no identity change to catch); and it cannot catch
  the SAME id, still needing a write in the yes/no sense either way,
  having its actual MANAGED CONTENT edited by something else in that same
  window (a required check re-pointed at a different integration, say) —
  three narrower fixes, each closing one of those specifically, were
  bolted on across separate rounds before the pattern was named for what
  it was: `apply_ruleset()`'s own revalidation never actually compared
  against anything outside its own single call.

  **What replaced it is not a revival of the shell's byte-for-byte TEXT
  comparison** — rendered plan text is a blunter signal than what
  actually gets written, and a `--rule` value that happened to collide
  with rendered output was a separate, already-fixed bug in its own right
  — **but a structural equivalent that subsumes all three of the
  narrower fixes above.** `apply_ruleset()` now computes a "fingerprint"
  — `(existing_id, needs_write, target_body)`, everything about what it
  has decided to write, as one comparable value — once per pass, exposes
  the preview's via `report["fingerprint"]`, and takes an earlier call's
  back as an optional `expected_fingerprint`, refusing if a freshly
  recomputed fingerprint (immediately before the real write) doesn't
  match. One check, run once, replaces the id-only comparison, the
  needs-write-only comparison, and the two hand-written "still resolves
  by name" rechecks (one per create/update branch) that had accumulated
  in their place. See `apply_ruleset()`'s own docstring for the full
  reasoning, including why `needs_write` has to be part of the
  fingerprint rather than left implied by `target_body` alone (a check
  removed and then re-added identically can leave target_body
  byte-for-byte equal to an earlier no-op's, even though a write happens
  this time where none did before), and why ownership and merge-method
  scope stay their own, separately-worded rechecks rather than folding
  into this one — both can fail for reasons a generic fingerprint
  mismatch would explain badly, and scope validation isn't even about
  this ruleset's own content in the first place.

  Not reversible in the sense of "go back to no recheck at all" being
  meaningful — the gap it closes is real and was independently
  rediscovered from three different angles. Revisit the SHAPE of the
  fingerprint (what's in the tuple, whether ownership/scope belong inside
  it too) if a future change to what `apply_ruleset()` manages makes the
  current split awkward.

- **`check_sibling_branch`'s own read failure (a non-404 gh error, e.g. an
  org's SAML enforcement blocking the call) is reported but does not fail
  `repo setup` as a whole.** The task instruction ("warn when the repo has
  an actual master branch... don't fail") covers the branch existing; it's
  silent on what a failure to even check should do. Chose non-fatal for the
  advisory — its outage shouldn't block the other steps. Since 2026-09-11
  the ruleset step asks the same question before it writes (a sibling
  branch holds the write), and THAT read failing fails the ruleset preview
  like any other unreadable precondition of the write, so the write is
  never made on a guess. Reversible: `sibling_branch` is one function, and
  `_hold_for_sibling_branch` is the only place that turns its error into a
  failed step.

- **`repo cleanup`'s curses picker measures label width with `len()`, not
  terminal columns.** A branch name with double-width characters (CJK, some
  emoji) occupies more cells than `len()` counts, so the too-small width
  gate (`width - 1 < 4 + max_label`) can under-count and let such a label
  clip or wrap into the next checkbox row, blurring which branch is which.
  Deferred (Codex review, mikelward/repo#54): this account's branch names
  are ASCII and the picker only runs in an interactive terminal, so it does
  not arise in practice. If it ever does, measure display width with
  `unicodedata.east_asian_width` (stdlib -- W/F count as two columns) in
  place of `len()` for both the gate and the `addnstr` cap, or fall back to
  the text prompt when a label's safe column width can't be established.
  Reversible: it only tightens an existing fallback and changes nothing for
  ASCII labels.
