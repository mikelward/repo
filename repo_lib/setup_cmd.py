"""`repo setup` -- compose rules, secrets, App membership, fleet credentials,
auto-merge, delete-branch-on-merge, and the fleet CI scaffold's gaps.

Port of mikelward/scripts's repo-setup (see its own header comment for the
full reasoning; repeated here only where the port changes something). A
thin composer, not a reimplementation: the ruleset step calls rules.py's
apply_ruleset directly and each secret step calls secrets_cmd.py's own
plan/write functions directly -- neither's real logic (the never-reported-
check guard, the confirm/diff machinery, the sealed-box secret write) is
duplicated here. Only the App-installation step is native (see apps.py),
because there is no sibling implementation to port from yet.

Every requested step runs its own dry-run/plan first, and the combined
plan is confirmed ONCE for the whole repository -- not once per step.
Once confirmed, each step is applied for real with its own force=True,
since this function's own confirmation already stands in for that
step's. The plan is also PRINTED once, but only when there's an actual
question to show it for (needs_confirmation and not --force) or under
--verbose; otherwise this stays quiet by default and Apply reports only
what actually changed -- see show_plan and _progress.

Every step is attempted regardless of whether an earlier one failed -- a
secret write failing must not also skip fixing the ruleset, or vice versa.
Failures are named at the end; the exit status is nonzero if any step
failed, whatever else succeeded.

One shell idiom from the porting source is deliberately NOT carried over,
because Python already removes the problem it existed to guard against
(see AGENTS.md: "don't port shell idioms that exist only because shell
has no better option"): reject_newline(), which existed because bash's
RULES/SECRETS/APPS arrays, once handed to a separately-invoked script,
relied on a newline as the between-entries delimiter, so a literal
newline WITHIN one entry was indistinguishable from two entries. Nothing
here is delimited that way -- args reach `gh` as a real argv list
(repo_lib.gh.run/try_run never goes through a shell), and every value
that reaches a --jq program's own text is already restricted, upstream,
to a character class that cannot contain a newline
(secrets_cmd.NAME_CHARS_RE / apps.SLUG_RE), so nothing downstream can
misread one as a second, unintended entry.

The shell's OTHER staleness idiom -- comparing two dry-run TEXT snapshots
byte-for-byte immediately before applying, to catch the ruleset having
changed since it was shown -- was ALSO dropped initially, on the
reasoning that rules.apply_ruleset()'s own "re-read and re-validate
immediately before writing" already covered the same ground since it's
called directly here, not spawned. That reasoning was wrong, in more
than one way, across several rounds of review: a same-call-only recheck
cannot catch the ruleset having been deleted and a DIFFERENT one created
under the same name in the window between an earlier PREVIEW call and a
later real apply's own start; the SAME ruleset id can need a write at
real-apply time its own preview never showed, with no identity change to
catch; and the SAME id, still needing the SAME write in the "yes/no"
sense, can have its actual MANAGED CONTENT edited by something else in
that same window -- a required check re-pointed at a different
integration, say -- so the write that actually happens differs from what
was shown and confirmed even though nothing about "does this need a
write" ever flagged it. See TODO.md's "Decisions needing review" for how
each of those was found and what finally replaced the dropped check.

What replaced it, after those rounds, is not a revival of the shell's
byte-for-byte TEXT comparison (which would also trip on cosmetic
differences having nothing to do with what actually gets written) but a
structural equivalent: apply_ruleset() computes a `fingerprint` --
`(existing_id, adopted_legacy, needs_write, target_body, deletions)`,
everything it has decided to do: the ruleset it's acting on, whether it
would actually write anything, the exact API body it's about to send if
so, and any superseded duplicate it will delete afterwards -- once per
pass, exposes the preview's via
`report["fingerprint"]`, and takes that back as an optional
expected_fingerprint on a later call, refusing if a freshly recomputed
fingerprint (right before the real write) doesn't match. One check
replaces what had grown to four narrower ones (an id-only comparison, a
needs-write-only comparison, and two hand-written "still resolves by
name" rechecks) because a fingerprint mismatch is exactly "the write
about to happen differs from the one that was shown and confirmed",
however that came about -- see apply_ruleset's own docstring for the
full reasoning and why ownership/scope validation stay as their own,
separately-worded rechecks rather than folding into it too.

The equivalent snapshot-before-showing-the-plan protection remains
needed, unchanged, for a --secret's file (see _validate_secret_specs
below): unlike the ruleset step, nothing else in this run re-reads that
file, so without a snapshot an edit during the confirmation prompt would
silently change what gets written.
"""

import io
import json
import re
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from typing import Optional

from repo_lib import apps, credentials, gh, rules, scaffold, secrets_cmd
from repo_lib.common import error, error_lines

# The lookaheads reject `.` and `..` components: made of allowed
# characters, but as path segments spliced into `repos/{repo}/...` they
# would address a different endpoint than the one that was validated.
OWNER_REPO_RE = re.compile(r"^(?!\.\.?/)[A-Za-z0-9._-]+/(?!\.\.?$)[A-Za-z0-9._-]+$")


def _progress(args, message):
    """A one-line "still working" marker for a step that's about to make
    a handful of gh calls -- shown only under --verbose. Run over a fleet
    of repositories with nothing printed in between, a step that takes a
    few seconds looks identical to one that's hung; this is what makes
    the difference visible without also dumping the full plan by default
    (see show_plan, on the combined-plan dump below, for that split)."""
    if args.verbose:
        error(message)


def add_arguments(parser):
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="apply without confirming")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show the full plan (not just what changed) and per-step progress",
    )
    parser.add_argument("--no-rules", action="store_true", help="skip the ruleset step")
    parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="skip adding any missing fleet CI scaffold file (codex-review, zizmor, lanes)",
    )
    parser.add_argument("--rule", action="append", default=[], help="a required check (repeatable)")
    parser.add_argument(
        "--secret", action="append", default=[], metavar="NAME[@ENV]=PATH", help="repeatable"
    )
    parser.add_argument(
        "--credential",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=(
            "a fleet credential's value (one of "
            + ", ".join(credentials.FLEET_CREDENTIALS)
            + "), set in the environment it belongs in when this repository uses it (repeatable)"
        ),
    )
    parser.add_argument("--app", action="append", default=[], help="a GitHub App slug (repeatable)")
    parser.add_argument("repo", metavar="OWNER/REPO")


@dataclass
class SecretSpec:
    name: str
    env: Optional[str]
    path: str
    raw: str
    value: bytes = b""


def _parse_secret_spec(raw):
    """NAME[@ENV]=PATH -> SecretSpec (value unset). A malformed spec is a
    usage error before anything else runs."""
    if "=" not in raw:
        error(f"'--secret {raw}' is not NAME[@ENV]=PATH (missing '=')")
        raise SystemExit(2)
    left, path = raw.split("=", 1)
    if "@" in left:
        name, env = left.split("@", 1)
        if not env:
            # An @ present, but nothing after it, is a malformed spec, not
            # "no environment" -- the caller wrote the @, meaning they
            # wanted an environment scope.
            error(f"'--secret {raw}' has an empty ENV after '@'")
            raise SystemExit(2)
    else:
        name, env = left, None
    if not name:
        error(f"'--secret {raw}' has an empty NAME")
        raise SystemExit(2)
    if not path:
        error(f"'--secret {raw}' has an empty PATH")
        raise SystemExit(2)
    return SecretSpec(name=name, env=env, path=path, raw=raw)


def _validate_secret_specs(raw_specs):
    """Parses and fully validates every --secret spec up front -- shape,
    name/env validity (reusing secrets_cmd's own rules rather than
    re-deriving a looser copy of them), and two specs colliding on the
    same NAME+ENV scope (case-insensitively, since both GitHub secret
    names and environment names are) -- as a usage error before any gh
    call runs. Each file's bytes are read right here too: one read serves
    both as the readability check and as the snapshot the porting source's
    own repo-setup takes for exactly this reason (a file edited during the
    later confirmation prompt must not ship different content than what
    was shown and confirmed) -- there's no reason to check readability
    once and then read the bytes again later from the same path."""
    specs = []
    seen = {}
    for raw in raw_specs:
        spec = _parse_secret_spec(raw)
        name_err = secrets_cmd.validate_name(spec.name)
        if name_err:
            error(name_err)
            raise SystemExit(2)
        if spec.env:
            env_err = secrets_cmd.validate_env(spec.env)
            if env_err:
                error(env_err)
                raise SystemExit(2)
        key = (spec.name.lower(), (spec.env or "").lower())
        if key in seen:
            error(f"'--secret {raw}' repeats an earlier --secret's NAME[@ENV]")
            label = spec.name + (f"@{spec.env}" if spec.env else "")
            error(f"({label}) -- the second would silently overwrite the first")
            error("with no warning; drop one of them.")
            raise SystemExit(2)
        seen[key] = True
        try:
            with open(spec.path, "rb") as f:
                spec.value = f.read()
        except OSError:
            error(f"cannot read '{spec.path}' (from --secret {raw})")
            raise SystemExit(2)
        if not spec.value:
            error(f"the value at '{spec.path}' (from --secret {raw}) is empty")
            raise SystemExit(2)
        specs.append(spec)
    return specs


@dataclass
class CredentialSpec:
    name: str
    path: str
    raw: str
    value: bytes = b""


def _validate_credential_specs(raw_specs):
    """NAME=PATH -> CredentialSpec, every one validated up front as a usage
    error: the name must be a fleet credential (there is no other kind
    setup knows where to put), spelled in any case since GitHub matches
    secret names case-insensitively; two specs for one name would silently
    have the second win; and the file is read here, once, as both the
    readability check and the snapshot -- see _validate_secret_specs."""
    specs = []
    seen = set()
    for raw in raw_specs:
        if "=" not in raw:
            error(f"'--credential {raw}' is not NAME=PATH (missing '=')")
            raise SystemExit(2)
        name, path = raw.split("=", 1)
        name = name.upper()
        if not name:
            error(f"'--credential {raw}' has an empty NAME")
            raise SystemExit(2)
        if not path:
            error(f"'--credential {raw}' has an empty PATH")
            raise SystemExit(2)
        if name not in credentials.FLEET_CREDENTIALS:
            error(f"'{name}' (from --credential {raw}) is not a fleet credential; those are:")
            for known in credentials.FLEET_CREDENTIALS:
                error(f"  {known} (environment '{credentials.home_environment(known)}')")
            error("For any other secret, --secret NAME[@ENV]=PATH sets it where you say.")
            raise SystemExit(2)
        if name in seen:
            error(f"'--credential {raw}' repeats an earlier --credential's NAME ({name})")
            error("-- the second would silently win; drop one of them.")
            raise SystemExit(2)
        seen.add(name)
        spec = CredentialSpec(name=name, path=path, raw=raw)
        try:
            with open(spec.path, "rb") as f:
                spec.value = f.read()
        except OSError:
            error(f"cannot read '{spec.path}' (from --credential {raw})")
            raise SystemExit(2)
        if not spec.value:
            error(f"the value at '{spec.path}' (from --credential {raw}) is empty")
            raise SystemExit(2)
        specs.append(spec)
    return specs


