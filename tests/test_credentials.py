import base64
import unittest
import urllib.parse
from unittest.mock import patch

from repo_lib import credentials, gh

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
    """The reading behind every move and every delete: which jobs call a
    reusable workflow, and whether they pass `secrets: inherit`. Its
    failure mode is a false "unused" -- a caller it cannot see makes the
    credential look stale, and setup deletes it -- so the shapes the fleet
    actually writes are pinned here, and so is what the parser rejects."""

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

    def test_mentions_is_the_backstop_for_a_document_the_parser_rejects(self):
        # Flow style is YAML like any other and reads as the caller it is;
        # what resolves nothing is a document PyYAML rejects, and every
        # mention in that is "cannot tell".
        flow = "jobs: {sync: {uses: mikelward/ci-commit-artifact/.github/workflows/commit-artifact.yml@main, secrets: inherit}}\n"
        self.assertIs(credentials.caller_inherits(flow, "mikelward/ci-commit-artifact/"), True)
        rejected = flow.replace("secrets: inherit}}", "secrets: inherit}")
        self.assertIsNone(credentials.caller_inherits(rejected, "mikelward/ci-commit-artifact/"))
        self.assertTrue(credentials.mentions(rejected, "mikelward/ci-commit-artifact/"))
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
        # A `#` inside a quoted scalar is content rather than a comment,
        # so `mentions` -- which counts the name anywhere in a string, the
        # direction that KEEPS a credential -- still sees it. A name in a
        # real comment calls nothing and is not a string at all.
        quoted = 'env:\n  NOTE: "# mikelward/npm-update/ is the batch"\n'
        self.assertTrue(credentials.mentions(quoted, prefix))
        spanning = 'env:\n  NOTE: "no comment\n    # mikelward/npm-update/ here"\n'
        self.assertTrue(credentials.mentions(spanning, prefix))
        self.assertFalse(credentials.mentions("env:\n  NOTE: plain # mikelward/npm-update/\n", prefix))

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


class WorkflowLabelTest(unittest.TestCase):
    """Workflow names are printed as they READ, not as they are stored: a
    caller is usually named after the batch it calls, so a line naming both
    said the same word twice with a `.yml` hung off one of them."""

    def test_an_extension_is_stripped(self):
        self.assertEqual(credentials.workflow_label("gradle-update.yml"), "gradle-update")
        self.assertEqual(credentials.workflow_label("ci.yaml"), "ci")

    def test_a_name_with_no_extension_is_unchanged(self):
        self.assertEqual(credentials.workflow_label("gradle-update"), "gradle-update")

    def test_an_inner_dot_is_kept(self):
        # Only the trailing extension goes; the rest of the name is the name.
        self.assertEqual(credentials.workflow_label("ci.release.yml"), "ci.release")

    def test_a_branch_qualified_name_keeps_its_branch(self):
        # `workflow_texts` reads other branches too, and only the file part
        # is a file name.
        name = credentials.WorkflowName("weekly.yml", "feature")
        self.assertEqual(credentials.workflow_label(name), "weekly on feature")
        self.assertEqual(str(name), "weekly.yml on feature")
        self.assertEqual(
            credentials.workflow_label(
                credentials.WorkflowName("weekly.yml", "feature/x#1")
            ),
            "weekly on feature/x#1",
        )

    def test_the_two_parts_are_never_parsed_back_out_of_one_string(self):
        # `_is_workflow` accepts any name ending in `.yml`/`.yaml`, spaces
        # included, so a filename may itself contain ` on ` -- and
        # `build.yml on prod.yml` as a filename is indistinguishable from
        # `build.yml` read on a branch named `prod.yml`. No rule can take
        # the old `f"{file} on {branch}"` key apart again, so the parts are
        # carried separately (Codex review, mikelward/repo#23).
        as_file = credentials.WorkflowName("build.yml on prod.yml")
        on_branch = credentials.WorkflowName("build.yml", "prod.yml")
        self.assertEqual(str(as_file), str(on_branch))
        self.assertEqual(credentials.workflow_label(as_file), "build.yml on prod")
        self.assertEqual(credentials.workflow_label(on_branch), "build on prod.yml")
        # And the same for a filename holding ` on ` with no branch at all.
        self.assertEqual(
            credentials.workflow_label(credentials.WorkflowName("deploy on push.yml")),
            "deploy on push",
        )

    def test_the_same_file_sorts_against_its_own_branch_copy(self):
        # `workflow_texts` keeps both when a workflow exists on the default
        # branch AND differs on another, and both audit and setup sort the
        # keys -- so a generated tuple ordering, comparing None with a
        # branch name, aborted the whole command on that routine case
        # (Codex review, mikelward/repo#23).
        default = credentials.WorkflowName("ci.yml")
        on_branch = credentials.WorkflowName("ci.yml", "feature")
        self.assertEqual(
            [str(n) for n in sorted([on_branch, default])],
            ["ci.yml", "ci.yml on feature"],
        )

    def test_a_bare_string_is_a_file_name_and_nothing_else(self):
        # Nothing parses ` on ` any more, so a plain string is only ever a
        # file: its extension goes and the rest is left alone.
        self.assertEqual(
            credentials.workflow_label("deploy on push.yml"), "deploy on push"
        )

    def test_a_list_is_joined(self):
        self.assertEqual(
            credentials.workflow_labels(["ci.yml", "release.yaml"]), "ci, release"
        )


class RefQueryTest(unittest.TestCase):
    def test_a_branch_name_is_percent_encoded(self):
        # Sent raw, `feature/x#1` would read as `feature/x`.
        self.assertEqual(credentials._ref_suffix("feature/x#1"), "?ref=feature%2Fx%231")
        self.assertEqual(credentials._ref_suffix("a b&c?d"), "?ref=a%20b%26c%3Fd")
        self.assertEqual(credentials._ref_suffix(None), "")


class LanesIndirectTest(unittest.TestCase):
    HEALTHY = (
        "name: ci\non: pull_request_target\njobs:\n  init:\n    runs-on: ubuntu-latest\n"
        "    environment: lanes\n    steps:\n      - uses: mikelward/lanes@main\n        with:\n"
        "          mode: init\n          app-id: ${{ secrets.LANES_APP_ID }}\n"
        "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n"
    )

    def test_a_healthy_publisher_accounts_for_its_own_mentions(self):
        self.assertEqual(credentials.lanes_indirect({"ci.yml": self.HEALTHY}), [])

    def test_a_shared_input_mapping_is_cannot_tell(self):
        # `_strings` visits a container once, so an anchored `with:` reused
        # by a lanes step and a composite one yields its mentions once
        # while consumption is counted per step -- the two balanced exactly
        # and the composite consumer went unseen (Codex, mikelward/repo#36).
        # Where the bases can disagree the comparison is not trusted.
        aliased = (
            "name: ci\non: pull_request_target\njobs:\n"
            "  init:\n    runs-on: ubuntu-latest\n    environment: lanes\n    steps:\n"
            "      - uses: mikelward/lanes@main\n        with: &creds\n          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n"
            "  extra:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: ./.github/actions/lanes-extra\n        with: *creds\n"
        )
        self.assertEqual(credentials.lanes_indirect({"ci.yml": aliased}), ["ci.yml"])

    def test_a_rejected_document_naming_the_pair_is_cannot_tell(self):
        self.assertEqual(
            credentials.lanes_indirect({"ci.yml": "env: [${{ secrets.LANES_APP_ID }}\n"}),
            ["ci.yml"],
        )

    def test_a_file_naming_neither_secret_is_not_reported(self):
        self.assertEqual(credentials.lanes_indirect({"ci.yml": "jobs: {}\n"}), [])


