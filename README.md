## About

**CloudAtHome client** is the home-side component of [CloudAtHome](https://github.com/otsakir/cloudathome), a system that lets you run application servers 
at home and reach them from the internet via a cloud proxy. All traffic passes outgoing SSH tunnels and no further 
configuration is required for your router.

This repo is everything you run **at home**. It consists of `cah.py`, a cli that _registers_ with a cloud server and 
manages the connection, plus "Home console", a small Django app that controls SSH revdrse tunnels for incoming traffic and 
TLS certificates.

The whole process goes like this:

* register an account on a cloud server
* register a domain you control
* add a port forward
* open its tunnel
* get it a certificate if you need to. 

Incoming traffic will now reach the cloud server's HAProxy and be routed to your home machine through the tunnel.
Routing will be based both on hostname (host http header or SNI SNI hostname for HTTPS) and public port.

## Prerequisites

- Python 3.11+
- `certbot` CLI installed on the home machine (e.g. `sudo apt install certbot` or `pip install certbot`) if you want to issue certificates through this client.
- A registered, active account on a cloud server. Check out this experimental [demo server](http://cloudathome.retalia.org/) or roll out your own.

## Setup

Download or clone this repo and install Python dependencies. You will use `cah.py`, the client's CLI tool.
Set up a virtual env end install its dependencies. For its full functionality the CLI needs django deps for 
migrations, tunnel sync, running the server etc. You'd better install those upfront.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r django/requirements.txt
```

Run these from the repo root, so the virtualenv sits next to `cah.py` as `.venv/`. Activate that same virtualenv (`source .venv/bin/activate`) any time you run `cah.py` or `manage.py` from a new shell.


## Cloud account & API token

Visit the cloud server and register for an account if you don't already have one. 

    `<cloud-server-url>/signup/`

Note, an administrator needs to activate your account before you can log in. Once you log in, you'll
have to generate an API token. 


## Home registration

After obtraining an API token register your home with the cloud server.

```bash
  python cah.py register profile-name --cloudserver-url http://localhost --token xxxxxxxxx
```

## Home Console

Home Console is a web application you will use for administering your home system. Setting up 
tunnels, forwarded domain names and public ports are typical tasks you can perform from it.

Start the Home Console

```bash
python cah.py start <profile-name>
```

### Base & forwarded domains and proxy entries

Base domains work like a DNS naming scope owned by a home. A home registers them to the cloud server and all requests 
targeting a domain (or nested subdomains ) can potentially be forwarded to the home. No other home can claim routing
for this naming branch. The `base domain` concept is about naming scope and ownership.

Actually forwarding traffic for a specific domain will require a `forwarded domain` entity to be in place. Create one from
the client Dashboard, under "HTTP(s) forwarded domains".

Almost done. Create a `proxy entry` under a `forwarded domain` to actually set up a reverse tunnel to your home. Configure
'Scheme' as http or https, the public listening port on the cloud server and the local port on your home network to forward
traffic to.

### Getting certificates

TBD


## CLI

| Command | What it does |
|---------|---------------|
| `python cah.py register [profile] --token <token> [--cloudserver-url URL]` | Register a new profile with a cloud server. |
| `python cah.py start <profile> [--port PORT] [--no-reconnect]` | Start the Home Console for a profile (auto-assigned port, auto-reconnects tunnels). |
| `python cah.py list` | List registered profiles — local only, no network calls. |
| `python cah.py remove <profile> [-y\|--yes] [-f\|--force]` | Deregister a profile from its cloud server and delete it locally (`-y`/`--yes` skips the confirmation prompt; `-f`/`--force` deletes locally even if the cloud server is unreachable or refuses to deregister). |

Run `python cah.py <command> --help` for the full set of flags.

## Learn more

- **[docs/forwards-and-certificates.md](docs/forwards-and-certificates.md)** — base domains, HTTP/HTTPS/TCP forwards, custom inbound ports, obtaining and renewing TLS certificates.
- **[docs/tunnels-and-sync.md](docs/tunnels-and-sync.md)** — tunnel lifecycle, reconnect/disconnect, LAN forwarding, bandwidth throttling.
- **[docs/configuration.md](docs/configuration.md)** — `home.yaml` and `config.yaml` reference (including reaching the Home Console from your LAN), portability, running multiple profiles side by side.
- **[docs/running-as-a-service.md](docs/running-as-a-service.md)** — auto-starting `cah.py start` on boot with systemd (no Docker).
- **[docs/architecture.md](docs/architecture.md)** — directory layout, components, design rationale, the cloud REST API surface — for anyone modifying this repo.

For standing up your own cloud server, or the full cloud-side REST API reference, see **[otsakir/cloudathome](https://github.com/otsakir/cloudathome)**.
