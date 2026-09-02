"""`repo create` -- create an empty GitHub repository.

Deliberately does one thing: create the repository, empty (no README, no
default branch content), so the first push to it is the caller's own
initial commit -- the "scaffolding commit made directly to an empty main"
every AGENTS.md in this fleet already carries as the one exception to
"never commit to main". Nothing here pushes a scaffold or picks a template:
what belongs in a fresh repo varies by project type, and this module has no
opinion on it.

`repo setup` is the next step, not something this module calls for you --
composing the two would need to guess whether to run it before any CI has
ever reported (which only works with --force; see rules.py's
never-reported-check guard) or wait for the caller to push workflow files
and let CI run first, and that choice depends on what the caller is about
to push, which this module doesn't know. So `create` prints the exact
follow-up command instead of running it.

Exit status: 0 on success, 1 if gh failed, 2 for a usage error.
"""

import json
import re

from repo_lib import gh
from repo_lib.common import error, error_lines

# Same shape as setup_cmd.py's/secrets_cmd.py's/audit_cmd.py's own
# OWNER_REPO_RE -- kept as its own module-level copy, matching this
# codebase's existing convention of a small, self-contained validator per
# subcommand module.
OWNER_REPO_RE = re.compile(r"^(?!\.\.?/)[A-Za-z0-9._-]+/(?!\.\.?$)[A-Za-z0-9._-]+$")


def add_arguments(parser):
    # Required, no default -- matching `gh repo create`'s own insistence on
    # an explicit choice rather than a guessed one. Visibility is the one
    # decision here with real consequences if guessed wrong, and a silent
    # default risks either an accidentally public private-fleet repo or an
    # unwanted private one nobody can see.
    visibility = parser.add_mutually_exclusive_group(required=True)
    visibility.add_argument("--private", action="store_true", help="create a private repository")
    visibility.add_argument("--public", action="store_true", help="create a public repository")
    parser.add_argument("repo", metavar="OWNER/REPO")


def run(args):
    if not OWNER_REPO_RE.match(args.repo):
        error(f"'{args.repo}' is not OWNER/REPO")
        raise SystemExit(2)

    gh.require_gh()

    owner, name = args.repo.split("/", 1)

    try:
        self_login = gh.run(["api", "user", "--jq", ".login"]).strip()
    except gh.GhError as e:
        error_lines("could not determine the authenticated user:", e.stderr)
        raise SystemExit(1)
    if not self_login:
        error("gh reported an empty authenticated username")
        raise SystemExit(1)

    # Same org-vs-self detection as list_cmd.py, minus its public-only
    # fallback: creation has no equivalent of "list what's public" for an
    # account that's neither self nor a visible org -- there is no API to
    # create a repository under somebody else's personal account at all.
    if owner.lower() == self_login.lower():
        endpoint = "user/repos"
    else:
        ok, org_probe_result = gh.try_run(["api", f"orgs/{owner}"])
        if ok:
            endpoint = f"orgs/{owner}/repos"
        elif "HTTP 404" in org_probe_result:
            error(f"'{owner}' is neither the authenticated user ('{self_login}') nor an")
            error("organization gh can see. A repository can only be created under the")
            error("account you're authenticated as, or an organization you belong to.")
            raise SystemExit(1)
        else:
            error_lines(
                f"could not determine whether '{owner}' is an organization:", org_probe_result
            )
            raise SystemExit(1)

    body = {"name": name, "private": args.private}
    try:
        gh.run_with_input(
            ["api", "--method", "POST", endpoint, "--input", "-"], json.dumps(body).encode()
        )
    except gh.GhError as e:
        error_lines(f"could not create {args.repo}:", e.stderr)
        raise SystemExit(1)

    visibility = "private" if args.private else "public"
    print(f"{args.repo}: created ({visibility}, empty)")
    print("Push your initial commit (workflows included), then run:")
    print(f"  repo setup {args.repo} --force")
    return 0
