"""The scaffold step of `repo create --scaffold` and `repo setup`'s own
always-on bootstrap step -- the fleet's standard CI files, pushed as one
atomic commit either to a repository that has none yet (`repo create`,
push_initial_commit) or as whatever's still missing from one that already
has some content (`repo setup`, plan_gaps/apply_gaps).

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
- `ci.yml` is generated too, but only the classify+gate wiring
  `mikelward/lanes`'s own README documents verbatim -- the actual project
  jobs (tests, a build) are NOT something this can know, so a trivial
  always-passing placeholder job is wired into the gate instead of
  guessing at what "the project's real jobs" are. This is a deliberate
  choice over shipping `ci.yml` with an empty `needs:`/`results:` list:
  that shape is not documented anywhere as supported, and a required
  check that turns out not to work is exactly the trap `lanes` exists to
  prevent. Delete the placeholder once a real job exists.

Cost and reliability: five `gh api` reads against mikelward/codex-review
and mikelward/lanes -- both this account's own public repos -- to build
the scaffold's file set, on EVERY call (one of the five resolves
codex-review's `main` to a single commit sha so its three template files
come from one revision rather than each independently resolving a `main`
that could move between them -- see _resolve_commit_sha): `repo create
--scaffold` makes them once per new repository, but `repo setup`'s
bootstrap step being always on (see setup_cmd.py) means every `repo
setup` invocation pays them too, even one that finds nothing missing,
since plan_gaps has no way to know the answer without building the
scaffold to compare against.
Three more reads against the target repository itself (its branch ref,
that commit, its current tree) price the comparison. All of this is
free, inside the same rate limit as everything else this tool does, but
it is the one step here whose read cost scales with two OTHER
repositories' availability rather than only the target's -- reported like
any other gh failure, and it fails the bootstrap/scaffold step alone
without touching anything else `repo setup`'s other steps did. Applying
the result, when there is one, costs one blob per missing file, one tree,
one commit, and one ref update (two commits total only for a repository
with no commits yet -- see push_initial_commit's own docstring for why).
"""

import base64
import json
import re
import urllib.parse
from dataclasses import dataclass, field

from repo_lib import gh
from repo_lib.common import error, error_lines

TEMPLATE_REPO = "mikelward/codex-review"
TEMPLATE_FILES = ("codex-review.yml", "codex-review-check.yml", "codex-review-listener.yml")
ZIZMOR_SOURCE_REPO = "mikelward/lanes"

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


