"""Loads Django-settings-facing values out of home.yaml -- the optional,
machine-wide settings file at the repo root that's shared across every
profile (see home.yaml.example). This is deliberately separate from
cloudlink/config.py: that module owns a single profile's cloud-connection
config (pointed to by HOME_CONFIG), while home.yaml describes the machine
itself and has nothing to do with any particular cloud link.
"""
from pathlib import Path

import yaml

_HOME_YAML_PATH = Path(__file__).resolve().parents[2] / 'home.yaml'


def _load_home_yaml():
    try:
        with open(_HOME_YAML_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def get_allowed_hosts() -> list[str]:
    """Django's ALLOWED_HOSTS, overridden via home.yaml's allowed_hosts list.
    Empty (the default) leaves Django's own DEBUG-mode fallback in place,
    which only permits localhost/127.0.0.1/[::1] -- set this to reach the
    Home Console via a LAN IP or a local domain name."""
    hosts = _load_home_yaml().get('allowed_hosts') or []
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise ValueError(
            f"home.yaml: 'allowed_hosts' must be a list of hostnames, got: {hosts!r}"
        )
    return hosts
