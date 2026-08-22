# TODO

## repo setup

- [ ] **Port `repo-rules`'s ruleset composition from mikelward/scripts**
      (`repo setup` is still the "not yet implemented" stub — README
      "Status"). When the port lands, build in the hardened branch
      targeting from the start rather than porting the old shape: the
      ruleset's conditions include `~DEFAULT_BRANCH`, `refs/heads/main`,
      AND `refs/heads/master`, so a branch carrying the deprecated name
      can't slip past protection (mikelward/web's default branch was
      renamed master → main while its workflows kept pushing to the dead
      name — the incident this closes); and warn when the repo has an
      actual `master` branch, since a lingering deprecated-name branch is
      exactly that backdoor surface. mikelward/scripts' `repo-rules` /
      `repo-rules-audit` are gaining the same behavior — match their
      semantics, in this repo's Python idiom.
