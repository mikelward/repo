"""The fleet's tag ruleset: a pushed `v*` tag stays where it was pushed
(SPEC.md, *The standard*).

A release is named by its tag -- `v<commits>` for the Android apps,
`v0.MINOR.<commits>` for tidewm -- and whatever pins one (a setup script
building a pinned compositor, a person reading the Releases page) trusts it
to name the same commit for good. GitHub lets anyone with write access move
or delete a tag, so this keeps one repository ruleset, TAG_RULESET_NAME,
restricting updates and deletions of tags matching TAG_PATTERN. Creating one
stays open: every release job in the fleet creates its tag and never moves or
deletes one.

The standard is a floor, as the branch ruleset's is: a ruleset already under
the name keeps everything it has, and a run only adds the pattern or rules it
lacks. Its bypass list stays as a person left it.

Separate from repo_lib.rules, which is about one branch ruleset and the
checks it requires: none of that applies here. A tag ruleset requires no
check, gates no merge and waits on nothing, so it lands on the first run, and
a pull request cannot be wedged by it.

`plan` only reads, so `repo audit` asks it too: one definition of
"protected" rather than two that drift.
"""

import fnmatch
import json
from dataclasses import dataclass, field
from typing import List, Optional

from repo_lib import gh

TAG_RULESET_NAME = "release tags"
TAG_PATTERN = "refs/tags/v*"
# GitHub's tag rule types: `update` stops a tag being moved, `deletion` stops
# it being deleted. `creation` is left out on purpose: a release job creates
# its tag.
TAG_RULE_TYPES = ("update", "deletion")
# The fields of a ruleset that say what it does, compared to tell whether
# one changed between the plan and the write. GitHub's read adds others
# (`updated_at`, `_links`) that say nothing about that.
_OBSERVED_FIELDS = ("name", "target", "enforcement", "conditions", "rules", "bypass_actors")


# Release tags as the fleet names them, for testing whether an exclude
# covers them all.
_RELEASE_TAGS = tuple(f"refs/tags/v{version}" for version in ("0", "1.2.3", "0.0.1448", "10.0.0-rc.1"))


def _undoes(exclude):
    """Whether an exclude takes every release tag back out: `~ALL`, or a
    pattern every release tag matches, as `refs/tags/*` does.

    Tested against release tags as the fleet names them, not against
    TAG_PATTERN's own spelling: `refs/tags/v?` matches the text
    "refs/tags/v*" (its ? takes the *) yet excludes no v1.2.3 (Codex,
    mikelward/repo#105). A narrower exclude (`refs/tags/v0.0.*`) is a
    person's decision about some tags and stays as it is. GitHub matches
    with fnmatch, whose `*` stops at a `/` where Python's does not, so this
    can also catch one GitHub wouldn't apply that widely: that is held for
    a person, never passed as protected."""
    return exclude == "~ALL" or all(fnmatch.fnmatchcase(tag, exclude) for tag in _RELEASE_TAGS)


PROTECTED_LINE = "no one may move or delete a tag matching v* (ruleset '" + TAG_RULESET_NAME + "')"


@dataclass
class TagPlan:
    """What a run will do: "protected" (nothing), "create", "update", or
    "error" (lines say why, and nothing is written)."""

    state: str
    lines: List[str] = field(default_factory=list)
    ruleset_id: Optional[str] = None
    body: Optional[dict] = None
    # What the plan read: the ids under the name and the written-to one's
    # own fields. A write goes ahead only while a fresh read still sees the
    # same, since two different rulesets can plan the same write.
    observed: Optional[tuple] = None


def _create_body():
    return {
        "name": TAG_RULESET_NAME,
        "target": "tag",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": [TAG_PATTERN], "exclude": []}},
        "rules": [{"type": rule_type} for rule_type in TAG_RULE_TYPES],
        "bypass_actors": [],
    }


def _read_error(what, e):
    return [what] + [f"  {line}" for line in (e.stderr or "").splitlines()]


