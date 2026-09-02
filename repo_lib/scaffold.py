"""The scaffold step of `repo create --scaffold` -- push the fleet's
standard CI files as one atomic initial commit to a repository that has
none yet.

Splits into what's mechanically safe to generate and what genuinely needs
project knowledge, and only ever does the former:

- codex-review's three workflow files are pinned byte for byte against
  `mikelward/codex-review`'s own `templates/` (see that repository's own
  AGENTS.md); fetched live from there rather than vendored here, so a
  scaffolded repo always gets the current template, not whatever was
  hardcoded when this module was written.
- `zizmor.yml` has no equivalent shared hub -- every consumer carries its
  own copy, hand-pinned to a zizmor release (see AGENTS.md's own finding on
  this) -- so this fetches the copy from `mikelward/lanes`, whose own
  header comment self-identifies as the pilot, as the least-bad canonical
  source.
- `.github/zizmor.yml` (the exceptions policy) and `.github/lanes.conf` are
  generated here directly: both are mechanical once you know which fleet
  files are present (codex-review's pair legitimately use
  `pull_request_target`; the docs-lane split is the same in every consumer
  that hasn't customized it).
- `ci.yml` is generated too, but only the classify+gate wiring
  `mikelward/lanes`'s own README documents verbatim -- the actual project
  jobs (tests, a build) are NOT something this can know, so a trivial
  always-passing placeholder job is wired into the gate instead of
  guessing at what "the project's real jobs" are. This is a deliberate
  choice over shipping `ci.yml` with an empty `needs:`/`results:` list:
  that shape is not documented anywhere as supported, and a required
  check that turns out not to work is exactly the trap `lanes` exists to
  prevent. Delete the placeholder once a real job exists.

Cost and reliability: two extra `gh api` reads (mikelward/codex-review,
mikelward/lanes -- both this account's own public repos) beyond repo
creation itself, then a bootstrap write plus one blob per file, one tree,
one commit, and one ref update to land the two commits (see
push_initial_commit's own docstring for why it's two, not one). Free,
inside the same rate limit as everything else this tool does. The one new
failure mode is those two source repositories being unreachable or having
moved their files -- reported like any other gh failure, and it fails the
scaffold step without touching the (already-created) empty repository
otherwise.
"""

import base64
import json

from repo_lib import gh
from repo_lib.common import error, error_lines

TEMPLATE_REPO = "mikelward/codex-review"
TEMPLATE_FILES = ("codex-review.yml", "codex-review-check.yml", "codex-review-listener.yml")
ZIZMOR_SOURCE_REPO = "mikelward/lanes"

_ZIZMOR_POLICY = """\
# Policy for zizmor; the workflow beside this file runs it.
#
# The defaults demand hash pins everywhere and refuse pull_request_target
# outright. Both collide with decisions this fleet makes deliberately, so
# the exceptions live here, scoped as narrowly as the tool allows -- a NEW
# workflow reaching for pull_request_target is still flagged, because only
# the named files below are excused.
rules:
  unpinned-uses:
    config:
      policies:
        # `@main` IS the release for these sibling actions: this fleet
        # tracks them deliberately, there is no release step on their
        # side, and a pin would be a thing to bump here on every one of
        # their merges.
        "mikelward/codex-review": ref-pin
        "mikelward/codex-review/.github/workflows/check-consumer.yml": ref-pin
        "mikelward/lanes": ref-pin
        # Official actions may pin to a tag, as every workflow here does.
        "actions/*": ref-pin
        # Everything else must pin to a hash.
        "*": hash-pin
  dangerous-triggers:
    # Load-bearing pull_request_target usage in the codex-review pair,
    # reasoned through in mikelward/codex-review's own docs/CONSUMER.md:
    # neither checks out or executes pull request code with the elevated
    # token.
    ignore:
      - codex-review.yml
      - codex-review-check.yml
"""

