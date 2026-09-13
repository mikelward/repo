"""The App-installation-membership step of `repo setup`.

Port of mikelward/scripts's repo-setup's native App-installation logic (see
its own header/inline comments for the full reasoning; repeated here only
where the port changes something). There is no standalone repo-app-* shell
script this was extracted from -- repo-setup's own comment explains why:
"the App-installation step is native, because there is no sibling script
for it yet." This module is the direct port of that native logic, given
its own file (mirroring rules.py's shape) rather than folded into
setup_cmd.py, since setup_cmd.py composes three independent steps and
shouldn't own any one of their internals.

An installation scoped to "all repositories" gives the App access to every
repository forked into that account too, the instant the fork is created,
since forking creates a new repository the grant already covers. An
explicit, reviewable --app membership list is the fix for that gap; a
"selected" installation is the one this module ever writes to.

Every function below returns a plain result (an AppPlan, a bool, or a
(id, selection) pair) rather than raising -- unlike rules.py's
RulesetError, there's no multi-call sequence here whose caller wants one
summary message for "something in this failed"; each of these calls is
already the whole story, and its own error() call already said why.

Cost and reliability: free -- a handful of GitHub REST API calls per App
per repository, well inside the 5,000-authenticated-requests-an-hour
limit. When the App's own key is supplied, coverage and slug read AS the
App instead (see register_app_keys): that path shells out to `openssl` to
sign the JWT -- the standard library has no RSA, and `openssl` is the one
new external tool this needs (a standard-distro binary, the maintainer's
call 2026-09-13; see AGENTS.md). The HTTP call itself is stdlib `urllib`,
not another binary, so the bearer token never lands in a process's argv;
if `openssl` is missing the caller warns and falls back to
`user/installations`.
"""

from dataclasses import dataclass
from typing import Optional

import base64
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from repo_lib import gh
from repo_lib.common import error, error_lines, warn, warn_lines

# The character class this module (and the App-membership PUT endpoint it
# calls) is prepared to handle in a slug -- refusing rather than guessing
# how to URL-encode anything wider, same reasoning as secrets_cmd's
# ENV_NAME_RE.
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class AppPlan:
    """The resolved plan for one --app SLUG against one repository.

    verdict is one of:
      ADD             -- a "selected" installation exists and the repo is
                          not yet a member; install_id names it.
      ALREADY_MEMBER   -- a "selected" installation exists and the repo is
                          already a member.
      ALREADY_ALL      -- an "all repositories" installation exists (and
                          the repo itself was confirmed to exist).
      ERROR            -- could not be determined; already reported via
                          error()/error_lines() at the point of failure.
    """

    slug: str
    verdict: str
    install_id: Optional[str] = None


def _positive_int(value):
    """`value` as a positive int, or None if it is not one. Both id-keyed
    lookups below refuse to splice anything but digits into their --jq
    program, and treat a non-integer / non-positive id (nothing GitHub would
    mint) as "no such App" rather than reaching the API."""
    try:
        numeric = int(str(value))
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


# Every App read in this module goes through `user/installations`, which
# GitHub serves only to a GitHub App USER-TO-SERVER token. The token `gh
# auth login` issues belongs to an OAuth App, and a PAT to no App at all,
# so both get a 403 no matter which scopes they carry -- not a permission
# an operator can grant themselves. Worth saying in full wherever that 403
# surfaces: "403" alone sends people to re-authenticate, which cannot fix
# it (mikelward/repo, maintainer's run 2026-09-12).
APP_TOKEN_HINT = (
    "`user/installations` answers only to a GitHub App user-to-server token. "
    "The token `gh auth login` issues is an OAuth-App token, so GitHub refuses "
    "it whatever its scopes -- re-authenticating with gh will not change this."
)


def is_missing_app_token(stderr):
    """True if `stderr` is GitHub's "not a GitHub App token" 403 rather than
    some other failure of the same read. Matched on the message, since gh
    relays the API's body and not a distinguishable status: a plain 403 can
    also mean a suspended install or a blocked account, which ARE worth
    retrying and must not be reported as the hopeless case."""
    return "authorized to a GitHub App" in (stderr or "")


