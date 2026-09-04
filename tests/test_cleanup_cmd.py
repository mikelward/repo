import contextlib
import datetime
import errno
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.parse import unquote

from repo_lib import cleanup_cmd, gh
from repo_lib.cli import main

REPO = "owner/repo"
NOW = datetime.datetime(2026, 9, 3, tzinfo=datetime.timezone.utc)


def days_ago(n):
    return (NOW - datetime.timedelta(days=n)).isoformat().replace("+00:00", "Z")


class FakeGh:
    """Stands in for repo_lib.gh.run/try_run. Answers from canned fixture
    data and records every call, so a test can assert on the exact gh
    invocation -- particularly that a DELETE was or was not issued."""

    def __init__(
        self,
        repo=REPO,
        alias=None,
        redirect_all=False,
        default_branch="main",
        branches=(),
        open_pulls=(),
        closed_pulls=(),
        compares=None,
        recompares=None,
        truncated_compares=(),
        commit_dates=None,
        commit_date_failures=(),
        delete_failures=(),
        compare_failures=(),
        repo_read_fails=False,
        branches_read_fails=False,
        pulls_read_fails=False,
        recheck=None,
        recheck_failures=(),
    ):
        self.repo = repo
        # A former owner/name this repository still answers to, as GitHub's
        # redirect does after a rename or transfer. Paths spelled with it
        # are served, but every response reports `repo` canonically.
        self.alias = alias
        # Serve EVERY aliased path, as GitHub's redirect really does,
        # instead of only the repository read. Off by default so a
        # regression that reverted to the caller's spelling fails on the
        # unexpected path; on for the test that needs to observe the
        # production harm itself -- a redirect makes every read succeed,
        # so the bug shows up as a deleted branch, not a failed call.
        self.redirect_all = redirect_all
        self.default_branch = default_branch
        # (name, sha, protected)
        self.branches = branches
        # dicts with number/head_ref/head_repo/base_ref (+ merged_at when closed)
        self.open_pulls = open_pulls
        self.closed_pulls = closed_pulls
        # branch -> (ahead_by, last_commit_date)
        self.compares = compares or {}
        # What the pre-delete containment recheck sees, when it must differ
        # from the plan's answer: branch -> (ahead_by, last_commit_date).
        self.recompares = recompares or {}
        self.compare_reads = {}
        # Branches whose compare response caps its `commits` array, as
        # GitHub's does past 250 -- so the last entry is not the tip.
        self.truncated_compares = set(truncated_compares)
        # sha -> committer date, for the tip read a truncated compare forces.
        self.commit_dates = commit_dates or {}
        self.commit_date_failures = set(commit_date_failures)
        self.delete_failures = set(delete_failures)
        self.compare_failures = set(compare_failures)
        self.repo_read_fails = repo_read_fails
        self.branches_read_fails = branches_read_fails
        self.pulls_read_fails = pulls_read_fails
        # What the immediately-before-delete re-read reports, when it must
        # differ from the plan-time state: branch -> (sha, protected).
        self.recheck = recheck or {}
        self.recheck_failures = set(recheck_failures)
        self.open_pull_reads = 0
        self.calls = []

    def _branch_sha(self, name):
        for n, sha, _ in self.branches:
            if n == name:
                return sha
        return "unknownsha"

    def _branch_protected(self, name):
        for n, _, protected in self.branches:
            if n == name:
                return protected
        return False

    def _pull_lines(self, pulls):
        return "".join(
            json.dumps(
                {
                    "number": p["number"],
                    "merged_at": p.get("merged_at"),
                    "head_ref": p["head_ref"],
                    # A merged pull request only proves the branch landed as
                    # it stood at this SHA, so it defaults to the branch's
                    # own tip; a test overrides it to model a branch pushed
                    # to after its merge.
                    "head_sha": p.get("head_sha", self._branch_sha(p["head_ref"])),
                    "head_repo": p.get("head_repo", REPO),
                    "base_ref": p.get("base_ref", "main"),
                }
            )
            + "\n"
            for p in pulls
        )

    def _resolve(self, args):
        """Serve an aliased path when this fake is modeling the redirect."""
        if not (self.alias and self.redirect_all):
            return args
        return [
            a.replace(f"repos/{self.alias}/", f"repos/{self.repo}/")
            if isinstance(a, str)
            else a
            for a in args
        ]

    def _repo_read(self, args):
        """True if `args` is the one repository read, by either spelling.

        ONLY this endpoint answers to the alias. Every other path must be
        spelled canonically or it falls through to the "unexpected call"
        assertion -- deliberately, so a regression that went back to the
        caller's spelling fails loudly here instead of being quietly
        served the way GitHub's real redirect would serve it.
        """
        names = [self.repo] + ([self.alias] if self.alias else [])
        return any(args[:2] == ["api", f"repos/{n}"] for n in names)

    def run(self, args):
        self.calls.append(list(args))
        args = self._resolve(args)
        if self._repo_read(args):
            if self.repo_read_fails:
                raise gh.GhError("gh: simulated repo read failure\n")
            return json.dumps(
                {"full_name": self.repo, "default_branch": self.default_branch}
            )
        if args[:2] == ["api", "--paginate"]:
            endpoint = args[2]
            if endpoint == f"repos/{self.repo}/branches":
                if self.branches_read_fails:
                    raise gh.GhError("gh: simulated branch list failure\n")
                return "".join(
                    json.dumps({"name": n, "sha": s, "protected": p}) + "\n"
                    for n, s, p in self.branches
                )
            if endpoint == f"repos/{self.repo}/pulls?state=open":
                if self.pulls_read_fails:
                    raise gh.GhError("gh: simulated pull list failure\n")
                self.open_pull_reads += 1
                return self._pull_lines(self.open_pulls)
            if endpoint == f"repos/{self.repo}/pulls?state=closed":
                if self.pulls_read_fails:
                    raise gh.GhError("gh: simulated pull list failure\n")
                return self._pull_lines(self.closed_pulls)
        raise AssertionError(f"unexpected gh.run call: {args}")

    def try_run(self, args):
        self.calls.append(list(args))
        args = self._resolve(args)
        if args[:2] == ["api", "--method"]:
            branch = unquote(
                args[3].split(f"repos/{self.repo}/git/refs/heads/", 1)[1]
            )
            if branch in self.delete_failures:
                return False, f"gh: HTTP 422 could not delete {branch}\n"
            return True, ""
        commit_prefix = f"repos/{self.repo}/commits/"
        if args[0] == "api" and args[1].startswith(commit_prefix):
            sha = args[1][len(commit_prefix) :]
            if sha in self.commit_date_failures:
                return False, f"gh: HTTP 404 no such commit {sha}\n"
            return True, self.commit_dates.get(sha, "") + "\n"
        state_prefix = f"repos/{self.repo}/branches/"
        if args[0] == "api" and args[1].startswith(state_prefix):
            name = unquote(args[1][len(state_prefix) :])
            if name in self.recheck_failures:
                return False, f"gh: HTTP 404 no such branch {name}\n"
            sha, protected = self.recheck.get(
                name, (self._branch_sha(name), self._branch_protected(name))
            )
            return True, json.dumps({"sha": sha, "protected": protected})
        prefix = f"repos/{self.repo}/compare/{self.default_branch}..."
        if args[0] == "api" and args[1].startswith(prefix):
            branch = unquote(args[1][len(prefix) :])
            if branch in self.compare_failures:
                return False, f"gh: HTTP 500 comparing {branch}\n"
            seen = self.compare_reads.get(branch, 0)
            self.compare_reads[branch] = seen + 1
            if seen and branch in self.recompares:
                ahead_by, last = self.recompares[branch]
            else:
                ahead_by, last = self.compares[branch]
            count = 250 if branch in self.truncated_compares else ahead_by
            return True, json.dumps(
                {"ahead_by": ahead_by, "count": count, "last": last}
            )
        raise AssertionError(f"unexpected gh.try_run call: {args}")

    def deleted(self):
        # `calls` holds the RAW args, so an aliased delete is spelled with
        # the old name; accept either, or a regression that deletes under
        # the alias shows up as an IndexError here instead of as the
        # deletion it actually is.
        names = [self.repo] + ([self.alias] if self.alias else [])
        out = []
        for c in self.calls:
            if c[:2] != ["api", "--method"]:
                continue
            for n in names:
                marker = f"repos/{n}/git/refs/heads/"
                if marker in c[3]:
                    out.append(unquote(c[3].split(marker, 1)[1]))
                    break
        return out


class _CaptureStream(io.StringIO):
    """A capture buffer that can claim to be a terminal.

    `redirect_stderr` hands the command a StringIO, whose isatty() is
    False -- so a test cannot model "a real terminal" and "2>file"
    separately without one of these.
    """

    def __init__(self, isatty=True):
        super().__init__()
        self._isatty = isatty

    def isatty(self):
        return self._isatty


