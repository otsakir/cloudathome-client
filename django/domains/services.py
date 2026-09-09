import datetime
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from cloudlink.config import get_config
from cloudlink.services import CloudServerClient, CloudServerError

logger = logging.getLogger(__name__)

_TUNNEL_LOG_MAX_BYTES = 5 * 1024 * 1024
_TUNNEL_LOG_BACKUP_COUNT = 3


def _tunnel_log_slug(entry):
    """Human-readable part of a tunnel's log filename -- not guaranteed unique
    on its own (a deleted domain's name could be reused), which is why callers
    prefix it with entry.pk."""
    if entry.scheme == entry.SCHEME_TCP:
        ident = f'tcp-{entry.public_port}'
    else:
        ident = f'{entry.domain.name}-{entry.scheme}'
    return re.sub(r'[^A-Za-z0-9._-]+', '-', ident).strip('-')


def _tunnel_logs_dir():
    """Resolved from the active profile's config_dir (like certbot_dir), never
    this module's own location, so concurrent profiles never share a directory."""
    d = get_config().config_dir / 'logs' / 'tunnels'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_tunnel_logger(entry):
    """One logger per ProxyEntry, writing timestamped lines to its own rotating
    file under providers/<profile>/logs/tunnels/. Looked up by name so opening
    the same entry again -- in this process or after a full restart -- appends
    to the same file instead of starting a new one. Left propagating to the
    root logger (its default), so tunnel output still also reaches the console/
    journalctl exactly as it always has -- this only adds a durable copy."""
    tlogger = logging.getLogger(f'domains.tunnel.{entry.pk}')
    if not tlogger.handlers:
        log_path = _tunnel_logs_dir() / f'{entry.pk}-{_tunnel_log_slug(entry)}.log'
        handler = RotatingFileHandler(
            log_path, maxBytes=_TUNNEL_LOG_MAX_BYTES, backupCount=_TUNNEL_LOG_BACKUP_COUNT,
        )
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        tlogger.addHandler(handler)
        tlogger.setLevel(logging.INFO)
    return tlogger


def _pump_tunnel_output(proc, tlogger, entry_pk):
    """Runs in a daemon thread for the lifetime of one tunnel process: forwards
    its (merged stdout+stderr) output line by line into tlogger, then -- once
    the pipe closes, meaning the process exited -- logs an exit marker and
    reflects the death into the database immediately, rather than leaving a
    stale 'open' status for something else to notice later."""
    try:
        for line in proc.stdout:
            tlogger.info(line.rstrip('\n'))
    except Exception:
        logger.exception('tunnel log pump crashed for pid %s', proc.pid)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            returncode = proc.poll()
        tlogger.info('--- tunnel process exited (pid %s, exit code %s) ---', proc.pid, returncode)
        _on_tunnel_process_exited(entry_pk, proc.pid, returncode)


def _on_tunnel_process_exited(entry_pk, pid, returncode):
    """Called from the pump thread the moment a tunnel's ssh process exits, for
    whatever reason -- a stale host key, the cloud restarting, the network
    dropping, an explicit kill. A conditional UPDATE (not fetch-then-save):
    only touches the row if it *still* thinks this pid is its live tunnel, so
    this can't clobber a newer state a concurrent open/close/reconnect_entry call
    already wrote (e.g. tunnel_pid was reassigned or cleared before this ran).
    If it does still match, nobody told this tunnel to stop -- it died on its
    own -- so mark it as an error instead of leaving a stale "open" status
    that would otherwise only get corrected the next time someone happens to
    load this entry's detail page.

    This is the one place to hook additional handling for an unexpected drop
    (e.g. an auto-retry, a notification/webhook) -- it already knows the pid,
    the exit code, and has the entry's pk to look up.
    """
    from domains.models import ProxyEntry
    updated = ProxyEntry.objects.filter(pk=entry_pk, tunnel_pid=pid).update(
        tunnel_pid=None, tunnel_status=ProxyEntry.TUNNEL_ERROR,
    )
    if updated:
        logger.warning(
            'Tunnel for proxy entry %s (pid %s) exited unexpectedly (exit code %s); marked as error',
            entry_pk, pid, returncode,
        )


class CertbotError(Exception):
    pass