@dataclass
class CredentialMove:
    """One reusable workflow's credential brought into line: the
    environment writes, then -- only once every write has landed -- the
    deletions of the copies that write makes redundant. A delete never
    runs ahead of the write it depends on, or after one that failed: the
    repository copy is what keeps the workflow working until the
    environment holds the credential. `recheck` re-reads the caller
    immediately before the deletes and answers None when the plan still
    holds, else why it no longer does -- the confirmation prompt can sit
    for an arbitrary time, and a caller that went back to naming its
    secrets in that window would be left with nothing (Codex,
    mikelward/repo#13)."""

    label: str
    writes: list  # (name, environment, value, environment already exists)
    deletes: list  # (name, environment or None for the repository level)
    recheck: object = None  # () -> None | str
    # (environment, default branch) to restrict the environment to, BEFORE
    # this move's writes and deletes -- the lanes environment, when any
    # branch can reach it. Before, because a restriction that fails after
    # the write leaves the credential somewhere every branch can read it.
    # Re-read at apply time; a policy someone set meanwhile is left alone.
    restrict: object = None
    # (environment, default branch) whose policy is confirmed once more
    # AFTER the writes and before the deletes. Everything else about this
    # move is checked before the writes, which leaves the writes' own
    # window: an administrator reopening the environment there had the
    # pair written into it and the repository copies deleted behind it,
    # and the run exited 0 with the credential reachable from an untrusted
    # branch (Codex, mikelward/repo#36). The deletes are the irreversible
    # half, so they are what this gates -- the copies stay, and the run
    # says why. Never a re-restriction: a policy someone set meanwhile is
    # theirs, and this rewrites nobody's.
    shut: object = None


@dataclass
class CredentialsPlan:
    lines: list  # what the combined plan shows, one per action or fact
    moves: list  # CredentialMove, in order
    unfixed: list  # what this run cannot fix, each reason a failure at the end
    # A subset of `lines`: a --credential value the caller supplied that
    # this run did nothing with (nothing here uses it). Never gated behind
    # --verbose or a confirmation, unlike the rest of `lines` -- a
    # supplied value going nowhere, with no move to apply and so no
    # Apply-time print of its own, is exactly the silent-drop this exists
    # to prevent (Codex, mikelward/repo#13).
    always_report: list = field(default_factory=list)
    failed: bool = False  # a read failed; `lines` carries the error


