"""`repo list` -- enumerate a fleet without missing one.

Port of mikelward/scripts's repo-list. See its own header comment for the
full reasoning (repeated here only where the port changes something):

- Private repositories: GET /users/{name}/repos only ever returns PUBLIC
  repositories, even for your own account authenticated as yourself. Listing
  your own account's private repos needs GET /user/repos instead, and an
  organization's needs GET /orgs/{org}/repos. This picks between them rather
  than always using the public-only endpoint, which would silently drop
  every private repo in the fleet.
- The one property this exists for: it never prints a partial list. Nothing
  is printed until every repo has been read and filtered, so a failure
  partway through leaves nothing on stdout rather than a list that looks
  complete but isn't.

Cost and reliability: free -- a couple of GitHub REST API calls plus one
paginated call over the repository list (5,000 authenticated requests an
hour). Exit status: 0 on success (zero repositories is a legitimate answer,
not a failure), 1 if gh failed, 2 for a usage error.
"""

import json
import re

from repo_lib import gh
from repo_lib.common import error, error_lines

ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def add_arguments(parser):
    parser.add_argument(
        "--owner",
        help=(
            "list this account's repositories instead of the authenticated "
            "user's. If OWNER is not an organization gh can see, only its "
            "public repositories can be listed -- the API has no way to "
            "list somebody else's private repos regardless of credentials."
        ),
    )
    parser.add_argument("--include-forks", action="store_true", help="include forks")
    parser.add_argument(
        "--include-archived", action="store_true", help="include archived repositories"
    )


def run(args):
    gh.require_gh()

    owner_given = args.owner is not None
    if owner_given and args.owner == "":
        # An explicitly empty --owner ('' or =) is indistinguishable from an
        # omitted one once it's just a plain string -- falling back to the
        # authenticated user here would silently retarget a batch a wrapper
        # script meant to point elsewhere (e.g. an unset variable expanded
        # into --owner "$X") onto this account's own repos instead of
        # failing on the mistake.
        error("--owner was given an empty value; drop --owner entirely to use")
        error("the authenticated user, or pass a real account name.")
        raise SystemExit(2)

    try:
        self_login = gh.run(["api", "user", "--jq", ".login"]).strip()
    except gh.GhError as e:
        error_lines("could not determine the authenticated user:", e.stderr)
        raise SystemExit(1)
    if not self_login:
        error("gh reported an empty authenticated username")
        raise SystemExit(1)

    owner = args.owner if owner_given else self_login

    if not ACCOUNT_NAME_RE.match(owner):
        error(f"'{owner}' contains characters GitHub does not allow in an account name")
        raise SystemExit(2)

    # GitHub account names are case-insensitive (SomeUser and someuser are
    # the same account); compare lowercased so a differently-cased --owner
    # still matches self instead of silently falling through to the
    # public-only endpoint below and dropping every private repo.
    affiliation_args = []
    if owner.lower() == self_login.lower():
        # The one endpoint that is not public-only for your own account.
        # affiliation=owner explicitly: GET /user/repos defaults to
        # owner,collaborator,organization_member, which also returns repos
        # this account can merely push to -- exactly the repos a fan-out
        # into `repo secrets`/`repo setup` must not touch. --method GET
        # because gh api switches to POST the instant a -f field is given.
        endpoint = "user/repos"
        affiliation_args = ["--method", "GET", "-f", "affiliation=owner"]
    else:
        ok, org_probe_result = gh.try_run(["api", f"orgs/{owner}"])
        if ok:
            # An org the caller can see: lists every repo visible to them,
            # private included, same as /user/repos does for a personal
            # account.
            endpoint = f"orgs/{owner}/repos"
        elif "HTTP 404" in org_probe_result:
            # A genuine "no such org" -- owner is (or might be) an
            # individual account instead. Only this specific failure may
            # fall through to the public-only endpoint.
            endpoint = f"users/{owner}/repos"
            error(f"note: '{owner}' is not an organization gh can see -- only its")
            error("public repositories can be listed. There is no API that lists")
            error("somebody else's private repos, whatever credentials are used.")
        else:
            # Any OTHER probe failure (403/SSO, a transient 5xx, network) is
            # NOT proof that owner is an individual account -- reading it
            # that way and falling back to the public-only endpoint would
            # exit 0 with a silently incomplete list for a real, existing
            # org this call merely couldn't reach right now.
            error_lines(
                f"could not determine whether '{owner}' is an organization:",
                org_probe_result,
            )
            raise SystemExit(1)

    try:
        # full_name/fork/archived only -- no reason to pull more than this
        # filters on. --jq does the per-page unwrapping (.[]), so pagination
        # and a final page's own JSON-line boundaries are gh's problem, not
        # ours.
        output = gh.run(
            [
                "api",
                "--paginate",
                endpoint,
                *affiliation_args,
                "--jq",
                ".[] | {full_name, fork, archived}",
            ]
        )
    except gh.GhError as e:
        error_lines(f"could not list {owner}'s repositories:", e.stderr)
        raise SystemExit(1)

    # Nothing is printed until every line above has been read and filtered
    # -- a failure anywhere in this function leaves stdout empty rather than
    # a list that looks complete but isn't.
    full_names = []
    for line in output.splitlines():
        if not line.strip():
            continue
        repo = json.loads(line)
        if repo["fork"] and not args.include_forks:
            continue
        if repo["archived"] and not args.include_archived:
            continue
        full_names.append(repo["full_name"])

    for full_name in sorted(full_names):
        print(full_name)
    return 0