class CertbotService:
    """Certbot state (config/work/logs) lives under the active profile's certbot_dir
    (get_config().certbot_dir), so concurrent profiles never share certbot's locks."""

    @classmethod
    def _config_dir(cls):
        return get_config().certbot_dir / 'config'

    @classmethod
    def _work_dir(cls):
        return get_config().certbot_dir / 'work'

    @classmethod
    def _logs_dir(cls):
        return get_config().certbot_dir / 'logs'

    @classmethod
    def obtain_certificate(cls, domain, email, home_port):
        """Run certbot standalone on home_port. The tunnel must already be open."""
        config_dir, work_dir, logs_dir = cls._config_dir(), cls._work_dir(), cls._logs_dir()
        for d in (config_dir, work_dir, logs_dir):
            d.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            [
                'certbot', 'certonly',
                '--standalone',
                '--non-interactive',
                '--agree-tos',
                '-m', email,
                '-d', domain.name,
                '--http-01-port', str(home_port),
                '--config-dir', str(config_dir),
                '--work-dir', str(work_dir),
                '--logs-dir', str(logs_dir),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise CertbotError(f'certbot failed (exit {proc.returncode}):\n{proc.stderr}')

        cert_path = config_dir / 'live' / domain.name / 'fullchain.pem'
        expiry = cls.check_certificate(str(cert_path))
        domain.cert_status = domain.CERT_VALID
        domain.cert_expiry = expiry
        domain.cert_path = str(cert_path)
        domain.save()

        cfg = get_config()
        if domain.deploy_path:
            raw = Path(domain.deploy_path)
            effective_deploy = raw if raw.is_absolute() else (cfg.config_dir / raw).resolve()
        else:
            effective_deploy = cfg.certbot.deploy_path  # already an absolute Path or None
        if effective_deploy:
            cls._deploy_certificates(domain.name, effective_deploy)

    @classmethod
    def _deploy_certificates(cls, domain_name, deploy_path):
        """Copy fullchain.pem and privkey.pem to deploy_path/<domain_name>/."""
        src = cls._config_dir() / 'live' / domain_name
        dst = deploy_path / domain_name
        dst.mkdir(parents=True, exist_ok=True)
        for filename in ('fullchain.pem', 'privkey.pem', 'chain.pem', 'cert.pem'):
            src_file = src / filename
            if src_file.exists():
                shutil.copy2(src_file, dst / filename)

    @classmethod
    def check_certificate(cls, cert_path):
        try:
            out = subprocess.check_output(
                ['openssl', 'x509', '-enddate', '-noout', '-in', cert_path],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            date_str = out.strip().split('=', 1)[1]
            return datetime.datetime.strptime(date_str, '%b %d %H:%M:%S %Y %Z').replace(
                tzinfo=datetime.timezone.utc
            )
        except Exception:
            return None


class TunnelService:

    @staticmethod
    def is_running(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    @staticmethod
    def open_tunnel(entry):
        """Open the SSH tunnel for one ProxyEntry. Its output (merged
        stdout+stderr) is streamed into a dedicated, timestamped logfile under
        providers/<profile>/logs/tunnels/ -- see _get_tunnel_logger -- in
        addition to reaching the console/journalctl exactly as before."""
        cfg = get_config()
        home_host = entry.home_host
        if home_host not in ('localhost', '127.0.0.1', '::1') and not cfg.features.lan_forwarding:
            raise PermissionError(
                f'Tunnel to home network host "{home_host}" is blocked: '
                'enable features.lan_forwarding in config.yaml to allow it.'
            )
        proc = subprocess.Popen(
            [
                'ssh', '-N',
                '-R', f'{entry.tunnel_port}:{home_host}:{entry.home_port}',
                '-i', str(cfg.ssh.private_key_path),
                '-o', 'StrictHostKeyChecking=accept-new',
                '-o', 'ServerAliveInterval=30',
                '-o', 'ExitOnForwardFailure=yes',
                '-p', str(cfg.ssh.port),
                f'{cfg.ssh.username}@{cfg.ssh.host}',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        tlogger = _get_tunnel_logger(entry)
        tlogger.info('--- tunnel opened (pid %s) ---', proc.pid)
        threading.Thread(target=_pump_tunnel_output, args=(proc, tlogger, entry.pk), daemon=True).start()
        return proc.pid

    @staticmethod
    def is_home_port_open(home_host: str, home_port: int) -> bool | None:
        """Check if home_port is listening. Returns None for non-localhost targets."""
        if home_host not in ('localhost', '127.0.0.1', '::1'):
            return None
        import psutil
        return any(
            c.laddr.port == home_port and c.status == psutil.CONN_LISTEN
            for c in psutil.net_connections(kind='tcp')
        )

    @staticmethod
    def close_tunnel(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # process already gone, nothing to do


class TunnelConnectionService:

    @staticmethod
    def reconnect_entry(entry):
        """Tear down and re-establish both the cloud mapping and the tunnel for one
        entry. Always forces a fresh tunnel rather than trusting a still-running
        local ssh process: a process can outlive the cloud restarting under it
        (it takes ServerAliveInterval*ServerAliveCountMax to notice a dead
        connection), so "still running" doesn't mean "still connected to the
        current cloud instance." Idempotent."""
        from domains.models import ProxyEntry
        client = CloudServerClient()

        if entry.tunnel_pid:
            TunnelService.close_tunnel(entry.tunnel_pid)
            entry.tunnel_pid = None

        # Remove any stale cloud mapping before re-creating it.
        try:
            if entry.scheme == ProxyEntry.SCHEME_TCP:
                client.delete_proxy_mapping('tcp', public_port=entry.public_port)
            else:
                client.delete_proxy_mapping(entry.scheme, host=entry.domain.name, public_port=entry.public_port)
        except CloudServerError as e:
            logger.info('reconnect_entry %r: no stale cloud mapping to remove (%s)', entry, e)

        try:
            if entry.scheme == ProxyEntry.SCHEME_TCP:
                result = client.create_proxy_mapping('tcp', public_port=entry.public_port)
            else:
                # Preserve this entry's existing public_port -- a host may have
                # independent mappings at more than one port, so recreating
                # without it would silently collapse back to the scheme
                # default instead of the port this entry actually uses.
                result = client.create_proxy_mapping(entry.scheme, host=entry.domain.name, public_port=entry.public_port)
            entry.tunnel_port = result['tunnel_port']
        except CloudServerError:
            entry.tunnel_status = ProxyEntry.TUNNEL_ERROR
            entry.save()
            raise

        try:
            pid = TunnelService.open_tunnel(entry)
            entry.tunnel_pid = pid
        except Exception:
            entry.tunnel_status = ProxyEntry.TUNNEL_ERROR
            entry.save()
            raise

        entry.tunnel_status = ProxyEntry.TUNNEL_OPEN
        entry.save()

    @staticmethod
    def reconnect_all():
        """Reconnect every ProxyEntry. Returns (succeeded, failed) counts."""
        from domains.models import ProxyEntry
        entries = list(ProxyEntry.objects.select_related('domain').all())
        succeeded = 0
        failed = 0
        for entry in entries:
            try:
                TunnelConnectionService.reconnect_entry(entry)
                succeeded += 1
            except Exception:
                logger.exception('reconnect_all: failed to reconnect entry %r', entry)
                failed += 1
        return succeeded, failed

    @staticmethod
    def disconnect_entry(entry):
        """Close tunnel + remove cloud mapping for one entry."""
        from domains.models import ProxyEntry
        if entry.tunnel_pid:
            TunnelService.close_tunnel(entry.tunnel_pid)
        client = CloudServerClient()
        try:
            if entry.scheme == ProxyEntry.SCHEME_TCP:
                client.delete_proxy_mapping('tcp', public_port=entry.public_port)
            else:
                client.delete_proxy_mapping(entry.scheme, host=entry.domain.name, public_port=entry.public_port)
        except CloudServerError as e:
            logger.info('disconnect_entry %r: no cloud mapping to remove (%s)', entry, e)
        entry.tunnel_pid = None
        entry.tunnel_status = ProxyEntry.TUNNEL_CLOSED
        entry.save()

    @staticmethod
    def disconnect_all():
        """Close tunnels and remove cloud mappings for every ProxyEntry."""
        from domains.models import ProxyEntry
        for entry in ProxyEntry.objects.select_related('domain').all():
            TunnelConnectionService.disconnect_entry(entry)


