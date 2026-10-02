import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from django.urls import reverse

from cloudlink.config import get_config
from cloudlink.services import PublicPortNotOfferedError
from domains.forms import ProxyEntryForm
from domains.models import Domain, ProxyEntry

INBOUND_PORTS = {'http': (80, 8080, 10), 'https': (443, 8443, 10)}


def _form(**data):
    return ProxyEntryForm(data={'scheme': 'http', 'home_port': 3000, **data}, inbound_ports=INBOUND_PORTS)


@pytest.fixture
def custom_ports_config(cloudlink_config, monkeypatch):
    """A profile whose cloud publishes HTTP/HTTPS on non-standard default ports."""
    import os
    import cloudlink.config as cfg_module
    path = Path(os.environ['HOME_CONFIG'])
    data = yaml.safe_load(path.read_text())
    data['cloudlink']['http_ports'] = {'default': 8000, 'base': 8080, 'count': 10}
    data['cloudlink']['https_ports'] = {'default': 8001}
    path.write_text(yaml.dump(data))
    monkeypatch.setattr(cfg_module, '_config', None)


# --- config ---------------------------------------------------------------

def test_inbound_ports_fall_back_to_80_443_when_not_cached():
    cfg = get_config()
    assert cfg.inbound_ports('http') == (80, None, None)
    assert cfg.inbound_ports('https') == (443, None, None)


def test_inbound_ports_read_cached_default_and_range(custom_ports_config):
    cfg = get_config()
    assert cfg.inbound_ports('http') == (8000, 8080, 10)
    assert cfg.inbound_ports('https') == (8001, None, None)


# --- form -----------------------------------------------------------------

def test_blank_public_port_is_valid():
    form = _form(public_port='')
    assert form.is_valid(), form.errors
    assert form.cleaned_data['public_port'] is None


@pytest.mark.parametrize('port', [80, 8080, 8089])
def test_default_or_in_range_port_is_valid(port):
    assert _form(public_port=port).is_valid()


@pytest.mark.parametrize('port', [0, -1, 65536])
def test_out_of_bounds_public_port_rejected(port):
    form = _form(public_port=port)
    assert not form.is_valid()
    assert 'public_port' in form.errors


def test_out_of_range_public_port_rejected_with_offered_ports():
    form = _form(public_port=8090)
    assert not form.is_valid()
    assert form.errors['public_port'] == ['Must be 80 (default) or in range 8080–8089.']


def test_range_is_checked_against_the_selected_scheme():
    form = _form(scheme='https', public_port=8080)
    assert not form.is_valid()
    assert form.errors['public_port'] == ['Must be 443 (default) or in range 8443–8452.']


def test_only_default_accepted_when_no_range_cached():
    form = ProxyEntryForm(
        data={'scheme': 'https', 'home_port': 3000, 'public_port': 8443},
        inbound_ports={'https': (443, None, None)},
    )
    assert not form.is_valid()
    assert form.errors['public_port'] == ['Must be 443 (default).']


@pytest.mark.parametrize('port', [0, 65536])
def test_out_of_bounds_home_port_rejected(port):
    form = _form(home_port=port)
    assert not form.is_valid()
    assert 'home_port' in form.errors


# --- view -----------------------------------------------------------------

@pytest.fixture
def domain(db):
    return Domain.objects.create(name='mysite.example.com')


def _post(client, domain, **data):
    return client.post(
        reverse('add_proxy_entry', args=[domain.pk]),
        {'scheme': 'http', 'home_port': 3000, 'public_port': '', **data},
    )


@pytest.mark.django_db
def test_view_uses_cloud_default_port_for_duplicate_check(client, domain, custom_ports_config):
    ProxyEntry.objects.create(domain=domain, scheme='http', public_port=8000, home_port=4000, tunnel_port=2000)
    with patch('domains.views.CloudServerClient') as MockClient:
        resp = _post(client, domain)
    assert resp.status_code == 200
    assert 'already has a HTTP proxy entry on port 8000' in resp.content.decode()
    MockClient.return_value.create_proxy_mapping.assert_not_called()