def plan(repo):
    """The TagPlan for `repo`. Reads only.

    A failed read is an error, never "there is no ruleset": creating one
    beside a ruleset this run could not see would leave two under the
    name."""
    try:
        out = gh.run(
            [
                "api",
                "--paginate",
                f"repos/{repo}/rulesets?includes_parents=false",
                "--jq",
                f".[] | select(.name == {json.dumps(TAG_RULESET_NAME)}) | .id",
            ]
        )
    except gh.GhError as e:
        return TagPlan(
            "error",
            _read_error(
                f"could not list {repo}'s rulesets, so cannot tell whether "
                f"'{TAG_RULESET_NAME}' already exists:",
                e,
            ),
        )
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    if not ids:
        return TagPlan(
            "create",
            [f"create ruleset '{TAG_RULESET_NAME}': no one may move or delete a tag matching v*"],
            body=_create_body(),
            observed=((), None),
        )

    rid = ids[0]
    others = [
        f"{len(ids) - 1} more ruleset{'s' if len(ids) > 2 else ''} named '{TAG_RULESET_NAME}' "
        f"(id {', '.join(ids[1:])}) left as {'they are' if len(ids) > 2 else 'it is'}; "
        f"this run reads and writes id {rid}"
    ] if len(ids) > 1 else []
    try:
        existing = json.loads(gh.run(["api", f"repos/{repo}/rulesets/{rid}"]))
    except gh.GhError as e:
        return TagPlan(
            "error",
            _read_error(f"could not read ruleset '{TAG_RULESET_NAME}' (id {rid}) on {repo}:", e),
            ruleset_id=rid,
        )
    except ValueError:
        return TagPlan(
            "error",
            [f"could not read ruleset '{TAG_RULESET_NAME}' (id {rid}) on {repo}: not JSON"],
            ruleset_id=rid,
        )

    where = f"ruleset '{TAG_RULESET_NAME}' (id {rid}) on {repo}"
    if existing.get("target") != "tag":
        return TagPlan(
            "error",
            [
                f"{where} targets '{existing.get('target')}', not tags, so the tag rules this "
                "writes don't belong in it; rename it, and a rerun adds the tag ruleset"
            ],
            ruleset_id=rid,
        )
    if existing.get("enforcement") != "active":
        return TagPlan(
            "error",
            [
                f"{where} is '{existing.get('enforcement')}', not 'active', so it stops no one; "
                "activate it by hand, since whoever turned it off may have had a reason"
            ],
            ruleset_id=rid,
        )
    conditions = dict(existing.get("conditions") or {})
    ref_name = conditions.get("ref_name") or {}
    include = list(ref_name.get("include") or [])
    exclude = list(ref_name.get("exclude") or [])
    undoing = [ref for ref in exclude if _undoes(ref)]
    if undoing:
        return TagPlan(
            "error",
            [
                f"{where} excludes {', '.join(undoing)}, which takes every v* tag back out of "
                "it; remove that by hand, since an exclude is a person's decision"
            ],
            ruleset_id=rid,
        )

    existing_rules = list(existing.get("rules") or [])
    have = {rule.get("type") for rule in existing_rules}
    missing_rules = [rule_type for rule_type in TAG_RULE_TYPES if rule_type not in have]
    missing_pattern = TAG_PATTERN not in include
    if not missing_rules and not missing_pattern:
        return TagPlan("protected", [f"already: {PROTECTED_LINE}"] + others, ruleset_id=rid)

    adds = []
    if missing_pattern:
        adds.append(f"cover {TAG_PATTERN}")
    adds += [
        {"update": "stop a tag being moved", "deletion": "stop a tag being deleted"}[rule_type]
        for rule_type in missing_rules
    ]
    conditions["ref_name"] = {
        "include": include + ([TAG_PATTERN] if missing_pattern else []),
        "exclude": exclude,
    }
    body = {
        "name": TAG_RULESET_NAME,
        "target": "tag",
        "enforcement": "active",
        "conditions": conditions,
        "rules": existing_rules + [{"type": rule_type} for rule_type in missing_rules],
        "bypass_actors": existing.get("bypass_actors") or [],
    }
    return TagPlan(
        "update",
        [f"update ruleset '{TAG_RULESET_NAME}' (id {rid}): {', '.join(adds)}"] + others,
        ruleset_id=rid,
        body=body,
        observed=(tuple(ids), json.dumps({k: existing.get(k) for k in _OBSERVED_FIELDS}, sort_keys=True)),
    )


def apply(repo, planned):
    """Makes `planned`'s write. Returns (ok, lines): what was done, or why
    nothing was.

    Plans again first and writes only while the fresh read sees what the
    plan read: a ruleset someone changed since is left as they left it, and
    the next run plans from that. The reads are compared, not the writes
    they plan: someone removing a rule since leaves a fresh plan writing
    the same body, and writing it would put their removal back (Codex,
    mikelward/repo#105)."""
    fresh = plan(repo)
    if fresh.state == "error":
        return False, fresh.lines
    if fresh.state == "protected":
        return True, [f"nothing to write after all: {PROTECTED_LINE}"]
    if (fresh.state, fresh.ruleset_id, fresh.observed) != (
        planned.state,
        planned.ruleset_id,
        planned.observed,
    ):
        return False, [
            f"ruleset '{TAG_RULESET_NAME}' on {repo} changed since this run read it, so "
            "nothing was written; the next run plans from what is there now"
        ]
    if planned.state == "create":
        args = ["api", "--method", "POST", f"repos/{repo}/rulesets", "--input", "-"]
        done = f"created ruleset '{TAG_RULESET_NAME}': no one may move or delete a tag matching v*"
    else:
        args = [
            "api",
            "--method",
            "PUT",
            f"repos/{repo}/rulesets/{planned.ruleset_id}",
            "--input",
            "-",
        ]
        done = planned.lines[0].replace("update ruleset", "updated ruleset", 1)
    try:
        gh.run_with_input(args, json.dumps(planned.body).encode())
    except gh.GhError as e:
        return False, _read_error(
            f"could not {'create' if planned.state == 'create' else 'update'} ruleset "
            f"'{TAG_RULESET_NAME}' on {repo}:",
            e,
        )
    return True, [done]
