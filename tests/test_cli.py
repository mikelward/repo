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
        # repo; setup needs a positional repo.
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

    def test_stub_subcommands_dispatch_to_their_own_module(self):
        # `list` and `secrets` now have real implementations (see
        # test_list_cmd.py / test_secrets_cmd.py) and are deliberately not
        # covered here, since running either for real would shell out to
        # the actual gh on PATH rather than exercising a stub. `setup`
        # remains a stub and raises SystemExit("... not yet implemented")
        # rather than silently doing nothing -- proves main() actually
        # calls the subcommand's run(), not just that argparse accepted
        # the args.
        with self.assertRaises(SystemExit) as cm:
            main(["setup", "owner/repo"])
        self.assertEqual(str(cm.exception), "repo setup: not yet implemented")


if __name__ == "__main__":
    unittest.main()
