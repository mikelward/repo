"""`repo setup` -- compose rules, secrets, and App membership.

Not yet implemented: this is the skeleton commit. Behavior ported from
mikelward/scripts's repo-setup lands in a follow-up PR.
"""


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
    raise SystemExit("repo setup: not yet implemented")