def _plan_credentials(repo, specs):
    """Where each fleet credential is on `repo`, against where it belongs
    (repo_lib.credentials has the reasoning), and what closes the gap:

    - a credential supplied by --credential is set in its environment when
      the repository uses the workflow that reads it -- a batch, by the
      caller file `<hub>.yml`; the commit-back workflow, by a job calling
      it -- and is otherwise left unset, with a line saying so;
    - a repository-level copy is deleted once the environment holds a
      usable credential (the PAT, or both halves of a batch's App pair),
      whether it already did or this run's write puts it there;
    - a copy for a workflow the repository does not use is deleted, at the
      repository level and in the environment alike;
    - neither a write nor a delete happens while the caller still passes
      its secrets by name: an environment secret reaches a called
      workflow only through `secrets: inherit`, so moving the credential
      out from under such a caller hands the workflow nothing -- and a
      credential the environment already holds is idle under such a
      caller, which is the same finding. That, and a credential this run
      was not given a value for, are reported as not fixed, and fail the
      run -- the point of this step is that a clean exit means the
      repository is in shape;
    - every delete is re-validated immediately before it runs (the caller
      re-read, the workflows re-listed), since the confirmation prompt can
      sit for an arbitrary time; a plan that no longer holds keeps the
      copy and fails the step;
    - the lanes App pair follows the same rules with one substitution: the
      action reads it from the job's own `environment:` declaration, not
      through `secrets: inherit`, so a publishing job (one handing the
      action `app-id`) that does not declare the `lanes` environment is
      what holds a move back; and the environment itself, when any branch
      can reach it, is restricted to the default branch after the write --
      a policy someone set by hand is reported, never rewritten;
    - "unused" is decided from the text, not from what the reader could
      parse: a credential is deleted as unused only when no workflow
      mentions its reader at all. A mention with no readable job-level
      caller (flow style, a shape the fleet does not write) is reported as
      not fixed and nothing is deleted -- the same fail-closed direction as
      the rest of this step.

    GitHub never returns a secret's value, which is why a move needs the
    value handed in: setup can delete a copy it can see, but cannot copy
    it."""
    plan = CredentialsPlan(lines=[], moves=[], unfixed=[])
    given = {spec.name: spec for spec in specs}
    try:
        repo_secrets = credentials.repository_secrets(repo)
        environments = credentials.environments(repo)
        default = credentials.default_branch(repo)
        texts = credentials.workflow_texts(repo, default)
        held = {
            env: credentials.environment_secrets(repo, environments, env)
            for env in (*credentials.BATCH_HUBS, credentials.COMMIT_ARTIFACT_ENV, credentials.LANES_ENV)
        }
    except credentials.ReadError as e:
        plan.failed = True
        plan.lines.append(e.message)
        plan.lines += [f"  {line}" for line in (e.detail or "").splitlines()]
        return plan

    def reread():
        """The workflows' texts as they are now, on every branch, for a
        recheck."""
        return credentials.workflow_texts(repo)

    def guarded(check):
        """Wraps a recheck so a failed read reads as "no longer holds"
        rather than an exception out of the apply loop: a delete this
        step cannot re-validate does not happen."""

        def run():
            try:
                return check()
            except credentials.ReadError as e:
                return f"{e.message.rstrip(':')} (the plan could not be re-validated)"

        return run

    def unused(label, names, listed, env_secrets, why, recheck):
        move = CredentialMove(label=label, writes=[], deletes=[], recheck=guarded(recheck))
        for name in names:
            if name in repo_secrets:
                move.deletes.append((name, None))
                plan.lines.append(f"{label}: delete repository secret {name} -- {why}")
            if name in env_secrets:
                move.deletes.append((name, listed))
                plan.lines.append(f"{label}: delete {name} from environment '{listed}' -- {why}")
            if name in given:
                line = f"{label}: {name} not set -- {why}"
                plan.lines.append(line)
                plan.always_report.append(line)
        if move.deletes:
            plan.moves.append(move)

    def move(
        label, names, listed, env_secrets, env, caller_desc, inherits, usable, suggest, recheck,
        how=None, hint="convert the caller to `secrets: inherit` first",
    ):
        at_repo = [name for name in names if name in repo_secrets]
        in_env = [name for name in names if name in env_secrets]
        supplied = [name for name in names if name in given]
        held_after = set(in_env) | set(supplied)
        # A caller that names its secrets is a problem whenever there is
        # something the environment holds or would hold: a value to set, a
        # repository copy to delete, or a credential already sitting there
        # that such a caller can never read. `how` and `hint` name the
        # shape for a reader that is not a reusable-workflow caller.
        if inherits is not True and (supplied or at_repo or usable(set(in_env))):
            if how is None:
                how = (
                    f"{caller_desc} passes its secrets by name"
                    if inherits is False
                    else f"{caller_desc} does not call the workflow"
                )
            left = ", ".join(dict.fromkeys(supplied + at_repo))
            plan.unfixed.append(
                f"{label}: {how}, so a credential in the '{env}' environment would never reach "
                f"it -- {hint}"
                + (f"; {left} left as is" if left else "")
            )
            return
        def recheck_all():
            reason = recheck()
            if reason is not None:
                return reason
            # The destination is re-read too, whenever usability rests on
            # what the environment already holds: a delete justified by a
            # credential already there has to find it still there, since
            # the confirmation prompt can have sat while an administrator
            # removed it (Codex, mikelward/repo#13). A value supplied on
            # the command line is about to be written, so it counts; the
            # environment's own copy is what has to be re-seen.
            #
            # Not gated on the deletes: completing a half already in the
            # environment queues a write and no delete at all, so that
            # gate skipped the read for exactly the case where the other
            # half is the thing that might be gone -- setup then wrote one
            # secret, restricted the environment and exited 0 with a
            # credential that cannot authenticate (Codex,
            # mikelward/repo#36). `usable(set(supplied))` is the real
            # question either way: false means this run is relying on the
            # destination.
            if not usable(set(supplied)):
                _listed, env_now = credentials.environment_secrets(repo, credentials.environments(repo), env)
                if not usable(set(env_now) | set(supplied)):
                    return f"the '{env}' environment no longer holds the credential"
            return None

        step = CredentialMove(label=label, writes=[], deletes=[], recheck=guarded(recheck_all))
        if usable(held_after):
            # Only a credential that WORKS after this run is written. Half a
            # pair does nothing in the environment except sit there, and for
            # the lanes App it sits there exposed: the caller returns early
            # on the unfixed line below, so the environment is never
            # restricted, and the supplied half lands somewhere any branch
            # can read it beside the other half still at repository level
            # (Codex, mikelward/repo#36).
            for name in supplied:
                state = "OVERWRITES an existing value" if name in in_env else "new"
                step.writes.append((name, env, given[name].value, listed is not None))
                plan.lines.append(f"{label}: set {name} in environment '{env}' ({state})")
            for name in at_repo:
                step.deletes.append((name, None))
                plan.lines.append(
                    f"{label}: delete repository secret {name} -- the '{env}' environment holds "
                    f"the credential{' once set' if supplied else ''}"
                )
            if not supplied and not at_repo:
                plan.lines.append(f"{label}: the credential lives in the '{env}' environment")
        else:
            flags = " ".join(f"--credential {name}=PATH" for name in suggest(held_after))
            stays = f"; {', '.join(at_repo)} stays a repository secret until then" if at_repo else ""
            plan.unfixed.append(
                f"{label}: environment '{env}' holds no credential -- pass {flags} to set one{stays}"
            )
            for name in supplied:
                # Said whatever the verbosity, like every other supplied
                # value this run declined to use: a --credential that
                # silently did nothing is the one a reader assumes landed.
                line = (
                    f"{label}: {name} not set -- half a credential is not one, and the rest of it "
                    f"has to arrive in the same run"
                )
                plan.lines.append(line)
                plan.always_report.append(line)
        if step.writes or step.deletes:
            plan.moves.append(step)

    def settle(label, names, prefix, listed, env_secrets, env, usable, suggest):
        """Plans one reusable workflow's credentials from every workflow that
        calls it from a job, whatever the file is named -- the fleet names a
        batch's caller `<hub>.yml`, but GitHub runs any name, and a second
        caller under another name would be stranded by a delete the named
        one justifies (Codex, mikelward/repo#13). A workflow that mentions
        it in a shape the reader cannot resolve is "cannot tell", and holds
        everything back."""
        found = credentials.callers(texts, prefix)
        unread = credentials.unread_mentions(texts, prefix)
        if unread:
            plan.unfixed.append(
                f"{label}: {credentials.workflow_labels(unread)} mentions {prefix} in a shape "
                f"this cannot read as a "
                f"caller -- whether it is used there cannot be told, so nothing is deleted; write "
                f"the caller as a job-level `uses:`, or delete the credential by hand"
            )
            return

        def now():
            """(reason, callers) as the workflows read now, for a recheck."""
            texts_now = reread()
            callers_now = credentials.callers(texts_now, prefix)
            if credentials.unread_mentions(texts_now, prefix):
                return f"a workflow now mentions {prefix} in a shape this cannot read as a caller", callers_now
            return None, callers_now

        if not found:

            def still_unused():
                reason, callers_now = now()
                if reason is None and callers_now:
                    reason = (
                        f"{credentials.workflow_labels(sorted(callers_now))} "
                        "appeared since the plan was built"
                    )
                return reason

            unused(
                label,
                names,
                listed,
                env_secrets,
                f"no workflow here calls {prefix.rstrip('/')}, so nothing uses it",
                still_unused,
            )
            return

        def still_inherits():
            reason, callers_now = now()
            if reason is not None:
                return reason
            if set(callers_now) != set(found):
                return (
                    f"the callers changed since the plan was built "
                    f"({credentials.workflow_labels(sorted(callers_now)) or 'none'} now)"
                )
            naming = sorted(name for name, inherits in callers_now.items() if not inherits)
            if naming:
                return (
                    f"{credentials.workflow_labels(naming)} no longer passes "
                    "`secrets: inherit`"
                )
            return None

        failing = sorted(name for name, inherits in found.items() if not inherits)
        move(
            label,
            names,
            listed,
            env_secrets,
            env,
            credentials.workflow_labels(failing or sorted(found)),
            not failing,
            usable,
            suggest,
            still_inherits,
        )

    for hub in credentials.BATCH_HUBS:
        pat, app_id, app_key = names = credentials.batch_credentials(hub)
        listed, env_secrets = held[hub]

        def suggest(held_after, pat=pat, app_id=app_id, app_key=app_key):
            # The other half of a pair the environment already holds half
            # of; otherwise the PAT, the simpler credential.
            if app_id in held_after and app_key not in held_after:
                return [app_key]
            if app_key in held_after and app_id not in held_after:
                return [app_id]
            return [pat]

        settle(
            hub,
            names,
            credentials.hub_workflow(hub),
            listed,
            env_secrets,
            listed or hub,
            lambda held_after, hub=hub: credentials.usable(held_after, hub),
            suggest,
        )

    token = credentials.COMMIT_ARTIFACT_TOKEN
    label = credentials.COMMIT_ARTIFACT_ENV
    listed, env_secrets = held[label]
    settle(
        label,
        (token,),
        credentials.COMMIT_ARTIFACT_WORKFLOW,
        listed,
        env_secrets,
        listed or label,
        lambda held_after: token in held_after,
        lambda held_after: [token],
    )

    # The lanes App pair. An action step, not a called workflow: the
    # secret reaches it through the job's `environment:` declaration, so
    # a publishing job without one is the equivalent of a caller naming
    # its secrets, and holds the move back the same way. The environment
    # is settled too -- see CredentialMove.restrict.
    app_id, app_key = credentials.LANES_APP_ID, credentials.LANES_APP_PRIVATE_KEY
    label = credentials.LANES_ENV
    action = credentials.LANES_ACTION
    listed, env_secrets = held[label]
    publishers = credentials.lanes_publishers(texts)
    unread = credentials.lanes_unread(texts)
    incomplete = credentials.lanes_incomplete(texts)
    if incomplete:
        # A step handing the action half the pair publishes nothing and is
        # not the ambient pattern: the pair is neither unused nor safely
        # moved for it. Held as is until the step takes both inputs.
        plan.unfixed.append(
            f"{label}: {credentials.workflow_labels(incomplete)} hands {action} one of `app-id` and "
            f"`app-private-key` without the other, so the step cannot authenticate as the App -- hand it "
            f"both first; {app_id}, {app_key} left as is"
        )
        return plan
    if unread:
        plan.unfixed.append(
            f"{label}: {credentials.workflow_labels(unread)} mentions {action} in a shape this "
            f"cannot read as a step -- whether the App credential is used there cannot be told, so "
            f"nothing is deleted; write it as a step-level `uses:`, or delete the credential by hand"
        )
        return plan

    def lanes_now():
        """(reason, publishers) as the workflows read now, for a recheck."""
        texts_now = reread()
        if credentials.lanes_unread(texts_now):
            return f"a workflow now mentions {action} in a shape this cannot read as a step", None
        if credentials.lanes_incomplete(texts_now):
            return f"a workflow now hands {action} one of the two App inputs without the other", None
        return None, credentials.lanes_publishers(texts_now)

    if not publishers:

        def still_no_publisher():
            reason, now = lanes_now()
            if reason is None and now:
                reason = f"{credentials.workflow_labels(sorted(now))} appeared since the plan was built"
            return reason

        unused(
            label,
            (app_id, app_key),
            listed,
            env_secrets,
            f"no workflow here publishes the lanes status as the App (a {action} step taking "
            f"`app-id`), so nothing uses it",
            still_no_publisher,
        )
        return plan

    # Only the default branch's publishers hold the move back. A branch copy
    # of a publishing workflow runs from its branch, which the restricted
    # environment shuts out whether or not the job declares it -- so it
    # loses the credential either way, and keeping the repository copy for
    # its sake leaves the pair exposed to exactly that branch's
    # push-triggered run, the hole the move closes (Codex,
    # mikelward/repo#36). The publishing that the credential exists for
    # runs under `pull_request_target` from the default branch's copy.
    def on_default(found):
        return {name: declares for name, declares in found.items() if name.branch is None}

    undeclared = sorted(name for name, declares in on_default(publishers).items() if not declares)
    if not on_default(publishers):
        # A publisher only on a branch -- the pull request adopting lanes,
        # ordinarily -- keeps the pair, as a branch-only batch caller keeps
        # its credential (Codex, mikelward/repo#13): the move and the
        # restriction are what it needs once merged, and until then nothing
        # publishes. Said, so a success here is not read as a publisher
        # already reaching the credential (Codex, mikelward/repo#36).
        plan.lines.append(
            f"{label}: no workflow on the default branch publishes the lanes status as the App; "
            f"{credentials.workflow_labels(sorted(publishers))} does from a branch, which reaches "
            f"the environment once merged -- the pair is kept for it"
        )

    def still_declares():
        # The default branch first: the restriction names it, and a rename
        # while the prompt sat would restrict the environment to the old
        # name -- the new trusted branch shut out, the stale one let in
        # (Codex, mikelward/repo#36).
        default_now = credentials.default_branch(repo)
        if default_now != default:
            return f"the default branch is now '{default_now}', not '{default}'"
        reason, now = lanes_now()
        if reason is not None:
            return reason
        # The publishers the plan rested on are the ones rechecked: the
        # default branch's, or -- when it had none -- the branch copies
        # that kept the pair, so one that went away while the prompt sat
        # holds the apply back rather than leaving a pair nothing uses
        # (Codex, mikelward/repo#36).
        rested_on = on_default(publishers) or publishers
        rested_now = on_default(now) if on_default(publishers) else now
        if set(rested_now) != set(rested_on):
            return (
                f"the publishing workflows changed since the plan was built "
                f"({credentials.workflow_labels(sorted(rested_now)) or 'none'} now)"
            )
        missing = sorted(name for name, declares in on_default(now).items() if not declares)
        if missing:
            return (
                f"{credentials.workflow_labels(missing)} no longer declares "
                f"`environment: {label}` on every publishing job"
            )
        return None

    def suggest(held_after):
        return [name for name in (app_id, app_key) if name not in held_after] or [app_id, app_key]

    moves_before = len(plan.moves)
    unfixed_before = len(plan.unfixed)
    lines_before = len(plan.lines)

    def hold_this_runs_moves(why):
        """Drops the moves this step queued and the plan lines that offered
        them, returning the repository copies that consequently stay. A run
        that cannot put the environment's door in a state it vouches for
        must not write the pair into it or delete the copies keeping the
        workflows going -- and there are two ways to reach that, a policy
        this run may not rewrite and a policy read that failed, so it is
        one function rather than two copies (Codex, mikelward/repo#36)."""
        held = plan.moves[moves_before:]
        stays = [name for step in held for name, _scope in step.deletes]
        if held:
            del plan.moves[moves_before:]
            del plan.lines[lines_before:]
        for name in [write[0] for step in held for write in step.writes]:
            # Said whatever the verbosity, like every other supplied value
            # this run declined to use: a --credential that silently did
            # nothing is the one a reader assumes landed.
            line = f"{label}: {name} not set -- {why}"
            plan.lines.append(line)
            plan.always_report.append(line)
        return stays
    move(
        label,
        (app_id, app_key),
        listed,
        env_secrets,
        listed or label,
        credentials.workflow_labels(undeclared or sorted(on_default(publishers)) or sorted(publishers)),
        not undeclared,
        credentials.lanes_usable,
        suggest,
        guarded(still_declares),
        how=(
            f"{credentials.workflow_labels(undeclared)} publishes the lanes status as the App "
            f"from a job that does not declare `environment: {label}`"
        ),
        hint="declare the environment on every job that takes `app-id` first",
    )
    if len(plan.unfixed) > unfixed_before:
        # The move refused -- a publisher without the environment, or no
        # value to set: the credential is stranded or missing, and the
        # environment's policy is settled with it, not around it.
        return plan
    written_here = any(m.writes for m in plan.moves[moves_before:])
    if listed is None and not written_here:
        # No environment, and this run creates none: the missing
        # credential is already reported above, and there is nothing to
        # restrict yet.
        return plan
    if listed is None:
        policy = "open"  # an environment this run creates admits every branch
        env = label
    else:
        env = listed
        try:
            policy = credentials.environment_branch_policy(repo, listed)
        except credentials.ReadError as e:
            # A door this run cannot read is one it cannot vouch for, and
            # `plan.failed` is only reported at the END of the apply -- the
            # moves run first, so a forced run wrote the pair into an
            # environment nobody had established was shut and deleted the
            # repository copies behind it (Codex, mikelward/repo#36).
            stays = hold_this_runs_moves(
                f"the '{env}' environment's branch policy could not be read, so this run cannot "
                f"tell which branches reach it"
            )
            plan.failed = True
            plan.lines.append(e.message)
            plan.lines += [f"  {line}" for line in (e.detail or "").splitlines()]
            if stays:
                plan.lines.append(
                    f"{label}: {', '.join(stays)} stays a repository secret until the policy can be read"
                )
            return plan
    wrong = credentials.branch_policy_verdict(policy, default)

    def still_trusted(recheck):
        """`recheck`, and then the environment's branch policy again. The
        policy is the one input a run whose policy was already right never
        reads twice: a restriction carries its own apply-time re-read, and
        `move`'s destination read asks what the environment holds, not who
        may enter it. So an administrator opening the environment while
        the prompt sat let the apply write the pair in and delete the
        repository copies, exiting 0 with the credential reachable from an
        untrusted branch (Codex, mikelward/repo#36). Composed onto the
        move's own recheck rather than folded into `lanes_changed`, which
        answers for the workflows and is asked on paths that touch no
        environment; `recheck` runs first, so the default branch this
        compares against is one it has already confirmed."""

        def run():
            reason = recheck()
            if reason is not None:
                return reason
            wrong_now = credentials.branch_policy_verdict(
                credentials.environment_branch_policy(repo, env), default
            )
            return None if wrong_now is None else f"environment '{env}' now {wrong_now}"

        return guarded(run)

    if wrong is None:
        plan.lines.append(f"{label}: environment '{env}' admits only the trusted base branch")
        for step in plan.moves[moves_before:]:
            step.recheck = still_trusted(step.recheck)
            step.shut = (env, default)
    elif credentials.restrictable(policy):
        plan.lines.append(
            f"{label}: restrict environment '{env}' to branch '{default}' -- it {wrong}, so a "
            f"same-repo pull request's push-triggered workflow reads the App credential too"
        )
        # Re-validated like a delete: when the environment already holds
        # the pair, this is the only apply-time action, and a workflow that
        # stopped publishing while the prompt sat would leave it restricting
        # an environment nothing justifies any more (Codex, mikelward/repo#36).
        #
        # Carried by THIS run's own move where it made one, rather than
        # appended beside it. The apply loop runs moves in order and
        # restricts before a move's writes and deletes, so two moves cannot
        # express "shut the environment first": the writes move would run
        # first, and a restriction that then failed would leave the pair in
        # an environment any branch can enter -- the exposure this
        # placement exists to close, created by the run closing it (Codex,
        # mikelward/repo#36).
        mine = plan.moves[moves_before:]
        for step in mine:
            step.shut = (env, default)
        if mine:
            mine[-1].restrict = (env, default)
        else:

            def still_holds_the_pair():
                # The standalone move writes and deletes nothing, so
                # `move`'s own destination re-read never runs for it -- and
                # a recheck that asks only what the workflows say would
                # restrict an environment an administrator emptied while
                # the prompt sat, then exit 0 while a fresh plan and audit
                # both report no usable credential (Codex,
                # mikelward/repo#36). The restriction itself is harmless;
                # reporting the repository as in shape is not.
                reason = still_declares()
                if reason is not None:
                    return reason
                _listed, env_now = credentials.environment_secrets(
                    repo, credentials.environments(repo), env
                )
                if not credentials.lanes_usable(set(env_now)):
                    return f"the '{env}' environment no longer holds the credential"
                return None

            plan.moves.append(
                CredentialMove(
                    label=label,
                    writes=[],
                    deletes=[],
                    recheck=guarded(still_holds_the_pair),
                    restrict=(env, default),
                    shut=(env, default),
                )
            )
    else:
        # The environment is shut before the credential goes into it, and
        # this one cannot be shut at all: the policy is somebody's choice
        # and is not rewritten, so it keeps admitting a branch this cannot
        # trust. So the move this run queued is dropped rather than applied
        # beside the finding -- writing the pair here would put the whole
        # App credential where such a branch reads it, which is the
        # exposure the placement exists to close, and the deletes would
        # then leave it only there. The same hold as a restriction that
        # fails; the difference is only that this one is known at plan
        # time, so the plan never offers the writes at all (Codex,
        # mikelward/repo#36).
        stays = hold_this_runs_moves(
            f"the '{env}' environment admits a branch this cannot trust, and the credential is "
            f"not placed where such a branch reads it"
        )
        plan.unfixed.append(
            f"{label}: environment '{env}' {wrong} -- restrict it to '{default}' in the "
            f"environment's settings; a policy someone set is not rewritten"
            + (f"; {', '.join(stays)} stays a repository secret until then" if stays else "")
        )
    return plan


