"""Shared bits every subcommand needs: the program name and error printing."""

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
