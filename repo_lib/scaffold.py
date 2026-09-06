"""The scaffold step of `repo create --scaffold` and `repo setup`'s own
always-on bootstrap step -- the fleet's standard CI files, as one atomic
commit: pushed straight to a repository that has none yet (`repo create`,
push_initial_commit), or, for whatever's still missing from one that
already has content, opened as a pull request against its default branch
(`repo setup`, plan_gaps/apply_gaps/open_gap_pull_request).

A pull request rather than a direct push for the gap-fill, because the
files being added are the CI itself: `lanes` and `zizmor` run on the pull
request, so opening one is what first makes them report, and a check that
has never reported is one a ruleset cannot require (rules.never_reported).
It is also the only write a branch an earlier run already protected will
accept at all. `codex` is the one this can't reach: its status-writing
workflow runs under `pull_request_target`, taken from the BASE branch's
copy, so it reports from the first pull request opened after this one
merges, not on this one.

The gap-filling half never overwrites: a scaffold file `repo setup` finds
already present is left exactly as it is, differences from the fleet's own
copy included -- reconciling those is a human decision (the drift might be
a deliberate, project-specific customization), not something this tool
silently corrects. Only a path genuinely absent gets added.

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
- `AGENTS.md` is the fleet's shared agent conventions, fetched from
  `mikelward/conf`'s `agents/AGENTS.md` -- the one maintained copy, rather
  than a second one here kept in step by hand, which is the arrangement
  this whole repository exists to replace. A generated header goes on top
  carrying the two things no template can know (what the project is, and
  what a contributor runs) as explicit TODOs. It carries the shared rules
  rather than deferring to a user-level file, because a freshly created
  repository is otherwise the one place in this fleet an agent works with
  no conventions loaded at all -- and a remote session does not
  necessarily load the user-level copy.
- `CLAUDE.md` is generated: a one-line `@AGENTS.md` import, which is what
  `mikelward/conf`'s own CLAUDE.md does. Not a symlink, though most of the
  fleet uses one there: the whole file map here is plain blobs, and
  push_initial_commit's bootstrap goes through the Contents API, which
  cannot write a symlink at all. A repository that already has the symlink
  keeps it -- see SYMLINK_IS_PRESENT_PATHS.
- `ci.yml` is generated too, but only the classify+gate wiring
  `mikelward/lanes`'s own README documents verbatim -- the actual project
  jobs (tests, a build) are NOT something this can know, so a trivial
  always-passing placeholder job is wired into the gate instead of
  guessing at what "the project's real jobs" are. This is a deliberate
  choice over shipping `ci.yml` with an empty `needs:`/`results:` list:
  that shape is not documented anywhere as supported, and a required
  check that turns out not to work is exactly the trap `lanes` exists to
  prevent. Delete the placeholder once a real job exists.

Cost and reliability: six `gh api` reads against mikelward/codex-review,
mikelward/lanes and mikelward/conf -- all three this account's own public
repos -- to build the scaffold's file set, on EVERY call (one of the six
resolves
codex-review's `main` to a single commit sha so its three template files
come from one revision rather than each independently resolving a `main`
that could move between them -- see _resolve_commit_sha): `repo create
--scaffold` makes them once per new repository, but `repo setup`'s
bootstrap step being always on (see setup_cmd.py) means every `repo
setup` invocation pays them too, even one that finds nothing missing,
since plan_gaps has no way to know the answer without building the
scaffold to compare against.
Three more reads against the target repository itself (its branch ref,
that commit, its current tree) price the comparison, plus one listing of
its open pull requests -- made only where something is actually missing,
so an already-complete repository never pays it -- to find a scaffold pull
request an earlier run left open rather than opening a second one beside
it. What that pull request contains is deliberately never read: it is
mutable by anyone at any moment, so nothing derived from it stays true,
and the step reports it rather than reasoning about it. All of this is
free, inside the same rate limit as everything else this tool does, but
it is the one step here whose read cost scales with two OTHER
repositories' availability rather than only the target's -- reported like
any other gh failure, and it fails the bootstrap/scaffold step alone
without touching anything else `repo setup`'s other steps did. Applying
the result, when there is one, costs one repeat of that pull-request
listing (the plan's answer is a snapshot, and a combined `repo setup` plan
waits on a confirmation that takes as long as it takes), one blob per
missing file, one tree, one commit, one branch create, and the pull
request itself (a repository with no commits yet pays two commits and no
pull request instead -- see push_initial_commit's own docstring for why).
One more read, only when something missing is under .github/workflows/:
_missing_workflow_scope checks this gh token's own OAuth scopes before
attempting the write at all (see its own docstring, mikelward/repo#18).
"""

import base64
import json
import re
import urllib.parse
from dataclasses import dataclass, field

from repo_lib import gh
from repo_lib.common import error, error_lines

# A prior version of this module retried a git-data CREATE call (blob/
# tree/commit) on a 404, on the theory that GitHub's git-data backend
# could take a short while to catch up right after a repository's first
# content landed. That theory turned out to be wrong: the actual, only
# real-world cause found (mikelward/repo#18) was a gh token missing the
# `workflow` OAuth scope, which makes a tree-create referencing a
# `.github/workflows/*` path 404 -- every time, indefinitely, regardless
# of how long a caller waits, since it's a permission wall rather than a
# timing window. _missing_workflow_scope below checks for that specific,
# confirmed cause up front instead, so this fails in milliseconds with an
# actionable message rather than after 62 seconds of pointless retrying
# into the same unhelpful "Not Found".


def _missing_workflow_scope(missing_paths):
    """True if `missing_paths` includes anything under .github/workflows/
    AND this gh token's own OAuth scopes are known and don't include
    `workflow` -- the one confirmed cause of a scaffold tree-create
    404ing no matter how long a caller waits (mikelward/repo#18: blob-
    create, which carries no path, always succeeded; tree-create, the
    first point a path gets attached to content, 404'd every time, on
    two separate days, for a token whose scopes were `gist, read:org,
    repo` -- no `workflow`).

    False (not blocking) when gh.token_scopes() can't tell -- a
    fine-grained PAT or GitHub App token carries no OAuth scopes at all,
    so "can't tell" must not read as "missing"; only an explicit absence
    in a real scope list is grounds to skip the write outright."""
    if not any(path.startswith(".github/workflows/") for path in missing_paths):
        return False
    scopes = gh.token_scopes()
    return scopes is not None and "workflow" not in scopes


