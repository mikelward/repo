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
      generates, diffs it against the target's current tree, and pushes
      whatever's missing as one commit -- never overwriting a path
      already occupied by anything, file or directory. Applies BEFORE
      the ruleset step in the same run, since a ruleset requiring pull
      requests blocks the direct ref update this uses (Codex review,
      mikelward/repo#14).
- [x] **The scaffold no longer writes a `TODO.md`, and that stands**
      (maintainer, 2026-09-04: "i'm not sure if we need that"; kept
      removed under autopilot, 2026-09-04 -- see *Decisions needing
      review*). The file carried exactly one item -- replace ci.yml's
      placeholder job with the project's real jobs -- and ci.yml already
      states the same thing in a comment directly above that placeholder,
      so the scaffold was writing a second copy of a single instruction
      into a second file, where the two could drift and where a project
      that keeps its own TODO.md would find the path taken. Removed;
      `build_scaffold_files` now produces seven files, and the instruction
      lives only where the work is. Bring it back only with something to
      say that ci.yml cannot say in place -- a fleet-wide checklist a new
      repository should work through, say -- not as a restatement of the
      placeholder comment.

- [ ] **Populate `CLAUDE.md` and `AGENTS.md` from a template**
      (maintainer, 2026-09-04). Every repository in the fleet carries an
      `AGENTS.md` with `CLAUDE.md` a symlink to it, and the scaffold
      writes neither -- so a freshly created repository is the one place
      in the fleet an agent works with no conventions loaded at all, which
      is exactly when it is most likely to invent some. The template
      should live somewhere shared rather than inline in this repo:
      `mikelward/conf`'s `templates/` (installed as `~/.templates`) is the
      candidate the maintainer named, and `scaffold.py` already knows how
      to fetch a file from another repository over the API
      (`_fetch_file`), so reading it from there is the same shape as the
      codex-review templates it already pins. Three things to settle
      first: what the template holds (the rules every repo's copy shares
      -- talking to the user, asking questions, git workflow, privacy,
      reviews -- against a near-empty stub the project fills in), whether
      it is pinned to a sha the way `TEMPLATE_FILES` are or tracked at
      `main`, and how the symlink gets made -- the Git Data API can write
      a `120000` blob, so `CLAUDE.md -> AGENTS.md` is reachable without a
      clone, and `plan_gaps` already refuses to overwrite one.

- [x] **The bootstrap failure names `--no-bootstrap`.** A repository the
      item below describes fails with GitHub's own rejection relayed
      verbatim -- "Changes must be made through a pull request" -- which
      says nothing about what to do next. `apply_gaps`'s ref-update
      failure now adds a line naming the way past both of the two causes
      it cannot tell apart: rerun if the branch simply moved, and
      `--no-bootstrap` to get the rest of `repo setup` through a branch
      this step cannot write to at all, with the scaffold left to add by
      hand. The write path itself is still the item below (maintainer,
      2026-09-04: "branch-and-PR write path is a TODO for later").

- [ ] **Which `lanes.conf` docs rule `repo setup` generates: guessed,
      not settled.** The fleet is split: eight repositories including
      `mikelward/lanes` use the shorthand `docs **/*.md`, while
      `mikelward/lanes`'s own README documents the narrow pair
      `docs *.md` + `docs docs/**/*.md` as the standard. Put to the
      maintainer 2026-09-04, unanswered; autopilot kept the narrow pair
      the scaffold already writes (see *Decisions needing review*). What
      is still open is the fleet: whichever form wins, the other half
      needs converting, and `mikelward/lanes`'s own `TODO.md` carries the
      matching entry. Nothing in this repository changes either way --
      `_LANES_CONF` in `scaffold.py` is one string.

- [ ] **A repository a PRIOR `repo setup` run already protected has no
      path to a scaffold fix here at all.** The ordering fix above only
      covers a ruleset THIS run is the one creating; apply_gaps's direct
      `git/refs/heads/{branch}` PATCH is still the only write this step
      knows how to make, and any ruleset requiring pull requests blocks
      it outright for a non-bypass caller (which `repo setup` never
      configures itself to be). Today that shows up as a plain write
      failure -- `failed on: bootstrap`, GitHub's own rejection relayed
      verbatim -- rather than a fix. Closing it for real needs a second
      write path: create a new (unprotected) branch off the current tip,
      push the missing files there, open a pull request, and either wait
      for it or auto-merge it once its own checks (which the scaffold it
      is adding may be the very thing that makes exist) pass. Until then,
      an already-protected repository missing a scaffold file needs it
      added by hand, through an ordinary PR.

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

- [ ] **A second ruleset under the STANDARD name is still picked over
      silently.** GitHub does not make a ruleset's name unique within a
      repository, and `find_legacy_rulesets` now returns every id sharing
      a legacy name (Codex review, mikelward/repo#31) -- but
      `_lookup_existing_ruleset` still answers with the first id under
      `main`, so a repository holding two of those has one updated and the
      other left applying with nothing said. Pre-existing, not introduced
      by the deletion work: the deletion path only ever targets legacy
      names, so nothing new is deleted on the strength of it. The fix is
      the same shape as the legacy one -- report every id, adopt the
      first -- and it wants its own change, since it decides what happens
      when the survivor and the second copy disagree.

## repo cleanup

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

- **The scaffold's `lanes.conf` keeps the narrow docs pair
  (`docs *.md` + `docs docs/**/*.md`), not the shorthand `docs **/*.md`**
  (autopilot, 2026-09-04; the question had been put to the maintainer and
  was unanswered). Two reasons. It is what `mikelward/lanes`'s own README
  documents as the standard, so a repository this creates matches the
  documentation a reader will find. And it is the safer of the two to
  apply to a repository nobody has looked at yet: `**/*.md` routes
  markdown at any depth down the docs lane, including a README sitting
  beside code the full CI validates -- readmo's own guide says markdown
  outside the root and `docs/` is code for exactly that reason -- so the
  shorthand can skip a code lane on a diff that needed it, while the
  narrow pair only ever costs a docs-only change a full CI run. The
  alternative was matching the eight repositories using the shorthand.
  Reversible: `_LANES_CONF` in `scaffold.py` is one string, and it only
  affects repositories scaffolded after the change -- an existing
  `lanes.conf` is never overwritten.

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
