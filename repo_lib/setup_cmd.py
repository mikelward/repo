"""`repo setup` -- compose rules, secrets, and App membership.

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
import re
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from typing import Optional

from repo_lib import apps, gh, rules, secrets_cmd
from repo_lib.common import error

# The lookaheads reject `.` and `..` components: made of allowed
# characters, but as path segments spliced into `repos/{repo}/...` they
# would address a different endpoint than the one that was validated.
OWNER_REPO_RE = re.compile(r"^(?!\.\.?/)[A-Za-z0-9._-]+/(?!\.\.?$)[A-Za-z0-9._-]+$")


def add_arguments(parser):
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="apply without confirming")
    parser.add_argument("--no-rules", action="store_true", help="skip the ruleset step")
    parser.add_argument("--rule", action="append", default=[], help="a required check (repeatable)")
    parser.add_argument(
        "--secret", action="append", default=[], metavar="NAME[@ENV]=PATH", help="repeatable"
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
    _validate_app_slugs(args.app)

    gh.require_gh()

    repo = args.repo
    repo_owner = repo.split("/", 1)[0]
    checks = args.rule if args.rule else list(rules.DEFAULT_CHECKS)

    # Independent of every step below and of --no-rules/--dry-run: worth
    # flagging on its own, and read-only, so it always runs exactly once.
    rules.check_master_branch(repo)

    if args.no_rules and not secret_specs and not args.app:
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
        if secret_previews:
            lines.append("  secrets (repo-secrets):")
            for spec, _entry, desc_lines in secret_previews:
                lines.append(f"    --name {_secret_label(spec)}:")
                lines += [f"      {line}" for line in desc_lines]
        if app_plans:
            lines.append("  App installation membership:")
            lines += [f"    {line}" for line in apps.describe_plan(repo, app_plans)]
        return lines

    if args.dry_run:
        for line in describe_combined_plan():
            print(line)
        if ruleset_preview_failed or secrets_preview_failed or app_plan_has_error:
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
    needs_confirmation = ruleset_needs_mutation or bool(secret_previews) or apps_need_mutation

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

    if not args.no_rules:
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
        if (
            rules.apply_ruleset(
                repo,
                checks,
                dry_run=False,
                force=args.force,
                expected_fingerprint=ruleset_report["fingerprint"],
                skip_confirm=True,
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

    if failed:
        error("failed on: " + " ".join(failed))
        raise SystemExit(1)
    return 0
