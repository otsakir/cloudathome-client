# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**CloudAtHome Client** is the home-side component of CloudAtHome, a system that makes home-hosted application servers reachable from the internet via a cloud proxy. This repo connects to a CloudAtHome cloud server (see the separate **[otsakir/cloudathome](https://github.com/otsakir/cloudathome)** repo for that component) to register a home, establish SSH reverse tunnels, and manage HTTP/HTTPS/TCP forwards and TLS certificates from a local web UI (the "Home Console").

A single install of this repo can hold several independent **profiles** — one per cloud server it's registered with — each with its own SSH key, database, and Home Console instance.

Locally, the cloud-side repo checks out as a sibling directory, `../cloudathome` relative to this one. Work spanning both sides of the system (e.g. a change to the REST API contract, or the `CloudServerClient`/`HAProxyService` URL shapes staying in sync) is typically done from a single Claude Code session rooted in `../cloudathome`, `cd`-ing into this repo as needed rather than starting a separate session per repo.

## Running & Building

### First-time setup

```bash
# from the repo root
python -m venv .venv && source .venv/bin/activate
pip install -r django/requirements.txt
```

`cah.py` itself only needs `requests`/`pyyaml`, but it shells out to `manage.py` (migrations, tunnel sync, running the server), which needs the full Django environment above. The virtualenv lives at the repo root (`.venv/`), not under `django/`, since `cah.py` — the usual entry point — is at the root too, and editors/tooling look for `.venv` next to the project root.

### The `cah.py` CLI (run from the repo root)

```bash
# Register a new profile with a cloud server (token from that cloud's dashboard)
python cah.py register [name] --token <token> [--cloudserver-url URL]

# Start the Home Console for a profile (auto-assigned port, auto-reconnects tunnels)
python cah.py start <name> [--port PORT] [--no-sync]

# List registered profiles (local only, no network calls)
python cah.py list

# Deregister a profile from its cloud server and delete it locally
python cah.py remove <name> [--yes]
```

`register`'s profile name is a plain positional argument; if omitted, one is derived from the cloud server's hostname. `--cloudserver-url` is optional too — it falls back to `default_cloudserver_url` in an optional `home.yaml` (see `home.yaml.example`), or otherwise a hardcoded public demo server.

### Django (once a profile exists, outside `cah.py`)

```bash
source .venv/bin/activate
cd django

HOME_CONFIG=../providers/<name>/config.yaml python manage.py runserver 0.0.0.0:8001
HOME_CONFIG=../providers/<name>/config.yaml python manage.py migrate
HOME_CONFIG=../providers/<name>/config.yaml python manage.py sync_tunnels
HOME_CONFIG=../providers/<name>/config.yaml python manage.py deregister
```

`HOME_CONFIG` points Django at a specific profile's `config.yaml`; `cah.py` sets this automatically for you, so working through it directly is only needed for scripting or debugging a single profile without going through the CLI.

### Tests

```bash
cd django
# use the `cloudathome-client` conda env (or equivalent), not a cloud-side env
HOME_CONFIG=<path-to-a-valid-config.yaml> pytest
```

`conftest.py` provides an autouse fixture with a minimal valid `config.yaml` for most tests, but `HOME_CONFIG` must already point at *some* valid file before pytest starts — Django settings (`homeserver/settings.py`) resolve the database path from `get_config()` at import time, before any fixture runs.

## Architecture

### Components

| Component | Role |
|-----------|------|
| **`cah.py`** | Single CLI: register with a cloud server, start/list/remove profiles. |
| **Home Console** (`django/`) | Django app that manages HTTP/HTTPS forwards (domain + TLS certificate lifecycle), TCP forwards, and SSH reverse tunnels for one active profile. Reads connection config from that profile's `config.yaml`. |

### Source layout

```
.
├── cah.py                               # single CLI: register / start / list / remove
├── home.yaml.example                    # template for optional global settings (home.yaml)
├── providers/                           # one subdirectory per registered cloud server ("profile"), gitignored
│   └── <name>/
│       ├── config.yaml                  # written by cah.py register — contains secrets
│       ├── db.sqlite3                   # this profile's Home Console database
│       ├── ssh_key / ssh_key.pub        # dedicated key pair for this profile's tunnel
│       └── certbot/                     # created on first certificate issuance
├── providers/config.yaml.example        # template showing all fields for a single profile
├── scripts/
│   └── generate_keys.py                 # standalone: generate an SSH key pair (rarely needed)
└── django/                              # Home Console Django app (one process runs against one active profile)
    ├── homeserver/                      # Django project package (settings, urls)
    ├── cloudlink/                       # config loading (config.py), cloud API client (services.py: CloudServerClient), dashboard views
    ├── domains/                         # Domain/ProxyEntry models, forms, views, tunnel/certbot services (services.py), management commands
    │   └── management/commands/
    │       ├── sync_tunnels.py          # re-registers cloud mappings + reopens tunnels (also used by "Connect all"/"Disconnect all")
    │       └── deregister.py            # disconnects tunnels, releases the home slot, revokes the API token
    ├── playbooks/                       # scripted multi-step flows (e.g. IssueCertificatePlaybook) with structured per-step results
    └── tests/                           # pytest suite (test_certbot_service.py, etc.)
```