def installations_readable():
    """(ok, detail) for the one endpoint every read below goes through."""
    return gh.try_run(["api", "user/installations", "--jq", ".total_count"])


# App id -> slug pairings the operator supplies (config `app_logins`), so a
# bound check's status creator can be matched without reading
# `user/installations` -- the endpoint GitHub serves only to a GitHub App
# user-to-server token (see app_slug_for_id / APP_TOKEN_HINT). Populated once
# per run from the config; empty otherwise, in which case resolution falls
# back to the API exactly as before.
_known_slugs = {}


def register_known_slugs(mapping):
    """Replace the operator-supplied App id -> slug pairings. Keys coerce to
    positive ints; a non-positive/non-integer key or an empty slug is dropped
    (config validation already rejects those, so this is only belt-and-braces).
    Call with {} to clear -- one run's pairings must never leak into another."""
    _known_slugs.clear()
    for app_id, slug in (mapping or {}).items():
        numeric = _positive_int(app_id)
        if numeric is not None and slug:
            _known_slugs[numeric] = slug


# App private keys the operator supplies (LANES_APP_ID + LANES_APP_PRIVATE_KEY),
# keyed by numeric App id -> PEM bytes. When the key for an id is held, coverage
# and slug both read AS the App -- a short-lived signed JWT against endpoints that
# answer app auth (`GET /app`, `GET /repos/{owner}/{repo}/installation`) -- instead
# of `user/installations`, the endpoint gh's own tokens are refused (APP_TOKEN_HINT).
# That is what lets a `lanes` binding be established and verified on a plain
# `gh auth login` token. Populated once per run from the supplied credentials and
# cleared on exit, exactly like _known_slugs -- one run's key never leaks into
# another. Unlike the config `app_logins` assertion, a slug read this way is ground
# truth (read from GitHub as the App), so it is trusted for coverage too.
_app_keys = {}
_app_slug_cache = {}  # numeric id -> slug, memoized per run: `GET /app` is repo-independent

GITHUB_API = "https://api.github.com"
# A GitHub App JWT may live at most 10 minutes (GitHub rejects a longer `exp`);
# `iat` is backdated a minute to tolerate clock skew between here and GitHub.
_JWT_SKEW_SECONDS = 60
_JWT_LIFETIME_SECONDS = 540  # 9 minutes, inside the 10-minute cap even with skew


def register_app_keys(mapping):
    """Replace the operator-supplied App id -> private-key PEM pairings. Keys
    coerce to positive ints; a non-positive/non-integer key or an empty value is
    dropped. Call with {} to clear -- one run's key must never leak into another."""
    _app_keys.clear()
    _app_slug_cache.clear()
    for app_id, pem in (mapping or {}).items():
        numeric = _positive_int(app_id)
        if numeric is not None and pem:
            _app_keys[numeric] = pem


def app_jwt_tooling_missing():
    """The external tool the App-JWT path needs (`openssl`, to sign) but that is
    absent -- or [] when it is present. The HTTP call is stdlib `urllib`, so
    `openssl` is the only binary to check. Checked up front when the key is
    supplied, so a run that cannot sign fails once with the reason rather than
    deferring every repository's binding."""
    return [tool for tool in ("openssl",) if shutil.which(tool) is None]