TEMPLATE_REPO = "mikelward/codex-review"
TEMPLATE_FILES = ("codex-review.yml", "codex-review-check.yml", "codex-review-listener.yml")
ZIZMOR_SOURCE_REPO = "mikelward/lanes"
# The fleet's shared agent conventions, and where a scaffolded repository
# gets its own copy from. One maintained file rather than a second copy
# kept in step by hand -- which is the arrangement this whole repository
# exists to replace. Tracked at `main` like zizmor.yml, not pinned to a
# resolved sha like TEMPLATE_FILES: those are pinned because they are a
# SET that has to come from one revision, and this is a single file with
# nothing to be consistent with.
CONVENTIONS_REPO = "mikelward/conf"
CONVENTIONS_PATH = "agents/AGENTS.md"
# Paths where a symlink counts as the scaffold content already being
# present, rather than as something a new blob would silently replace.
# Only CLAUDE.md: pointing it at AGENTS.md is what most of this fleet
# already does, and it is the same content by another route. Everywhere
# else a symlink stays an occupied path -- a symlink at ci.yml could point
# anywhere, and refusing is right there.
SYMLINK_IS_PRESENT_PATHS = frozenset({"CLAUDE.md"})

# A `branches: [main]` YAML mapping entry as a whole line -- not a
# commented-out one, since `#` would sit in the leading-whitespace-only
# group this requires. Matched per-line (not re.MULTILINE across the
# whole text) so _branches_main_line_indices can check each match's own
# ancestor lines.
_BRANCHES_MAIN_LINE_RE = re.compile(r"^([ \t]*)branches:[ \t]*\[main\][ \t]*$")
# A bare `push:` YAML mapping key as a whole line.
_PUSH_KEY_LINE_RE = re.compile(r"^[ \t]*push:[ \t]*$")
# A bare `on:` YAML mapping key as a whole line.
_ON_KEY_LINE_RE = re.compile(r"^[ \t]*on:[ \t]*$")


def _indentation(line):
    return len(line) - len(line.lstrip(" \t"))


def _branches_main_line_indices(lines):
    """Indices into `lines` of every `branches: [main]` whole line whose
    parent, grandparent, and grandparent's OWN indentation -- by simple
    YAML block-indentation nesting, not a real parse -- are `push:`,
    `on:`, and column 0 respectively: the exact shape zizmor.yml's own
    top-level trigger has. Each ancestry check narrows out one class of
    lookalike a looser match could rewrite by mistake while the real
    trigger stays pointed at "main": the line's text alone can't tell it
    apart from an unrelated key also named "branches" (a job's matrix
    entry); a push: parent alone can't tell it apart from some OTHER
    push: mapping; and an on: grandparent alone can't tell it apart from
    an on:/push:/branches: shape nested somewhere that isn't the
    document's actual top-level trigger (Codex review, mikelward/repo#14).
    Still not a real YAML parse in general."""
    stack = []  # (indent, line) of every currently-open ancestor
    indices = []
    for i, line in enumerate(lines):
        if line.strip() == "":
            continue
        indent = _indentation(line)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if (
            _BRANCHES_MAIN_LINE_RE.match(line)
            and len(stack) >= 2
            and _PUSH_KEY_LINE_RE.match(stack[-1][1])
            and stack[-2][0] == 0
            and _ON_KEY_LINE_RE.match(stack[-2][1])
        ):
            indices.append(i)
        stack.append((indent, line))
    return indices


def _branch_ref_path(default_branch):
    """`heads/{branch}`, safe to splice into a `git/ref/...` (read) or
    `git/refs/...` (create/update/delete) URL path: a
    branch name can carry `#`, `?`, `&`, or a space, and sent raw the
    request would misparse or 404 -- the same class of bug
    credentials._ref_suffix exists to avoid for a query-string ref (Codex,
    mikelward/repo#13). Different encoding from that helper on purpose: an
    embedded `/` is left alone (`safe="/"`) rather than escaped to `%2F`,
    because GitHub's git-data-api ref endpoints expect a multi-segment
    branch name (`release/1.0`) as literal path segments -- matching how
    git itself names the ref (`refs/heads/release/1.0`) -- not as one
    opaque token the way a query-string value is (Codex review,
    mikelward/repo#14)."""
    return "heads/" + urllib.parse.quote(default_branch, safe="/")


def _ref_missing_commits(ref_raw):
    """True if `ref_raw` -- the error text from a failed
    `git/ref/heads/{branch}` read -- means the branch simply doesn't
    exist yet, safe to treat as "no commits on this branch": an HTTP 404
    (this particular ref was never created), or GitHub's specific HTTP 409
    "Git Repository is empty" (the whole repository has zero git objects
    yet, the state right after `repo create`). Matching on that exact
    message, not bare "HTTP 409", matters: a 409 can mean other kinds of
    conflict against a branch that does have commits, and misreading one
    of those as "empty" would let a caller bootstrap straight onto it
    (Codex review, mikelward/repo#14)."""
    return "HTTP 404" in ref_raw or ("HTTP 409" in ref_raw and "Git Repository is empty" in ref_raw)


# Prepended to the fetched conventions. Everything a scaffolded repository
# has to say for itself is a TODO here, because none of it is derivable:
# what the project is, and what a contributor actually runs. The shared
# conventions follow, so a repository nobody has written a word about yet
# still gives an agent the rules the rest of the fleet works under.
_AGENTS_HEADER = """\
# AGENTS.md

Conventions for AI agents working in this repository.

`CLAUDE.md` imports this file, so every agent reads the same conventions.
Edit `AGENTS.md`.

## What this repository is

TODO: one paragraph -- what it does, what it is built with, and what a
reader has to know before changing anything. Name the normative documents
(`SPEC.md`, `TODO.md`, `README.md`) if there are any.

## Building and testing

TODO: the commands a contributor actually runs, and which one of them to
run before every commit. `.github/workflows/ci.yml` ships with a
placeholder job standing in for this project's real ones -- replacing it
and describing it here are the same task.

## Everything below

The rest of this file was copied from this account's shared conventions
(`mikelward/conf`, `agents/AGENTS.md`) when the repository was scaffolded,
so it says what the rest of the fleet already does. It is this
repository's copy now: edit it where this project genuinely differs,
rather than treating it as something that syncs.

Keep the whole file as short as it can be and still work. Every session
loads it whole, so each rule costs context on every turn: add one the
first time something bites, say it once in the fewest words that carry the
*why*, rewrite or trim an existing rule rather than appending beside it,
and delete one that has stopped biting.

"""

