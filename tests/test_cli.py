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

    def test_each_subcommand_parses_with_no_extra_arguments(self):
        # list and secrets take none required; setup needs a positional repo.
        self.assertEqual(build_parser().parse_args(["list"]).command, "list")
        self.assertEqual(build_parser().parse_args(["secrets"]).command, "secrets")
        self.assertEqual(
            build_parser().parse_args(["setup", "owner/repo"]).command, "setup"
        )

    def test_setup_requires_the_owner_repo_positional(self):
        with self.assertRaises(SystemExit) as cm:
            build_parser().parse_args(["setup"])
        self.assertEqual(cm.exception.code, 2)

    def test_each_subcommand_dispatches_to_its_own_module(self):
        # Skeleton stubs raise SystemExit("... not yet implemented") rather
        # than silently doing nothing -- proves main() actually calls the
        # subcommand's run(), not just that argparse accepted the args.
        for argv, expected in (
            (["list"], "repo list: not yet implemented"),
            (["secrets"], "repo secrets: not yet implemented"),
            (["setup", "owner/repo"], "repo setup: not yet implemented"),
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as cm:
                    main(argv)
                self.assertEqual(str(cm.exception), expected)


if __name__ == "__main__":
    unittest.main()
