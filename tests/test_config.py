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

    def test_safe_load_refuses_arbitrary_tags(self):
        # The hardening: safe_load raises on a python-object tag rather than
        # constructing it, so a config file can never execute code.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError):
                config.load(_write(tmp, "force: !!python/object/apply:os.system ['x']\n"))


if __name__ == "__main__":
    unittest.main()
