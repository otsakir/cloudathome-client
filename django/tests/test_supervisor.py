from unittest.mock import patch

import pytest

from cloudlink import hostkey
from cloudlink.services import PublicPortNotOfferedError
from domains import supervisor as sup
from domains.models import ProxyEntry
from domains.services import TunnelConnectionService
from domains.supervisor import TunnelSupervisor


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _entry(**kwargs):
    defaults = dict(scheme=ProxyEntry.SCHEME_TCP, public_port=2222, home_port=22, tunnel_port=3000)
    defaults.update(kwargs)
    return ProxyEntry.objects.create(**defaults)


@pytest.fixture
def running_pids():
    """pids is_running() reports alive; everything else is dead."""
    pids = set()
    with patch('domains.supervisor.TunnelService.is_running', side_effect=lambda pid: pid in pids):
        yield pids


@pytest.fixture
def reachable():
    with patch('domains.supervisor.cloud_reachable', return_value=True) as m:
        yield m


@pytest.fixture
def reconnect():
    with patch('domains.supervisor.TunnelConnectionService.reconnect_entry') as m:
        yield m


@pytest.mark.django_db
def test_dead_keep_open_tunnel_is_reconnected(running_pids, reachable, reconnect):
    entry = _entry(keep_open=True, tunnel_status=ProxyEntry.TUNNEL_ERROR)
    TunnelSupervisor(clock=_Clock()).check_once()
    reconnect.assert_called_once()
    assert reconnect.call_args.args[0].pk == entry.pk


@pytest.mark.django_db
def test_closed_or_alive_tunnels_are_left_alone(running_pids, reachable, reconnect):
    _entry(keep_open=False, tunnel_status=ProxyEntry.TUNNEL_CLOSED)
    _entry(public_port=2223, home_port=23, keep_open=True, tunnel_pid=42, tunnel_status=ProxyEntry.TUNNEL_OPEN)
    running_pids.add(42)
    TunnelSupervisor(clock=_Clock()).check_once()
    reconnect.assert_not_called()


@pytest.mark.django_db
def test_unwatched_dead_tunnel_is_marked_as_error(running_pids, reachable, reconnect):
    entry = _entry(keep_open=True, tunnel_pid=42, tunnel_status=ProxyEntry.TUNNEL_OPEN)
    reachable.return_value = False
    TunnelSupervisor(clock=_Clock()).check_once()
    entry.refresh_from_db()
    assert entry.tunnel_status == ProxyEntry.TUNNEL_ERROR
    assert entry.tunnel_pid is None
    assert entry.tunnel_error == 'Tunnel process is no longer running'


@pytest.mark.django_db
def test_no_attempt_while_cloud_unreachable_and_no_backoff_consumed(running_pids, reachable, reconnect):
    _entry(keep_open=True, tunnel_status=ProxyEntry.TUNNEL_ERROR)
    s = TunnelSupervisor(clock=_Clock())
    reachable.return_value = False
    for _ in range(5):
        s.check_once()
    reconnect.assert_not_called()

    reachable.return_value = True  # internet back: first attempt is immediate
    s.check_once()
    reconnect.assert_called_once()


@pytest.mark.django_db
def test_failed_attempts_back_off_exponentially_up_to_the_cap(running_pids, reachable, reconnect):
    _entry(keep_open=True, tunnel_status=ProxyEntry.TUNNEL_ERROR)
    reconnect.side_effect = RuntimeError('remote port forwarding failed')
    clock = _Clock()
    s = TunnelSupervisor(clock=clock)

    attempt_times = []
    for _ in range(400):
        before = reconnect.call_count
        s.check_once()
        if reconnect.call_count > before:
            attempt_times.append(clock.now)
        clock.now += 1
    gaps = [b - a for a, b in zip(attempt_times, attempt_times[1:])]
    assert gaps[:5] == [5, 10, 20, 40, 80]
    assert set(gaps[5:]) == {sup.BACKOFF_MAX}


