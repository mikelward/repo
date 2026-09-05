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
    pinned, and anything else is "cannot tell"."""

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
            "        with: {mode: init, app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: x}\n",
        )
        self.assertNotEqual(flow, PUBLISHER)
        self.assertEqual(credentials.lanes_publishers({"ci.yml": flow}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": flow}), [])
        ambient = flow.replace("{mode: init, app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: x}", "{mode: init}")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": ambient}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": ambient}), [])
        # Inputs the reader cannot see -- an alias -- leave the step
        # unresolved, so the file is "cannot tell" rather than stale.
        alias = flow.replace("{mode: init, app-id: '${{ secrets.LANES_APP_ID }}', app-private-key: x}", "*inputs")
        self.assertEqual(credentials.lanes_publishers({"ci.yml": alias}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": alias}), ["ci.yml"])

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

    def test_flow_inputs_are_read_by_quote_and_nesting_state(self):
        # A regex over the flow text found `app-id:` inside a quoted VALUE
        # and read the step as a publisher; the scanner walks the mapping
        # with its quote and nesting state instead, and anything it cannot
        # read is "cannot tell" (Codex, mikelward/repo#36).
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
        ):
            text = flow(inputs)
            self.assertNotEqual(text, PUBLISHER)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {}, inputs)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), [], inputs)
        for inputs in (
            'mode: init, "app-id": x, app-private-key: y',
            "mode: init, 'app-id': x, app-private-key: y",
            'note: "a\\\\", app-id: x, app-private-key: y',
            "url: http://example.com, app-id: x, app-private-key: y",
            "mode: init, app-id: x, app-private-key: y,",
        ):
            self.assertEqual(credentials.lanes_publishers({"ci.yml": flow(inputs)}), {"ci.yml": True}, inputs)
        # What the scanner cannot read -- an unclosed quote or bracket, an
        # entry with no key -- leaves the step unresolved rather than
        # resolved as the ambient pattern.
        for inputs in (
            'mode: init, note: "open', "mode: init, [x", "mode", "a: 1, , b: 2",
            "mode: init, &input app-id: x", "mode: init, *alias: x", "? app-id : x", "mode: init, !!str app-id: x",
            'note: &x "x, app-id: placeholder", dummy: *x', "note: [&x 'a, app-id: b'], mode: init", "mode: !!str init",
        ):
            text = flow(inputs)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {}, inputs)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), ["ci.yml"], inputs)

    def test_a_block_key_the_reader_cannot_read_leaves_the_step_unread(self):
        # An anchored or complex key at the mapping's own level is a key
        # this does not read, and a mapping with one is not a read
        # mapping: the step is "cannot tell", never a list short of one
        # key that resolves the publisher away (Codex, mikelward/repo#36).
        for key in ("&input app-id", "? app-id\n          : x\n          mode", "*alias"):
            text = PUBLISHER.replace("          app-id:", f"          {key}:")
            self.assertNotEqual(text, PUBLISHER)
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {}, key)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), ["ci.yml"], key)
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
            "        with: {mode: init, APP-ID: x, App-Private-Key: y}\n",
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
            "        with: {mode: init, App-Id: '${{ secrets.LANES_APP_ID }}', app-private-key: x}\n",
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
            "        with : {mode : init, app-id : x, app-private-key : y}\n",
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
        for uses in ('"mikelward/lanes@main"', "'mikelward/lanes@v2'", "MikelWard/Lanes@abc1234", "mikelward/lanes"):
            text = PUBLISHER.replace("uses: mikelward/lanes@main", f"uses: {uses}")
            self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {"ci.yml": True}, uses)
            self.assertEqual(credentials.lanes_unread({"ci.yml": text}), [], uses)

    def test_a_shape_the_reader_cannot_resolve_is_unread(self):
        flow = "jobs: {init: {environment: lanes, steps: [{uses: mikelward/lanes@main, with: {app-id: x}}]}}\n"
        self.assertEqual(credentials.lanes_publishers({"ci.yml": flow}), {})
        self.assertEqual(credentials.lanes_unread({"ci.yml": flow}), ["ci.yml"])
        # A comment mentions nothing; a resolved step's own reference was read.
        self.assertEqual(credentials.lanes_unread({"ci.yml": "# see mikelward/lanes\njobs: {}\n"}), [])
        # A readable publisher beside an unreadable second step is unread too.
        self.assertEqual(
            credentials.lanes_unread({"ci.yml": PUBLISHER + "  extra: {steps: [{uses: mikelward/lanes@main}]}\n"}),
            ["ci.yml"],
        )

    def test_a_decoded_line_break_in_a_run_block_does_not_end_the_job(self):
        # A `run:` block scalar quoting `$'\n'` decodes to a line break
        # under the double-quoted-scalar reader; on its own line at column
        # 0 that ended the jobs section, so every job after it -- one
        # consumer's `finalize` -- went unread, and the file read as
        # "cannot tell" (the fail-closed direction, but a false one).
        text = (
            "jobs:\n  diff:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: |\n          existing=\"${ids%%$'\\n'*}\"\n"
            "          echo \"$existing\"\n"
        ) + PUBLISHER.split("jobs:\n", 1)[1]
        self.assertEqual(credentials.lanes_publishers({"ci.yml": text}), {"ci.yml": True})
        self.assertEqual(credentials.lanes_unread({"ci.yml": text}), [])

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