class LanesCalledWorkflowsTest(unittest.TestCase):
    """A job-level `uses:` this reader cannot follow can hold the lanes
    step that publishes, and the caller names neither the action nor either
    secret -- so the pair read as used by nothing and was deleted (Codex,
    mikelward/repo#36)."""

    def test_an_external_call_is_reported(self):
        text = (
            "name: ci\non: pull_request\njobs:\n  ci:\n"
            "    uses: some-org/shared/.github/workflows/ci.yml@main\n    secrets: inherit\n"
        )
        self.assertEqual(credentials.lanes_called_workflows({"ci.yml": text}), ["ci.yml"])

    def test_an_external_call_passing_nothing_is_reported_too(self):
        # The called job's own `environment: lanes` reaches the pair
        # whatever the caller passes, so the call is the signal rather than
        # the `secrets:` beside it.
        text = (
            "name: ci\non: pull_request\njobs:\n  ci:\n"
            "    uses: some-org/shared/.github/workflows/ci.yml@main\n"
        )
        self.assertEqual(credentials.lanes_called_workflows({"ci.yml": text}), ["ci.yml"])

    def test_a_local_call_is_not_one_of_these(self):
        # That file is among the texts and read directly, so a lanes step
        # in it counts as a publisher on its own account.
        text = (
            "name: ci\non: pull_request\njobs:\n  ci:\n"
            "    uses: ./.github/workflows/inner.yml\n    secrets: inherit\n"
        )
        self.assertEqual(credentials.lanes_called_workflows({"ci.yml": text}), [])

    def test_a_step_level_uses_is_not_a_call(self):
        text = (
            "name: ci\non: pull_request\njobs:\n  ci:\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
        )
        self.assertEqual(credentials.lanes_called_workflows({"ci.yml": text}), [])


class DefaultBranchPinTest(unittest.TestCase):
    """`workflow_texts` names the default branch on its own reads."""

    class Stub:
        """A repository whose trees answer only for the ref that names
        them, as GitHub's do: a branch that no longer exists is a 404,
        not a silent redirect to whatever the default is now."""

        def __init__(self, trees, reports):
            self.trees = trees  # branch -> {file: text}
            self.reports = reports  # what `.default_branch` returns
            self.endpoints = []

        def _ref(self, endpoint):
            """Which tree the request reads: an unqualified one resolves
            to whatever the default is AT REQUEST TIME, which is the whole
            hazard -- not to the name a caller read a moment earlier."""
            _, _, query = endpoint.partition("?ref=")
            return urllib.parse.unquote(query) if query else self.reports

        def run(self, args):
            endpoint, jq = args[1], args[-1]
            self.endpoints.append(endpoint)
            if jq == ".default_branch":
                return self.reports + "\n"
            if jq == ".[].name":
                return "".join(b + "\n" for b in self.trees)
            tree = self.trees.get(self._ref(endpoint))
            name = endpoint.split("/workflows/")[1].split("?")[0]
            if tree is None or name not in tree:
                raise gh.GhError("gh: HTTP 404: Not Found\n")
            return base64.encodebytes(tree[name].encode()).decode()

        def try_run(self, args):
            endpoint = args[1]
            self.endpoints.append(endpoint)
            tree = self.trees.get(self._ref(endpoint))
            if tree is None:
                return False, "gh: HTTP 404: Not Found\n"
            return True, "".join(f"{n} sha-{b}-{n}\n" for b in [self._ref(endpoint)] for n in tree)

    def _read(self, stub, default):
        with patch("repo_lib.gh.run", stub.run), patch("repo_lib.gh.try_run", stub.try_run):
            return credentials.workflow_texts("o/r", default=default)

    def test_the_default_branch_s_own_reads_name_it(self):
        # Unqualified, the Contents API answers from whatever the default
        # is at request time -- so the supplied name decided only which
        # branch the loop below skipped, and the copies it filed under the
        # bare name came from wherever the default happened to point.
        stub = self.Stub({"main": {"ci.yml": "jobs: {}\n"}}, "main")
        self.assertEqual(sorted(str(n) for n in self._read(stub, "main")), ["ci.yml"])
        for endpoint in stub.endpoints:
            if "/contents/.github/workflows" in endpoint:
                self.assertIn("?ref=main", endpoint, endpoint)

    def test_a_rename_before_the_read_is_a_failure_not_another_branch(self):
        # The rename lands after the caller read the name and before these
        # calls. Unqualified they returned TRUNK's copies filed as the
        # default's, while the environment policy was judged against
        # 'main'; a plan clean enough to queue no move never reached the
        # apply-time rename check, so the run exited 0 with the real
        # default branch shut out (Codex, mikelward/repo#36).
        stub = self.Stub({"trunk": {"ci.yml": "jobs: {}\n"}}, "trunk")
        with self.assertRaises(credentials.ReadError) as caught:
            self._read(stub, "main")
        self.assertIn("main", str(caught.exception))

    def test_an_unsupplied_default_is_resolved_then_pinned(self):
        # Reading the name itself is the same snapshot, so it is pinned
        # too -- the resolve does not license an unqualified read.
        stub = self.Stub({"trunk": {"ci.yml": "jobs: {}\n"}}, "trunk")
        self.assertEqual(sorted(str(n) for n in self._read(stub, None)), ["ci.yml"])
        for endpoint in stub.endpoints:
            if "/contents/.github/workflows" in endpoint:
                self.assertIn("?ref=trunk", endpoint, endpoint)


