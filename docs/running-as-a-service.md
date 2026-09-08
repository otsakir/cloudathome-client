# Running as a service

`cah.py start <profile>` runs in the foreground — it execs straight into
`manage.py runserver`, it doesn't daemonize itself. That's fine for a one-off
`&`-backgrounded run, but it won't survive a reboot and won't restart itself
if it dies. For that, use **systemd** — no Docker required.

## systemd unit (system-wide)

Create `/etc/systemd/system/cloudathome-client.service`, substituting your
Linux username, the repo path, and the profile name:

```ini
[Unit]
Description=CloudAtHome Client (retalia)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<your-linux-username>
WorkingDirectory=/home/<your-linux-username>/cloudathome-client
ExecStart=/home/<your-linux-username>/cloudathome-client/.venv/bin/python cah.py start retalia
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Notes:

- No `source .venv/bin/activate` — systemd doesn't run through your shell, so
  `ExecStart` calls the venv's `python` binary directly by full path instead.
  That's equivalent to activating first.
- `User=` should be your normal (non-root) user — the one that owns
  `providers/<profile>/` and its SSH key, not root.
- `After=network-online.target` / `Wants=network-online.target` delays startup
  until the network is actually up — `start` refreshes inbound port ranges and
  syncs tunnels against the cloud server on every launch (see
  [tunnels-and-sync.md](tunnels-and-sync.md)), both of which need connectivity.
- `Restart=on-failure` auto-recovers the process if it dies (e.g. the cloud is
  briefly unreachable), which a manual `&` doesn't give you.

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloudathome-client
```

Manage/inspect it:

```bash
sudo systemctl status cloudathome-client
sudo journalctl -u cloudathome-client -f    # follow logs
sudo systemctl restart cloudathome-client
sudo systemctl stop cloudathome-client
```

Tunnel-specific chatter (SSH connection messages, drops) is also written per
tunnel under `providers/<profile>/logs/tunnels/` regardless of how you're
running the console — see [tunnels-and-sync.md](tunnels-and-sync.md) — so you
don't have to scroll through `journalctl`'s combined output to find one
tunnel's history.

This replaces `cd cloudathome-client/ && source .venv/bin/activate && ./cah.py
start <profile> &` entirely — on reboot, systemd starts it automatically once
networking is up, and `journalctl` gives you what you'd otherwise see in the
terminal.

## systemd unit (user service, no root)

The same unit also works as a **user service**, if you'd rather not touch
`/etc/systemd/system`: save it as `~/.config/systemd/user/cloudathome-client.service`
(identical contents, `User=` is unnecessary there) and run:

```bash
systemctl --user daemon-reload
systemctl --user enable --now cloudathome-client
```

You then also need to let it start at boot without you logging in
interactively:

```bash
sudo loginctl enable-linger <your-linux-username>
```

Otherwise, prefer the system-wide unit above — it's simpler for a
headless/always-on box.

## Multiple profiles

Running more than one profile as a service is the same recipe repeated: one
unit file per profile (different `Description`, `ExecStart` profile name, and
unit filename), each pointing at the same repo checkout — profiles are
independent under `providers/<profile>/`, so there's no conflict beyond
picking distinct `home_console_port`s (see
[configuration.md](configuration.md#portability--moving-a-profile)).