def _bootstrap_default_branch(repo):
    """`repo`'s default branch name for the bootstrap step, or None (with
    the failure already reported) if the read fails. Reuses
    credentials.default_branch rather than re-deriving the same `gh api
    repos/{repo} --jq .default_branch` call setup_cmd already reaches for
    via that module -- only the error handling differs, since credentials
    raises ReadError and the other steps here report-and-return-None."""
    try:
        return credentials.default_branch(repo)
    except credentials.ReadError as e:
        error_lines(e.message, e.detail or "")
        return None


def _plan_auto_merge(repo):
    """Whether the repository allows auto-merge: ("allowed" | "enable" |
    "error", the plan lines). The weekly batches arm auto-merge on the pull
    requests they open, so a repository with the setting off leaves them
    parked. Always on, like the fleet-credentials step: there is nothing to
    request, only a setting with one right value."""
    try:
        value = gh.run(["api", f"repos/{repo}", "--jq", ".allow_auto_merge"]).strip()
    except gh.GhError as e:
        lines = [f"could not read whether {repo} allows auto-merge:"]
        lines += [f"  {line}" for line in (e.stderr or "").splitlines()]
        return "error", lines
    if value == "true":
        return "allowed", ["already allowed"]
    return "enable", ["enable auto-merge on the repository (the weekly batches arm it on their pull requests)"]


def _plan_delete_branch_on_merge(repo):
    """Whether the repository deletes a pull request's head branch once it
    merges: ("allowed" | "enable" | "error", the plan lines). Same shape as
    _plan_auto_merge, and for the same reason -- there is nothing to
    request, only a setting with one right value. This is GitHub's own
    sweep, so it fires from the merge event itself: unlike an ancestry
    check, it is unaffected by this fleet rebase-merging (which rewrites a
    branch's commits, so the branch is never an ancestor of the default
    branch after merge -- see mikelward/repo's own `repo cleanup`, written
    to sweep up everything this setting being off left behind)."""
    try:
        value = gh.run(["api", f"repos/{repo}", "--jq", ".delete_branch_on_merge"]).strip()
    except gh.GhError as e:
        lines = [f"could not read whether {repo} deletes branches on merge:"]
        lines += [f"  {line}" for line in (e.stderr or "").splitlines()]
        return "error", lines
    if value == "true":
        return "allowed", ["already allowed"]
    return "enable", ["delete a pull request's head branch automatically once it merges"]


def _reject_fleet_credentials_under_secret(secret_specs):
    """A fleet credential has one place, and --credential is the flag that
    puts it there; a --secret naming one is refused whatever scope it
    names. At repository level, or in any environment but its own, it
    would write exactly the copy the fleet-credentials step exists to
    remove, and since that step plans from what it read before any write,
    the copy would survive the run with a clean exit. In its own
    environment it is a write that plan does not know about either: the
    step still reports the credential missing and keeps the repository
    copy, so the run does the right write and exits 1 for it (Codex,
    mikelward/repo#13, both). A usage error, before any gh call."""
    for spec in secret_specs:
        home = credentials.home_environment(spec.name)
        if home is None:
            continue
        name = spec.name.upper()
        error(f"'--secret {spec.raw}' names the fleet credential {name}, which `repo setup` places")
        error(f"itself: in the '{home}' environment and nowhere else, deleting the repository copy.")
        error(f"Use --credential {name}=PATH instead.")
        raise SystemExit(2)


def _validate_app_slugs(slugs):
    for slug in slugs:
        if not slug:
            error("--app needs a non-empty slug")
            raise SystemExit(2)
        if not apps.SLUG_RE.match(slug):
            error(f"'{slug}' contains characters this tool does not handle in an App slug")
            raise SystemExit(2)


def _secret_label(spec):
    return spec.name + (f" --env {spec.env}" if spec.env else "")