class CallersTest(unittest.TestCase):
    PREFIX = "mikelward/npm-update/"
    CALLER = (
        "jobs:\n  update:\n"
        "    uses: mikelward/npm-update/.github/workflows/npm-update.yml@main\n"
        "    secrets: inherit\n"
    )
    NAMING = CALLER.replace("secrets: inherit", "secrets:\n      token: ${{ secrets.NPM_UPDATE_PAT }}")

    def test_an_aliased_caller_job_is_one_node_on_both_sides(self):
        # `_strings` visits a container once, so an anchored job aliased
        # under a second name contributes ONE mention while the verdict
        # list held two -- and the two disagreeing by one let a third,
        # unresolvable mention balance the totals, so the file read as
        # fully understood and setup deleted the credential out from under
        # the caller it could not read. The lanes reader was given the
        # node-counting discipline first and this path was left mixing the
        # two (Codex, mikelward/repo#36).
        aliased = (
            "jobs:\n"
            "  update: &j\n"
            "    uses: mikelward/npm-update/.github/workflows/npm-update.yml@main\n"
            "    secrets: inherit\n"
            "  again: *j\n"
        )
        # On its own the aliased job is consistent: one node, one mention.
        self.assertEqual(credentials.unread_mentions({"ci.yml": aliased}, self.PREFIX), [])
        self.assertEqual(credentials.caller_inherits(aliased, self.PREFIX), True)
        # Both names are still callers, so the reading itself is unchanged.
        self.assertEqual(len(credentials._caller_verdicts(aliased, self.PREFIX)), 2)
        # Add a mention nothing resolves -- a step-level `uses:`, which is
        # not a job -- and the file is unread, which it was not before.
        with_step = aliased + (
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: mikelward/npm-update/.github/workflows/npm-update.yml@main\n"
        )
        self.assertEqual(credentials.unread_mentions({"ci.yml": with_step}, self.PREFIX), ["ci.yml"])

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
            # Flow style is YAML too, and reads as the caller it is.
            "batch.yml": flow,
            # A mention anywhere but a job's `uses:` resolves to no caller.
            "ci.yml": "env:\n  HUB: mikelward/npm-update/\n",
            # A document PyYAML rejects resolves nothing, so its mention is
            # read from the raw text and is unread.
            "broken.yml": self.CALLER.replace("secrets: inherit", "secrets: [inherit"),
            # A comment calls nothing, so it is neither a caller nor unread.
            "docs.yml": "# see mikelward/npm-update/README.md\n",
        }
        found = credentials.callers(texts, self.PREFIX)
        self.assertEqual(sorted(found), ["batch.yml", "npm-update.yml"])
        self.assertEqual(credentials.unread_mentions(texts, self.PREFIX), ["broken.yml", "ci.yml"])
        self.assertFalse(credentials.mentions(texts["docs.yml"], self.PREFIX))
        # A resolved caller's own reference was read, so it is not "unread".
        self.assertEqual(credentials.unread_mentions({"npm-update.yml": self.CALLER}, self.PREFIX), [])

    def test_a_second_mention_no_caller_resolves_in_a_read_file_is_unread(self):
        # Counted per mention, not per file: the readable caller does not
        # vouch for a second mention beside it that is not a job's `uses:`.
        text = self.CALLER + (
            "  weekly:\n    runs-on: ubuntu-latest\n"
            "    env:\n      HUB: mikelward/npm-update/.github/workflows/npm-update.yml@main\n"
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


PUBLISHER = """name: ci
on:
  pull_request_target:
jobs:
  init:
    runs-on: ubuntu-latest
    environment: lanes
    permissions:
      statuses: write
    steps:
      - uses: mikelward/lanes@main
        with:
          mode: init
          app-id: ${{ secrets.LANES_APP_ID }}
          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}
  classify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: mikelward/lanes@main
        with:
          mode: classify
"""


class LanesReaderTest(unittest.TestCase):
    """Which jobs publish the lanes status as the App, and whether each
    declares the environment the credential lives in. Same failure mode
    as the caller reader: a publisher it cannot see makes the pair look
    unused, and setup deletes it -- so the shapes the fleet writes are
    pinned, every YAML shape the line reader once refused reads as YAML
    reads it, and what PyYAML rejects is "cannot tell"."""

    def test_a_publishing_job_declaring_the_environment(self):
        self.assertEqual(credentials.lanes_publishers({"ci.yml": PUBLISHER}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": PUBLISHER}), [])

    def test_the_ambient_pattern_publishes_nothing(self):
        # classify above hands the action no credential; a workflow with
        # only such steps is neither a publisher nor unread.
        ambient = PUBLISHER.split("  classify:")[1]
        text = "jobs:\n  classify:" + ambient
        self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": text}), [])

    def test_a_publisher_without_the_environment_is_the_finding(self):
        bare = PUBLISHER.replace("    environment: lanes\n", "")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": bare}), {"ci.yml": False})
        other = PUBLISHER.replace("environment: lanes", "environment: production")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": other}), {"ci.yml": False})
        # A deeper `environment:` key -- a step's `with:`, say -- is not the job's.
        nested = bare.replace("          mode: init\n", "          mode: init\n          environment: lanes\n")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": nested}), {"ci.yml": False})

    def test_the_environment_may_be_a_block_and_any_case(self):
        block = PUBLISHER.replace("    environment: lanes\n", "    environment:\n      name: Lanes\n      url: https://example.com\n")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": block}), {"ci.yml": True})
        quoted = PUBLISHER.replace("environment: lanes", 'environment: "LANES"')
        self.assertEqual(credentials.lanes_publishers({"ci.yml": quoted}), {"ci.yml": True})

    def test_app_id_counts_only_inside_the_lanes_step(self):
        # Another action in the same job taking an input of that name -- a
        # token-minting step -- hands lanes nothing, so the lanes step is
        # still the ambient pattern (Codex, mikelward/repo#36).
        minting = PUBLISHER.replace(
            "      - uses: mikelward/lanes@main\n        with:\n          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
            "      - uses: actions/create-github-app-token@v2\n        with:\n"
            "          app-id: ${{ secrets.OTHER_APP_ID }}\n"
            "      - uses: mikelward/lanes@main\n        with:\n          mode: init\n",
        )
        self.assertNotEqual(minting, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": minting}), {})
        # The other way round is still a publisher, and so is a step whose
        # `uses:` follows a `name:` on the item's own dash line.
        both = PUBLISHER.replace(
            "      - uses: mikelward/lanes@main\n",
            "      - uses: actions/create-github-app-token@v2\n        with:\n          app-id: x\n"
            "      - name: Publish\n        uses: mikelward/lanes@main\n",
        )
        self.assertEqual(credentials.lanes_publishers({"ci.yml": both}), {"ci.yml": True})
        named = PUBLISHER.replace("      - uses: mikelward/lanes@main\n", "      - name: Publish\n        uses: mikelward/lanes@main\n")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": named}), {"ci.yml": True})

    def test_flow_style_inputs_are_read(self):
        # `with: {app-id: ..., app-private-key: ...}` hands the action the
        # credential exactly as the block form does; a reader that saw only
        # the block form deleted such a publisher's pair as unused (Codex,
        # mikelward/repo#36).
        flow = PUBLISHER.replace(
            "        with:\n          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
            "        with: {mode: init, app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'}\n",
        )
        self.assertNotEqual(flow, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": flow}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": flow}), [])
        ambient = flow.replace("{mode: init, app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'}", "{mode: init}")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": ambient}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": ambient}), [])
        # An alias reads as what it names -- here the ambient inputs the
        # first step anchors, on the second.
        aliased = flow.replace("{mode: init, app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'}", "&inputs {mode: classify}").replace(
            "        with:\n          mode: classify\n", "        with: *inputs\n"
        )
        self.assertNotEqual(aliased, flow)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": aliased}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": aliased}), [])
        # An alias naming no anchor is not YAML, and the file is "cannot
        # tell" rather than stale.
        dangling = flow.replace("{mode: init, app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'}", "*inputs")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": dangling}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": dangling}), ["ci.yml"])

    def test_the_action_named_in_prose_is_not_an_unresolved_reference(self):
        # The unread backstop compares references against what the reader
        # resolved, so counting the name wherever it appeared made a
        # healthy publisher unreadable whenever anything else in the file
        # said it: a workflow titled after the action, a job name, a step
        # name, a description. Setup then refused every move and
        # restriction that repository needed and its pair stayed a
        # repository secret -- the exposure the command exists to end
        # (Codex, mikelward/repo#36). A reference is the whole scalar.
        for prose in [
            "name: mikelward/lanes trusted publisher\n",
            "name: ci\nrun-name: gate by mikelward/lanes\n",
        ]:
            titled = PUBLISHER.replace("name: ci\n", prose, 1)
            self.assertNotEqual(titled, PUBLISHER)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": titled}), {"ci.yml": True})
            self.assertEqual(credentials.lanes_unread({"ci.yml": titled}), [], prose)
        # A reference the walk cannot reach is still one, and still holds
        # the delete back: that is what the backstop is for.
        for broken in [
            PUBLISHER.replace("jobs:\n", "jobs: [mikelward/lanes@main]\n#", 1),
            PUBLISHER.replace("    steps:\n      - uses: mikelward/lanes@main\n", "    steps: mikelward/lanes@main\n", 1),
        ]:
            self.assertNotEqual(broken, PUBLISHER)
            self.assertEqual(credentials.lanes_unread({"ci.yml": broken}), ["ci.yml"], broken)

    def test_the_batch_prefix_in_prose_is_not_an_unresolved_caller(self):
        # Same rule for a reusable workflow's caller: a repository whose
        # own workflow is titled after the batch resolved one caller and
        # counted two, so every credential move it needed was refused.
        prefix = "mikelward/npm-update/"
        text = (
            "name: mikelward/npm-update/ weekly\njobs:\n  update:\n"
            "    uses: mikelward/npm-update/.github/workflows/npm-update.yml@main\n"
            "    secrets: inherit\n"
        )
        self.assertEqual(credentials.callers({"a.yml": text}, prefix), {"a.yml": True})
        self.assertEqual(credentials.unread_mentions({"a.yml": text}, prefix), [])
        # And a caller the walk cannot reach still reads as unread.
        broken = text.replace("jobs:\n  update:\n    uses: ", "jobs: [", 1)
        self.assertEqual(credentials.unread_mentions({"a.yml": broken}, prefix), ["a.yml"])

    def test_a_quoted_uses_key_keeps_its_column(self):
        # `"uses":` starts one column before the word; a reader that found
        # the key by searching for the word read the step's `with:` as
        # shallower than the key and stopped before it, so a valid
        # publisher counted as the ambient pattern and its pair was
        # deleted (Codex, mikelward/repo#36). On the dash line and on its
        # own line alike.
        quoted = PUBLISHER.replace("      - uses: mikelward/lanes@main\n        with:\n          mode: init\n", '      - "uses": mikelward/lanes@main\n        with:\n          mode: init\n')
        self.assertNotEqual(quoted, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": quoted}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": quoted}), [])
        named = PUBLISHER.replace("      - uses: mikelward/lanes@main\n        with:\n          mode: init\n", "      - name: Publish\n        'uses': mikelward/lanes@main\n        with:\n          mode: init\n")
        self.assertNotEqual(named, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": named}), {"ci.yml": True})
        # And the quoted key still ends where the step ends: the ambient
        # classify step's quoted `uses:` does not swallow anything.
        ambient = quoted.replace("      - uses: mikelward/lanes@main\n        with:\n          mode: classify\n", '      - "uses": mikelward/lanes@main\n        with:\n          mode: classify\n')
        self.assertNotEqual(ambient, quoted)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": ambient}), {"ci.yml": True})

    def test_flow_inputs_are_read_as_yaml(self):
        # A regex over the flow text found `app-id:` inside a quoted VALUE
        # and read the step as a publisher (Codex, mikelward/repo#36); the
        # mapping is parsed now, so a quoted value, a nested collection, a
        # node property and an explicit key all read as YAML reads them,
        # and only what PyYAML rejects is "cannot tell".
        def flow(inputs):
            return PUBLISHER.replace(
                "        with:\n          mode: init\n"
                "          app-id: ${{ secrets.LANES_APP_ID }}\n"
                "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
                f"        with: {{{inputs}}}\n",
            )

        for inputs in (
            'mode: classify, note: "x, app-id: placeholder"',
            "mode: classify, note: 'it''s, app-id: z'",
            'mode: classify, note: "say \\"hi\\", app-id: z"',
            "mode: classify, extra: {app-id: nope}",
            "mode: classify, extra: [app-id, x]",
            "mode: it's classify, note: app-id",
            'mode: classify, note: ["], app-id: placeholder, dummy: ["]',
            "mode: classify, note: {k: '], app-id: placeholder', j: 1}",
            'mode: classify, note: ["a, b", "app-id: c"]',
            "mode",
            'note: &x "x, app-id: placeholder", dummy: *x',
            "note: [&x 'a, app-id: b'], mode: init",
            "mode: !!str init",
        ):
            text = flow(inputs)
            self.assertNotEqual(text, PUBLISHER)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {}, inputs)
            self.assertEqual(credentials.lanes_incomplete({"ci.yml": text}), [], inputs)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), [], inputs)
        for inputs in (
            'mode: init, "app-id": \'${{ secrets.LANES_APP_ID }}\', app-private-key: \'${{ secrets.LANES_APP_PRIVATE_KEY }}\'',
            "mode: init, 'app-id': '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'",
            'note: "a\\\\", app-id: \'${{ secrets.LANES_APP_ID }}\', app-private-key: \'${{ secrets.LANES_APP_PRIVATE_KEY }}\'',
            "url: http://example.com, app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'",
            "mode: init, app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}',",
            "mode: init, &input app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'",
            "? app-id : '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'",
            "mode: init, !!str app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'",
        ):
            self.assertEqual(credentials.lanes_publishers({"ci.yml": flow(inputs)}), {"ci.yml": True}, inputs)
        # What PyYAML rejects -- an unclosed quote or bracket, an entry with
        # no key, an alias naming no anchor -- is not a document at all, so
        # the file is unread rather than resolved as the ambient pattern.
        for inputs in ('mode: init, note: "open', "mode: init, [x", "a: 1, , b: 2", "mode: init, *alias: x"):
            text = flow(inputs)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {}, inputs)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), ["ci.yml"], inputs)

    def test_a_node_property_on_a_block_key_reads_as_yaml_reads_it(self):
        # An anchored, tagged or explicit key was one the line reader did
        # not read, and a mapping with one was "cannot tell" (Codex,
        # mikelward/repo#36); parsed, the key is `app-id` as YAML says.
        for key in ("&input app-id", "!!str app-id", "? app-id\n          : ${{ secrets.LANES_APP_ID }}\n          mode"):
            text = PUBLISHER.replace("          app-id:", f"          {key}:")
            self.assertNotEqual(text, PUBLISHER)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {"ci.yml": True}, key)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), [], key)
        # An alias naming no anchor is not a document, and the file is unread.
        dangling = PUBLISHER.replace("          app-id:", "          *alias:")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": dangling}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": dangling}), ["ci.yml"])
        # A deeper line under a key -- a block scalar's body -- is a value,
        # not a key, and reads fine.
        folded = PUBLISHER.replace("          mode: init\n", "          mode: >\n            init\n")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": folded}), {"ci.yml": True})

    def test_half_the_pair_is_a_finding_not_a_publisher(self):
        # `app-id` without `app-private-key` (or the reverse) cannot
        # authenticate as the App: not a publisher, not the ambient
        # pattern, and named so the pair is neither deleted as unused nor
        # moved for a step that publishes nothing (Codex, mikelward/repo#36).
        for drop in ("          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n", "          app-id: ${{ secrets.LANES_APP_ID }}\n"):
            text = PUBLISHER.replace(drop, "")
            self.assertNotEqual(text, PUBLISHER)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {})
            self.assertEqual(credentials.lanes_incomplete({"ci.yml": text}), ["ci.yml"])
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), [])
        self.assertEqual(credentials.lanes_incomplete({"ci.yml": PUBLISHER}), [])
        # The flow form and the case rule apply to the pair as to app-id.
        flow = PUBLISHER.replace(
            "        with:\n          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
            "        with: {mode: init, APP-ID: '${{ secrets.LANES_APP_ID }}', App-Private-Key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'}\n",
        )
        self.assertEqual(credentials.lanes_publishers({"ci.yml": flow}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_incomplete({"ci.yml": flow}), [])

    def test_input_names_are_case_insensitive(self):
        # GitHub matches action input names case-insensitively, so a step
        # writing `APP-ID:` receives the credential; a reader that missed
        # it deleted the pair as unused (Codex, mikelward/repo#36). Block
        # and flow forms alike.
        upper = PUBLISHER.replace("          app-id:", "          APP-ID:")
        self.assertNotEqual(upper, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": upper}), {"ci.yml": True})
        mixed = PUBLISHER.replace(
            "        with:\n          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
            "        with: {mode: init, App-Id: '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'}\n",
        )
        self.assertNotEqual(mixed, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": mixed}), {"ci.yml": True})

    def test_only_the_with_mapping_names_inputs(self):
        # `app-id` under `env:` -- or anywhere but the step's `with:` -- is
        # not an input the action receives (Codex, mikelward/repo#36).
        ambient = PUBLISHER.replace(
            "        with:\n          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
            "        env: {app-id: placeholder}\n        with:\n          mode: init\n",
        )
        self.assertNotEqual(ambient, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": ambient}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": ambient}), [])
        block_env = PUBLISHER.replace(
            "        with:\n          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
            "        env:\n          app-id: placeholder\n        with:\n          mode: init\n",
        )
        self.assertEqual(credentials.lanes_publishers({"ci.yml": block_env}), {})
        # A step with no `with:` at all is the ambient pattern too.
        bare = PUBLISHER.replace(
            "        with:\n          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
            "",
        )
        self.assertEqual(credentials.lanes_publishers({"ci.yml": bare}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": bare}), [])
        # Quoted keys are still the keys.
        quoted = PUBLISHER.replace("          app-id:", '          "app-id":')
        self.assertEqual(credentials.lanes_publishers({"ci.yml": quoted}), {"ci.yml": True})

    def test_whitespace_before_a_colon_is_still_the_key(self):
        # YAML permits `app-id : x`; a reader that rejected it read a
        # publisher as the ambient pattern (Codex, mikelward/repo#36).
        spaced = PUBLISHER.replace("          app-id:", "          app-id :").replace(
            "        with:", "        with :"
        ).replace("      - uses:", "      - uses :").replace("    environment:", "    environment :")
        self.assertNotEqual(spaced, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": spaced}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": spaced}), [])
        flow = PUBLISHER.replace(
            "        with:\n          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
            "        with : {mode : init, app-id : '${{ secrets.LANES_APP_ID }}', app-private-key : '${{ secrets.LANES_APP_PRIVATE_KEY }}'}\n",
        )
        self.assertEqual(credentials.lanes_publishers({"ci.yml": flow}), {"ci.yml": True})

    def test_every_publishing_job_must_declare_it(self):
        second = PUBLISHER + (
            "  finalize:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: mikelward/lanes@main\n        with:\n          mode: gate\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n"
        )
        self.assertEqual(credentials.lanes_publishers({"ci.yml": second}), {"ci.yml": False})

    def test_a_quoted_or_pinned_reference_is_still_the_action(self):
        for uses in ('"mikelward/lanes@main"', "'mikelward/lanes@v2'", "MikelWard/Lanes@abc1234"):
            text = PUBLISHER.replace("uses: mikelward/lanes@main", f"uses: {uses}")
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {"ci.yml": True}, uses)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), [], uses)

    def test_a_reference_with_no_ref_is_a_step_that_cannot_start(self):
        # GitHub requires `owner/repo@ref` for a remote action, so these are
        # workflows that fail to start. Reading one as a step made a job
        # that cannot run report as a healthy publisher, and setup kept or
        # moved the credential for a workflow publishing nothing (Codex,
        # mikelward/repo#36). It resolves to no step, so the mention is
        # unread -- "cannot tell", which holds everything.
        for uses in ("mikelward/lanes", "mikelward/lanes@", '"mikelward/lanes"'):
            text = PUBLISHER.replace("uses: mikelward/lanes@main", f"uses: {uses}")
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {}, uses)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), ["ci.yml"], uses)

    def test_an_aliased_step_is_one_node_on_both_sides_of_the_backstop(self):
        # `_strings` visits a container once, so an anchored step aliased
        # into two jobs contributes ONE mention while the per-job counts
        # saw it twice. That one-off let a third step which mentions the
        # action and resolves to nothing balance the totals, and the file
        # read as fully resolved -- setup trusting the aliased publishers
        # and deleting the pair out from under the step it could not read
        # (Codex, mikelward/repo#36).
        shared = (
            "jobs:\n"
            "  init:\n"
            "    environment: lanes\n"
            "    steps:\n"
            "      - &step\n"
            "        uses: mikelward/lanes@main\n"
            "        with:\n"
            "          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n"
            "  second:\n"
            "    environment: lanes\n"
            "    steps:\n"
            "      - *step\n"
        )
        # On its own the alias is consistent: one node, one mention, and
        # both jobs still read as publishers.
        self.assertEqual(credentials.lanes_unread({"ci.yml": shared}), [])
        self.assertEqual(credentials.lanes_publishers({"ci.yml": shared}), {"ci.yml": True})
        # Add a step the reader cannot resolve -- a `with:` that is not a
        # mapping -- and the file must be unread whatever the aliasing.
        opaque = shared + (
            "  third:\n"
            "    steps:\n"
            "      - uses: mikelward/lanes@main\n"
            "        with: [not, a, mapping]\n"
        )
        self.assertEqual(credentials.lanes_unread({"ci.yml": opaque}), ["ci.yml"])

    def test_a_shape_the_reader_cannot_resolve_is_unread(self):
        # A whole workflow in flow style reads as the publisher it is.
        flow = "jobs: {init: {environment: lanes, steps: [{uses: mikelward/lanes@main, with: {app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: '${{ secrets.LANES_APP_PRIVATE_KEY }}'}}]}}\n"
        self.assertEqual(credentials.lanes_publishers({"ci.yml": flow}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": flow}), [])
        # A document PyYAML rejects resolves no step, so its mention is unread.
        self.assertEqual(credentials.lanes_publishers({"ci.yml": flow[:-3] + "\n"}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": flow[:-3] + "\n"}), ["ci.yml"])
        # So is a step whose `with:` is not a mapping: its inputs cannot be read.
        scalar = PUBLISHER.replace(
            "        with:\n          mode: init\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
            "        with: app-id\n",
        )
        self.assertNotEqual(scalar, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": scalar}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": scalar}), ["ci.yml"])
        # A comment mentions nothing; a resolved step's own reference was read.
        self.assertEqual(credentials.lanes_unread({"ci.yml": "# see mikelward/lanes\njobs: {}\n"}), [])
        # A readable publisher beside an unreadable second step is unread too.
        self.assertEqual(
            credentials.lanes_unread({"ci.yml": PUBLISHER + "  extra: {steps: [{uses: mikelward/lanes@main, with: [x]}]}\n"}),
            ["ci.yml"],
        )

    def test_an_escaped_line_break_in_a_run_block_does_not_end_the_job(self):
        # A `run:` block scalar quoting `$'\n'` decoded to a line break
        # under the line reader this replaced; on its own line at column 0
        # that ended the jobs section, so every job after it -- one
        # consumer's `finalize` -- went unread, and the file read as
        # "cannot tell" (the fail-closed direction, but a false one).
        text = (
            "jobs:\n  diff:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: |\n          existing=\"${ids%%$'\\n'*}\"\n"
            "          echo \"$existing\"\n"
        ) + PUBLISHER.split("jobs:\n", 1)[1]
        self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": text}), [])

    def test_a_document_too_deep_to_parse_is_unread_not_a_crash(self):
        # `yaml.safe_load` exhausts its own stack on deep-but-valid
        # nesting, and `RecursionError` is not a `YAMLError`, so it left
        # the reader as a crashed command where every other unreadable
        # shape is a finding (Codex, mikelward/repo#36).
        deep = "mikelward/lanes: " + "[" * 500 + "]" * 500
        self.assertEqual(credentials.lanes_publishers({"ci.yml": deep}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": deep}), ["ci.yml"])
        self.assertEqual(credentials.lanes_incomplete({"ci.yml": deep}), [])
        # Not only the syntax half: a scalar YAML's own resolver claims and
        # Python then rejects raises a bare `ValueError` out of a
        # constructor -- `2026-13-01` reads as a timestamp and dies on
        # "month must be in 1..12" (Codex, mikelward/repo#36).
        dated = "mikelward/lanes: 2026-13-01"
        self.assertEqual(credentials.lanes_publishers({"ci.yml": dated}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": dated}), ["ci.yml"])

    def test_the_pair_the_step_reads_is_the_pair_it_consumes(self):
        # The input NAMES say the step authenticates as an App; only the
        # values say as which. A step wired to another App's secrets is not
        # a consumer of the fleet pair, and reading it as one reported that
        # pair healthy while the credential actually publishing sat at
        # repository level (Codex, mikelward/repo#36).
        other = PUBLISHER.replace("secrets.LANES_APP_ID", "secrets.OTHER_ID").replace(
            "secrets.LANES_APP_PRIVATE_KEY", "secrets.OTHER_KEY"
        )
        self.assertEqual(credentials.lanes_publishers({"ci.yml": other}), {})
        self.assertEqual(credentials.lanes_foreign({"ci.yml": other}), ["ci.yml"])
        # Resolved, so not "cannot tell" -- and not unused either: the
        # finding is what holds the pair, not a deletion.
        self.assertEqual(credentials.lanes_unread({"ci.yml": other}), [])
        # Half of the fleet's pair is still not the fleet's pair.
        mixed = PUBLISHER.replace("secrets.LANES_APP_PRIVATE_KEY", "secrets.OTHER_KEY")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": mixed}), {})
        self.assertEqual(credentials.lanes_foreign({"ci.yml": mixed}), ["ci.yml"])
        # Case-folded, as GitHub matches secret names.
        cased = PUBLISHER.replace("secrets.LANES_APP_ID", "secrets.lanes_app_id")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": cased}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_foreign({"ci.yml": cased}), [])
        # The value has to BE a secret reference, not merely contain the
        # name: a fallback resolves to whatever the other source holds,
        # which this cannot know, and the expected name's presence is not
        # the answer (Codex, mikelward/repo#36, twice -- once for a second
        # secret, once for a non-secret source).
        for value in (
            "${{ secrets.OTHER_ID || secrets.LANES_APP_ID }}",
            "${{ env.OTHER_ID || secrets.LANES_APP_ID }}",
            "${{ vars.PICK && secrets.LANES_APP_ID || secrets.OTHER_ID }}",
            "prefix-${{ secrets.LANES_APP_ID }}",
            "${{ secrets.LANES_APP_ID }} ${{ secrets.OTHER_ID }}",
        ):
            text = PUBLISHER.replace("${{ secrets.LANES_APP_ID }}", value)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {}, value)
            self.assertEqual(credentials.lanes_foreign({"ci.yml": text}), [], value)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), ["ci.yml"], value)
        # Whitespace inside the expression is still the same reference.
        spaced = PUBLISHER.replace("${{ secrets.LANES_APP_ID }}", "${{   secrets . LANES_APP_ID   }}")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": spaced}), {"ci.yml": True})
        # So is the index form, which Actions accepts alongside the dotted
        # one: reading only the dotted form reported a workflow written
        # that way unreadable, and setup then refused every move and
        # restriction it needed (Codex, mikelward/repo#36).
        for value in ("${{ secrets['LANES_APP_ID'] }}", "${{ secrets[ 'LANES_APP_ID' ] }}"):
            text = PUBLISHER.replace("${{ secrets.LANES_APP_ID }}", value)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {"ci.yml": True}, value)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), [], value)
        indexed_other = PUBLISHER.replace("${{ secrets.LANES_APP_ID }}", "${{ secrets['OTHER_ID'] }}")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": indexed_other}), {})
        self.assertEqual(credentials.lanes_foreign({"ci.yml": indexed_other}), ["ci.yml"])
        # A double-quoted index is not an Actions expression at all, so it
        # names no secret this can resolve -- "cannot tell", not the pair.
        quoted_index = PUBLISHER.replace("${{ secrets.LANES_APP_ID }}", '${{ secrets["LANES_APP_ID"] }}')
        self.assertEqual(credentials.lanes_publishers({"ci.yml": quoted_index}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": quoted_index}), ["ci.yml"])
        # A value naming no secret at all cannot be resolved either way:
        # "cannot tell", so the file is unread and nothing of its is
        # deleted -- never a guess in either direction.
        for value in ("${{ env.APP_ID }}", "${{ inputs.app-id }}", "12345", "${{ vars.APP_ID }}"):
            text = PUBLISHER.replace("${{ secrets.LANES_APP_ID }}", value)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {}, value)
            self.assertEqual(credentials.lanes_foreign({"ci.yml": text}), [], value)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), ["ci.yml"], value)

    def test_a_classify_step_holds_the_credential_but_publishes_nothing(self):
        # `classify` takes the pair for the generated lane -- it carries
        # forward only a `lanes` status the App itself posted, so it needs
        # the credential to know the App's login (mikelward/lanes's
        # action.yml). So it keeps the pair and needs the environment, and
        # is not a finding; what IS a finding is the pair reaching only
        # such steps, since then nothing publishes the required status as
        # the App (Codex, mikelward/repo#36).
        classify_only = PUBLISHER.replace("          mode: init\n", "          mode: classify\n")
        self.assertNotEqual(classify_only, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": classify_only}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": classify_only}), [])
        self.assertEqual(credentials.lanes_status_publishers({"ci.yml": classify_only}), [])
        # Any publishing mode answers it, on any job.
        for mode in ("init", "gate", "attest"):
            text = PUBLISHER.replace("          mode: init\n", f"          mode: {mode}\n")
            self.assertEqual(credentials.lanes_status_publishers({"ci.yml": text}), ["ci.yml"], mode)
        # But only spelled exactly as the action compares it. `lanes.mjs`
        # tests the raw input with `!==` against four lower-case words --
        # no folding, no trim -- and GitHub hands a `with:` value to a step
        # as written, so each of these throws `Unknown mode` exactly as a
        # typo does. Reading them as `gate` said a step published a status
        # when it could not start (Codex, mikelward/repo#36).
        for mode in ("Gate", "GATE", "' gate '", "'gate '"):
            text = PUBLISHER.replace("          mode: init\n", f"          mode: {mode}\n")
            self.assertEqual(credentials.lanes_status_publishers({"ci.yml": text}), [], mode)
            # Still a publisher for every other purpose: it holds the pair.
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {"ci.yml": True}, mode)
        # The input NAME is the opposite case, and stays case-insensitive:
        # GitHub upper-cases it into `INPUT_MODE` however it was written.
        named = PUBLISHER.replace("          mode: init\n", "          Mode: gate\n")
        self.assertEqual(credentials.lanes_status_publishers({"ci.yml": named}), ["ci.yml"])
        beside = classify_only + (
            "  finalize:\n    runs-on: ubuntu-latest\n    environment: lanes\n    steps:\n"
            "      - uses: mikelward/lanes@main\n        with:\n          mode: gate\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n"
        )
        self.assertEqual(credentials.lanes_status_publishers({"ci.yml": beside}), ["ci.yml"])
        # An expression is the one mode this cannot decide, so the one
        # that counts as publishing: "cannot tell" raises no finding of
        # its own. That holds even where a reader could see through the
        # expression -- `${{ 'classify' }}` always resolves to a mode that
        # publishes nothing, and deciding so needs an Actions expression
        # evaluator rather than a special case, since the next shape is
        # `format(...)`, then `inputs.mode`, then `env.M || 'init'`. Each
        # partial answer is a new way to be confidently wrong, which is
        # what cost this reader nine rounds before PyYAML replaced it
        # (Codex, mikelward/repo#36).
        for mode in ("${{ inputs.mode }}", "${{ 'classify' }}", "${{ env.M || 'init' }}"):
            expression = PUBLISHER.replace("          mode: init\n", f"          mode: {mode}\n")
            self.assertEqual(credentials.lanes_status_publishers({"ci.yml": expression}), ["ci.yml"], mode)
        # A mode that is missing, empty, or not a string is not "cannot
        # tell": `lanes.mjs` reads the input as a string and throws
        # `Unknown mode` unless it is exactly one of its four words, so
        # the step publishes nothing -- counting these as publishers hid
        # the finding for a step that cannot start (Codex,
        # mikelward/repo#36).
        for mode in ("\n            name: init", "\n            - gate", "", ": true"):
            text = PUBLISHER.replace("          mode: init\n", f"          mode:{mode}\n")
            self.assertEqual(credentials.lanes_status_publishers({"ci.yml": text}), [], repr(mode))
        gone = PUBLISHER.replace("          mode: init\n", "")
        self.assertEqual(credentials.lanes_status_publishers({"ci.yml": gone}), [])
        # Still a publisher for every other purpose: it holds the pair, so
        # it needs the environment and keeps the credential.
        self.assertEqual(credentials.lanes_publishers({"ci.yml": gone}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": gone}), [])
        # A word the action does not know publishes nothing either.
        typo = PUBLISHER.replace("          mode: init\n", "          mode: gaet\n")
        self.assertEqual(credentials.lanes_status_publishers({"ci.yml": typo}), [])
        # The ambient pattern hands the action nothing, so there is no
        # credentialed step to ask the mode of.
        ambient = "jobs:\n  classify:" + PUBLISHER.split("  classify:")[1]
        self.assertEqual(credentials.lanes_status_publishers({"ci.yml": ambient}), [])

    def test_a_neighboring_repository_name_is_not_a_lanes_mention(self):
        # `mikelward/lanes-helper@main` is a different repository, and
        # `_is_lanes` says so -- but the mention count was a substring, so
        # the file read as holding an unresolved mention and setup refused
        # every move the repository needed, leaving the pair at repository
        # level (Codex, mikelward/repo#36).
        neighbor = PUBLISHER + """  helper:
    runs-on: ubuntu-latest
    steps:
      - uses: mikelward/lanes-helper@main
"""
        self.assertEqual(credentials.lanes_unread({"ci.yml": neighbor}), [])
        self.assertEqual(credentials.lanes_publishers({"ci.yml": neighbor}), {"ci.yml": True})
        # A name that merely ends where this one does is still a mention:
        # `mikelward/lanes` with no ref, and a subdirectory reference, are
        # both shapes the reader cannot resolve, so both stay "cannot tell".
        for reference in ("mikelward/lanes", "mikelward/lanes/sub@main"):
            text = PUBLISHER + f"""  more:
    runs-on: ubuntu-latest
    steps:
      - uses: {reference}
"""
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), ["ci.yml"], reference)
        # And a name this one merely ends with is not one either.
        prefixed = PUBLISHER.replace("mikelward/lanes@main", "notmikelward/lanes@main", 1)
        self.assertEqual(credentials.lanes_unread({"ci.yml": prefixed}), [])

    def test_an_input_named_twice_in_two_cases_is_unreadable(self):
        # GitHub matches an action's input names case-insensitively, so
        # `app-id` beside `APP-ID` is one input written twice and nothing
        # here can say which value the step is handed. Taking the first
        # spelling read the fleet's secret as the answer while the other
        # named another App's, so the step reported as a healthy publisher
        # (Codex, mikelward/repo#36).
        collide = PUBLISHER.replace(
            "          app-id: ${{ secrets.LANES_APP_ID }}\n",
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          APP-ID: ${{ secrets.OTHER_ID }}\n",
        )
        self.assertNotEqual(collide, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": collide}), {})
        self.assertEqual(credentials.lanes_foreign({"ci.yml": collide}), [])
        self.assertEqual(credentials.lanes_incomplete({"ci.yml": collide}), [])
        self.assertEqual(credentials.lanes_unread({"ci.yml": collide}), ["ci.yml"])
        # The same for the mode, which this reader also consults.
        mode = PUBLISHER.replace(
            "          mode: init\n", "          mode: init\n          MODE: gate\n"
        )
        self.assertEqual(credentials.lanes_publishers({"ci.yml": mode}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": mode}), ["ci.yml"])
        # An input this reader never looks at is the author's problem, not
        # a value it misreads: the step still reads as it did.
        other = PUBLISHER.replace(
            "          mode: init\n", "          mode: init\n          token: a\n          TOKEN: b\n"
        )
        self.assertEqual(credentials.lanes_publishers({"ci.yml": other}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": other}), [])
        self.assertEqual(credentials.lanes_status_publishers({"ci.yml": other}), ["ci.yml"])
        # One spelling is still read whatever its case.
        cased = PUBLISHER.replace("          app-id:", "          APP-Id:")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": cased}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": cased}), [])

    def test_an_uncredentialed_gate_step_is_the_silent_fallback(self):
        # `gate` handed neither input is opt-in and does exactly what it did
        # before the credential existed: it lets the ambient check-run
        # report. So a workflow whose `init` step holds the pair and whose
        # `gate` step does not publishes the required verdict ambiently
        # while `lanes_status_publishers` reads non-empty on `init` alone
        # (Codex, mikelward/repo#36).
        mixed = PUBLISHER + """  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: mikelward/lanes@main
        with:
          mode: gate
"""
        self.assertEqual(credentials.lanes_status_publishers({"ci.yml": mixed}), ["ci.yml"])
        self.assertEqual(credentials.lanes_unread({"ci.yml": mixed}), [])
        self.assertEqual(credentials.lanes_incomplete({"ci.yml": mixed}), [])
        self.assertEqual(credentials.lanes_ambient_gate({"ci.yml": mixed}), ["ci.yml"])
        # Hand that step the pair and it is not a fallback any more.
        backed = mixed.replace(
            "        with:\n          mode: gate\n",
            "        with:\n          mode: gate\n"
            "          app-id: ${{ secrets.LANES_APP_ID }}\n"
            "          app-private-key: ${{ secrets.LANES_APP_PRIVATE_KEY }}\n",
        )
        self.assertNotEqual(backed, mixed)
        self.assertEqual(credentials.lanes_ambient_gate({"ci.yml": backed}), [])
        # The other publishing modes refuse to start without the pair, so
        # they are loud rather than silent and raise nothing here; nor does
        # an expression, which is "cannot tell".
        for mode in ("init", "attest", "classify", "${{ inputs.mode }}"):
            other = mixed.replace("          mode: gate\n", f"          mode: {mode}\n")
            self.assertEqual(credentials.lanes_ambient_gate({"ci.yml": other}), [], mode)
        # Half the pair is `lanes_incomplete`'s finding, not this one.
        half = mixed.replace(
            "        with:\n          mode: gate\n",
            "        with:\n          mode: gate\n          app-id: ${{ secrets.LANES_APP_ID }}\n",
        )
        self.assertEqual(credentials.lanes_ambient_gate({"ci.yml": half}), [])
        self.assertEqual(credentials.lanes_incomplete({"ci.yml": half}), ["ci.yml"])

    def test_a_cyclic_alias_is_walked_once_not_forever(self):
        # `yaml.safe_load` resolves an alias pointing at its own ancestor
        # into a self-referential object rather than rejecting it, and a
        # naive walk recurses until Python gives up -- a crashed command
        # where every other unreadable shape is a finding (Codex,
        # mikelward/repo#36).
        cyclic = "x: &x [*x]\n" + PUBLISHER
        self.assertEqual(credentials.lanes_publishers({"ci.yml": cyclic}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": cyclic}), [])
        selfish = "jobs: &j\n  init:\n    steps: []\n  more: *j\n"
        self.assertEqual(credentials.lanes_unread({"ci.yml": selfish}), [])
        # Identity, not equality: two equal lists are two nodes, and
        # skipping the second would drop real mentions.
        twice = (
            "one: [mikelward/lanes]\nagain: [mikelward/lanes]\n"
            "jobs:\n  init:\n    steps: []\n"
        )
        self.assertEqual(credentials.lanes_unread({"ci.yml": twice}), ["ci.yml"])

    def test_the_pair_belongs_in_the_lanes_environment(self):
        self.assertEqual(credentials.home_environment("lanes_app_id"), "lanes")
        self.assertEqual(credentials.home_environment("LANES_APP_PRIVATE_KEY"), "lanes")
        self.assertIn("LANES_APP_ID", credentials.FLEET_CREDENTIALS)
        self.assertTrue(credentials.lanes_usable({"LANES_APP_ID", "LANES_APP_PRIVATE_KEY", "X"}))
        self.assertFalse(credentials.lanes_usable({"LANES_APP_ID"}))
        self.assertFalse(credentials.lanes_usable({"LANES_PAT"}))


class BranchPolicyTest(unittest.TestCase):
    """The environment's own gate: which branches may reach the lanes
    credential. Only the trusted base branch may -- protected branches,
    or a custom policy naming exactly it."""

    def test_what_setup_may_restrict(self):
        self.assertTrue(credentials.restrictable("open"))
        self.assertTrue(credentials.restrictable([]))
        self.assertFalse(credentials.restrictable("protected"))
        self.assertFalse(credentials.restrictable(["main"]))
        self.assertFalse(credentials.restrictable(["release/*"]))

    def test_the_verdict(self):
        self.assertIsNone(credentials.branch_policy_verdict(["main"], "main"))
        # "Protected branches only" is every branch while no branch-protection
        # rule exists -- and this fleet protects main with a ruleset, which
        # is not one -- so it is never the guarantee (Codex, mikelward/repo#36).
        self.assertEqual(
            credentials.branch_policy_verdict("protected", "main"),
            "admits protected branches, which is every branch while no branch-protection rule exists "
            "(a ruleset is not one) and every protected branch otherwise",
        )
        self.assertEqual(credentials.branch_policy_verdict("open", "main"), "can be reached from any branch")
        self.assertEqual(credentials.branch_policy_verdict([], "main"), "admits no branch at all")
        self.assertEqual(
            credentials.branch_policy_verdict(["main", "tag:v*"], "main"),
            "is restricted to 'main', 'tag:v*', not to 'main' alone",
        )
        self.assertEqual(
            credentials.branch_policy_verdict(["release/*"], "main"),
            "is restricted to 'release/*', not to 'main' alone",
        )

    def test_the_policy_is_read_from_the_environment(self):
        import json
        from unittest.mock import patch

        def gh_run(args):
            endpoint = args[1] if args[1] != "--paginate" else args[2]
            if endpoint.endswith("/deployment-branch-policies"):
                return "branch main\ntag v*\n"
            return json.dumps({"deployment_branch_policy": self.policy})

        with patch("repo_lib.gh.run", gh_run):
            self.policy = None
            self.assertEqual(credentials.environment_branch_policy("o/r", "lanes"), "open")
            self.policy = {"protected_branches": True, "custom_branch_policies": False}
            self.assertEqual(credentials.environment_branch_policy("o/r", "lanes"), "protected")
            self.policy = {"protected_branches": False, "custom_branch_policies": True}
            self.assertEqual(credentials.environment_branch_policy("o/r", "lanes"), ["main", "tag:v*"])

    def test_a_policy_set_meanwhile_is_left_alone(self):
        from unittest.mock import patch

        writes = []
        # Settled to the default branch meanwhile: done, and said so.
        def satisfied(args):
            endpoint = args[1] if args[1] != "--paginate" else args[2]
            if endpoint.endswith("/deployment-branch-policies"):
                return "branch main\n"
            return '{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}'

        with patch("repo_lib.gh.run", satisfied), patch("repo_lib.gh.run_with_input", lambda args, body: writes.append(args)):
            self.assertEqual(
                credentials.restrict_environment("o/r", "lanes", "main"),
                "environment 'lanes' was restricted to branch 'main' since the plan was built",
            )
        # Set to something else: refused as a failure, never rewritten --
        # a run exiting 0 here would leave the audit's finding in place
        # (Codex, mikelward/repo#36).
        with patch("repo_lib.gh.run", lambda args: '{"deployment_branch_policy": {"protected_branches": true}}'), patch(
            "repo_lib.gh.run_with_input", lambda args, body: writes.append(args)
        ):
            with self.assertRaises(credentials.RestrictRefused) as refused:
                credentials.restrict_environment("o/r", "lanes", "main")
        self.assertIn("admits protected branches", str(refused.exception))
        self.assertIn("restrict it to 'main' by hand", str(refused.exception))
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
