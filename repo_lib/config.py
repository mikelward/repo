"""The fleet config file for `repo setup`.

The convergence invocation is the same flags over every repository (see
SPEC.md): `--force`, the fleet's `--credential` set, and the `--rule`/
`--app` the fleet wants. Those are fleet constants -- identical on every
repository and every run -- so typing them each time is pure repetition.
This reads them from one file instead, and a command-line flag still
overrides it for a one-off run.

YAML because it is the tool's one existing dependency (the workflow reader
in `repo_lib/credentials.py`); TOML's `tomllib` is standard only in 3.11+
and this supports 3.9, so it would need a backport -- a second dependency.
Read with a SafeLoader (no code execution) that also rejects duplicate
keys, plus a strict schema (unknown keys, non-string keys and wrong types
are usage errors), so the only YAML accepted is the flat mapping documented
here -- never an arbitrary tagged object, and never a silent last-wins drop.

Secret VALUES never live here: a credential entry is a path the value is
read from, exactly as `--credential NAME=PATH` is. Resolving a value from
a command or a secret manager is a tracked follow-up (TODO.md).
"""

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

DEFAULT_RELPATH = "repo/config.yaml"


class _BadConfig(Exception):
    """A structural fault found while constructing the YAML: a repeated key,
    or a key that is not a string. Carries a ready-made message; `load`
    catches it and re-raises as a ConfigError with the file path. Enforcing
    both inside the loader keeps it the single place every key -- top-level
    or nested, scalar or composite -- passes through, so no wrong-type key
    can reach a later `key in mapping` and escape as a raw TypeError (Codex,
    mikelward/repo#62)."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader (no code execution) that additionally requires every
    mapping key to be a string and rejects duplicates, at any nesting."""


def _construct_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise _BadConfig(
                f"keys must be strings, not {type(key).__name__} ({key!r})"
            )
        if key in mapping:
            raise _BadConfig(f"duplicate key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)
_KNOWN_KEYS = {"credentials", "rules", "apps", "force"}


class ConfigError(Exception):
    """A missing (when named) or malformed config file. Carries a
    ready-to-print message; the caller turns it into a usage-error exit."""


@dataclass
class SetupConfig:
    # NAME -> path the value is read from (the config form of --credential).
    credentials: dict = field(default_factory=dict)
    # None means "unset" -- fall through to the CLI default. An empty
    # `rules` is rejected at load (the tool has no zero-checks mode), so
    # callers never see the None/empty ambiguity for it; empty `apps` is
    # valid and means no apps, the same as unset.
    rules: Optional[list] = None
    apps: Optional[list] = None
    force: bool = False


def default_path():
    """`$XDG_CONFIG_HOME/repo/config.yaml`, or `~/.config/repo/config.yaml`."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, DEFAULT_RELPATH)


def load(path=None):
    """The fleet config, or None when there is none to read.

    `path` None reads the default location, whose ABSENCE is the normal
    case and returns None. An explicit `path` that is missing is an error:
    the operator named a file that isn't there. A malformed file is always
    an error -- a typo silently ignored would drop fleet config without a
    word, exactly the failure this file exists to avoid.
    """
    # Distinguish "not given" (None -> default, absence is fine) from an
    # explicitly named path, INCLUDING an empty one: `--config "$VAR"` with
    # VAR unset must be a usage error, not a silent fall-through to the
    # default (Codex, mikelward/repo#62).
    if path is not None and not path:
        raise ConfigError("--config was given an empty path")
    resolved = default_path() if path is None else path
    try:
        with open(resolved, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        if path is not None:
            raise ConfigError(f"config file not found: {resolved}")
        return None
    except OSError as e:
        raise ConfigError(f"cannot read config file {resolved}: {e}")
    except UnicodeDecodeError as e:
        # Not an OSError, so it would otherwise escape as a traceback.
        raise ConfigError(f"config file {resolved} is not valid UTF-8: {e}")
    try:
        data = yaml.load(raw, Loader=_StrictLoader)
    except _BadConfig as e:
        raise ConfigError(f"config file {resolved}: {e.message}")
    except Exception as e:
        # The try body only parses untrusted YAML, so ANY failure here means
        # the file is malformed -- and PyYAML's constructors raise an
        # open-ended set beyond yaml.YAMLError (a ValueError on an impossible
        # date like `2022-13-40` or `!!int not`, an AttributeError or KeyError
        # from other built-in tags). Catching the class rather than chasing
        # each type is what keeps a bad file a usage error instead of a
        # traceback (Codex, mikelward/repo#62). _BadConfig is ours and handled
        # above; KeyboardInterrupt/SystemExit are BaseException, not caught.
        raise ConfigError(f"config file {resolved} is not valid YAML: {e}")
    if data is None:
        return SetupConfig()  # an empty file is a valid empty config
    if not isinstance(data, dict):
        raise ConfigError(
            f"config file {resolved} must be a mapping, not {type(data).__name__}"
        )
    # Every key is a string here: the loader enforced it while building the
    # mapping (see _construct_mapping), so this is only about known-ness.
    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(
            f"config file {resolved}: unknown key(s) {', '.join(sorted(unknown))}; "
            f"known keys are {', '.join(sorted(_KNOWN_KEYS))}"
        )
    cfg = SetupConfig()
    if "credentials" in data:
        creds = data["credentials"]
        if not isinstance(creds, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in creds.items()
        ):
            raise ConfigError(
                f"config file {resolved}: 'credentials' must be a mapping of NAME "
                "to a path string"
            )
        for name in creds:
            if "=" in name:
                raise ConfigError(
                    f"config file {resolved}: credential name {name!r} must not "
                    "contain '='"
                )
        cfg.credentials = creds
    for key in ("rules", "apps"):
        if key in data:
            value = data[key]
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ConfigError(
                    f"config file {resolved}: '{key}' must be a list of strings"
                )
            setattr(cfg, key, value)
    if cfg.rules == []:
        raise ConfigError(
            f"config file {resolved}: 'rules' must not be empty; omit it to use "
            "the default checks"
        )
    if "force" in data:
        value = data["force"]
        # A YAML bool, not a truthy int/string: `force: 1` is a mistake, not
        # a quiet yes.
        if not isinstance(value, bool):
            raise ConfigError(f"config file {resolved}: 'force' must be true or false")
        cfg.force = value
    return cfg
