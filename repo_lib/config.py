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
Read with `safe_load` and a strict schema (unknown keys and wrong types
are usage errors), so the only YAML accepted is the flat mapping documented
here -- never an arbitrary tagged object.

Secret VALUES never live here: a credential entry is a path the value is
read from, exactly as `--credential NAME=PATH` is. Resolving a value from
a command or a secret manager is a tracked follow-up (TODO.md).
"""

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

DEFAULT_RELPATH = "repo/config.yaml"
_KNOWN_KEYS = {"credentials", "rules", "apps", "force"}


class ConfigError(Exception):
    """A missing (when named) or malformed config file. Carries a
    ready-to-print message; the caller turns it into a usage-error exit."""


@dataclass
class SetupConfig:
    # NAME -> path the value is read from (the config form of --credential).
    credentials: dict = field(default_factory=dict)
    # None means "unset" -- fall through to the CLI default -- as distinct
    # from an explicit empty list.
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
    resolved = path or default_path()
    try:
        with open(resolved) as f:
            raw = f.read()
    except FileNotFoundError:
        if path:
            raise ConfigError(f"config file not found: {resolved}")
        return None
    except OSError as e:
        raise ConfigError(f"cannot read config file {resolved}: {e}")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigError(f"config file {resolved} is not valid YAML:\n{e}")
    if data is None:
        return SetupConfig()  # an empty file is a valid empty config
    if not isinstance(data, dict):
        raise ConfigError(
            f"config file {resolved} must be a mapping, not {type(data).__name__}"
        )
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
        cfg.credentials = creds
    for key in ("rules", "apps"):
        if key in data:
            value = data[key]
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ConfigError(
                    f"config file {resolved}: '{key}' must be a list of strings"
                )
            setattr(cfg, key, value)
    if "force" in data:
        value = data["force"]
        # A YAML bool, not a truthy int/string: `force: 1` is a mistake, not
        # a quiet yes.
        if not isinstance(value, bool):
            raise ConfigError(f"config file {resolved}: 'force' must be true or false")
        cfg.force = value
    return cfg
