"""The cloud server's SSH host key, as trusted by this profile.

Each profile pins its cloud's host key in its own known_hosts file
(providers/<profile>/known_hosts) rather than the user's ~/.ssh/known_hosts,
so resetting trust only ever touches a file this client owns.

When a tunnel's ssh process reports that the host key changed (typically the
cloud was rebuilt/redeployed with fresh keys), the new fingerprint is recorded
in a marker file next to it. The Home Console shows it as a banner until the
user explicitly resets trust -- never automatically, since silently accepting
a changed key would defeat host key checking.
"""
import datetime
import json
import logging
import re
import subprocess

from cloudlink.config import get_config

logger = logging.getLogger(__name__)

_CHANGED_MARKER = 'REMOTE HOST IDENTIFICATION HAS CHANGED'
_FINGERPRINT_RE = re.compile(r'^(SHA256:[A-Za-z0-9+/=]+?)\.?$')


def known_hosts_path():
    return get_config().config_dir / 'known_hosts'


def _change_marker_path():
    return get_config().config_dir / 'host_key_changed.json'


def ssh_host_spec():
    """The cloud's ssh host as known_hosts names it: bare for port 22, [host]:port otherwise."""
    ssh = get_config().ssh
    return ssh.host if int(ssh.port) == 22 else f'[{ssh.host}]:{ssh.port}'


class HostKeyChangeDetector:
    """Fed a tunnel's ssh output line by line; notices the "host identification
    has changed" warning and the new fingerprint ssh prints along with it."""

    def __init__(self):
        self.changed = False
        self.fingerprint = None

    def feed(self, line):
        line = line.strip()
        if _CHANGED_MARKER in line:
            self.changed = True
        elif self.changed and self.fingerprint is None:
            m = _FINGERPRINT_RE.match(line)
            if m:
                self.fingerprint = m.group(1)

    def error_message(self):
        new_key = f' (new key: {self.fingerprint})' if self.fingerprint else ''
        return (
            f"The cloud server's SSH host key has changed{new_key}. This is expected if the "
            'cloud server was rebuilt or redeployed; otherwise it may indicate the connection '
            'is being intercepted. Compare it with the fingerprint shown on the cloud '
            'dashboard, then use "Trust new host key" on the Home Console dashboard '
            '(or: cah.py reset-host-key <profile>).'
        )


def record_host_key_change(fingerprint):
    data = {
        'host': ssh_host_spec(),
        'fingerprint': fingerprint,
        'detected_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
    }
    _change_marker_path().write_text(json.dumps(data))


def get_host_key_change():
    """The recorded host key change ({host, fingerprint, detected_at}), or None."""
    try:
        return json.loads(_change_marker_path().read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.exception('Unreadable host key change marker')
        return None


def trusted_fingerprints():
    """[(key_type, fingerprint), ...] currently trusted for the cloud's ssh host."""
    path = known_hosts_path()
    if not path.exists():
        return []
    proc = subprocess.run(
        ['ssh-keygen', '-l', '-F', ssh_host_spec(), '-f', str(path)],
        capture_output=True, text=True,
    )
    result = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if line.startswith('#') or len(parts) < 3:
            continue
        result.append((parts[1], parts[2]))
    return result


def reset_host_key():
    """Forget the trusted host key and the recorded change. The next tunnel
    accepts and pins whatever key the cloud presents (StrictHostKeyChecking=accept-new).
    The known_hosts file only ever holds this profile's cloud, so it's removed
    outright. Returns the fingerprints that were trusted until now."""
    previous = trusted_fingerprints()
    known_hosts_path().unlink(missing_ok=True)
    _change_marker_path().unlink(missing_ok=True)
    return previous
