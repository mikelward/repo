"""The fleet's Actions event policy: the workflows that run on
`pull_request_target` keep running (SPEC.md, *The standard*).

From December 2, 2026, GitHub blocks `pull_request_target` in every public
repository that has no Actions event policy allowing it. The fleet relies
on that trigger on purpose: it loads a workflow from the default branch
rather than from the pull request, so a pull request cannot rewrite the job
that judges it. Three scaffold workflows use it -- `codex-review.yml` (the
required `codex` status), `codex-review-check.yml` (the consumer check) and,
in the repositories that publish `lanes` from it, `ci.yml` -- so this keeps
one repository policy, POLICY_NAME, aimed at exactly those files.

The policy lists every event GitHub accepts, not just `pull_request_target`.
An event rule is an allowlist for the workflows it targets: listing only the
one trigger would block each file's other ones (`schedule`, `workflow_run`,
`workflow_dispatch`, `push`). Every other workflow stays under GitHub's
default, so a new workflow reaching for `pull_request_target` is still
blocked until someone adds it here.

`ci.yml` is targeted in every repository, not only where it uses the
trigger: allowing an event a workflow never declares changes nothing, and
one list means a repository that moves its `ci.yml` to the trigger is
already covered.

Private repositories are left alone: GitHub's block applies to public ones
only.

The standard is a floor, as the tag ruleset's is: a policy already under the
name gains the files and events it lacks and keeps everything else. One that
is turned off, or that excludes a file this needs, is a person's decision,
held and reported.

`plan` only reads, so `repo audit` asks it too: one definition of "allowed"
rather than two that drift.
"""

import json
from dataclasses import dataclass, field
from typing import List, Optional

from repo_lib import gh

POLICY_NAME = "pull_request_target"
WORKFLOW_PATHS = (
    ".github/workflows/codex-review.yml",
    ".github/workflows/codex-review-check.yml",
    ".github/workflows/ci.yml",
)
# Every event GitHub's policy API accepts (REST API endpoints for GitHub
# Actions policies, `restrict_action_events`).
ALLOWED_EVENTS = (
    "branch_protection_rule",
    "check_run",
    "check_suite",
    "create",
    "delete",
    "deployment",
    "deployment_status",
    "discussion",
    "discussion_comment",
    "fork",
    "gollum",
    "image_version",
    "issue_comment",
    "issues",
    "label",
    "merge_group",
    "milestone",
    "page_build",
    "project",
    "project_card",
    "project_column",
    "public",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_target",
    "push",
    "registry_package",
    "release",
    "repository_dispatch",
    "schedule",
    "status",
    "watch",
    "workflow_call",
    "workflow_dispatch",
    "workflow_run",
)
EVENTS_RULE = "restrict_action_events"
# The policy endpoints are documented under this API version.
API_VERSION_HEADER = ["-H", "X-GitHub-Api-Version: 2026-03-10"]
# The fields of a policy that say what it does, compared to tell whether one
# changed between the plan and the write.
_OBSERVED_FIELDS = ("name", "enforcement", "conditions", "rules")

ALLOWED_LINE = (
    "codex-review.yml, codex-review-check.yml and ci.yml may run on pull_request_target "
    f"(Actions policy '{POLICY_NAME}')"
)


@dataclass
class PolicyPlan:
    """What a run will do: "allowed" (nothing), "private" (nothing: GitHub
    blocks nothing there), "create", "update", or "error" (lines say why,
    and nothing is written)."""

    state: str
    lines: List[str] = field(default_factory=list)
    policy_id: Optional[str] = None
    body: Optional[dict] = None
    # What the plan read: the ids under the name and the written-to one's
    # own fields. A write goes ahead only while a fresh read still sees the
    # same.
    observed: Optional[tuple] = None


def _create_body():
    return {
        "name": POLICY_NAME,
        "enforcement": "active",
        "conditions": {"workflow_path": {"include": list(WORKFLOW_PATHS), "exclude": []}},
        "rules": [{"type": EVENTS_RULE, "parameters": {"allowed_events": list(ALLOWED_EVENTS)}}],
    }


def _error_lines(what, e):
    return [what] + [f"  {line}" for line in (e.stderr or "").splitlines()]


def _api(*args):
    return ["api", *API_VERSION_HEADER, *args]


def _is_private(repo):
    """True, False, or a PolicyPlan error when the read fails."""
    try:
        out = gh.run(["api", f"repos/{repo}", "--jq", ".private"]).strip()
    except gh.GhError as e:
        return PolicyPlan("error", _error_lines(f"could not tell whether {repo} is public:", e))
    if out not in ("true", "false"):
        return PolicyPlan("error", [f"could not tell whether {repo} is public: GitHub answered '{out}'"])
    return out == "true"