def run(args):
    if not OWNER_REPO_RE.match(args.repo):
        error(f"'{args.repo}' is not OWNER/REPO")
        raise SystemExit(2)

    if args.no_rules and args.rule:
        error("--no-rules and --rule are contradictory")
        raise SystemExit(2)

    secret_specs = _validate_secret_specs(args.secret)
    credential_specs = _validate_credential_specs(args.credential)
    _reject_fleet_credentials_under_secret(secret_specs)
    _validate_app_slugs(args.app)

    gh.require_gh()

    repo = args.repo
    repo_owner = repo.split("/", 1)[0]
    checks = args.rule if args.rule else list(rules.DEFAULT_CHECKS)

    # Independent of every step below and of --no-rules/--dry-run: worth
    # flagging on its own, and read-only, so it always runs exactly once.
    rules.check_master_branch(repo)

    # Always on, like the master-branch check: the fleet credentials have
    # one right place each, so there is nothing to request -- this step
    # reads where they are and plans the difference. It reads before the
    # early return below so that a run requesting nothing else still
    # closes a gap it found; a repository already in shape costs the reads
    # and nothing more.
    _progress(args, f"{repo}: checking fleet credentials")
    credentials_plan = _plan_credentials(repo, credential_specs)
    credentials_idle = not (credentials_plan.moves or credentials_plan.unfixed or credentials_plan.failed)
    _progress(args, f"{repo}: checking auto-merge")
    auto_merge_state, auto_merge_lines = _plan_auto_merge(repo)
    _progress(args, f"{repo}: checking delete-branch-on-merge")
    delete_branch_state, delete_branch_lines = _plan_delete_branch_on_merge(repo)

    # Always on, like credentials and auto-merge: there is nothing to
    # request here either, only one right state (the fleet's scaffold
    # files present) and a diff against it. Unlike those two, --no-
    # bootstrap can skip it outright -- a repository can have a genuine
    # reason to carry no CI at all, and unlike a credential or a setting,
    # there's no way to represent "leave this alone" other than not
    # running the step.
    bootstrap_plan = None
    bootstrap_default_branch = None
    if not args.no_bootstrap:
        _progress(args, f"{repo}: checking the fleet CI scaffold")
        bootstrap_default_branch = _bootstrap_default_branch(repo)
        if bootstrap_default_branch is None:
            bootstrap_plan = scaffold.GapPlan(error=True)
        else:
            bootstrap_plan = scaffold.plan_gaps(repo, bootstrap_default_branch)
    bootstrap_idle = args.no_bootstrap or (not bootstrap_plan.error and not bootstrap_plan.missing)

    would_skip_everything = (
        args.no_rules
        and not secret_specs
        and not args.app
        # A supplied credential is never dropped silently: with nothing to
        # move it still shows the "not set" line saying why (Codex,
        # mikelward/repo#13).
        and not credential_specs
        and credentials_idle
        and auto_merge_state == "allowed"
        and delete_branch_state == "allowed"
        and bootstrap_idle
    )
    if would_skip_everything and bootstrap_plan is not None and not bootstrap_plan.error:
        # About to report "nothing to do" on bootstrap_idle's word alone --
        # but that reflects plan_gaps's own read from a moment ago, and
        # this path returns before the Apply section's own rename recheck
        # ever runs, so an administrative rename since then would exit 0
        # having never inspected the new default branch (Codex review,
        # mikelward/repo#14). Re-verify here too; a mismatch or failed
        # read falls through into the normal flow as a bootstrap failure
        # instead of exiting early.
        current_default_branch = _bootstrap_default_branch(repo)
        if current_default_branch is None:
            bootstrap_plan = scaffold.GapPlan(error=True)  # already reported
            would_skip_everything = False
        elif current_default_branch != bootstrap_default_branch:
            error(
                f"{repo}: default branch changed from '{bootstrap_default_branch}' to "
                f"'{current_default_branch}' while this was waiting; refusing to report "
                "nothing to do. Rerun to gap-fill the real default branch."
            )
            bootstrap_plan = scaffold.GapPlan(error=True)
            would_skip_everything = False
        elif scaffold.apply_gaps(repo, bootstrap_default_branch, bootstrap_plan) is None:
            # Same branch name, but has ITS TIP moved -- a concurrent
            # push could have deleted or replaced a scaffold file since
            # plan_gaps read the tree (Codex review, mikelward/repo#14).
            # bootstrap_idle guarantees plan.missing is empty here, so
            # this call only re-verifies the tip; it writes nothing.
            # apply_gaps already reported why it failed.
            bootstrap_plan = scaffold.GapPlan(error=True)
            would_skip_everything = False

    if would_skip_everything:
        # Nothing requested actually mutates anything -- no ruleset step,
        # no secrets, no apps -- so there's nothing to build a plan for or
        # confirm. Codex review: reaching the confirmation gate below for
        # an empty request meant a non-interactive, no-force invocation of
        # e.g. `repo setup --no-rules OWNER/REPO` (a legitimate way to run
        # just the master-branch check across a fleet) refused outright
        # ("stdin is not a terminal") over a question with no actual
        # mutation behind it to confirm.
        return 0

    # ---- Preview: every step's own dry-run/plan, before anything is shown ----

    ruleset_lines = []
    ruleset_preview_failed = False
    # Set instead of ruleset_preview_failed when the ONLY reason the
    # preview refused is the never-reported-check guard: the checks this
    # run's own bootstrap step is about to push haven't run yet, which
    # isn't a problem with the request -- it's the ordinary state of a
    # repository that has never had CI run against it. That's recoverable
    # by waiting, not by anything this run can fix now, so it skips just
    # the ruleset step (below) instead of refusing to apply anything at
    # all -- everything else this run CAN finish, it does.
    ruleset_never_reported = None
    ruleset_report = {}
    if not args.no_rules:
        _progress(args, f"{repo}: checking rules")
        buf = io.StringIO()
        with redirect_stdout(buf):
            # force=args.force here, NOT hardcoded True: this is the call
            # that decides whether apply_ruleset's never-reported-check
            # guard blocks outright or merely warns, and only an EXPLICIT
            # --force may waive that guard -- not this function's own
            # combined-plan confirmation, whether that comes from a "yes"
            # or from --force. (The real apply below passes force=args.force
            # too, not a hardcoded True, for the same reason -- see
            # skip_confirm on that call for how it avoids trying to
            # re-confirm from stdin a second time instead. Gating the
            # never-reported guard specifically on args.force here, up
            # front -- before the single confirmation is ever offered --
            # gets the same "only --force overrides it" outcome without
            # that second read. Codex review.)
            code = rules.apply_ruleset(repo, checks, dry_run=True, force=args.force, report=ruleset_report)
        if code == 2:
            # A usage error (an empty or control-character check/ruleset
            # name), validated by apply_ruleset before any gh call it
            # makes -- propagate directly rather than folding it into
            # "the preview failed", which would misreport a usage problem
            # as a remote-state one.
            raise SystemExit(2)
        ruleset_lines = buf.getvalue().splitlines()
        if code != 0:
            ruleset_never_reported = ruleset_report.get("never_reported")
            if not ruleset_never_reported:
                ruleset_preview_failed = True

    # --no-bootstrap means bootstrap_plan stays None, so nothing above has
    # checked whether the branch has any commits -- needed here because a
    # ruleset that first requires pull requests would strand a branch with
    # none (no direct push could create it, no PR could target it). Computed
    # during planning, not the Apply section, so --dry-run previews it too;
    # and independent of the preview's own introduces_pr_protection, which
    # can go stale by the time the real write happens (Codex review,
    # mikelward/repo#14).
    empty_branch_would_be_stranded = False
    if bootstrap_plan is None and not args.no_rules:
        no_bootstrap_default_branch = _bootstrap_default_branch(repo)
        if no_bootstrap_default_branch is None:
            empty_branch_would_be_stranded = True
        else:
            ok, ref_raw = gh.try_run(
                # Singular git/ref/... -- see scaffold.push_initial_commit's
                # own comment; plural git/refs/... has no GET route at all
                # and would 404 a populated branch here too (Codex review,
                # mikelward/repo#14).
                ["api", f"repos/{repo}/git/ref/{scaffold._branch_ref_path(no_bootstrap_default_branch)}"]
            )
            if not ok:
                if not scaffold._ref_missing_commits(ref_raw):
                    error_lines(
                        f"could not check whether {repo}'s '{no_bootstrap_default_branch}' branch "
                        "has any commits yet:",
                        ref_raw,
                    )
                empty_branch_would_be_stranded = True
    empty_branch_would_strand_ruleset = (
        not args.no_rules and empty_branch_would_be_stranded and ruleset_report.get("introduces_pr_protection")
    )

    secret_previews = []  # (SecretSpec, (repo, state, env_state), description lines)
    secrets_preview_failed = False
    if secret_specs:
        _progress(args, f"{repo}: checking secrets")
    for spec in secret_specs:
        plan = secrets_cmd._build_plan([repo], spec.name, spec.env)
        entry = plan[0]
        if entry[1] == "error":
            secrets_preview_failed = True
        secret_previews.append((spec, entry, secrets_cmd._describe_plan(spec.name, spec.env, plan)))

    if args.app:
        _progress(args, f"{repo}: checking App installation membership")
    app_plans = [apps.plan_app_step(repo, repo_owner, slug) for slug in args.app]
    app_plan_has_error = any(p.verdict == "ERROR" for p in app_plans)

    def describe_combined_plan():
        lines = [f"{repo}:"]
        if not args.no_rules:
            lines.append("  ruleset (repo-rules):")
            lines += [f"    {line}" for line in ruleset_lines]
            if empty_branch_would_strand_ruleset:
                lines.append(
                    "    SKIPPED: would strand this repository -- its branch has no commits "
                    "yet and --no-bootstrap means nothing here will add one"
                )
            if ruleset_never_reported:
                lines.append(
                    f"    SKIPPED: {rules.describe_missing(ruleset_never_reported)} never "
                    "reported on this repo yet; rerun once they have (--force adds the "
                    "ruleset anyway, blocking every merge until then)"
                )
        if secret_previews:
            lines.append("  secrets (repo-secrets):")
            for spec, _entry, desc_lines in secret_previews:
                lines.append(f"    --name {_secret_label(spec)}:")
                lines += [f"      {line}" for line in desc_lines]
        if app_plans:
            lines.append("  App installation membership:")
            lines += [f"    {line}" for line in apps.describe_plan(repo, app_plans)]
        lines.append("  fleet credentials:")
        lines += [f"    {line}" for line in credentials_plan.lines]
        lines += [f"    NOT FIXED: {reason}" for reason in credentials_plan.unfixed]
        if not credentials_plan.lines and not credentials_plan.unfixed:
            lines.append("    nothing to do")
        lines.append("  auto-merge:")
        lines += [f"    {line}" for line in auto_merge_lines]
        lines.append("  delete-branch-on-merge:")
        lines += [f"    {line}" for line in delete_branch_lines]
        if not args.no_bootstrap:
            lines.append("  bootstrap (fleet CI scaffold):")
            lines += [f"    {line}" for line in scaffold.describe_gap_plan(bootstrap_plan)]
        return lines

    if args.dry_run:
        for line in describe_combined_plan():
            print(line)
        # A NOT FIXED line exits 1 here too: the real run would, and the
        # dry run's exit status is a preview of that as much as of the
        # plan's text.
        if (
            ruleset_preview_failed
            or secrets_preview_failed
            or app_plan_has_error
            or credentials_plan.failed
            or credentials_plan.unfixed
            or auto_merge_state == "error"
            or delete_branch_state == "error"
            or (bootstrap_plan is not None and bootstrap_plan.error)
            or (bootstrap_plan is not None and bootstrap_plan.missing_workflow_scope)
            or empty_branch_would_strand_ruleset
            or ruleset_never_reported
        ):
            raise SystemExit(1)
        return 0

    if ruleset_preview_failed or secrets_preview_failed:
        # App-plan errors do NOT gate this: an App-plan ERROR is a genuine
        # per-step runtime outcome (no installation found, a listing call
        # that failed), not a usage-shaped problem with the whole plan --
        # it's exactly the kind of independent step failure "every step is
        # attempted regardless of an earlier one" exists to let the other
        # steps proceed past, and apply_step already reports it as a
        # failure the normal way.
        for line in describe_combined_plan():
            error(line)
        error(f"the preview above failed; nothing was changed on {repo}.")
        raise SystemExit(1)

    # Codex review: whether there is anything for the confirmation gate
    # below to even ask about. A --secret step never counts as a no-op --
    # secrets_cmd.py's own design note explains why: GitHub never returns
    # a secret's value, so "does the existing one already match" is
    # unanswerable, and every write is attempted every time. The ruleset
    # step's no-op-ness comes from ruleset_report (structured data
    # apply_ruleset computed and handed back directly), NOT from
    # substring-searching ruleset_lines for NO_OP_MESSAGE -- Codex review:
    # that printed text also contains every requested check name, so a
    # --rule value that happened to equal (or contain) NO_OP_MESSAGE's own
    # text would satisfy the substring search even in a genuine "would
    # create/update" plan, skipping confirmation for a real write.
    # ruleset_report only has "needs_write" once the preview has actually
    # succeeded that far, hence the default True -- fail toward asking
    # rather than silently skipping if it's ever somehow missing. An App
    # step is a no-op unless its verdict is ADD; an ERROR verdict needs no
    # confirmation either, since apply_step reports it as a failure
    # without ever attempting a write, confirmed or not. ruleset_never_
    # reported is excluded outright: the Apply section below skips the
    # step entirely rather than writing anything, so there is nothing
    # for a confirmation to be about.
    # needs_write is not the whole of what the ruleset step mutates: it
    # also deletes a legacy-named ruleset that is identical to the one
    # this run leaves behind, and the steady state that happens in is an
    # already-correct ruleset -- so a gate reading needs_write alone would
    # apply that deletion with nothing ever asked or shown.
    ruleset_needs_mutation = (
        (not args.no_rules)
        and not ruleset_never_reported
        and (ruleset_report.get("needs_write", True) or bool(ruleset_report.get("deletions")))
    )
    apps_need_mutation = any(p.verdict == "ADD" for p in app_plans)
    needs_confirmation = (
        ruleset_needs_mutation
        or bool(secret_previews)
        or apps_need_mutation
        or bool(credentials_plan.moves)
        or auto_merge_state == "enable"
        or delete_branch_state == "enable"
        or (
            bootstrap_plan is not None
            and not bootstrap_plan.error
            and not bootstrap_plan.missing_workflow_scope
            and bool(bootstrap_plan.missing)
        )
    )

    # Shown under --verbose unconditionally -- the full audit trail
    # (notably any secret's own OVERWRITES-an-existing-value warning),
    # even under --force and even when nothing needs confirming. Without
    # --verbose it's shown only when there's an actual question to answer
    # below (needs_confirmation and not args.force): the user needs to see
    # what they're agreeing to, or -- stdin not a terminal -- what this
    # refused to apply unconfirmed. A --force run with nothing to confirm
    # (the common case running this over a whole fleet) stays quiet here;
    # Apply, below, still prints whatever it actually changes.
    show_plan = args.verbose or (needs_confirmation and not args.force)
    if show_plan:
        for line in describe_combined_plan():
            error(line)

    if needs_confirmation and not args.force:
        if not sys.stdin.isatty():
            error("stdin is not a terminal and --force was not given, so nothing was")
            error(f"changed on {repo} rather than either blocking on a question nobody")
            error("can answer or silently applying an unconfirmed change. Pass --force")
            error("to apply non-interactively, or run this from a terminal.")
            raise SystemExit(1)
        print(f"Apply this to {repo}? [y/N] ", file=sys.stderr, end="")
        try:
            answer = input()
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            error(f"not confirmed; nothing was changed on {repo}.")
            raise SystemExit(1)

    # ---- Apply ------------------------------------------------------------

    failed = []

    # Runs FIRST, before the ruleset step: apply_gaps writes straight to
    # the branch (a plain, non-force ref update -- see its own docstring),
    # and a ruleset requiring pull requests blocks exactly that kind of
    # direct push for any caller that isn't a configured bypass actor --
    # which this tool's own ruleset step never configures one to be. Apply
    # the scaffold while the branch is still unprotected (a fresh
    # repository's first-ever `repo setup --force`) or before this run's
    # own ruleset write takes effect, rather than after. This does not
    # cover a repository a PRIOR run already protected: apply_gaps's own
    # ref-update failure there is exactly this rejection, reported as
    # whatever GitHub's own error says rather than guessed at (Codex
    # review, mikelward/repo#14) -- there is no direct-push path once
    # protection is active, and adding one (via a pull request instead of
    # a ref update) is follow-up work, not this step's job today.
    bootstrap_failed = False
    # The branch's tip right after the bootstrap step verified or wrote
    # it -- carried into the ruleset gate below so it can re-check the
    # scaffold is still intact right before activating protection, not
    # just whether bootstrap itself succeeded (Codex review,
    # mikelward/repo#14).
    bootstrap_completed_sha = None
    if bootstrap_plan is not None:
        if bootstrap_plan.error:
            # Already reported (either the default-branch read's own
            # failure, or plan_gaps's) when the plan was built.
            failed.append("bootstrap")
            bootstrap_failed = True
        elif bootstrap_plan.missing_workflow_scope:
            # Same shape as ruleset.py's never_reported skip below:
            # nothing is wrong with the repository or the request, only
            # with what this gh token may write, and that's recoverable
            # by the caller (add the scope, rerun) -- not something to
            # attempt and watch fail, or to let block everything else
            # this run could otherwise still do (mikelward/repo#18).
            workflow_count = sum(
                1 for path in bootstrap_plan.missing if path.startswith(".github/workflows/")
            )
            error(
                f"{repo}: skipping the bootstrap step -- this gh token is missing the 'workflow' "
                f"OAuth scope, needed to add {workflow_count} file(s) under "
                ".github/workflows/. Run `gh auth refresh -s workflow` (or add the scope your "
                "token's own way) and rerun; nothing under .github/workflows/ was touched, and "
                "this run's other steps still ran."
            )
            failed.append("bootstrap")
            bootstrap_failed = True
        else:
            # Re-verify the default branch is still the one plan_gaps
            # built this plan against, whether or not anything was
            # missing -- an administrative rename (repo settings, not a
            # push) during the wait must not let a no-op plan ("the old
            # branch was already complete") silently skip inspecting the
            # new one, which the ruleset step below would then go on to
            # protect while it's still wholly unscaffolded (Codex review,
            # mikelward/repo#14).
            current_default_branch = _bootstrap_default_branch(repo)
            if current_default_branch is None:
                failed.append("bootstrap")
                bootstrap_failed = True
            elif current_default_branch != bootstrap_default_branch:
                error(
                    f"{repo}: default branch changed from '{bootstrap_default_branch}' to "
                    f"'{current_default_branch}' while this was waiting; refusing to add the "
                    "scaffold to a branch that's no longer current. Rerun to gap-fill the real "
                    "default branch."
                )
                failed.append("bootstrap")
                bootstrap_failed = True
            else:
                # apply_gaps itself re-verifies the tip before reporting
                # success either way -- write or, for a no-op plan, just
                # the recheck -- and returns it, so nothing further is
                # needed here beyond capturing what it returned.
                bootstrap_completed_sha = scaffold.apply_gaps(repo, bootstrap_default_branch, bootstrap_plan)
                if bootstrap_completed_sha is None:
                    failed.append("bootstrap")
                    bootstrap_failed = True
                elif bootstrap_plan.missing:
                    print(f"{repo}: added {len(bootstrap_plan.missing)} fleet CI scaffold file(s)")

    # empty_branch_would_be_stranded (the --no-bootstrap-on-an-empty-branch
    # case) was already computed above during planning, not here -- see its
    # own comment there for why. bootstrap_failed, above, is the only thing
    # this Apply section still needs to determine fresh: a real write's
    # success or failure genuinely can't be known ahead of attempting it.

    # Blocks the ruleset step only when its write would be the one that
    # FIRST makes the branch require a pull request -- from the existing
    # ruleset's own rules (ruleset_report["introduces_pr_protection"]), not
    # merely whether it already exists, since an update that ADDS
    # pull_request to a ruleset that didn't have it is just as dangerous as
    # creating one fresh (per TODO.md, apply_gaps has no direct-push path
    # past that protection once it's active). An update that doesn't
    # introduce it is let through regardless -- unchanged exposure either
    # way. empty_branch_would_strand_ruleset (planning, above) is the same
    # guard from the other direction: --no-bootstrap on an empty branch
    # rather than a failed one (Codex review, mikelward/repo#14). A
    # concurrent push moving the scaffold branch after bootstrap finished
    # (or an administrator changing the default branch itself) is a
    # separate, later check -- verify_scaffold_before_introducing_pr_
    # protection, passed to the real apply_ruleset call below, since only
    # ITS fresh recompute knows the CURRENT default branch and the CURRENT
    # (not preview-snapshot) answer to whether this write introduces
    # protection (Codex review, mikelward/repo#14).
    if not args.no_rules and (
        (bootstrap_failed and ruleset_report.get("introduces_pr_protection"))
        or empty_branch_would_strand_ruleset
    ):
        if bootstrap_failed:
            error(
                f"{repo}: skipping the ruleset step -- it would make its branch require a pull "
                "request for the first time, and the bootstrap step that must land first (see "
                "above) failed. Activating pull-request protection now would leave this repository "
                "permanently missing the fleet CI scaffold (see TODO.md). Fix the bootstrap failure "
                "and rerun."
            )
        else:
            error(
                f"{repo}: skipping the ruleset step -- it would make its branch require a pull "
                "request for the first time, and the branch has no commits yet (--no-bootstrap "
                "means nothing here will add one). Activating pull-request protection now would "
                "permanently strand the repository: no direct push could create the branch, and no "
                "pull request can target one that doesn't exist yet to use as a base. Push an "
                "initial commit by hand first (or drop --no-bootstrap so this scaffolds one), then "
                "rerun."
            )
        failed.append("ruleset")
    elif not args.no_rules and ruleset_never_reported:
        # The chicken-and-egg case: a repository whose required checks
        # have never run has no way to satisfy them by the time this
        # ruleset would take effect, so creating it now would just block
        # every future merge. Nothing here is broken -- everything else
        # this run could do (bootstrap included) already happened above
        # -- so this skips only the ruleset step rather than the whole
        # run refusing to change anything, and says what unblocks it.
        error(
            f"{repo}: skipping the ruleset step -- {rules.describe_missing(ruleset_never_reported)} "
            "never reported on this repo yet, so requiring them now would block every merge with no "
            "way to satisfy it. Rerun once they have (e.g. once this run's own scaffold push above "
            "triggers them, or a pull request does) -- or pass --force to add the ruleset anyway."
        )
        failed.append("ruleset")
    elif not args.no_rules:
        # expected_fingerprint carries forward what the preview call
        # decided and showed -- which ruleset (or none) and the exact body
        # it would write -- so this refuses rather than silently acting on
        # something that was created, deleted, replaced, or edited under
        # the same name while this was waiting on the confirmation above.
        # A bare subscript, not .get(): the preview always sets
        # ruleset_report["fingerprint"] before returning 0, and a failed
        # preview (code != 0) already raised SystemExit above via
        # ruleset_preview_failed, so this branch is only ever reached with
        # it present. A missing key here would be a real bug in this
        # file, and a KeyError is a more honest signal of that than
        # papering over it with a default that -- unlike apply_ruleset's
        # own _NO_EXPECTATION sentinel -- would just make every real
        # apply refuse. See apply_ruleset's own doc.
        #
        # force=args.force, skip_confirm=True (not force=True): Codex
        # review -- this call must never re-prompt (the confirmation above
        # already spent stdin), but that is a DIFFERENT thing from the user
        # having authorized overriding apply_ruleset's own never-reported-
        # check guard. A hardcoded force=True conflated the two, so a check
        # that was reporting when the preview ran but had fallen out of the
        # reported set by the time THIS call made its own fresh scan (the
        # default branch advancing to a not-yet-reported commit in that
        # window, say) got silently downgraded from "block" to "warn and
        # proceed" even without --force. skip_confirm carries the "don't
        # re-prompt" half on its own, unconditionally; force carries only
        # what the user actually passed, so the guard still blocks unless
        # they explicitly authorized overriding it. See apply_ruleset's own
        # doc for both parameters.
        def _verify_scaffold_still_current(fresh_default_branch):
            """Called by apply_ruleset itself, only when its OWN fresh
            recompute says this write would introduce pull-request
            protection -- so fresh_default_branch is the CURRENT default
            branch, not bootstrap_default_branch, which an administrator
            could have repointed at an unscaffolded branch since bootstrap
            ran (Codex review, mikelward/repo#14)."""
            if fresh_default_branch != bootstrap_default_branch:
                return (
                    f"{repo}: default branch is now '{fresh_default_branch}', not the "
                    f"'{bootstrap_default_branch}' this run's bootstrap step scaffolded -- "
                    "refusing to activate pull-request protection on a branch whose scaffold "
                    "state is unknown. Rerun to check it."
                )
            current_sha = scaffold._recheck_branch_sha(repo, fresh_default_branch)
            if current_sha is None or current_sha != bootstrap_completed_sha:
                return (
                    f"{repo}: '{fresh_default_branch}' changed after the bootstrap step finished "
                    "verifying the scaffold (a concurrent push). Refusing to activate "
                    "pull-request protection over what might now be an incomplete scaffold "
                    "(see TODO.md). Rerun to check its current state."
                )
            return None

        if (
            rules.apply_ruleset(
                repo,
                checks,
                dry_run=False,
                force=args.force,
                expected_fingerprint=ruleset_report["fingerprint"],
                skip_confirm=True,
                # The gate above uses the PREVIEW's introduces_pr_protection,
                # a snapshot; passing this through has apply_ruleset's own
                # fresh recompute (right before the real write) refuse if it
                # only became true during the confirmation wait -- covering
                # both bootstrap_failed and the --no-bootstrap-on-an-empty-
                # branch case, which never sets bootstrap_failed on its own
                # (Codex review, mikelward/repo#14).
                refuse_if_introduces_pr_protection=bootstrap_failed or empty_branch_would_be_stranded,
                # Only meaningful once bootstrap has actually verified a
                # tip to compare against -- bootstrap_failed above already
                # blocks the case where it hasn't (Codex review,
                # mikelward/repo#14).
                verify_scaffold_before_introducing_pr_protection=(
                    _verify_scaffold_still_current if bootstrap_completed_sha is not None else None
                ),
                # Nothing needing a write here is itself "what changed",
                # so suppress its own no-op report under the same default
                # that suppresses the plan dump above -- see _progress's
                # docstring for the quiet/verbose split.
                quiet=not args.verbose,
            )
            != 0
        ):
            failed.append("ruleset")

    for spec, entry, _desc_lines in secret_previews:
        _repo, state, env_state = entry
        if state == "error":
            # Already reported when the plan was built; carried through so
            # the final summary names every secret nothing happened to.
            failed.append(f"secret:{spec.name}")
            continue
        if state == "new":
            # The confirmation prompt (or an earlier step's own duration)
            # can sit for an arbitrary amount of real time; re-verify
            # immediately before writing that nobody else created this
            # secret in the meantime, since a value only ever confirmed as
            # CREATING must never be silently overwritten instead.
            recheck = secrets_cmd._recheck_still_absent(repo, spec.name, spec.env)
            if recheck == "now_exists":
                error(
                    f"'{spec.name}' on {repo}{f' (environment {spec.env})' if spec.env else ''} "
                    "was created by"
                )
                error("someone else since the plan was built; refusing to overwrite")
                error("it without the confirmation that was given for a different,")
                error("now-stale plan.")
                failed.append(f"secret:{spec.name}")
                continue
            if recheck == "error":
                failed.append(f"secret:{spec.name}")
                continue
        if spec.env and env_state == "missing":
            if not secrets_cmd._ensure_environment(repo, spec.env):
                failed.append(f"secret:{spec.name}")
                continue
        # spec.value is the snapshot taken before the plan was even built,
        # not a fresh read of spec.path -- see _validate_secret_specs.
        if not secrets_cmd._write_secret(repo, spec.name, spec.env, spec.value):
            failed.append(f"secret:{spec.name}")

    for plan in app_plans:
        if not apps.apply_step(repo, repo_owner, plan):
            failed.append(f"app:{plan.slug}")

    for move in credentials_plan.moves:
        # The caller is re-read right before the move -- same reason
        # --secret rechecks before its write: the confirmation prompt above
        # can have sat for any length of time. Before the writes as well as
        # the deletes: a caller that went back to naming its secrets makes
        # the environment copy idle, not only the delete unsafe, and a move
        # with nothing to delete used to skip the recheck altogether and
        # report that idle write as success (Codex, mikelward/repo#13).
        reason = move.recheck()
        if reason is not None:
            for name, _env, _value, _env_exists in move.writes:
                error(f"{name} not set: {reason}")
            for name, env in move.deletes:
                error(f"{name} kept: {reason}")
            if move.restrict is not None:
                error(f"environment '{move.restrict[0]}' not restricted: {reason}")
            failed.append(f"credential:{move.label}")
            continue
        # No absent-then-created recheck for these writes, unlike --secret's:
        # a --credential value is "what this name must hold", so a copy
        # someone else set in the meantime is overwritten on purpose, and
        # the plan already said OVERWRITES where one was there to begin
        # with. _ensure_environment does its own recheck.
        # Environments this run has just restricted. `env_exists` below is
        # a plan-time snapshot, so without this the writes re-ran
        # `_ensure_environment` on the environment the restriction had
        # just shut -- and a deletion in that window is a 404 there, which
        # CREATES it again with GitHub's default open policy. The pair
        # then went in and the repository copies came out, into an
        # environment any branch can enter: the run closed the door,
        # somebody removed the door, and the run rebuilt it open and put
        # the credential behind it, reporting success (Codex,
        # mikelward/repo#36). A restriction that landed is what says the
        # environment exists; a deletion after it makes the write fail
        # rather than recreate.
        established = set()
        if move.restrict is not None:
            env, default = move.restrict
            # BEFORE the writes and the deletes, not between them. A
            # restriction that fails after the pair is written leaves the
            # credential in an environment any branch can enter -- the
            # exposure this placement exists to close, created by the run
            # that was closing it -- and the deletes then ran anyway, since
            # a failed restriction only recorded a failure and fell
            # through (Codex, mikelward/repo#36). Shut the door first and
            # nothing goes through it: on any failure here the writes and
            # deletes are held back and said to be.
            #
            # The environment has to exist to be restricted, and creating
            # an empty one costs nothing if the restriction then fails.
            if any(not exists for _n, into, _v, exists in move.writes if into == env):
                if not secrets_cmd._ensure_environment(repo, env):
                    for name, _e, _v, _x in move.writes:
                        error(f"{name} not set: environment '{env}' could not be created")
                    failed.append(f"credential:{move.label}")
                    continue
            held = None
            try:
                print(f"{repo}: {credentials.restrict_environment(repo, env, default)}")
            except credentials.RestrictRefused as e:
                error(f"not fixed: {move.label}: {e}")
                held = "the environment was not restricted"
            except credentials.ReadError as e:
                error_lines(e.message, e.detail)
                held = "the environment was not restricted"
            except gh.GhError as e:
                error_lines(f"could not restrict environment '{env}' on {repo}:", e.stderr)
                held = "the environment was not restricted"
            if held is not None:
                for name, _e, _v, _x in move.writes:
                    error(f"{name} not set: {held}")
                for name, _e in move.deletes:
                    error(f"{name} kept: {held}")
                failed.append(f"credential:{move.label}")
                continue
            established.add(env)
        # Which of the names about to be written the environment does not
        # hold yet, read now rather than taken from the plan. A half that
        # lands while its partner's write fails SHADOWS the repository copy
        # of that name for every job declaring the environment, so the job
        # authenticates with one new half and one old one and cannot start
        # -- a pair that worked before the run, broken by it (Codex,
        # mikelward/repo#36). Only a half this run created is rolled back:
        # one it overwrote holds a value the run does not have, and
        # deleting that would lose it.
        creating = set()
        unreadable = None
        into = {env for _n, env, _v, _x in move.writes if env}
        for env in into:
            try:
                _listed, held_now = credentials.environment_secrets(
                    repo, credentials.environments(repo), env
                )
            except credentials.ReadError as e:
                unreadable = (env, e)
                break
            creating |= {
                (name, env) for name, at, _v, _x in move.writes if at == env and name not in held_now
            }
        if unreadable is not None:
            # Without this reading the rollback cannot run, and a write
            # that lands while its partner fails then shadows a working
            # repository half with no way back -- so the writes do not
            # start (Codex, mikelward/repo#36). Carrying on with an empty
            # inventory made the read failure silently disable the very
            # thing that keeps a half-written pair from breaking the
            # publisher.
            env, e = unreadable
            error_lines(
                f"{move.label}: not moved -- environment '{env}' could not be read, so a write that "
                f"landed while its partner failed could not be undone:",
                e.detail,
            )
            for name, _e, _v, _x in move.writes:
                error(f"{name} not set: the environment's secrets could not be read first")
            for name, _e in move.deletes:
                error(f"{name} kept: the environment's secrets could not be read first")
            failed.append(f"credential:{move.label}")
            continue
        written = True
        created = []
        overwrote = []
        # The pair is written as a unit: the first failure stops the rest.
        # Carrying on wrote the SECOND half over a working pair after the
        # first had already failed -- turning a run that could have left
        # the credential untouched into one that broke it, and in the one
        # direction (an overwrite) nothing can undo (Codex,
        # mikelward/repo#36). Stopping costs a rerun; continuing costs the
        # publisher.
        for name, env, value, env_exists in move.writes:
            if not env_exists and env not in established and not secrets_cmd._ensure_environment(repo, env):
                written = False
                failed.append(f"credential:{name}")
                break
            if not secrets_cmd._write_secret(repo, name, env, value):
                written = False
                failed.append(f"credential:{name}")
                break
            if (name, env) in creating:
                created.append((name, env))
            else:
                overwrote.append((name, env))
        if not written:
            for name, env in created:
                try:
                    credentials.delete_secret(repo, name, env)
                except gh.GhError as e:
                    error_lines(
                        f"could not undo the write of '{name}' to {repo} (environment {env}), so "
                        f"it shadows the repository copy while its other half does not:",
                        e.stderr,
                    )
                    continue
                print(
                    f"{repo}: undid the write of '{name}' (environment '{env}') -- its other half "
                    f"failed, and half a pair in the environment shadows the working repository one"
                )
            if overwrote:
                # A rotation overwrites both halves, so neither is in
                # `created` and the rollback above has nothing to undo: the
                # environment is left holding one new half and one old one,
                # and every job declaring it fails to authenticate until
                # both are set (Codex, mikelward/repo#36). Nothing here can
                # put it back -- GitHub never returns a secret's value, so
                # the run holds the new one and not the old -- and refusing
                # every rotation that overwrites would leave no way to
                # rotate at all. So the run says exactly what it left and
                # what settles it, rather than reporting a bare write
                # failure over a credential that is broken as of now.
                for name, env in overwrote:
                    error(
                        f"{name} now holds the new value in environment '{env}' while its other half "
                        f"does not, so the App cannot authenticate until both are set -- re-run with "
                        f"both `--credential` values (the old value is not recoverable: GitHub never "
                        f"returns a secret)"
                    )
            for name, env in move.deletes:
                error(f"{name} kept: the write it waited on failed")
            continue
        if move.shut is not None and (move.writes or move.deletes):
            # The writes' own window, the last one left: everything else
            # about this move is checked before them, so an administrator
            # reopening the environment while the pair was going in had the
            # repository copies deleted behind it and the run exited 0 with
            # the credential reachable from an untrusted branch (Codex,
            # mikelward/repo#36). The deletes are the irreversible half, so
            # the copies stay and the run says why. Nothing is rewritten:
            # the environment is left as whoever changed it left it, and
            # the next run re-plans against what it finds.
            #
            # Asked of every move that WROTE, not only one with copies to
            # delete. A rotation into an environment that already held the
            # pair deletes nothing, so gating on the deletes skipped the
            # check exactly where the run had just put a fresh credential
            # somewhere any branch could read it -- and then said the
            # repository was in shape (Codex, mikelward/repo#36 again).
            # There is nothing to hold back on that path; what the finding
            # buys is the run not claiming otherwise.
            env, default = move.shut
            try:
                reopened = credentials.branch_policy_verdict(
                    credentials.environment_branch_policy(repo, env), default
                )
            except credentials.ReadError as e:
                reopened = f"could not be re-read ({e.message.rstrip(':')})"
            if reopened is not None:
                said = f"environment '{env}' {reopened} after the credential was written"
                # A half this run CREATED is the run's to take back out, and
                # taking it out is what ends the exposure: reporting alone
                # left a credential the operator had just handed in sitting
                # in an environment every branch could now read, which is
                # the whole thing this placement exists to prevent (Codex,
                # mikelward/repo#36). The same inventory the write-failure
                # rollback uses, for the same reason -- and the repository
                # copies are held either way, so undoing the writes leaves
                # the state the run found. A half it OVERWROTE is not
                # undoable: the old value is gone and deleting it would
                # leave no pair at all, so it is named instead.
                for name, at in created:
                    try:
                        credentials.delete_secret(repo, name, at)
                    except gh.GhError as e:
                        error_lines(
                            f"could not undo the write of '{name}' to {repo} (environment {at}), so it "
                            f"stays in an environment that {reopened}:",
                            e.stderr,
                        )
                        continue
                    print(
                        f"{repo}: undid the write of '{name}' (environment '{at}') -- {said}"
                    )
                for name, at in overwrote:
                    error(
                        f"{name} holds the new value in environment '{at}', which {reopened} -- it "
                        f"cannot be taken back out without leaving no credential at all, so restrict "
                        f"the environment by hand or rotate it again once you have"
                    )
                for name, _e in move.deletes:
                    error(f"{name} kept: {said}")
                if not move.deletes:
                    error(f"{move.label}: {said}")
                failed.append(f"credential:{move.label}")
                continue
        for name, env in move.deletes:
            try:
                credentials.delete_secret(repo, name, env)
            except gh.GhError as e:
                error_lines(
                    f"could not delete '{name}' from {repo}{f' (environment {env})' if env else ''}:",
                    e.stderr,
                )
                failed.append(f"credential:{name}")
                continue
            print(f"{repo}: deleted '{name}'" + (f" (environment '{env}')" if env else ""))

    for reason in credentials_plan.unfixed:
        error(f"not fixed: {reason}")
        failed.append(f"credential:{reason.split(':', 1)[0]}")
    if not show_plan:
        # A supplied --credential this run did nothing with -- never
        # gated behind quiet mode; see CredentialsPlan.always_report.
        # Skipped when show_plan already printed all of credentials_plan
        # .lines (a superset), so this doesn't double it.
        for line in credentials_plan.always_report:
            error(line)
    if credentials_plan.failed:
        # The read failure is in credentials_plan.lines -- shown already
        # if show_plan printed the combined plan above, but quiet mode
        # (no --verbose, nothing to confirm) skips that, and this is a
        # genuine failure, not a routine "nothing to report": print it
        # here too rather than leaving "failed on: credentials" with no
        # explanation. Like an App-plan error, it fails this step alone: a
        # ruleset or secret write must not be held back by a listing this
        # step could not get.
        if not show_plan:
            for line in credentials_plan.lines:
                error(line)
        failed.append("credentials")

    if auto_merge_state == "enable":
        try:
            gh.run_with_input(
                ["api", "--method", "PATCH", f"repos/{repo}", "--input", "-"],
                json.dumps({"allow_auto_merge": True}).encode(),
            )
        except gh.GhError as e:
            error_lines(f"could not enable auto-merge on {repo}:", e.stderr)
            failed.append("auto-merge")
        else:
            print(f"{repo}: enabled auto-merge")
    elif auto_merge_state == "error":
        # Same reasoning as credentials_plan.failed just above: the
        # explanation is in auto_merge_lines, already shown if show_plan
        # printed the combined plan, otherwise printed here so a genuine
        # failure is never silent.
        if not show_plan:
            for line in auto_merge_lines:
                error(line)
        failed.append("auto-merge")

    if delete_branch_state == "enable":
        try:
            gh.run_with_input(
                ["api", "--method", "PATCH", f"repos/{repo}", "--input", "-"],
                json.dumps({"delete_branch_on_merge": True}).encode(),
            )
        except gh.GhError as e:
            error_lines(f"could not enable delete-branch-on-merge on {repo}:", e.stderr)
            failed.append("delete-branch-on-merge")
        else:
            print(f"{repo}: enabled delete-branch-on-merge")
    elif delete_branch_state == "error":
        if not show_plan:
            for line in delete_branch_lines:
                error(line)
        failed.append("delete-branch-on-merge")

    if failed:
        error("failed on: " + " ".join(failed))
        raise SystemExit(1)
    return 0
