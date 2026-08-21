"""`repo list` -- enumerate a fleet without missing one.

Not yet implemented: this is the skeleton commit. Behavior ported from
mikelward/scripts's repo-list lands in a follow-up PR.
"""


def add_arguments(parser):
    parser.add_argument(
        "--owner", help="list this account's repositories instead of your own"
    )
    parser.add_argument(
        "--include-forks", action="store_true", help="include forks"
    )
    parser.add_argument(
        "--include-archived", action="store_true", help="include archived repositories"
    )


def run(args):
    raise SystemExit("repo list: not yet implemented")
