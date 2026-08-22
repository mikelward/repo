"""Argument dispatch for the `repo` command.

Each subcommand (list, secrets, setup) lives in its own module with a
`run(args)` entry point, mirroring the standalone repo-list/repo-secrets/
repo-setup scripts in mikelward/scripts that this project is replacing --
one subcommand per concern, composed rather than duplicated.
"""

import argparse
import sys

from repo_lib import audit_cmd, list_cmd, secrets_cmd, setup_cmd

PROGRAM = "repo"


def build_parser():
    parser = argparse.ArgumentParser(prog=PROGRAM)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list", help="enumerate a fleet without missing one"
    )
    list_cmd.add_arguments(list_parser)
    list_parser.set_defaults(func=list_cmd.run)

    secrets_parser = subparsers.add_parser(
        "secrets", help="fan a secret out across repositories"
    )
    secrets_cmd.add_arguments(secrets_parser)
    secrets_parser.set_defaults(func=secrets_cmd.run)

    setup_parser = subparsers.add_parser(
        "setup", help="compose rules, secrets, and App membership"
    )
    setup_cmd.add_arguments(setup_parser)
    setup_parser.set_defaults(func=setup_cmd.run)

    audit_parser = subparsers.add_parser(
        "audit", help="report whether a repository's branch rules actually hold"
    )
    audit_cmd.add_arguments(audit_parser)
    audit_parser.set_defaults(func=audit_cmd.run)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
