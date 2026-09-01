import unittest

from repo_lib import credentials

CALLER = """name: npm update
on:
  schedule:
    - cron: '17 6 * * 6'
permissions: {}
jobs:
  npm-update:
    uses: mikelward/npm-update/.github/workflows/npm-update.yml@main
    permissions:
      contents: write
    with:
      dispatch-workflows: zizmor.yml
    # why this is inherit
    secrets: inherit
"""


class CallerInheritsTest(unittest.TestCase):
    """The text reading behind every move and every delete: which jobs call
    a reusable workflow, and whether they pass `secrets: inherit`. Its
    failure mode is a false "unused" -- a caller it cannot see makes the
    credential look stale, and setup deletes it -- so the shapes the fleet
    actually writes are pinned here."""

    def test_an_inheriting_job_level_caller(self):
        self.assertIs(credentials.caller_inherits(CALLER, "mikelward/npm-update/"), True)

    def test_a_caller_naming_its_secrets(self):
        named = CALLER.replace("    secrets: inherit\n", "    secrets:\n      token: ${{ secrets.X }}\n")
        self.assertIs(credentials.caller_inherits(named, "mikelward/npm-update/"), False)

    def test_a_caller_passing_no_secrets_at_all(self):
        bare = CALLER.replace("    secrets: inherit\n", "")
        self.assertIs(credentials.caller_inherits(bare, "mikelward/npm-update/"), False)

    def test_no_caller_is_none(self):
        self.assertIsNone(credentials.caller_inherits(CALLER, "mikelward/gradle-update/"))
        self.assertIsNone(credentials.caller_inherits("", "mikelward/npm-update/"))

    def test_a_quoted_reference_is_still_the_caller(self):
        # YAML allows the value quoted either way; unquoted-only reading
        # would report such a caller absent and its credential unused.
        for quote in ('"', "'"):
            quoted = CALLER.replace(
                "uses: mikelward/npm-update/.github/workflows/npm-update.yml@main",
                f"uses: {quote}mikelward/npm-update/.github/workflows/npm-update.yml@main{quote}",
            )
            self.assertIs(credentials.caller_inherits(quoted, "mikelward/npm-update/"), True, quote)
            self.assertIs(
                credentials.caller_inherits(quoted.replace("secrets: inherit", f"secrets: {quote}inherit{quote}"), "mikelward/npm-update/"),
                True,
                quote,
            )

    def test_a_quoted_key_is_still_the_key(self):
        for quote in ('"', "'"):
            quoted = CALLER.replace("    uses: mikelward", f"    {quote}uses{quote}: mikelward").replace(
                "    secrets: inherit", f"    {quote}secrets{quote}: inherit"
            )
            self.assertIs(credentials.caller_inherits(quoted, "mikelward/npm-update/"), True, quote)

    def test_mentions_is_the_backstop_for_shapes_the_reader_cannot_parse(self):
        flow = "jobs: {sync: {uses: mikelward/ci-commit-artifact/.github/workflows/commit-artifact.yml@main, secrets: inherit}}\n"
        self.assertIsNone(credentials.caller_inherits(flow, "mikelward/ci-commit-artifact/"))
        self.assertTrue(credentials.mentions(flow, "mikelward/ci-commit-artifact/"))
        self.assertFalse(credentials.mentions(CALLER, "mikelward/ci-commit-artifact/"))

    def test_a_trailing_comment_does_not_hide_the_value(self):
        commented = CALLER.replace(
            "npm-update.yml@main\n", "npm-update.yml@main  # @main is the release\n"
        ).replace("secrets: inherit\n", "secrets: inherit  # see zizmor.yml\n")
        self.assertIs(credentials.caller_inherits(commented, "mikelward/npm-update/"), True)

    def test_a_step_level_uses_is_not_a_caller(self):
        steps = """jobs:
  build:
    steps:
      - uses: mikelward/lanes@main
      - uses: "mikelward/lanes@main"
"""
        self.assertIsNone(credentials.caller_inherits(steps, "mikelward/lanes"))

    def test_every_calling_job_must_inherit(self):
        two = """jobs:
  a:
    uses: mikelward/ci-commit-artifact/.github/workflows/commit-artifact.yml@main
    secrets: inherit
  b:
    needs: a
    uses: mikelward/ci-commit-artifact/.github/workflows/commit-artifact.yml@main
    with:
      artifact-name: x
    secrets:
      push-token: ${{ secrets.T }}
"""
        self.assertIs(credentials.caller_inherits(two, "mikelward/ci-commit-artifact/"), False)
        both = two.replace("    secrets:\n      push-token: ${{ secrets.T }}\n", "    secrets: inherit\n")
        self.assertIs(credentials.caller_inherits(both, "mikelward/ci-commit-artifact/"), True)

    def test_a_deeper_secrets_key_is_not_the_jobs_own(self):
        # `secrets: inherit` at a deeper indentation belongs to something
        # else (a nested mapping), not to this job.
        nested = CALLER.replace("    secrets: inherit\n", "    with:\n      secrets: inherit\n")
        self.assertIs(credentials.caller_inherits(nested, "mikelward/npm-update/"), False)


    def test_a_trailing_comment_naming_the_workflow_is_not_a_second_mention(self):
        # `uses: ... # see mikelward/npm-update/ docs` is one caller, not
        # a caller and an unread mention; a `#` inside a quoted scalar is
        # not a comment, and a quoted scalar can span lines.
        prefix = "mikelward/npm-update/"
        for text in [
            "jobs:\n  update:\n"
            "    uses: mikelward/npm-update/.github/workflows/npm-update.yml@main # see mikelward/npm-update/ docs\n"
            "    secrets: inherit  # the mikelward/npm-update/ credential comes from its environment\n",
            "jobs:\n  update:\n"
            '    uses: "mikelward/npm-update/.github/workflows/npm-update.yml@main" # mikelward/npm-update/\n'
            "    secrets: inherit\n",
            "jobs:\n  update:\n"
            "    uses: 'mikelward/npm-update/.github/workflows/npm-update.yml@main' # mikelward/npm-update/\n"
            "    secrets: inherit\n",
            "name: it's the batch # mikelward/npm-update/\n"
            "jobs:\n  update:\n"
            "    uses: mikelward/npm-update/.github/workflows/npm-update.yml@main\n"
            "    secrets: inherit\n",
        ]:
            found = credentials.callers({"npm-update.yml": text}, prefix)
            self.assertEqual(found, {"npm-update.yml": True}, text)
            self.assertEqual(credentials.unread_mentions({"npm-update.yml": text}, prefix), [], text)
        # A `#` inside a quoted scalar is content, and the mention counts.
        quoted = 'env:\n  NOTE: "# mikelward/npm-update/ is the batch"\n'
        self.assertEqual(credentials.unread_mentions({"ci.yml": quoted}, prefix), ["ci.yml"])
        spanning = 'env:\n  NOTE: "no comment\n    # mikelward/npm-update/ here"\n'
        self.assertEqual(credentials.unread_mentions({"ci.yml": spanning}, prefix), ["ci.yml"])

    def test_the_repository_name_is_matched_in_any_case(self):
        # Owner and repository names are case-insensitive on GitHub, so a
        # caller spelling them differently is the same caller -- and a
        # mention in another case still holds a delete back.
        text = (
            "jobs:\n  sync:\n"
            "    uses: MikelWard/CI-Commit-Artifact/.github/workflows/commit-artifact.yml@main\n"
            "    secrets: inherit\n"
        )
        self.assertIs(credentials.caller_inherits(text, "mikelward/ci-commit-artifact/"), True)
        self.assertTrue(credentials.mentions(text, "mikelward/ci-commit-artifact/"))
        self.assertFalse(credentials.mentions(text, "mikelward/npm-update/"))


    def test_yaml_escapes_in_a_double_quoted_reference_are_decoded(self):
        # YAML reads `"mikelward\/npm-up\u0064ate/..."` as the plain
        # reference; a reader that did not would delete the credential of
        # a caller written that way as unused.
        text = (
            "jobs:\n  update:\n"
            '    uses: "mikelward\\/npm-up\\u0064ate/.github/workflows/npm-update.yml@main"\n'
            "    secrets: inherit\n"
        )
        self.assertIs(credentials.caller_inherits(text, "mikelward/npm-update/"), True)
        self.assertTrue(credentials.mentions(text, "mikelward/npm-update/"))
        self.assertFalse(credentials.mentions(text, "mikelward/gradle-update/"))
        # An escape YAML does not define stays as written rather than
        # raising or vanishing.
        odd = 'uses: "mikelward\\qnpm-update/"\n'
        self.assertFalse(credentials.mentions(odd, "mikelward/npm-update/"))