# Claude Code reads CLAUDE.md, not AGENTS.md, so a scaffolded repository
# needs both. An import rather than a symlink: it is what mikelward/conf's
# own CLAUDE.md does, and the scaffold's file map is plain blobs
# throughout -- push_initial_commit's bootstrap goes through the Contents
# API, which cannot write a symlink at all. A repository that already has
# the symlink keeps it (see SYMLINK_IS_PRESENT_PATHS).
_CLAUDE_MD = "@AGENTS.md\n"

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


def _resolve_commit_sha(repo, ref="main"):
    """The commit sha `repo`@`ref` currently points at, or None (with the
    failure already reported) if the read fails. Used to pin a set of
    RELATED file fetches to the one commit this resolves to, rather than
    each fetch resolving the mutable branch name independently -- if the
    source repo's `main` advances between two of those fetches, files
    resolved independently could come from two different pushes, so the
    scaffold this builds wouldn't correspond to any revision that source
    repo ever actually had (Codex review, mikelward/repo#14)."""
    try:
        return gh.run(["api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"]).strip()
    except gh.GhError as e:
        error_lines(f"could not resolve {repo}@{ref} to a commit for the scaffold:", e.stderr)
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
    "main" and _branches_main_line_indices doesn't find exactly one
    branches: [main] line parented under on: push: -- a re-spelled filter
    leaves nothing to match, and more than one match means which one is
    real can't be told apart (see that function's own docstring for why
    ancestry, not just the line's text, is what decides a match). Failing
    the whole build is what "reported" means here, matching every other
    build_scaffold_files failure: nothing gets pushed rather than
    something silently broken."""
    if default_branch == "main":
        return text
    lines = text.split("\n")
    indices = _branches_main_line_indices(lines)
    if len(indices) != 1:
        error(
            "a fetched workflow's push filter isn't a single, unambiguous "
            "'branches: [main]' line directly under push:, so it can't be safely re-pointed at "
            f"this repository's real default branch ('{default_branch}'). Refusing to push a "
            "workflow whose real push filter this can't confidently identify."
        )
        return None
    i = indices[0]
    line = lines[i]
    indent = line[: _indentation(line)]
    lines[i] = f"{indent}branches: [{json.dumps(default_branch)}]"
    return "\n".join(lines)


def build_scaffold_files(default_branch):
    """The scaffold's files as {path: content}, or None (with the failure
    already reported) if fetching either template source fails, or if
    zizmor.yml's push filter can't be safely re-pointed at
    `default_branch` (see _branches_line). Pure -- makes no write of its
    own, so a caller can build this before deciding anything is safe to
    push."""
    files = {}
    # The three template files are pinned byte for byte against each other
    # as a SET (see this module's own docstring), so they're fetched at one
    # resolved commit sha rather than each resolving TEMPLATE_REPO's mutable
    # `main` independently -- if `main` advances between two of those three
    # fetches, resolving it per-fetch could hand back files from two
    # different pushes, which is a scaffold that never corresponded to any
    # revision codex-review actually shipped (Codex review,
    # mikelward/repo#14).
    template_sha = _resolve_commit_sha(TEMPLATE_REPO)
    if template_sha is None:
        return None
    for name in TEMPLATE_FILES:
        # No _branches_line rewrite here: none of codex-review's three
        # templates has a branches:-filtered push trigger at all --
        # verified against the real templates, not assumed -- so passing
        # them through a rewrite meant for zizmor.yml's own `on: push:
        # branches: [main]` would (after the fail-loud fix below) refuse
        # every non-"main" scaffold outright, for files that were never
        # wrong in the first place.
        content = _fetch_file(TEMPLATE_REPO, f"templates/{name}", ref=template_sha)
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

    conventions = _fetch_file(CONVENTIONS_REPO, CONVENTIONS_PATH)
    if conventions is None:
        return None
    files["AGENTS.md"] = _AGENTS_HEADER + conventions
    files["CLAUDE.md"] = _CLAUDE_MD

    files[".github/zizmor.yml"] = _ZIZMOR_POLICY
    files[".github/lanes.conf"] = _LANES_CONF
    # Quoted for the same reason _branches_line quotes its own
    # replacement: a default branch name can contain YAML flow-syntax
    # characters a bare scalar would misparse.
    files[".github/workflows/ci.yml"] = _CI_YML.format(default_branch=json.dumps(default_branch))
    return files


def push_initial_commit(repo, default_branch, files):
    """Writes `files` to `repo`, whose default branch has no commits (and
    so no ref) yet, as two commits: a bootstrap commit of one arbitrary
    file (the first, sorted by path), then a second carrying the complete
    tree, parented on the bootstrap commit.

    Two commits because GitHub's Git Data API refuses to create a ref in a
    repository with no commits at all, even with the commit object already
    built -- so blob/tree/commit/ref-create can't be the first write here.
    The Contents API's create-file endpoint is the one write GitHub allows
    against a genuinely empty repository, and creates the branch as a side
    effect, so this bootstraps with it and supersedes the seed with the
    normal Git Data API commit; `git/refs` (create) becomes
    `git/refs/heads/{branch}` (update) for that second write.

    Returns the resulting commit's sha on success. A failure partway
    through is reported (returns None) and leaves the branch pointed at
    whatever the last successful write left it at -- the bootstrap commit
    alone, never something partial or unreferenced.

    Re-checks immediately before the bootstrap write that the branch is
    still empty: a caller can reach this well after plan_gaps first saw an
    empty branch (across `repo setup`'s own confirmation wait), and
    without the recheck a real initial commit landing in that window would
    still let the Contents-API PUT succeed, then get silently discarded
    when the base_tree-less scaffold commit fast-forwards over it (Codex
    review, mikelward/repo#14)."""
    # git/ref/... (singular) is GitHub's documented read; git/refs/...
    # (plural) is create/update/delete only and has no GET route at all --
    # every read call in this module used the plural form until this was
    # caught, which meant every one of them 404'd against a populated
    # branch and mistook it for empty (Codex review, mikelward/repo#14).
    ok, ref_raw = gh.try_run(["api", f"repos/{repo}/git/ref/{_branch_ref_path(default_branch)}"])
    if ok:
        error(
            f"{repo}'s '{default_branch}' branch now has commits -- someone pushed to it since "
            "the plan was built. Refusing to bootstrap over what's there; rerun to gap-fill it "
            "properly instead."
        )
        return None
    if not _ref_missing_commits(ref_raw):
        error_lines(f"could not re-check {repo}'s '{default_branch}' branch before bootstrapping:", ref_raw)
        return None

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
        return None
    bootstrap_commit = json.loads(raw)["commit"]
    bootstrap_commit_sha = bootstrap_commit["sha"]
    if bootstrap_commit.get("parents"):
        # The precheck above and this PUT are two separate requests, so
        # someone could still have pushed a real first commit to the
        # branch in the window between them -- the Contents API doesn't
        # require an empty branch, so the PUT itself would have succeeded
        # right on top of theirs rather than failing. The commit it
        # returns is the only place that window's outcome is visible: a
        # parent means the branch wasn't empty when this write actually
        # landed, whatever the precheck saw moments earlier. Stop here,
        # before the tree below (which has no base_tree) would fast-
        # forward over their content -- the bootstrap file itself is
        # already safely on the branch, nothing lost, nothing else
        # written (Codex review, mikelward/repo#14).
        error(
            f"{repo}'s '{default_branch}' branch already had a commit by the time the bootstrap "
            f"write landed -- someone pushed to it in the same window this was checking. The "
            f"bootstrap file ({bootstrap_path}) is on the branch now; nothing else was written. "
            "Rerun to gap-fill the rest safely."
        )
        return None

    tree_entries = []
    for path, content in ordered:
        try:
            raw = gh.run_with_input(
                ["api", "--method", "POST", f"repos/{repo}/git/blobs", "--input", "-"],
                json.dumps({"content": content, "encoding": "utf-8"}).encode(),
            )
        except gh.GhError as e:
            error_lines(f"could not create a blob for {path}:", e.stderr)
            return None
        blob_sha = json.loads(raw)["sha"]
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})

    try:
        raw = gh.run_with_input(
            ["api", "--method", "POST", f"repos/{repo}/git/trees", "--input", "-"],
            json.dumps({"tree": tree_entries}).encode(),
        )
    except gh.GhError as e:
        error_lines(f"could not create the scaffold's tree on {repo}:", e.stderr)
        return None
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
        return None
    commit_sha = json.loads(raw)["sha"]

    try:
        gh.run_with_input(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/git/refs/{_branch_ref_path(default_branch)}",
                "--input",
                "-",
            ],
            json.dumps({"sha": commit_sha}).encode(),
        )
    except gh.GhError as e:
        error_lines(
            f"could not fast-forward {default_branch} to the scaffold's commit on {repo}:", e.stderr
        )
        return None

    return commit_sha


