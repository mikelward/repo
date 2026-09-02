"""`repo setup` -- compose rules, secrets, App membership, fleet credentials,
auto-merge, and the fleet CI scaffold's gaps.

Port of mikelward/scripts's repo-setup (see its own header comment for the
full reasoning; repeated here only where the port changes something). A
thin composer, not a reimplementation: the ruleset step calls rules.py's
apply_ruleset directly and each secret step calls secrets_cmd.py's own
plan/write functions directly -- neither's real logic (the never-reported-
check guard, the confirm/diff machinery, the sealed-box secret write) is
duplicated here. Only the App-installation step is native (see apps.py),
because there is no sibling implementation to port from yet.

Every requested step runs its own dry-run/plan first, and the combined
plan is printed and confirmed ONCE for the whole repository -- not once
per step. --force skips the confirmation, not the plan output. Once
confirmed, each step is applied for real with its own force=True, since
this function's own confirmation already stands in for that step's.

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
`(existing_id, needs_write, target_body)`, the ruleset it's acting on,
whether it would actually write anything, and the exact API body it's
about to send if so -- once per pass, exposes the preview's via
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
from dataclasses import dataclass
from typing import Optional

from repo_lib import apps, credentials, gh, rules, scaffold, secrets_cmd
from repo_lib.common import error, error_lines

# The lookaheads reject `.` and `..` components: made of allowed
# characters, but as path segments spliced into `repos/{repo}/...` they
# would address a different endpoint than the one that was validated.
OWNER_REPO_RE = re.compile(r"^(?!\.\.?/)[A-Za-z0-9._-]+/(?!\.\.?$)[A-Za-z0-9._-]+$")


def add_arguments(parser):
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="apply without confirming")
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


@dataclass
class CredentialsPlan:
    lines: list  # what the combined plan shows, one per action or fact
    moves: list  # CredentialMove, in order
    unfixed: list  # what this run cannot fix, each reason a failure at the end
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
        texts = credentials.workflow_texts(repo)
        held = {
            env: credentials.environment_secrets(repo, environments, env)
            for env in (*credentials.BATCH_HUBS, credentials.COMMIT_ARTIFACT_ENV)
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
                plan.lines.append(f"{label}: {name} not set -- {why}")
        if move.deletes:
            plan.moves.append(move)

    def move(label, names, listed, env_secrets, env, caller_desc, inherits, usable, suggest, recheck):
        at_repo = [name for name in names if name in repo_secrets]
        in_env = [name for name in names if name in env_secrets]
        supplied = [name for name in names if name in given]
        held_after = set(in_env) | set(supplied)
        # A caller that names its secrets is a problem whenever there is
        # something the environment holds or would hold: a value to set, a
        # repository copy to delete, or a credential already sitting there
        # that such a caller can never read.
        if inherits is not True and (supplied or at_repo or usable(set(in_env))):
            how = (
                f"{caller_desc} passes its secrets by name"
                if inherits is False
                else f"{caller_desc} does not call the workflow"
            )
            left = ", ".join(dict.fromkeys(supplied + at_repo))
            plan.unfixed.append(
                f"{label}: {how}, so a credential in the '{env}' environment would never reach "
                f"it -- convert the caller to `secrets: inherit` first"
                + (f"; {left} left as is" if left else "")
            )
            return
        def recheck_all():
            reason = recheck()
            if reason is not None:
                return reason
            # The destination is re-read too: a delete justified by a
            # credential already in the environment has to find it still
            # there, since the confirmation prompt can have sat while an
            # administrator removed it (Codex, mikelward/repo#13). A value
            # supplied on the command line is about to be written, so it
            # counts; the environment's own copy is what has to be re-seen.
            if step.deletes and not usable(set(supplied)):
                _listed, env_now = credentials.environment_secrets(repo, credentials.environments(repo), env)
                if not usable(set(env_now) | set(supplied)):
                    return f"the '{env}' environment no longer holds the credential"
            return None

        step = CredentialMove(label=label, writes=[], deletes=[], recheck=guarded(recheck_all))
        for name in supplied:
            state = "OVERWRITES an existing value" if name in in_env else "new"
            step.writes.append((name, env, given[name].value, listed is not None))
            plan.lines.append(f"{label}: set {name} in environment '{env}' ({state})")
        if usable(held_after):
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
                f"{label}: {', '.join(unread)} mentions {prefix} in a shape this cannot read as a "
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
                    reason = f"{', '.join(sorted(callers_now))} appeared since the plan was built"
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
                    f"({', '.join(sorted(callers_now)) or 'none'} now)"
                )
            naming = sorted(name for name, inherits in callers_now.items() if not inherits)
            if naming:
                return f"{', '.join(naming)} no longer passes `secrets: inherit`"
            return None

        failing = sorted(name for name, inherits in found.items() if not inherits)
        move(
            label,
            names,
            listed,
            env_secrets,
            env,
            ", ".join(failing or sorted(found)),
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
    credentials_plan = _plan_credentials(repo, credential_specs)
    credentials_idle = not (credentials_plan.moves or credentials_plan.unfixed or credentials_plan.failed)
    auto_merge_state, auto_merge_lines = _plan_auto_merge(repo)

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
    ruleset_report = {}
    if not args.no_rules:
        buf = io.StringIO()
        with redirect_stdout(buf):
            # force=args.force here, NOT hardcoded True: this is the call
            # that decides whether apply_ruleset's never-reported-check
            # guard blocks outright or merely warns, and only an EXPLICIT
            # --force may waive that guard -- not this function's own
            # combined-plan confirmation, whether that comes from a "yes"
            # or from --force. (The real apply below is different: it
            # always passes force=True, because by the time it runs, the
            # single confirmation this function already gates on has been
            # given one way or another, and repassing force=args.force
            # there would make apply_ruleset try to confirm a SECOND time
            # via its own _confirm() -- but stdin's one line of input was
            # already consumed by the confirmation just above, so that
            # second read would see nothing left to answer with. Gating
            # the never-reported guard specifically on args.force here, up
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
        ruleset_preview_failed = code != 0

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
    for spec in secret_specs:
        plan = secrets_cmd._build_plan([repo], spec.name, spec.env)
        entry = plan[0]
        if entry[1] == "error":
            secrets_preview_failed = True
        secret_previews.append((spec, entry, secrets_cmd._describe_plan(spec.name, spec.env, plan)))

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
            or (bootstrap_plan is not None and bootstrap_plan.error)
            or empty_branch_would_strand_ruleset
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
    # without ever attempting a write, confirmed or not.
    ruleset_needs_mutation = (not args.no_rules) and ruleset_report.get("needs_write", True)
    apps_need_mutation = any(p.verdict == "ADD" for p in app_plans)
    needs_confirmation = (
        ruleset_needs_mutation
        or bool(secret_previews)
        or apps_need_mutation
        or bool(credentials_plan.moves)
        or auto_merge_state == "enable"
        or (bootstrap_plan is not None and not bootstrap_plan.error and bool(bootstrap_plan.missing))
    )

    # Printed unconditionally -- including under --force, and even when
    # nothing actually needs confirming -- so a forced, unattended run
    # still leaves the full plan (notably any secret's own OVERWRITES-an-
    # existing-value warning) in its own output, not just silently
    # applied. --force (and a no-mutations plan) skip the confirmation
    # *question* below, not this audit trail -- matching secrets_cmd.py's
    # own established convention for exactly this reason. Codex review.
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
        (bootstrap_failed and ruleset_report["introduces_pr_protection"]) or empty_branch_would_strand_ruleset
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
            failed.append(f"credential:{move.label}")
            continue
        # No absent-then-created recheck for these writes, unlike --secret's:
        # a --credential value is "what this name must hold", so a copy
        # someone else set in the meantime is overwritten on purpose, and
        # the plan already said OVERWRITES where one was there to begin
        # with. _ensure_environment does its own recheck.
        written = True
        for name, env, value, env_exists in move.writes:
            if not env_exists and not secrets_cmd._ensure_environment(repo, env):
                written = False
                failed.append(f"credential:{name}")
                continue
            if not secrets_cmd._write_secret(repo, name, env, value):
                written = False
                failed.append(f"credential:{name}")
        if not written:
            for name, env in move.deletes:
                error(f"{name} kept: the write it waited on failed")
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
    if credentials_plan.failed:
        # The read failure is already in the plan above. Like an App-plan
        # error, it fails this step alone: a ruleset or secret write must
        # not be held back by a listing this step could not get.
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
        # Already in the plan above; a step failure of its own, same as a
        # failed credentials read.
        failed.append("auto-merge")

    if failed:
        error("failed on: " + " ".join(failed))
        raise SystemExit(1)
    return 0
