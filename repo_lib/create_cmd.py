"""`repo create` -- create an empty GitHub repository, scaffolded with the
fleet's standard CI.

Creates the repository first, always empty (no README, no default-branch
content from GitHub itself) -- then, unless --no-scaffold is given, pushes
what's mechanically safe to generate (see scaffold.py's own docstring for
the split between that and what genuinely needs project knowledge) as the
repository's first commits -- two of them, a small bootstrap commit
followed immediately by the real one; see push_initial_commit's own
docstring for why a genuinely empty repository can't take this in a
single write. Together they become the "scaffolding commit made directly
to an empty main" every AGENTS.md in this fleet already carries as the
one exception to "never commit to main" -- still the caller's first
commits, just written by this tool instead of by hand.

`repo setup` is still a separate step, not something this module calls for
you: it needs to know which fleet credentials/Apps/secrets this particular
repository uses, none of which `create` has any way to guess. So `create`
prints the exact follow-up command instead of running it.

Exit status: 0 on success, 1 if gh or the scaffold push failed, 2 for a
usage error.
"""

import argparse
import json
import re

from repo_lib import gh, scaffold
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
    parser.add_argument(
        "--scaffold",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="push the fleet's standard CI files as the initial commit (default: yes)",
    )
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
        raw = gh.run_with_input(
            ["api", "--method", "POST", endpoint, "--input", "-"], json.dumps(body).encode()
        )
    except gh.GhError as e:
        error_lines(f"could not create {args.repo}:", e.stderr)
        raise SystemExit(1)

    visibility = "private" if args.private else "public"
    print(f"{args.repo}: created ({visibility}, empty)")

    if not args.scaffold:
        print("Push your initial commit (workflows included), then run:")
        print(f"  repo setup {args.repo} --force")
        return 0

    # GitHub sets this at creation from the account/org's configured
    # default branch name -- true even though no ref exists yet, which is
    # exactly what lets the scaffold commit below create that ref itself.
    default_branch = (json.loads(raw) or {}).get("default_branch")
    if not default_branch:
        error(f"{args.repo} was created, but the response named no default branch;")
        error("cannot scaffold. Push your initial commit by hand, then run:")
        error(f"  repo setup {args.repo} --force")
        raise SystemExit(1)

    files = scaffold.build_scaffold_files(default_branch)
    if files is None:
        error(f"{args.repo} was created, but fetching the scaffold's template files failed")
        error("(see above); nothing was pushed. Push your initial commit by hand, then run:")
        error(f"  repo setup {args.repo} --force")
        raise SystemExit(1)

    if not scaffold.push_initial_commit(args.repo, default_branch, files):
        error(f"{args.repo} was created, but pushing the scaffold commit failed (see above).")
        error("Push your initial commit by hand, then run:")
        error(f"  repo setup {args.repo} --force")
        raise SystemExit(1)

    print(f"{args.repo}: pushed the CI scaffold ({len(files)} files) to {default_branch}")
    print("lanes, codex and zizmor will all report on the next push or pull request --")
    print("ci.yml's placeholder job stands in for this project's real jobs until you")
    print("replace it (see the TODO.md this pushed). Once something has reported, run:")
    print(f"  repo setup {args.repo} --force")
    return 0
