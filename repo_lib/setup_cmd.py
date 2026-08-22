"""`repo setup` -- compose rules, secrets, and App membership.

The ruleset step (mikelward/scripts's repo-rules -- see repo_lib/rules.py
for the port) is implemented: it composes the required-checks ruleset,
with the hardened branch targeting (~DEFAULT_BRANCH, refs/heads/main, and
refs/heads/master together) built in from the start rather than ported as
a later hardening, and it warns whenever the repository has an actual
branch literally named "master" -- a lingering deprecated-name branch is
exactly the backdoor that targeting exists to close.

repo-setup's other two steps -- fanning a secret out via repo-secrets, and
ensuring GitHub App installation membership -- are a separate concern from
the ruleset core (the former already exists as `repo secrets`; the latter
has no sibling implementation to port from yet) and are deferred; see
TODO.md. --secret and --app stay declared here so the CLI shape matches
what repo-setup will eventually cover, but using either refuses outright
rather than silently doing nothing.
"""

import re

from repo_lib import gh, rules
from repo_lib.common import error

# The lookaheads reject `.` and `..` components: made of allowed
# characters, but as path segments spliced into `repos/{repo}/...` they
# would address a different endpoint than the one that was validated.
OWNER_REPO_RE = re.compile(r"^(?!\.\.?/)[A-Za-z0-9._-]+/(?!\.\.?$)[A-Za-z0-9._-]+$")


def add_arguments(parser):
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="apply without confirming")
    parser.add_argument("--no-rules", action="store_true", help="skip the ruleset step")
    parser.add_argument("--rule", action="append", default=[], help="a required check (repeatable)")
    parser.add_argument(
        "--secret", action="append", default=[], metavar="NAME[@ENV]=PATH", help="repeatable"
    )
    parser.add_argument("--app", action="append", default=[], help="a GitHub App slug (repeatable)")
    parser.add_argument("repo", metavar="OWNER/REPO")


def run(args):
    if not OWNER_REPO_RE.match(args.repo):
        error(f"'{args.repo}' is not OWNER/REPO")
        raise SystemExit(2)

    if args.no_rules and args.rule:
        error("--no-rules and --rule are contradictory")
        raise SystemExit(2)

    if args.secret:
        raise SystemExit(
            "repo setup: --secret is not yet implemented -- run `repo secrets` "
            "directly for now (see TODO.md)."
        )
    if args.app:
        raise SystemExit("repo setup: --app is not yet implemented (see TODO.md).")

    gh.require_gh()

    exit_code = 0
    if not args.no_rules:
        checks = args.rule if args.rule else list(rules.DEFAULT_CHECKS)
        exit_code = rules.apply_ruleset(args.repo, checks, dry_run=args.dry_run, force=args.force)

    # Independent of the ruleset step and of --no-rules: worth flagging on
    # its own, and read-only, so it runs even under --dry-run.
    rules.check_master_branch(args.repo)

    if exit_code != 0:
        raise SystemExit(exit_code)
    return 0
