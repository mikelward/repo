"""`repo secrets` -- fan a secret out across repositories.

Port of mikelward/scripts's repo-secrets. See its own header comment for
the full reasoning (repeated here only where the port changes something):

- The value comes from --file PATH, or from stdin when no --file is given --
  never from a bare command-line argument, which would land in shell
  history and in `ps` output for anyone else on the machine. Reading it
  from an interactive terminal is refused outright: there is no portable
  way to hide terminal input the way `gh secret set`'s own prompt does.
  Only fetched when a write might actually happen -- neither --dry-run nor
  the every-repo-failed early exit ever reads or needs it.
- With --env NAME, the secret is scoped to that deployment environment,
  creating the environment first if it does not already exist -- a PUT to
  that endpoint upserts either way, so this is safe to run whether or not
  it is there yet.
- Before writing, this lists which targets already have a secret by this
  name and prints the plan (new vs. overwrites an existing value), always
  -- including under --force, which skips only the confirmation question,
  not the plan itself, so an unattended run still leaves the "OVERWRITES an
  existing value" warning in its own output. There is no "already matches,
  nothing to do" shortcut -- GitHub never returns a secret's value, so
  whether an existing one already equals what is being set is unanswerable,
  and every write is attempted every time.
- Every repository is attempted even if an earlier one fails. The repos
  that failed are named at the end and the exit status is nonzero if any
  did.
- A secret classified "new" (or an environment classified "missing") at
  plan-build time is revalidated immediately before the write -- it can
  have been created by someone else in the window since (the confirmation
  prompt, or an earlier repo's own writes in a large batch).

Cost and reliability: free -- two or three GitHub REST API calls per
repository (list existing names, ensure the environment, set the secret);
well under the 5,000-authenticated-requests-an-hour limit for any
realistic fleet size. Exit status: 0 if every target was set (or
--dry-run), 1 if any target failed or a read needed to build the plan
failed, 2 for a usage error.
"""

import re
import sys

from repo_lib import gh
from repo_lib.common import error, error_lines

# GitHub's own rule for a secret name: letters, digits, underscores; cannot
# start with a digit; the GITHUB_ prefix is reserved (case-insensitive).
NAME_START_RE = re.compile(r"^[A-Za-z_]")
NAME_CHARS_RE = re.compile(r"^[A-Za-z0-9_]+$")
GITHUB_PREFIX_RE = re.compile(r"^github_", re.IGNORECASE)
# The lookaheads reject `.` and `..` components: made of allowed
# characters, but as path segments spliced into `repos/{repo}/...` or
# `.../environments/{env}/...` they would address a different endpoint
# than the one that was validated.
ENV_NAME_RE = re.compile(r"^(?!\.\.?$)[A-Za-z0-9._-]+$")
OWNER_REPO_RE = re.compile(r"^(?!\.\.?/)[A-Za-z0-9._-]+/(?!\.\.?$)[A-Za-z0-9._-]+$")


def add_arguments(parser):
    parser.add_argument("--name", required=True, help="the secret's name")
    parser.add_argument(
        "--env",
        help=(
            "set an environment-scoped secret instead of a repository "
            "secret, creating the environment if it does not exist"
        ),
    )
    parser.add_argument(
        "--file", help="read the value from this file instead of stdin"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan; make no writes"
    )
    parser.add_argument(
        "--force", action="store_true", help="apply without asking for confirmation"
    )
    parser.add_argument("repos", nargs="+", metavar="OWNER/REPO")


def validate_name(name):
    """Returns an error message if `name` is not a valid GitHub secret name,
    else None. Pulled out of run() so `repo setup`'s --secret validation can
    reuse the exact same rule rather than re-deriving it."""
    if not NAME_START_RE.match(name):
        return f"'{name}' is not a valid secret name: must start with a letter or underscore"
    if not NAME_CHARS_RE.match(name):
        return f"'{name}' is not a valid secret name: only letters, digits, and underscores"
    if GITHUB_PREFIX_RE.match(name):
        return f"'{name}' starts with the reserved GITHUB_ prefix; GitHub will refuse it"
    return None


def validate_env(env):
    """Returns an error message if the already-known-non-empty `env` is not
    an environment name this tool handles, else None."""
    if not ENV_NAME_RE.match(env):
        return (
            f"'{env}' is not an environment name this script handles (kept to "
            "letters, digits, '.', '_', '-', excluding the reserved '.' and '..' "
            "path segments -- refusing rather than guessing how to URL-encode "
            "anything wider)."
        )
    return None


def _list_endpoint(repo, env):
    if env:
        return f"repos/{repo}/environments/{env}/secrets"
    return f"repos/{repo}/actions/secrets"


