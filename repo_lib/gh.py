"""A thin `gh` CLI wrapper.

Every call goes through subprocess.run with an argument list -- never a
shell string -- so there is no quoting/injection hazard to reason about,
unlike the shell scripts this is replacing.
"""

import shutil
import subprocess
import time

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


# GitHub's two rate limits (docs.github.com/rest/using-the-rest-api/rate-
# limits-for-the-rest-api) call for different responses: the SECONDARY
# (abuse-detection) limit is a short burst throttle GitHub's own guidance
# says to wait out and retry, so that's retried here, bounded; the PRIMARY
# limit's reset can be up to an hour away, so it's reported instead --
# blocking a script that long with no explanation is worse than failing.
_RATE_LIMIT_RETRY_ATTEMPTS = 5
_RATE_LIMIT_RETRY_DELAY_SECONDS = 60  # GitHub's own floor for a secondary limit


def _is_secondary_rate_limit(stderr):
    return "secondary rate limit" in stderr.lower()


def _is_mutating(argv, kwargs):
    """True if this call is a write (POST/PUT/PATCH/DELETE, or anything
    carrying an input body), not a plain read. A write may depend on a
    precondition its caller checked immediately before calling this; a
    delayed retry can invalidate that, so only a read retries here."""
    if kwargs.get("input") is not None:
        return True
    try:
        return argv[argv.index("--method") + 1].upper() != "GET"
    except ValueError:
        return False


def _run_subprocess_retrying_secondary_rate_limit(argv, **kwargs):
    """subprocess.run(argv, **kwargs), retrying only GitHub's secondary rate
    limit on a plain read (see _is_mutating) -- every other failure, the
    primary rate limit and any write included, is returned on the first
    attempt for the caller to handle exactly as before."""
    if _is_mutating(argv, kwargs):
        return subprocess.run(argv, **kwargs)
    delay = _RATE_LIMIT_RETRY_DELAY_SECONDS
    for attempt in range(1, _RATE_LIMIT_RETRY_ATTEMPTS + 1):
        proc = subprocess.run(argv, **kwargs)
        if proc.returncode == 0:
            return proc
        stderr_text = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        if attempt == _RATE_LIMIT_RETRY_ATTEMPTS or not _is_secondary_rate_limit(stderr_text):
            return proc
        error(
            f"hit GitHub's secondary rate limit -- waiting {delay}s before retrying "
            f"({attempt}/{_RATE_LIMIT_RETRY_ATTEMPTS}): {stderr_text.strip()}"
        )
        time.sleep(delay)
        delay *= 2
    return proc


def run(args):
    """Run `gh <args>`, return stdout. Raises GhError(stderr) on failure."""
    proc = _run_subprocess_retrying_secondary_rate_limit(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(proc.stderr)
    return proc.stdout


def try_run(args):
    """Run `gh <args>`, return (ok, stdout_if_ok_else_stderr).

    For call sites that need to inspect a failure (a 404 vs. everything
    else) rather than treat any nonzero exit identically.
    """
    proc = _run_subprocess_retrying_secondary_rate_limit(["gh", *args], capture_output=True, text=True)
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr


def run_with_input(args, input_bytes):
    """Run `gh <args>` with `input_bytes` fed to stdin, return stdout.
    Raises GhError(stderr) on failure. Bytes, not text -- `gh secret set`
    is the one subcommand here whose payload is an opaque secret value,
    not something to decode/re-encode through a text codec."""
    proc = _run_subprocess_retrying_secondary_rate_limit(
        ["gh", *args], input=input_bytes, capture_output=True
    )
    if proc.returncode != 0:
        raise GhError(proc.stderr.decode(errors="replace"))
    return proc.stdout
