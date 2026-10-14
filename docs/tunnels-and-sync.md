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
hook further handling of an unexpected drop, if you want one (a notification,
etc.) — retrying is already handled, see below.

## Automatic reconnect

The Home Console keeps tunnels up on its own: if one drops — most commonly the
home's internet going down — it's reconnected once that's possible again,
without anyone clicking anything.

- **What it keeps open**: each proxy entry records whether to keep it open
  (`keep_open`). Opening
  it, **Reconnect**, or **Connect all** turns that on; **Close tunnel** or
  **Disconnect all** turns it off. A tunnel you closed is never reopened
  behind your back, also not on the next `cah.py start`.
- **How**: a supervisor thread (`domains/supervisor.py`) checks every 10s — and
  immediately when a tunnel process exits — for `keep_open` entries whose ssh
  process is gone, and runs the same full reconnect as the **Reconnect** button
  (cloud mapping included). It checks the process itself, not just the stored
  status, so it also covers tunnels this console didn't open itself.
- **Backoff**: per entry, 5s, 10s, 20s, … capped at 2 minutes between failed
  attempts; reset once a reconnected tunnel has stayed up for a minute.
- **While the internet is down**: before retrying it checks whether the cloud's
  SSH port is reachable at all; until it is, it just waits (logging once per
  outage). The first attempt after it's back is immediate.
- **Not retried** (shown on the entry, reconnect it yourself once fixed): the
  cloud no longer offering the entry's public port, and a LAN target with
  `features.lan_forwarding` off. Everything is paused while a [changed host
  key](#when-the-clouds-ssh-host-key-changes) awaits your decision.
- **Detection time**: ssh notices a dead connection after ~90s
  (`ServerAliveInterval=30` × 3). The cloud likewise drops the stale session
  after ~45s (its sshd's `ClientAliveInterval`), which frees the tunnel port for
  the reconnect — until then a reconnect may fail with "remote port forwarding
  failed" and is simply retried.

It runs only in the Home Console started by `cah.py start`, which also uses it
to reconnect every `keep_open` entry on startup. `cah.py start --no-reconnect`
turns both off. To run the supervisor under a hand-started `manage.py
runserver`, set `CAH_TUNNEL_SUPERVISOR=1`.

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

## When the cloud's SSH host key changes

Each profile pins its cloud server's SSH host key in its own
`providers/<profile>/known_hosts` (never `~/.ssh/known_hosts`): the first
tunnel trusts whatever key the cloud presents (`StrictHostKeyChecking=accept-new`),
and every later one must match it.

If the cloud is rebuilt or redeployed with fresh host keys, every tunnel is then
refused with ssh's `REMOTE HOST IDENTIFICATION HAS CHANGED` warning. The client
recognises that output: each affected entry's error says the host key changed
(with the new fingerprint), and the Home Console dashboard shows a banner with
the new and previously trusted fingerprints.

Before trusting the new key, compare its fingerprint with the one shown under
*Connection details* on the cloud dashboard — a changed key you can't explain
may mean the connection is being intercepted. Then either:

- click **Trust new host key and reconnect** on the Home Console dashboard, or
- run `python cah.py reset-host-key <profile>` (prints both fingerprints and
  asks for confirmation; `-y` skips it), then **Connect all** or restart the
  console.

Both just delete the profile's `known_hosts` (it only ever holds this one
cloud's key), so the next tunnel pins the new key. This is never done
automatically.

## Per-entry controls

From a proxy entry's detail page:

- **Open tunnel / Close tunnel** — manually open or close a single tunnel.
- **Reconnect** — force-reconnect: tears down and re-establishes both the cloud
  mapping and the SSH tunnel for this entry, even if the local tunnel process
  still looks alive (it may be a stale connection to a cloud server that has
  since restarted). Use this to recover a single entry after a crash or restart.

## Global controls

From the dashboard:

- **Connect all** — reconnects every proxy entry at once, including closed ones
  (which are then kept up from then on). `python cah.py start` reconnects the
  `keep_open` ones automatically on every launch (skip with `--no-reconnect`).
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
