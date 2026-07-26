## About

**CloudAtHome Client** is the home-side component of [CloudAtHome](https://github.com/otsakir/cloudathome) — a system that lets you run application servers at home and reach them from the internet via a cloud proxy, without opening any inbound firewall ports on your home network.

This repo is everything you run **at home**: a single CLI (`cah.py`) to register with a cloud server and manage the connection, plus a small Django app (the "Home Console") that manages HTTP/HTTPS forwards, TCP forwards, TLS certificates, and the SSH reverse tunnels themselves.

The cloud-side component (HAProxy + the Django API/SSH server that homes connect to) lives in a separate repo: **[otsakir/cloudathome](https://github.com/otsakir/cloudathome)**. You need access to a running cloud server (your own, or someone else's) to use this client — see that repo if you need to stand one up yourself.

### How it fits together

1. A home operator generates an API token from the cloud server's dashboard, then runs `python cah.py register` with that token. It registers the home (generating a dedicated SSH key pair by default), and writes the resulting connection details to a per-profile `providers/<name>/config.yaml`.
2. `python cah.py start <name>` starts the Home Console Django app for that profile. A single client install can hold several such profiles side by side — one per cloud server — each run as its own process, started independently.
3. For HTTP/HTTPS forwards, the operator first registers one or more **base domains** with the cloud server (e.g. `mysite.example.com`). The cloud enforces that no two homes can claim overlapping domains. The home is then authoritative for that domain and all its subdomains.
4. The operator adds forwards in the Home Console — either HTTP/HTTPS (domain-based) or TCP (port-based). Each forward registers a mapping directly in HAProxy on the cloud server (no persistent cloud-side state) and records the allocated tunnel port locally. HTTP/HTTPS forwards are only accepted if the hostname falls under one of the home's registered base domains.
5. For HTTP/HTTPS forwards: the operator opens the SSH tunnel and triggers certificate issuance from the proxy entry page. Certbot runs standalone locally; Let's Encrypt validates via the tunnel. The certificate is stored under `providers/<name>/certbot/`.
6. The operator closes the temporary tunnel if needed, or keeps it open for production traffic.
7. Incoming HTTPS traffic hits the cloud server's HAProxy, routed by SNI hostname through the tunnel. Incoming TCP traffic hits HAProxy on the allocated public port, routed by destination port through the tunnel.

## Directory layout

```
.
├── cah.py                               # single CLI: register / start / list / remove
├── home.yaml.example                    # template for optional global settings (home.yaml)
├── providers/                           # one subdirectory per registered cloud server ("profile")
│   └── <name>/                          # e.g. providers/cloud-example-com/, gitignored
│       ├── config.yaml                  # written by cah.py register — contains secrets
│       ├── db.sqlite3                   # this profile's Home Console database
│       ├── ssh_key / ssh_key.pub        # dedicated key pair for this profile's tunnel
│       └── certbot/                     # created on first certificate issuance
│           ├── config/                  # certbot config and issued certificates
│           ├── work/                    # certbot working directory
│           └── logs/                    # certbot logs
├── providers/config.yaml.example        # template showing all fields for a single profile
├── scripts/
│   └── generate_keys.py                 # standalone: generate an SSH key pair (rarely needed — see below)
└── django/                              # Home Console Django app (one process runs against one active profile)
    ├── cloudlink/                       # config loading, cloud API client, dashboard
    └── domains/                         # domain, certificate, and tunnel management
```

## Prerequisites

- Python 3.11+
- `certbot` CLI installed on the home machine (e.g. `sudo apt install certbot` or `pip install certbot`)
- A registered, active account on the target cloud server (self-register at `http://<cloud-host>:8000/signup/`, then wait for an admin to activate it)
- The Home Console's dependencies installed once, up front — `cah.py` itself only needs `requests`/`pyyaml`, but it shells out to `manage.py` (migrations, tunnel sync, running the server), which needs the full Django environment:
  ```bash
  cd django
  python -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  ```

## Registering with a new cloud site

Each cloud server you connect to gets its own **profile** under `providers/<name>/`, so the same home machine can stay connected to multiple independent cloud servers at once (e.g. a personal cloud and a family member's).

**1. Get an API token from that cloud server's dashboard.** Log in at `http://<cloud-host>:8000/`, and if you don't already have one, click **Generate an API token**. It's shown only once — copy it now.

**2. Run `cah.py register <name>` with that token.** The profile name is a plain positional argument; if you omit it, one is derived from the cloud server's hostname. By default this generates a dedicated SSH key pair for the new profile, registers the home, and writes `providers/<name>/config.yaml`:

```bash
python cah.py register my-cloud \
    --cloudserver-url https://cloud.example.com \
    --token <token-from-the-dashboard>
```

`--cloudserver-url` is optional: omit it to register against the default server (either `default_cloudserver_url` from an optional `home.yaml`, copied from `home.yaml.example`, or otherwise the public demo server, `http://cloudathome.retalia.org`) — so registering against the default is just `python cah.py register my-cloud --token <token>`.

On success it prints a summary, runs `manage.py migrate` for the new profile automatically, and tells you how to start it:

```
Done. Configuration written to: providers/my-cloud/config.yaml
  home_slug    : xK3mAbcDef9pQr
  ssh_username : home02_alice
  ssh_host     : cloud.example.com:22
  port range   : 2200 – 2209
  console port : 8001

Start this profile with:
  python cah.py start my-cloud
```

Pass `--public-key`/`--private-key` together instead if you want to bring your own existing key pair rather than generating a dedicated one (`generate_keys.py` is only needed for that path). If automatic migration fails, `cah.py register` tells you to run `manage.py migrate` yourself before starting — registration itself has already succeeded at that point.

## Starting the Home Console

```bash
python cah.py start my-cloud
```

No port or `HOME_CONFIG` bookkeeping needed: `start` auto-assigned and remembered a port for this profile at registration time (or the first time you start it, if it was registered before this existed), reconnects any existing tunnels/mappings automatically (equivalent to the dashboard's "Connect all" — skip with `--no-sync`), then runs the Home Console at `http://localhost:<port>/`. Pass `--port` to override.

## Listing registered profiles

```bash
python cah.py list
```

Purely local and instant (no network calls) — shows each profile's name, cloud server, home slug, console port, and whether it's currently running. Useful once you have more than one profile to keep track of.

## Removing a profile

```bash
python cah.py remove my-cloud
```

Disconnects all tunnels, releases the home slot on the cloud server (which also cleans up this home's live HAProxy mappings, base domains, and bandwidth limit server-side), revokes this profile's API token, and — only once all of that has succeeded — permanently deletes `providers/my-cloud/` (database, certificates, SSH key). Prompts for confirmation first; skip it with `--yes`. If any step fails, nothing local is deleted and the error is printed — fix the issue and re-run to retry; it's safe to run more than once.

## Portability

Each profile's state lives entirely under its own `providers/<name>/` directory:

| Piece | Default location | Configured by |
|-------|-----------------|---------------|
| Connection config | `providers/<name>/config.yaml` | `HOME_CONFIG` env var |
| Database | `providers/<name>/db.sqlite3` | `database` in config.yaml |
| TLS certificates | `providers/<name>/certbot/` | certbot working directory |
| SSH key pair | `providers/<name>/ssh_key` | `ssh.private_key_path` in config.yaml |

To move a profile to another machine: copy its `providers/<name>/` directory, update any absolute paths in its `config.yaml`, and run `python cah.py start <name>` as usual.

To run several cloud connections at once, just run `cah.py start` for each profile — each auto-assigned its own port at registration time:

```bash
python cah.py start my-cloud     # e.g. port 8001
python cah.py start family-cloud # e.g. port 8002
```

## Base domains

Before creating any HTTP/HTTPS proxy entry, the home must register at least one base domain with the cloud server. A base domain is a domain the operator controls in DNS — the cloud server enforces that no two homes can claim the same domain or overlapping domains (e.g. if Home A owns `example.com`, Home B cannot register `sub.example.com`).

The cloud validates that the domain is a proper registrable domain (not a bare TLD like `com` or a public suffix like `co.uk`) using the Public Suffix List.

**From the Home Console dashboard:**
- Click **Register base domain**, enter the domain name, and submit.
- The domain is stored on the cloud server and returned in the home's info response.
- To remove a domain, click **Remove** next to it on the dashboard. This is blocked with an error if any active proxy mappings still use that domain or its subdomains — disconnect those mappings first.

A home can register multiple base domains. Subdomains do not need to be registered separately — once `example.com` is registered, the home can freely create proxy entries for `blog.example.com`, `api.example.com`, etc.

## Obtaining a TLS certificate

Certificate issuance is tied to a proxy entry. The full sequence from the Home Console:

**1. Add a domain** — go to **Domains → Add domain** and enter the domain name (e.g. `mysite.example.com`). DNS must already point to the cloud server.

**2. Add a proxy entry** — from the domain detail page click **Add**. Choose a scheme and the local port certbot will listen on (e.g. `8082`). This registers the proxy mapping on the cloud server; the tunnel port is allocated server-side.

**3. Open the tunnel** — on the proxy entry detail page click **Open tunnel**. This starts an SSH reverse tunnel: `cloud_tunnel_port → home:home_port`.

**4. Issue the certificate** — with the tunnel open, click **Issue certificate**. Enter your email on the certificate page and submit. Certbot runs in standalone mode, Let's Encrypt validates the HTTP-01 challenge through the tunnel, and the certificate is saved to `providers/<name>/certbot/config/live/<domain>/`.

The domain record is updated with the certificate path and expiry date on success.

## Bandwidth throttling

Per-home bandwidth limits cap how much of the home's internet upload the cloud tunnel can consume, enforced on the cloud server (see the cloud repo for how). A home owner sets or clears their own limit via the API:

```
PATCH /api/homes/<slug>/
{"bandwidth_limit_kbps": 5000}   # set to 5 Mbit/s
{"bandwidth_limit_kbps": null}   # remove limit (unlimited)
```

Accepted range: 100 – 10,000,000 kbps. `null` means unlimited.

## Managing tunnels

Tunnels are OS-level SSH processes. Their PIDs are stored in the database so they can be stopped cleanly even after a Django restart. If a tunnel process dies unexpectedly, the status is corrected automatically the next time the proxy entry page is loaded.

SSH process output (stdout/stderr) is inherited from the Django process and appears directly in the Home Console's terminal. For example, if the local service is not yet listening on its port, you will see repeated `connect_to localhost port <N>: failed.` lines — these come from SSH, not Django.

**Per-entry controls** (proxy entry detail page):
- **Open tunnel / Close tunnel** — manually open or close a single tunnel.
- **Sync** — force-reconnect: tears down and re-establishes both the cloud mapping and the SSH tunnel for this entry, even if the local tunnel process still looks alive (it may be a stale connection to a cloud server that has since restarted). Use this to recover a single entry after a crash or restart.

**Global controls** (dashboard):
- **Connect all** — syncs every proxy entry at once. `python cah.py start` already does this automatically on every launch (skip with `--no-sync`); use this button to reconnect without restarting the console.
- **Disconnect all** — closes all tunnels and removes all cloud proxy mappings cleanly.

**Management command** — the same sync operations are available from the command line:

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

## Walkthrough: from zero to a publicly reachable service

This assumes you already have access to a running cloud server with a public IP, and that a domain you control (e.g. `mysite.example.com`) points to it in DNS.

**1. Create and activate a cloud account** — go to `http://<cloud-host>:8000/signup/` and register; an admin needs to activate the account before you can log in.

**2. Generate an API token** — log in at `http://<cloud-host>:8000/`, click **Generate an API token**, and copy it (shown once).

**3. Install this client's dependencies (once)**:
```bash
cd django
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd ..
```

**4. Register the home**:
```bash
python cah.py register \
    --cloudserver-url http://<cloud-host>:8000 \
    --token <token-from-the-dashboard>
```

**5. Start the Home Console**:
```bash
python cah.py start <name>
```

**6. Register a base domain** — on the dashboard (`http://localhost:<port>/`), click **Register base domain**, enter `mysite.example.com`, submit.

**7. Add a domain and proxy entry** — go to `http://localhost:<port>/domains/add/`, enter `mysite.example.com`; from the domain detail page click **Add**, choose scheme `http` and a local port for certbot (e.g. `8082`).

**8. Open the tunnel and get a certificate** — click **Open tunnel**, then **Issue certificate** with your email.

**9. Test**:
```bash
curl https://mysite.example.com
```

**10. (Optional) Remove the home when you're done**:
```bash
python cah.py remove <name>
```

For standing up your own cloud server, or the full REST API reference, see **[otsakir/cloudathome](https://github.com/otsakir/cloudathome)**.
