import base64
import http.client
import io
import json
import subprocess
import os
import unittest
import urllib.error
from unittest.mock import patch

from repo_lib import apps, gh


class _FakeResponse:
    """A minimal stand-in for urllib's response context manager."""

    def __init__(self, status, body):
        self.status = status
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class MintAppJwtTest(unittest.TestCase):
    """The signing wrapper itself: patch subprocess.run (as test_gh.py does for
    gh.py) to prove the argv, the temp-key handling, and the JWT shape without a
    real openssl or a real key."""

    def test_builds_a_three_segment_rs256_jwt_signed_via_openssl(self):
        seen = {}

        def fake_run(argv, input=None, capture_output=False):
            seen["argv"] = argv
            seen["input"] = input
            seen["key_present_at_call"] = os.path.exists(argv[-1])
            with open(argv[-1], "rb") as f:
                seen["key_bytes"] = f.read()
            return subprocess.CompletedProcess(argv, 0, stdout=b"SIGNATURE", stderr=b"")

        with patch("repo_lib.apps.subprocess.run", side_effect=fake_run):
            jwt = apps._mint_app_jwt(4650916, b"PEM-BYTES")

        self.assertEqual(seen["argv"][:4], ["openssl", "dgst", "-sha256", "-sign"])
        self.assertTrue(seen["key_present_at_call"])  # the key file exists while openssl runs
        self.assertEqual(seen["key_bytes"], b"PEM-BYTES")
        self.assertFalse(os.path.exists(seen["argv"][-1]))  # and is removed afterward

        header_b64, payload_b64, sig_b64 = jwt.split(".")
        self.assertEqual(json.loads(_b64url_decode(header_b64)), {"alg": "RS256", "typ": "JWT"})
        payload = json.loads(_b64url_decode(payload_b64))
        self.assertEqual(payload["iss"], "4650916")
        # iat backdated 60s for skew, exp 540s ahead: a 600s span, inside the 10-min cap.
        self.assertEqual(payload["exp"] - payload["iat"], 600)
        # The signature is base64url(openssl's raw output), no padding.
        self.assertEqual(sig_b64, base64.urlsafe_b64encode(b"SIGNATURE").rstrip(b"=").decode())
        # openssl signs exactly header.payload.
        self.assertEqual(seen["input"], (header_b64 + "." + payload_b64).encode())

    def test_a_signing_failure_is_a_gherror(self):
        def fake_run(argv, input=None, capture_output=False):
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"not a private key")

        with patch("repo_lib.apps.subprocess.run", side_effect=fake_run):
            with self.assertRaises(gh.GhError) as caught:
                apps._mint_app_jwt(123, b"garbage")
        self.assertIn("not a private key", str(caught.exception))

    def test_a_temp_file_failure_is_a_gherror_not_a_traceback(self):
        # An unwritable/full TMPDIR: mkstemp raises OSError before the cleanup
        # block. The callers only translate GhError into a deferred step, so an
        # escaping OSError would abort the whole fleet run (Codex, #65).
        with patch("repo_lib.apps.tempfile.mkstemp", side_effect=OSError("No space left on device")):
            with self.assertRaises(gh.GhError) as caught:
                apps._mint_app_jwt(123, b"PEM")
        self.assertIn("No space left", str(caught.exception))

    def test_openssl_missing_at_call_time_is_a_gherror_not_a_traceback(self):
        # openssl vanished since the preflight -> FileNotFoundError (an OSError).
        with patch("repo_lib.apps.subprocess.run", side_effect=FileNotFoundError("openssl")):
            with self.assertRaises(gh.GhError):
                apps._mint_app_jwt(123, b"PEM")

    def test_a_failed_key_cleanup_warns_with_the_path(self):
        # If the temp key file can't be removed, the private key is left on disk --
        # the operator must be told where, not left thinking all is well (Codex, #65).
        def ok(argv, input=None, capture_output=False):
            return subprocess.CompletedProcess(argv, 0, stdout=b"SIG", stderr=b"")

        warnings = []
        with patch("repo_lib.apps.subprocess.run", side_effect=ok), patch(
            "repo_lib.apps.os.remove", side_effect=OSError("read-only file system")
        ), patch("repo_lib.apps.warn", side_effect=lambda m: warnings.append(m)):
            jwt = apps._mint_app_jwt(4650916, b"PEM")
        self.assertEqual(len(jwt.split(".")), 3)  # signing still succeeded
        self.assertTrue(any("remove it by hand" in w for w in warnings))
        self.assertTrue(any("repo-app-key-" in w for w in warnings))  # names the file


