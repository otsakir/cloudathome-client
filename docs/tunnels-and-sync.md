# Tunnels, sync, and bandwidth

## How tunnels work

Tunnels are OS-level SSH processes (`ssh -R <tunnel_port>:<home_host>:<home_port>
...`). Their PIDs are stored in the database so they can be stopped cleanly even
after a Django restart. If a tunnel process dies unexpectedly, the status is
corrected automatically the next time the proxy entry page is loaded.

SSH process output (stdout/stderr) is inherited from the Django process and
appears directly in the Home Console's terminal. For example, if the local
service is not yet listening on its port, you will see repeated
`connect_to localhost port <N>: failed.` lines — these come from SSH, not
Django.

By default a tunnel targets `localhost` on the home machine. To target another
host on your home network instead, set `features.lan_forwarding: true` in
`config.yaml` first — see
[configuration.md](configuration.md#profile-configyaml). With it left off (the
default), `home_host` is silently forced back to `localhost` regardless of what
a form submits.

## Per-entry controls

From a proxy entry's detail page:

- **Open tunnel / Close tunnel** — manually open or close a single tunnel.
- **Sync** — force-reconnect: tears down and re-establishes both the cloud
  mapping and the SSH tunnel for this entry, even if the local tunnel process
  still looks alive (it may be a stale connection to a cloud server that has
  since restarted). Use this to recover a single entry after a crash or restart.

## Global controls

From the dashboard:

- **Connect all** — syncs every proxy entry at once. `python cah.py start`
  already does this automatically on every launch (skip with `--no-sync`); use
  this button to reconnect without restarting the console.
- **Disconnect all** — closes all tunnels and removes all cloud proxy mappings
  cleanly.

## Management command

The same sync operations are available from the command line:

```bash
# Sync all entries
python manage.py sync_tunnels

# Sync one entry by domain name
python manage.py sync_tunnels --domain mysite.example.com

# Disconnect all entries
python manage.py sync_tunnels --disconnect

# Disconnect one entry
python manage.py sync_tunnels --domain mysite.example.com --disconnect
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