class CleanupTestCase(unittest.TestCase):
    def invoke(
        self,
        fake,
        argv,
        stdin_isatty=True,
        stderr_isatty=True,
        answers=(),
        clock=None,
    ):
        """Run `repo cleanup ...` against `fake`, returning
        (code, out, err, log).

        `log` is the run's log file read back, or "" where none was
        written. A `--log` already in `argv` is left alone; otherwise one
        is pointed at a temporary directory, so no test writes into the
        real state directory and every test can assert on both halves --
        what the terminal said and what the record kept.

        `clock`, when given, is a callable returning the current time; the
        index-staleness tests advance it per call rather than sleeping.
        """
        answer_iter = iter(answers)

        def fake_input():
            # EOFError, not StopIteration: that is what a real terminal
            # raises on ^D, so a test running out of answers exercises the
            # command's own end-of-input path rather than the stub's.
            try:
                return next(answer_iter)
            except StopIteration:
                raise EOFError

        # stdout stays a plain buffer (isatty() False): the widget picker
        # requires a terminal there, so the suite drives the prompt path
        # whether or not prompt_toolkit happens to be installed. stderr's
        # answer is a parameter because it decides whether a question can
        # be asked at all.
        out, err = io.StringIO(), _CaptureStream(stderr_isatty)
        argv = list(argv)
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "cleanup.log")
            if "--log" not in argv:
                argv += ["--log", log_path]
            else:
                log_path = argv[argv.index("--log") + 1]
            code = self._invoke(
                fake, argv, stdin_isatty, fake_input, out, err, clock
            )
            try:
                with open(log_path, encoding="utf-8") as f:
                    log = f.read()
            except OSError:
                log = ""
        return code, out.getvalue(), err.getvalue(), log

    def _invoke(self, fake, argv, stdin_isatty, fake_input, out, err, clock):
        with patch.object(gh, "run", fake.run), patch.object(
            gh, "try_run", fake.try_run
        ), patch.object(gh, "require_gh", lambda: None), patch(
            "repo_lib.cleanup_cmd.sys.stdin"
        ) as stdin, patch(
            "repo_lib.cleanup_cmd._now", clock or (lambda: NOW)
        ), patch(
            "builtins.input", fake_input
        ):
            stdin.isatty.return_value = stdin_isatty
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(["cleanup", *argv])
            except SystemExit as e:
                code = e.code
        return code


class StatusLineTest(CleanupTestCase):
    def test_the_repo_being_checked_is_announced_before_the_work(self):
        # Several seconds of API calls separate the command starting from
        # its plan. Without this the terminal shows nothing at all in that
        # window, which is indistinguishable from a hang.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[{"number": 7, "head_ref": "claude/done", "merged_at": "x"}],
        )
        code, out, err, _log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 0)
        self.assertIn(f"Checking {REPO}", err)

    def test_the_status_line_stays_out_of_the_plan_on_stdout(self):
        # stderr, so `repo cleanup --dry-run owner/repo > plan` still gets
        # exactly the plan, and the wait is still visible on the terminal.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[{"number": 7, "head_ref": "claude/done", "merged_at": "x"}],
        )
        code, out, err, _log = self.invoke(fake, [REPO, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn(f"Checking {REPO}", err)
        self.assertNotIn("Checking", out)


class ClassificationTest(CleanupTestCase):
    def test_a_rebase_merged_branch_is_deletable_via_its_merged_pull_request(self):
        # The case that motivates the whole command: the fleet rebase-merges,
        # so this branch is NOT an ancestor of main and compare would call it
        # unmerged. The merged pull request is what proves it landed.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[{"number": 7, "head_ref": "claude/done", "merged_at": "x"}],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), ["claude/done"])
        self.assertIn("merged by PR #7", log)
        # No compare was needed -- the merged pull request answered it.
        self.assertFalse(
            [c for c in fake.calls if len(c) > 1 and "/compare/" in str(c[1])]
        )

    def test_a_branch_contained_in_the_default_branch_is_deletable_with_no_pr(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("old", "bbb", False)],
            compares={"old": (0, days_ago(200))},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), ["old"])
        self.assertIn("already contained in main", log)

    def test_an_unmerged_branch_is_never_swept_by_default(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/wip", "bbb", False)],
            compares={"claude/wip": (3, days_ago(30))},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("3 commits not on the default branch", log)
        self.assertIn("only offered on a terminal", log)

    def test_a_closed_unmerged_pull_request_is_reported_as_the_abandoned_signal(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/dropped", "bbb", False)],
            closed_pulls=[
                {"number": 12, "head_ref": "claude/dropped", "merged_at": None}
            ],
            compares={"claude/dropped": (2, days_ago(40))},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 0)
        self.assertIn("PR #12 closed without merging", log)

    def test_a_merged_pull_request_wins_over_a_closed_one_on_the_same_branch(self):
        # A branch reopened and re-merged has both; the merge is what counts.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/retried", "bbb", False)],
            closed_pulls=[
                {"number": 3, "head_ref": "claude/retried", "merged_at": None},
                {"number": 4, "head_ref": "claude/retried", "merged_at": "x"},
            ],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), ["claude/retried"])
        self.assertIn("merged by PR #4", log)

    def test_a_merged_pull_request_from_a_fork_does_not_mark_a_local_branch(self):
        # The fork's branch happens to share a name with a live local one.
        # Reading the fork's merge as this branch's would delete real work.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("patch-1", "bbb", False)],
            closed_pulls=[
                {
                    "number": 9,
                    "head_ref": "patch-1",
                    "head_repo": "someone-else/repo",
                    "merged_at": "x",
                }
            ],
            compares={"patch-1": (5, days_ago(3))},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("5 commits not on the default branch", log)