class ApiGetAsAppTest(unittest.TestCase):
    def test_sets_a_bearer_header_in_the_request_not_a_command_line(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["auth"] = request.get_header("Authorization")
            seen["timeout"] = timeout
            return _FakeResponse(200, '{"slug":"lanes-app"}')

        with patch("repo_lib.apps.urllib.request.urlopen", side_effect=fake_urlopen):
            status, body = apps._api_get_as_app("JWT-TOKEN", "repos/o/r/installation")

        self.assertEqual(status, "200")
        self.assertEqual(body, '{"slug":"lanes-app"}')
        self.assertEqual(seen["url"], "https://api.github.com/repos/o/r/installation")
        self.assertEqual(seen["method"], "GET")
        # The bearer lives in the request headers -- never in a process's argv.
        self.assertEqual(seen["auth"], "Bearer JWT-TOKEN")
        self.assertIsNotNone(seen["timeout"])  # never hangs forever

    def test_an_http_error_status_comes_back_as_data_not_an_exception(self):
        err = urllib.error.HTTPError(
            "https://api.github.com/repos/o/r/installation",
            404,
            "Not Found",
            hdrs=None,
            fp=io.BytesIO(b'{"message":"Not Found"}'),
        )
        with patch("repo_lib.apps.urllib.request.urlopen", side_effect=err):
            status, body = apps._api_get_as_app("JWT", "repos/o/r/installation")
        self.assertEqual(status, "404")
        self.assertIn("Not Found", body)

    def test_a_transport_failure_is_a_gherror(self):
        with patch(
            "repo_lib.apps.urllib.request.urlopen",
            side_effect=urllib.error.URLError("could not resolve host"),
        ):
            with self.assertRaises(gh.GhError) as caught:
                apps._api_get_as_app("JWT", "app")
        self.assertIn("could not resolve host", str(caught.exception))

    def test_a_truncated_body_is_a_gherror_not_a_traceback(self):
        # A successful response cut short mid-body raises http.client.IncompleteRead
        # (an HTTPException, not URLError/OSError) from read() -- it must not escape
        # the App-read boundary (Codex, #65).
        class _Truncated:
            status = 200

            def read(self):
                raise http.client.IncompleteRead(b"partial")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch("repo_lib.apps.urllib.request.urlopen", return_value=_Truncated()):
            with self.assertRaises(gh.GhError):
                apps._api_get_as_app("JWT", "app")


class AppKeyRegistryTest(unittest.TestCase):
    def tearDown(self):
        apps.register_app_keys({})
        apps.register_known_slugs({})

    def test_register_coerces_keys_and_drops_bad_ones(self):
        apps.register_app_keys({"4650916": b"PEM", "0": b"x", "nope": b"y", "7": b""})
        self.assertEqual(apps._app_keys, {4650916: b"PEM"})

    def test_register_empty_clears(self):
        apps.register_app_keys({"7": b"PEM"})
        apps.register_app_keys({})
        self.assertEqual(apps._app_keys, {})
        self.assertEqual(apps._app_slug_cache, {})

    def test_tooling_missing_reports_openssl_when_absent(self):
        # openssl is the only external tool now (the HTTP call is stdlib urllib).
        with patch("repo_lib.apps.shutil.which", side_effect=lambda t: None if t == "openssl" else "/x"):
            self.assertEqual(apps.app_jwt_tooling_missing(), ["openssl"])
        with patch("repo_lib.apps.shutil.which", return_value="/x"):
            self.assertEqual(apps.app_jwt_tooling_missing(), [])


class AppCoversRepoViaJwtTest(unittest.TestCase):
    def setUp(self):
        apps.register_app_keys({4650916: b"PEM"})
        self.addCleanup(apps.register_app_keys, {})
        self._mint = patch("repo_lib.apps._mint_app_jwt", lambda app_id, pem: f"JWT:{app_id}")
        self._mint.start()
        self.addCleanup(self._mint.stop)

    def _installation(self, status, body):
        return patch("repo_lib.apps._api_get_as_app", lambda jwt, path: (status, body))

    def test_present_and_active_installation_covers(self):
        with self._installation("200", json.dumps({"app_id": 4650916, "suspended_at": None})):
            self.assertTrue(apps.app_covers_repo("owner", 4650916, "owner/repo"))

    def test_a_404_is_not_covered(self):
        with self._installation("404", json.dumps({"message": "Not Found"})):
            self.assertFalse(apps.app_covers_repo("owner", 4650916, "owner/repo"))

    def test_a_suspended_installation_is_not_covered(self):
        with self._installation(
            "200", json.dumps({"app_id": 4650916, "suspended_at": "2020-01-01T00:00:00Z"})
        ):
            self.assertFalse(apps.app_covers_repo("owner", 4650916, "owner/repo"))

    def test_a_200_for_a_different_app_id_is_a_gherror(self):
        # A stale/other-App 200 (suspended_at null but a foreign app_id) must not be
        # read as coverage for our App (Codex, #65).
        with self._installation("200", json.dumps({"app_id": 999, "suspended_at": None})):
            with self.assertRaises(gh.GhError):
                apps.app_covers_repo("owner", 4650916, "owner/repo")

    def test_an_unexpected_status_is_a_gherror_not_a_guess(self):
        with self._installation("500", "oops"):
            with self.assertRaises(gh.GhError):
                apps.app_covers_repo("owner", 4650916, "owner/repo")

    def test_a_200_with_non_object_json_is_a_gherror_not_a_traceback(self):
        # `[]` / `null` parse fine but have no `.get`; without the object check
        # that AttributeError would abort the fleet (Codex, #65).
        for body in ("[]", "null", '"a string"'):
            with self._installation("200", body):
                with self.assertRaises(gh.GhError):
                    apps.app_covers_repo("owner", 4650916, "owner/repo")

    def test_a_200_without_suspended_at_is_a_gherror_not_assumed_active(self):
        # A real installation always carries `suspended_at`; a 200 that omits it
        # must not be read as active coverage -- that could bind lanes on an
        # unvalidated response and wedge merges (Codex, #65). app_id present so this
        # isolates the suspended_at check.
        with self._installation("200", json.dumps({"app_id": 4650916})):
            with self.assertRaises(gh.GhError):
                apps.app_covers_repo("owner", 4650916, "owner/repo")

    def test_a_200_with_suspended_at_null_covers(self):
        with self._installation("200", json.dumps({"app_id": 4650916, "suspended_at": None})):
            self.assertTrue(apps.app_covers_repo("owner", 4650916, "owner/repo"))

    def test_an_unforeseen_exception_in_the_read_still_becomes_a_gherror(self):
        # The boundary catches the class: a failure that is not GhError, transport,
        # or one of the named shapes still defers rather than aborting the fleet.
        with patch("repo_lib.apps._mint_app_jwt", lambda a, p: "J"), patch(
            "repo_lib.apps._api_get_as_app", side_effect=ValueError("something unforeseen")
        ):
            with self.assertRaises(gh.GhError):
                apps.app_covers_repo("owner", 4650916, "owner/repo")

    def test_the_jwt_path_never_reads_user_installations(self):
        with self._installation("404", "{}"), patch(
            "repo_lib.apps.gh.run", side_effect=AssertionError("must not read user/installations")
        ):
            self.assertFalse(apps.app_covers_repo("owner", 4650916, "owner/repo"))


class AppSlugViaJwtTest(unittest.TestCase):
    def tearDown(self):
        apps.register_app_keys({})
        apps.register_known_slugs({})

    def test_reads_the_slug_from_get_app_and_memoizes_it(self):
        calls = []

        def fake_api(jwt, path):
            calls.append(path)
            return "200", json.dumps({"id": 777, "slug": "lanes-app"})

        apps.register_app_keys({777: b"PEM"})
        with patch("repo_lib.apps._mint_app_jwt", lambda a, p: "JWT"), patch(
            "repo_lib.apps._api_get_as_app", fake_api
        ):
            self.assertEqual(apps.app_slug_for_id("owner", 777), "lanes-app")
            self.assertEqual(apps.app_slug_for_id("owner", 777), "lanes-app")
        self.assertEqual(calls, ["app"])  # repo-independent, so read once per run

    def test_a_200_with_non_object_json_is_a_gherror_not_a_traceback(self):
        apps.register_app_keys({777: b"PEM"})
        with patch("repo_lib.apps._mint_app_jwt", lambda a, p: "JWT"), patch(
            "repo_lib.apps._api_get_as_app", lambda j, path: ("200", "[]")
        ):
            with self.assertRaises(gh.GhError):
                apps.app_slug_for_id("owner", 777)

    def test_a_missing_or_non_string_slug_is_a_gherror(self):
        apps.register_app_keys({777: b"PEM"})
        for body in (
            {"id": 777},  # no slug
            {"id": 777, "slug": ""},  # empty
            {"id": 777, "slug": 5},  # not a string
        ):
            with patch("repo_lib.apps._mint_app_jwt", lambda a, p: "JWT"), patch(
                "repo_lib.apps._api_get_as_app", lambda j, path, b=body: ("200", json.dumps(b))
            ):
                apps._app_slug_cache.clear()
                with self.assertRaises(gh.GhError):
                    apps.app_slug_for_id("owner", 777)

    def test_a_mismatched_app_id_in_the_record_is_a_gherror(self):
        # GET /app as the App returns THIS App's record; a different id is untrusted.
        apps.register_app_keys({777: b"PEM"})
        with patch("repo_lib.apps._mint_app_jwt", lambda a, p: "JWT"), patch(
            "repo_lib.apps._api_get_as_app", lambda j, path: ("200", json.dumps({"id": 999, "slug": "x"}))
        ):
            with self.assertRaises(gh.GhError):
                apps.app_slug_for_id("owner", 777)

    def test_jwt_slug_is_ground_truth_so_it_wins_and_ignores_use_known(self):
        # A held key outranks a config `app_logins` assertion, and is trusted even
        # under use_known=False (coverage prediction), unlike a config pairing.
        apps.register_app_keys({777: b"PEM"})
        apps.register_known_slugs({"777": "config-asserted"})
        with patch("repo_lib.apps._mint_app_jwt", lambda a, p: "JWT"), patch(
            "repo_lib.apps._api_get_as_app",
            lambda j, path: ("200", json.dumps({"id": 777, "slug": "real"})),
        ):
            self.assertEqual(apps.app_slug_for_id("owner", 777, use_known=False), "real")


if __name__ == "__main__":
    unittest.main()
