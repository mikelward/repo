"""A thin `gh` CLI wrapper.

Every call goes through subprocess.run with an argument list -- never a
shell string -- so there is no quoting/injection hazard to reason about,
unlike the shell scripts this is replacing.
"""

import shutil
import subprocess
import time

from repo_lib.common import error, error_lines, warn, warn_lines


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


def _is_auth_rejection(stderr):
    """True if `stderr` is GitHub (or gh) REFUSING the credentials, rather
    than any other way the same read can fail. Only a refusal is proof the
    token is the problem: a 500, an exhausted rate limit or a dropped
    connection say nothing about it, and treating those as "no credentials"
    would stop every step over one unlucky read (Codex, mikelward/repo#60).
    """
    text = (stderr or "").lower()
    return (
        "http 401" in text
        or "bad credentials" in text
        or "requires authentication" in text
        # gh's own wording when nothing is logged in at all.
        or "gh auth login" in text
    )


def require_auth():
    """The authenticated login, or exit 2 before the run touches anything.

    A run whose credentials GitHub refuses cannot read a repository, let
    alone write one, so it fails here rather than at whichever step
    happened to read first -- by which point the operator has a wall of
    per-step failures to read backwards instead of the one line that
    explains them. That is a usage error, the one thing SPEC.md's
    invariant 1 lets stop a whole run.

    Any OTHER failure of the probe is not: the token may be perfectly good
    and this one read unlucky, so it is reported and the run goes on to
    make whatever progress it can, each step reporting its own failure.
    Returns the login, or None when the probe could not answer.

    One `user` read: the cheapest call that proves a token is BOTH present
    and still accepted. `gh auth status` is not that -- it reports the
    stored login without proving GitHub still honors it, so an expired or
    revoked token passes it.
    """
    ok, out = try_run(["api", "user", "--jq", ".login"])
    if ok:
        return out.strip()
    if _is_auth_rejection(out):
        error_lines("GitHub refused these credentials, so nothing this run does can work:", out)
        error("Run `gh auth login` (or set GH_TOKEN to a token with repo access), then rerun.")
        raise SystemExit(2)
    warn_lines(
        "could not check this gh token before starting (continuing -- each step reports "
        "its own failure):",
        out,
    )
    return None


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
        warn(
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


def token_scopes():
    """The OAuth scopes this token carries, or None if that can't be told.
    Read from the X-OAuth-Scopes response header on an authenticated GET --
    the same thing a user checking by hand would run (`gh api -i user`).

    A caller gating on a scope's presence should not block just because
    this couldn't answer -- only act on an explicit "no" (a non-empty
    returned set that doesn't contain it), never on "couldn't tell" (Codex
    review, mikelward/repo#18: an empty-but-present header parsed to set()
    rather than None, which read as "confirmed no scopes" and blocked a
    token this couldn't actually make that claim about). But "couldn't
    tell" still covers two very different situations, and only one of them
    is silent: the header being absent or empty is routine -- a
    fine-grained PAT or a GitHub App installation token doesn't use OAuth
    scopes at all, nothing to report -- while the probe read itself
    failing (auth, network, GitHub down) is a real failure a caller falling
    through to the same opaque 404 this exists to diagnose would otherwise
    never see reported at all (Codex review, mikelward/repo#18)."""
    try:
        raw = run(["api", "-i", "user"])
    except GhError as e:
        warn(f"could not check this gh token's OAuth scopes (continuing without that check): {e.stderr.strip()}")
        return None
    headers, _, _ = raw.partition("\n\n")
    for line in headers.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "x-oauth-scopes":
            scopes = {s.strip() for s in value.split(",") if s.strip()}
            return scopes or None
    return None
