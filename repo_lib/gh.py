"""A thin `gh` CLI wrapper.

Every call goes through subprocess.run with an argument list -- never a
shell string -- so there is no quoting/injection hazard to reason about,
unlike the shell scripts this is replacing.
"""

import shutil
import subprocess

from repo_lib.common import error


class GhError(Exception):
    """Raised when a `gh` invocation exits nonzero. `.stderr` is gh's own
    stderr text, for relaying to the user without losing its wording."""

    def __init__(self, stderr):
        super().__init__(stderr)
        self.stderr = stderr


def require_gh():
    if shutil.which("gh") is None:
        error("gh is not installed. It carries the authentication this needs;")
        error("installing it is less work than reimplementing that with curl.")
        raise SystemExit(1)


def run(args):
    """Run `gh <args>`, return stdout. Raises GhError(stderr) on failure."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(proc.stderr)
    return proc.stdout


def try_run(args):
    """Run `gh <args>`, return (ok, stdout_if_ok_else_stderr).

    For call sites that need to inspect a failure (a 404 vs. everything
    else) rather than treat any nonzero exit identically.
    """
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr
