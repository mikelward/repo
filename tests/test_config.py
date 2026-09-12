import os
import tempfile
import unittest
from unittest.mock import patch

from repo_lib import config


def _write(tmp, text, name="config.yaml"):
    path = os.path.join(tmp, name)
    with open(path, "w") as f:
        f.write(text)
    return path


class DefaultPathTest(unittest.TestCase):
    def test_honors_xdg_config_home(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg"}):
            self.assertEqual(config.default_path(), "/xdg/repo/config.yaml")

    def test_falls_back_to_dot_config(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                config.default_path(), os.path.expanduser("~/.config/repo/config.yaml")
            )


class LoadTest(unittest.TestCase):
    def test_absent_default_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                self.assertIsNone(config.load())

    def test_absent_named_file_is_an_error(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.load("/no/such/config.yaml")
        self.assertIn("not found", str(caught.exception))

    def test_empty_file_is_a_valid_empty_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.load(_write(tmp, ""))
        self.assertEqual(cfg.credentials, {})
        self.assertIsNone(cfg.rules)
        self.assertIsNone(cfg.apps)
        self.assertFalse(cfg.force)

    def test_a_full_config_parses(self):
        text = (
            "credentials:\n"
            "  LANES_APP_ID: /keys/id\n"
            "  LANES_APP_PRIVATE_KEY: /keys/pem\n"
            "rules:\n  - lanes\n  - codex\n"
            "apps:\n  - some-app\n"
            "force: true\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.load(_write(tmp, text))
        self.assertEqual(
            cfg.credentials, {"LANES_APP_ID": "/keys/id", "LANES_APP_PRIVATE_KEY": "/keys/pem"}
        )
        self.assertEqual(cfg.rules, ["lanes", "codex"])
        self.assertEqual(cfg.apps, ["some-app"])
        self.assertTrue(cfg.force)

    def test_unknown_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, "secret: TOKEN=/x\n"))
        self.assertIn("unknown key", str(caught.exception))
        self.assertIn("secret", str(caught.exception))

    def test_a_non_mapping_document_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, "- just\n- a\n- list\n"))
        self.assertIn("must be a mapping", str(caught.exception))

    def test_credentials_must_be_a_string_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError):
                config.load(_write(tmp, "credentials:\n  LANES_APP_ID: 12345\n"))

    def test_rules_must_be_a_list_of_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError):
                config.load(_write(tmp, "rules: lanes\n"))

    def test_force_must_be_a_bool_not_a_truthy_int(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, "force: 1\n"))
        self.assertIn("true or false", str(caught.exception))

    def test_malformed_yaml_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, "credentials: {unterminated\n"))
        self.assertIn("not valid YAML", str(caught.exception))

    def test_a_duplicate_top_level_key_is_rejected(self):
        # safe_load would keep the last silently; fleet-wide that could
        # replace the intended checks without a word (Codex, mikelward/repo#62).
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, "rules:\n  - lanes\nrules:\n  - codex\n"))
        self.assertIn("duplicate key", str(caught.exception))

    def test_a_duplicate_credential_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(
                    _write(tmp, "credentials:\n  LANES_APP_ID: /a\n  LANES_APP_ID: /b\n")
                )
        self.assertIn("duplicate key", str(caught.exception))

    def test_a_non_string_top_level_key_is_a_usage_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, "1: value\n"))
        self.assertIn("keys must be strings", str(caught.exception))

    def test_a_composite_key_is_a_usage_error_not_a_traceback(self):
        # A list key (`? [a, b]`) constructs to an unhashable list; the
        # loader must reject it before any `key in mapping` (Codex,
        # mikelward/repo#62).
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, "? [a, b]\n: value\n"))
        self.assertIn("keys must be strings", str(caught.exception))

    def test_invalid_utf8_is_a_usage_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "wb") as f:
                f.write(b"force: \xff\xfe\n")
            with self.assertRaises(config.ConfigError) as caught:
                config.load(path)
        self.assertIn("UTF-8", str(caught.exception))

    def test_an_empty_rules_list_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, "rules: []\n"))
        self.assertIn("must not be empty", str(caught.exception))

    def test_an_empty_apps_list_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.load(_write(tmp, "apps: []\n"))
        self.assertEqual(cfg.apps, [])

    def test_a_credential_name_with_equals_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, "credentials:\n  ? LANES_APP_ID=/tmp/id\n  : x\n"))
        self.assertIn("must not", str(caught.exception))

    def test_an_empty_explicit_path_is_a_usage_error(self):
        # `--config "$VAR"` with VAR unset arrives as "" -- it must not fall
        # through to the default (Codex, mikelward/repo#62).
        with self.assertRaises(config.ConfigError) as caught:
            config.load("")
        self.assertIn("empty path", str(caught.exception))

    def test_a_scalar_that_fails_to_construct_is_a_usage_error(self):
        # A value that parses but cannot be constructed (an impossible date,
        # or !!int on non-digits) raises ValueError from PyYAML's
        # constructor, not YAMLError -- it must still be a ConfigError, not a
        # traceback (Codex, mikelward/repo#62).
        for bad in ("force: 2022-13-40\n", "force: !!int not\n"):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(config.ConfigError, msg=bad):
                    config.load(_write(tmp, bad))

    def test_app_logins_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.load(_write(tmp, 'app_logins:\n  "4650916": mikelward-lanes\n'))
        self.assertEqual(cfg.app_logins, {"4650916": "mikelward-lanes"})

    def test_app_logins_key_must_be_a_positive_integer(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, "app_logins:\n  not-a-number: some-app\n"))
        self.assertIn("positive integer", str(caught.exception))

    def test_app_logins_aliased_ids_are_rejected(self):
        # "123" and "0123" both normalize to int 123 -- the second slug would
        # silently win; the non-canonical spelling is rejected instead (Codex,
        # mikelward/repo#63).
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, 'app_logins:\n  "123": a\n  "0123": b\n'))
        self.assertIn("plain decimal form", str(caught.exception))

    def test_app_logins_slug_with_trailing_newline_is_rejected(self):
        # `$` matches before a trailing newline; the slug pattern uses \Z so
        # "lanes-app\n" is rejected rather than registered as "lanes-app\n[bot]"
        # (Codex, mikelward/repo#63).
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, 'app_logins:\n  "4650916": "lanes-app\\n"\n'))
        self.assertIn("slug", str(caught.exception))

    def test_app_logins_slug_must_be_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, 'app_logins:\n  "4650916": "not a slug"\n'))
        self.assertIn("slug", str(caught.exception))

    def test_app_logins_duplicate_slug_across_ids_is_rejected(self):
        # A bot slug names one App, so two ids sharing a slug would let one
        # App's status satisfy the evidence check for the other id -- the
        # pairing must stay one-to-one (Codex, mikelward/repo#63).
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as caught:
                config.load(_write(tmp, 'app_logins:\n  "123": lanes-app\n  "456": lanes-app\n'))
        self.assertIn("names one App", str(caught.exception))

    def test_safe_load_refuses_arbitrary_tags(self):
        # The hardening: safe_load raises on a python-object tag rather than
        # constructing it, so a config file can never execute code.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError):
                config.load(_write(tmp, "force: !!python/object/apply:os.system ['x']\n"))


if __name__ == "__main__":
    unittest.main()
