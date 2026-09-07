"""Shared bits every subcommand needs: the program name and error printing."""

import os
import sys

PROGRAM = "repo"


def error(message):
    print(f"{PROGRAM}: {message}", file=sys.stderr)


def status(message):
    """A progress line, not a result: stderr, so a redirected stdout stays
    exactly the command's own output. Not gated on isatty -- it is one line,
    and a fleet loop's log is better for having it."""
    print(f"{PROGRAM}: {message}", file=sys.stderr)


def error_lines(prefix, text):
    """Print `prefix`, then every line of `text` indented -- for relaying a
    gh error message without losing its own line breaks."""
    error(prefix)
    for line in (text or "").splitlines():
        error(f"  {line}")


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
