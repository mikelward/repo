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
      before the ruleset write.

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

- [ ] **The scaffold adds, it never updates.** A scaffold file already on
      a branch is left exactly as it is, template drift included -- the
      module's own promise, and deliberate, since reconciling drift is a
      human decision and the difference may be a project's own
      customization. The cost is that nothing in the fleet ever brings an
      old copy forward when `mikelward/codex-review`'s templates move: the
      file is present, so every later run passes over it. Raised as a
      finding against the reuse path (Codex, mikelward/repo#42: an older
      scaffold pull request adding the same filenames with older contents
      merges, and those contents then persist), but it is the whole
      design, not that path -- the same is true of any repository whose
      copy predates a template change, pull request or not. Closing it
      means a real update step: compare each present scaffold file against
      the template, report the drift, and offer to take the template's
      version -- which is a different, riskier thing from gap-filling and
      wants its own flag and its own confirmation.

- [ ] **Converging a repository still takes three runs, and `codex` is
      why.** The scaffold pull request makes `lanes` and `zizmor` report,
      but not `codex`: its status-writing workflow runs under
      `pull_request_target`, which GitHub takes from the BASE branch's
      copy, so the pull request adding that workflow is the one pull
      request it cannot run on. So: run one opens the pull request, a
      human merges it, run two still skips the ruleset step (`codex` has
      never reported), some real pull request happens, run three writes
      the ruleset. `--force` waives the never-reported guard and is a
      legitimate way to cut that to two, since the workflow IS on the
      default branch by then and the next pull request will report.
      Closing it properly needs `repo setup` to open something for
      `codex` to run on once the scaffold has landed -- which means
      inventing a diff, and no good candidate has turned up yet.

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

- [x] **Delete a superseded legacy ruleset, rather than only reporting
      it.** `repo setup` now deletes one whose content is identical to
      what the standard ruleset will hold once this run has written it,
      and reports any that is not. The comparison is whole-object
      equality (`_comparable_ruleset`: everything GitHub reports except
      the fields identifying that copy, and its name) rather than "is A
      at least as strict as B", which had been reimplemented field by
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
      Immediately before each delete, `_still_superseded` re-reads BOTH
      rulesets and compares them as they now are (Codex review,
      mikelward/repo#31): the plan's own reading is a network round trip
      old by then, since the survivor's write sits between, and it
      compares against the body this run meant to write rather than the
      one GitHub actually stored. A read it cannot make keeps the
      duplicate rather than counting as "unchanged", and both names are
      checked alongside the content: equality deliberately ignores the
      name, so it says nothing about which of the two is the standard
      ruleset, and a rename landing in that window would otherwise delete
      whichever one had just become canonical.
      The merge-method conflict scan needed no "skip the one that is
      about to go": a deletable ruleset is identical to the target, and
      the target always allows rebase, so the scan can never flag one.

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

      **Deleting an extra once it adds nothing beyond the standard is
      deliberately NOT done.** It reads as the tidy finish, and it needs
      exactly the comparison this module already retreated from: "is A at
      least as strict as B", per field, which was reimplemented five times
      and was one field short every time (see `_comparable_ruleset`). The
      legacy path can use equality because equality cannot be incomplete;
      a subset test cannot, and here a false "adds nothing" deletes a
      ruleset that was holding the branch up. Tidiness is not worth that
      failure mode. If it is ever revisited, the safe slice is an extra
      whose *scope* provably excludes the default branch -- no strictness
      comparison needed at all.

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

- **`check_master_branch`'s own read failure (a non-404 gh error, e.g. an
  org's SAML enforcement blocking the call) is reported but does not fail
  `repo setup`.** The task instruction ("warn when the repo has an actual
  master branch... don't fail") covers the branch existing; it's silent on
  what a failure to even check should do. Chose non-fatal — an advisory
  check's own outage shouldn't block the ruleset write, which is the part
  of `repo setup` that actually matters — over failing closed the way the
  ruleset steps themselves do. Reversible: flip the last branch in
  `check_master_branch` to return a failure signal and have `setup_cmd.run`
  fold it into the exit code, if the owner wants this check held to the
  same fail-closed standard as the rest of the module.