# Where the bootstrap step's pull request puts its commit. A fixed prefix
# so a LATER run recognizes a scaffold pull request an earlier one opened:
# `repo setup` runs unattended across a fleet, and a step that cannot see
# its own earlier pull request opens a second one every time it runs. The
# commit's own sha goes on the end so two runs never collide on one ref --
# a rerun before the first pull request merged builds a different commit
# and gets a different branch.
GAP_BRANCH_PREFIX = "repo-setup/fleet-ci-scaffold"

_GAP_SUBJECT = "Add missing fleet CI scaffold files"

_GAP_PULL_REQUEST_BODY = """\
Opened by `repo setup`: the fleet's standard CI scaffold files this
repository was missing.

{files}

Added through a pull request rather than pushed to the default branch so
that the checks they install actually run: `lanes` and `zizmor` report on
this pull request itself, which is what lets a required-checks ruleset
name them. `codex` cannot report here -- its status-writing workflow runs
under `pull_request_target`, which GitHub takes from the BASE branch's
copy, so the pull request adding that workflow is the one pull request it
cannot run on; it starts reporting on the first pull request opened after
this one merges.

Nothing already in the repository was touched: only paths that were
absent are added.
"""


@dataclass
class GapPullRequest:
    """A pull request adding the scaffold's missing files -- the one this
    run opened, or (`opened` False) one an earlier run left open.

    Deliberately carries nothing about what that pull request CONTAINS.
    An earlier version read its file list to decide whether the gap still
    let a required check report, and that read was wrong in a new way ten
    times over one review: a deleted path counted as coverage, a
    renamed-away path invisible, an answer cached across the confirmation
    wait, a head force-pushed after the read, a branch edited into
    something this step never opened, a pull request closed or retargeted
    while its diff still read the same. A pull request is mutable by
    anyone at any moment, so no answer derived from one stays true;
    nothing here derives one now (Codex review, mikelward/repo#42)."""

    number: int
    url: str
    head_branch: str
    opened: bool = True


@dataclass
class GapOutcome:
    """What apply_gaps did. `error` means it failed (already reported).
    Otherwise exactly one of the other two is set: `branch_sha` is the
    default branch's freshly verified tip when the scaffold is ON it
    (nothing was missing, or a branch with no commits was bootstrapped),
    and `pull_request` is where the scaffold is instead waiting when it
    went through a pull request -- in which case the branch itself is
    still unscaffolded, which is what a caller about to activate
    pull-request protection has to know."""

    error: bool = False
    branch_sha: str = None
    pull_request: GapPullRequest = None


def _gap_branch(ref):
    """True if `ref` is a branch name this step would have created."""
    return ref == GAP_BRANCH_PREFIX or ref.startswith(GAP_BRANCH_PREFIX + "-")


def find_open_gap_pull_request(repo, default_branch):
    """(ok, GapPullRequest or None): the scaffold pull request an earlier
    run left open against `default_branch`, if there is one.

    ok is False -- with the failure already reported -- when the read
    itself failed. Fail closed: "could not tell" must not read as "there
    is none", which would open a second pull request beside the first.

    "In this repository" is asked of GitHub as `head.repo.id ==
    base.repo.id` rather than compared against `repo` here: `repo` is
    whatever the caller typed, and GitHub answers to a name in any casing
    and to a name it has since renamed away from, so an exact string
    comparison against the canonical `full_name` would read this tool's
    own open pull request as a fork's and open a second one beside it
    (Codex review, mikelward/repo#42). The base repository IS the one
    just listed from, so comparing the two ids settles same-repo against
    fork with nothing to normalize. A head whose fork was deleted answers
    null, which is not the base's id either."""
    ok, raw = gh.try_run(
        [
            "api",
            "--paginate",
            f"repos/{repo}/pulls?state=open&per_page=100&base={urllib.parse.quote(default_branch)}",
            "--jq",
            r'.[] | "\(.number) \(.head.ref) \(.head.repo.id == .base.repo.id) \(.html_url)"',
        ]
    )
    if not ok:
        error_lines(f"could not list {repo}'s open pull requests:", raw)
        return False, None
    for line in raw.splitlines():
        parts = line.split(" ")
        if len(parts) != 4:
            continue
        number, head_ref, same_repo, url = parts
        # Only a branch in this repository: a fork's branch can be named
        # anything at all, and one that happens to carry this prefix is
        # not a pull request this tool opened.
        if same_repo != "true" or not _gap_branch(head_ref):
            continue
        try:
            number = int(number)
        except ValueError:
            continue
        return True, GapPullRequest(number=number, url=url, head_branch=head_ref, opened=False)
    return True, None


