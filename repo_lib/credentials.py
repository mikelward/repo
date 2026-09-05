"""Where this fleet's shared credentials belong, and how to read where they
actually are. Shared by `repo audit` (which reports) and `repo setup`
(which fixes), so the two cannot disagree about what "in the right place"
means.

The weekly dependency batches -- mikelward/npm-update, gradle-update and
rust-update -- and the screenshot commit-back workflow,
mikelward/ci-commit-artifact, are reusable workflows. A secret passed to
one by name reaches the runner of every job in it: for a batch, the
untrusted update job that runs whatever the batch resolved, with sudo. So
each reads its credential from an environment named after it, which only
its clean publish (or commit) job declares, and a caller passes
`secrets: inherit`, the only route by which an environment secret reaches
a called workflow. A copy left as a repository secret still reaches the
untrusted job through `inherit`, so the fix is a move, not a copy: set it
in the environment, then delete the repository one.

The lanes App credential -- `LANES_APP_ID` and `LANES_APP_PRIVATE_KEY`,
which mikelward/lanes's `init`, `attest` and `gate` modes publish the
required `lanes` status with -- is read by an action step, not a called
workflow, so no `secrets: inherit` is involved: it reaches a job only when
that job declares the `lanes` environment, and a copy at repository level
reaches every job of every workflow instead -- a same-repo pull request's
own push-triggered run included, which is the hole mikelward/lanes's
trusted publishing exists to close. The environment must also admit only
the trusted base branch, or that same run reaches it by declaring the
environment itself.

Names only, throughout: GitHub never returns a secret's value, which is
why `repo setup` needs the value handed to it to complete a move.
"""

import base64
import dataclasses
import json
import urllib.parse

import yaml

from repo_lib import gh

BATCH_HUBS = ("npm-update", "gradle-update", "rust-update")

COMMIT_ARTIFACT_TOKEN = "CI_COMMIT_ARTIFACT_TOKEN"
COMMIT_ARTIFACT_ENV = "ci-commit-artifact"
# The `uses:` prefix that identifies a caller of the commit-back workflow.
COMMIT_ARTIFACT_WORKFLOW = "mikelward/ci-commit-artifact/"

LANES_APP_ID = "LANES_APP_ID"
LANES_APP_PRIVATE_KEY = "LANES_APP_PRIVATE_KEY"
LANES_ENV = "lanes"
# The `uses:` value (before its `@ref`) of a mikelward/lanes step. The
# action, not a reusable workflow: a step's `uses:` carries no secrets,
# so the environment declaration on the job is what reaches it a credential.
LANES_ACTION = "mikelward/lanes"


def batch_credentials(hub):
    """The secret names a hub's publish job reads: (PAT, App ID, App key)."""
    prefix = hub.upper().replace("-", "_")
    return (f"{prefix}_PAT", f"{prefix}_APP_ID", f"{prefix}_APP_PRIVATE_KEY")


@dataclasses.dataclass(frozen=True)
class WorkflowName:
    """A workflow, and the branch it was read from when that is not the
    default one.

    The two used to travel as `f"{file} on {branch}"`, which no rule can
    take apart again: a workflow file may legally be named
    `build.yml on prod.yml`, and that is indistinguishable from `build.yml`
    read on a branch named `prod.yml` (Codex review, mikelward/repo#23).
    AGENTS.md asks for a real data structure rather than a string-encoded
    collection, and this is why.

    It renders as the identity it always had -- `str()` is what names the
    workflow in an error or a key -- while `label` is the prose form, with
    the file's extension dropped and the branch's left alone.
    """

    file: str
    branch: str = None

    def __str__(self):
        return self.file if self.branch is None else f"{self.file} on {self.branch}"

    @property
    def label(self):
        return _strip_extension(self.file) + (
            "" if self.branch is None else f" on {self.branch}"
        )

    def __lt__(self, other):
        # Ordered by hand rather than by `order=True`: the generated one
        # compares the fields as a tuple, and `None < "feature"` raises.
        # Both audit and setup sort these, and a workflow that exists on
        # the default branch AND differs on another produces exactly that
        # pair -- so the routine case aborted the whole command (Codex
        # review, mikelward/repo#23). A ref name is never empty, so the
        # default-branch copy sorts first.
        if not isinstance(other, WorkflowName):
            return NotImplemented
        return (self.file, self.branch or "") < (other.file, other.branch or "")