@dataclass
class GapPlan:
    """What `repo setup`'s bootstrap step found on an already-existing
    repository, and what it would do about it. `error` means the read
    failed (already reported); `present`/`missing` are only meaningful
    when it's False. `base_commit_sha`/`base_tree_sha` are None when the
    branch has no commits yet -- plan_gaps still builds `missing` as every
    scaffold file in that case, and apply_gaps falls back to
    push_initial_commit's own empty-repository bootstrap for it, rather
    than this dataclass needing a second shape for that case."""

    error: bool = False
    base_commit_sha: str = None
    base_tree_sha: str = None
    present: list = field(default_factory=list)
    missing: dict = field(default_factory=dict)


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
            return GapPlan(missing=files)
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
        return kind == "blob" and mode in ("100644", "100755")

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
        with a directory named `TODO.md` is strange, but "never overwrites
        what's already there" (this module's own promise) has to hold
        even then (Codex review, mikelward/repo#14)."""
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

    return GapPlan(
        base_commit_sha=commit_sha, base_tree_sha=tree_sha, present=sorted(present), missing=missing
    )


def describe_gap_plan(plan):
    """Combined-plan lines for a GapPlan, in the same style as the other
    steps' own description helpers (rules._describe_plan,
    secrets_cmd._describe_plan)."""
    if plan.error:
        return ["could not plan (see above); nothing added"]
    lines = [f"add {path}" for path in sorted(plan.missing)]
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


def apply_gaps(repo, default_branch, plan):
    """Applies a GapPlan built by plan_gaps: pushes `plan.missing` as one
    commit on top of `plan.base_commit_sha`, touching nothing already
    present. Returns the resulting branch tip's sha on success (None,
    already reported, on failure) -- a plan with nothing missing still
    re-verifies the branch hasn't moved and returns its (unchanged) tip,
    so callers get a fresh, verified sha to build further checks on either
    way, not just a bool.

    plan.base_commit_sha is None exactly when the branch had no commits at
    plan time, in which case this is the same bootstrap push `repo create
    --scaffold` uses (push_initial_commit) rather than a gap-fill commit.

    The final ref update is a plain, non-force PATCH: if the branch has
    moved since the plan was built, GitHub refuses it as a non-fast-forward
    rather than silently overwriting or rewinding whatever landed in the
    meantime -- the same protection push_initial_commit's own second write
    already relies on, not a new mechanism.

    A branch a PRIOR `repo setup` run already protected with a ruleset
    requiring pull requests refuses this PATCH too, for an unrelated
    reason: that rule blocks a direct push from anyone not configured as a
    bypass actor, which this tool's own ruleset step never configures the
    caller to be (Codex review, mikelward/repo#14). There is no way to
    tell the two failures apart from the response alone, so the error
    below states neither cause and relays gh's own message instead of
    guessing -- calling code (setup_cmd.py) applies this step before its
    own ruleset step for exactly this reason, but that only ever covers a
    branch this same run is the one protecting; an already-protected one
    from an earlier run has no direct-push path here at all yet."""
    if plan.base_commit_sha is None:
        # plan.missing is always non-empty here in practice (plan_gaps
        # treats every scaffold file as missing when there's no commit to
        # compare against) -- the None fallback is defensive, not a real
        # path.
        return push_initial_commit(repo, default_branch, plan.missing) if plan.missing else None

    if not plan.missing:
        # A no-op plan is still a claim the branch is COMPLETE -- a
        # concurrent push that deleted or replaced a scaffold file since
        # plan_gaps read the tree must not go unnoticed just because
        # there's nothing left here to write (Codex review,
        # mikelward/repo#14).
        current_sha = _recheck_branch_sha(repo, default_branch)
        if current_sha is None:
            return None
        if current_sha != plan.base_commit_sha:
            error(
                f"{repo}'s '{default_branch}' branch no longer points at the commit this plan "
                "was built from -- it moved (or was reset) while this was waiting. Refusing to "
                "report the scaffold complete; rerun to check its current state."
            )
            return None
        return current_sha

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

    message = "Add missing fleet CI scaffold files\n\n" + "\n".join(
        f"- {path}" for path in sorted(plan.missing)
    )
    try:
        raw = gh.run_with_input(
            ["api", "--method", "POST", f"repos/{repo}/git/commits", "--input", "-"],
            json.dumps(
                {"message": message, "tree": tree_sha, "parents": [plan.base_commit_sha]}
            ).encode(),
        )
    except gh.GhError as e:
        error_lines(f"could not create the scaffold gap-fill's commit on {repo}:", e.stderr)
        return None
    commit_sha = json.loads(raw)["sha"]

    # force: False alone only guarantees a fast-forward -- an ancestry
    # check, not "the ref hasn't moved". If the branch was force-pushed
    # BACKWARD to an ancestor of plan.base_commit_sha while this waited
    # (a deliberate reset, discarding commits on purpose), that ancestor
    # still passes the fast-forward check against the commit this built
    # -- it descends from exactly that ancestor -- so a bare force:False
    # PATCH would silently restore the commits the reset just removed.
    # An explicit equality check against the CURRENT ref, right before
    # the write, catches that: only an exact match is safe to build on
    # (Codex review, mikelward/repo#14). This narrows the race to the gap
    # between this read and the PATCH itself rather than closing it
    # outright -- GitHub's git-data-api has no compare-and-swap ref
    # update, so a true atomic guarantee isn't available here, the same
    # limit every other recheck-before-write in this codebase already
    # accepts (see e.g. secrets_cmd._recheck_still_absent).
    current_sha = _recheck_branch_sha(repo, default_branch)
    if current_sha is None:
        return None
    if current_sha != plan.base_commit_sha:
        error(
            f"{repo}'s '{default_branch}' branch no longer points at the commit this plan was "
            "built from -- it moved (or was reset) while this was waiting. Refusing to update it; "
            "rerun to gap-fill from its current state."
        )
        return None

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
            json.dumps({"sha": commit_sha, "force": False}).encode(),
        )
    except gh.GhError as e:
        error_lines(
            f"could not update {default_branch} to the scaffold gap-fill's commit on {repo} "
            "(moved since the plan was built, or a ruleset already blocks a direct push):",
            e.stderr,
        )
        return None

    return commit_sha