### Key design points

- **Profiles are fully self-contained**: everything for a given cloud connection lives under `providers/<name>/` — config, SQLite database, dedicated SSH key pair, certbot state. Moving a profile to another machine is just copying that directory.
- **`CloudServerClient`** (`cloudlink/services.py`) is the sole HTTP client talking to the cloud's REST API (auth via `Authorization: Token <auth_token>`). Its method signatures for mapping create/delete (`create_proxy_mapping(scheme, host=None, public_port=None)`, `delete_proxy_mapping(scheme, host=None, public_port=None)`) must stay in sync with the cloud's URL structure (`/api/homes/<slug>/proxy-mappings/<scheme>/<host>/` for HTTP/HTTPS, `/api/homes/<slug>/proxy-mappings/tcp/<port>/` for TCP) — a past bug here silently dropped every delete because the client built the wrong URL shape; watch for this class of drift since the two repos no longer share a single commit history.
- **Tunnels are OS-level SSH processes**: `TunnelService.open_tunnel`/`close_tunnel` (`domains/services.py`) manage them via `subprocess`/`os.kill`; PIDs are persisted on `ProxyEntry` so they survive a Django restart. `SyncService.sync_entry` always tears down and re-establishes both the cloud mapping and the tunnel (never trusts a "still running" PID as proof the tunnel is still connected to the *current* cloud instance — a local ssh client can outlive the cloud restarting under it until `ServerAliveInterval`/`ServerAliveCountMax` time out).
- **`deregister`** (management command) is the counterpart to `cah.py remove`: disconnects tunnels, calls the cloud to release the home slot (which itself cascades cleanup of base domains/mappings/bandwidth server-side), then revokes the API token — in that order, since revoking the token must be last (it invalidates the credential every prior call used).
- **Certbot state** lives under the active profile's `certbot_dir` (`get_config().certbot_dir`), never a path derived from the module's own location, so concurrent profiles never share certbot's lock files.
- **`features.lan_forwarding`** (per-profile config, off by default) gates whether a proxy entry may forward to a home-network host other than `localhost` — otherwise `home_host` is forced to `localhost` regardless of what's submitted.
- **HTTP/HTTPS inbound port range** (`cloudlink.http_ports`/`https_ports` in `config.yaml`, mirroring `tcp_ports`'s `{base, count}` shape) is cloud-wide config, not home-specific — `cah.py`'s `_refresh_inbound_port_ranges` re-fetches it from `GET /api/config/inbound-ports/<scheme>/` on every `start` (not just the one-time `register`, since this value can change independently of this home's registration) and writes it back to `config.yaml` only if it changed. `CloudConfig.http_port_base`/`http_port_count`/`https_port_base`/`https_port_count` (`cloudlink/config.py`) surface it to `ProxyEntryForm`/`ProxyEntryCreateView` (`domains/`) for client-side range validation before calling `create_proxy_mapping(scheme, host=..., public_port=...)` — the cloud is still authoritative and validates again server-side.
- **A `Domain` can hold one HTTP and one HTTPS `ProxyEntry` at once** (`domain` is a `ForeignKey` with `UniqueConstraint(['domain', 'scheme'])`, not the tighter `OneToOneField` it briefly was) — needed so `IssueCertificatePlaybook` can obtain/renew a certificate via a temporary HTTP entry without ever requiring a live HTTPS entry for the same domain to be torn down first. Certificate issuance itself only works from an HTTP-scheme entry (ACME HTTP-01 always validates over port 80, which only an HTTP-scheme mapping is routed to on the cloud side) — `IssueCertificateView` rejects it server-side for an HTTPS entry, and the Home Console doesn't show the link there at all.

### Talking to the cloud

Full REST API reference lives in the cloud repo's `CLAUDE.md`/`README.md`. From this side, the
relevant surface (all via `CloudServerClient`, all `TokenAuthentication`) is:

| Method | Endpoint | Used by |
|--------|----------|---------|
| POST | `/api/homes/` | `cah.py register` |
| DELETE | `/api/homes/<slug>/` | `deregister` (via `cah.py remove`) |
| PATCH | `/api/homes/<slug>/` | bandwidth-limit form (`cloudlink/views.py`) |
| GET/POST/DELETE | `/api/homes/<slug>/base-domains/...` | base-domain views (`cloudlink/views.py`) |
| POST/DELETE | `/api/homes/<slug>/proxy-mappings/...` | `SyncService`, `_delete_proxy_entry`, proxy-entry create views (`domains/`) |
| GET | `/api/config/inbound-ports/<scheme>/` | `cah.py start`'s `_refresh_inbound_port_ranges` — called directly via `requests`, not through `CloudServerClient`, since it runs before Django/the profile config are bootstrapped (same reason `cmd_register` also calls the cloud with raw `requests`) |
| DELETE | `/api/auth/token/` | `deregister` |

Authentication is a DRF token generated on the cloud dashboard (`RotateTokenView` there) and
pasted into `cah.py register --token ...`; it's stored in that profile's `config.yaml`.
