# Tunnels, reconnecting, and bandwidth

## How tunnels work

Tunnels are OS-level SSH processes (`ssh -R <tunnel_port>:<home_host>:<home_port>
...`). Their PIDs are stored in the database so they can be stopped cleanly even
after a Django restart. If a tunnel process dies unexpectedly (for any reason —
a stale SSH host key, the cloud restarting, a network drop, ...), that's
detected and reflected into the database (status flips to `error`, the stored
pid is cleared) the moment it happens, not just the next time you happen to
load that proxy entry's page — a background thread started alongside the
tunnel notices the process exit directly and also logs a warning at that
point. That's the one place (`domains.services._on_tunnel_process_exited`) to
hook further handling of an unexpected drop, if you want one (auto-retry,
a notification, etc.).

SSH process output (stdout/stderr) still reaches the Home Console's own
terminal/`journalctl` exactly as before — for example, if the local service is
not yet listening on its port, you will see repeated
`connect_to localhost port <N>: failed.` lines, which come from SSH, not
Django. In addition, each tunnel now also logs to its own timestamped,
rotating file at `providers/<profile>/logs/tunnels/<entry-id>-<slug>.log`
(e.g. `7-mysite.example.com-https.log`, or `12-tcp-2222.log` for a TCP
forward) — rotated at ~5MB with 3 backups kept. The file accumulates across
reconnects and process restarts, so it's the place to look when investigating
why a tunnel dropped, rather than relying on having watched the console live.

By default a tunnel targets `localhost` on the home machine. To target another
host on your home network instead, set `features.lan_forwarding: true` in
`config.yaml` first — see
[configuration.md](configuration.md#profile-configyaml). With it left off (the
default), `home_host` is silently forced back to `localhost` regardless of what
a form submits.

## Per-entry controls

From a proxy entry's detail page:

- **Open tunnel / Close tunnel** — manually open or close a single tunnel.
- **Reconnect** — force-reconnect: tears down and re-establishes both the cloud
  mapping and the SSH tunnel for this entry, even if the local tunnel process
  still looks alive (it may be a stale connection to a cloud server that has
  since restarted). Use this to recover a single entry after a crash or restart.

## Global controls

From the dashboard:

- **Connect all** — reconnects every proxy entry at once. `python cah.py start`
  already does this automatically on every launch (skip with `--no-reconnect`);
  use this button to reconnect without restarting the console.
- **Disconnect all** — closes all tunnels and removes all cloud proxy mappings
  cleanly.

## Management command

The same reconnect/disconnect operations are available from the command line:

```bash
# Reconnect all entries
python manage.py reconnect_tunnels

# Reconnect one entry by domain name
python manage.py reconnect_tunnels --domain mysite.example.com

# Disconnect all entries
python manage.py reconnect_tunnels --disconnect

# Disconnect one entry
python manage.py reconnect_tunnels --domain mysite.example.com --disconnect
```

## Bandwidth throttling

Per-home bandwidth limits cap how much of the home's internet upload the cloud
tunnel can consume, enforced on the cloud server (see the cloud repo for how). A
home owner sets or clears their own limit from the dashboard, or directly via
the API:

```
PATCH /api/homes/<slug>/
{"bandwidth_limit_kbps": 5000}   # set to 5 Mbit/s
{"bandwidth_limit_kbps": null}   # remove limit (unlimited)
```

Accepted range: 100 – 10,000,000 kbps. `null` means unlimited.
