"""Shared bits every subcommand needs: leveled output and the state-log path.

Three levels, all on stderr so a redirected stdout stays exactly the
command's own result:

- `error` -- a failure the user must see; prefixed `error:`.
- `warn`  -- a caveat or fallback the command carries on past; `warning:`.
- `info`  -- routine progress or a result note; no level prefix.

Only `error`/`warn` carry a prefix: the program name led every line before,
which buried the one word that said whether a line was a problem.
"""

import os
import sys


def error(message):
    print(f"error: {message}", file=sys.stderr)


def warn(message):
    print(f"warning: {message}", file=sys.stderr)


def info(message):
    """Routine progress or a result note. Not gated on isatty -- it is one
    line, and a fleet loop's log is better for having it."""
    print(message, file=sys.stderr)


def error_lines(headline, text):
    """`headline` as an error, then each line of `text` indented under it --
    for relaying a gh error message without losing its own line breaks. Only
    the headline carries the level prefix."""
    error(headline)
    for line in (text or "").splitlines():
        print(f"  {line}", file=sys.stderr)


def default_log_path(command, repo, now):
    """A timestamped file under the XDG state directory -- state, not
    cache: a run's record of what it changed is not something another
    program is entitled to clear.

    Shared so `repo cleanup` and `repo setup` write to one directory with
    one naming convention, rather than each inventing its own.
    """
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(base, "repo", f"{command}-{repo.replace('/', '-')}-{stamp}.log")