_LANES_CONF = """\
# Ordered: the FIRST matching rule wins, and anything matching no rule is code.
#
# Starting policy for a fresh repo -- root markdown and the docs/ tree ride
# the docs lane, everything else is code. Narrow this as the project's real
# layout emerges (see any sibling repo's own .github/lanes.conf for examples
# of a tighter code/docs split).
docs *.md
docs docs/**/*.md

# The subject prefixes a commit on the docs lane must carry -- only the two
# every fleet AGENTS.md documents for a docs-only diff.
prefixes docs todo

# The pull request title is never linted. Squash merging is disabled on
# every repository in this fleet, so a title never becomes a commit
# subject; only the commit prefixes above decide what may ride the docs
# lane.
lint-title no
"""

_CI_YML = """\
name: CI

on:
  push:
    branches: [{default_branch}]
  pull_request:
    types: [opened, synchronize, reopened, edited]

permissions:
  contents: read
  pull-requests: read

jobs:
  classify:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    outputs:
      docs_only: ${{{{ steps.lane.outputs.docs_only }}}}
      base_sha: ${{{{ steps.lane.outputs.base_sha }}}}
    steps:
      - uses: actions/checkout@v5
        with:
          persist-credentials: false
      - uses: mikelward/lanes@main
        id: lane
        with:
          mode: classify
          pr: ${{{{ github.event.pull_request.number }}}}

  # TODO: replace this with the project's real jobs (test, build, lint,
  # ...), each carrying `needs: classify` and
  # `if: needs.classify.outputs.docs_only != 'true'`. Add each job's name
  # to the `lanes` job's own `needs:` and `results:` below, and delete this
  # placeholder once a real job exists -- see mikelward/lanes's README for
  # the documented shape.
  placeholder:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    needs: classify
    if: needs.classify.outputs.docs_only != 'true'
    steps:
      - run: echo "no real jobs yet -- see the TODO in this file"

  lanes:
    name: lanes
    runs-on: ubuntu-latest
    timeout-minutes: 5
    needs: [classify, placeholder]
    if: always()
    steps:
      - uses: actions/checkout@v5
        with:
          persist-credentials: false
      - uses: mikelward/lanes@main
        with:
          mode: gate
          pr: ${{{{ github.event.pull_request.number }}}}
          classify-result: ${{{{ needs.classify.result }}}}
          base-sha: ${{{{ needs.classify.outputs.base_sha }}}}
          results: placeholder=${{{{ needs.placeholder.result }}}}
"""

_TODO_MD = """\
# TODO

## Set up

- [ ] Replace the placeholder job in `.github/workflows/ci.yml` with this
      project's real jobs (tests, a build, lint -- whatever applies), each
      carrying `needs: classify` and
      `if: needs.classify.outputs.docs_only != 'true'`. Add each job's
      name to the `lanes` job's own `needs:` and `results:`, then delete
      the placeholder.
"""

_INITIAL_COMMIT_MESSAGE = """\
Add the fleet's standard CI scaffold

codex-review's three workflow files (pinned byte for byte against
mikelward/codex-review's own templates/), zizmor.yml (from
mikelward/lanes, its self-identified pilot), and the exceptions/policy
files each needs: .github/zizmor.yml and .github/lanes.conf.

ci.yml wires the lanes classify+gate job pair per mikelward/lanes's own
README, with a placeholder job standing in for this project's real jobs
until they exist -- see the TODO in ci.yml.
"""


def _fetch_file(repo, path, ref="main"):
    """The decoded text content of `path` on `repo`@`ref`, or None (with
    the failure already reported) if the read fails."""
    try:
        encoded = gh.run(
            ["api", f"repos/{repo}/contents/{path}?ref={ref}", "--jq", ".content"]
        ).strip()
    except gh.GhError as e:
        error_lines(f"could not fetch {repo}@{ref}:{path} for the scaffold:", e.stderr)
        return None
    try:
        return base64.b64decode(encoded).decode()
    except (ValueError, UnicodeDecodeError) as e:
        error(f"could not decode {repo}@{ref}:{path}: {e}")
        return None


