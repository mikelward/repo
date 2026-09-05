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

import json

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
    prefix = workflow_prefix.lower()
    return sorted(
        name
        for name, text in texts.items()
        if _content(text).lower().count(prefix) > len(_caller_verdicts(text, workflow_prefix))
    )


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
        if code in ('"', "\\"):
            # An escaped quote or backslash stays as written: the flow
            # reader below walks a decoded line by its quote state, and a
            # bare `"` decoded into the middle of a scalar would end the
            # scalar early -- everything after it read as keys. No
            # reference this decodes for carries either.
            return m.group(0)
        if code in _ESCAPES:
            # A decoded line break stays on its line: the readers here walk
            # the text by line and indentation, and a `\n` inside a
            # `run:` block scalar (`"${ids%%$'\n'*}"`, in one consumer)
            # would otherwise split the job it sits in, ending every job
            # after it early. No reference this decodes for can carry one.
            return " " if _ESCAPES[code] in ("\n", "\r") else _ESCAPES[code]
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


# YAML allows separation whitespace before a mapping's `:`, so every key
# here accepts it -- a reader that rejected `app-id :` read a publisher as
# the ambient pattern and deleted its pair (Codex, mikelward/repo#36).
# Group 1 is everything before the key -- indentation and the item's dash,
# when the key sits on the dash line -- so the key's column is its length
# whether or not the key is quoted; searching the line for `uses` found a
# quoted key one column late, and the step's `with:` beneath it went
# unread (Codex, mikelward/repo#36).
_STEP_USES_RE = re.compile(r"""^( *(?:- +)?)["']?uses["']?\s*: *["']?([^"'\s]+)["']? *$""")
_JOBS_RE = re.compile(r"""^["']?jobs["']?\s*: *$""")
# A plain key never starts with a node property or indicator (`&anchor`,
# `*alias`, `!tag`, `? complex`): an entry that does is one the reader does
# not read, and says so rather than reading the property as the key.
_KEY_RE = re.compile(r"""^( *)(?:["']([^"']+)["']|([^"'\s:&*!?][^"'\s:]*))\s*:( .*)?$""")
_WITH_RE = re.compile(r"""^( *)["']?with["']?\s*:(.*)$""")


def _flow_entries(inner):
    """The entries of a flow mapping's inside (`a: 1, b: "x, y", c: {d: 2}`
    without its braces), split on the commas that separate them -- not on
    one inside a quoted scalar or a nested collection -- or None when the
    text cannot be read as one: an unclosed quote or bracket. A quote opens
    a scalar only where a scalar can start (at an entry's start, or after
    the `:` that ends its key), so the apostrophe in `it's` starts
    nothing. Inside a double-quoted scalar a backslash escapes the next
    character -- `_decode_double_quoted` leaves `\\"` and `\\\\` as written
    for exactly this reason."""
    entries = []
    entry = []
    depth = 0
    quote = None
    i = 0
    while i < len(inner):
        c = inner[i]
        if quote == '"':
            if c == "\\" and i + 1 < len(inner):
                entry.append(inner[i : i + 2])
                i += 2
                continue
            if c == '"':
                quote = None
        elif quote == "'":
            if c == "'":
                if inner[i + 1 : i + 2] == "'":
                    entry.append("''")
                    i += 2
                    continue
                quote = None
        elif c in "\"'&*!":
            # A scalar can start at the entry's start, or after the `:`
            # ending a key, a `,` or an opening bracket inside a nested
            # collection -- a quoted item of a nested sequence carries the
            # same commas and brackets an unquoted one would be read by
            # (Codex, mikelward/repo#36). A node property or alias in that
            # position (`&x "..."`, `*x`, `!!str`) is a shape this does not
            # read at all: it can precede a quoted scalar whose quote would
            # otherwise never open, so the whole mapping is "cannot tell".
            before = "".join(entry).rstrip()
            if before == "" or before[-1] in ":,[{":
                if c in "&*!":
                    return None
                quote = c
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth < 0:
                return None
        elif c == "," and depth == 0:
            entries.append("".join(entry))
            entry = []
            i += 1
            continue
        entry.append(c)
        i += 1
    if quote is not None or depth != 0:
        return None
    entries.append("".join(entry))
    return entries


