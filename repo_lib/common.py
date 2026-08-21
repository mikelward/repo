"""Shared bits every subcommand needs: the program name and error printing."""

import sys

PROGRAM = "repo"


def error(message):
    print(f"{PROGRAM}: {message}", file=sys.stderr)


def error_lines(prefix, text):
    """Print `prefix`, then every line of `text` indented -- for relaying a
    gh error message without losing its own line breaks."""
    error(prefix)
    for line in text.splitlines():
        error(f"  {line}")
