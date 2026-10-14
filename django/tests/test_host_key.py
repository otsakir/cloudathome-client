import json
from unittest.mock import patch

import pytest
from django.urls import reverse

from cloudlink import hostkey
from cloudlink.hostkey import HostKeyChangeDetector
from domains.models import Domain, ProxyEntry
from domains.services import TunnelService
from tests.test_tunnel_logging import _CapturingThread, _FakeProcess, tunnel_logger_cleanup  # noqa: F401

# Verbatim ssh output for a cloud rebuilt with fresh host keys.
_HOST_KEY_CHANGED_OUTPUT = """\
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!
Someone could be eavesdropping on you right now (man-in-the-middle attack)!
It is also possible that a host key has just been changed.
The fingerprint for the ED25519 key sent by the remote host is
SHA256:x4wg1CJEZ1LxduxGwosoqcEqteTbmzrPY5V6furx8r4.
Please contact your system administrator.
Add correct host key in /home/user/.ssh/known_hosts to get rid of this message.
Offending ECDSA key in /home/user/.ssh/known_hosts:59
  remove with:
  ssh-keygen -f '/home/user/.ssh/known_hosts' -R '[localhost]:8022'
Host key for [localhost]:8022 has changed and you have requested strict checking.
Host key verification failed.
""".splitlines(keepends=True)


def test_detector_extracts_new_fingerprint():
    d = HostKeyChangeDetector()
    for line in _HOST_KEY_CHANGED_OUTPUT:
        d.feed(line)
    assert d.changed
    assert d.fingerprint == 'SHA256:x4wg1CJEZ1LxduxGwosoqcEqteTbmzrPY5V6furx8r4'
    assert d.fingerprint in d.error_message()


def test_detector_ignores_unrelated_output():
    d = HostKeyChangeDetector()
    for line in ['connect_to localhost port 8080: failed.\n', 'SHA256:abc\n']:
        d.feed(line)
    assert not d.changed
    assert d.fingerprint is None


def test_ssh_host_spec_brackets_non_default_port():
    from cloudlink.config import get_config
    assert hostkey.ssh_host_spec() == 'cloud.example.com'  # conftest's port is 22
    get_config().ssh.port = 8022
    assert hostkey.ssh_host_spec() == '[cloud.example.com]:8022'


@pytest.mark.django_db
def test_open_tunnel_uses_per_profile_known_hosts(tunnel_logger_cleanup):
    entry = ProxyEntry.objects.create(
        scheme=ProxyEntry.SCHEME_TCP, public_port=2222, home_port=22, tunnel_port=3000,
    )
    tunnel_logger_cleanup.append(f'domains.tunnel.{entry.pk}')
    with patch('domains.services.subprocess.Popen', return_value=_FakeProcess(1, [])) as popen, \
         patch('domains.services.threading.Thread', _CapturingThread):
        TunnelService.open_tunnel(entry)
    cmd = popen.call_args.args[0]
    assert f'UserKnownHostsFile={hostkey.known_hosts_path()}' in cmd


@pytest.mark.django_db
def test_host_key_change_is_recorded_on_entry_and_profile(tunnel_logger_cleanup):
    domain = Domain.objects.create(name='mysite.example.com')
    entry = ProxyEntry.objects.create(
        domain=domain, scheme=ProxyEntry.SCHEME_HTTP, home_port=8080, tunnel_port=2000,
    )
    tunnel_logger_cleanup.append(f'domains.tunnel.{entry.pk}')

    with patch('domains.services.subprocess.Popen', return_value=_FakeProcess(4242, _HOST_KEY_CHANGED_OUTPUT)), \
         patch('domains.services.threading.Thread', _CapturingThread):
        entry.tunnel_pid = TunnelService.open_tunnel(entry)
    entry.tunnel_status = ProxyEntry.TUNNEL_OPEN
    entry.save()
    _CapturingThread.instances[-1].run()

    entry.refresh_from_db()
    assert entry.tunnel_status == ProxyEntry.TUNNEL_ERROR
    assert 'host key has changed' in entry.tunnel_error
    change = hostkey.get_host_key_change()
    assert change['fingerprint'] == 'SHA256:x4wg1CJEZ1LxduxGwosoqcEqteTbmzrPY5V6furx8r4'
    assert change['host'] == 'cloud.example.com'


@pytest.mark.django_db
def test_other_unexpected_exit_gets_generic_error(tunnel_logger_cleanup):
    entry = ProxyEntry.objects.create(
        scheme=ProxyEntry.SCHEME_TCP, public_port=2222, home_port=22, tunnel_port=3000,
    )
    tunnel_logger_cleanup.append(f'domains.tunnel.{entry.pk}')
    with patch('domains.services.subprocess.Popen', return_value=_FakeProcess(7, ['Connection refused\n'])), \
         patch('domains.services.threading.Thread', _CapturingThread):
        entry.tunnel_pid = TunnelService.open_tunnel(entry)
    entry.tunnel_status = ProxyEntry.TUNNEL_OPEN
    entry.save()
    _CapturingThread.instances[-1].run()

    entry.refresh_from_db()
    assert entry.tunnel_error == 'Tunnel process exited unexpectedly (exit code 255)'
    assert hostkey.get_host_key_change() is None


def test_reset_host_key_removes_known_hosts_and_marker(tmp_path):
    hostkey.known_hosts_path().write_text('cloud.example.com ssh-ed25519 AAAA\n')
    hostkey.record_host_key_change('SHA256:new')
    hostkey.reset_host_key()
    assert not hostkey.known_hosts_path().exists()
    assert hostkey.get_host_key_change() is None


def test_trusted_fingerprints_reads_profile_known_hosts(tmp_path):
    import subprocess
    key = tmp_path / 'k'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    hostkey.known_hosts_path().write_text('cloud.example.com ' + (tmp_path / 'k.pub').read_text())
    [(key_type, fp)] = hostkey.trusted_fingerprints()
    assert key_type == 'ED25519'
    assert fp.startswith('SHA256:')


@pytest.mark.django_db
def test_dashboard_shows_banner_and_reset_reconnects(client):
    hostkey.record_host_key_change('SHA256:newkey')
    with patch('cloudlink.views.CloudServerClient') as cloud:
        cloud.return_value.get_home.return_value = {}
        resp = client.get(reverse('dashboard'))
    assert b'SSH host key has changed' in resp.content
    assert b'SHA256:newkey' in resp.content

    with patch('cloudlink.views.TunnelConnectionService.reconnect_all', return_value=(2, 0)) as reconnect:
        resp = client.post(reverse('reset_host_key'))
    assert resp.status_code == 302
    reconnect.assert_called_once()
    assert hostkey.get_host_key_change() is None
