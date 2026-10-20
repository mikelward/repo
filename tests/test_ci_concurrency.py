"""ci.yml's workflow-level concurrency group, pinned. Its failure is silent: a
group that cancels the wrong run leaves a required check canceled or missing
on a head, and one that cancels nothing quietly doubles every push's runs;
neither turns any job red."""

import os
import pathlib
import re
import unittest

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
MAIN = (
    "(github.event_name == 'push' || (github.event_name == 'workflow_dispatch'"
    " && github.ref == 'refs/heads/main'))"
)
BLOCK = re.search(r"^concurrency:\n((?: {2}.*\n)+)", WORKFLOW, re.M)


class CiConcurrencyTest(unittest.TestCase):
    def test_declares_one_workflow_level_group(self):
        self.assertIsNotNone(BLOCK, "ci.yml has no workflow-level concurrency block")
        self.assertEqual(len(re.findall(r"^concurrency:", WORKFLOW, re.M)), 1)

    def test_groups_a_pull_request_by_number_and_main_by_commit(self):
        self.assertIn(
            "  group: ci-${{ " + MAIN + " && format('main-{0}', github.sha)"
            " || github.event.pull_request.number || github.ref }}\n",
            BLOCK.group(1),
        )

    def test_cancels_on_a_push_open_or_reopen_never_on_main_or_an_edit(self):
        # An `edited` run carries the head as of the edit, and GitHub doesn't
        # promise to start runs in event order, so a canceling edit could
        # cancel the only run on a newer head; it queues instead.
        self.assertIn(
            "  cancel-in-progress: ${{ !" + MAIN + " && github.event.action != 'edited' }}\n",
            BLOCK.group(1),
        )

    def test_still_runs_on_an_edit_so_a_retarget_reruns_the_lane(self):
        self.assertRegex(WORKFLOW, r"types: \[opened, synchronize, reopened, edited\]")


if __name__ == "__main__":
    unittest.main()