class ProtectionTest(CleanupTestCase):
    def test_the_default_and_protected_branches_are_left_alone(self):
        fake = FakeGh(
            branches=[
                ("main", "aaa", False),  # default, but not flagged protected
                ("release", "bbb", True),
            ],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("main: the default branch", log)
        self.assertIn("release: protected", log)

    def test_an_open_pull_requests_head_is_left_alone_even_when_merged_before(self):
        # Reopened work: an older merged PR would otherwise mark it deletable
        # while a live PR is still pointing at it.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/live", "bbb", False)],
            open_pulls=[{"number": 20, "head_ref": "claude/live"}],
            closed_pulls=[{"number": 5, "head_ref": "claude/live", "merged_at": "x"}],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("head of open PR #20", log)

    def test_an_open_pull_requests_base_is_left_alone(self):
        # The stacked-pair trap: `lower` is merged and looks sweepable, but
        # an open PR is stacked on top of it and deleting it closes that PR.
        fake = FakeGh(
            branches=[
                ("main", "aaa", True),
                ("claude/lower", "bbb", False),
                ("claude/upper", "ccc", False),
            ],
            open_pulls=[
                {"number": 31, "head_ref": "claude/upper", "base_ref": "claude/lower"}
            ],
            closed_pulls=[{"number": 30, "head_ref": "claude/lower", "merged_at": "x"}],
            compares={"claude/upper": (1, days_ago(1))},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("base of open PR #31", log)
        self.assertIn("would close that PR", log)

    def test_a_traversing_branch_name_is_left_alone(self):
        # No encoding fixes a name whose segments address a different
        # endpoint than the one that was validated.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("bad/../name", "bbb", False)],
            closed_pulls=[{"number": 1, "head_ref": "bad/../name", "merged_at": "x"}],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("safely address as a ref path", log)

    def test_a_legal_but_awkward_branch_name_is_encoded_not_skipped(self):
        # `release#1` and `release%231` are legal branch names. Refusing
        # them put them beyond cleanup's reach forever (Codex review,
        # mikelward/repo#20).
        for name in ("release#1", "release%231", "has space"):
            with self.subTest(name=name):
                fake = FakeGh(
                    branches=[("main", "aaa", True), (name, "bbb", False)],
                    closed_pulls=[{"number": 1, "head_ref": name, "merged_at": "x"}],
                )
                code, out, err, log = self.invoke(fake, [REPO, "--force"])
                self.assertEqual(fake.deleted(), [name])

    def test_a_fork_pull_requests_base_branch_is_still_protected(self):
        # The head filter must not reach the base: a fork's PR targets a
        # branch HERE, and deleting it closes that still-open PR (Codex
        # review, mikelward/repo#20).
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/target", "bbb", False)],
            open_pulls=[
                {
                    "number": 50,
                    "head_ref": "their-branch",
                    "head_repo": "someone-else/repo",
                    "base_ref": "claude/target",
                }
            ],
            closed_pulls=[
                {"number": 49, "head_ref": "claude/target", "merged_at": "x"}
            ],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("base of open PR #50", log)

    def test_a_renamed_repository_invoked_by_its_old_name_still_protects_heads(
        self,
    ):
        # A renamed or transferred repository still answers to its old
        # name -- GitHub redirects -- but reports head.repo.full_name
        # CANONICALLY. Comparing that against the caller's spelling made
        # every local head look like a fork, so an open PR's head was
        # neither protected nor merged-classified; one already contained
        # in the default branch was then swept and its PR closed.
        fake = FakeGh(
            repo="owner/newname",
            alias="owner/oldname",
            # Model the real redirect, so the bug presents as production
            # would present it: every read succeeds and the branch is
            # deleted, rather than a call failing on an unexpected path.
            redirect_all=True,
            branches=[("main", "aaa", True), ("claude/live", "bbb", False)],
            open_pulls=[
                {
                    "number": 77,
                    "head_ref": "claude/live",
                    "head_repo": "owner/newname",
                    "base_ref": "main",
                }
            ],
            # Contained in main, so nothing but the head protection stands
            # between this branch and the sweep.
            compares={"claude/live": (0, "2026-08-01T00:00:00Z")},
        )
        code, out, err, log = self.invoke(fake, ["owner/oldname", "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("head of open PR #77", log)

    def test_a_renamed_repository_uses_the_canonical_name_for_later_calls(self):
        # The corollary, asserted directly rather than inferred: only the
        # one repository read may be spelled with the old name. If any
        # later call still used it the fake would raise on the unexpected
        # path, but assert it here so the reason is stated rather than
        # showing up as an opaque failure.
        fake = FakeGh(
            repo="owner/newname",
            alias="owner/oldname",
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[
                {
                    "number": 12,
                    "head_ref": "claude/done",
                    "head_repo": "owner/newname",
                    "merged_at": "2026-08-01T00:00:00Z",
                    "head_sha": "bbb",
                }
            ],
        )
        code, out, err, log = self.invoke(fake, ["owner/oldname", "--force"])
        self.assertEqual(fake.deleted(), ["claude/done"])
        aliased = [
            c
            for c in fake.calls
            if any("owner/oldname" in a for a in c if isinstance(a, str))
        ]
        self.assertEqual(len(aliased), 1, aliased)
        self.assertEqual(aliased[0], ["api", "repos/owner/oldname", "--jq",
                                      "{full_name, default_branch}"])

    def test_a_differently_cased_owner_repo_still_matches_its_pull_requests(self):
        # GitHub names are case-insensitive, and head.repo.full_name comes
        # back canonically cased. Comparing it against the caller's spelling
        # exactly would drop every pull request, so this merged branch would
        # read as unmerged and survive the sweep.
        fake = FakeGh(
            repo="Owner/Repo",
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[
                {
                    "number": 7,
                    "head_ref": "claude/done",
                    "head_repo": "owner/repo",
                    "merged_at": "x",
                }
            ],
        )
        code, out, err, log = self.invoke(fake, ["Owner/Repo", "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), ["claude/done"])


class PlanGroupingTest(CleanupTestCase):
    """A fleet-sized plan is mostly one prefix repeated -- simmo's 184 dead
    branches were 172 `claude/`, 5 `codex/` and 2 `deps/`. Printing it once
    per group leaves the part that differs (maintainer, 2026-09-03)."""

    def _merged(self, *names):
        return FakeGh(
            branches=[("main", "aaa", True)]
            + [(n, f"sha{i}", False) for i, n in enumerate(names)],
            closed_pulls=[
                {
                    "number": i,
                    "head_ref": n,
                    "merged_at": "x",
                    "head_sha": f"sha{i}",
                }
                for i, n in enumerate(names)
            ],
        )

    def test_a_shared_prefix_is_printed_once_and_dropped_from_members(self):
        fake = self._merged("claude/one", "claude/two", "claude/three")
        code, out, err, log = self.invoke(fake, [REPO, "--dry-run"])
        self.assertIn("  claude/ (3):", out)
        self.assertIn("    one: ", out)
        self.assertIn("    two: ", out)
        # The prefix appears in the heading and nowhere else.
        self.assertEqual(out.count("claude/"), 1)

    def test_a_prefix_held_by_one_branch_gets_no_heading(self):
        # A heading over a single line is worse than the repetition it
        # saves, so it prints in full instead.
        fake = self._merged("claude/one", "claude/two", "codex/only")
        code, out, err, log = self.invoke(fake, [REPO, "--dry-run"])
        self.assertIn("  claude/ (2):", out)
        self.assertNotIn("codex/ (1):", out)
        self.assertIn("  codex/only: ", out)

    def test_a_branch_with_no_prefix_prints_in_full(self):
        fake = self._merged("claude/one", "claude/two", "hotfix")
        code, out, err, log = self.invoke(fake, [REPO, "--dry-run"])
        self.assertIn("  hotfix: ", out)

    def test_groups_come_before_the_ungrouped_remainder(self):
        fake = self._merged("zz/one", "zz/two", "aaa-loose")
        code, out, err, log = self.invoke(fake, [REPO, "--dry-run"])
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertLess(
            lines.index("  zz/ (2):"),
            next(i for i, l in enumerate(lines) if l.startswith("  aaa-loose:")),
        )

    def test_the_offered_and_kept_sections_group_too(self):
        fake = FakeGh(
            branches=[("main", "aaa", True)]
            + [(f"claude/old{i}", f"o{i}", False) for i in range(2)]
            + [(f"claude/new{i}", f"n{i}", False) for i in range(2)],
            compares={
                **{f"claude/old{i}": (1, days_ago(90)) for i in range(2)},
                **{f"claude/new{i}": (1, days_ago(0)) for i in range(2)},
            },
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"]
        )
        # Once in the offered section, once in the too-recent one.
        self.assertEqual(out.count("claude/ (2):"), 2)


class AgeTest(CleanupTestCase):
    def test_an_unmerged_branch_newer_than_the_threshold_is_not_offered(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/fresh", "bbb", False)],
            compares={"claude/fresh": (1, days_ago(2))},
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"], answers=[]
        )
        self.assertEqual(code, 0)
        self.assertIn("too recent to offer", out)

    def test_older_than_moves_the_threshold(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/fresh", "bbb", False)],
            compares={"claude/fresh": (1, days_ago(2))},
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--older-than", "1", "--dry-run"]
        )
        self.assertIn("to ask about", out)
        self.assertNotIn("too recent to offer", out)

    def test_a_closed_pull_request_is_offered_however_recent(self):
        # Age is a proxy for abandonment. A closed pull request is somebody
        # saying so outright, so the proxy has nothing to add -- a branch
        # closed an hour ago is as finished with as one closed in March
        # (maintainer, 2026-09-03).
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/dropped", "bbb", False)],
            compares={"claude/dropped": (2, days_ago(0))},
            closed_pulls=[
                {"number": 31, "head_ref": "claude/dropped", "head_sha": "bbb"}
            ],
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"]
        )
        self.assertIn("to ask about", out)
        self.assertNotIn("too recent to offer", out)
        self.assertIn("PR #31 closed without merging", out)

    def test_a_stacked_pull_request_closed_by_base_deletion_keeps_the_age_gate(
        self,
    ):
        # GitHub closes a pull request automatically when its base branch is
        # deleted, so in a stack the upper one goes closed without anybody
        # deciding anything -- and its head may be live work. `repo cleanup`
        # can cause exactly that itself by sweeping the merged lower branch
        # (Codex review, mikelward/repo#22).
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/upper", "bbb", False)],
            compares={"claude/upper": (2, days_ago(0))},
            closed_pulls=[
                {
                    "number": 41,
                    "head_ref": "claude/upper",
                    "head_sha": "bbb",
                    "base_ref": "claude/lower",
                }
            ],
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"]
        )
        self.assertIn("too recent to offer", out)
        self.assertNotIn("to offer one at a time", out)
        # The label still shows -- it is informative; only the age exemption
        # is withheld.
        self.assertIn("PR #41 closed without merging", out)

    def test_a_non_default_closure_does_not_mask_a_default_one(self):
        # One SHA can carry several closed pull requests. Keeping whichever
        # the API listed first let a stacked closure hide a default-branch
        # one and withhold the exemption (Codex review, mikelward/repo#22).
        # The non-default one is listed FIRST here, which is the order that
        # used to lose.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/both", "bbb", False)],
            compares={"claude/both": (2, days_ago(0))},
            closed_pulls=[
                {
                    "number": 50,
                    "head_ref": "claude/both",
                    "head_sha": "bbb",
                    "base_ref": "claude/lower",
                },
                {
                    "number": 51,
                    "head_ref": "claude/both",
                    "head_sha": "bbb",
                    "base_ref": "main",
                },
            ],
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"]
        )
        self.assertIn("to ask about", out)
        self.assertNotIn("too recent to offer", out)
        # And it names the closure that justifies the offer, not the one
        # that happened to be listed first.
        self.assertIn("PR #51 closed without merging", out)

    def test_only_non_default_closures_still_keep_the_age_gate(self):
        # The aggregate is `any`, not `first` -- with no default-base
        # closure among them the exemption stays withheld.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/both", "bbb", False)],
            compares={"claude/both": (2, days_ago(0))},
            closed_pulls=[
                {
                    "number": 52,
                    "head_ref": "claude/both",
                    "head_sha": "bbb",
                    "base_ref": "claude/lower",
                },
                {
                    "number": 53,
                    "head_ref": "claude/both",
                    "head_sha": "bbb",
                    "base_ref": "claude/other",
                },
            ],
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"]
        )
        self.assertIn("too recent to offer", out)

    def test_the_too_recent_heading_does_not_contradict_its_own_lines(self):
        # The held-back group is not "no pull request": a closure against a
        # non-default base lands here too, and its detail line says so, so a
        # heading claiming otherwise contradicts the entry beneath it (Codex
        # review, mikelward/repo#22).
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/upper", "bbb", False)],
            compares={"claude/upper": (2, days_ago(0))},
            closed_pulls=[
                {
                    "number": 43,
                    "head_ref": "claude/upper",
                    "head_sha": "bbb",
                    "base_ref": "claude/lower",
                }
            ],
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"]
        )
        self.assertIn("Unmerged and too recent to offer (1):", out)
        self.assertNotIn("no pull request", out)
        self.assertIn("PR #43 closed without merging", out)

    def test_the_offered_heading_names_the_default_branch(self):
        # The exemption is a closure against the default branch specifically,
        # so the heading says which branch that is rather than implying any
        # closure qualifies.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/dropped", "bbb", False)],
            compares={"claude/dropped": (2, days_ago(0))},
            closed_pulls=[
                {"number": 44, "head_ref": "claude/dropped", "head_sha": "bbb"}
            ],
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"]
        )
        self.assertIn("pull request closed against main", out)

    def test_a_stacked_closed_pull_request_is_still_offered_once_old(self):
        # Withholding the exemption is not withholding the branch: the age
        # gate reaches it like any other unmerged branch.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/upper", "bbb", False)],
            compares={"claude/upper": (2, days_ago(90))},
            closed_pulls=[
                {
                    "number": 42,
                    "head_ref": "claude/upper",
                    "head_sha": "bbb",
                    "base_ref": "claude/lower",
                }
            ],
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"]
        )
        self.assertIn("to ask about", out)

    def test_a_recent_branch_with_no_pull_request_is_still_held_back(self):
        # The other half of the same rule: with no pull request there is no
        # signal but the date, so recent work is not asked about.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/wip", "bbb", False)],
            compares={"claude/wip": (2, days_ago(0))},
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"]
        )
        self.assertIn("too recent to offer", out)
        self.assertNotIn("to offer one at a time", out)

    def test_a_closed_pull_request_on_a_reused_name_does_not_bypass_the_age_gate(
        self,
    ):
        # The label is matched by SHA, so a name reset onto new work does
        # not inherit the old occupant's closed pull request -- and must not
        # inherit its exemption from --older-than either, or the reuse the
        # guide encourages would drag live work into the prompt.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/reused", "newsha", False)],
            compares={"claude/reused": (2, days_ago(0))},
            closed_pulls=[
                {"number": 32, "head_ref": "claude/reused", "head_sha": "oldsha"}
            ],
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"]
        )
        self.assertIn("too recent to offer", out)
        self.assertNotIn("PR #32", out)

    def test_a_closed_pull_request_branch_is_actually_offered_and_deleted(self):
        # Not just classified into the right group -- the prompt reaches it.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/dropped", "bbb", False)],
            compares={"claude/dropped": (2, days_ago(0))},
            closed_pulls=[
                {"number": 33, "head_ref": "claude/dropped", "head_sha": "bbb"}
            ],
        )
        code, out, err, log = self.invoke(
            fake, [REPO], answers=["a"]
        )
        self.assertEqual(fake.deleted(), ["claude/dropped"])

    def test_a_truncated_compare_dates_the_branch_by_its_tip(self):
        # GitHub caps compare's `commits` at 250, so a branch further ahead
        # would be dated by its 250th commit -- a long-lived branch updated
        # yesterday could be offered as months old (Codex review,
        # mikelward/repo#20).
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/long", "tipsha", False)],
            compares={"claude/long": (400, days_ago(300))},
            truncated_compares=["claude/long"],
            commit_dates={"tipsha": days_ago(1)},
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"], stdin_isatty=False
        )
        self.assertIn("last commit 1d ago", out)
        self.assertIn("too recent to offer", out)

    def test_an_untruncated_compare_costs_no_extra_read(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/x", "bbb", False)],
            compares={"claude/x": (3, days_ago(30))},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--dry-run"])
        self.assertFalse(
            [c for c in fake.calls if len(c) > 1 and "/commits/" in str(c[1])]
        )

    def test_an_unreadable_tip_date_leaves_the_branch_unclassified(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/long", "tipsha", False)],
            compares={"claude/long": (400, days_ago(300))},
            truncated_compares=["claude/long"],
            commit_date_failures=["tipsha"],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("COULD NOT CLASSIFY", out)

    def test_an_unreadable_commit_date_is_kept_never_offered(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/odd", "bbb", False)],
            compares={"claude/odd": (1, "not-a-date")},
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--older-than", "0", "--dry-run"]
        )
        self.assertIn("age unknown", out)
        self.assertIn("too recent to offer", out)


class TwoStageTest(CleanupTestCase):
    def _mixed(self):
        return FakeGh(
            branches=[
                ("main", "aaa", True),
                ("claude/done", "bbb", False),
                ("claude/stale", "ccc", False),
            ],
            closed_pulls=[{"number": 7, "head_ref": "claude/done", "merged_at": "x"}],
            compares={"claude/stale": (2, days_ago(40))},
        )

    def _only_unmerged(self):
        """Nothing merged to sweep, one branch worth offering: the shape
        where the merged stage's own no-terminal refusal never runs."""
        return FakeGh(
            branches=[("main", "aaa", True), ("claude/stale", "ccc", False)],
            compares={"claude/stale": (2, days_ago(40))},
        )

    def test_one_run_asks_about_merged_then_unmerged(self):
        # The point of the two stages: a provably redundant branch and one
        # carrying unmerged commits are different questions, so they are
        # never one keystroke apart.
        fake = self._mixed()
        code, out, err, log = self.invoke(fake, [REPO], answers=["a", "a"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(fake.deleted()), ["claude/done", "claude/stale"])
        self.assertIn("Merged branches on owner/repo, safe to delete", err)
        self.assertIn("Unmerged on owner/repo", err)

    def test_unmerged_are_offered_without_any_flag(self):
        fake = self._mixed()
        code, out, err, log = self.invoke(fake, [REPO], answers=["n", "a"])
        self.assertEqual(fake.deleted(), ["claude/stale"])

    def test_declining_the_second_stage_keeps_the_first(self):
        fake = self._mixed()
        code, out, err, log = self.invoke(fake, [REPO], answers=["a", "n"])
        self.assertEqual(fake.deleted(), ["claude/done"])

    def test_a_deselected_merged_branch_is_recorded_as_declined(self):
        # The plan lists every merged branch as "to delete", so one that
        # goes unpicked needs its own line -- otherwise the record cannot
        # tell a deliberate deselection from a deletion that silently
        # produced no outcome.
        fake = self._mixed()
        code, out, err, log = self.invoke(fake, [REPO], answers=["s", "n", "n"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("declined claude/done", log)

    def test_force_sweeps_merged_and_never_offers_unmerged(self):
        # --force means "do not ask", and an unmerged branch is nothing but
        # asking -- so it is left alone rather than swept unattended.
        fake = self._mixed()
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), ["claude/done"])
        self.assertIn("NOT deleted", log)

    def test_a_non_terminal_offers_neither_stage(self):
        fake = self._mixed()
        code, out, err, log = self.invoke(fake, [REPO], stdin_isatty=False)
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("this is not an interactive terminal", err)

    def test_a_redirected_stderr_asks_nothing(self):
        # `repo cleanup owner/repo 2>cleanup.log` leaves stdin a terminal,
        # but every question here is written to stderr -- so gating on
        # stdin alone would print an invisible prompt into the file and
        # then block on input the user has no idea is wanted, which looks
        # exactly like a hang (Codex review, mikelward/repo#27). Neither
        # stage is offered, and the reason says so.
        fake = self._mixed()
        code, out, err, log = self.invoke(fake, [REPO], stderr_isatty=False)
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("this is not an interactive terminal", err)
        self.assertIn("the question is written to stderr", err)
        self.assertNotIn("Delete [A]ll", err)

    def test_a_redirected_stderr_leaves_the_unmerged_stage_unoffered(self):
        # The same redirect with nothing merged to delete: the first stage
        # has nothing to say, so only the unmerged offer could run -- and
        # it is the one that would hang, since it never reaches the
        # --force/no-terminal refusal above.
        fake = self._only_unmerged()
        code, out, err, log = self.invoke(fake, [REPO], stderr_isatty=False)
        self.assertEqual(fake.deleted(), [])
        self.assertNotIn("Delete [A]ll", err)
        self.assertIn("NOT deleted", err)

    def test_an_unrecognized_answer_selects_nothing(self):
        # Anything that is not a, s or n is not a guess to resolve toward
        # deleting: it selects nothing and says so.
        fake = self._mixed()
        code, out, err, log = self.invoke(fake, [REPO], answers=["wat", "n"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("is not one of a, s or n", err)


@contextlib.contextmanager
def _every_stream_a_terminal():
    """stdin, stdout and stderr all claiming to be terminals -- the one
    state in which the widget path runs at all.

    Yields the stderr capture, since the fallback path reports on it.
    A real buffer rather than a mock: `error()` writes to whatever
    `sys.stderr` is at call time, so a mock would swallow the message
    this needs to assert on.
    """
    err = _CaptureStream(isatty=True)
    with patch.object(cleanup_cmd.sys, "stdin") as stdin, patch.object(
        cleanup_cmd.sys, "stdout"
    ) as stdout, patch.object(cleanup_cmd.sys, "stderr", err):
        stdin.isatty.return_value = True
        stdout.isatty.return_value = True
        yield err


@contextlib.contextmanager
def _fake_checkboxlist(result, raises=None):
    """A stand-in `prompt_toolkit.shortcuts` so the dialog path itself is
    executed and asserted on.

    prompt_toolkit is not a dependency and must not become one (AGENTS.md:
    standard library only), but "not installed here" is why the constructor,
    its defaults, cancellation and the name-to-entry mapping had no coverage
    at all -- the path could break for everyone who does have it while the
    suite stayed green (Codex review, mikelward/repo#27). Injecting the
    module answers that without installing anything.

    Yields the dict of keyword arguments the dialog was built with.
    """
    recorded = {}

    class _Dialog:
        def run(self):
            if raises is not None:
                raise raises
            return result

    def checkboxlist_dialog(**kwargs):
        recorded.update(kwargs)
        return _Dialog()

    shortcuts = types.ModuleType("prompt_toolkit.shortcuts")
    shortcuts.checkboxlist_dialog = checkboxlist_dialog
    package = types.ModuleType("prompt_toolkit")
    package.shortcuts = shortcuts
    with patch.dict(
        sys.modules,
        {"prompt_toolkit": package, "prompt_toolkit.shortcuts": shortcuts},
    ):
        yield recorded


class WidgetPickerTest(CleanupTestCase):
    def test_a_redirected_stdout_means_no_dialog(self):
        # prompt_toolkit draws on stdout. `repo cleanup owner/repo >out`
        # leaves stdin and stderr terminals, so checking only those would
        # paint the dialog into the file and then wait on input to it.
        with patch.object(cleanup_cmd.sys, "stdin") as stdin, patch.object(
            cleanup_cmd.sys, "stdout"
        ) as stdout, patch.object(cleanup_cmd.sys, "stderr") as stderr:
            stdin.isatty.return_value = True
            stderr.isatty.return_value = True
            stdout.isatty.return_value = False
            self.assertIs(
                cleanup_cmd._select_widgets([], "t"), cleanup_cmd._NO_PICKER
            )

    def test_the_dialog_is_built_with_everything_ticked(self):
        entries = [
            {"name": "claude/a", "why": "merged by PR #1"},
            {"name": "claude/b", "why": "contained in main"},
        ]
        with _every_stream_a_terminal(), _fake_checkboxlist(["claude/a"]) as recorded:
            chosen = cleanup_cmd._select_widgets(entries, "Pick some")
        self.assertEqual(recorded["title"], "Pick some")
        # (value, label) pairs, and every value preselected: the common
        # case is "yes, all of these", so the user unticks exceptions
        # rather than ticking the rule.
        self.assertEqual(
            recorded["values"],
            [
                ("claude/a", "claude/a -- merged by PR #1"),
                ("claude/b", "claude/b -- contained in main"),
            ],
        )
        self.assertEqual(recorded["default_values"], ["claude/a", "claude/b"])
        # Names come back from the dialog; entries go out, so the caller
        # never has to map them itself.
        self.assertEqual(chosen, [entries[0]])

    def test_unticking_everything_is_not_canceling(self):
        # An empty list means "delete none of these" and is a real answer;
        # None means the dialog was dismissed. Collapsing the two would
        # make Escape silently delete nothing while reporting success, or
        # an empty selection abort the second stage.
        entries = [{"name": "claude/a", "why": "merged by PR #1"}]
        with _every_stream_a_terminal(), _fake_checkboxlist([]):
            self.assertEqual(cleanup_cmd._select_widgets(entries, "t"), [])

    def test_a_canceled_dialog_is_none(self):
        entries = [{"name": "claude/a", "why": "merged by PR #1"}]
        with _every_stream_a_terminal(), _fake_checkboxlist(None):
            self.assertIsNone(cleanup_cmd._select_widgets(entries, "t"))

    def test_a_dialog_that_cannot_draw_falls_back(self):
        # Broad on purpose: an unsupported terminal raises prompt_toolkit's
        # own types, and an older release may not accept default_values at
        # all. Either way the command must ask in plain text rather than
        # dying between the plan and the deletions.
        entries = [{"name": "claude/a", "why": "merged by PR #1"}]
        with _every_stream_a_terminal() as err, _fake_checkboxlist(
            None, raises=TypeError("no default_values")
        ):
            result = cleanup_cmd._select_widgets(entries, "t")
        self.assertIs(result, cleanup_cmd._NO_PICKER)
        self.assertIn("could not show the selection dialog", err.getvalue())
        self.assertIn("no default_values", err.getvalue())

    def test_an_absent_library_is_not_an_error(self):
        # `repo` runs from a checkout with no install step, and that has to
        # keep being true: not installed is the documented fallback, not a
        # failure.
        entries = [{"name": "claude/a", "why": "merged by PR #1"}]
        with _every_stream_a_terminal() as err, patch.dict(
            sys.modules, {"prompt_toolkit": None, "prompt_toolkit.shortcuts": None}
        ):
            result = cleanup_cmd._select_widgets(entries, "t")
        self.assertIs(result, cleanup_cmd._NO_PICKER)
        self.assertEqual(err.getvalue(), "")

    def test_no_terminal_means_no_dialog(self):
        # The widget path needs a real terminal at both ends; without one it
        # reports that rather than trying to draw, so the caller falls back
        # to the prompt the test suite drives.
        with patch.object(cleanup_cmd.sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertIs(
                cleanup_cmd._select_widgets([], "t"), cleanup_cmd._NO_PICKER
            )


class OfferTest(CleanupTestCase):
    def _two_stale(self):
        return FakeGh(
            branches=[
                ("main", "aaa", True),
                ("claude/a", "bbb", False),
                ("claude/b", "ccc", False),
            ],
            compares={
                "claude/a": (1, days_ago(30)),
                "claude/b": (2, days_ago(40)),
            },
        )

    def test_each_offered_branch_is_asked_about_individually(self):
        fake = self._two_stale()
        code, out, err, log = self.invoke(
            fake, [REPO], answers=["s", "y", "n"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), ["claude/a"])

    def test_declining_leaves_the_branch_alone(self):
        fake = self._two_stale()
        code, out, err, log = self.invoke(
            fake, [REPO], answers=["s", "n", "n"]
        )
        self.assertEqual(fake.deleted(), [])

    def test_exhausted_input_stops_asking_rather_than_agreeing(self):
        fake = self._two_stale()
        # One answer for two questions: end-of-input at the second must stop
        # the loop, not be read as a yes and sweep the rest.
        code, out, err, log = self.invoke(fake, [REPO], answers=["s", "y"])
        self.assertEqual(fake.deleted(), ["claude/a"])
        self.assertIn("no more input", err)

    def test_the_deleted_sha_is_logged_so_the_branch_can_be_restored(self):
        fake = self._two_stale()
        code, out, err, log = self.invoke(
            fake, [REPO], answers=["s", "y", "n"]
        )
        self.assertIn("deleted claude/a (was bbb)", log)
        # The full SHA and an API restore, not a `git push` needing a clone
        # that already holds the object (Codex review, mikelward/repo#20).
        self.assertIn(
            "gh api --method POST repos/owner/repo/git/refs "
            "-f ref=refs/heads/claude/a -f sha=bbb",
            log,
        )


class RevalidationTest(CleanupTestCase):
    """The plan is built before the confirmation prompt, so every branch is
    re-read immediately before its own delete (Codex review,
    mikelward/repo#20)."""

    def _one_merged(self, **kwargs):
        return FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[{"number": 7, "head_ref": "claude/done", "merged_at": "x"}],
            **kwargs,
        )

    def test_a_branch_pushed_to_during_the_prompt_is_refused(self):
        fake = self._one_merged(recheck={"claude/done": ("ccc", False)})
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("was NOT deleted", err)
        self.assertIn("it moved to ccc", err)

    def test_a_branch_protected_during_the_prompt_is_refused(self):
        fake = self._one_merged(recheck={"claude/done": ("bbb", True)})
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("it is protected now", err)

    def test_an_unreadable_recheck_refuses_rather_than_deleting(self):
        fake = self._one_merged(recheck_failures=["claude/done"])
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("could not be re-read", err)

    def test_a_branch_contained_in_the_default_branch_is_deleted(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("old", "bbb", False)],
            compares={"old": (0, days_ago(200))},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), ["old"])

    def test_a_merged_pull_request_entry_needs_no_compare(self):
        # A merge is durable, so re-comparing would be a wasted request on
        # every branch of a fleet-sized sweep.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[{"number": 7, "head_ref": "claude/done", "merged_at": "x"}],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), ["claude/done"])
        self.assertEqual(fake.compare_reads, {})

    def test_an_offered_branch_is_revalidated_too(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/a", "bbb", False)],
            compares={"claude/a": (1, days_ago(30))},
            recheck={"claude/a": ("ccc", False)},
        )
        code, out, err, log = self.invoke(
            fake, [REPO], answers=["a"]
        )
        self.assertEqual(fake.deleted(), [])
        self.assertIn("it moved to ccc", err)


class MergedIntoNonDefaultBaseTest(CleanupTestCase):
    """A merged pull request proves the commits reached ITS BASE. Only when
    that base is the default branch is it the claim cleanup needs. Stacking
    is documented in `AGENTS.md`, so an upper pull request merging into an
    ordinary branch is the workflow, not an edge case (Codex review,
    mikelward/repo#20)."""

    def _stacked(self, **kwargs):
        return FakeGh(
            branches=[("main", "aaa", True), ("claude/upper", "bbb", False)],
            closed_pulls=[
                {
                    "number": 5,
                    "head_ref": "claude/upper",
                    "base_ref": "claude/lower",
                    "merged_at": "x",
                    "head_sha": "bbb",
                }
            ],
            **kwargs,
        )

    def test_an_abandoned_stack_is_not_swept(self):
        # The lower branch was abandoned, so these commits never reached
        # main and this branch is the only ref left to them -- but its
        # merged pull request said "merged" all the same.
        fake = self._stacked(compares={"claude/upper": (3, "2026-08-01T00:00:00Z")})
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])

    def test_a_stack_whose_base_landed_is_still_swept(self):
        # No capability lost: the compare answers the question that matters
        # and this is deleted, just as `contained` rather than `merged-pr`.
        fake = self._stacked(compares={"claude/upper": (0, "2026-08-01T00:00:00Z")})
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), ["claude/upper"])
        # One compare: the classification's. The pre-delete re-read asks
        # only about the branch itself now.
        self.assertEqual(fake.compare_reads.get("claude/upper"), 1)

    def test_a_pull_request_merged_into_the_default_branch_costs_no_compare(self):
        # The common case is unchanged, which is what keeps the request
        # budget where the docs say it is.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[
                {
                    "number": 6,
                    "head_ref": "claude/done",
                    "base_ref": "main",
                    "merged_at": "x",
                    "head_sha": "bbb",
                }
            ],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), ["claude/done"])
        self.assertEqual(fake.compare_reads, {})