# Which scaffold file publishes each check this fleet requires, and where
# GitHub reads that workflow from when it runs for a pull request. `head`
# is the pull request's own copy, `base` the default branch's -- so a pull
# request adding `ci.yml` does run it, while `codex`'s publisher, running
# under `pull_request_target`, is taken from the base branch either way.
#
# Both halves are acted on, by the two predicates below: the file decides
# whether the BRANCH can publish a check, the head/base half whether a
# pull request could publish it instead. Kept here, beside the generator
# that produces these very files, because this is the one place it can be
# checked against reality.
CHECK_PUBLISHERS = {
    "lanes": (".github/workflows/ci.yml", "head"),
    "zizmor": (".github/workflows/zizmor.yml", "head"),
    "codex": (".github/workflows/codex-review-check.yml", "base"),
}


def checks_a_gap_leaves_unpublished(missing, checks):
    """Which of `checks` the branch has no publisher for, because their
    publishing workflow is among `missing`.

    Asked of the BRANCH, never of a pending pull request. A pull request
    adding `ci.yml` does run `ci.yml`, so `lanes` could in principle
    report on it -- but establishing that means reading a pull request
    anyone may change at any moment, and every answer read out of one has
    to be re-established at every later step to stay true. The cost of not
    asking is a ruleset write deferred until that pull request merges, on
    a repository whose gap contains a publisher, and the run already says
    to merge it. The cost of asking was ten findings in one review (Codex
    review, mikelward/repo#42). A gap containing no publisher at all --
    only `AGENTS.md`, say, which is the common case across this fleet --
    defers nothing either way."""
    return [
        check for check in checks if CHECK_PUBLISHERS.get(check, (None, None))[0] in missing
    ]


def checks_no_pull_request_can_report(missing, checks):
    """Which of `checks` no pull request can rescue, because GitHub reads
    their publishing workflow from the BASE branch -- where `missing` says
    it is absent -- rather than from the pull request's own head.

    Narrower than checks_a_gap_leaves_unpublished, and narrower on
    purpose. That one asks whether the branch can publish a check today,
    which is what decides whether to defer a ruleset write; a `lanes`
    requirement is answered yes-it-defers there because the branch really
    has no ci.yml. This one asks whether the repository is WEDGED, and a
    head-published check does not wedge it: the pull request carrying
    ci.yml runs ci.yml, so `lanes` reports and the pull request merges on
    its own (Codex review, mikelward/repo#42).

    Whether one particular pending pull request carries that workflow is
    the question this module refuses to ask -- so this stays silent about
    head-published checks rather than guessing, on the same grounds as an
    App-bound requirement: an advisory that names a wedge that isn't one
    sends someone looking for a bypass actor they don't need."""
    return [
        check
        for check in checks
        if CHECK_PUBLISHERS.get(check, (None, None))[0] in missing
        and CHECK_PUBLISHERS[check][1] == "base"
    ]


def _docs_lane_only(paths):
    """True if every path rides the docs lane under the lanes.conf this
    scaffold itself writes (root markdown, plus docs/**/*.md). The gate
    fails a docs-only diff whose commit subject carries no docs prefix, so
    a pull request adding only AGENTS.md and CLAUDE.md needs one. A
    repository that has narrowed its own lanes.conf can still disagree
    with this reading -- that shows up as a failing check on the pull
    request, which is a reviewable thing, rather than as a wrong file
    landing."""
    return bool(paths) and all(
        path.endswith(".md") and ("/" not in path or path.startswith("docs/")) for path in paths
    )


def _gap_commit_message(missing):
    subject = f"docs: {_GAP_SUBJECT}" if _docs_lane_only(missing) else _GAP_SUBJECT
    return subject + "\n\n" + "\n".join(f"- {path}" for path in sorted(missing))


@dataclass
class GapPlan:
    """What `repo setup`'s bootstrap step found on an already-existing
    repository, and what it would do about it. `error` means the read
    failed (already reported); `present`/`missing` are only meaningful
    when it's False. `base_commit_sha`/`base_tree_sha` are None when the
    branch has no commits yet -- plan_gaps still builds `missing` as every
    scaffold file in that case, and apply_gaps falls back to
    push_initial_commit's own empty-repository bootstrap for it, rather
    than this dataclass needing a second shape for that case.

    `missing_workflow_scope` is distinct from `error`, the same way
    rules.py's own `never_reported` is distinct from a genuine ruleset
    preview failure: nothing is wrong with the repository or the request,
    only with what this gh token is allowed to write, and that's
    recoverable (add the scope, rerun) rather than something to retry or
    give up on. setup_cmd.py reads it to skip only the bootstrap step
    while still doing everything else this run can (mikelward/repo#18).

    `open_pull_request` is a scaffold pull request an earlier run already
    left open (read only when something is missing, since it is only ever
    the answer to "what would this run open"): the files are on their way
    in, and opening a second pull request for them would be noise, so
    this run reports that one and writes nothing."""

    error: bool = False
    missing_workflow_scope: bool = False
    base_commit_sha: str = None
    base_tree_sha: str = None
    present: list = field(default_factory=list)
    missing: dict = field(default_factory=dict)
    open_pull_request: GapPullRequest = None


