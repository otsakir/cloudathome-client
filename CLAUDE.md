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
python cah.py register [profile] --token <token> [--cloudserver-url URL]

# Start the Home Console for a profile (auto-assigned port, auto-reconnects tunnels)
python cah.py start <profile> [--port PORT] [--no-reconnect]

# List registered profiles (local only, no network calls)
python cah.py list

# Deregister a profile from its cloud server and delete it locally
python cah.py remove <profile> [-y|--yes] [-f|--force]
```

`register`'s profile name is a plain positional argument; if omitted, one is derived from the cloud server's hostname. `--cloudserver-url` is optional too — it falls back to `default_cloudserver_url` in an optional `home.yaml` (see `home.yaml.example`), or otherwise a hardcoded public demo server.

### Django (once a profile exists, outside `cah.py`)

```bash
source .venv/bin/activate
cd django

HOME_CONFIG=../providers/<profile>/config.yaml python manage.py runserver 0.0.0.0:8001
HOME_CONFIG=../providers/<profile>/config.yaml python manage.py migrate
HOME_CONFIG=../providers/<profile>/config.yaml python manage.py reconnect_tunnels
HOME_CONFIG=../providers/<profile>/config.yaml python manage.py deregister
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

Full writeup — components, directory layout, design rationale, the cloud REST
API surface — lives in **[docs/architecture.md](docs/architecture.md)**. User/
admin-facing how-tos are split across **[docs/forwards-and-certificates.md](docs/forwards-and-certificates.md)**,
**[docs/tunnels-and-sync.md](docs/tunnels-and-sync.md)**,
**[docs/configuration.md](docs/configuration.md)**, and
**[docs/running-as-a-service.md](docs/running-as-a-service.md)** — check those
before re-deriving something that's already documented, and update them (not
just this file) when a change affects what they describe.

The two sharpest landmines, worth keeping front-of-mind without a doc hop:

- **`CloudServerClient`** (`cloudlink/services.py`) method signatures for mapping
  create/delete must stay in sync with the cloud's URL structure — a past bug
  here silently dropped every delete because the client built the wrong URL
  shape. The two repos no longer share a single commit history, so this can
  drift silently.
- **`TunnelConnectionService.reconnect_entry`** always tears down and
  re-establishes both the cloud mapping and the SSH tunnel — never trust a
  "still running" tunnel PID as proof it's still connected to the *current*
  cloud instance; a local ssh client can outlive the cloud restarting under it.
  On failure it records the reason in `ProxyEntry.tunnel_error` (shown in the
  UI while `tunnel_status` is `error`), since `reconnect_all` runs unattended
  and only logs. The main case: the cloud operator shrank the HTTP/HTTPS
  inbound range, so re-registering an entry's stored `public_port` gets the
  cloud's `code: public_port_not_offered` 400, which `create_proxy_mapping`
  raises as `PublicPortNotOfferedError` (message lists the ports still offered).
