import logging
import re
from unittest.mock import patch

import pytest

from domains.models import Domain, ProxyEntry
from domains.services import TunnelService, _tunnel_log_slug, _tunnel_logs_dir


@pytest.mark.django_db
def test_tunnel_log_slug_for_http_and_https_entries():
    domain = Domain.objects.create(name='mysite.example.com')
    http_entry = ProxyEntry.objects.create(
        domain=domain, scheme=ProxyEntry.SCHEME_HTTP, home_port=8080, tunnel_port=2000,
    )
    https_entry = ProxyEntry.objects.create(
        domain=domain, scheme=ProxyEntry.SCHEME_HTTPS, home_port=8443, tunnel_port=2001,
    )
    assert _tunnel_log_slug(http_entry) == 'mysite.example.com-http'
    assert _tunnel_log_slug(https_entry) == 'mysite.example.com-https'


@pytest.mark.django_db
def test_tunnel_log_slug_for_tcp_entry():
    entry = ProxyEntry.objects.create(
        scheme=ProxyEntry.SCHEME_TCP, public_port=2222, home_port=22, tunnel_port=3000,
    )
    assert _tunnel_log_slug(entry) == 'tcp-2222'


class _FakeProcess:
    """Minimal stand-in for the subprocess.Popen object open_tunnel launches."""

    def __init__(self, pid, lines):
        self.pid = pid
        self.stdout = iter(lines)

    def wait(self, timeout=None):
        return 0


class _SyncThread:
    """Stand-in for threading.Thread that runs its target synchronously, so
    the test doesn't have to race a real background thread."""

    def __init__(self, target, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


@pytest.mark.django_db
def test_open_tunnel_writes_timestamped_log_file():
    domain = Domain.objects.create(name='mysite.example.com')
    entry = ProxyEntry.objects.create(
        domain=domain, scheme=ProxyEntry.SCHEME_HTTP, home_port=8080, tunnel_port=2000,
    )
    # The logger is keyed by entry.pk and cached process-wide by the stdlib
    # logging module -- clear out any handler a prior test run left behind
    # (e.g. pointing at a since-deleted tmp_path) before exercising it here.
    tlogger_name = f'domains.tunnel.{entry.pk}'
    logging.getLogger(tlogger_name).handlers.clear()

    fake_lines = ['Warning: Permanently added...\n', 'connect_to localhost port 8080: failed.\n']
    fake_proc = _FakeProcess(pid=12345, lines=fake_lines)

    try:
        with patch('domains.services.subprocess.Popen', return_value=fake_proc), \
             patch('domains.services.threading.Thread', _SyncThread):
            pid = TunnelService.open_tunnel(entry)

        assert pid == 12345

        log_files = list(_tunnel_logs_dir().glob(f'{entry.pk}-*.log'))
        assert len(log_files) == 1
        log_file = log_files[0]
        assert log_file.name == f'{entry.pk}-mysite.example.com-http.log'

        content = log_file.read_text()
        timestamp_re = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ')
        lines = content.splitlines()
        assert lines, 'expected the tunnel log file to contain lines'
        assert all(timestamp_re.match(line) for line in lines)
        assert any('tunnel opened (pid 12345)' in line for line in lines)
        assert any('Warning: Permanently added' in line for line in lines)
        assert any('connect_to localhost port 8080: failed.' in line for line in lines)
        assert any('tunnel process exited (pid 12345, exit code 0)' in line for line in lines)
    finally:
        # Avoid leaking file handles/handlers into later tests.
        for handler in logging.getLogger(tlogger_name).handlers[:]:
            handler.close()
        logging.getLogger(tlogger_name).handlers.clear()
