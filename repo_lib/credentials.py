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

Names only, throughout: GitHub never returns a secret's value, which is
why `repo setup` needs the value handed to it to complete a move.
"""

import base64
import dataclasses
import re
import urllib.parse

from repo_lib import gh

BATCH_HUBS = ("npm-update", "gradle-update", "rust-update")

COMMIT_ARTIFACT_TOKEN = "CI_COMMIT_ARTIFACT_TOKEN"
COMMIT_ARTIFACT_ENV = "ci-commit-artifact"
# The `uses:` prefix that identifies a caller of the commit-back workflow.
COMMIT_ARTIFACT_WORKFLOW = "mikelward/ci-commit-artifact/"


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
    prefix = workflow_prefix.lower()
    return sorted(
        name
        for name, text in texts.items()
        if _content(text).lower().count(prefix) > len(_caller_verdicts(text, workflow_prefix))
    )


FLEET_CREDENTIALS = tuple(name for hub in BATCH_HUBS for name in batch_credentials(hub)) + (
    COMMIT_ARTIFACT_TOKEN,
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
    return None


def usable(names, hub):
    """Whether `names` hold a credential the hub's publish job can open a
    pull request with: the PAT, or both halves of the App pair."""
    pat, app_id, app_key = batch_credentials(hub)
    return pat in names or (app_id in names and app_key in names)


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


def workflow_texts(repo):
    """Every workflow's text: the default branch's under its file name,
    and, under `<name> on <branch>`, each workflow on another branch whose
    blob differs from the default branch's copy (new there, or changed).
    A push-triggered workflow runs from its own branch, so a caller that
    exists only on a branch still needs its credential, and a reading of
    the default branch alone would delete it as unused (Codex,
    mikelward/repo#13). Unchanged copies are not re-read: the same blob
    reads the same."""
    default = default_branch(repo)
    entries = workflow_entries(repo)
    texts = {
        WorkflowName(name): workflow_text(repo, name)
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


# The key and the value may each be quoted -- YAML allows both, and a
# quoted reference is still the caller (Codex, mikelward/repo#13: read
# unquoted, a quoted commit-back caller made its token look unused, and
# setup deleted it). What this cannot read -- flow style, an anchor, a
# multi-line scalar -- is what `mentions` is for: a decision to delete
# needs the workflow absent from the text altogether, not merely unparsed.
_USES_RE = re.compile(r"""^( *)["']?uses["']?: *["']?([^"'\s]+)["']? *(?:#.*)?$""")
_SECRETS_INHERIT_RE = re.compile(r"""^( *)["']?secrets["']?: *["']?inherit["']? *(?:#.*)?$""")


def _meaningful(line):
    stripped = line.strip()
    return stripped and not stripped.startswith("#")


_DOUBLE_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
_ESCAPE_RE = re.compile(r"\\(\r?\n[ \t]*|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)", re.S)
_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
    "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\u0085",
    "_": "\u00a0", "L": "\u2028", "P": "\u2029",
}


def _decode_double_quoted(text):
    """`text` with the YAML escapes of every double-quoted scalar decoded,
    so `"mikelward\\/npm-up\\u0064ate/..."` reads as the reference YAML
    makes of it. A caller written that way is valid, and a reader that
    missed it would delete its credential as unused (Codex,
    mikelward/repo#13). An escape YAML does not define is left as written."""

    def escape(m):
        code = m.group(1)
        if code.startswith(("\n", "\r\n")):
            # An escaped line break, LF or CRLF: the value continues on the
            # next line, its indentation dropped, with nothing in between.
            return ""
        if code in _ESCAPES:
            return _ESCAPES[code]
        if code[0] in "xuU" and len(code) > 1:
            return chr(int(code[1:], 16))
        return m.group(0)

    return _DOUBLE_QUOTED_RE.sub(lambda m: '"' + _ESCAPE_RE.sub(escape, m.group(1)) + '"', text)


def _strip_comments(text):
    """`text` without its YAML comments -- a whole line, or a trailing
    ` #...` -- outside quoted scalars, which can carry a `#` and can span
    lines (an escaped line break in a double-quoted one, or a plain
    newline in either), so the quote state is tracked across the whole
    text rather than a line at a time. A quote opens a scalar only at a
    value position (after whitespace, `:`, `-`, `[`, `{` or `,`): the
    apostrophe in `it's` starts nothing."""
    out = []
    quote = None
    prev = "\n"
    i = 0
    while i < len(text):
        c = text[i]
        if quote == '"':
            if c == "\\" and i + 1 < len(text):
                out.append(text[i : i + 2])
                i += 2
                continue
            if c == '"':
                quote = None
        elif quote == "'":
            if c == "'":
                if text[i + 1 : i + 2] == "'":
                    out.append("''")
                    i += 2
                    continue
                quote = None
        elif c == "#" and prev in " \t\n":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif c in "\"'" and prev in " \t\n:-[{,":
            quote = c
        out.append(c)
        prev = c
        i += 1
    return "".join(out)


def _content(text):
    """`text` with its comments dropped and its double-quoted scalars
    decoded: a comment cannot call anything, so a reference in one --
    whole-line or trailing -- is neither a caller nor a mention that needs
    reading (Codex, mikelward/repo#13, for the trailing one)."""
    return _decode_double_quoted(_strip_comments(text))


def _caller_verdicts(text, workflow_prefix):
    """One True/False per job in `text` calling the reusable workflow at
    `workflow_prefix` -- see `caller_inherits`."""
    lines = _content(text).splitlines()
    verdicts = []
    prefix = workflow_prefix.lower()
    for index, line in enumerate(lines):
        m = _USES_RE.match(line)
        if not m or not m.group(2).lower().startswith(prefix):
            continue
        indent = len(m.group(1))
        start = index
        while start > 0 and (not _meaningful(lines[start - 1]) or _indent(lines[start - 1]) >= indent):
            start -= 1
        end = index + 1
        while end < len(lines) and (not _meaningful(lines[end]) or _indent(lines[end]) >= indent):
            end += 1
        verdicts.append(
            any(
                (sm := _SECRETS_INHERIT_RE.match(candidate)) and len(sm.group(1)) == indent
                for candidate in lines[start:end]
            )
        )
    return verdicts


def caller_inherits(text, workflow_prefix):
    """How the jobs in `text` that call the reusable workflow at
    `workflow_prefix` pass their secrets: None when none does, True when
    every such job passes `secrets: inherit`, False when any names its
    secrets instead (or passes none). Job-level only -- a step's `- uses:`
    never carries secrets, and the leading dash keeps it from matching.

    A job's block is the lines indented deeper than its key; the `uses:`
    sits directly under that key, so `secrets:` at the same indentation
    inside the block is the job's own. Text, not YAML: enough for the
    fleet's own callers, which this exists to read.

    Owner and repository names are case-insensitive on GitHub, so
    `MikelWard/CI-Commit-Artifact/...` is the same caller; the whole
    reference is folded, which can only over-match, and over-matching is
    the safe direction here -- a caller read is still checked for
    `inherit` (Codex, mikelward/repo#13)."""
    verdicts = _caller_verdicts(text, workflow_prefix)
    if not verdicts:
        return None
    return all(verdicts)


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def mentions(text, workflow_prefix):
    """Whether `text` refers to the reusable workflow at all, in any shape
    -- the backstop for `caller_inherits`, which reads only the shapes the
    fleet writes. A credential is deleted as unused only when no workflow
    so much as mentions its reader; a mention the structured reading did
    not resolve to a caller is "cannot tell", never "unused" (Codex,
    mikelward/repo#13). Case-folded, since owner and repository names are
    case-insensitive on GitHub and a mention that keeps a credential is the
    safe direction to over-match in, and the double-quoted scalars decoded
    first, since an escaped reference is the same reference. Full-line
    comments do not count: a comment calls nothing."""
    return workflow_prefix.lower() in _content(text).lower()


def delete_secret(repo, name, env):
    """Deletes secret `name` from `repo` -- from environment `env` when
    given, else the repository level. Raises gh.GhError on failure."""
    if env:
        endpoint = f"repos/{repo}/environments/{env}/secrets/{name}"
    else:
        endpoint = f"repos/{repo}/actions/secrets/{name}"
    gh.run(["api", "--method", "DELETE", endpoint])
