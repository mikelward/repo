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
reuse it, but it is not a drop-in extension of `credentials.py`/
`secrets_cmd.py` as they stand -- see items 1 and 2 below (Codex review,
mikelward/repo#26, caught both: the detector needs to parse a real
workflow step rather than grep for the secret names, and the environment
this credential lands in needs a branch policy `_ensure_environment`
doesn't set today).

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
      **Feeding this detector `credentials.workflow_texts()` would be
      wrong in a way none of the batch credentials' own use of it is --
      that function deliberately reads EVERY branch, and a
      `pull_request_target` publisher's existence is a property of ONE
      branch specifically** (Codex review, mikelward/repo#26):
      `workflow_texts` (`credentials.py:322-344`) aggregates a workflow's
      text from every branch where it differs from the default branch's
      copy, by design -- its own docstring explains why: a push-triggered
      batch-credential caller runs from whatever branch it lives on, so a
      caller that exists only on a feature branch still needs its
      credential, and reading the default branch alone would delete it as
      unused. That reasoning does not transfer here. A `lanes` `gate`
      step's *existence on some branch* says nothing about whether the
      branch this rollout is actually auditing -- `--branch NAME` or the
      resolved default -- has a working publisher: a migration attempt
      left on an unmerged feature branch would still make this detector
      report "App design consumer" for the repository, and `repo setup`
      run against `main` could then move the credentials and bind `lanes`
      while `main` itself has no publisher at all, blocking every merge
      there. This detector needs to read only the workflow text of the
      branch actually being audited, not `workflow_texts()`'s all-branches
      aggregation -- a narrower read than the batch-credential callers use,
      for a narrower question than theirs.
      "Does this repo use the App design" needs a new detector -- and a
      raw text-presence check for `LANES_APP_ID`/`LANES_APP_PRIVATE_KEY`
      is not enough (Codex review, mikelward/repo#26): both names could
      appear in a comment, or in a step that isn't a live `lanes`
      invocation at all, and a false match would then move or delete a
      credential the repository doesn't actually use lanes' publisher
      with -- `repo audit` reporting it compliant while the publisher is
      actually unusable. The detector needs to parse a structurally
      recognized step -- `uses: mikelward/lanes@...` with `mode: gate` AND
      a `with:` block that sets both `app-id:` and `app-private-key:` --
      and fail closed (report "cannot tell", the same posture
      `credentials.py`'s `unread_mentions` already takes for a
      reusable-workflow call it can't parse) on any shape it doesn't
      recognize, rather than treating an unparseable shape as "not
      present."
      **The `gate` step also has to target the RIGHT pull request, and
      mode plus credentials alone say nothing about that** (Codex review,
      mikelward/repo#26): lanes takes which PR to publish against as its
      own `pr:` input rather than inferring it, and `scaffold.py`'s
      generated `gate` step sets it to
      `${{ github.event.pull_request.number }}` (`scaffold.py:305`) for
      exactly that reason -- under `pull_request_target` that resolves to
      the triggering PR. A structural match on `mode: gate` plus the two
      credential inputs still accepts a step with `pr:` omitted, set to a
      literal, hard-coded to a different PR number, or built from an
      unrelated expression -- any of which either misdirects the
      published status or leaves the step unable to publish at all, while
      still reading as a fully credentialed publisher. The detector needs
      to also confirm the `pr:` input resolves to
      `github.event.pull_request.number` (or a proven equivalent under an
      indirection it can actually follow), failing closed on an
      expression it can't resolve.
      **A correctly-shaped `gate` step can still be a rubber stamp -- none
      of the checks above confirm its VERDICT inputs actually reflect the
      jobs it's supposed to be protecting** (Codex review, mikelward/repo#26):
      `gate` computes its terminal status from `classify-result:`,
      `base-sha:`, and `results:`, and the generated scaffold wires every
      heavy job through both `needs:` and `results:` for exactly that
      reason (`scaffold.py:278-308`). A `gate` step matching every
      structural check so far -- mode, credentials, `pr:`, environment,
      trigger, isolation, condition -- can still omit a failing heavy job
      from `results:`, or supply a literal or stale value for any of the
      three inputs, and publish a clean verdict regardless of what actually
      happened. The detector needs to also confirm `results:` corresponds
      to the job's own `needs:` dependency graph -- every heavy job present,
      none missing -- and that `classify-result:`/`base-sha:` come from
      `classify`'s own outputs rather than a literal or an unrelated
      expression, failing closed on a `results:` expression or dependency
      graph it can't fully resolve rather than assuming correspondence.
      **Setting `app-id:`/`app-private-key:` at all is not the same as
      setting them FROM `LANES_APP_ID`/`LANES_APP_PRIVATE_KEY`
      specifically** (Codex review, mikelward/repo#26): a `gate` step
      could set those two keys from a literal, a different variable, or a
      differently-named secret, and the structural match above -- key
      presence only -- would still call it a consumer of THESE two
      credentials. That would move or delete `LANES_APP_ID`/
      `LANES_APP_PRIVATE_KEY` for a publisher that never reads them, and
      bind the ruleset with no real publisher for the App it names. The
      detector needs the two input EXPRESSIONS themselves to resolve to
      `secrets.LANES_APP_ID`/`secrets.LANES_APP_PRIVATE_KEY`, not just
      the two keys to be present, and to fail closed on an expression it
      can't resolve -- an indirection through a job-level `env:`, a
      workflow input, or similar.
      **The mode matters, not just the two inputs' presence** (Codex
      review, mikelward/repo#26): `classify` mode never publishes
      anything -- only `init` (posts `pending`) and `gate` (posts the
      terminal result) do, per lanes' own README. A `classify` step that
      happens to carry `app-id:`/`app-private-key:` inputs too (a
      copy-paste, most likely, since they do nothing there) is not a
      publisher, and matching on the two inputs alone would still call it
      one -- moving the credential and binding the ruleset with no
      `init`/`gate` step anywhere actually reading it. The detector's
      structural match has to include the mode, not stop at `uses:` plus
      the two `with:` keys.
      **A credentialed `init` step alone is not a publisher either --
      only `gate` posts a terminal result, so `gate` is the one that has
      to be there** (Codex review, mikelward/repo#26, catching a real gap
      in the fix right above: an earlier revision of this item accepted
      `mode: init` OR `mode: gate` as equally sufficient). `init` only
      ever posts `pending`, which never satisfies a required check by
      itself; a repository with a credentialed `init` step and no
      credentialed `gate` step anywhere would be recognized as an App
      design consumer and have its ruleset bound, with no step that could
      ever post the success that unblocks a merge. The detector requires
      a credentialed `mode: gate` step; a credentialed `init` step is
      additional evidence when present (the real design normally has
      both, per the initializer/finalizer split), never a substitute for
      it.
      **`init` is not merely "additional evidence when present" -- without
      it, a re-run that doesn't change the head SHA leaves the OLD
      terminal status covering the whole re-run window, and that was
      wrong to accept as optional** (Codex review, mikelward/repo#26):
      GitHub evaluates a required status check against whatever the
      latest posted status for that `(context, sha)` pair is, and a
      re-run triggered by something other than a new commit -- a title
      edit, a retarget, a `synchronize` that lanes' own trigger filters
      still accept -- doesn't clear the previous run's status first. A
      `gate`-only publisher leaves the PREVIOUS success sitting there,
      green, for the entire time the new run takes to reach its own
      `gate` step -- during which the check reads "satisfied" even though
      the diff being re-evaluated might turn out to fail. `init`'s whole
      job is to post `pending` as early as the new run reaches it, which
      SHRINKS that window drastically compared to `gate`-only; that's a
      correctness property, not extra credit. `repo`'s own generated
      `classify` step already sets `mode: classify` early for a different
      reason (docs-lane skip), but nothing there posts `pending` for
      `lanes` itself the way `init` does. The detector needs to require
      BOTH a credentialed `init` step early in the trigger's run AND a
      credentialed `gate` step as the terminal one -- `init` is no longer
      "additional evidence when present," it's a second required element,
      and the compliant predicate below needs to check for it the same
      way.
      **`init` NARROWS the window, and this item should not have claimed
      it makes the window "not exist" -- a real residual gap remains, and
      it is a GitHub platform limit this tool cannot close, not something
      `init` failed to do** (Codex review, mikelward/repo#26): between the
      triggering event (the title edit, the retarget) and the moment
      `init`'s own API call actually completes, the previous status is
      still the latest one on record -- GitHub has to schedule the new
      workflow run before anything in it executes, and that scheduling
      latency plus one HTTP round-trip is real, non-zero time during which
      a merge (manual, or an auto-merge already armed on the strength of
      the old green status) can still land against a diff whose
      re-evaluation hasn't started. No mechanism this design has access
      to -- `init`, a webhook, a required check -- can make that interval
      exactly zero; closing it fully would need GitHub itself to
      invalidate a status synchronously with the triggering event, which
      is not a capability the platform offers. `init` is still worth
      requiring because it turns a whole-run-duration window into a
      scheduling-latency one, but this item should say that precisely
      rather than claim the window is closed, and should carry the
      residual gap forward as an explicit, named limit rather than a
      solved problem.
      **"Credentialed" isn't a precise enough bar for `init` either --
      every structural check this item spells out in detail for `gate`
      (the right `pr:`, the right environment, a condition that doesn't
      quietly skip it, isolation from untrusted steps) applies to `init`
      just as much, and leaving those unstated for `init` reopens the
      exact gap requiring it was meant to close** (Codex review,
      mikelward/repo#26): an `init` job that references
      `LANES_APP_ID`/`LANES_APP_PRIVATE_KEY` but omits `environment:
      lanes`, targets the wrong `pr:`, or sits behind a condition that
      evaluates false for the run that needs it, satisfies "a credentialed
      `init` step" by name while never actually posting `pending` -- the
      previous status stays green exactly as long as it would with no
      `init` at all. The detector needs to hold `init` to the same
      structural bar as `gate`: the `pr:` input, `environment: lanes`, a
      condition that runs it for the triggering event, and the same
      same-job isolation from untrusted execution -- not a separate,
      looser "credentialed" check that only verifies the two secret
      inputs are present.
      **None of those checks say WHEN `init` runs, and an `init` job
      gated behind `needs:` on a heavy job passes every one of them while
      recreating the exact whole-run window requiring `init` was meant to
      shrink** (Codex review, mikelward/repo#26): `init`'s value is
      entirely in posting `pending` as early as possible -- an `init` job
      with `needs: build` can carry the right `pr:`, `environment: lanes`,
      an `if: ${{ !cancelled() }}` condition, and clean isolation, and
      still not post anything until `build` finishes, during which the
      previous run's status is exactly as stale as it would be with no
      `init` at all. The detector needs to also confirm `init` has no
      `needs:` (or, if it does, that every job it depends on is itself
      near-instant and not part of the heavy-job graph being checked) --
      the safe, checkable shape is simply "first in the graph, nothing it
      waits on," which is what the real design's own `init` job already
      is (typelauncher's `ci.yml`: "First in the graph, no `needs:`").
      **`init` having no `needs:` only makes it ELIGIBLE to run early --
      nothing about that shape stops `gate` from being scheduled
      concurrently with it, or finishing first** (Codex review,
      mikelward/repo#26): GitHub schedules jobs by dependency
      satisfaction, not by which one was declared "first" in the file, so
      unless `gate` itself depends -- directly or transitively through the
      heavy-job graph -- on `init`, a short or cache-warmed job graph can
      let `gate` complete and publish its terminal status before a
      delayed `init` (queued behind a concurrency limit, a slow runner
      allocation) ever posts `pending`. That's the same stale-status
      problem this whole `init` requirement exists to shrink, just
      relocated: instead of a stale PREVIOUS run's status sitting there
      too long, it's this run's own terminal status landing with no
      `pending` ever having appeared ahead of it. The detected publisher
      shape needs `gate`'s job to declare `init` in its own `needs:`
      (directly, or transitively through every path to it), not just
      `init` declaring no incoming ones.
      **The step's own trigger matters too -- a credentialed `gate` step
      inside a workflow that never runs `pull_request_target` is not a
      working publisher for pull requests either** (Codex review,
      mikelward/repo#26): the whole reason this design needs the App at
      all is that `pull_request_target` is the trigger whose ambient
      check-run can't land on the PR head, so the App posts a status
      instead. A workflow triggered on `push` (or on `pull_request_target`
      for some unrelated job only) could still contain a syntactically
      matching, credentialed `gate` step -- producing real App-attributed
      history on pushes to the default branch, enough to clear
      `never_reported`'s preflight -- while actual pull requests keep
      running whatever the old plain check was, since nothing in a
      `push`-triggered run ever executes for a PR event at all. `repo
      setup` would then bind `lanes` to the App on the strength of history
      that has nothing to do with pull requests, and every PR merge blocks
      the moment the ruleset requires a status nothing posts for it. The
      detector needs to also confirm the credentialed `gate` step's
      workflow is triggered by `pull_request_target`, and fail closed on
      a trigger shape it can't parse (a matrix, an external reusable-
      workflow indirection, or similar) rather than assume coverage.
      **A credentialed `gate` step also has to actually RUN when one of
      its own job's dependencies fails, or it never publishes anything at
      all -- not even a failure** (Codex review, mikelward/repo#26): a
      `needs:`-dependent job is skipped by GitHub the instant a dependency
      fails or is cancelled, unless the job's own `if:` overrides that
      default -- which is exactly why lanes' README template puts
      `if: ${{ !cancelled() }}` on the finalizer (see this section's
      closing note on the missing `concurrency:` block, which quotes the
      same line). A `gate` job with `needs:` and no such condition still
      matches every structural check above -- credentials, mode,
      environment, trigger -- while a single upstream failure silently
      skips it, leaving the required check with no producer at all rather
      than a failing one. The detector needs to also confirm the `gate`
      job's own condition keeps it running after a dependency failure
      (`if: ${{ !cancelled() }}` or an equivalent that isn't strictly
      narrower), and fail closed on a condition expression it can't
      analyze.
      **A job-level `if: !cancelled()` does not, by itself, make the
      `gate` STEP run either -- steps have their own independent default,
      and the paragraph above stopped one level too high** (Codex review,
      mikelward/repo#26): each step in a job evaluates its own `if:`
      condition, defaulting to `success()` when none is given, regardless
      of what the enclosing job's own `if:` says. A job with
      `if: ${{ !cancelled() }}` and an earlier step that fails still skips
      every later step that carries no override of its own -- so a `gate`
      step sitting after some other step in the same job, with no `if:`
      on the `gate` step itself, is skipped exactly the way the job-level
      fix above was meant to prevent, whenever that earlier step fails.
      The detector needs to check the `gate` STEP's own condition (not
      just its job's), unless it is the only step in the job -- and fail
      closed the same way on a step-level condition it can't analyze.
      **A correct `if:` on the `gate` step protects against a SKIPPED
      step, not a TAMPERED one -- a preceding step in the same job that
      executes PR-controlled content can compromise the runner itself,
      and `gate`'s credentials ride on the same compromised runner
      whatever its own `if:` says** (Codex review, mikelward/repo#26):
      every step in a job shares one runner and one filesystem, so a
      step that runs untrusted code before `gate` -- a build, a test
      suite, anything that executes rather than merely fetches PR
      content -- can tamper with the toolcache, `PATH`, a downloaded
      action's cached files, or other runner state in a way that
      compromises `gate`'s own execution and lets it exfiltrate
      `app-id`/`app-private-key` without ever touching `secrets.*`
      directly itself. This is a different threat from every check above:
      it doesn't care whether `gate`'s own `if:`, mode, or trigger are
      correct, because the compromise happens before `gate` runs at all.
      The real design's own `finalize` job (typelauncher's `ci.yml`) is
      the safe shape already: its only step before `gate` is
      `actions/checkout` with `persist-credentials: false` -- fetching PR
      content as data, never executing it -- and every job that DOES
      execute PR content (`build`, `connected-tests`) is a separate job
      with no credentials in it at all. The detector needs to require
      that shape: the credentialed `gate` step runs in a job whose only
      other steps fetch content rather than execute it (a checkout is
      fine; a build/test/lint step is not), and fail closed on a job
      whose other steps it can't classify either way. Full verification
      -- proving a "safe" step like `checkout` itself wasn't compromised
      further upstream -- is out of scope for a static check; this item
      only closes the same-job, executes-before-`gate` case, which is the
      one a migrated `ci.yml` could introduce by accident.
      **A credentialed, correctly-triggered `gate` step still isn't a safe
      publisher without a PR-scoped, cancel-in-progress `concurrency:`
      group on its workflow -- and this section's own closing note below
      was wrong to treat that as ONLY an independent README fix, separate
      from what the detector checks** (Codex review, mikelward/repo#26):
      without one, a run superseded by a retarget, a title edit, or a
      fresh push can keep executing instead of being canceled, and if it
      finishes after the newer run does, its stale terminal status can
      overwrite the newer, correct one on the same PR -- the newly
      App-bound required check would then read "satisfied" on the strength
      of a superseded run's verdict, not the current head's. A repository
      whose workflow otherwise matches every check above but carries no
      such `concurrency:` group is not a safe publisher yet, so it isn't
      enough to fix lanes' own README template and leave every consumer's
      own workflow file unchecked. The detector needs to also confirm the
      workflow declares a `concurrency:` group scoped to the PR with
      `cancel-in-progress: true`, and the compliant predicate below needs
      to require it the same way it requires the trigger and the mode.
      **"Scoped to the PR" has to mean `github.event.pull_request.number`
      specifically -- `github.ref` alone is not PR-unique under
      `pull_request_target` and accepting it was a real gap in the
      paragraph above, not just an imprecise gloss on it** (Codex review,
      mikelward/repo#26): `pull_request_target` runs against the BASE
      branch's context, so `github.ref` there resolves to something like
      `refs/heads/main` -- identical across every open PR targeting that
      branch. A concurrency group keyed on `github.ref` alone would put
      every PR targeting `main` in the SAME group, so starting one PR's
      run cancels another PR's already-running publisher, leaving that
      other PR's `lanes` status stuck `pending` indefinitely -- the exact
      failure mode this whole item exists to prevent, just caused by the
      fix meant to prevent it. `github.ref` is a safe fallback only for a
      non-PR trigger this workflow might also run under (a `push`, which
      is genuinely one ref at a time) -- exactly how yaml-lite's own
      `github.event.pull_request.number || github.ref` pattern (quoted in
      this section's closing note) uses it: PR number first, ref only
      when there is no PR. The detector needs to confirm the group key
      resolves to the PR number for every `pull_request_target` run, not
      merely that SOME expression involving `ref` appears in it.
      **A correct concurrency group only protects against a SECOND RUN OF
      THE SAME workflow -- it does nothing if a repository has TWO
      independently triggered publisher pairs, each internally correct
      and each in its own group** (Codex review, mikelward/repo#26): if
      two workflow files each contain a recognized, credentialed
      `init`/`gate` pair -- a leftover from a previous migration attempt
      alongside a new one, say -- GitHub's `concurrency:` cancellation
      only applies within one group, so a group keyed
      `ci-${{ ...number }}` in one file and `lanes-${{ ...number }}` in
      the other never interact: both can run for the same PR at once, and
      whichever finishes last wins the required check's latest status
      regardless of which one is actually current. Every check this item
      has added so far -- mode, credentials, `pr:`, environment, trigger,
      isolation, the group key itself -- can pass independently for BOTH
      publishers while this failure mode still exists. The detector needs
      to also confirm there is exactly one qualifying publisher pair on
      the audited branch (per the branch-scoping note above); more than
      one is `[FIX]`, full stop.
      **"Or every pair shares the same group" was a wrong escape hatch --
      sharing a group stops one from overwriting the other's STATUS, but
      says nothing about whether they check the same things** (Codex
      review, mikelward/repo#26): `concurrency:` cancellation launches both
      runs for the same event and then cancels one, ordering-dependent,
      until a single survivor remains -- it does not prevent both from
      starting, and it does not require the two workflows' job graphs to
      match. If one publisher's `gate` covers `build`+`test` and the
      other's covers only `test`, whichever survives can post success on
      the strength of a narrower check than the one that got canceled --
      the canceled workflow's `build` might have failed, and nothing ever
      reports that. A shared group is not equivalence, so it can't stand
      in for uniqueness. The detector's requirement is simply exactly one
      qualifying pair; more than one is `[FIX]` regardless of whether they
      share a group.
      **The step alone is not enough -- the ENCLOSING JOB has to select
      the `lanes` environment too, or the move this detector triggers
      breaks the very thing it's meant to protect** (Codex review,
      mikelward/repo#26): an environment-scoped secret is invisible to a
      job that doesn't declare that environment (the same fact this
      section's own credential-placement item, and every sibling hub's
      `AGENTS.md`, already states for the batch credentials). If the
      detector matches the step but the job around it never sets
      `environment: lanes`, moving `LANES_APP_ID`/`LANES_APP_PRIVATE_KEY`
      into that environment and deleting the repository-level copies (per
      the credential-placement item below) leaves the step reading empty
      secret values -- the publisher silently stops working, and `repo
      audit`'s compliant state (secrets correctly in the `lanes`
      environment) reports it fine regardless, since it never checks
      whether anything actually reads them from there. The detector needs
      to confirm the job declares `environment: lanes` as part of
      recognizing the step, not just the step in isolation, and fail
      closed the same way on an environment expression it can't parse.
- [ ] **The `lanes` environment needs a deployment-branch policy BEFORE
      the credential is placed in it, and today's environment creation
      doesn't set one** (Codex review, mikelward/repo#26). Lanes' own
      TODO.md is explicit that this credential's safety depends on it
      living in "a GitHub Environment with a deployment branch/ref policy
      restricted to the trusted base ref" -- without that, any workflow in
      the repository that can declare `environment: lanes`, including one
      a same-repo pull request adds on its own branch, reads the same
      secret the legitimate `init`/`finalize` jobs do, and can mint its
      own installation token and forge the status. `secrets_cmd.py`'s
      `_ensure_environment` (`secrets_cmd.py:218`) is a bare
      `PUT repos/{repo}/environments/{env}` with no body -- GitHub's
      default for a newly created environment has no branch restriction
      at all, and the function's own docstring already warns that PUTting
      an EXISTING environment silently resets whatever policy it had back
      to that default. So placing `LANES_APP_ID`/`LANES_APP_PRIVATE_KEY`
      cannot reuse `_ensure_environment` unmodified the way a batch
      credential's environment does today -- it needs an explicit branch-
      policy write (the `deployment-branch-policies` endpoint) as part of
      creating or confirming the `lanes` environment, checked BEFORE the
      secret is written, not assumed from the environment merely
      existing. This is the environment-side counterpart of the "confirm
      against a real ruleset" item below on `integration_id`, and it
      blocks the credential-placement item below, not just the detector
      above.
      **Writing entries via `deployment-branch-policies` doesn't work on
      an environment created by the bare PUT above -- that endpoint
      requires the environment's OWN `deployment_branch_policy` mode to
      already be `custom_branch_policies: true` first, a second write
      this item hadn't named** (Codex review, mikelward/repo#26): GitHub's
      environment object carries a `deployment_branch_policy` field of its
      own (`{protected_branches, custom_branch_policies}`, mutually
      exclusive), and the `deployment-branch-policies` sub-resource only
      accepts entries when that field is set to `custom_branch_policies:
      true` -- the bodyless PUT `_ensure_environment` issues leaves it
      `null` (GitHub's default, meaning "no branch restriction," which is
      consistent with "no restriction at all" above but is a DIFFERENT
      fact from "custom policies enabled"). Attempting the branch-policy
      write this item requires against a freshly created or still-default
      environment would fail outright rather than silently doing nothing.
      Creating or confirming the `lanes` environment needs to set
      `deployment_branch_policy: {custom_branch_policies: true,
      protected_branches: false}` on the environment itself FIRST, then
      write the three (or ruleset-derived) branch-policy entries -- two
      writes in sequence, not one, both still before the credential lands.
      **"Restricted to the repository's default branch" is too narrow --
      it has to match every ref the `lanes` RULESET actually protects,
      not just the one branch** (Codex review, mikelward/repo#26):
      `rules.py`'s own hardened targeting (`_HARDENED_INCLUDE`,
      `rules.py:66`) covers `~DEFAULT_BRANCH`, `refs/heads/main`, AND
      `refs/heads/master` together, precisely so a leftover or
      accidentally-recreated `master` stays covered too. If the
      environment's branch policy names only the resolved default branch,
      a `pull_request_target` run whose base is `main` or `master` -- one
      of the OTHER two hardened targets, on a repo whose actual default
      is neither -- cannot read the credential and can never publish
      `lanes` there, permanently blocking merges to a branch the ruleset
      itself requires the check on. The branch policy needs to cover the
      same three targets the ruleset does -- but not by copying
      `_HARDENED_INCLUDE`'s three strings into the branch-policy API
      call verbatim, which was implied above and is wrong (Codex review,
      mikelward/repo#26): `~DEFAULT_BRANCH` and `refs/heads/main`/
      `refs/heads/master` are ruleset ref-CONDITION syntax, not the plain
      branch-name patterns the `deployment-branch-policies` endpoint
      matches against. Creating a literal policy entry for the string
      `~DEFAULT_BRANCH`, say, would not resolve to anything a real
      `pull_request_target` run's branch ever equals. The three policy
      entries have to be the resolved default branch's actual name (the
      same `default_branch(repo)` lookup this codebase already has)
      plus the literal names `main` and `master` -- not the ruleset's own
      encoding of those three targets.
      **That three-name set is only right for a FRESHLY CREATED ruleset --
      `rules.py`'s own `_compute_scope` (`rules.py:410-`) preserves an
      EXISTING ruleset's actual conditions on update instead of replacing
      them with `_HARDENED_INCLUDE`, and this item assumed the opposite**
      (Codex review, mikelward/repo#26): `_create_body` (`rules.py:527-`)
      does write the hardened three-ref set for a brand-new ruleset, but
      `_compute_scope` explicitly reads back `.conditions.ref_name` from
      the ruleset that already exists rather than overwriting it, which is
      precisely how `repo setup` avoids clobbering a repository that has
      genuinely widened its own ruleset -- to include a `release` branch,
      say. A branch policy hard-coded to default/`main`/`master` would
      then be wrong for that repository: `pull_request_target` runs
      targeting the branch the ruleset actually protects but this item
      doesn't know about can't read the credential and can never publish
      `lanes` there. The branch policy needs to be derived from the
      SAME conditions `_compute_scope` reads for the repository's actual
      ruleset -- the hardened three-ref set only when creating one fresh,
      the existing ruleset's own `ref_name.include` when updating one --
      not a fixed three-name assumption, and it needs to fail closed on a
      condition shape (a glob, an exclude, `~ALL`) it can't safely
      translate into a plain branch-name policy entry.
      **Covering those three is necessary but not sufficient -- the
      policy also has to contain NOTHING else, and "compliant" has to
      check that, not just that the three are present** (Codex review,
      mikelward/repo#26): a `lanes` environment whose policy is the
      correct three names PLUS a leftover `*` or some other extra entry
      still lets an untrusted branch select the environment and read the
      App private key, which is exactly what this whole branch-policy
      requirement exists to prevent -- three correct entries do not
      un-do a fourth permissive one. `repo audit`'s compliant state
      (below) needs to check the policy is EXACTLY that three-name set,
      not merely a superset of it, and `repo setup` needs to remove any
      entry outside it before -- or as part of -- placing the credential,
      the same direction as deleting a stray copy from the wrong
      environment.
      **The branch policy is not the only protection an environment can
      carry, and the others can block a publisher just as completely as a
      missing branch policy would** (Codex review, mikelward/repo#26): a
      `lanes` environment can also have required reviewers, a wait timer,
      or a custom deployment-protection rule (a separate GitHub Apps
      surface from the branch-policy endpoint this item has been about).
      Any of those can satisfy the exact three-name branch-policy check
      above while every `init`/`gate` run against it stalls waiting for an
      approval or an external decision that never comes -- a different
      failure mode than the forgery this section exists to close, but one
      that leaves the same symptom (pull requests waiting on a `lanes`
      status nothing posts), and a history-based preflight can look
      green in the meantime on the strength of an old status from before
      the protection was added. `repo audit`'s compliant predicate needs
      to also read the environment's protection rules and treat required
      reviewers, a wait timer, or a custom rule as `[FIX]`, not just an
      exact-or-not branch-policy set; `repo setup` needs to remove or
      explicitly refuse to touch an incompatible one before placing the
      credential, the same posture as the extra-branch-policy-entry case
      just above.
      **"The same environment GET response carries all three" was wrong
      for the custom-rule case -- a genuinely custom, App-backed
      deployment protection rule lives behind its OWN endpoint, not in
      the plain environment response this item already reads for the
      branch policy** (Codex review, mikelward/repo#26): the
      `GET /repos/{owner}/{repo}/environments/{env}` response's built-in
      `protection_rules` array does cover required reviewers and a wait
      timer, but an ENABLED custom deployment protection rule (a
      third-party or in-house App gating deployments) is exposed only
      through the separate `deployment_protection_rules` sub-resource
      (`GET .../environments/{env}/deployment_protection_rules`). Reading
      only the environment response and treating a `custom` entry there
      as sufficient would miss a genuinely custom rule, report the
      environment clean, and leave `init`/`gate` runs stalled on an
      approval this item's own audit never saw. Closing this needs a
      second read against that endpoint, with its own cost/latency/
      failure note alongside the environment-response one this section
      already carries -- fail closed the same way on an unavailable or
      incomplete read.
      **Widening the branch policy to all three targets does not by
      itself make all three branches' WORKFLOWS App publishers, and
      leaving that as a documented tool-model limitation was wrong -- this
      item's whole point is preventing a protected PR from waiting forever
      on a publisher that doesn't exist for it, and an unmigrated target
      branch is exactly that failure, not a different category of one**
      (Codex review, mikelward/repo#26, escalating an earlier round's own
      "worth naming, not fixing here" framing): a repository could
      genuinely have migrated only one of the ruleset's targeted branches
      (typically the default one) while another -- a real leftover
      `master`, say -- still carries the old plain-`pull_request`
      workflow. The credential being *readable* from all three refs says
      nothing about whether all three branches' `ci.yml` actually calls
      `mode: gate` with it; a single ruleset binding still applies
      uniformly across every branch it targets, so the unmigrated one's
      ordinary Actions check-run can never satisfy it either. `repo
      audit`/`repo setup` inspecting one branch at a time
      (`repo audit [--branch NAME]`) is a real constraint on how the check
      runs, but it doesn't excuse the binding step from the requirement:
      before writing `integration_id`, `repo setup` needs to resolve every
      concrete branch the ruleset's own conditions target (the same
      `_compute_scope` read the branch-policy item already uses) and run
      the full structural publisher check against each one, not just the
      branch named on the command line -- refusing to bind, not merely
      noting the gap, when any targeted branch can't be confirmed.
      **The same failure mode shows up through the trigger's OWN filters
      too, and it gets the same fix, not a separate "documented gap"** --
      confirming the event name is `pull_request_target` is not confirming
      it fires for every PR the ruleset protects** (Codex review,
      mikelward/repo#26, same escalation): a `branches:`, `paths:`, or
      narrowed `types:` filter on that trigger can exclude a protected PR
      the same way an unmigrated branch does, and a filter that DID match
      once is enough to satisfy the history-based preflight even though a
      differently-shaped PR then waits forever. Parsing every GitHub
      Actions filter shape correctly (branch globs, path globs,
      `branches-ignore`, the interaction between them) to PROVE coverage
      is real static-analysis work this item isn't taking on -- but
      accepting an unprovable filter as compliant, with a footnote, is not
      a safe substitute for that proof. The detector doesn't need to parse
      every filter shape to close this: it needs to require the trigger
      have NO narrowing filter beyond a safe default -- and "bare
      `pull_request_target:`" is NOT that safe default, which the
      previous revision of this item got backwards** (Codex review,
      mikelward/repo#26): a trigger with no `types:` at all fires only
      for GitHub's default activity types -- `opened`, `synchronize`,
      `reopened` -- which does not include `edited`. This item's own
      earlier rounds require `edited` to fire: it's the event a title
      edit or a retarget produces, and `init`/`gate` re-running on it is
      exactly what shrinks the stale-status window discussed there.
      `scaffold.py`'s own generated trigger (`scaffold.py:255`) lists
      `types: [opened, synchronize, reopened, edited]` for precisely this
      reason. A bare trigger structurally cannot re-run on a title edit or
      retarget at all, so accepting it as "safe" would silently defeat the
      init/gate re-run mechanism this section's own earlier items depend
      on. The detector needs an EXPLICIT `types:` list covering at least
      `opened`, `synchronize`, `reopened`, and `edited`, and fail closed --
      report "cannot confirm," `[FIX]` -- on a bare trigger, any
      `branches:`/`paths:` filter, or a `types:` list missing any of those
      four, rather than accepting the filter's presence (or absence) as a
      documented, tolerated gap.
      **This same gap is latent in the existing batch credentials too**
      (`NPM_UPDATE_APP_ID`/`GRADLE_UPDATE_APP_ID`/`RUST_UPDATE_APP_ID` and
      their private keys) -- `_ensure_environment` gives every one of them
      the same unrestricted-by-default environment. Lower severity there
      today (those Apps' installation tokens gate a reusable workflow
      whose own definition is pinned at `@main`, not read from the
      calling repo's branch), but worth its own follow-up rather than
      assuming it's fine because nothing has exploited it yet.
      **Cost and reliability of everything in this section** (Codex
      review, mikelward/repo#26, citing AGENTS.md's cost-and-reliability
      rule; revised once more below after the credential-placement item
      turned out to need a full environment sweep, not a fixed handful of
      calls): free, on the same GitHub REST API and the same
      5,000-authenticated-requests-an-hour limit `rules.py` and
      `apps.py`'s own module docstrings already budget against. Most of
      this section adds only a handful of fixed calls per repository (a
      branch-policy read and, when missing, a write) on top of what
      `repo setup`/`repo audit` already make -- except the App-membership
      read, which paginates with installation size (see below), and
      except the credential-placement item's environment sweep, which is
      one secrets-list call per environment the repository has, not a
      fixed count; still free and still well inside the rate limit for
      any repository with a realistic number of environments, but worth
      stating as "scales with environment count" rather than "a handful,"
      since a repository with an unusual number of environments is where
      that stops being negligible.
      **The environment sweep is not the only variable-cost operation
      here -- the organization-secret check below scales too (though only
      with the two secrets that matter here, per the correction on that
      item -- reading them by name rather than scanning every org secret
      is what keeps this bounded to at most two lookups plus pagination,
      not thousands), and this note still named the sweep as the sole
      one** (Codex review, mikelward/repo#26). A third read belongs here too: the separate
      `deployment_protection_rules` endpoint the custom-rule correction
      above needs is one more fixed call per repository (not variable,
      unlike the other two), alongside the environment response already
      read for the branch policy and the built-in reviewer/wait-timer
      checks.
      **A fourth operation scales too, and it's the one this section is
      most directly about: reconciling the branch policy to EXACTLY the
      resolved set is a list, then one delete per stale entry and one
      create per missing entry, not the single read-and-write this
      estimate implied** (Codex review, mikelward/repo#26): the
      `deployment-branch-policies` API has no bulk "set this exact list"
      call, so making a drifted policy match -- removing a leftover `*`,
      adding a missing branch name -- means a list call plus one API call
      per entry that has to change. That scales with the ruleset's own
      scope and however much the existing policy has drifted from it, the
      same shape of cost as the environment sweep and the org-secret
      check, for the same reason: a per-item reconciliation this codebase
      hasn't needed before.
      **A fifth operation was budgeted as "a handful of calls" when it
      isn't fixed either: the App-membership read paginates over the
      installation's own repository list** (Codex review, mikelward/repo#26):
      `plan_app_step` (`apps.py:143-144`) calls
      `user/installations/{id}/repositories --paginate` -- for a
      "selected" installation covering many repositories, that's however
      many pages the installation has, not a single call, however the
      earlier framing of this estimate had it. Its latency, rate-limit
      usage, and failure behavior belong alongside the other four variable-
      cost operations, not folded into "a handful" the way it was.
      All five belong in this estimate together, and all fail
      closed the same way: an unavailable or incomplete read refuses the
      step rather than reporting a clean sweep it couldn't actually
      confirm. Interactive, not scheduled either way,
      so a slow or failed call is a visible error for the person running
      the command, not a silent gap -- and per this section's own
      fail-closed posture throughout, a failed branch-policy, membership,
      or environment-sweep read must refuse the affected step (report
      `[FIX]`/error, same as a failed ruleset or credential read does
      today) rather than proceed as if the policy, membership, or sweep
      were confirmed clean.
- [ ] **`repo audit` should report which design a repo is actually wired
      for, and flag drift the same way it flags a stray batch credential.**
      Four states: plain `pull_request` with no App secrets present AND
      the ruleset's `lanes` entry unbound (no `integration_id`) -- the
      accepted baseline, not a finding; `pull_request_target` with both
      secrets correctly in the `lanes` environment (whose branch policy is
      EXACTLY the set the repository's actual ruleset scope resolves to --
      the hardened three-ref set for a freshly created ruleset, or the
      existing ruleset's own conditions when one was already there, per
      `_compute_scope` -- and nothing more; see the branch-policy item's
      own note on this) AND the environment carries NO other protection
      (no required reviewers, no wait timer, no custom deployment
      protection rule -- see the protection-rules item's own note) AND NO
      organization-scoped copy of either secret reaching the repository
      (see the credential-placement item's own note on this -- fail
      closed, "cannot confirm," when that read isn't available, rather
      than silently treating it as clean), the lanes App still covering
      the repository AND its installation neither suspended
      (`suspended_at` null) nor missing the Statuses API write permission
      (see the installation-health item's own note), AND the ruleset's
      `lanes` entry bound to THAT App's own id specifically (compliant);
      `pull_request_target` with the secrets missing, repository-scoped,
      in the wrong environment, OR reachable via an inherited organization
      secret, the environment's branch policy missing an entry, carrying
      an extra one, OR the environment carrying a required-reviewer/
      wait-timer/custom protection rule, the App no longer covering the
      repository, its installation suspended, OR its permissions no
      longer including the Statuses API write, OR the ruleset entry
      unbound or bound to a different App's id (broken, `[FIX]`); and
      plain `pull_request` with `LANES_APP_ID`/`LANES_APP_PRIVATE_KEY`
      present anywhere -- a dead credential, since a workflow that never
      passes `app-id`/`app-private-key` to `mode: gate` never uses them,
      the same "stale copy for a workflow the repository does not use"
      treatment `credentials.py` already gives a batch credential.
      **The App-membership half is required, not optional** (Codex
      review, mikelward/repo#26): a repository that had the App removed
      after a compliant `repo setup` run still has correctly-placed
      secrets, but the installation-token exchange those secrets feed can
      no longer succeed, so no legitimate `lanes` status can be published
      at all -- reading the secret placement alone reports it compliant
      while its publisher is actually dead.
      **Covering the repository is not the same as being ABLE TO PUBLISH
      to it -- an installation can stay a member while suspended, or
      while its granted permissions no longer include the Statuses API
      write lanes needs** (Codex review, mikelward/repo#26): GitHub lets
      an organization admin suspend an App installation (`suspended_at`
      on the installation object) without removing it from the account,
      and lets them downgrade what it's granted at any time -- either way
      `plan_app_step` can still return `ALREADY_MEMBER`/`ALREADY_ALL`,
      since membership and suspension/permissions are different fields on
      the same object, and neither is what that function currently reads.
      A stale App-attributed status from before the suspension or the
      permission downgrade can keep the history-based preflight looking
      fine in the meantime. `repo audit`'s compliant predicate needs to
      also read the installation's `suspended_at` (must be null) and its
      `permissions` map (must still grant the Statuses API write) as part
      of the App-membership check, not just that the repo appears on a
      member/all-repos list.
      **"Bound" isn't enough either -- it has to be bound to THE LANES
      APP specifically, not merely bound to something** (Codex review,
      mikelward/repo#26): an earlier revision of this item's compliant
      state only checked that the ruleset entry had a non-null
      `integration_id` at all, which a `lanes` entry bound to an unrelated
      App would also satisfy -- and if that other App had ever posted a
      `lanes` status too, the generic history read would pass it as
      genuinely satisfied, recreating the exact forged-publisher gap this
      whole design exists to close, just moved from "no App" to "the
      wrong App." `repo audit` needs to resolve the lanes App's own
      numeric id and compare the ruleset entry's `integration_id` against
      that specific value, not just check that one is present.
      **That id is NOT what the App-membership lookup returns, and
      reusing it as if it were would compare the wrong number** (Codex
      review, mikelward/repo#26): `apps.resolve_installation` resolves
      `.installations[].id` -- the INSTALLATION's own id, which is what
      `apps.py`'s existing membership machinery needs and calls
      `install_id` throughout. A ruleset's `integration_id` identifies
      the App itself, a different number in a different namespace.
      Comparing an installation id against an `integration_id` would make
      every correctly bound repository read as broken (the numbers never
      match), and using the installation id to WRITE the binding would
      activate a required check restricted to an id nothing can ever
      satisfy. The same installations listing `resolve_installation`
      already queries carries the App's own id too, on the installation
      object's `app_id` field -- retain that field alongside `install_id`
      rather than reusing `install_id` for both purposes.
      **The plain-`pull_request` baseline needs the inverse check too, for
      a repository ROLLED BACK off the App design** (Codex review,
      mikelward/repo#26): remove the App secrets without also unbinding
      the ruleset's `lanes` entry (the `integration_id` write the item
      below adds), and the repository is left with no App secrets --
      reading as the accepted baseline under the original three-state
      version of this item -- while the required check is still
      restricted to a publisher nothing can reach any more. An ordinary
      Actions check-run can never satisfy a bound entry, so every future
      merge blocks, and a history-based read (the same class of read
      flagged above for a deleted workflow) can keep looking fine for a
      while on the strength of the App's last, now-stale, status. Plain
      `pull_request` with no App secrets is therefore only the accepted
      baseline when the ruleset entry is ALSO unbound; plain
      `pull_request` with a still-bound entry (secrets present or not) is
      broken, `[FIX]`, and its fix needs an unbind operation the
      `integration_id` item below does not actually define yet -- "applied
      in reverse" was hand-waved in an earlier revision of this item, and
      there is no such reverse today: `_build_update_body`'s merge keeps
      an EXISTING entry's `integration_id` exactly as it was for any
      context it already recognizes by name, and a bare `--rule lanes`
      currently means "no opinion, leave whatever's there alone," not
      "make sure this is unbound." (Codex review, mikelward/repo#26.) The
      `integration_id` item's `--rule NAME@APP_ID`-shaped surface needs an
      explicit unbind form too -- `--rule NAME` alone changing meaning to
      "ensure unbound," or a distinct `--rule NAME@` / `--unbind NAME`
      shape -- and `rules.py`'s writer needs to actually drop a
      pre-existing `integration_id` for that request rather than
      preserving it by default.
      **Fixing that `[FIX]` has its own ordering requirement, and it runs
      OPPOSITE to the migration direction the binding item below settles
      on** (Codex review, mikelward/repo#26): repairing a rollback means
      the credential-placement step deletes the now-unused App secrets
      (per its "delete a stray/repository-level copy" behavior) while the
      ruleset item unbinds `lanes`. If the unbind write fails after the
      secrets are already gone, or simply runs second and its own failure
      is swallowed the way every `setup_cmd.run` step's failure already
      is, the repository is left with an App-bound required check and no
      credential anywhere that could ever satisfy it -- blocking every
      merge until a second run fixes it, which is worse than the drifted
      state being repaired. So for a rollback repair specifically, the
      unbind has to succeed BEFORE the App credential is removed -- the
      reverse of the "bind last, after the credential and membership are
      confirmed" ordering the binding item below requires for a
      migration. `repo setup` needs to tell the two directions apart (is
      this run adding a binding or removing one) and sequence its ruleset
      and credential steps accordingly, not apply one fixed order to both.
      **"Covering the repository" is deliberately not "a selected member"
      -- that was wrong in an earlier revision of this item, and so was
      the function it cited** (Codex review, mikelward/repo#26, twice, in
      the same round): `repo_lib/apps.py`'s `resolve_installation`
      (lines 65-104) only resolves the installation's id and its
      `repository_selection` ("selected" vs. "all") -- it never checks
      whether THIS repo is in a selected installation's member list, so
      citing it as "the membership check" was itself wrong. The actual
      per-repo check is `plan_app_step` (lines 122-158), which calls
      `resolve_installation` and then either lists the selected
      installation's repositories and looks for a match, or -- when the
      installation covers "all repositories" -- confirms the repo exists
      and reports `ALREADY_ALL` without needing a membership list at all.
      That second case is a real, fully-covered outcome, not a narrower
      one: `apps.apply_step` already treats it as a no-op success, same
      as `ALREADY_MEMBER`. Requiring "selected" specifically, as an
      earlier revision of this item did, would report `[FIX]` on a
      repository an all-repositories install already protects -- a
      `[FIX]` `repo setup --app` could never clear, since `apply_step`
      correctly does nothing for `ALREADY_ALL` in the first place.
      `repo audit` needs `plan_app_step`'s actual verdict (`ALREADY_MEMBER`
      or `ALREADY_ALL`, not just "selected and present"), not a fresh
      reimplementation of it and not `resolve_installation` alone -- and
      not an inference from a past `repo setup` run having succeeded,
      since the ruleset's own `codex`/`lanes` check-history read stays
      green regardless of current membership, GitHub keeps reporting the
      LAST status that publisher posted, however long ago.
      **None of the above actually confirms the private key WORKS -- only
      that something with the right name sits in the right place** (Codex
      review, mikelward/repo#26): a `LANES_APP_PRIVATE_KEY` that has been
      revoked, rotated on GitHub's side without updating the secret, or
      paired with the wrong `LANES_APP_ID`, still reads as "present, in
      the right environment, right shape" to every check above -- and a
      stale App-attributed status from before the key broke can still
      clear the history-based preflight, so `repo audit` would report
      compliant right up until a real pull request needs a fresh
      installation-token exchange that can no longer succeed. Confirming
      the credential actually authenticates needs a live token exchange
      -- signing a JWT with the private key and calling GitHub's
      installation-token endpoint -- which this tool doesn't do anywhere
      today; `apps.py`'s existing membership machinery reads installation
      listings, it doesn't mint a token from a private key. That's real
      build work, not a detector tweak, so it's named here as a boundary
      rather than folded into the compliant predicate: "compliant" means
      the credential is correctly placed and shaped, not that it has been
      verified to still authenticate, and `repo setup` should not treat
      an already-present credential as "already correct" under a
      stronger check until that token-exchange verification exists to
      back it -- until then, this is a known gap the section names rather
      than silently claims to close.
      **Signing that JWT has no supported path under this repository's own
      no-third-party-dependencies rule, and "build work, not a detector
      tweak" glossed over that this item genuinely can't specify a plan
      without resolving it** (Codex review, mikelward/repo#26): RS256
      signing needs an RSA private-key operation, and Python's standard
      library has nothing that does it -- `hashlib`/`hmac` cover hashing
      and symmetric MACs, not asymmetric signing. That leaves two real
      options, both a genuine tradeoff this item can't wave past: shell out
      to `openssl` (a new external-binary dependency, needing this repo's
      own cost-and-reliability note the same as any other new dependency),
      or add a third-party crypto package (a policy exception AGENTS.md
      says is "a conversation about the tradeoff, not a quiet `pip
      install`," and explicitly not something to resolve by adding one
      quietly). This item should stop describing token-exchange
      verification as future work with an implied implementation and
      instead name it as an open decision -- which of the two paths, with
      its cost/reliability note -- that has to be made before any of it is
      buildable, not merely before it's built.
      **Even once an implementation exists, `repo audit` (and `repo setup`
      run without `--credential`) can NEVER perform this validation against
      an already-placed secret -- GitHub's secret-read API returns names
      only, never values, so there is no private-key bytes to sign a JWT
      with** (Codex review, mikelward/repo#26): this item's revised text
      talks about "signing a JWT with the private key" as if reading the
      existing `LANES_APP_PRIVATE_KEY` secret and using its value were an
      implementation detail to fill in -- it isn't reachable at all.
      `GET /repos/{owner}/{repo}/actions/secrets/{name}` (and the
      environment-scoped equivalent) returns only the name and timestamps;
      the value is write-only by design, the same reason `credentials.py`
      and `secrets_cmd.py` never read one back anywhere in this codebase
      today. The only place this tool ever sees a credential's actual
      value is a `--credential NAME=value` invocation, at the moment the
      operator supplies it for placement. So real validation can only ever
      happen there: `repo setup --credential LANES_APP_ID=...
      --credential LANES_APP_PRIVATE_KEY=...` signs and exchanges the
      JWT with the freshly-supplied pair before placing it, and carries
      that successful exchange forward as what makes the placement (and
      later the binding) trustworthy. `repo audit`, and `repo setup` run
      without `--credential` against an already-placed secret, can never
      confirm authenticity this way -- the compliant predicate needs to
      say so plainly ("credential shape and placement confirmed;
      authenticity cannot be confirmed without resupplying it") rather
      than implying a future audit-only check that the platform makes
      impossible.
      **Out of scope for this item, on purpose: a repository whose lanes
      workflow has been removed from the default branch entirely, with no
      App secrets left either** (Codex review, mikelward/repo#26). None of
      the four states above name that repository, because it isn't
      running either design -- it has no lanes gate producing anything at
      all, which is a general "required check nothing currently produces"
      problem this tool already has a place for (`rules.never_reported`/
      `describe_missing`, exercised in `repo audit`'s own required-check
      section), not something specific to distinguishing the App design
      from the default one. Worth naming precisely, though, since that
      existing check's own `_collect_reported` walks the default branch
      head, then recent open and closed pull requests, for whether a
      context has EVER reported -- which can find an old success from
      before the workflow was deleted and read the check as fine, not as
      currently missing its producer. That is a real, general gap in an
      existing check, not a new one this item's four states should try to
      absorb -- its own follow-up, if it turns out to matter in practice.
- [ ] **`repo setup --credential LANES_APP_ID=... --credential
      LANES_APP_PRIVATE_KEY=...`** should place them like a batch
      credential does today: fan into the `lanes` environment for a repo
      whose workflow already references them (per the detector above),
      delete a repository-level copy, leave an unaffected repo alone --
      but NOT via `_ensure_environment` unmodified; the branch-policy
      item above has to run first (or be folded into the same step) so
      the environment this writes into is actually restricted before the
      secret lands in it.
      **"Like a batch credential does today" undersells what this
      actually needs -- the batch pattern doesn't clean up a copy sitting
      in some OTHER, wrong environment, and the audit item above already
      promises to catch exactly that** (Codex review, mikelward/repo#26):
      `setup_cmd._plan_credentials` (`setup_cmd.py:317-`) only ever reads
      `repository_secrets` and `environment_secrets` for each credential's
      OWN designated environment (`npm-update`, `gradle-update`,
      `rust-update`, `ci-commit-artifact`) -- it has never scanned every
      environment on the repo for a stray copy elsewhere, because no
      batch credential's audit state names "wrong environment" as
      something it detects. This item's audit state does. So placing
      `LANES_APP_ID`/`LANES_APP_PRIVATE_KEY` needs new work the batch
      pattern doesn't already do: enumerate every environment on the
      repo (`credentials.environments`), and once the correct, policy-
      protected `lanes` environment holds a usable credential, delete any
      copy found in a DIFFERENT environment too, not just a repository-
      level one -- otherwise a stale private key keeps sitting under
      whatever (possibly unrestricted) environment it was in, and the
      `[FIX]` the audit reports for it can never actually clear. That
      also changes the cost estimate below: a full environment sweep is
      one secrets-list call per environment on the repo, not the fixed
      handful this section currently budgets, so update that note too
      rather than leaving it describing only the narrower batch-style
      check.
      **The sweep above still can't see an ORGANIZATION-level secret
      granted to the repository, and that's a real hole on an org-owned
      repo, not just an unlikely corner** (Codex review, mikelward/repo#26):
      an org Actions secret named `LANES_APP_ID`/`LANES_APP_PRIVATE_KEY`
      and shared with the repository is invisible to
      `repository_secrets`/`environment_secrets` alike, so a same-repo PR
      could add an ordinary job -- no `environment: lanes` needed -- that
      reads the inherited copy and forges the status, while this item's
      own audit reports the repository compliant the whole time. Closing
      it needs reading the org's secret grants -- but only for the two
      names that matter here, not a scan of every org secret (Codex
      review, mikelward/repo#26: enumerating all of an org's Actions
      secrets and checking each selected-visibility one's repo grants
      could be thousands of unrelated paginated calls in a large org, when
      only `LANES_APP_ID` and `LANES_APP_PRIVATE_KEY` can possibly affect
      this check). `GET /orgs/{org}/actions/secrets/{secret_name}` reads
      each of those two BY NAME directly -- a 404 means that name isn't
      granted at the org level at all, nothing further to do for it; a
      200 means branching on that secret's own `visibility` field, not
      assuming "selected" (Codex review, mikelward/repo#26: an earlier
      revision of this fix only handled the "selected" case, silently
      passing a repository that inherits a secret set to `all` or
      `private` -- `visibility: all` reaches every repository in the org
      with no per-repo grant list to check at all, and `visibility:
      private` reaches every PRIVATE repository in the org the same way,
      so either would let the check read "not granted" while the
      repository actually inherits the credential). The three cases: `all`
      -- reachable, no further read needed, report as a hit; `private` --
      reachable if and only if the audited repository's own visibility is
      private (a fact this tool's repository read already carries);
      `selected` -- paginate the selected-repository grants and check
      whether this repository is among them, same as before. At most two
      lookups plus pagination for whichever of those two secrets actually
      exists and is `selected`, not an org-wide sweep. This is a
      genuinely different permission footprint than anything else this
      tool reads -- every other call here is repository-scoped; this one
      needs organization-level Actions-secrets visibility, which a
      fine-grained token may not carry even when it can do everything else
      `repo setup`/`repo audit` do. Fail closed (report "cannot confirm no
      org secret is granted") when that read isn't available, rather than
      silently skipping it. **This is also latent in every
      existing batch credential**, for the identical reason the
      environment branch-policy gap above is: nothing here has ever
      checked for an inherited org secret, for any of them. Worth its own
      follow-up across the whole tool rather than a one-off built only for
      lanes.
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
      `codex`, which is App-published via a Checks-API check run, and
      whose App attribution the read side already resolves). Without a
      write path, a repo running the App design still has a ruleset that
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
      **The read side does NOT already understand lanes' actual
      publishing mechanism, and this is the most consequential correction
      in this whole item -- everything above assumed the wrong thing about
      it** (Codex review, mikelward/repo#26, P1): `codex`'s App attribution
      works because Codex publishes via a Checks-API check run, and
      `_collect_reported` (`rules.py:186-278`) only ever populates
      `app_pairs` -- the set `_entry_satisfied` checks a bound entry
      against -- from check runs' `.app.id` field (`rules.py:207-222`).
      Its commit-status scan, right below that in the same function
      (`rules.py:224-239`), reads ONLY `.statuses[].context` and drops
      everything else, `creator` included. Lanes does not publish a check
      run at all -- per its own TODO.md ("round eight"), `mode: gate`'s
      publishing half calls `repos.createCommitStatus` directly, the
      legacy Statuses API. So the instant `lanes` is bound to an
      `integration_id`, `_entry_satisfied` can NEVER find a
      `(context, integration_id)` pair for it, no matter how many times
      the real App posts the real status -- `never_reported` reports it
      as never having reported from the right App even on a repository
      that is working exactly as designed, which fails `apply_ruleset`'s
      own preflight (a check it refuses to require until proven it can
      report) and would make `repo audit`'s new compliant state
      (immediately above) unreachable by construction. Closing this needs
      `_collect_reported`'s status scan extended to also populate
      `app_pairs` from something on each status entry that identifies its
      App -- and that "something" is itself unconfirmed, not a trivial
      swap: a commit status's `creator` is a bot-user-shaped object
      (`login`, `id`, `type`), and whether that `id` equals the App's own
      `integration_id` the way a check run's `.app.id` does, or names a
      different numbering GitHub uses for the App's bot identity, has to
      be confirmed against a real status GitHub App has actually posted
      before this is built, not assumed from the check-run shape working
      that way.
      **This has to bind LAST, after every publisher prerequisite is
      confirmed in place -- not reuse `setup_cmd.run`'s current step
      order** (Codex review, mikelward/repo#26, real and worth catching
      before implementation rather than after: `rules.apply_ruleset`
      runs at `setup_cmd.py:1220`, before the `--secret`/environment loop,
      the App-membership step (`apps.apply_step`, `:1288`), and the
      fleet-credential moves (`:1292`) -- and every step still runs
      regardless of an earlier one's failure, by this module's own
      design. Binding `integration_id` at the ruleset step's current
      position, unchanged, means a single `repo setup` run that both
      migrates a repo onto the App design AND flips the binding on could
      activate an App-restricted required check before that run's own
      later steps have placed the credential or confirmed App membership
      -- or, if either of those later steps fails, leave the binding
      active with no working publisher at all, blocking every merge
      until a second run fixes it. The binding step needs the App
      confirmed a member, the environment's branch policy confirmed, and
      the credential confirmed placed (or already correct from an
      earlier run) as its own preconditions, checked immediately before
      the write -- which likely means moving it to run after the
      App-membership and credential steps, not merely adding it to the
      existing ruleset call.
      **"Bind last" fixes the ordering WITHIN one `repo setup` run, but the
      detector's own read can still be stale by the time the write
      actually happens -- the audited branch is live, and nothing stops it
      changing during the membership/credential steps this ordering now
      runs first** (Codex review, mikelward/repo#26): the detector reads
      the workflow once, at the start of the run; the App-membership and
      credential-placement steps that now run before the binding write can
      each take real time (an API round trip, in the worst case a retry).
      If a push to the audited branch removes or breaks `init`/`gate`
      during that window, the binding step still proceeds on the strength
      of a detection that's no longer true, activating an App-restricted
      required check with no current publisher -- the exact failure mode
      "bind last" was meant to prevent, just moved from a same-run ordering
      bug to a cross-run race. The binding step needs to re-read the
      audited branch's current tip and rerun the full structural publisher
      check immediately before the ruleset write -- not reuse the
      run-start detection -- and refuse the write if either the branch tip
      or the structural verdict has changed since.
      **That revalidation only covered the audited branch, but the
      compliant predicate above requires every ruleset-targeted branch to
      have a working publisher -- the two items say different things about
      the same write** (Codex review, mikelward/repo#26): if another
      targeted branch (not the one named on the command line) loses its
      publisher during the membership/credential window -- or the ruleset
      itself gains a new target while that window runs -- the audited
      branch alone can still read as unchanged, and this revalidation as
      written would let the write through across a scope that's now wrong
      or broken elsewhere. The pre-write revalidation needs to re-resolve
      the ruleset's CURRENT scope (the same `_compute_scope` read, run
      again, not reused from earlier in the same command) and rerun the
      full structural check against every branch it now names, not just
      the audited one -- refusing the write if any of them fails, changed,
      or the scope itself has changed since the run-start detection.
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
      actually pilots a consumer onto this."
      **"References no `secrets.*`" is necessary but not sufficient for
      the ambient `GITHUB_TOKEN`, which is available to a
      `pull_request_target` job whether or not anything names it** (Codex
      review, mikelward/repo#26): the whole reason this design needs
      `pull_request_target` at all is that it runs with base-branch
      permissions against attacker-controlled PR content, and two things
      leak that token into reach of that content by default. First,
      `permissions:` defaults to whatever the repository or organization
      grants `GITHUB_TOKEN` (which can be write access) unless the
      workflow or job explicitly narrows it -- checking for an explicit
      `secrets.GITHUB_TOKEN`/`secrets.*` reference says nothing about the
      ambient token every step already has via `github.token` /
      `GITHUB_TOKEN`, with no `secrets.` prefix to grep for. Second,
      `actions/checkout` persists that token into the local git
      credential store by default (`persist-credentials: true`), so any
      code that then runs in the checked-out merge snapshot -- exactly
      the untrusted content this design exists to run safely -- can push
      with it unless the step sets `persist-credentials: false`. Neither
      is specific to lanes' own `init`/`gate` steps; both are properties
      of the *heavy job* being migrated onto `pull_request_target`, which
      is the actual per-repo migration work this item is about. The
      migration checklist needs an explicit least-privilege `permissions:`
      block (job- or workflow-level) and `persist-credentials: false` on
      every `pull_request_target` checkout of PR content, not just a scan
      for named secrets. `repo setup`/`repo audit`
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
