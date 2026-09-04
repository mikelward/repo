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
      `audit_cmd.audit_secrets`, `audit_auto_merge`, and
      `audit_delete_branch_on_merge`, and flip the
      `assertEqual(code, 0, ...)` assertions in `SecretsAuditTest`,
      `AutoMergeAuditTest`, and `DeleteBranchOnMergeAuditTest` -- they are
      written to have to change. Until then the hubs and every converted
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
- [ ] **The scaffold no longer writes a `TODO.md`, and whether it should
      ever have is open** (maintainer, 2026-09-04: "i'm not sure if we
      need that"). The file carried exactly one item -- replace ci.yml's
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

- [ ] **The bootstrap failure does not say `--no-bootstrap` unblocks it.**
      A repository the item below describes fails with GitHub's own
      rejection relayed verbatim -- "Changes must be made through a pull
      request" -- which says nothing about what to do next, though
      `--no-bootstrap` gets the rest of `repo setup` through. The
      maintainer chose this over building the write path itself
      (2026-09-04: "branch-and-PR write path is a TODO for later"), so it
      is the near-term half: name the flag in the error, and say the
      scaffold still needs adding by hand.

- [ ] **Which `lanes.conf` docs rule `repo setup` should generate is
      undecided.** The scaffold writes one today, but the fleet is split:
      eight repositories including `mikelward/lanes` use the shorthand
      `docs **/*.md`, while `mikelward/lanes`'s own README documents the
      narrow pair `docs *.md` + `docs docs/**/*.md` as the standard. Put
      to the maintainer 2026-09-04, unanswered. Whichever wins, the other
      half of the fleet needs converting, and `mikelward/lanes`'s own
      `TODO.md` carries the matching entry.

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

- [ ] **Delete a superseded legacy ruleset, rather than only reporting
      it.** Where both names exist, `repo setup` now says so and leaves
      both in place. Deleting the redundant one was written and then cut
      back to this (maintainer, 2026-09-04) after review found the same
      class of gap five times, each only visible once the previous was
      fixed:

      1. it holds a rule type outside `MANAGED_RULE_TYPES`;
      2. it covers a ref the standard ruleset does not -- and an update
         leaves the standard one's own conditions alone, so a hand-made
         `main` scoped to `~DEFAULT_BRANCH` beside a legacy one covering
         `refs/heads/master` is the real case;
      3. it sets a managed parameter more strictly -- an approval count,
         code-owner review, strict-up-to-date;
      4. it binds a required check to a specific App via `integration_id`,
         which a context-name comparison misses;
      5. it grants no bypass actor where the standard one does, so
         deleting it lets that actor past every remaining gate.

      The lesson is the shape, not the list: "is A at least as strict as
      B" was being reimplemented field by field, which can always be one
      item short. Whoever picks this up should consider refusing to delete
      anything whose managed rules, scope and bypass actors are not
      byte-identical to the target's -- that cannot be one item short,
      at the cost of keeping duplicates that differ harmlessly. Also
      needed regardless: the deletion must be a planned mutation (shown in
      `--dry-run`, covered by the confirmation, and part of the
      fingerprint), it must run on the no-write path too since an
      already-correct `main` is the steady state, and where the
      merge-method conflict scan is told to skip a ruleset on the promise
      it will go, a failed delete has to fail the step.

- [ ] **`repo audit` does not report a surviving legacy ruleset.** `repo
      setup` notes one on every run, which is how the fleet finds them,
      but there is no read-only way to ask "which repositories still have
      two?" short of running setup against each. A `[FIX]` for a ruleset
      named in `LEGACY_RULESET_NAMES` would make the existing fleet sweep
      answer that.

- [ ] **Widening `main`'s own scope to the hardened three refs.** An
      update leaves an existing ruleset's conditions alone, so a hand-made
      `main` keeps whatever narrow scope it was given -- while a freshly
      created one gets `~DEFAULT_BRANCH`, `refs/heads/main` and
      `refs/heads/master`. Closing that would also make most legacy
      rulesets genuinely superseded, so it is worth doing before the
      deletion above. It is the first change here that would rewrite an
      existing ruleset's conditions, which is why it is its own item.

## repo setup and repo audit: lanes' trusted-verdicts design

Two designs for the required `lanes` check coexist in the fleet today, and
`repo setup`/`repo audit` only know about one of them. The default -- every
repo except typelauncher and yaml-lite -- runs `classify`/`lanes` on plain
`pull_request` with no credential at all: `mode: gate`'s own Actions
check-run IS the required check, `permissions: contents: read,
pull-requests: read` is enough, and `repo_lib/scaffold.py` generates exactly
this for `repo create --scaffold`. typelauncher and yaml-lite instead run
under `pull_request_target` and authenticate as a dedicated GitHub App
(`LANES_APP_ID`/`LANES_APP_PRIVATE_KEY` secrets in a `lanes` environment
scoped to the base ref), which `mode: init`/`gate` use to post the `lanes`
commit status directly via API rather than relying on the ambient
check-run. yaml-lite's own comment calls itself the fleet's pilot for this
"trusted-verdicts design" -- it exists to close a real hole in the default:
under plain `pull_request`, a PR can rewrite its own `ci.yml` to make the
`lanes` job `exit 0` and forge a green required check, something
`pull_request_target` (which always loads job definitions from the base
branch) closes on its own, at the cost of needing an explicit publisher
since a `pull_request_target` run's ambient check-run attributes to the
base tip, never the PR head.

This is shaped like the fleet-credentials work above, and should mostly
reuse it, but it is not a drop-in extension of `credentials.py` as it
stands -- see item 1.

- [ ] **Extend the fleet-credential machinery to `LANES_APP_ID`/
      `LANES_APP_PRIVATE_KEY`, with a detector of its own.** The naming
      already fits `batch_credentials(hub)`'s convention, but `lanes` is
      not a `workflow_call` reusable workflow invoked at job level with
      `secrets: inherit` -- it's a composite Action invoked at step level
      (`uses: mikelward/lanes@main`), and its `init`/`gate` steps reference
      `secrets.LANES_APP_ID`/`secrets.LANES_APP_PRIVATE_KEY` directly in
      their own `with:` block. `credentials.py`'s `callers()`/
      `caller_inherits()` look for a job-level `uses: mikelward/<hub>/...`
      reusable-workflow call and an `inherit`; neither matches this shape.
      "Does this repo use the App design" needs a new detector -- a step
      with `uses: mikelward/lanes@` whose `with:` sets `app-id:`/
      `app-private-key:` (or just the presence of both secret references
      in the workflow text) -- that plugs into the same environment-fanout
      logic `repo setup --credential`/`repo audit` already use, once it
      exists.
- [ ] **`repo audit` should report which design a repo is actually wired
      for, and flag drift the same way it flags a stray batch credential.**
      Four states: plain `pull_request` with no App secrets present (the
      accepted baseline -- not a finding); `pull_request_target` with both
      secrets correctly in the `lanes` environment (compliant); `pull_request_target`
      with the secrets missing, repository-scoped, or in the wrong
      environment (broken, `[FIX]`); and plain `pull_request` with
      `LANES_APP_ID`/`LANES_APP_PRIVATE_KEY` present anywhere -- a dead
      credential, since a workflow that never passes `app-id`/
      `app-private-key` to `mode: gate` never uses them, the same "stale
      copy for a workflow the repository does not use" treatment
      `credentials.py` already gives a batch credential.
- [ ] **`repo setup --credential LANES_APP_ID=... --credential
      LANES_APP_PRIVATE_KEY=...`** should place them exactly like a batch
      credential does today: fan into the `lanes` environment for a repo
      whose workflow already references them (per the detector above),
      delete a repository-level copy, leave an unaffected repo alone. This
      part is mechanically identical to the existing fleet-credentials
      step once the detector exists -- no new placement logic.
- [ ] **Wire the lanes App's slug into `repo_lib/apps.py`'s `--app` step**
      so a repo migrating onto the App design gets installation membership
      fixed by the same `repo setup` run that places its credential,
      rather than a separate manual step. Confirm the slug first --
      nothing in this repo currently records it.
- [ ] **The required `lanes` check is unbound for every repo `repo setup`
      has ever created, and that is the actual gap the App design exists
      to close -- closing it is `repo`'s work, not lanes'.**
      `rules.py:859`'s own comment says so: "Names off the command line
      carry no App binding, so every entry is unbound: any producer of
      that context counts." Both `_build_update_body` (`rules.py:591`) and
      the ruleset-create path (`rules.py:538`) compose `{"context": c}`
      with no `integration_id`, even though the READ side -- the
      `(context, integration_id)` pairs, `bound_to_another_app`, the
      whole `never_reported`/`describe_missing` App-aware plumbing --
      already understands a bound check and is exercised today (for
      `codex`, which GitHub may already report as App-bound on the read
      side without this tool ever having asked for it). Without a write
      path, a repo running the App design still has a ruleset that
      accepts a `lanes` context from ANY source -- including a forged one
      from an unrelated `push`-triggered workflow a same-repo PR could
      add, which is exactly the threat lanes' own TODO.md (its "round
      seven") names as still unclosed and calls "real infrastructure...
      not a consumer-template detail." Closing this needs: a
      `--rule NAME@APP_ID`-shaped CLI surface, `rules.py` writing
      `integration_id` into the entries it composes, and `repo audit`
      reporting an unbound `lanes` check as a `[FIX]` specifically on a
      repo that ALSO holds the App credential (that pairing is the tell
      that the repo believes it's protected and isn't). Confirm against a
      real ruleset, before relying on it, that this account's GitHub plan
      actually enforces `required_status_checks[].integration_id` the way
      lanes' TODO.md still marks unverified ("whether GitHub's
      required-check 'expected source' feature actually restricts by app
      identity on this account's plan") -- this tool already round-trips
      ruleset JSON, so it's a reasonable place to do that confirmation
      rather than lanes itself.
- [ ] **Don't default `repo create --scaffold` onto the App design yet --
      that's the owner's call, not autopilot's, once the above exists.**
      `repo_lib/scaffold.py` only ever generates the plain-`pull_request`
      wiring today. Lanes' own TODO.md still lists two gaps in the App
      design as open, not this rollout's to close: an `actions/cache`
      poisoning path in a `pull_request_target` heavy job with no answer
      yet, and the same unverified `integration_id`/ruleset-restriction
      question item 5 above depends on. Scaffolding every new repo onto a
      design with a documented, unclosed gap baked in from day one is a
      worse default than the current one; revisit once those two close.
- [ ] **Migrating an EXISTING repo's `ci.yml` is not scaffold-fill work and
      doesn't belong in `repo setup` as a mechanical step.** Unlike a
      missing file, each repo's heavy-job graph (`build`, `check`, `msrv`,
      `connected-tests`, ...) is bespoke, and the migration has to move
      each one to `pull_request_target`, switch its checkout to the merge
      snapshot, and verify it references no `secrets.*` -- exactly the
      per-repo work lanes' own TODO.md defers to "whichever pull request
      actually pilots a consumer onto this." `repo setup`/`repo audit`
      carry the credential/App-membership/ruleset-binding side (the items
      above) and can flag which repos are wired for which design; the
      workflow-file rewrite itself stays a human-or-agent-authored PR per
      repo, built from typelauncher's or yaml-lite's actual `ci.yml` --
      not the README's abbreviated template alone (see below). Once the
      items above land here, pilot exactly one more repo (neither
      typelauncher nor yaml-lite, which are already done) before treating
      this as a routine fleet-wide pass, per lanes' own AGENTS.md: "a
      change that touches consumers goes through ONE of them first."

**What this does NOT need from `mikelward/lanes` itself:** the
App-publishing mechanism (`mode: init`/`gate` with `app-id`/
`app-private-key`, JWT signing, the installation-token exchange) is
already built and tested, per lanes' own TODO.md ("round eight"). Every
item above is `repo`-side tooling or per-consumer workflow-file work.

One real gap in lanes IS worth a small fix, independent of anything above:
its README's copy-paste template for the trusted-publishing design (the
`init`/`finalize` example) shows `environment: lanes` and the finalizer's
`if: ${{ !cancelled() }}`, but never shows the `concurrency:`/
`cancel-in-progress` group its own TODO.md ("round six") says the design
needs -- without one, an in-flight run superseded by a retarget or title
edit can still land its stale terminal write after the newer run's. Both
real consumers carry one (yaml-lite's is the simpler of the two:
`concurrency: {group: ci-${{ github.event.pull_request.number ||
github.ref }}, cancel-in-progress: true}`); the README template doesn't,
so a reader following it literally reconstructs that piece from prose
scattered across TODO.md instead of copying working code. Worth a small
PR against `mikelward/lanes` on its own, ahead of anything above.

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