class RefQueryTest(unittest.TestCase):
    def test_a_branch_name_is_percent_encoded(self):
        # Sent raw, `feature/x#1` would read as `feature/x`.
        self.assertEqual(credentials._ref_suffix("feature/x#1"), "?ref=feature%2Fx%231")
        self.assertEqual(credentials._ref_suffix("a b&c?d"), "?ref=a%20b%26c%3Fd")
        self.assertEqual(credentials._ref_suffix(None), "")


class CallersTest(unittest.TestCase):
    PREFIX = "mikelward/npm-update/"
    CALLER = (
        "jobs:\n  update:\n"
        "    uses: mikelward/npm-update/.github/workflows/npm-update.yml@main\n"
        "    secrets: inherit\n"
    )
    NAMING = CALLER.replace("secrets: inherit", "secrets:\n      token: ${{ secrets.NPM_UPDATE_PAT }}")

    def test_every_workflow_calling_it_is_a_caller_whatever_its_name(self):
        # The fleet names the caller `<hub>.yml`; GitHub runs any name, so
        # a second caller under another one is read too.
        texts = {
            "ci.yml": "jobs:\n  build:\n    runs-on: ubuntu-latest\n",
            "npm-update.yml": self.CALLER,
            "weekly.yaml": self.NAMING,
        }
        self.assertEqual(credentials.callers(texts, self.PREFIX), {"npm-update.yml": True, "weekly.yaml": False})
        self.assertEqual(credentials.callers({"ci.yml": "jobs: {}\n"}, self.PREFIX), {})

    def test_a_mention_no_caller_resolves_is_unread(self):
        flow = "jobs: {update: {uses: mikelward/npm-update/.github/workflows/npm-update.yml@main, secrets: inherit}}\n"
        texts = {
            "npm-update.yml": self.CALLER,
            "batch.yml": flow,
            "ci.yml": "env:\n  HUB: mikelward/npm-update/\n",
            # A comment calls nothing, so it is neither a caller nor unread.
            "docs.yml": "# see mikelward/npm-update/README.md\n",
        }
        found = credentials.callers(texts, self.PREFIX)
        self.assertEqual(sorted(found), ["npm-update.yml"])
        self.assertEqual(credentials.unread_mentions(texts, self.PREFIX), ["batch.yml", "ci.yml"])
        self.assertFalse(credentials.mentions(texts["docs.yml"], self.PREFIX))
        # A resolved caller's own reference was read, so it is not "unread".
        self.assertEqual(credentials.unread_mentions({"npm-update.yml": self.CALLER}, self.PREFIX), [])

    def test_a_second_caller_the_reader_cannot_resolve_in_a_read_file_is_unread(self):
        # Counted per mention, not per file: the readable caller does not
        # vouch for a flow-style second one beside it.
        text = self.CALLER + (
            "  weekly: {uses: mikelward/npm-update/.github/workflows/npm-update.yml@main, "
            "secrets: {token: x}}\n"
        )
        self.assertEqual(credentials.callers({"npm-update.yml": text}, self.PREFIX), {"npm-update.yml": True})
        self.assertEqual(credentials.unread_mentions({"npm-update.yml": text}, self.PREFIX), ["npm-update.yml"])

    def test_an_escaped_line_break_in_a_double_quoted_reference_is_joined(self):
        # YAML joins `"mikelward/npm-up\` + newline + indented `date/..."`
        # into the plain reference, indentation dropped.
        text = (
            "jobs:\n  update:\n"
            '    uses: "mikelward/npm-up\\\n      date/.github/workflows/npm-update.yml@main"\n'
            "    secrets: inherit\n"
        )
        self.assertIs(credentials.caller_inherits(text, "mikelward/npm-update/"), True)
        self.assertTrue(credentials.mentions(text, "mikelward/npm-update/"))
        self.assertEqual(credentials.unread_mentions({"npm-update.yml": text}, "mikelward/npm-update/"), [])
        # A file checked in with CRLF line endings escapes the break as
        # backslash + CRLF, and joins the same way.
        crlf = text.replace("\n", "\r\n")
        self.assertIs(credentials.caller_inherits(crlf, "mikelward/npm-update/"), True)
        self.assertTrue(credentials.mentions(crlf, "mikelward/npm-update/"))
        self.assertEqual(credentials.unread_mentions({"npm-update.yml": crlf}, "mikelward/npm-update/"), [])

    def test_home_environment(self):
        self.assertEqual(credentials.home_environment("npm_update_pat"), "npm-update")
        self.assertEqual(credentials.home_environment("RUST_UPDATE_APP_PRIVATE_KEY"), "rust-update")
        self.assertEqual(credentials.home_environment("CI_COMMIT_ARTIFACT_TOKEN"), "ci-commit-artifact")
        self.assertIsNone(credentials.home_environment("RELEASE_KEYSTORE_BASE64"))


if __name__ == "__main__":
    unittest.main()