def _build_plan(repos, name, env):
    """Reads every repo's existing secret names, returns a list of
    (repo, state, env_state) -- state in {"new", "overwrite", "error"},
    env_state in {"exists", "missing", "unknown"}. Never raises; a read
    failure is recorded as state "error" so one bad repo doesn't abort the
    batch."""
    plan = []
    for repo in repos:
        try:
            output = gh.run(
                ["api", "--paginate", _list_endpoint(repo, env), "--jq", ".secrets[].name"]
            )
        except gh.GhError as e:
            if env and "HTTP 404" in e.stderr:
                # Ambiguous: $env doesn't exist yet on a real, accessible
                # repo (the expected case), or the repo itself doesn't
                # exist or isn't accessible -- GitHub deliberately doesn't
                # distinguish, to avoid leaking a private repo's existence.
                # Positively confirm the repo exists via its
                # environment-independent secrets endpoint before trusting
                # that the environment, not the repo, is what's missing.
                try:
                    gh.run(["api", f"repos/{repo}/actions/secrets", "--jq", ".secrets[].name"])
                except gh.GhError as e2:
                    error_lines(
                        f"could not confirm {repo} exists and is accessible "
                        "(the environment lookup 404'd too):",
                        e2.stderr,
                    )
                    plan.append((repo, "error", "unknown"))
                else:
                    plan.append((repo, "new", "missing"))
            else:
                error_lines(f"could not list existing secrets on {repo}:", e.stderr)
                plan.append((repo, "error", "unknown"))
        else:
            names = [line for line in output.splitlines() if line.strip()]
            state = "overwrite" if any(n.lower() == name.lower() for n in names) else "new"
            plan.append((repo, state, "exists"))
    return plan


def _describe_plan(name, env, plan):
    lines = []
    if env:
        lines.append(f"About to set secret '{name}' (environment '{env}') on:")
    else:
        lines.append(f"About to set secret '{name}' (repository-level) on:")
    for repo, state, _ in plan:
        if state == "overwrite":
            lines.append(f"  {repo}: OVERWRITES an existing value")
        elif state == "new":
            lines.append(f"  {repo}: new")
        else:
            lines.append(f"  {repo}: SKIPPED -- could not read its existing secrets")
    return lines


def _confirm():
    """True if the user confirmed. False means "stop, nothing was
    written" -- the caller has already had the reason (and the plan)
    printed."""
    if not sys.stdin.isatty():
        error("stdin is not a terminal and --force was not given, so no secrets")
        error("were changed rather than either blocking on a question nobody can")
        error("answer or silently applying an unconfirmed change. Pass --force to")
        error("apply non-interactively, or run this from a terminal.")
        return False
    print("Apply this to every repository above? [y/N] ", file=sys.stderr, end="")
    try:
        confirmed = input()
    except EOFError:
        confirmed = ""
    if confirmed.strip().lower() not in ("y", "yes"):
        error("not confirmed; no secrets were changed.")
        return False
    return True


def _recheck_still_absent(repo, name, env):
    """Re-reads repo's existing secret names immediately before a write
    classified "new" at plan-build time. Returns "ok" (still absent, safe
    to write), "now_exists" (someone else created it since -- refuse), or
    "error" (couldn't recheck -- refuse)."""
    try:
        output = gh.run(
            ["api", "--paginate", _list_endpoint(repo, env), "--jq", ".secrets[].name"]
        )
    except gh.GhError as e:
        if env and "HTTP 404" in e.stderr:
            # The environment still doesn't exist -- nothing on it could
            # have been created since the plan was built.
            return "ok"
        error_lines(
            f"could not recheck whether '{name}' still doesn't exist on "
            f"{repo}{f' (environment {env})' if env else ''}:",
            e.stderr,
        )
        return "error"
    names = [line for line in output.splitlines() if line.strip()]
    if any(n.lower() == name.lower() for n in names):
        return "now_exists"
    return "ok"


def _ensure_environment(repo, env):
    """Creates `env` on `repo` if it still doesn't exist, immediately
    before the write -- revalidated here rather than trusted from the plan,
    since a PUT to an already-existing environment silently resets its
    protection settings (required reviewers, wait timer, branch policy) to
    the endpoint's defaults. Returns True if it's safe to proceed, False if
    creation failed."""
    ok, err = gh.try_run(["api", f"repos/{repo}/environments/{env}"])
    if ok:
        return True  # someone else created it since the plan was built
    if "HTTP 404" not in err:
        # A 403/SSO/transient failure here is NOT proof the environment is
        # still missing -- PUTting on that assumption could hit a real,
        # existing environment this call merely couldn't reach right now,
        # resetting its protection settings for no reason.
        error_lines(f"could not recheck whether environment '{env}' exists on {repo}:", err)
        return False
    ok, err = gh.try_run(["api", "--method", "PUT", f"repos/{repo}/environments/{env}"])
    if ok:
        return True
    error_lines(f"could not create environment '{env}' on {repo}:", err)
    return False