def _flow_entry_key(entry):
    """The key of one flow-mapping entry (`key: value`, the key plain or
    quoted), or None when the entry has no key the reader can see -- a
    flow-set entry, or a shape this does not read."""
    text = entry.strip()
    if not text:
        return None
    if text[0] in "\"'":
        q = text[0]
        i = 1
        while i < len(text):
            if q == '"' and text[i] == "\\":
                i += 2
                continue
            if text[i] == q:
                if q == "'" and text[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                break
            i += 1
        else:
            return None
        key = text[1:i].replace("''", "'") if q == "'" else text[1:i]
        rest = text[i + 1 :].lstrip()
        return key if rest.startswith(":") else None
    # A plain key ends at the first `: ` (or a `:` ending the entry); a
    # `:` with no space after it is part of the scalar, as in a URL.
    # The same rule as `_KEY_RE`: a plain key never starts with a node
    # property or indicator (`&anchor`, `*alias`, `!tag`, `? complex`), and
    # an entry that does is one this reader does not read.
    m = re.match(r"([^{}\[\],&*!?][^{}\[\],]*?)\s*:(?:\s|$)", text)
    return m.group(1).strip() if m else None


def _flow_keys(inner):
    """The top-level keys of a flow mapping's inside, in order, or None when
    it cannot be read: an unclosed quote or bracket, or an entry with no
    key. Read by walking the text with its quote and nesting state rather
    than by a regex over it: a regex found `app-id:` inside a quoted VALUE
    and read the step as a publisher, and the deleted pair is the reason
    "cannot tell" has to be the answer to every shape this does not read
    (Codex, mikelward/repo#36)."""
    entries = _flow_entries(inner)
    if entries is None:
        return None
    keys = []
    for index, entry in enumerate(entries):
        if not entry.strip():
            # A trailing comma leaves an empty last entry, which YAML
            # allows; an empty one anywhere else is not a mapping.
            if index == len(entries) - 1:
                continue
            return None
        key = _flow_entry_key(entry)
        if key is None:
            return None
        keys.append(key)
    return keys


def _step_inputs(step, key_indent):
    """The keys of the step's `with:` mapping -- the inputs the action is
    actually handed -- or None when the step names inputs in a shape this
    cannot read. Block form: the keys at the first indentation under
    `with:`. Flow form (`with: {a: 1, b: 2}`, on one line): the keys inside
    the braces. Anything else as the value -- an alias, a scalar -- is
    unreadable. Read from the mapping itself rather than by searching the
    step's lines for `app-id:`: an `env:` or a `name:` carrying that word
    is not an input, and three readings by search each let something else
    in (Codex, mikelward/repo#36)."""
    for n, line in enumerate(step):
        w = _WITH_RE.match(line)
        if not w or len(w.group(1)) != key_indent:
            continue
        value = w.group(2).strip()
        if not value:
            child = None
            keys = []
            for later in step[n + 1 :]:
                if not _meaningful(later):
                    continue
                indent = _indent(later)
                if indent <= key_indent:
                    break
                if child is None:
                    child = indent
                if indent != child:
                    continue
                k = _KEY_RE.match(later)
                if not k:
                    # An entry at the mapping's own level that is not a
                    # plain or quoted key -- an anchored one (`&x app-id:`),
                    # a complex one (`? ...`) -- is a key this cannot read,
                    # and a mapping with an unread key is not a read
                    # mapping: "cannot tell", never a list short of one
                    # (Codex, mikelward/repo#36).
                    return None
                keys.append(k.group(2) or k.group(3))
            return keys
        if value.startswith("{") and value.endswith("}"):
            return _flow_keys(value[1:-1])
        return None
    return []
_ENVIRONMENT_RE = re.compile(r"""^( *)["']?environment["']?\s*: *(?:["']?([^"'\s]*)["']?)? *$""")
_ENV_NAME_RE = re.compile(r"""^( *)["']?name["']?\s*: *["']?([^"'\s]+)["']? *$""")


def _job_blocks(lines):
    """(start, end) line ranges of the jobs under a block-style `jobs:`
    key, each starting at the job's own key: a job is a key at the
    indentation of the first one, and runs until the next key at that
    indentation or the first line shallower than it. Text, not YAML,
    like `_caller_verdicts` -- enough for the shapes the fleet writes, and
    `lanes_unread` is the backstop for the rest."""
    start = next((i for i, line in enumerate(lines) if _JOBS_RE.match(line)), None)
    if start is None:
        return []
    body = [i for i in range(start + 1, len(lines)) if _meaningful(lines[i])]
    if not body or _indent(lines[body[0]]) == 0:
        return []
    job_indent = _indent(lines[body[0]])
    keys = []
    for i in body:
        indent = _indent(lines[i])
        if indent < job_indent:
            break
        if indent == job_indent and _KEY_RE.match(lines[i]):
            keys.append(i)
    end_of_jobs = next(
        (i for i in body if _indent(lines[i]) < job_indent), len(lines)
    )
    return [(key, keys[n + 1] if n + 1 < len(keys) else end_of_jobs) for n, key in enumerate(keys)]


def _declares_environment(lines, block, env):
    """Whether the job at `block` declares `environment: <env>` -- inline,
    or as a block with `name: <env>` under it -- at its own indentation, the
    way GitHub reads it. Case-insensitive, as environment names are."""
    start, end = block
    key_indent = _indent(lines[start])
    child_indent = next(
        (_indent(lines[i]) for i in range(start + 1, end) if _meaningful(lines[i])), None
    )
    if child_indent is None or child_indent <= key_indent:
        return False
    for i in range(start + 1, end):
        m = _ENVIRONMENT_RE.match(lines[i])
        if not m or len(m.group(1)) != child_indent:
            continue
        if m.group(2):
            return m.group(2).lower() == env.lower()
        for j in range(i + 1, end):
            if not _meaningful(lines[j]):
                continue
            if _indent(lines[j]) <= child_indent:
                break
            n = _ENV_NAME_RE.match(lines[j])
            if n and n.group(2).lower() == env.lower():
                return True
        return False
    return False


def _step_block(lines, uses, start, end):
    """The line range of the step whose `uses:` is at line `uses`, inside
    the job block (start, end): the item's own dash line, when the `uses:`
    is not on it, through every line indented at or past the `uses:` key.
    The next step's dash sits two columns shallower than the key, and a
    job-level key shallower still, so either ends it."""
    key_indent = len(_STEP_USES_RE.match(lines[uses]).group(1))
    first = uses
    # A `uses:` on the item's own dash line starts the step; only one on a
    # later line has the item's earlier lines -- back to its dash -- to
    # collect. Walking back from a dash line would collect the PREVIOUS
    # step's body, whose `with:` is indented past this key too.
    on_dash = lines[uses].lstrip().startswith("-")
    while not on_dash and first > start + 1:
        prev = lines[first - 1]
        if not _meaningful(prev):
            first -= 1
            continue
        if _indent(prev) >= key_indent:
            first -= 1
            continue
        if _indent(prev) == key_indent - 2 and prev.lstrip().startswith("-"):
            first -= 1
        break
    last = uses + 1
    while last < end and (not _meaningful(lines[last]) or _indent(lines[last]) >= key_indent):
        last += 1
    return first, last, key_indent


def _lanes_jobs(text):
    """For each job in `text` with a step using the lanes action:
    (how many such steps it has, whether one of them is handed `app-id`,
    whether the job declares the `lanes` environment). A job handing the
    action the credential is a publisher of the `lanes` status; one without
    is the ambient classify/gate pattern, which reads no secret. `app-id`
    counts only inside the lanes step's own block -- another action in the
    same job taking an input of that name (a token-minting step, say) hands
    lanes nothing (Codex, mikelward/repo#36)."""
    lines = _content(text).splitlines()
    prefix = LANES_ACTION.lower()
    jobs = []
    for start, end in _job_blocks(lines):
        uses = 0
        publishes = False
        half = False
        for i in range(start + 1, end):
            m = _STEP_USES_RE.match(lines[i])
            if not m or not (m.group(2).lower() == prefix or m.group(2).lower().startswith(prefix + "@")):
                continue
            first, last, key_indent = _step_block(lines, i, start, end)
            step = [lines[j] for j in range(first, last) if j != i]
            inputs = _step_inputs(step, key_indent)
            # Inputs this cannot read -- an alias, say -- leave the step
            # unresolved: `lanes_unread` then reports the file, and nothing
            # of its is deleted.
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
        jobs.append((uses, publishes, _declares_environment(lines, (start, end), LANES_ENV), half))
    return jobs


# The two inputs the lanes action authenticates as the App with; a step
# handing it one without the other publishes nothing.
LANES_INPUTS = frozenset({"app-id", "app-private-key"})


def lanes_publishers(texts):
    """Every workflow in `texts` (name -> text) with a job that hands the
    lanes action `app-id` -- the jobs that publish the required status as
    the App -- and, for each, whether every such job declares the `lanes`
    environment. False is the finding: without the declaration the
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
    `unread_mentions` is for a reusable workflow. A mention in a shape the
    reader cannot see may be a publisher, so a credential is never deleted
    as unused over one."""
    prefix = LANES_ACTION.lower()
    return sorted(
        name
        for name, text in texts.items()
        if _content(text).lower().count(prefix) > sum(uses for uses, _p, _d, _h in _lanes_jobs(text))
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
