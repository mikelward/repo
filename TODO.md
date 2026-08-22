# TODO

## repo setup

- [x] Port `repo-rules`'s ruleset composition from mikelward/scripts, with
      the hardened branch targeting built in from the start rather than
      the old single-target shape: a ruleset this creates targets
      `~DEFAULT_BRANCH`, `refs/heads/main`, AND `refs/heads/master`
      together (`repo_lib/rules.py`, exercised via `repo setup`), and
      `check_master_branch` warns on stderr — without failing the run —
      when the repo has an actual `master` branch. Ported: create vs.
      update, the ownership check (refuses to overwrite a same-named
      ruleset holding an unmanaged rule type), the never-reported-check
      guard, the merge-method conflict scan against a repo's other active
      rulesets, and the confirm/re-validate-before-write flow.

- [ ] **Port repo-setup's other two steps: fanning a secret out (via the
      logic `repo secrets` already has) and ensuring GitHub App
      installation membership.** `repo setup --secret`/`--app` currently
      refuse outright ("not yet implemented") rather than doing anything.
      The App-installation step has no sibling Python implementation to
      port from yet — see mikelward/scripts's repo-setup for the
      `/user/installations` API shape (unverified against GitHub's own
      docs when that script was written; a classic PAT is its documented
      fallback if a fine-grained one is refused there).

## Decisions needing review

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