def _write_secret(repo, name, env, value):
    """Writes `value` as secret `name` (optionally environment-scoped to
    `env`) on `repo`, printing the outcome. Returns True on success. Pulled
    out of run()'s apply loop so `repo setup`'s --secret step can reuse the
    exact same write, not a reimplementation of it."""
    set_args = ["secret", "set", name, "--repo", repo]
    if env:
        set_args += ["--env", env]
    try:
        gh.run_with_input(set_args, value)
    except gh.GhError as e:
        error_lines(f"could not set '{name}' on {repo}{f' (environment {env})' if env else ''}:", e.stderr)
        return False
    if env:
        print(f"{repo}: set '{name}' (environment '{env}')")
    else:
        print(f"{repo}: set '{name}'")
    return True


def run(args):
    name_err = validate_name(args.name)
    if name_err:
        error(name_err)
        raise SystemExit(2)

    env_given = args.env is not None
    if env_given and args.env == "":
        error("--env was given an empty value; drop --env entirely for a")
        error("repository-level secret, or pass a real environment name.")
        raise SystemExit(2)
    env = args.env if env_given else None
    if env:
        env_err = validate_env(env)
        if env_err:
            error(env_err)
            raise SystemExit(2)

    file_given = args.file is not None
    if file_given and args.file == "":
        error("--file was given an empty value; drop --file entirely to read")
        error("the value from stdin, or pass a real path.")
        raise SystemExit(2)

    for repo in args.repos:
        if not OWNER_REPO_RE.match(repo):
            error(f"'{repo}' is not OWNER/REPO")
            raise SystemExit(2)

    # Deduplicated, preserving first-occurrence order, case-insensitively --
    # a repeated OWNER/REPO is always redundant to process twice, and
    # without dedup the second occurrence's write-time recheck would see
    # the secret this same run's first occurrence just wrote and misread it
    # as "created by someone else".
    seen = set()
    repos = []
    for repo in args.repos:
        key = repo.lower()
        if key in seen:
            continue
        seen.add(key)
        repos.append(repo)

    gh.require_gh()

    plan = _build_plan(repos, args.name, env)
    plan_has_error = any(state == "error" for _, state, _ in plan)

    if args.dry_run:
        # The value can't affect the plan and nothing gets written, so a
        # dry run needs no value at all -- an interactive `--dry-run` with
        # no `--file` would otherwise be refused for no reason, and a piped
        # one would consume stdin it never needed.
        for line in _describe_plan(args.name, env, plan):
            print(line)
        raise SystemExit(1 if plan_has_error else 0)

    if all(state == "error" for _, state, _ in plan):
        # Every repo failed the read phase -- nothing left to confirm or
        # write, so (same reasoning as --dry-run) no need for a value.
        for line in _describe_plan(args.name, env, plan):
            error(line)
        raise SystemExit(1)

    if file_given:
        try:
            with open(args.file, "rb") as f:
                value = f.read()
        except OSError:
            error(f"cannot read '{args.file}'")
            raise SystemExit(2)
    else:
        if sys.stdin.isatty():
            error("refusing to read a secret value from an interactive terminal --")
            error("there is no way to hide it as you type. Pipe the value in, or pass")
            error("--file PATH.")
            raise SystemExit(2)
        value = sys.stdin.buffer.read()
    if not value:
        error("the secret value is empty")
        raise SystemExit(2)

    # Printed unconditionally -- including under --force -- so a forced,
    # unattended run still leaves the OVERWRITES-an-existing-value warning
    # (and the rest of the plan) in its own output, not just silently
    # applied. --force skips the *question*, not the audit trail.
    for line in _describe_plan(args.name, env, plan):
        print(line, file=sys.stderr)

    if not args.force and not _confirm():
        raise SystemExit(1)

    failed = []
    for repo, state, env_state in plan:
        if state == "error":
            # Already reported when the plan was built; carried through so
            # the final summary names every repo nothing happened to.
            failed.append(repo)
            continue
        if state == "new":
            recheck = _recheck_still_absent(repo, args.name, env)
            if recheck == "now_exists":
                error(
                    f"'{args.name}' on {repo}{f' (environment {env})' if env else ''} "
                    "was created by"
                )
                error("someone else since the plan was built; refusing to overwrite")
                error("it without the confirmation that was given for a different,")
                error("now-stale plan.")
                failed.append(repo)
                continue
            if recheck == "error":
                failed.append(repo)
                continue
        if env and env_state == "missing":
            if not _ensure_environment(repo, env):
                failed.append(repo)
                continue
        if not _write_secret(repo, args.name, env, value):
            failed.append(repo)

    if failed:
        error(f"failed on: {' '.join(failed)}")
        raise SystemExit(1)
    return 0
