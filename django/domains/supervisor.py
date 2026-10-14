"""Keeps the tunnels the user opened (ProxyEntry.keep_open) actually open.

A single daemon thread inside the Home Console process. Every CHECK_INTERVAL
seconds -- or right away when a tunnel's pump thread reports an exit (see
domains.services._on_tunnel_process_exited) -- it looks for keep_open entries
whose ssh process is gone and reconnects them via reconnect_entry (cloud
mapping included), with a per-entry exponential backoff.

Checking the process itself, not just what the database says, also covers
tunnels this process didn't open and so has no pump thread watching (e.g.
left over from before an autoreload).

Retries are skipped while:
- the cloud's ssh port is unreachable (typically the home's internet is down):
  nothing to gain, and it keeps the logs to one line per outage;
- the cloud's host key has changed (cloudlink.hostkey): every attempt would be
  refused until the user decides whether to trust the new key.
Failures retrying can't fix clear keep_open instead (see reconnect_entry).
"""
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass

from django.apps import apps
from django.db import close_old_connections

from cloudlink import hostkey
from cloudlink.config import get_config
from domains.services import TunnelConnectionService, TunnelService, entry_lock

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 10
BACKOFF_INITIAL = 5
BACKOFF_MAX = 120
# A reconnected tunnel still alive this long after the attempt counts as
# recovered, resetting its backoff. Not reset on reconnect_entry merely
# returning: ssh can still exit seconds later (e.g. the cloud still holds the
# old session's port, so the -R forward fails).
STABLE_AFTER = 60
CLOUD_PROBE_TIMEOUT = 5

# Set by cah.py start (unless --no-reconnect) on the runserver it execs.
ENABLE_ENV = 'CAH_TUNNEL_SUPERVISOR'
# Unique per `cah.py start` run, so the forced reconnect of every keep_open
# entry happens once per start -- not again on each runserver autoreload.
START_ID_ENV = 'CAH_START_ID'

_supervisor = None


def wake_supervisor():
    if _supervisor is not None:
        _supervisor.wake()


def start_supervisor_if_enabled():
    """Called from DomainsConfig.ready(). Only in the process actually serving
    (runserver's autoreloader child), never in management commands or tests."""
    global _supervisor
    if os.environ.get(ENABLE_ENV) != '1' or os.environ.get('RUN_MAIN') != 'true':
        return
    if _supervisor is None:
        _supervisor = TunnelSupervisor()
        _supervisor.start(reconnect_first=_is_new_start())


def _is_new_start():
    start_id = os.environ.get(START_ID_ENV)
    if not start_id:
        return True
    marker = get_config().config_dir / '.supervisor_start_id'
    try:
        if marker.read_text() == start_id:
            return False
    except OSError:
        pass
    marker.write_text(start_id)
    return True


def cloud_reachable():
    ssh = get_config().ssh
    try:
        with socket.create_connection((ssh.host, int(ssh.port)), timeout=CLOUD_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


@dataclass
class _Backoff:
    attempts: int = 0
    next_at: float = 0.0
    last_attempt: float = 0.0


class TunnelSupervisor:

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._wake = threading.Event()
        self._backoff = {}
        self._cloud_down = False

    def start(self, reconnect_first):
        threading.Thread(
            target=self._run, args=(reconnect_first,), name='tunnel-supervisor', daemon=True,
        ).start()

    def wake(self):
        self._wake.set()

    def _run(self, reconnect_first):
        while not apps.ready:  # started from ready(), possibly before every app is
            time.sleep(0.1)
        if reconnect_first:
            # Forced, not just "revive the dead ones": a still-running ssh may be
            # connected to a cloud instance that has since restarted.
            try:
                succeeded, failed = TunnelConnectionService.reconnect_all(only_keep_open=True)
                logger.info('Startup reconnect: %s connected, %s failed (retried by the supervisor)',
                            succeeded, failed)
            except Exception:
                logger.exception('Startup reconnect failed')
        while True:
            self._wake.wait(CHECK_INTERVAL)
            self._wake.clear()
            close_old_connections()
            try:
                self.check_once()
            except Exception:
                logger.exception('Tunnel supervisor check failed')

    def check_once(self):
        from domains.models import ProxyEntry

        if hostkey.get_host_key_change():
            return

        now = self._clock()
        kept = list(ProxyEntry.objects.filter(keep_open=True))
        for pk in set(self._backoff) - {e.pk for e in kept}:
            del self._backoff[pk]

        down = []
        for entry in kept:
            if entry.tunnel_pid and TunnelService.is_running(entry.tunnel_pid):
                state = self._backoff.get(entry.pk)
                if state and now - state.last_attempt >= STABLE_AFTER:
                    del self._backoff[entry.pk]
                continue
            down.append(entry)
            if entry.tunnel_status == ProxyEntry.TUNNEL_OPEN:
                # Died without a pump thread here to notice (not opened by this process).
                ProxyEntry.objects.filter(pk=entry.pk, tunnel_pid=entry.tunnel_pid).update(
                    tunnel_pid=None, tunnel_status=ProxyEntry.TUNNEL_ERROR,
                    tunnel_error='Tunnel process is no longer running',
                )

        due = [e for e in down if now >= self._backoff.get(e.pk, _Backoff()).next_at]
        if not due:
            return

        if not cloud_reachable():
            if not self._cloud_down:
                ssh = get_config().ssh
                logger.warning('Cloud server %s:%s unreachable; %d tunnel(s) down, will reconnect once it is back',
                               ssh.host, ssh.port, len(down))
                self._cloud_down = True
            return
        if self._cloud_down:
            logger.info('Cloud server reachable again; reconnecting %d tunnel(s)', len(due))
            self._cloud_down = False

        for entry in due:
            self._revive(entry, now)

    def _revive(self, entry, now):
        from domains.models import ProxyEntry
        with entry_lock(entry.pk):
            try:
                entry.refresh_from_db()
            except ProxyEntry.DoesNotExist:
                self._backoff.pop(entry.pk, None)
                return
            # The user may have closed it, or reconnected it themselves, meanwhile.
            if not entry.keep_open or (entry.tunnel_pid and TunnelService.is_running(entry.tunnel_pid)):
                return
            state = self._backoff.setdefault(entry.pk, _Backoff())
            state.attempts += 1
            state.last_attempt = now
            delay = min(BACKOFF_INITIAL * 2 ** (state.attempts - 1), BACKOFF_MAX)
            state.next_at = now + delay
            logger.info('Reconnecting tunnel for %r (attempt %d)', entry, state.attempts)
            try:
                TunnelConnectionService.reconnect_entry(entry)
            except Exception as e:
                logger.warning('Reconnect attempt %d for %r failed: %s; next attempt in %ds',
                               state.attempts, entry, e, delay)
