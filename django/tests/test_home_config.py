import pytest

from homeserver import home_config


def _patch_home_yaml(monkeypatch, tmp_path, content=None):
    path = tmp_path / 'home.yaml'
    if content is not None:
        path.write_text(content)
    monkeypatch.setattr(home_config, '_HOME_YAML_PATH', path)


def test_get_allowed_hosts_defaults_to_empty_when_home_yaml_missing(tmp_path, monkeypatch):
    _patch_home_yaml(monkeypatch, tmp_path)  # no file written
    assert home_config.get_allowed_hosts() == []


def test_get_allowed_hosts_defaults_to_empty_when_key_absent(tmp_path, monkeypatch):
    _patch_home_yaml(monkeypatch, tmp_path, 'default_cloudserver_url: https://cloud.example.com\n')
    assert home_config.get_allowed_hosts() == []


def test_get_allowed_hosts_returns_configured_list(tmp_path, monkeypatch):
    _patch_home_yaml(monkeypatch, tmp_path, 'allowed_hosts:\n  - myhome.local\n  - 192.168.1.50\n')
    assert home_config.get_allowed_hosts() == ['myhome.local', '192.168.1.50']


def test_get_allowed_hosts_rejects_non_list_value(tmp_path, monkeypatch):
    _patch_home_yaml(monkeypatch, tmp_path, 'allowed_hosts: myhome.local\n')
    with pytest.raises(ValueError):
        home_config.get_allowed_hosts()