class ReusedBranchNameTest(CleanupTestCase):
    """A merged pull request only proves the branch landed AS IT STOOD at
    that SHA. This fleet resets and reuses branch names, so matching by name
    alone would delete new work (Codex review, mikelward/repo#20)."""

    def test_a_branch_pushed_to_after_its_merge_is_not_swept(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/reused", "newsha", False)],
            closed_pulls=[
                {
                    "number": 7,
                    "head_ref": "claude/reused",
                    "head_sha": "oldsha",
                    "merged_at": "x",
                }
            ],
            compares={"claude/reused": (4, days_ago(2))},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("4 commits not on the default branch", log)

    def test_a_reused_name_does_not_inherit_the_old_closed_pr_label(self):
        # The offer prompt's abandonment signal must describe the commits
        # being offered, not an older occupant of the same branch name --
        # it is what a person says yes or no on (Codex review,
        # mikelward/repo#20).
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/reused", "newsha", False)],
            closed_pulls=[
                {
                    "number": 12,
                    "head_ref": "claude/reused",
                    "head_sha": "oldsha",
                    "merged_at": None,
                }
            ],
            compares={"claude/reused": (2, days_ago(40))},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--dry-run"])
        self.assertIn("claude/reused", out)
        self.assertNotIn("PR #12 closed without merging", out)

    def test_a_branch_still_at_its_closed_prs_sha_keeps_the_label(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/dropped", "bbb", False)],
            closed_pulls=[
                {
                    "number": 12,
                    "head_ref": "claude/dropped",
                    "head_sha": "bbb",
                    "merged_at": None,
                }
            ],
            compares={"claude/dropped": (2, days_ago(40))},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--dry-run"])
        self.assertIn("PR #12 closed without merging", out)

    def test_a_branch_still_at_its_merged_sha_is_swept(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[
                {
                    "number": 7,
                    "head_ref": "claude/done",
                    "head_sha": "bbb",
                    "merged_at": "x",
                }
            ],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), ["claude/done"])


class LogFileTest(CleanupTestCase):
    """The plan runs to hundreds of lines on a repository with a weekly
    batch and every deletion adds two more, so the record goes to a file
    and the terminal gets a summary and its path."""

    def _merged(self):
        return FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[{"number": 7, "head_ref": "claude/done", "merged_at": "x"}],
        )

    def test_the_plan_prints_and_the_deletion_stream_does_not(self):
        # The plan is what the confirmation is about, so it prints. The two
        # lines a deletion writes are what the log is for -- and the
        # restore command in them is the half that outlives the terminal.
        code, out, err, log = self.invoke(self._merged(), [REPO, "--force"])
        self.assertEqual(code, 0)
        self.assertIn("Merged branches on owner/repo, to delete (1):", err)
        self.assertIn("Merged branches on owner/repo, to delete (1):", log)
        self.assertIn("gh api --method POST", log)
        self.assertNotIn("gh api --method POST", err)
        self.assertNotIn("deleted claude/done (was bbb)", err)
        self.assertEqual(out, "")

    def test_the_terminal_names_the_log_and_the_count_deleted(self):
        # What is left on the terminal has to be enough to act on: that it
        # happened, and where to read the rest of it.
        fake = self._merged()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.log")
            code, out, err, log = self.invoke(fake, [REPO, "--force", "--log", path])
        self.assertIn(f"full record: {path}", err)
        self.assertIn(f"deleted 1; the command restoring each is in {path}", err)

    def test_the_progress_counter_is_a_terminal_only_overwrite(self):
        # Redirected, `\r` is just a character in the file, so a scripted
        # run would collect one line per branch of a counter nobody
        # watched.
        fake = FakeGh(
            branches=[
                ("main", "aaa", True),
                ("claude/a", "bbb", False),
                ("claude/b", "ccc", False),
            ],
            closed_pulls=[
                {"number": 1, "head_ref": "claude/a", "merged_at": "x"},
                {"number": 2, "head_ref": "claude/b", "merged_at": "x"},
            ],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"], stderr_isatty=False)
        self.assertEqual(sorted(fake.deleted()), ["claude/a", "claude/b"])
        self.assertNotIn("\r", err)
        self.assertNotIn("deleting 1/2", err)

        class Tty(io.StringIO):
            def isatty(self):
                return True

        tty = Tty()
        fake = FakeGh(
            branches=[
                ("main", "aaa", True),
                ("claude/a", "bbb", False),
                ("claude/b", "ccc", False),
            ],
            closed_pulls=[
                {"number": 1, "head_ref": "claude/a", "merged_at": "x"},
                {"number": 2, "head_ref": "claude/b", "merged_at": "x"},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.log")
            self._invoke(
                fake,
                [REPO, "--force", "--log", path],
                True,
                lambda: "",
                io.StringIO(),
                tty,
                lambda: NOW,
            )
        shown = tty.getvalue()
        self.assertIn("\rdeleting 1/2...", shown)
        self.assertIn("\rdeleting 2/2...", shown)
        # Cleared, so the summary that follows is not printed onto the
        # tail of the counter.
        self.assertIn("\r\033[K", shown)

    def test_the_default_path_is_under_the_state_directory(self):
        fake = self._merged()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                # No --log, so the command picks its own path.
                code = self._invoke(
                    fake, [REPO, "--force"], True, lambda: "", io.StringIO(), io.StringIO()
                    , lambda: NOW
                )
            written = os.listdir(os.path.join(tmp, "repo"))
        self.assertEqual(code, 0)
        self.assertEqual(written, ["cleanup-owner-repo-20260903T000000Z.log"])

    def test_a_dry_run_writes_no_log_and_still_prints_its_plan(self):
        # --dry-run changes nothing, and a file appearing where none was
        # asked for is a change. Its plan is the whole of its output, so it
        # stays on stdout where it can be piped.
        fake = self._merged()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.log")
            code, out, err, log = self.invoke(
                fake, [REPO, "--dry-run", "--log", path]
            )
            self.assertFalse(os.path.exists(path))
        self.assertIn("Merged branches on owner/repo, to delete (1):", out)
        self.assertEqual(fake.deleted(), [])

    def test_a_log_that_cannot_be_opened_stops_the_run(self):
        # The restore command for a deleted branch exists nowhere else, so
        # deleting with nowhere to write it would be deleting with no way
        # back. Refuse instead of sweeping unrecorded.
        fake = self._merged()
        with tempfile.TemporaryDirectory() as tmp:
            # A directory where the log file should be: open() fails, and
            # the failure is the filesystem's rather than a stub's.
            path = os.path.join(tmp, "run.log")
            os.mkdir(path)
            code, out, err, log = self.invoke(fake, [REPO, "--force", "--log", path])
        # Asserted before the exit code: the harm this guards against is a
        # branch swept with its restore command written nowhere.
        self.assertEqual(fake.deleted(), [])
        self.assertEqual(code, 1)
        self.assertIn("could not open the log", err)
        self.assertIn("Nothing was deleted", err)

    def test_the_restore_record_is_on_disk_before_the_ref_is_deleted(self):
        # The plan carries no SHAs, so this log is the only record of one.
        # A process killed between the DELETE and the write -- or a disk
        # that fills there -- would take the branch with nothing left to
        # recreate it (Codex review, mikelward/repo#23).
        fake = self._merged()
        seen = []
        inner = fake.try_run

        def try_run(argv):
            if "DELETE" in argv:
                with open(path, encoding="utf-8") as f:
                    seen.append(f.read())
            return inner(argv)

        fake.try_run = try_run
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.log")
            self._invoke(
                fake,
                [REPO, "--force", "--log", path],
                True,
                lambda: "",
                io.StringIO(),
                io.StringIO(),
                lambda: NOW,
            )
        self.assertEqual(len(seen), 1)
        # Flushed, not merely written into a buffer the kill would lose.
        self.assertIn("deleting claude/done (was bbb)", seen[0])
        self.assertIn("-f sha=bbb", seen[0])

    def test_the_restore_record_is_fsynced_before_the_ref_is_deleted(self):
        # A flush only reaches the kernel: a power loss, or a writeback
        # error reported late, would discard the record after the DELETE
        # had already succeeded (Codex review, mikelward/repo#23).
        fake = self._merged()
        order = []
        inner = fake.try_run

        def try_run(argv):
            if "DELETE" in argv:
                order.append("delete")
            return inner(argv)

        fake.try_run = try_run
        real_sync = cleanup_cmd._sync

        def sync(handle):
            # The log handle's own sync, not the directory entry's -- the
            # directory is fsynced at open, so watching os.fsync itself
            # passes whether or not the record is ever synced.
            order.append("sync")
            return real_sync(handle)

        with patch.object(cleanup_cmd, "_sync", sync):
            code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertIn("sync", order)
        self.assertLess(order.index("sync"), order.index("delete"))

    def test_a_plan_that_cannot_be_written_stops_before_any_delete(self):
        # Nothing has been deleted yet, so this is the cheap place to
        # stop: a run whose plan never reached the file is a run whose
        # record starts blank.
        fake = self._merged()

        class Full(io.StringIO):
            def flush(self):
                raise OSError("No space left on device")

        with patch.object(cleanup_cmd, "_open_log", lambda path: Full()):
            code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertEqual(code, 1)
        self.assertIn("could not write the plan", err)

    def test_every_directory_created_for_the_log_has_its_entry_synced(self):
        # A directory's own entry lives in its PARENT, so syncing only the
        # leaf leaves the hierarchy above it -- created on a machine's
        # first run -- able to vanish in a crash, taking the log and the
        # only restore records with it (Codex review, mikelward/repo#23).
        fake = self._merged()
        synced = []
        real = cleanup_cmd._sync_directory

        def record(path):
            synced.append(os.path.realpath(path))
            return real(path)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state", "repo", "run.log")
            with patch.object(cleanup_cmd, "_sync_directory", record):
                self._invoke(
                    fake,
                    [REPO, "--force", "--log", path],
                    True,
                    lambda: "",
                    io.StringIO(),
                    io.StringIO(),
                    lambda: NOW,
                )
            root = os.path.realpath(tmp)
            # The parent of each directory created, plus the leaf holding
            # the log file itself.
            self.assertIn(root, synced)
            self.assertIn(os.path.join(root, "state"), synced)
            self.assertIn(os.path.join(root, "state", "repo"), synced)

    def test_a_directory_sync_failure_stops_before_any_delete(self):
        # A directory entry that may not survive a crash is no better than
        # no log, and the file it names holds the only restore commands
        # (Codex review, mikelward/repo#23).
        fake = self._merged()
        real_fsync = os.fsync
        calls = []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 1:
                # The first fsync of a run is the directory's, at open.
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_fsync(fd)

        with patch.object(cleanup_cmd.os, "fsync", fsync):
            code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertEqual(code, 1)
        self.assertIn("for durable writing", err)

    def test_a_failure_opening_the_log_directory_stops_before_any_delete(self):
        # A blanket catch here read EIO, EMFILE and a concurrent ENOENT as
        # "this platform has no directory handle" and let the sweep start
        # anyway (Codex review, mikelward/repo#23).
        fake = self._merged()
        real_open = os.open

        def opener(path, flags, *args, **kwargs):
            if flags == os.O_RDONLY:
                raise OSError(errno.EIO, "Input/output error")
            return real_open(path, flags, *args, **kwargs)

        with patch.object(cleanup_cmd.os, "open", opener):
            code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertEqual(code, 1)
        self.assertIn("for durable writing", err)

    def test_a_platform_with_no_directory_handle_still_runs(self):
        # Decided by name rather than by catching whatever os.open raises:
        # the errno Windows gives is EACCES, which means something real on
        # POSIX.
        fake = self._merged()

        real_open = os.open
        opened = []

        def opener(path, flags, *args, **kwargs):
            # Patching os.open reaches the real module, which tempfile and
            # the harness also use -- so record rather than refuse.
            if flags == os.O_RDONLY and os.path.isdir(path):
                opened.append(path)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(cleanup_cmd.os, "name", "nt"), patch.object(
            cleanup_cmd.os, "open", opener
        ):
            code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(opened, [])
        self.assertEqual(fake.deleted(), ["claude/done"])
        self.assertEqual(code, 0)

    def test_a_filesystem_that_cannot_sync_directories_still_runs(self):
        # EINVAL says the operation does not exist here, not that a write
        # was lost. Refusing to run would cost the whole command on such a
        # filesystem for no durability gained.
        fake = self._merged()
        real_fsync = os.fsync
        calls = []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 1:
                raise OSError(errno.EINVAL, "Invalid argument")
            return real_fsync(fd)

        with patch.object(cleanup_cmd.os, "fsync", fsync):
            code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), ["claude/done"])
        self.assertEqual(code, 0)

    def test_a_restore_record_that_cannot_be_written_leaves_the_branch_alone(self):
        # A full disk lands on the safe side of the delete: the write
        # fails, and the branch stays.
        fake = self._merged()

        class FullOnRestore(io.StringIO):
            def write(self, text):
                if text.startswith("  restore:"):
                    raise OSError("No space left on device")
                return super().write(text)

        with patch.object(cleanup_cmd, "_open_log", lambda path: FullOnRestore()):
            code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [])
        self.assertEqual(code, 1)
        self.assertIn("could not write the restore record", err)

    def test_a_best_effort_write_failure_still_makes_the_run_nonzero(self):
        # The run advertises a full record; reporting a write it could not
        # make and then exiting 0 would say otherwise (Codex review,
        # mikelward/repo#23).
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/a", "bbb", False)],
            compares={"claude/a": (1, days_ago(30))},
        )

        class FullOnDecline(io.StringIO):
            def write(self, text):
                if text.startswith("declined "):
                    raise OSError("No space left on device")
                return super().write(text)

        with patch.object(cleanup_cmd, "_open_log", lambda path: FullOnDecline()):
            code, out, err, log = self.invoke(
                fake, [REPO, "--include-unmerged"], answers=["n"]
            )
        # Nothing failed except the record itself, and that is enough.
        self.assertEqual(fake.deleted(), [])
        self.assertIn("could not be written", err)
        self.assertEqual(code, 1)

    def test_declining_an_offer_survives_an_unwritable_log(self):
        # The one write left outside the guarded path. It can run after
        # confirmed merged branches have already been deleted, so escaping
        # would skip the remaining offers and the summary (Codex review,
        # mikelward/repo#23).
        fake = FakeGh(
            branches=[
                ("main", "aaa", True),
                ("claude/a", "bbb", False),
                ("claude/b", "ccc", False),
            ],
            compares={
                "claude/a": (1, days_ago(30)),
                "claude/b": (2, days_ago(40)),
            },
        )

        class FullOnDecline(io.StringIO):
            def write(self, text):
                if text.startswith("declined "):
                    raise OSError("No space left on device")
                return super().write(text)

        with patch.object(cleanup_cmd, "_open_log", lambda path: FullOnDecline()):
            code, out, err, log = self.invoke(
                fake, [REPO], answers=["s", "n", "y"]
            )
        # Declining the first did not abandon the second.
        self.assertEqual(fake.deleted(), ["claude/b"])
        self.assertIn("could not be written", err)

    def test_a_failure_closing_the_log_is_reported_not_raised(self):
        # Closing flushes, so it is the last place a full disk can bite --
        # and by then branches are deleted. A context manager's implicit
        # close would escape `run` as a traceback (Codex review,
        # mikelward/repo#23).
        fake = self._merged()

        class FullOnClose(io.StringIO):
            def close(self):
                raise OSError("No space left on device")

        with patch.object(cleanup_cmd, "_open_log", lambda path: FullOnClose()):
            code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), ["claude/done"])
        self.assertEqual(code, 1)
        self.assertIn("could not be completed", err)

    def test_an_existing_log_is_appended_to_not_truncated(self):
        # A --log pointed at a previous run's file, or two default-path
        # runs starting inside the same second, would otherwise destroy
        # the only copy of that run's restore commands -- before this run
        # had even asked for confirmation (Codex review,
        # mikelward/repo#23).
        fake = self._merged()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write("restore: gh api ... -f sha=earlier\n")
            code, out, err, log = self.invoke(fake, [REPO, "--force", "--log", path])
        self.assertIn("-f sha=earlier", log)
        self.assertIn("-f sha=bbb", log)

    def test_a_log_failure_after_the_delete_is_reported_not_raised(self):
        # The restore command is already flushed, so this loses the
        # outcome and not the way back -- but escaping would abandon every
        # remaining branch and the closing summary AFTER a branch had been
        # deleted (Codex review, mikelward/repo#23).
        fake = FakeGh(
            branches=[
                ("main", "aaa", True),
                ("claude/a", "bbb", False),
                ("claude/b", "ccc", False),
            ],
            closed_pulls=[
                {"number": 1, "head_ref": "claude/a", "merged_at": "x"},
                {"number": 2, "head_ref": "claude/b", "merged_at": "x"},
            ],
        )

        class FullAfterFlush(io.StringIO):
            """Writes until the first branch's restore record is flushed,
            then fails -- the disk filling mid-sweep."""

            def __init__(self):
                super().__init__()
                self.flushed = False

            def flush(self):
                self.flushed = True

            def write(self, text):
                if self.flushed and text.startswith("deleted "):
                    raise OSError("No space left on device")
                return super().write(text)

        with patch.object(cleanup_cmd, "_open_log", lambda path: FullAfterFlush()):
            code, out, err, log = self.invoke(fake, [REPO, "--force"])
        # The first branch really was deleted, and the run said so rather
        # than dying: the summary is still printed and the exit is nonzero.
        self.assertIn("claude/a", fake.deleted())
        self.assertIn("the outcome could not be logged", err)
        self.assertEqual(code, 1)
        # And it is reported as what it is. Calling it a failure to delete
        # would tell the reader the opposite of what happened, and drop it
        # from the count (Codex review, mikelward/repo#23).
        self.assertIn("claude/a was deleted, but not fully recorded", err)
        self.assertNotIn("could not delete claude/a", err)
        self.assertIn("deleted 2;", err)

    def test_a_refusal_reaches_both_the_terminal_and_the_log(self):
        # Read once on a scrolling terminal is not a record, and the log is
        # what the user still has tomorrow.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[{"number": 7, "head_ref": "claude/done", "merged_at": "x"}],
            recheck={"claude/done": ("ccc", False)},
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        for text in (err, log):
            self.assertIn("claude/done was NOT deleted", text)


