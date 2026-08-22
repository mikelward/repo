import unittest

from repo_lib.cli import build_parser, main


class CliTest(unittest.TestCase):
    def test_no_command_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm:
            build_parser().parse_args([])
        self.assertEqual(cm.exception.code, 2)

    def test_unknown_command_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm:
            build_parser().parse_args(["frobnicate"])
        self.assertEqual(cm.exception.code, 2)

    def test_each_subcommand_parses_with_its_required_arguments(self):
        # list takes none required; secrets needs --name and at least one
        # repo; setup and audit each need a positional repo.
        self.assertEqual(build_parser().parse_args(["list"]).command, "list")
        self.assertEqual(
            build_parser()
            .parse_args(["secrets", "--name", "TOKEN", "owner/repo"])
            .command,
            "secrets",
        )
        self.assertEqual(
            build_parser().parse_args(["setup", "owner/repo"]).command, "setup"
        )
        self.assertEqual(
            build_parser().parse_args(["audit", "owner/repo"]).command, "audit"
        )
        self.assertEqual(
            build_parser().parse_args(["audit", "owner/repo", "lanes", "codex"]).command,
            "audit",
        )

    def test_secrets_requires_name_and_at_least_one_repo(self):
        for argv in (["secrets"], ["secrets", "--name", "TOKEN"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as cm:
                    build_parser().parse_args(argv)
                self.assertEqual(cm.exception.code, 2)

    def test_setup_requires_the_owner_repo_positional(self):
        with self.assertRaises(SystemExit) as cm:
            build_parser().parse_args(["setup"])
        self.assertEqual(cm.exception.code, 2)

    def test_audit_requires_the_owner_repo_positional(self):
        with self.assertRaises(SystemExit) as cm:
            build_parser().parse_args(["audit"])
        self.assertEqual(cm.exception.code, 2)

    def test_audit_dispatches_to_its_own_module(self):
        # Proves main() actually calls audit_cmd.run(), not just that
        # argparse accepted the args, via a validation path that fails
        # before any gh call -- see test_audit_cmd.py for full coverage.
        with self.assertRaises(SystemExit) as cm:
            main(["audit", "not-an-owner-repo"])
        self.assertEqual(cm.exception.code, 2)

    def test_setup_dispatches_to_its_own_module(self):
        # All three subcommands now have real implementations (see
        # test_list_cmd.py / test_secrets_cmd.py / test_setup_cmd.py) and
        # are deliberately not exercised end-to-end here, since running
        # any of them for real would shell out to the actual gh on PATH.
        # This proves main() actually calls setup_cmd.run(), not just that
        # argparse accepted the args, via a validation path that fails
        # before any gh call.
        with self.assertRaises(SystemExit) as cm:
            main(["setup", "not-an-owner-repo"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
