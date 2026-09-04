## About

**CloudAtHome Client** is the home-side component of [CloudAtHome](https://github.com/otsakir/cloudathome) — a system that lets you run application servers at home and reach them from the internet via a cloud proxy, without opening any inbound firewall ports on your home network.

This repo is everything you run **at home**: a single CLI (`cah.py`) to register with a cloud server and manage the connection, plus a small Django app (the "Home Console") that manages HTTP/HTTPS forwards, TCP forwards, TLS certificates, and the SSH reverse tunnels themselves.

The cloud-side component (HAProxy + the Django API/SSH server that homes connect to) lives in a separate repo: **[otsakir/cloudathome](https://github.com/otsakir/cloudathome)**. You need access to a running cloud server (your own, or someone else's) to use this client — see that repo if you need to stand one up yourself.

In short: register with a cloud server, register a domain you control, add a forward, open its tunnel, get it a certificate if you need to. Incoming traffic hits the cloud server's HAProxy and is routed to your home machine through the tunnel — by SNI hostname for HTTPS, host header for HTTP or by public port for TCP.

## Prerequisites

- Python 3.11+
- `certbot` CLI installed on the home machine (e.g. `sudo apt install certbot` or `pip install certbot`) if you want to issue certificates through this client.
- A registered, active account on the target cloud server (self-register at `<cloud-server-url>/signup/`, then wait for an admin to activate it — `<cloud-server-url>` is whatever base URL that server is reachable at, e.g. `https://cloud.example.com`; there's no fixed port, it depends on how that server is deployed)

## Setup

After downloading or cloning this repo, install the Python dependencies once, up front. `cah.py` itself only needs `requests`/`pyyaml`, but it shells out to the Home Console's `manage.py` (migrations, tunnel sync, running the server), which needs the full Django environment — so a single virtualenv, installed from `django/requirements.txt`, covers both:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r django/requirements.txt
```

Run these from the repo root, so the virtualenv sits next to `cah.py` as `.venv/`. Activate that same virtualenv (`source .venv/bin/activate`) any time you run `cah.py` or `manage.py` from a new shell.

## Quickstart: zero to a publicly reachable service

This assumes you already have access to a running cloud server with a public IP, and that a domain you control (e.g. `mysite.example.com`) points to it in DNS.

**1. Create and activate a cloud account** — go to `<cloud-server-url>/signup/` and register; an admin needs to activate the account before you can log in.

**2. Generate an API token** — log in at `<cloud-server-url>/` (the cloud server's own dashboard), click **Generate an API token**, and copy it (shown once).

**3. Install this client's dependencies (once)** — see [Setup](#setup) above.

**4. Register the home**:
```bash
python cah.py register \
    --cloudserver-url <cloud-server-url> \
    --token <token-from-the-cloud-dashboard>
```

**5. Start the Home Console**:
```bash
python cah.py start <profile>
```

**6. Register a base domain** — on the Home Console's dashboard (`http://localhost:<port>/`), click **Register base domain**, enter `mysite.example.com`, submit.

**7. Add a domain and proxy entry** — go to `http://localhost:<port>/domains/add/`, enter `mysite.example.com`; from the domain detail page click **Add**, choose scheme `http` and a local port for certbot (e.g. `8082`).

**8. Open the tunnel and get a certificate** — click **Open tunnel**, then **Issue certificate** with your email. (There's also a one-click **Issue certificate** playbook on the Home Console's dashboard that does steps 7–8 in one go — see [docs/forwards-and-certificates.md](docs/forwards-and-certificates.md#obtaining-a-tls-certificate).)

**9. Add an HTTPS proxy entry and open its tunnel** — back on the domain detail page, click **Add** again, choose scheme `https` this time, pick the local port your actual TLS-terminated service listens on, then open its tunnel too. (The HTTP entry from step 7 can stay — a domain can hold one of each, which is also what lets you renew the certificate later without taking this HTTPS forward down.)

**10. Test**:
```bash
curl https://mysite.example.com
```

**11. Dismantle service** when your services reach their end of life. Tunnels, cloud registration, and local files all get cleaned up in one go if you ever want to fully remove this profile:
```bash
python cah.py remove <profile>
```

## Everyday commands

| Command | What it does |
|---------|---------------|
| `python cah.py register [profile] --token <token> [--cloudserver-url URL]` | Register a new profile with a cloud server. |
| `python cah.py start <profile> [--port PORT] [--no-sync]` | Start the Home Console for a profile (auto-assigned port, auto-reconnects tunnels). |
| `python cah.py list` | List registered profiles — local only, no network calls. |
| `python cah.py remove <profile> [--yes]` | Deregister a profile from its cloud server and delete it locally. |

Run `python cah.py <command> --help` for the full set of flags.

## Learn more

- **[docs/forwards-and-certificates.md](docs/forwards-and-certificates.md)** — base domains, HTTP/HTTPS/TCP forwards, custom inbound ports, obtaining and renewing TLS certificates.
- **[docs/tunnels-and-sync.md](docs/tunnels-and-sync.md)** — tunnel lifecycle, sync/disconnect, LAN forwarding, bandwidth throttling.
- **[docs/configuration.md](docs/configuration.md)** — `home.yaml` and `config.yaml` reference (including reaching the Home Console from your LAN), portability, running multiple profiles side by side.
- **[docs/running-as-a-service.md](docs/running-as-a-service.md)** — auto-starting `cah.py start` on boot with systemd (no Docker).
- **[docs/architecture.md](docs/architecture.md)** — directory layout, components, design rationale, the cloud REST API surface — for anyone modifying this repo.

For standing up your own cloud server, or the full cloud-side REST API reference, see **[otsakir/cloudathome](https://github.com/otsakir/cloudathome)**.