class RestoreHintTest(CleanupTestCase):
    def test_a_merged_sweep_logs_the_restore_command_too(self):
        # --force deletes the most branches and was the path omitting the
        # recovery line it advertises (Codex review, mikelward/repo#20).
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[{"number": 7, "head_ref": "claude/done", "merged_at": "x"}],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertIn(
            "gh api --method POST repos/owner/repo/git/refs "
            "-f ref=refs/heads/claude/done -f sha=bbb",
            log,
        )

    def test_a_branch_name_with_shell_metacharacters_is_quoted(self):
        # `topic$(id)` is a legal git branch name and passes _safe_ref_name,
        # so an unquoted one in a command the user pastes is arbitrary code
        # chosen by whoever pushed the branch (Codex review,
        # mikelward/repo#20).
        name = "topic$(id)"
        fake = FakeGh(
            branches=[("main", "aaa", True), (name, "bbb", False)],
            closed_pulls=[{"number": 7, "head_ref": name, "merged_at": "x"}],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(fake.deleted(), [name])
        self.assertIn("'refs/heads/topic$(id)'", log)
        self.assertNotIn("-f ref=refs/heads/topic$(id)", out)


class UsageTest(CleanupTestCase):
    def test_include_unmerged_is_accepted_but_no_longer_needed(self):
        # The flag is kept so an existing invocation does not break, but an
        # interactive run offers unmerged branches with or without it.
        fake = FakeGh(branches=[("main", "aaa", True)])
        code, out, err, log = self.invoke(fake, [REPO, "--include-unmerged", "--force"])
        self.assertEqual(code, 0)
        self.assertIn("no longer needed", err)
    def test_a_dry_run_may_use_include_unmerged_without_a_terminal(self):
        # Nothing is asked and nothing is written, so the terminal
        # requirement would only block a legitimate `| less`.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/x", "bbb", False)],
            compares={"claude/x": (1, days_ago(30))},
        )
        code, out, err, log = self.invoke(
            fake, [REPO, "--include-unmerged", "--dry-run"], stdin_isatty=False
        )
        self.assertEqual(code, 0)

    def test_a_bad_repository_name_is_rejected_before_any_call(self):
        fake = FakeGh()
        code, out, err, log = self.invoke(fake, ["not-a-repo-name"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])

    def test_a_negative_older_than_is_rejected(self):
        fake = FakeGh()
        code, out, err, log = self.invoke(fake, [REPO, "--older-than", "-1"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])


