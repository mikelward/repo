"""`repo secrets` -- fan a secret out across repositories.

Not yet implemented: this is the skeleton commit. Behavior ported from
mikelward/scripts's repo-secrets lands in a follow-up PR.
"""


def add_arguments(parser):
    parser.add_argument("--name", help="secret name")
    parser.add_argument("--env", help="deployment environment to scope the secret to")
    parser.add_argument("--file", help="read the secret value from this file")
    parser.add_argument("--force", action="store_true", help="apply without confirming")
    parser.add_argument("repos", nargs="*", metavar="OWNER/REPO")


def run(args):
    raise SystemExit("repo secrets: not yet implemented")