def _strip_extension(name):
    for suffix in (".yml", ".yaml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def workflow_label(name):
    """`name` as it should READ, not as it is stored: `gradle-update.yml`
    is written `gradle-update`.

    A caller is usually named after the batch it calls, so a line naming
    both said the same word twice with one `.yml` hung off it. The
    extension identifies a file, and these sentences are about a workflow.
    Only for prose -- a path being read or written keeps its real name.
    A `WorkflowName` answers for itself, since it kept the file and the
    branch apart; a bare string is a file name and nothing else.
    """
    if isinstance(name, WorkflowName):
        return name.label
    return _strip_extension(name)


def workflow_labels(names):
    """`workflow_label` over a comma-joined list, for prose naming several
    callers at once."""
    return ", ".join(workflow_label(n) for n in names)


def hub_workflow(hub):
    """The `uses:` prefix that identifies a caller of the hub's batch."""
    return f"mikelward/{hub}/"


def callers(texts, workflow_prefix):
    """Every workflow in `texts` (name -> text) that calls the reusable
    workflow at `workflow_prefix` from a job, with `caller_inherits`'s
    verdict for it -- whatever the file is named. The fleet names a batch's
    caller `<hub>.yml`, but GitHub runs any name, and a second caller under
    another name would be stranded by a delete the named one justifies
    (Codex, mikelward/repo#13)."""
    found = {}
    for name, text in texts.items():
        verdict = caller_inherits(text, workflow_prefix)
        if verdict is not None:
            found[name] = verdict
    return found


def unread_mentions(texts, workflow_prefix):
    """The workflows that mention the reusable workflow more often than the
    reader resolved a caller in them: "cannot tell", which holds a delete
    back whether or not other callers were read (see `mentions`). Counted
    per mention, not per file, since a file can hold a readable caller and
    a second one in a shape the reader cannot resolve (Codex,
    mikelward/repo#13)."""
    unread = []
    for name, text in texts.items():
        resolved = set()
        _caller_verdicts(text, workflow_prefix, resolved)
        if _reference_count(text, workflow_prefix) > len(resolved):
            unread.append(name)
    return sorted(unread)


FLEET_CREDENTIALS = tuple(name for hub in BATCH_HUBS for name in batch_credentials(hub)) + (
    COMMIT_ARTIFACT_TOKEN,
    LANES_APP_ID,
    LANES_APP_PRIVATE_KEY,
)


def home_environment(name):
    """The environment a fleet credential belongs in, or None for a name
    that is not one. Case-insensitive, as GitHub treats secret names."""
    name = name.upper()
    for hub in BATCH_HUBS:
        if name in batch_credentials(hub):
            return hub
    if name == COMMIT_ARTIFACT_TOKEN:
        return COMMIT_ARTIFACT_ENV
    if name in (LANES_APP_ID, LANES_APP_PRIVATE_KEY):
        return LANES_ENV
    return None


def usable(names, hub):
    """Whether `names` hold a credential the hub's publish job can open a
    pull request with: the PAT, or both halves of the App pair."""
    pat, app_id, app_key = batch_credentials(hub)
    return pat in names or (app_id in names and app_key in names)


def lanes_usable(names):
    """Whether `names` hold the lanes App credential: both halves of the
    pair -- there is no PAT form, since the point of the App is that its
    login, not a user's, is what the status's creator field records."""
    return LANES_APP_ID in names and LANES_APP_PRIVATE_KEY in names


class ReadError(Exception):
    """A read this module needed failed. `.message` says which, `.detail`
    is gh's own stderr -- the caller decides whether that is an exit (the
    audit) or a failed preview (setup); neither may report it as a
    finding, since "could not tell" and "found a gap" are different
    answers."""

    def __init__(self, message, detail):
        super().__init__(message)
        self.message = message
        self.detail = detail


def _names(endpoint, jq):
    """One name per line from a --paginate --jq listing, blanks dropped.
    Raises gh.GhError on failure."""
    return [
        line.strip()
        for line in gh.run(["api", "--paginate", endpoint, "--jq", jq]).splitlines()
        if line.strip()
    ]


def repository_secrets(repo):
    """The repository-level secret names, upper-cased: secret names are
    case-insensitive on GitHub, so every comparison is made in the fleet
    credentials' own spelling."""
    try:
        return [name.upper() for name in _names(f"repos/{repo}/actions/secrets", ".secrets[].name")]
    except gh.GhError as e:
        raise ReadError(f"could not list {repo}'s repository secrets:", e.stderr)


def environments(repo):
    """The environment names as GitHub lists them."""
    try:
        return _names(f"repos/{repo}/environments", ".environments[].name")
    except gh.GhError as e:
        raise ReadError(f"could not list {repo}'s environments:", e.stderr)


def environment_secrets(repo, environments, env):
    """(the environment's name as GitHub lists it, its secret names
    upper-cased) for `env`, or (None, []) when the repository has no such
    environment. Environment names are case-insensitive on GitHub too, so
    `Gradle-Update` IS the `gradle-update` environment, and its secrets are
    read under the name GitHub lists."""
    listed = next((name for name in environments if name.lower() == env.lower()), None)
    if listed is None:
        return None, []
    try:
        names = _names(f"repos/{repo}/environments/{listed}/secrets", ".secrets[].name")
    except gh.GhError as e:
        raise ReadError(f"could not list {repo}'s '{listed}' environment secrets:", e.stderr)
    return listed, [name.upper() for name in names]


def _ref_suffix(ref):
    """The `?ref=` query for a branch, percent-encoded: a branch name can
    carry `#`, `?`, `&` or a space, and sent raw the request would name
    another ref -- `feature/x#1` reads as `feature/x` -- so a caller that
    exists only there would be read as unused (Codex, mikelward/repo#13)."""
    return "?ref=" + urllib.parse.quote(ref, safe="") if ref else ""


def workflow_entries(repo, ref=None):
    """The files under .github/workflows on `ref` (the default branch when
    None) as name -> blob sha, or {} when the branch has no such directory.
    The one tolerated failure is a 404 on the directory, and only once the
    root listing confirms it is an absent directory rather than contents
    the token cannot read -- a fine-grained token without Contents access
    gets the same 404 -- or GitHub says the repository is empty. Taken for
    "no workflows", an unreadable tree would skip every credential check
    and report the real credentials as stale, with an instruction to
    delete them."""
    where = f"{repo}'s workflows" + (f" on branch {ref}" if ref else "")
    succeeded, output = gh.try_run(
        [
            "api",
            f"repos/{repo}/contents/.github/workflows{_ref_suffix(ref)}",
            "--jq",
            '.[] | "\\(.name) \\(.sha)"',
        ]
    )
    if succeeded:
        entries = {}
        for line in output.splitlines():
            name, _, sha = line.strip().rpartition(" ")
            if name:
                entries[name] = sha
        return entries
    if "HTTP 404" not in output:
        raise ReadError(f"could not list {where}:", output)
    root_ok, root_output = gh.try_run(["api", f"repos/{repo}/contents{_ref_suffix(ref)}", "--jq", "length"])
    if root_ok:
        return {}
    if "HTTP 404" in root_output and "empty" in root_output.lower():
        # "This repository is empty." -- no commits, so no workflows.
        return {}
    raise ReadError(
        f"could not tell whether {repo} has workflows" + (f" on branch {ref}" if ref else "") + " -- its "
        "contents are unreadable (a token without Contents access reads as 404):",
        root_output,
    )


def workflow_files(repo):
    """The file names under .github/workflows on the default branch, or []
    when the repository has no such directory (see `workflow_entries`)."""
    return list(workflow_entries(repo))


def workflow_text(repo, name, ref=None):
    """A workflow file's text, on `ref` when given. The contents endpoint
    answers base64 with line breaks, which b64decode discards."""
    try:
        content = gh.run(
            ["api", f"repos/{repo}/contents/.github/workflows/{name}{_ref_suffix(ref)}", "--jq", ".content"]
        )
    except gh.GhError as e:
        raise ReadError(
            f"could not read {repo}'s .github/workflows/{name}" + (f" on branch {ref}" if ref else "") + ":",
            e.stderr,
        )
    return base64.b64decode(content).decode("utf-8", errors="replace")


def default_branch(repo):
    try:
        return gh.run(["api", f"repos/{repo}", "--jq", ".default_branch"]).strip()
    except gh.GhError as e:
        raise ReadError(f"could not read {repo}'s default branch:", e.stderr)


def branches(repo):
    """Every branch name. One page of a hundred: these repositories carry a
    handful, and a fleet where that stopped being true would want the
    listing paged rather than truncated."""
    try:
        output = gh.run(["api", f"repos/{repo}/branches?per_page=100", "--jq", ".[].name"])
    except gh.GhError as e:
        raise ReadError(f"could not list {repo}'s branches:", e.stderr)
    return [line.strip() for line in output.splitlines() if line.strip()]


def _is_workflow(name):
    return name.endswith((".yml", ".yaml"))


def workflow_texts(repo, default=None):
    """Every workflow's text: the default branch's under its file name,
    and, under `<name> on <branch>`, each workflow on another branch whose
    blob differs from the default branch's copy (new there, or changed).
    A push-triggered workflow runs from its own branch, so a caller that
    exists only on a branch still needs its credential, and a reading of
    the default branch alone would delete it as unused (Codex,
    mikelward/repo#13). Unchanged copies are not re-read: the same blob
    reads the same. `default` is the default branch's name when the caller
    has already read it, so a plan that needs the name for its own reasons
    reads it once.

    The default branch's own reads name that branch explicitly rather than
    letting the Contents API resolve "the default" itself. Unqualified,
    they answer from whatever the default is at request time, so a rename
    between the caller's read of the name and these calls returned the NEW
    branch's copies filed under the bare name -- read as the default's,
    while the environment policy was judged against the old name, and a
    plan clean enough to queue no move never reached the apply-time rename
    check (Codex, mikelward/repo#36). Named explicitly, that rename is a
    404 on a branch this snapshot says exists, which every caller already
    reports rather than taking for "no workflows"."""
    default = default_branch(repo) if default is None else default
    entries = workflow_entries(repo, default)
    texts = {
        WorkflowName(name): workflow_text(repo, name, default)
        for name in entries
        if _is_workflow(name)
    }
    for branch in branches(repo):
        if branch == default:
            continue
        for name, sha in workflow_entries(repo, branch).items():
            if _is_workflow(name) and entries.get(name) != sha:
                texts[WorkflowName(name, branch)] = workflow_text(repo, name, branch)
    return texts


# Workflows are read as YAML, through PyYAML -- the one third-party
# dependency this tool takes (AGENTS.md, "Style"). The reader this replaced
# walked the text by line and regex, and in eight of the fourteen review
# rounds on mikelward/repo#36 Codex found a shape it misread -- a search for
# `app-id` matching another step, then `env:`, a flow mapping, whitespace
# before a colon, a quoted key, an upper-case input name, a quoted `uses`
# key, a node property before a quoted scalar. Each fix widened what
# resolved by one case, because the class was reading YAML without a
# parser; the parser deletes the class (maintainer, 2026-09-05). What a
# parser cannot read -- a document PyYAML rejects, a `with:` that is not a
# mapping -- is "cannot tell", never "unused".

# What `_document` returns for text PyYAML rejects, as distinct from the
# None an empty document parses to: a rejected document leaves every
# mention in the raw text unread, while an empty one mentions nothing.
_REJECTED = object()


def _document(text):
    """`text` parsed as YAML, `_REJECTED` when PyYAML rejects it -- an
    unclosed quote or bracket, an alias with no anchor, a key that cannot
    be hashed. Nothing in a rejected document resolves, so the callers
    report every mention in it as "cannot tell"; that is the whole
    handling, and the reason the error itself is not re-raised."""
    try:
        return yaml.safe_load(text)
    except Exception:
        # Every way this can fail means one thing here -- the document
        # resolves nothing -- so it is caught as one, and the `try` holds a
        # single third-party call, which is what makes that safe rather
        # than a blanket. `YAMLError` is only the syntax half: deep-but-
        # valid nesting (500 opened flow sequences will do it) exhausts the
        # parser's own stack for a `RecursionError`, and a constructor
        # raises a bare `ValueError` on a scalar YAML's own resolver claims
        # and Python then rejects -- `2026-13-01` reads as a timestamp and
        # dies on "month must be in 1..12". Both escaped as a crashed
        # command from a reader whose every other unreadable shape is a
        # finding (Codex, mikelward/repo#36, twice); enumerating the
        # constructors' failure types would be the one-more-case pattern
        # the line-by-line reader died of.
        return _REJECTED


def _strings(node, seen=None):
    """Every string in `node`, keys and values alike.

    Containers are visited once. A YAML alias can point at its own
    ancestor (`x: &x [*x]`), which `yaml.safe_load` resolves into a
    genuinely self-referential object rather than rejecting: walking it
    naively recurses until Python gives up, and a `RecursionError` out of
    a reader is a crashed command where every other unreadable shape is a
    finding (Codex, mikelward/repo#36). Identity, not equality: two equal
    lists are two nodes, and skipping the second would drop real
    mentions."""
    if isinstance(node, str):
        yield node
        return
    if not isinstance(node, (dict, list, tuple)):
        return
    if seen is None:
        seen = set()
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(key, seen)
            yield from _strings(value, seen)
    else:
        for item in node:
            yield from _strings(item, seen)


# What a GitHub owner or repository name is spelled with, so a needle that
# is one can be counted where the name ends rather than continues. Lower
# case only: `_mention_count` folds both sides before asking.
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


def _occurrences(haystack, needle, boundary):
    """How often `needle` occurs in `haystack`; with `boundary`, only where
    a repository name ends there rather than continuing."""
    if not boundary:
        return haystack.count(needle)
    total = 0
    at = haystack.find(needle)
    while at >= 0:
        before = haystack[at - 1] if at else ""
        after = haystack[at + len(needle) : at + len(needle) + 1]
        if before not in _NAME_CHARS and after not in _NAME_CHARS:
            total += 1
        at = haystack.find(needle, at + 1)
    return total


def _is_reference(value, needle, boundary):
    """Whether `value` could be a `uses:` naming `needle` -- the whole
    scalar, folded and stripped, never the name found inside prose. A
    reference carries no whitespace, so a workflow whose `name:` reads
    `mikelward/lanes trusted publisher` is not one."""
    value = value.strip().lower()
    if not value.startswith(needle) or any(char.isspace() for char in value):
        return False
    if not boundary:
        return True
    return value[len(needle) : len(needle) + 1] not in _NAME_CHARS


def _reference_count(text, needle, boundary=False):
    """How many strings in `text`'s document could be a `uses:` naming
    `needle`, for comparing against what a reader resolved.

    Counting the name wherever it appeared was the over-matching
    direction, which is safe for `mentions` -- there an extra mention only
    KEEPS a credential. Here it is the opposite: this count is compared
    against resolved references, and one nothing can resolve makes the
    file unread, so `repo setup` refuses every move and restriction that
    repository needs and its copies stay at repository level. A workflow
    named after the action did that (Codex, mikelward/repo#36), as did a
    neighbouring repository whose name merely starts the same way.

    A document PyYAML rejects still falls back to the raw substring count:
    nothing there can be resolved either, so over-counting is the
    fail-closed direction and the only one available."""
    needle = needle.lower()
    doc = _document(text)
    if doc is _REJECTED:
        return _occurrences(text.lower(), needle, boundary)
    return sum(1 for string in _strings(doc) if _is_reference(string, needle, boundary))


def _mention_count(text, needle):
    """How often `needle` occurs anywhere in the strings of `text`'s
    document -- keys and values, never a comment, since a comment calls
    nothing -- or in the raw text when PyYAML rejects the document.

    This is the over-counting direction, and it is only ever asked where
    over-counting KEEPS a credential; where a count is compared against
    what the reader resolved, `_reference_count` asks the narrower
    question instead. Case-folded, since owner and repository names are
    case-insensitive on GitHub (Codex, mikelward/repo#13)."""
    needle = needle.lower()
    doc = _document(text)
    if doc is _REJECTED:
        return _occurrences(text.lower(), needle, False)
    return sum(_occurrences(string.lower(), needle, False) for string in _strings(doc))


def _jobs(doc):
    """The jobs of a parsed workflow that are mappings, by name -- {} for
    a rejected document or a `jobs:` that is not a mapping."""
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        return {}
    return {name: job for name, job in doc["jobs"].items() if isinstance(job, dict)}


def _caller_verdicts(text, workflow_prefix, resolved=None):
    """One True/False per job in `text` calling the reusable workflow at
    `workflow_prefix` -- see `caller_inherits`.

    `resolved`, when given, is filled with the identity of every job
    mapping this resolved, which is what `unread_mentions` compares
    against. An anchored job aliased under a second name is ONE node:
    `_strings` visits a container once, so it contributes one mention
    while this list holds two verdicts, and the two disagreeing by one let
    an unresolved mention balance the totals -- the file then read as
    fully understood and setup deleted a credential out from under the
    caller it could not read. `_lanes_jobs` was given this discipline
    first and the generic path was left mixing node and occurrence counts
    (Codex, mikelward/repo#36, twice)."""
    prefix = workflow_prefix.lower()
    resolved = set() if resolved is None else resolved
    verdicts = []
    for job in _jobs(_document(text)).values():
        if isinstance(job.get("uses"), str) and job["uses"].lower().startswith(prefix):
            resolved.add(id(job))
            verdicts.append(job.get("secrets") == "inherit")
    return verdicts


def caller_inherits(text, workflow_prefix):
    """How the jobs in `text` that call the reusable workflow at
    `workflow_prefix` pass their secrets: None when none does, True when
    every such job passes `secrets: inherit`, False when any names its
    secrets instead (or passes none). Job-level only -- a step's `- uses:`
    never carries secrets, and a step is not a job.

    Owner and repository names are case-insensitive on GitHub, so
    `MikelWard/CI-Commit-Artifact/...` is the same caller; the whole
    reference is folded, which can only over-match, and over-matching is
    the safe direction here -- a caller read is still checked for
    `inherit` (Codex, mikelward/repo#13)."""
    verdicts = _caller_verdicts(text, workflow_prefix)
    if not verdicts:
        return None
    return all(verdicts)


def mentions(text, workflow_prefix):
    """Whether `text` refers to the reusable workflow at all, anywhere in
    its document -- the backstop for `caller_inherits`, which reads only a
    job-level `uses:`. A credential is deleted as unused only when no
    workflow so much as mentions its reader; a mention that did not
    resolve to a caller is "cannot tell", never "unused" (Codex,
    mikelward/repo#13). A comment does not count: a comment calls
    nothing."""
    return _mention_count(text, workflow_prefix) > 0


def _is_lanes(reference):
    """Whether a step's `uses:` names the lanes action, at any ref."""
    prefix = LANES_ACTION.lower()
    reference = reference.lower()
    return reference == prefix or reference.startswith(prefix + "@")


def _step_inputs(step):
    """The names of the inputs a step is handed -- the keys of its `with:`
    mapping, [] without one -- or None when `with:` is something else (a
    scalar, a sequence): a step this cannot read the inputs of. Read from
    the mapping itself rather than by searching the step for `app-id`: an
    `env:` or a `name:` carrying that word is not an input (Codex,
    mikelward/repo#36)."""
    inputs = step.get("with")
    if inputs is None:
        return []
    if not isinstance(inputs, dict):
        return None
    return [str(key) for key in inputs]


def _declares_environment(job, env):
    """Whether `job` declares `environment: <env>` -- inline, or as a
    mapping with `name: <env>` -- the way GitHub reads it.
    Case-insensitive, as environment names are."""
    declared = job.get("environment")
    if isinstance(declared, dict):
        declared = declared.get("name")
    return isinstance(declared, str) and declared.lower() == env.lower()


def _lanes_jobs(text):
    """For each job in `text` with a step using the lanes action: (how many
    such steps it has, whether one of them is handed both App inputs,
    whether the job declares the `lanes` environment, whether one is
    handed only half the pair). A job handing the action the credential is
    a publisher of the `lanes` status; one without is the ambient
    classify/gate pattern, which reads no secret. The inputs count only on
    the lanes step itself -- another action in the same job taking an
    input of that name (a token-minting step, say) hands lanes nothing
    (Codex, mikelward/repo#36)."""
    jobs = []
    for job in _jobs(_document(text)).values():
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        uses = 0
        publishes = False
        half = False
        for step in steps:
            if not isinstance(step, dict):
                continue
            reference = step.get("uses")
            if not isinstance(reference, str) or not _is_lanes(reference):
                continue
            inputs = _step_inputs(step)
            # Inputs this cannot read leave the step unresolved:
            # `lanes_unread` then reports the file, and nothing of its is
            # deleted.
            if inputs is None:
                continue
            uses += 1
            # Action input names are case-insensitive on GitHub, so `APP-ID:`
            # hands the action the credential as surely as `app-id:` does
            # (Codex, mikelward/repo#36).
            handed = {key.lower() for key in inputs} & LANES_INPUTS
            if handed == LANES_INPUTS:
                publishes = True
            elif handed:
                # One input without the other: the action cannot authenticate
                # as the App, so this publishes nothing -- and it is not the
                # ambient pattern either, since the pair is plainly meant
                # for it. Reported by `lanes_incomplete`; it keeps the pair
                # from reading as unused (Codex, mikelward/repo#36).
                half = True
        if not uses:
            continue
        jobs.append((uses, publishes, _declares_environment(job, LANES_ENV), half))
    return jobs


# The two inputs the lanes action authenticates as the App with; a step
# handing it one without the other publishes nothing.
LANES_INPUTS = frozenset({"app-id", "app-private-key"})


def lanes_publishers(texts):
    """Every workflow in `texts` (name -> text) with a job that hands the
    lanes action the App pair -- the jobs that publish the required status
    as the App -- and, for each, whether every such job declares the
    `lanes` environment. False is the finding: without the declaration the
    environment's secret never reaches the job, `init` fails outright and
    `gate` silently falls back to the ambient check-run (mikelward/lanes's
    README, "not optional")."""
    found = {}
    for name, text in texts.items():
        publishing = [declares for _uses, publishes, declares, _half in _lanes_jobs(text) if publishes]
        if publishing:
            found[name] = all(publishing)
    return found


def lanes_incomplete(texts):
    """The workflows with a job handing the lanes action one of the two App
    inputs without the other -- a step that cannot authenticate as the App
    and so publishes nothing, while plainly meant to. A finding in both
    commands: the audit names the missing input, and setup holds the pair
    where it is rather than moving, deleting or restricting around a
    publisher that does not publish (Codex, mikelward/repo#36)."""
    return sorted(
        name
        for name, text in texts.items()
        if any(half for _uses, _publishes, _declares, half in _lanes_jobs(text))
    )


def lanes_unread(texts):
    """The workflows that mention the lanes action more often than the
    reader resolved a step using it: "cannot tell", the same backstop
    `unread_mentions` is for a reusable workflow. A mention in a document
    PyYAML rejects, or on a step whose `with:` is not a mapping, may be a
    publisher, so a credential is never deleted as unused over one."""
    return sorted(
        name
        for name, text in texts.items()
        if _reference_count(text, LANES_ACTION) > sum(uses for uses, _p, _d, _h in _lanes_jobs(text))
    )


def environment_branch_policy(repo, listed):
    """Which branches may reach environment `listed` on `repo`: "open" when
    any branch can (no deployment branch policy), "protected" when only
    branches with branch protection can, or the sorted list of the custom
    policies' patterns -- a tag policy is listed as `tag:<pattern>`, since a
    tag named after the default branch is not the branch. Raises ReadError."""
    try:
        text = gh.run(["api", f"repos/{repo}/environments/{listed}"])
    except gh.GhError as e:
        raise ReadError(f"could not read {repo}'s '{listed}' environment:", e.stderr)
    try:
        policy = json.loads(text).get("deployment_branch_policy")
    except (ValueError, AttributeError) as e:
        raise ReadError(f"could not read {repo}'s '{listed}' environment:", str(e))
    if not policy:
        return "open"
    if policy.get("protected_branches"):
        return "protected"
    return _branch_policy_patterns(repo, listed)


def _branch_policy_patterns(repo, listed):
    """The custom policy's patterns, for a caller that has already read the
    environment and found it in custom mode. Split out so the rollback can
    confirm the list without re-reading the environment it just read: every
    extra round trip between a fact and the write acting on it is more time
    for that fact to go stale, and the settings the write resends were
    two round trips old (Codex, mikelward/repo#36). Raises ReadError."""
    try:
        entries = gh.run(
            [
                "api",
                "--paginate",
                f"repos/{repo}/environments/{listed}/deployment-branch-policies",
                "--jq",
                '.branch_policies[] | "\(.type // "branch") \(.name)"',
            ]
        ).splitlines()
    except gh.GhError as e:
        raise ReadError(f"could not read {repo}'s '{listed}' environment's branch policies:", e.stderr)
    patterns = []
    for entry in entries:
        kind, _, name = entry.strip().partition(" ")
        if not name:
            continue
        patterns.append(name if kind == "branch" else f"{kind}:{name}")
    return sorted(patterns)


def branch_policy_verdict(policy, default):
    """None when `policy` (as `environment_branch_policy` reports it) admits
    only the trusted base branch -- a custom policy naming exactly `default`
    -- else what is wrong with it, in prose. "Protected branches only" is
    not that: GitHub reads it as every branch while the repository has no
    branch-protection rule (this fleet protects `main` with a ruleset, which
    is not one), and as every protected branch otherwise (Codex,
    mikelward/repo#36)."""
    if policy == "open":
        return "can be reached from any branch"
    if policy == "protected":
        return (
            "admits protected branches, which is every branch while no branch-protection "
            "rule exists (a ruleset is not one) and every protected branch otherwise"
        )
    if policy == [default]:
        return None
    if not policy:
        return "admits no branch at all"
    return f"is restricted to {', '.join(repr(p) for p in policy)}, not to {default!r} alone"


class RestrictRefused(Exception):
    """`restrict_environment` found, on its apply-time re-read, a policy it
    may not rewrite -- one someone set while the plan waited -- so the
    environment is still not restricted to the default branch. The caller
    reports it as a failure: a run that exits 0 here would leave a state
    the audit reports as a finding (Codex, mikelward/repo#36)."""


def restrictable(policy):
    """Whether `repo setup` may restrict an environment whose policy reads
    `policy`: one open to every branch, or one in custom-policy mode
    naming no branch at all -- the state a restriction left in when its
    second write failed (Codex, mikelward/repo#36), which nobody sets on
    purpose since it admits nothing. Anything else is somebody's choice."""
    return policy == "open" or policy == []


def _environment_settings(current):
    """The protection settings the environment PUT must resend, read from
    the environment object `current`: GitHub applies its default to every
    setting the request omits, so a PUT that names only the branch policy
    would reset the wait timer, the reviewers, self-review and the
    administrator bypass (Codex, mikelward/repo#36, for the last)."""
    body = {}
    for rule in current.get("protection_rules") or []:
        if rule.get("type") == "wait_timer" and rule.get("wait_timer") is not None:
            body["wait_timer"] = rule["wait_timer"]
        if rule.get("type") == "required_reviewers":
            body["reviewers"] = [
                {"type": r["type"], "id": r["reviewer"]["id"]}
                for r in rule.get("reviewers") or []
                if r.get("reviewer") and "id" in r["reviewer"]
            ]
            if rule.get("prevent_self_review") is not None:
                body["prevent_self_review"] = rule["prevent_self_review"]
    if current.get("can_admins_bypass") is not None:
        body["can_admins_bypass"] = current["can_admins_bypass"]
    return body


def _policy_mode(current):
    """The deployment-branch-policy mode an environment GET reports:
    "open" (no policy), "protected", or "custom"."""
    policy = current.get("deployment_branch_policy") or None
    if not policy:
        return "open"
    return "protected" if policy.get("protected_branches") else "custom"


def restrict_environment(repo, listed, default):
    """Restricts environment `listed` to the branch `default`, when it is
    open to every branch (or left in custom-policy mode naming no branch);
    returns what it did, in prose. Re-reads the policy first, since the
    confirmation prompt can have sat: a policy someone set meanwhile is
    left alone and raised as `RestrictRefused`, never overwritten. The PUT resends the
    environment's own protection settings (`_environment_settings`),
    because GitHub resets every one the request omits -- the same trap
    secrets_cmd._ensure_environment documents. Two writes, and the second
    can fail after the first: then the policy is put back as it was, so a
    failure leaves the environment open rather than admitting nothing
    (Codex, mikelward/repo#36) -- unless a policy someone set meanwhile is
    there to lose: the list is re-read first, and a pattern added between
    the two writes is left in place rather than reopened over (Codex,
    mikelward/repo#36 again). If even the restore fails, the next run
    still completes it, since custom mode naming no branch is
    `restrictable`. Raises gh.GhError on a failed write."""
    policy = environment_branch_policy(repo, listed)
    if not restrictable(policy):
        # Settled in the meantime, or set to something else: the first is
        # done, the second is the finding the audit would report, and
        # exiting 0 over it would say the run left the repository in shape.
        if policy == [default]:
            return f"environment '{listed}' was restricted to branch '{default}' since the plan was built"
        raise RestrictRefused(
            f"environment '{listed}' {branch_policy_verdict(policy, default)} -- set since the plan "
            f"was built, and a policy someone set is not rewritten; restrict it to '{default}' by hand"
        )
    endpoint = f"repos/{repo}/environments/{listed}"
    changed = RestrictRefused(
        f"environment '{listed}' changed its deployment branch policy between two reads of it -- "
        f"set since the plan was built, and a policy someone set is not rewritten; restrict it to "
        f"'{default}' by hand"
    )
    settings = None
    if policy == "open":
        try:
            current = json.loads(gh.run(["api", endpoint]))
        except (gh.GhError, ValueError) as e:
            raise ReadError(f"could not read {repo}'s '{listed}' environment:", getattr(e, "stderr", str(e)))
        # The read that supplies the settings is the last one before the
        # PUT, so it is the one the mode is held to: a policy set between
        # the policy read above and this one would otherwise be overwritten
        # (Codex, mikelward/repo#36).
        if _policy_mode(current) != "open":
            raise changed
        settings = _environment_settings(current)
    else:
        # Custom mode naming no branch needs no PUT, so the last read before
        # its one write is the policy list itself, re-read here: a pattern
        # someone added meanwhile would otherwise gain the default branch
        # beside it (Codex, mikelward/repo#36).
        again = environment_branch_policy(repo, listed)
        if again == [default]:
            return f"environment '{listed}' was restricted to branch '{default}' since the plan was built"
        if again != []:
            raise changed
    if policy == "open":
        gh.run_with_input(
            ["api", "--method", "PUT", endpoint, "--input", "-"],
            json.dumps(
                {**settings, "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
            ).encode(),
        )
    try:
        gh.run_with_input(
            ["api", "--method", "POST", f"{endpoint}/deployment-branch-policies", "--input", "-"],
            json.dumps({"name": default, "type": "branch"}).encode(),
        )
    except gh.GhError as e:
        # The POST fails when someone else installed the branch policy
        # between the re-read above and this write, and then the state that
        # matters is the environment's, not this run's exit code -- whichever
        # mode the run started in. Reading it only on the "open" path
        # reported failure over a policy that was already installed, for a
        # run that started at custom-mode-naming-nothing and so had no PUT
        # of its own to undo (Codex, mikelward/repo#36).
        try:
            now = environment_branch_policy(repo, listed)
        except ReadError as unread:
            if policy != "open":
                # Nothing was written, so there is nothing to restore and
                # the environment is as the run found it: the POST's own
                # failure is the whole story.
                raise
            raise gh.GhError(
                f"{e.stderr.rstrip()}\n(and the branch policies could not be re-read afterwards, so the "
                f"environment was left in custom-policy mode naming no branch until the next `repo setup` "
                f"run completes it: {unread.message} {(unread.detail or '').strip()})\n"
            ) from e
        if now == [default]:
            return (
                f"restricted environment '{listed}' to branch '{default}' (the branch policy write "
                f"failed, and the branch was added meanwhile)"
            )
        if policy == "open":
            # Back to open: custom mode with no branch named admits nothing,
            # which would break the publisher this exists to protect. But
            # only over a list still empty: a pattern someone added between
            # the two writes is their choice, and the restore PUT would drop
            # it and reopen the environment to every branch -- so it is left
            # alone with the reason (custom mode naming no branch is what
            # the next run completes). A failure here is the same failure,
            # reported once.
            if now != []:
                # A mode is one value and a custom policy is a list of
                # patterns; joining over the first spells it one character
                # at a time.
                said = ", ".join(repr(p) for p in now) if isinstance(now, list) else repr(now)
                raise gh.GhError(
                    f"{e.stderr.rstrip()}\n(a policy someone set meanwhile -- {said} -- was left in "
                    f"place rather than reopened over; restrict the environment to '{default}' by hand)\n"
                ) from e
            # The settings snapshot was taken before the PUT and this
            # restore can be a failed write later, so it is re-read: the
            # PUT resends every protection setting, and the branch-policy
            # re-read above sees none of them, so a wait timer, reviewer,
            # self-review or administrator-bypass change made meanwhile
            # would be silently reverted by the restore (Codex,
            # mikelward/repo#36). A read that fails leaves the environment
            # in custom mode naming no branch, like the other unreadable
            # cases here -- the next run completes it, and that is better
            # than writing back settings already known to be stale.
            try:
                fresh = json.loads(gh.run(["api", endpoint]))
            except (gh.GhError, ValueError) as unread:
                raise gh.GhError(
                    f"{e.stderr.rstrip()}\n(and the environment's protection settings could not be re-read, "
                    f"so the open policy was not restored over a stale copy of them and the environment is "
                    f"left in custom-policy mode naming no branch until the next `repo setup` run completes "
                    f"it: {getattr(unread, 'stderr', str(unread)).rstrip()})\n"
                ) from e
            # That read reports the mode as well as the settings, and the
            # mode is the thing about to be overwritten: the branch-policy
            # list above says nothing about it, so an administrator
            # switching to protected mode in between would have been
            # reopened over by a restore that only looked at the settings
            # it came for (Codex, mikelward/repo#36). The half-completed
            # state this restores from is custom mode; anything else is
            # somebody's, and is left alone like a pattern they added.
            if _policy_mode(fresh) != "custom":
                raise gh.GhError(
                    f"{e.stderr.rstrip()}\n(and the environment's policy mode was set to "
                    f"{_policy_mode(fresh)!r} meanwhile, so the open policy was not restored over it; "
                    f"restrict the environment to '{default}' by hand)\n"
                ) from e
            # And the list is confirmed LAST, immediately before the write
            # that acts on it. The refusal above rests on a listing taken
            # before the settings read, and that read is a round trip: a
            # policy added across it was deleted by this PUT and the
            # environment reopened -- someone's deliberate act destroyed by
            # the rollback that exists to disturb nothing (Codex,
            # mikelward/repo#36).
            #
            # Which fact goes last is not arbitrary. Alternating these
            # reads cannot make both current, so the one that goes last is
            # the one whose staleness costs more: a stale LIST reopens the
            # environment over a policy somebody just set, exposing the
            # credential; a stale SETTINGS reverts a wait timer or reviewer
            # change, which is somebody's edit lost but nothing exposed.
            # What is not a trade-off is the number of round trips between
            # them, and the settings were two old because this confirmation
            # re-read the environment `fresh` had already read -- so it
            # lists the patterns directly and the gap is one call, the
            # minimum any confirm-then-write has (Codex, mikelward/repo#36).
            # Whether the open policy should be restored AT ALL is the
            # design question underneath, and it is the maintainer's:
            # TODO.md carries it.
            try:
                still = _branch_policy_patterns(repo, listed)
            except ReadError as unread:
                raise gh.GhError(
                    f"{e.stderr.rstrip()}\n(and the branch policies could not be confirmed empty before "
                    f"restoring the open policy, so it was not restored and the environment is left in "
                    f"custom-policy mode naming no branch until the next `repo setup` run completes it: "
                    f"{unread.message} {(unread.detail or '').strip()})\n"
                ) from e
            if still != []:
                said = ", ".join(repr(p) for p in still) if isinstance(still, list) else repr(still)
                raise gh.GhError(
                    f"{e.stderr.rstrip()}\n(a policy someone set meanwhile -- {said} -- was left in "
                    f"place rather than reopened over; restrict the environment to '{default}' by hand)\n"
                ) from e
            try:
                gh.run_with_input(
                    ["api", "--method", "PUT", endpoint, "--input", "-"],
                    json.dumps({**_environment_settings(fresh), "deployment_branch_policy": None}).encode(),
                )
            except gh.GhError as undo:
                raise gh.GhError(
                    f"{e.stderr.rstrip()}\n(and restoring the open policy failed too, leaving the environment "
                    f"admitting no branch until the next `repo setup` run completes it: {undo.stderr.rstrip()})\n"
                ) from e
        raise
    # Both writes landed, which is not the same as the environment being
    # restricted: an administrator adding another custom policy between
    # them leaves `[default, other]`, and reporting success over that says
    # the run left an environment the audit would report a finding on
    # (Codex, mikelward/repo#36). The postcondition is the thing this
    # function exists for, so it is checked rather than inferred from two
    # exit codes. The other policy is left in place -- someone set it, and
    # this deletes nobody's -- and named, so the hand fix is one step.
    try:
        settled = environment_branch_policy(repo, listed)
    except ReadError as unread:
        raise gh.GhError(
            f"could not confirm environment '{listed}' admits only '{default}' after restricting it: "
            f"{unread.message} {(unread.detail or '').strip()}\n"
        ) from None
    if settled != [default]:
        raise RestrictRefused(
            f"environment '{listed}' still admits {', '.join(repr(p) for p in settled) or 'no branch'} "
            f"after adding '{default}' -- a policy set while this ran, left in place rather than "
            f"deleted; remove it by hand so only '{default}' remains"
        )
    return f"restricted environment '{listed}' to branch '{default}'"


def delete_secret(repo, name, env):
    """Deletes secret `name` from `repo` -- from environment `env` when
    given, else the repository level. Raises gh.GhError on failure."""
    if env:
        endpoint = f"repos/{repo}/environments/{env}/secrets/{name}"
    else:
        endpoint = f"repos/{repo}/actions/secrets/{name}"
    gh.run(["api", "--method", "DELETE", endpoint])