class ConfirmationTest(CleanupTestCase):
    def _one_merged(self):
        return FakeGh(
            branches=[("main", "aaa", True), ("claude/done", "bbb", False)],
            closed_pulls=[{"number": 7, "head_ref": "claude/done", "merged_at": "x"}],
        )

    def test_choosing_none_deletes_nothing(self):
        fake = self._one_merged()
        code, out, err, log = self.invoke(fake, [REPO], answers=["n"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), [])

    def test_choosing_all_deletes(self):
        fake = self._one_merged()
        code, out, err, log = self.invoke(fake, [REPO], answers=["a"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), ["claude/done"])

    def test_empty_answer_means_all_since_everything_starts_selected(self):
        fake = self._one_merged()
        code, out, err, log = self.invoke(fake, [REPO], answers=[""])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), ["claude/done"])

    def test_selecting_one_by_one_asks_per_branch(self):
        fake = self._one_merged()
        code, out, err, log = self.invoke(fake, [REPO], answers=["s", "y"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), ["claude/done"])
        self.assertIn("Delete claude/done?", err)

    def test_a_non_terminal_without_force_deletes_nothing(self):
        fake = self._one_merged()
        code, out, err, log = self.invoke(fake, [REPO], stdin_isatty=False)
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("this is not an interactive terminal", err)

    def test_force_still_records_the_whole_plan(self):
        # --force skips the question, not the record of what it touched.
        fake = self._one_merged()
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertIn("Merged branches on owner/repo, to delete (1):", log)

    def test_a_dry_run_writes_nothing(self):
        fake = self._one_merged()
        code, out, err, log = self.invoke(fake, [REPO, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("claude/done: merged by PR #7", out)


class DecliningStillReportsTest(CleanupTestCase):
    """Declining the prompt is a fourth exit that used to skip the relay of
    WHY a branch could not be classified. The relay now runs once, before
    the gate, so no exit below it can swallow it (Codex review,
    mikelward/repo#20)."""

    def _mixed(self):
        return FakeGh(
            branches=[
                ("main", "aaa", True),
                ("claude/done", "bbb", False),
                ("claude/broken", "ccc", False),
            ],
            closed_pulls=[
                {
                    "number": 1,
                    "head_ref": "claude/done",
                    "merged_at": "x",
                    "head_sha": "bbb",
                }
            ],
            compare_failures=("claude/broken",),
        )

    def test_declining_the_prompt_still_says_why_a_branch_failed(self):
        fake = self._mixed()
        code, out, err, log = self.invoke(fake, [REPO], answers=["n"])
        self.assertEqual(fake.deleted(), [])
        self.assertIn("could not classify claude/broken", err)
        self.assertIn("HTTP 500", err)

    def test_a_non_terminal_run_without_force_still_says_why(self):
        fake = self._mixed()
        code, out, err, log = self.invoke(fake, [REPO], stdin_isatty=False)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("could not classify claude/broken", err)
        self.assertIn("HTTP 500", err)


class FailureTest(CleanupTestCase):
    def test_a_failed_delete_is_reported_and_exits_nonzero(self):
        fake = FakeGh(
            branches=[
                ("main", "aaa", True),
                ("claude/a", "bbb", False),
                ("claude/b", "ccc", False),
            ],
            closed_pulls=[
                {"number": 1, "head_ref": "claude/a", "merged_at": "x"},
                {"number": 2, "head_ref": "claude/b", "merged_at": "x"},
            ],
            delete_failures=["claude/a"],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 1)
        # The other branch is still attempted -- one failure does not abort.
        self.assertIn("claude/b", fake.deleted())
        self.assertIn("could not delete claude/a", err)

    def test_a_dry_run_reports_why_a_branch_could_not_be_classified(self):
        # The plan names the branch; the reason is relayed too, or a failure
        # hides whether it was auth, a rate limit, or something else (Codex
        # review, mikelward/repo#20).
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/x", "bbb", False)],
            compare_failures=["claude/x"],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("COULD NOT CLASSIFY", out)
        self.assertIn("could not classify claude/x", err)
        self.assertIn("HTTP 500", err)

    def test_an_unreadable_compare_leaves_that_branch_alone_and_exits_nonzero(self):
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/x", "bbb", False)],
            compare_failures=["claude/x"],
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("COULD NOT CLASSIFY", log)
        self.assertIn("could not classify claude/x", err)

    def test_a_failed_repository_read_stops_before_any_deletion(self):
        fake = FakeGh(repo_read_fails=True)
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("could not read owner/repo", err)

    def test_a_failed_branch_list_stops_before_any_deletion(self):
        fake = FakeGh(branches_read_fails=True)
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("could not list owner/repo's branches", err)

    def test_a_failed_pull_request_list_stops_before_any_deletion(self):
        # Without the pull requests, every rebase-merged branch would look
        # unmerged and every open PR's head would look unprotected -- the
        # plan would be wrong in both directions, so nothing may proceed.
        fake = FakeGh(
            branches=[("main", "aaa", True), ("claude/a", "bbb", False)],
            pulls_read_fails=True,
        )
        code, out, err, log = self.invoke(fake, [REPO, "--force"])
        self.assertEqual(code, 1)
        self.assertEqual(fake.deleted(), [])
        self.assertIn("pull requests", err)


class NothingToDoTest(CleanupTestCase):
    def test_a_repository_with_nothing_to_delete_says_so_and_exits_zero(self):
        fake = FakeGh(branches=[("main", "aaa", True)])
        code, out, err, log = self.invoke(fake, [REPO])
        self.assertEqual(code, 0)
        self.assertIn("No merged branches to delete", log)
        self.assertEqual(fake.deleted(), [])


if __name__ == "__main__":
    unittest.main()