def _b64url(raw):
    """base64url without padding, as JWT requires."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _mint_app_jwt(app_id, pem):
    """A short-lived RS256 JWT signed with the App's private key, authenticating
    AS the App. Signed by shelling out to `openssl` (the standard library has no
    RSA) -- the same shell-out philosophy as calling `gh`, and the reason the App
    step stays PyYAML-only. The key touches disk only as a 0600 temp file removed
    immediately after signing, since `openssl dgst -sign` takes the key as a file,
    not on stdin.

    Every failure becomes gh.GhError -- the can't-tell the callers already
    translate into a deferred step. That includes the OS errors around the temp
    file and launching openssl (an unwritable/full TMPDIR, openssl vanished since
    the preflight): letting an OSError escape would abort the whole fleet run with
    a traceback instead of failing this step alone (Codex, mikelward/repo#65)."""
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {"iat": now - _JWT_SKEW_SECONDS, "exp": now + _JWT_LIFETIME_SECONDS, "iss": str(app_id)},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = header + b"." + payload
    try:
        fd, path = tempfile.mkstemp(prefix="repo-app-key-", suffix=".pem")
    except OSError as e:
        raise gh.GhError(f"could not create a temp file to sign App {app_id}'s JWT: {e}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pem if isinstance(pem, bytes) else pem.encode())
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", path],
            input=signing_input,
            capture_output=True,
        )
    except OSError as e:
        # A write failure, or openssl gone since the preflight (FileNotFoundError
        # is an OSError) -- a can't-tell, not a traceback out of the run.
        raise gh.GhError(f"could not sign App {app_id}'s JWT: {e}")
    finally:
        try:
            os.remove(path)
        except OSError as e:
            # The temp file holds the App's private key; if it cannot be removed,
            # say where so the operator can delete it by hand -- silently reporting
            # success would leave the key on disk with no one the wiser (Codex,
            # mikelward/repo#65). Not fatal: the signing already happened.
            warn(f"could not remove the temporary App key file {path}: {e} -- remove it by hand")
    if proc.returncode != 0:
        raise gh.GhError(
            "could not sign a JWT for App "
            f"{app_id} (is LANES_APP_PRIVATE_KEY a valid PEM private key?): "
            + proc.stderr.decode(errors="replace").strip()
        )
    return (signing_input + b"." + _b64url(proc.stdout)).decode("ascii")


_API_TIMEOUT_SECONDS = 30  # an interactive tool must not hang forever on a dead socket


def _api_get_as_app(jwt, path):
    """`GET {GITHUB_API}/{path}` authenticated as the App, returning (status, body).

    Not `gh` (it injects its own token and offers no App auth) and not `curl` (the
    bearer JWT in a process's argv is readable via `ps`, and it can be exchanged
    for installation tokens with the App's permissions -- Codex, mikelward/repo#65).
    Stdlib `urllib` instead: the Authorization header lives in the request object,
    never on a command line, and it adds no external binary. An HTTP error status
    (404, 500) is a real answer returned as data, not an exception; only a transport
    failure (DNS, TLS, a dropped socket) raises gh.GhError -- the same can't-tell a
    failed gh read is."""
    request = urllib.request.Request(
        f"{GITHUB_API}/{path}",
        method="GET",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mikelward-repo-setup",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_API_TIMEOUT_SECONDS) as response:
            return str(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # HTTPError is a URLError subclass, caught first: a 404/500 is the server's
        # answer, read as data (its body may be empty). Its own read() can also be
        # truncated, so that read is guarded too rather than escaping this boundary.
        try:
            body = e.read().decode("utf-8", errors="replace") if e.fp is not None else ""
        except (OSError, http.client.HTTPException):
            body = ""
        return str(e.code), body
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        # http.client.HTTPException (e.g. IncompleteRead when a successful response
        # is cut short mid-body) is neither URLError nor OSError, so without it a
        # truncated read would escape as a traceback and abort the fleet instead of
        # deferring this step (Codex, mikelward/repo#65).
        raise gh.GhError(f"reading {path} as the App failed: {e}")


def _json_object(body, what):
    """`body` parsed as a JSON object, or gh.GhError. Both a parse error and a
    valid-but-non-object payload (`[]`, `null`, a bare string -- which have no
    `.get`) are can't-tells, not tracebacks out of the run: the callers isolate
    only GhError, so an AttributeError here would abort the whole fleet (Codex,
    mikelward/repo#65)."""
    try:
        data = json.loads(body)
    except ValueError as e:
        raise gh.GhError(f"could not parse {what}: {e}")
    if not isinstance(data, dict):
        raise gh.GhError(f"{what} was not a JSON object: {body[:200]}")
    return data


# A JWT read (sign -> HTTP -> parse) is one bounded operation whose only honest
# outcomes are the answer or "can't tell". Its steps can fail in an open-ended set
# of ways -- an OS error signing, a transport failure, a truncated or non-object
# body, and whatever GitHub or a proxy does next -- and three rounds of review each
# named one more escaping the boundary as its own traceback. The callers isolate
# only gh.GhError, so the boundary catches the CLASS: anything that is not already
# a GhError becomes one here, deleting that whole family of "one more exception
# type" findings rather than chasing each (Codex, mikelward/repo#65; the same
# tradeoff, and the same reasoning, as config.load's broad parse guard). GhError is
# re-raised untouched so the specific, useful messages above survive.
def _cant_tell_as_gherror(what, thunk):
    try:
        return thunk()
    except gh.GhError:
        raise
    except Exception as e:  # noqa: BLE001 -- deliberate boundary; see comment above
        raise gh.GhError(f"{what}: {e}")


def _app_slug_via_jwt(numeric, pem):
    """The App's own slug, read AS the App (`GET /app`). Ground truth, so trusted
    for coverage prediction as well as evidence -- and repo-independent, so memoized
    for the run. Raises gh.GhError (can't-tell) on any read failure, like the
    user/installations path."""
    if numeric in _app_slug_cache:
        return _app_slug_cache[numeric]

    def read():
        status, body = _api_get_as_app(_mint_app_jwt(numeric, pem), "app")
        if status != "200":
            raise gh.GhError(f"reading App {numeric}'s own record returned HTTP {status}: {body[:200]}")
        data = _json_object(body, f"App {numeric}'s own record")
        # `GET /app` authenticated AS this App returns THIS App's record, so its id
        # must be the one we signed for; a mismatch is an untrusted response, not our
        # App's slug. And the slug must be a nonempty string: a missing one would
        # read as "never published" and a wrong-typed/other-App value could match a
        # different `{slug}[bot]` status and bind `lanes` early -- both can't-tells,
        # not ground truth (Codex, mikelward/repo#65).
        if data.get("id") != numeric:
            raise gh.GhError(f"App /app record id {data.get('id')!r} != {numeric}: {body[:200]}")
        slug = data.get("slug")
        if not isinstance(slug, str) or not slug:
            raise gh.GhError(f"App {numeric}'s own record has no valid 'slug': {body[:200]}")
        return slug

    slug = _cant_tell_as_gherror(f"could not read App {numeric}'s slug", read)
    if slug:
        _app_slug_cache[numeric] = slug
    return slug


def _app_covers_repo_via_jwt(numeric, pem, repo):
    """Coverage read AS the App: `GET /repos/{repo}/installation` answers 200 when
    the App can act on `repo` (an "all repositories" install, or a "selected" one
    that includes it -- GitHub resolves that server-side, so no member-list walk),
    and 404 when it cannot. A suspended installation returns 200 but cannot act, so
    `suspended_at` is checked, matching the user/installations path. Any failure
    (any status other than 200/404 included) raises gh.GhError (can't-tell)."""

    def read():
        status, body = _api_get_as_app(_mint_app_jwt(numeric, pem), f"repos/{repo}/installation")
        if status == "404":
            return False
        if status == "200":
            data = _json_object(body, f"{repo}'s App installation")
            # The installation object identifies its App by `app_id`; it must be the
            # one we authenticated as, or the 200 is a stale/other-App response and
            # its coverage says nothing about our App -- a can't-tell, not coverage
            # (Codex, mikelward/repo#65; the same identity check the /app path makes).
            if data.get("app_id") != numeric:
                raise gh.GhError(
                    f"{repo}'s App installation app_id {data.get('app_id')!r} != {numeric}: {body[:200]}"
                )
            # A real installation object always carries `suspended_at` (null when
            # active). Absent, the 200 is not a shape we can trust -- treating a
            # missing field as "not suspended" would bind `lanes` to an App on an
            # unvalidated response and could wedge merges, so it is a can't-tell,
            # not coverage.
            if "suspended_at" not in data:
                raise gh.GhError(
                    f"{repo}'s App installation response has no 'suspended_at' field: {body[:200]}"
                )
            return data["suspended_at"] is None
        raise gh.GhError(f"reading {repo}'s App installation returned HTTP {status}: {body[:200]}")

    return _cant_tell_as_gherror(f"could not read {repo}'s coverage as App {numeric}", read)


def require_installations_readable(slugs):
    """Exit 2 before the run touches anything when `--app` was passed and
    this token can NEVER list App installations.

    `--app` has nothing to fall back on -- listing installations is how a
    slug becomes an installation id -- so with the wrong kind of token it
    fails that step on every repository in the fleet, one repository at a
    time, and no rerun changes that. Said once, up front, instead.

    Only for the token refusal, which is the request being impossible: any
    other failure of the same read may be this read's bad luck, so it is
    reported and the run goes on to make its other progress, the App step
    failing alone (SPEC.md, invariant 1). The `lanes` binding reads this
    endpoint too and is never checked here -- it has other evidence to fall
    back on, so it holds its own step either way.
    """
    ok, detail = installations_readable()
    if ok:
        return
    if is_missing_app_token(detail):
        error_lines(
            f"--app {' '.join(slugs)} needs to list this account's App installations, "
            "and this token may not:",
            detail,
        )
        error(APP_TOKEN_HINT)
        error("Drop --app to run everything else, or supply a GitHub App user token.")
        raise SystemExit(2)
    warn_lines(
        f"could not list this account's App installations, which --app {' '.join(slugs)} "
        "needs (continuing -- the App step reports its own failure):",
        detail,
    )


def app_slug_for_id(owner, app_id, use_known=True):
    """The slug of the GitHub App whose numeric id is `app_id`, as installed
    on `owner`'s account, or None if no installation of it is visible there.

    This answers only "which App is this id", for matching a status's
    `{slug}[bot]` creator back to the bound `integration_id` -- the Statuses
    REST API surfaces the bot user, not the id, so evidence that the bound App
    posted a status is recognized by that login. It is deliberately NOT a
    coverage check: whether the App can still publish to a given repo is a
    separate, current-state question (`app_covers_repo`), kept out of the
    evidence scan so "has it reported" and "can it still publish" don't get
    entangled -- four review rounds of coverage findings came from entangling
    them (Codex, mikelward/repo#52).

    Filters on the target account, not the id alone: `user/installations`
    spans every account the caller can see, and the same App installed on
    another account must not answer here. Lets a gh read failure propagate
    rather than reporting and returning None: the caller must tell "not
    installed here" (a real gap) from "could not read" (can't-tell), and only
    a raised error carries the second. A non-integer / non-positive `app_id`
    returns None without reaching the API."""
    numeric = _positive_int(app_id)
    if numeric is None:
        return None
    if numeric in _app_keys:
        # The App's own key is held, so its slug is read AS the App (`GET /app`) --
        # ground truth, not the operator's config assertion, so it stands ahead of
        # both _known_slugs and the owner-filtered API read, and is trusted even
        # under use_known=False (coverage prediction), unlike a config pairing.
        return _app_slug_via_jwt(numeric, _app_keys[numeric])
    if use_known and numeric in _known_slugs:
        # The operator asserted this id's slug (config `app_logins`), so the
        # match needs no `user/installations` read -- and the assertion is
        # account-agnostic, so it stands ahead of the owner filter below. Only
        # for EVIDENCE (matching a status creator): coverage prediction passes
        # use_known=False, since whether an App covers a repo is ground truth
        # to read, not an operator assertion (Codex, mikelward/repo#63).
        return _known_slugs[numeric]
    if not SLUG_RE.match(owner or ""):
        # `owner` is spliced into the --jq text below; anything outside the
        # slug character class cannot be, so refuse rather than guess -- a
        # fail-closed "not resolvable", never a wrong match.
        return None
    jq = (
        f".installations[] | select(.app_id == {numeric} and "
        '(.account.login | ascii_downcase) == '
        f'("{owner}" | ascii_downcase)) | .app_slug'
    )
    out = gh.run(["api", "user/installations", "--paginate", "--jq", jq])
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def app_covers_repo(owner, app_id, repo):
    """True if the App with numeric id `app_id`, installed on `owner`'s
    account, can currently act on `repo` -- an ACTIVE installation that is
    either "all repositories" or a "selected" one whose member list includes it.
    False if it is not installed on `owner` at all, a selected install excludes
    `repo`, or the installation is suspended.

    A suspended installation keeps its `repository_selection` but cannot act, so
    it can never publish the status -- counting it as coverage would let setup
    bind `lanes` to an App that then blocks every merge, and audit report the
    binding as healthy, so `suspended_at` is checked explicitly (Codex,
    mikelward/repo#52).

    This is the liveness/coverage question the binding precondition turns on,
    kept separate from the evidence scan (see app_slug_for_id). Binding a
    required check to an App that cannot publish here would wedge every merge,
    so setup refuses it -- and, unlike "has not reported yet", that refusal is
    NOT `--force`-overridable, since no amount of forcing makes an uninstalled
    App able to report (Codex, mikelward/repo#52).

    Lets a gh read failure propagate (can't-tell); the caller must tell that
    from a definite "not covered". A non-integer / non-positive id returns
    False."""
    numeric = _positive_int(app_id)
    if numeric is None:
        return False
    if numeric in _app_keys:
        # The App's own key is held, so coverage is read AS the App -- the
        # repo-scoped installation endpoint, which gh's token is refused. Needs no
        # `owner`: the endpoint is keyed by the repository itself.
        return _app_covers_repo_via_jwt(numeric, _app_keys[numeric], repo)
    if not SLUG_RE.match(owner or ""):
        return False
    jq = (
        f".installations[] | select(.app_id == {numeric} and "
        '(.account.login | ascii_downcase) == '
        f'("{owner}" | ascii_downcase)) | '
        "[.id, .repository_selection, (.suspended_at != null)] | @tsv"
    )
    out = gh.run(["api", "user/installations", "--paginate", "--jq", jq])
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        install_id, selection, suspended = line.split("\t", 2)
        if suspended == "true":
            # Suspended: it holds a repository_selection but cannot act, so it
            # never publishes here and does not count as coverage.
            return False
        if selection != "selected":
            # "all repositories" (any non-selected scope) covers every repo in
            # the account, this one included.
            return True
        return _installation_covers(install_id, repo)
    return False


def _installation_covers(install_id, repo):
    """True if `repo` is among the repositories a "selected" installation
    (`install_id`) can act on.

    Lets a gh read failure propagate rather than returning False:
    app_slug_for_id's caller must tell "not covered here" -- a real gap --
    from "the member list could not be read", which is can't-tell, and only a
    raised error carries the second, exactly as app_slug_for_id itself does.
    Same case-insensitive full-name compare plan_app_step uses."""
    if not str(install_id).isdigit():
        # `install_id` is GitHub's own numeric installation id, spliced into
        # the URL below; anything else cannot be, so fail closed rather than
        # guess -- never a wrong match.
        return False
    out = gh.run(
        [
            "api",
            "--paginate",
            f"user/installations/{install_id}/repositories",
            "--jq",
            ".repositories[].full_name",
        ]
    )
    members = [line.strip() for line in out.splitlines() if line.strip()]
    return any(member.lower() == (repo or "").lower() for member in members)


def resolve_installation(slug, repo_owner):
    """The (install_id, repository_selection) of the one installation of
    `slug` on `repo_owner`'s account, or None if it could not be resolved
    unambiguously (already reported).

    user/installations lists every installation the AUTHENTICATED USER can
    see, across every account they belong to -- personal and every org
    they are a member of. Filtering on app_slug alone would therefore
    reject a perfectly unambiguous target whenever the same App happens to
    be installed on more than one of those accounts (a personal install
    and an org install, say). repo_owner -- the target repository's own
    account -- is what disambiguates it, the same way the account itself
    disambiguates it on GitHub's own UI. Compared case-insensitively,
    since GitHub account names are.

    Only ever called with a slug/repo_owner that have already passed
    SLUG_RE / the OWNER/REPO character check, so splicing them raw into
    the --jq program's own text carries no quote-breaking risk -- neither
    character class can contain `"` or a backslash.
    """
    jq = (
        f'.installations[] | select(.app_slug=="{slug}" and '
        '(.account.login | ascii_downcase) == '
        f'("{repo_owner}" | ascii_downcase)) | [.id, .repository_selection] | @tsv'
    )
    try:
        out = gh.run(["api", "user/installations", "--paginate", "--jq", jq])
    except gh.GhError as e:
        error_lines(f"could not list App installations (needed for --app {slug}):", e.stderr)
        return None
    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        error(f"no installation of an App with slug '{slug}' was found on {repo_owner}'s account")
        return None
    if len(lines) > 1:
        error(f"more than one installation matched App slug '{slug}' on {repo_owner}'s")
        error("account -- refusing to guess which one")
        return None
    install_id, selection = lines[0].split("\t", 1)
    return install_id, selection


def _confirm_repo_exists(repo, context):
    """True if `repo` was confirmed, via a real gh api call, to exist and
    be accessible. `context` is folded into the error message when it
    isn't -- used both for the ALREADY_ALL case (an installation's own
    scope alone never confirms the repo itself) and for the not-yet-a-
    member case (absent from a "selected" installation's member list is
    ambiguous the same way -- a real, not-yet-added repo, or a typo'd one
    that would never appear in any installation's list either)."""
    ok, out = gh.try_run(["api", f"repos/{repo}", "--jq", ".id"])
    if ok:
        return True
    error_lines(f"could not confirm {repo} exists and is accessible (needed before {context}):", out)
    return False


def plan_app_step(repo, repo_owner, slug):
    """Resolves the plan for one --app SLUG against `repo`. Never raises;
    an unresolvable step is reported (via error()/error_lines() at the
    point of failure) and returned as AppPlan(slug, "ERROR")."""
    resolved = resolve_installation(slug, repo_owner)
    if resolved is None:
        return AppPlan(slug, "ERROR")
    install_id, selection = resolved

    if selection != "selected":
        # ALREADY_ALL is reported on the strength of the installation's
        # own scope alone ONLY once $repo itself is independently
        # confirmed to exist -- otherwise an App-only run against a
        # typo'd or inaccessible repo would exit 0 claiming coverage
        # nothing was ever checked.
        if _confirm_repo_exists(
            repo, f"treating it as already covered by {slug}'s 'all repositories' scope"
        ):
            return AppPlan(slug, "ALREADY_ALL")
        return AppPlan(slug, "ERROR")

    ok, out = gh.try_run(
        ["api", "--paginate", f"user/installations/{install_id}/repositories", "--jq", ".repositories[].full_name"]
    )
    if not ok:
        error_lines(f"could not list {slug}'s installed repositories:", out)
        return AppPlan(slug, "ERROR")
    # Case-insensitive: GitHub repository names are case-insensitive to
    # resolve, but .full_name is returned in the repo's own canonical
    # casing, which can differ from however the caller typed `repo`. An
    # exact-case miss here would plan ADD for a repo that's already a
    # member, which can then fail outright for a credential allowed to
    # list an installation's repositories but not modify them, reporting
    # failure on a state that already held.
    members = [line.strip() for line in out.splitlines() if line.strip()]
    if any(member.lower() == repo.lower() for member in members):
        return AppPlan(slug, "ALREADY_MEMBER")

    # Absent from the list is ambiguous the same way ALREADY_ALL's bare
    # scope check was: genuinely not-yet-added, or a typo'd/inaccessible
    # repo that would never appear in ANY installation's member list. The
    # real apply's ADD case already resolves the repo's id before adding
    # it and would catch a typo there, but planning ADD purely on "not in
    # the list" would report success for a step that would actually fail
    # at apply time.
    if _confirm_repo_exists(repo, f"planning to add it to {slug}'s installation"):
        return AppPlan(slug, "ADD", install_id)
    return AppPlan(slug, "ERROR")


def describe_plan(repo, plans):
    """Plain-text lines describing every AppPlan in `plans` -- no leading
    indentation of their own; the caller (setup_cmd's combined plan)
    indents them to fit its own nesting."""
    lines = []
    for plan in plans:
        if plan.verdict == "ADD":
            lines.append(f"{plan.slug}: would add {repo}")
        elif plan.verdict == "ALREADY_MEMBER":
            lines.append(f"{plan.slug}: already a member")
        elif plan.verdict == "ALREADY_ALL":
            lines.append(f"{plan.slug}: installed with 'All repositories' -- already covered")
        else:
            lines.append(f"{plan.slug}: could not determine (see error above)")
    return lines


_VERDICT_LABEL = {
    "ALREADY_MEMBER": "already a member",
    "ALREADY_ALL": "installed with 'All repositories'",
    "ERROR": "could not be determined",
}


def apply_step(repo, repo_owner, previewed_plan):
    """Applies one AppPlan for real. Returns True on success (including
    the ALREADY_MEMBER/ALREADY_ALL no-ops). ERROR always returns False.

    The plan is re-resolved here, fresh, via a full plan_app_step call --
    not trusted from whatever `previewed_plan` (built earlier, for the
    preview) saw. Codex review: the confirmation prompt (or an earlier
    step's own duration) can sit for an arbitrary amount of real time, and
    membership can change in that window -- a "selected" installation
    losing the repo, an "all repositories" installation narrowing, or the
    App being uninstalled outright -- so treating a previewed
    ALREADY_MEMBER/ALREADY_ALL as a no-op success without rechecking could
    report success for a repo the App no longer actually covers.
    Re-resolving is cheap (a handful of API calls, well inside the rate
    limit -- see the module docstring) and gives every verdict here the
    same fresh-state-at-write-time guarantee rules.apply_ruleset's own
    real (non-preview) call gets for free from being an independent call
    each time; there is no reason a direct function call here should
    trust a stale snapshot when a fresh read is this cheap.

    Codex review, one level deeper: re-resolving fixed trusting a STALE
    no-op as success, but blindly acting on whatever the fresh
    resolution says opened a DIFFERENT gap -- if `previewed_plan` was
    itself NOT "ADD" (a no-op, or an ERROR), setup_cmd.run's own
    needs_confirmation may have skipped asking about this App ENTIRELY,
    on the strength of "nothing to do here". If the fresh resolution then
    finds it needs ADD after all, silently performing that write would
    apply something the user was never shown, let alone confirmed --
    refused below instead, same principle as the needs_write half of
    rules.apply_ruleset's own fingerprint: a previewed no-op turning out
    to need a write is never silently promoted into one."""
    plan = plan_app_step(repo, repo_owner, previewed_plan.slug)
    if plan.verdict == "ERROR":
        return False
    if plan.verdict != "ADD":
        return True
    if previewed_plan.verdict != "ADD":
        error(f"{previewed_plan.slug}'s installation now needs {repo} added, but the plan")
        error(f"shown and confirmed said otherwise ({_VERDICT_LABEL[previewed_plan.verdict]}) --")
        error("something changed while this was waiting. Refusing to add it without a")
        error("fresh plan and confirmation covering that change. Rerun to re-check.")
        return False

    ok, out = gh.try_run(["api", f"repos/{repo}", "--jq", ".id"])
    if not ok:
        error_lines(f"could not resolve {repo}'s numeric id (needed to add it to {plan.slug}):", out)
        return False
    repo_id = out.strip()

    ok, out = gh.try_run(
        ["api", "--method", "PUT", f"user/installations/{plan.install_id}/repositories/{repo_id}"]
    )
    if ok:
        print(f"{repo}: added to {plan.slug}'s installation")
        return True
    error_lines(f"could not add {repo} to {plan.slug}'s installation:", out)
    error("if this looks like a permission problem rather than a 'not found', a")
    error("classic PAT is the documented fallback -- these endpoints predate")
    error("fine-grained ones and may not accept them (unverified against GitHub's")
    error("own docs; see repo-setup's own header comment in mikelward/scripts).")
    return False
