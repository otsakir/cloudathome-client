import logging
import re
from unittest.mock import patch

import pytest

from domains.models import Domain, ProxyEntry
from domains.services import TunnelService, _tunnel_log_slug, _tunnel_logs_dir


@pytest.fixture
def tunnel_logger_cleanup():
    """The per-tunnel logger is keyed by entry.pk and cached process-wide by
    the stdlib logging module -- register its name here so its handlers (and
    the file they hold open) get torn down after the test, instead of leaking
    into later tests (e.g. still pointing at a since-deleted tmp_path)."""
    names = []
    yield names
    for name in names:
        tlogger = logging.getLogger(name)
        for handler in tlogger.handlers[:]:
            handler.close()
        tlogger.handlers.clear()


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
        return 255


class _SyncThread:
    """Stand-in for threading.Thread that runs its target synchronously, so
    the test doesn't have to race a real background thread."""

    def __init__(self, target, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


class _CapturingThread:
    """Stand-in for threading.Thread that captures its target instead of
    running it, so a test can invoke the pump/exit-handling at a precise
    point -- e.g. only after simulating what the real caller does right after
    open_tunnel() returns (set tunnel_pid/status, save) -- rather than have it
    run inline during open_tunnel() itself, which would get the ordering
    backwards versus production."""

    instances = []

    def __init__(self, target, args=(), daemon=None):
        self._target = target
        self._args = args
        _CapturingThread.instances.append(self)

    def start(self):
        pass

    def run(self):
        self._target(*self._args)


@pytest.mark.django_db
def test_open_tunnel_writes_timestamped_log_file(tunnel_logger_cleanup):
    domain = Domain.objects.create(name='mysite.example.com')
    entry = ProxyEntry.objects.create(
        domain=domain, scheme=ProxyEntry.SCHEME_HTTP, home_port=8080, tunnel_port=2000,
    )
    tunnel_logger_cleanup.append(f'domains.tunnel.{entry.pk}')

    fake_lines = ['Warning: Permanently added...\n', 'connect_to localhost port 8080: failed.\n']
    fake_proc = _FakeProcess(pid=12345, lines=fake_lines)

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
    assert any('tunnel process exited (pid 12345, exit code 255)' in line for line in lines)


@pytest.mark.django_db
def test_tunnel_death_is_reflected_in_db_without_viewing_any_page(tunnel_logger_cleanup):
    """The bug this guards against: a tunnel that fails right after opening
    (e.g. a host key mismatch) used to leave the DB saying 'open' forever,
    since the only place that ever corrected it was the proxy entry detail
    page's lazy is_running() check. The pump thread's exit-handling must fix
    this proactively instead."""
    domain = Domain.objects.create(name='mysite.example.com')
    entry = ProxyEntry.objects.create(
        domain=domain, scheme=ProxyEntry.SCHEME_HTTP, home_port=8080, tunnel_port=2000,
    )
    tunnel_logger_cleanup.append(f'domains.tunnel.{entry.pk}')

    fake_proc = _FakeProcess(pid=99999, lines=['Host key verification failed.\n'])

    with patch('domains.services.subprocess.Popen', return_value=fake_proc), \
         patch('domains.services.threading.Thread', _CapturingThread):
        pid = TunnelService.open_tunnel(entry)

    # Mirror exactly what a real caller (TunnelToggleView /
    # TunnelConnectionService.reconnect_entry) does right after open_tunnel()
    # returns: optimistically record it as open.
    entry.tunnel_pid = pid
    entry.tunnel_status = ProxyEntry.TUNNEL_OPEN
    entry.save()

    # Only now does the process's exit actually get processed.
    _CapturingThread.instances[-1].run()

    entry.refresh_from_db()
    assert entry.tunnel_status == ProxyEntry.TUNNEL_ERROR
    assert entry.tunnel_pid is None


@pytest.mark.django_db
def test_stale_exit_handler_does_not_clobber_a_newer_tunnel(tunnel_logger_cleanup):
    """If the entry has since been reconnected with a new pid (e.g. a
    reconnect raced ahead of the old process actually dying), a late
    exit-handler for the *old* pid must be a no-op, not stomp the newer
    state."""
    domain = Domain.objects.create(name='mysite.example.com')
    entry = ProxyEntry.objects.create(
        domain=domain, scheme=ProxyEntry.SCHEME_HTTP, home_port=8080, tunnel_port=2000,
    )
    tunnel_logger_cleanup.append(f'domains.tunnel.{entry.pk}')

    fake_proc = _FakeProcess(pid=1111, lines=[])

    with patch('domains.services.subprocess.Popen', return_value=fake_proc), \
         patch('domains.services.threading.Thread', _CapturingThread):
        TunnelService.open_tunnel(entry)
    old_thread = _CapturingThread.instances[-1]

    entry.tunnel_pid = 2222
    entry.tunnel_status = ProxyEntry.TUNNEL_OPEN
    entry.save()

    old_thread.run()  # the stale exit-handler for pid 1111 fires late

    entry.refresh_from_db()
    assert entry.tunnel_pid == 2222
    assert entry.tunnel_status == ProxyEntry.TUNNEL_OPEN