@pytest.mark.django_db
def test_view_accepts_cloud_default_port_explicitly(client, domain, custom_ports_config):
    with patch('domains.views.CloudServerClient') as MockClient:
        MockClient.return_value.create_proxy_mapping.return_value = {'tunnel_port': 2000, 'public_port': 8000}
        resp = _post(client, domain, public_port=8000)
    assert resp.status_code == 302
    entry = ProxyEntry.objects.get()
    assert entry.public_port == 8000
    # The cloud's default port isn't shown as a suffix.
    assert str(entry) == 'mysite.example.com → localhost:3000 (http)'


@pytest.mark.django_db
def test_view_shows_cloud_port_rejection_on_public_port_field(client, domain):
    error = PublicPortNotOfferedError(80, 80, [{'port_base': 9080, 'port_count': 5}])
    with patch('domains.views.CloudServerClient') as MockClient:
        MockClient.return_value.create_proxy_mapping.side_effect = error
        resp = _post(client, domain)
    assert resp.status_code == 200
    assert resp.context['form'].errors['public_port'] == [str(error)]


@pytest.mark.django_db
def test_view_lists_cloud_default_ports(client, domain, custom_ports_config):
    resp = client.get(reverse('add_proxy_entry', args=[domain.pk]))
    assert resp.context['inbound_ports'] == [
        {'scheme': 'http', 'default': 8000, 'range': (8080, 8089)},
        {'scheme': 'https', 'default': 8001, 'range': None},
    ]


@pytest.mark.django_db
def test_tcp_port_check_ignores_http_entries_on_the_same_port(client, domain):
    ProxyEntry.objects.create(domain=domain, scheme='http', public_port=9000, home_port=4000, tunnel_port=2000)
    with patch('domains.views.CloudServerClient') as MockClient:
        MockClient.return_value.create_proxy_mapping.return_value = {'tunnel_port': 2001}
        resp = client.post(reverse('add_tcp_proxy_entry'), {'public_port': 9000, 'home_port': 5000})
    assert resp.status_code == 302
    assert ProxyEntry.objects.filter(scheme='tcp', public_port=9000).exists()


# --- cah.py refresh -------------------------------------------------------

def _load_cah():
    path = Path(__file__).resolve().parents[2] / 'cah.py'
    spec = importlib.util.spec_from_file_location('cah', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inbound_response(body):
    resp = MagicMock()
    resp.json.return_value = body
    return resp


def test_refresh_caches_default_port_and_range(tmp_path):
    cah = _load_cah()
    data = {'cloudlink': {'cloudserver_url': 'https://cloud.example.com', 'auth_token': 't'}}
    path = tmp_path / 'config.yaml'
    bodies = {
        'http': {'scheme': 'http', 'default_port': 8000, 'ranges': [{'port_base': 8080, 'port_count': 10}]},
        'https': {'scheme': 'https', 'default_port': 443, 'ranges': []},
    }
    with patch.object(cah.requests, 'get', side_effect=lambda url, **kw: _inbound_response(bodies[url.split('/')[-2]])):
        cah._refresh_inbound_port_ranges(data, path)
    saved = yaml.safe_load(path.read_text())['cloudlink']
    assert saved['http_ports'] == {'default': 8000, 'base': 8080, 'count': 10}
    assert saved['https_ports'] == {'default': 443}


def test_refresh_from_older_cloud_without_default_port(tmp_path):
    cah = _load_cah()
    data = {'cloudlink': {'cloudserver_url': 'https://cloud.example.com', 'auth_token': 't'}}
    path = tmp_path / 'config.yaml'
    body = {'ranges': [{'port_base': 8080, 'port_count': 10}]}
    with patch.object(cah.requests, 'get', return_value=_inbound_response(body)):
        cah._refresh_inbound_port_ranges(data, path)
    saved = yaml.safe_load(path.read_text())['cloudlink']
    assert saved['http_ports'] == {'base': 8080, 'count': 10}