def plan_gaps(repo, default_branch):
    """Which of the scaffold's files `repo` is missing on `default_branch`,
    read-only: builds the scaffold (same as build_scaffold_files) and
    compares it against the branch's current tree. Returns a GapPlan --
    `error` set (with the failure already reported) if fetching the
    scaffold's own template sources fails, if reading the branch's
    current state does, or if a scaffold path (or an ancestor directory
    component of one) is occupied by something other than a plain file
    (see occupied_reason below) -- there is no safe way to add the
    scaffold there without silently replacing whatever that is."""
    files = build_scaffold_files(default_branch)
    if files is None:
        return GapPlan(error=True)

    ok, ref_raw = gh.try_run(["api", f"repos/{repo}/git/ref/{_branch_ref_path(default_branch)}"])
    if not ok:
        if _ref_missing_commits(ref_raw):
            # No commits on this branch yet -- there is nothing to compare
            # against, so every scaffold file counts as missing and
            # apply_gaps bootstraps it the same way `repo create
            # --scaffold` does for a brand-new repository.
            return GapPlan(missing=files, missing_workflow_scope=_missing_workflow_scope(files))
        error_lines(f"could not read {repo}'s '{default_branch}' branch:", ref_raw)
        return GapPlan(error=True)

    try:
        commit_sha = json.loads(ref_raw)["object"]["sha"]
    except (ValueError, KeyError, TypeError):
        error(f"could not read {repo}'s '{default_branch}' branch: unexpected response")
        return GapPlan(error=True)

    try:
        commit_raw = gh.run(["api", f"repos/{repo}/git/commits/{commit_sha}"])
    except gh.GhError as e:
        error_lines(f"could not read {repo}'s current commit ({commit_sha}):", e.stderr)
        return GapPlan(error=True)
    try:
        tree_sha = json.loads(commit_raw)["tree"]["sha"]
    except (ValueError, KeyError, TypeError):
        error(f"could not read {repo}'s current commit ({commit_sha}): unexpected response")
        return GapPlan(error=True)

    try:
        tree_raw = gh.run(["api", f"repos/{repo}/git/trees/{tree_sha}?recursive=1"])
    except gh.GhError as e:
        error_lines(f"could not read {repo}'s current tree ({tree_sha}):", e.stderr)
        return GapPlan(error=True)
    try:
        tree = json.loads(tree_raw)
        entries = tree["tree"]
    except (ValueError, KeyError, TypeError):
        error(f"could not read {repo}'s current tree ({tree_sha}): unexpected response")
        return GapPlan(error=True)
    if tree.get("truncated"):
        # A truncated listing cannot tell "present" from "absent" for
        # whatever fell off the end -- treating those paths as missing
        # risks landing a duplicate/conflicting file GitHub would then
        # refuse (two entries for one path), and treating them as present
        # risks silently skipping a genuinely missing file. Neither is
        # safe to guess, so this fails closed instead.
        error(f"{repo}'s tree ({tree_sha}) is too large to list in one call; cannot safely tell")
        error("which scaffold files are already present. Add any missing ones by hand.")
        return GapPlan(error=True)
    # (type, mode) per entry -- git stores a symlink as type "blob" too
    # (mode "120000"), so type alone can't tell it apart from a real file;
    # only a regular-file mode may be treated as the scaffold content
    # already being there (Codex review, mikelward/repo#14).
    existing = {entry["path"]: (entry.get("type"), entry.get("mode")) for entry in entries if "path" in entry}

    def is_regular_file(path):
        kind, mode = existing.get(path, (None, None))
        if kind != "blob":
            return False
        if mode == "120000" and path in SYMLINK_IS_PRESENT_PATHS:
            # Most of this fleet points CLAUDE.md at AGENTS.md with a
            # symlink, which is the scaffold's content by another route.
            # Counting it as occupied instead would fail the whole
            # bootstrap step on every one of those repositories, over a
            # file that is already exactly right.
            return True
        return mode in ("100644", "100755")

    def occupied_reason(path):
        """None if `path` is safe to add as a new blob under base_tree
        (nothing there, or already the right kind of thing -- a blob,
        handled by the caller as "present" before this is even called);
        otherwise why it can't be, without guessing what to do about it.
        Three ways a path can be occupied by something a new blob would
        silently replace: the path itself already names a tree, a
        submodule gitlink, or a symlink (none of them a real file -- a
        symlink included, since it may point anywhere and isn't the
        scaffold content just because it shares the type "blob"), or an
        ANCESTOR directory component of it is itself a blob (a file
        sitting where a directory needs to be, so nothing under it can
        exist at all). All three are real if unlikely -- a repository
        with a directory named `.github/lanes.conf` is strange, but
        "never overwrites what's already there" (this module's own
        promise) has to hold even then (Codex review,
        mikelward/repo#14)."""
        kind, mode = existing.get(path, (None, None))
        if kind is not None and not is_regular_file(path):
            detail = kind if mode is None else f"{kind}, mode {mode}"
            return f"{path} already exists and is not a regular file ({detail})"
        parts = path.split("/")
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i])
            ancestor_kind, _ = existing.get(ancestor, (None, None))
            if ancestor_kind is not None and ancestor_kind != "tree":
                return f"{ancestor} already exists and is not a directory ({ancestor_kind})"
        return None

    present = []
    missing = {}
    occupied = {}
    for path, content in files.items():
        if is_regular_file(path):
            present.append(path)
            continue
        reason = occupied_reason(path)
        if reason is not None:
            occupied[path] = reason
        else:
            missing[path] = content

    if occupied:
        # Same fail-closed direction as a truncated tree: adding the blob
        # anyway would silently replace whatever occupies the path, and
        # there is no safe automatic resolution to guess at instead.
        for path in sorted(occupied):
            error(f"{repo}: cannot add {path} to the scaffold: {occupied[path]}; add it by hand")
        return GapPlan(error=True)

    open_pull_request = None
    if missing:
        # Only when there IS something to add: on an already-complete
        # repository -- every one of them, once a fleet has converged --
        # this read would answer a question nobody asked.
        ok, open_pull_request = find_open_gap_pull_request(repo, default_branch)
        if not ok:
            return GapPlan(error=True)

    return GapPlan(
        base_commit_sha=commit_sha,
        base_tree_sha=tree_sha,
        present=sorted(present),
        missing=missing,
        # Only where this run would actually write. With a scaffold pull
        # request already open there is nothing to write, so a token that
        # could not have written it is not a problem to report -- and
        # reporting one would fail the step over a repository whose
        # scaffold is already on its way in (Codex review,
        # mikelward/repo#42).
        missing_workflow_scope=(
            open_pull_request is None and _missing_workflow_scope(missing)
        ),
        open_pull_request=open_pull_request,
    )