def _branches_line(text, default_branch):
    """A fetched workflow's own `branches: [main]` re-pointed at this
    repository's real default branch, when it isn't literally "main" --
    every fleet source this scaffolds from assumes "main". The
    replacement is quoted (`branches: ["name"]`) even though the sources
    never quote their own "main": git branch names may contain YAML
    flow-syntax characters a bare scalar would misparse -- a comma reads
    as a second branch, an unbalanced brace breaks the sequence outright
    (Codex review, mikelward/repo#14) -- and json.dumps gives a correctly
    escaped YAML-safe double-quoted string for any of them.

    Returns None (with the failure reported) if `default_branch` isn't
    "main" and the literal text this rewrites isn't there to find: a
    fetched source that ever re-spells its own filter (quotes it, block-
    style, whatever) would otherwise make replace() a silent no-op,
    landing a workflow still filtered to "main" while the real default
    branch is something else -- wrong, and no different in kind from any
    other push: branches: filter mismatch this scaffold exists to avoid
    (Codex review, mikelward/repo#14). Failing the whole build is what
    "reported" means here, matching every other build_scaffold_files
    failure: nothing gets pushed rather than something silently broken."""
    if default_branch == "main":
        return text
    if "branches: [main]" not in text:
        error(
            "a fetched workflow no longer spells its push filter literally as "
            "'branches: [main]', so it can't be re-pointed at this repository's "
            f"real default branch ('{default_branch}'). Refusing to push a "
            "workflow that would stay filtered to main."
        )
        return None
    return text.replace("branches: [main]", f"branches: [{json.dumps(default_branch)}]")


def build_scaffold_files(default_branch):
    """The scaffold's files as {path: content}, or None (with the failure
    already reported) if fetching either template source fails, or if
    zizmor.yml's push filter can't be safely re-pointed at
    `default_branch` (see _branches_line). Pure -- makes no write of its
    own, so a caller can build this before deciding anything is safe to
    push."""
    files = {}
    for name in TEMPLATE_FILES:
        # No _branches_line rewrite here: none of codex-review's three
        # templates has a branches:-filtered push trigger at all --
        # verified against the real templates, not assumed -- so passing
        # them through a rewrite meant for zizmor.yml's own `on: push:
        # branches: [main]` would (after the fail-loud fix below) refuse
        # every non-"main" scaffold outright, for files that were never
        # wrong in the first place.
        content = _fetch_file(TEMPLATE_REPO, f"templates/{name}")
        if content is None:
            return None
        files[f".github/workflows/{name}"] = content

    zizmor_workflow = _fetch_file(ZIZMOR_SOURCE_REPO, ".github/workflows/zizmor.yml")
    if zizmor_workflow is None:
        return None
    zizmor_rewritten = _branches_line(zizmor_workflow, default_branch)
    if zizmor_rewritten is None:
        return None
    files[".github/workflows/zizmor.yml"] = zizmor_rewritten

    files[".github/zizmor.yml"] = _ZIZMOR_POLICY
    files[".github/lanes.conf"] = _LANES_CONF
    # Quoted for the same reason _branches_line quotes its own
    # replacement: a default branch name can contain YAML flow-syntax
    # characters a bare scalar would misparse.
    files[".github/workflows/ci.yml"] = _CI_YML.format(default_branch=json.dumps(default_branch))
    files["TODO.md"] = _TODO_MD
    return files