@pytest.mark.django_db
def test_backoff_resets_once_tunnel_stays_up(running_pids, reachable, reconnect):
    entry = _entry(keep_open=True, tunnel_status=ProxyEntry.TUNNEL_ERROR)
    clock = _Clock()
    s = TunnelSupervisor(clock=clock)
    s.check_once()
    s.check_once()  # not due yet
    assert reconnect.call_count == 1

    ProxyEntry.objects.filter(pk=entry.pk).update(tunnel_pid=42, tunnel_status=ProxyEntry.TUNNEL_OPEN)
    running_pids.add(42)
    clock.now += sup.STABLE_AFTER
    s.check_once()
    assert entry.pk not in s._backoff

    running_pids.clear()  # drops again: retried immediately, not after a stale backoff
    s.check_once()
    assert reconnect.call_count == 2


@pytest.mark.django_db
def test_paused_while_host_key_change_awaits_a_decision(running_pids, reachable, reconnect):
    _entry(keep_open=True, tunnel_status=ProxyEntry.TUNNEL_ERROR)
    hostkey.record_host_key_change('SHA256:new')
    TunnelSupervisor(clock=_Clock()).check_once()
    reconnect.assert_not_called()


@pytest.mark.django_db
def test_entry_closed_meanwhile_is_not_revived(running_pids, reachable, reconnect):
    entry = _entry(keep_open=True, tunnel_status=ProxyEntry.TUNNEL_ERROR)
    s = TunnelSupervisor(clock=_Clock())
    ProxyEntry.objects.filter(pk=entry.pk).update(keep_open=False)
    s._revive(entry, 0)
    reconnect.assert_not_called()


@pytest.mark.django_db
def test_port_not_offered_stops_auto_reconnect():
    entry = _entry(keep_open=True)
    with patch('domains.services.CloudServerClient') as client:
        client.return_value.create_proxy_mapping.side_effect = PublicPortNotOfferedError(2222, 443, [])
        with pytest.raises(PublicPortNotOfferedError):
            TunnelConnectionService.reconnect_entry(entry)
    entry.refresh_from_db()
    assert not entry.keep_open
    assert entry.tunnel_status == ProxyEntry.TUNNEL_ERROR
    assert 'not retried automatically' in entry.tunnel_error


@pytest.mark.django_db
def test_reconnect_and_disconnect_set_keep_open():
    entry = _entry()
    with patch('domains.services.CloudServerClient') as client, \
         patch('domains.services.TunnelService.open_tunnel', return_value=4242), \
         patch('domains.services.TunnelService.close_tunnel'):
        client.return_value.create_proxy_mapping.return_value = {'tunnel_port': 3000}
        TunnelConnectionService.reconnect_entry(entry)
        entry.refresh_from_db()
        assert entry.keep_open
        TunnelConnectionService.disconnect_entry(entry)
    entry.refresh_from_db()
    assert not entry.keep_open


@pytest.mark.django_db
def test_startup_reconnect_only_touches_keep_open_entries():
    kept = _entry(keep_open=True)
    _entry(public_port=2223, home_port=23, keep_open=False)
    with patch('domains.services.TunnelConnectionService.reconnect_entry') as reconnect:
        TunnelConnectionService.reconnect_all(only_keep_open=True)
    assert [c.args[0].pk for c in reconnect.call_args_list] == [kept.pk]


def test_supervisor_not_started_outside_the_serving_process(monkeypatch):
    monkeypatch.setattr(sup, '_supervisor', None)
    monkeypatch.setenv(sup.ENABLE_ENV, '1')
    monkeypatch.delenv('RUN_MAIN', raising=False)  # e.g. a management command, or runserver's reloader parent
    sup.start_supervisor_if_enabled()
    assert sup._supervisor is None


def test_forced_startup_reconnect_happens_once_per_start(monkeypatch):
    monkeypatch.setenv(sup.START_ID_ENV, 'abc')
    assert sup._is_new_start()
    assert not sup._is_new_start()  # runserver autoreload: same start
    monkeypatch.setenv(sup.START_ID_ENV, 'def')
    assert sup._is_new_start()


def test_cloud_calls_have_a_timeout_and_network_errors_become_cloud_errors():
    """A call hanging forever during an outage would freeze the supervisor."""
    import requests
    from cloudlink.services import CloudServerClient, CloudServerError
    with patch('cloudlink.services.requests.request', side_effect=requests.ConnectTimeout('timed out')) as req:
        with pytest.raises(CloudServerError, match='unreachable'):
            CloudServerClient().delete_proxy_mapping('tcp', public_port=2222)
    assert req.call_args.kwargs['timeout']