def describe_gap_plan(plan):
    """Combined-plan lines for a GapPlan, in the same style as the other
    steps' own description helpers (rules._describe_plan,
    secrets_cmd._describe_plan)."""
    if plan.error:
        return ["could not plan (see above); nothing added"]
    if plan.missing_workflow_scope:
        workflow_count = sum(1 for path in plan.missing if path.startswith(".github/workflows/"))
        return [
            f"SKIPPED: this gh token is missing the 'workflow' OAuth scope, needed to add "
            f"{workflow_count} file(s) under .github/workflows/ -- run `gh auth refresh -s "
            "workflow` (or add the scope your token's own way) and rerun"
        ]
    if plan.open_pull_request:
        pr = plan.open_pull_request
        # No claim about what it contains: it is somebody's pull request,
        # editable at any moment, and a rerun after the merge says what is
        # actually left far more reliably than a read of it can.
        lines = [
            f"pull request #{pr.number} is adding the scaffold ({pr.url}); nothing to open here "
            "-- merge it, then rerun to see what is left",
        ]
        lines.append(f"still absent from the default branch: {len(plan.missing)} file(s)")
        return lines
    lines = [f"add {path}" for path in sorted(plan.missing)]
    if plan.missing:
        lines.insert(0, f"open a pull request adding {len(plan.missing)} file(s):")
    if plan.present:
        lines.append(f"already present, untouched: {len(plan.present)} file(s)")
    if not lines:
        lines.append("already complete")
    return lines


def _recheck_branch_sha(repo, default_branch):
    """The branch's current tip sha, or None (with the failure already
    reported) if the read fails or the response is unreadable. Shared by
    apply_gaps's no-op and real-write paths, which both need the same
    "has this moved since the plan was built" answer."""
    ok, raw = gh.try_run(["api", f"repos/{repo}/git/ref/{_branch_ref_path(default_branch)}"])
    if not ok:
        error_lines(f"could not re-check {repo}'s '{default_branch}' branch before updating it:", raw)
        return None
    try:
        return json.loads(raw)["object"]["sha"]
    except (ValueError, KeyError, TypeError):
        error(f"could not re-check {repo}'s '{default_branch}' branch before updating it: unexpected response")
        return None


def _create_gap_commit(repo, plan):
    """One commit adding `plan.missing` on top of plan.base_commit_sha --
    blobs, then a tree over the branch's own base_tree, then the commit
    itself. Returns its sha, or None with the failure already reported.

    Writes no ref: where that commit then goes (a new branch, for the
    pull request apply_gaps opens) is the caller's decision."""
    tree_entries = []
    for path, content in sorted(plan.missing.items()):
        try:
            raw = gh.run_with_input(
                ["api", "--method", "POST", f"repos/{repo}/git/blobs", "--input", "-"],
                json.dumps({"content": content, "encoding": "utf-8"}).encode(),
            )
        except gh.GhError as e:
            error_lines(f"could not create a blob for {path}:", e.stderr)
            return None
        blob_sha = json.loads(raw)["sha"]
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})

    try:
        raw = gh.run_with_input(
            ["api", "--method", "POST", f"repos/{repo}/git/trees", "--input", "-"],
            json.dumps({"base_tree": plan.base_tree_sha, "tree": tree_entries}).encode(),
        )
    except gh.GhError as e:
        error_lines(f"could not create the scaffold gap-fill's tree on {repo}:", e.stderr)
        return None
    tree_sha = json.loads(raw)["sha"]

    try:
        raw = gh.run_with_input(
            ["api", "--method", "POST", f"repos/{repo}/git/commits", "--input", "-"],
            json.dumps(
                {
                    "message": _gap_commit_message(plan.missing),
                    "tree": tree_sha,
                    "parents": [plan.base_commit_sha],
                }
            ).encode(),
        )
    except gh.GhError as e:
        error_lines(f"could not create the scaffold gap-fill's commit on {repo}:", e.stderr)
        return None
    return json.loads(raw)["sha"]


def _create_gap_branch(repo, commit_sha):
    """A new branch at `commit_sha`, named after it. Returns the branch
    name, or None with the failure already reported.

    A ref that already exists under this name is accepted only when it
    already points at exactly this commit -- the name carries the commit's
    own sha, so that is a rerun that rebuilt an identical commit, not
    somebody else's branch. Anything else is refused rather than
    force-moved: this module never overwrites what is already there."""
    branch = f"{GAP_BRANCH_PREFIX}-{commit_sha[:7]}"
    try:
        gh.run_with_input(
            ["api", "--method", "POST", f"repos/{repo}/git/refs", "--input", "-"],
            json.dumps({"ref": f"refs/heads/{branch}", "sha": commit_sha}).encode(),
        )
    except gh.GhError as e:
        if _read_ref_sha(repo, branch) == commit_sha:
            return branch
        error_lines(f"could not create the branch '{branch}' on {repo}:", e.stderr)
        return None
    return branch


def _read_ref_sha(repo, branch):
    """`branch`'s tip sha on `repo`, or None if it can't be read. Quiet:
    the one caller uses it to tell "already there, and it's mine" from
    "already there, and it isn't", and reports the failure it is actually
    diagnosing itself."""
    ok, raw = gh.try_run(["api", f"repos/{repo}/git/ref/{_branch_ref_path(branch)}"])
    if not ok:
        return None
    try:
        return json.loads(raw)["object"]["sha"]
    except (ValueError, KeyError, TypeError):
        return None