def push_initial_commit(repo, default_branch, files):
    """Writes `files` to `repo`, whose default branch has no commits (and
    so no ref) yet, as two commits: a bootstrap commit of one arbitrary
    file, then a second carrying the complete tree.

    Two, not the one this originally shipped with: GitHub's Git Data API
    refuses to create a ref in a repository with no commits at all --
    "Create a reference" is explicit that a branchless repo cannot receive
    one even when the commit object already exists (Codex review,
    mikelward/repo#14) -- so blob/tree/commit/ref-create cannot be the
    FIRST write here no matter how it's sequenced; it needs an existing
    commit to build on. The Contents API's create-file endpoint is the one
    write GitHub allows against a genuinely empty repository, and creates
    the branch itself as a side effect -- so this bootstraps with it,
    using the first file (sorted by path) as an arbitrary seed, then
    supersedes it with a second commit -- built the normal Git Data API
    way -- carrying every file (the bootstrap one included, so its content
    isn't silently missing from the final tree) and parented on the
    bootstrap commit. `git/refs` (create) becomes `git/refs/heads/{branch}`
    (update) for that second write, a plain fast-forward.

    Returns True on success; a failure partway through is reported and
    leaves the branch pointed at whatever the last successful write left
    it at -- the bootstrap commit alone, never something partial or
    unreferenced.

    Re-checks immediately before the bootstrap write that the branch is
    still empty. `repo setup`'s bootstrap step can call this well after
    plan_gaps first saw an empty branch -- across the combined-plan
    confirmation prompt, which (like every other confirm-then-apply step
    in setup_cmd.py) can sit for an arbitrary length of real time -- and
    the tree this builds afterward has no `base_tree`, only the scaffold
    files. Without the recheck, a real initial commit someone else pushed
    in that window would still let the Contents-API PUT below succeed (it
    happily adds a new file to an existing branch, empty or not) and the
    scaffold-only commit that follows it would still fast-forward cleanly
    on top of it, silently discarding every file that commit added --
    `repo create --scaffold`'s own call site has no such window (it runs
    immediately after creating a repository nobody else can have reached
    yet) but this function no longer only serves that caller (Codex
    review, mikelward/repo#14)."""
    ok, ref_raw = gh.try_run(["api", f"repos/{repo}/git/refs/heads/{default_branch}"])
    if ok:
        error(
            f"{repo}'s '{default_branch}' branch now has commits -- someone pushed to it since "
            "the plan was built. Refusing to bootstrap over what's there; rerun to gap-fill it "
            "properly instead."
        )
        return False
    if "HTTP 404" not in ref_raw:
        error_lines(f"could not re-check {repo}'s '{default_branch}' branch before bootstrapping:", ref_raw)
        return False

    ordered = sorted(files.items())
    bootstrap_path, bootstrap_content = ordered[0]
    try:
        raw = gh.run_with_input(
            ["api", "--method", "PUT", f"repos/{repo}/contents/{bootstrap_path}", "--input", "-"],
            json.dumps(
                {
                    "message": f"Bootstrap {default_branch}",
                    "content": base64.b64encode(bootstrap_content.encode()).decode(),
                    "branch": default_branch,
                }
            ).encode(),
        )
    except gh.GhError as e:
        error_lines(f"could not create the bootstrap commit on {repo}:", e.stderr)
        return False
    bootstrap_commit_sha = json.loads(raw)["commit"]["sha"]

    tree_entries = []
    for path, content in ordered:
        try:
            raw = gh.run_with_input(
                ["api", "--method", "POST", f"repos/{repo}/git/blobs", "--input", "-"],
                json.dumps({"content": content, "encoding": "utf-8"}).encode(),
            )
        except gh.GhError as e:
            error_lines(f"could not create a blob for {path}:", e.stderr)
            return False
        blob_sha = json.loads(raw)["sha"]
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})

    try:
        raw = gh.run_with_input(
            ["api", "--method", "POST", f"repos/{repo}/git/trees", "--input", "-"],
            json.dumps({"tree": tree_entries}).encode(),
        )
    except gh.GhError as e:
        error_lines(f"could not create the scaffold's tree on {repo}:", e.stderr)
        return False
    tree_sha = json.loads(raw)["sha"]

    try:
        raw = gh.run_with_input(
            ["api", "--method", "POST", f"repos/{repo}/git/commits", "--input", "-"],
            json.dumps(
                {
                    "message": _INITIAL_COMMIT_MESSAGE,
                    "tree": tree_sha,
                    "parents": [bootstrap_commit_sha],
                }
            ).encode(),
        )
    except gh.GhError as e:
        error_lines(f"could not create the scaffold's commit on {repo}:", e.stderr)
        return False
    commit_sha = json.loads(raw)["sha"]

    try:
        gh.run_with_input(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/git/refs/heads/{default_branch}",
                "--input",
                "-",
            ],
            json.dumps({"sha": commit_sha}).encode(),
        )
    except gh.GhError as e:
        error_lines(
            f"could not fast-forward {default_branch} to the scaffold's commit on {repo}:", e.stderr
        )
        return False

    return True