def plan(repo):
    """The PolicyPlan for `repo`. Reads only.

    A failed read is an error, never "there is no policy": creating one
    beside a policy this run could not see would leave two under the name."""
    private = _is_private(repo)
    if isinstance(private, PolicyPlan):
        return private
    if private:
        return PolicyPlan(
            "private", ["not needed: GitHub blocks pull_request_target only in public repositories"]
        )
    try:
        out = gh.run(
            _api(
                "--paginate",
                f"repos/{repo}/actions/policies",
                "--jq",
                f".policies[] | select(.name == {json.dumps(POLICY_NAME)}) | .id",
            )
        )
    except gh.GhError as e:
        return PolicyPlan(
            "error",
            _error_lines(
                f"could not list {repo}'s Actions policies, so cannot tell whether "
                f"'{POLICY_NAME}' already exists:",
                e,
            ),
        )
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    if not ids:
        return PolicyPlan(
            "create",
            [
                f"create Actions policy '{POLICY_NAME}': codex-review.yml, codex-review-check.yml "
                "and ci.yml may run on pull_request_target"
            ],
            body=_create_body(),
            observed=((), None),
        )

    pid = ids[0]
    others = (
        [
            f"{len(ids) - 1} more polic{'ies' if len(ids) > 2 else 'y'} named '{POLICY_NAME}' "
            f"(id {', '.join(ids[1:])}) left as {'they are' if len(ids) > 2 else 'it is'}; "
            f"this run reads and writes id {pid}"
        ]
        if len(ids) > 1
        else []
    )
    where = f"Actions policy '{POLICY_NAME}' (id {pid}) on {repo}"
    try:
        existing = json.loads(gh.run(_api(f"repos/{repo}/actions/policies/{pid}")))
    except gh.GhError as e:
        return PolicyPlan("error", _error_lines(f"could not read {where}:", e), policy_id=pid)
    except ValueError:
        return PolicyPlan("error", [f"could not read {where}: not JSON"], policy_id=pid)

    if existing.get("enforcement") != "active":
        return PolicyPlan(
            "error",
            [
                f"{where} is '{existing.get('enforcement')}', not 'active', so it allows nothing; "
                "activate it by hand, since whoever turned it off may have had a reason"
            ],
            policy_id=pid,
        )

    conditions = dict(existing.get("conditions") or {})
    workflow_path = conditions.get("workflow_path")
    # No workflow_path at all targets every workflow, as do `~ALL` and an
    # empty include (everything the exclude does not name).
    include = list((workflow_path or {}).get("include") or [])
    exclude = list((workflow_path or {}).get("exclude") or [])
    targets_all = workflow_path is None or include == ["~ALL"] or not include
    excluded = [path for path in WORKFLOW_PATHS if path in exclude]
    if excluded:
        return PolicyPlan(
            "error",
            [
                f"{where} excludes {', '.join(excluded)}, so GitHub blocks "
                f"{'it' if len(excluded) == 1 else 'them'} on pull_request_target; remove the "
                "exclude by hand, since it is a person's decision"
            ],
            policy_id=pid,
        )
    missing_paths = [] if targets_all else [path for path in WORKFLOW_PATHS if path not in include]

    existing_rules = list(existing.get("rules") or [])
    events_rule = next((rule for rule in existing_rules if rule.get("type") == EVENTS_RULE), None)
    have_events = list(((events_rule or {}).get("parameters") or {}).get("allowed_events") or [])
    missing_events = [event for event in ALLOWED_EVENTS if event not in have_events]

    if not missing_paths and not missing_events:
        return PolicyPlan("allowed", [f"already: {ALLOWED_LINE}"] + others, policy_id=pid)

    adds = []
    if missing_paths:
        adds.append("target " + ", ".join(path.rsplit("/", 1)[-1] for path in missing_paths))
    if missing_events:
        adds.append("allow " + ", ".join(missing_events))
    if missing_paths:
        conditions["workflow_path"] = {"include": include + missing_paths, "exclude": exclude}
    rules = [
        (
            {**rule, "parameters": {**(rule.get("parameters") or {}), "allowed_events": have_events + missing_events}}
            if rule is events_rule
            else rule
        )
        for rule in existing_rules
    ]
    if events_rule is None:
        rules.append({"type": EVENTS_RULE, "parameters": {"allowed_events": list(ALLOWED_EVENTS)}})
    body = {"name": POLICY_NAME, "enforcement": "active", "conditions": conditions, "rules": rules}
    return PolicyPlan(
        "update",
        [f"update Actions policy '{POLICY_NAME}' (id {pid}): {'; '.join(adds)}"] + others,
        policy_id=pid,
        body=body,
        observed=(tuple(ids), json.dumps({k: existing.get(k) for k in _OBSERVED_FIELDS}, sort_keys=True)),
    )


def apply(repo, planned):
    """Makes `planned`'s write. Returns (ok, lines): what was done, or why
    nothing was.

    Plans again first and writes only while the fresh read sees what the
    plan read: a policy someone changed since is left as they left it, and
    the next run plans from that."""
    fresh = plan(repo)
    if fresh.state == "error":
        return False, fresh.lines
    if fresh.state in ("allowed", "private"):
        return True, [f"nothing to write after all: {fresh.lines[0]}"]
    if (fresh.state, fresh.policy_id, fresh.observed) != (planned.state, planned.policy_id, planned.observed):
        return False, [
            f"Actions policy '{POLICY_NAME}' on {repo} changed since this run read it, so "
            "nothing was written; the next run plans from what is there now"
        ]
    if planned.state == "create":
        args = _api("--method", "POST", f"repos/{repo}/actions/policies", "--input", "-")
        done = f"created Actions policy '{POLICY_NAME}': {ALLOWED_LINE.split(' (')[0]}"
    else:
        args = _api("--method", "PUT", f"repos/{repo}/actions/policies/{planned.policy_id}", "--input", "-")
        done = planned.lines[0].replace("update Actions policy", "updated Actions policy", 1)
    try:
        gh.run_with_input(args, json.dumps(planned.body).encode())
    except gh.GhError as e:
        return False, _error_lines(
            f"could not {'create' if planned.state == 'create' else 'update'} Actions policy "
            f"'{POLICY_NAME}' on {repo}:",
            e,
        )
    return True, [done]