def open_gap_pull_request(repo, default_branch, plan):
    """Adds `plan.missing` to `repo` as a pull request against
    `default_branch`: one commit on a new branch off plan.base_commit_sha,
    then the pull request itself. Returns a GapPullRequest -- the one this
    opened, or one an earlier run left open (`opened` False) -- or None,
    with the failure already reported.

    A pull request rather than the direct ref update this step used to
    make, for two reasons that point the same way:

    - It is the only write a branch a ruleset already protects will
      accept. A pull_request rule blocks a direct push outright for any
      caller not configured as a bypass actor, which this tool never
      configures itself to be -- so a repository a PRIOR `repo setup` run
      protected had no path to a scaffold fix at all (Codex review,
      mikelward/repo#14, and TODO.md's own entry for it).
    - A direct push does not run the checks the scaffold is being added
      to make reportable. `lanes` and `zizmor` run on the pull request,
      so this is what first makes them report, which is what a ruleset
      requiring them needs before it can be written at all (see
      rules.never_reported). `codex` is the one that still cannot report
      here: its status-writing workflow runs under `pull_request_target`,
      which GitHub takes from the BASE branch's copy, so the pull request
      adding that workflow is the one pull request it cannot run on. It
      reports from the first pull request opened after this one merges.

    The open-pull-request check is made again here, not just at plan time:
    a combined `repo setup` plan waits on a confirmation that can take as
    long as the person takes, and this step runs unattended across a
    fleet. The base branch is re-checked too, and an exact match
    required -- `plan.missing` was computed against that tree, so a branch
    that moved may already carry one of these files, and adding it again
    is a conflict rather than a gap-fill."""
    # Before anything else, including accepting a pull request an earlier
    # run left open: `plan.missing` was computed against this exact tree,
    # and every answer built on it -- which files are still absent, and so
    # which checks the gap keeps from reporting -- is wrong if the branch
    # has moved. A branch that lost a base-published workflow during the
    # wait would leave the caller concluding the gap blocks nothing and
    # requiring a check nothing can publish (Codex review,
    # mikelward/repo#42).
    current_sha = _recheck_branch_sha(repo, default_branch)
    if current_sha is None:
        return None
    if current_sha != plan.base_commit_sha:
        error(
            f"{repo}'s '{default_branch}' branch no longer points at the commit this plan was "
            "built from -- it moved (or was reset) while this was waiting. Refusing to act on a "
            "tree this hasn't compared against; rerun to gap-fill from its current state."
        )
        return None

    ok, existing = find_open_gap_pull_request(repo, default_branch)
    if not ok:
        return None
    if existing is not None:
        return existing
    if plan.open_pull_request is not None:
        # The plan found one open, so the caller counted this step as
        # having nothing to write and never asked anyone to agree to one.
        # It has closed since. Opening one now would be a write the
        # preview said would not happen and nobody confirmed -- including
        # on a non-interactive run, which refuses unconfirmed changes
        # precisely so this cannot occur (Codex review,
        # mikelward/repo#42). Refused rather than silently switching to
        # the write path, on the same ground as the moved base above.
        error(
            f"the scaffold pull request #{plan.open_pull_request.number} this plan was built "
            f"around is no longer open on {repo}, and opening another is a write this run "
            "never asked about. Rerun to plan against the repository's current state."
        )
        return None

    commit_sha = _create_gap_commit(repo, plan)
    if commit_sha is None:
        return None
    branch = _create_gap_branch(repo, commit_sha)
    if branch is None:
        return None

    body = _GAP_PULL_REQUEST_BODY.format(
        files="\n".join(f"- `{path}`" for path in sorted(plan.missing))
    )
    try:
        raw = gh.run_with_input(
            ["api", "--method", "POST", f"repos/{repo}/pulls", "--input", "-"],
            json.dumps(
                {
                    "title": _GAP_SUBJECT,
                    "head": branch,
                    "base": default_branch,
                    "body": body,
                }
            ).encode(),
        )
    except gh.GhError as e:
        error_lines(f"could not open a pull request adding the scaffold to {repo}:", e.stderr)
        error(
            f"The commit is already pushed, on the branch '{branch}' -- open the pull request "
            "from it by hand, or delete that branch and rerun. `--no-bootstrap` skips this "
            "step so the rest of `repo setup` can finish meanwhile."
        )
        return None
    try:
        data = json.loads(raw)
        return GapPullRequest(number=int(data["number"]), url=data["html_url"], head_branch=branch)
    except (ValueError, KeyError, TypeError):
        # The pull request itself was very likely created -- the write
        # succeeded, only its response didn't parse -- so this must not
        # read as "nothing happened". A rerun finds it by its branch name
        # and reports it rather than opening a second one.
        error(
            f"opened a pull request adding the scaffold to {repo} from '{branch}', but could "
            "not read which one from the response. Check the repository's open pull requests; "
            "rerunning will find it rather than open another."
        )
        return None


def apply_gaps(repo, default_branch, plan):
    """Applies a GapPlan built by plan_gaps, and reports what it did as a
    GapOutcome. Three shapes, decided by the plan:

    - Nothing missing: the branch is already complete, so this only
      re-verifies its tip hasn't moved since the plan was built -- a no-op
      plan is still a CLAIM that the branch is complete, and a concurrent
      push that deleted or replaced a scaffold file since plan_gaps read
      the tree must not go unnoticed just because there is nothing left
      here to write (Codex review, mikelward/repo#14). The verified tip
      comes back as `branch_sha`, so a caller gets a fresh sha to build
      further checks on rather than only a bool.
    - No commits on the branch at all (plan.base_commit_sha is None): the
      same two-commit bootstrap `repo create --scaffold` uses
      (push_initial_commit). The one case that still writes the branch
      directly, because it has to -- a pull request needs a base branch,
      and there isn't one yet.
    - Anything missing on a branch that has commits: a pull request
      (open_gap_pull_request), never a direct push. See that function's
      own docstring for both reasons. `branch_sha` stays None there: the
      scaffold is not on the branch yet, which is exactly what a caller
      about to activate pull-request protection needs to know."""
    if plan.base_commit_sha is None:
        if not plan.missing:
            # Defensive: plan_gaps treats every scaffold file as missing
            # when there is no commit to compare against, so this pairing
            # isn't a real path -- said rather than returned silently.
            error(f"{repo}: '{default_branch}' has no commits and no scaffold file to add")
            return GapOutcome(error=True)
        sha = push_initial_commit(repo, default_branch, plan.missing)
        return GapOutcome(error=True) if sha is None else GapOutcome(branch_sha=sha)

    if not plan.missing:
        current_sha = _recheck_branch_sha(repo, default_branch)
        if current_sha is None:
            return GapOutcome(error=True)
        if current_sha != plan.base_commit_sha:
            error(
                f"{repo}'s '{default_branch}' branch no longer points at the commit this plan "
                "was built from -- it moved (or was reset) while this was waiting. Refusing to "
                "report the scaffold complete; rerun to check its current state."
            )
            return GapOutcome(error=True)
        return GapOutcome(branch_sha=current_sha)

    pull_request = open_gap_pull_request(repo, default_branch, plan)
    if pull_request is None:
        return GapOutcome(error=True)
    return GapOutcome(pull_request=pull_request)
