# Architecture

This is the deeper reference for anyone modifying this repo, not just running it —
directory layout, how the pieces fit together, and the design decisions that
aren't obvious from reading a single file in isolation.

## Components

| Component | Role |
|-----------|------|
| `cah.py` | Single CLI: register with a cloud server, start/list/remove profiles. Deliberately independent of Django (only needs `requests`/`pyyaml`) so it can bootstrap a profile before a Django environment is even relevant. |
| Home Console (`django/`) | Django app that manages HTTP/HTTPS forwards (domain + TLS certificate lifecycle), TCP forwards, and SSH reverse tunnels for one active profile. Reads connection config from that profile's `config.yaml`. One `runserver` process per profile. |

## Directory layout

```
.
├── cah.py                               # single CLI: register / start / list / remove
├── home.yaml.example                    # template for optional global settings (home.yaml, gitignored)
├── providers/                            # one subdirectory per registered cloud server ("profile"), gitignored
│   └── <profile>/
│       ├── config.yaml                  # written by cah.py register — contains secrets
│       ├── db.sqlite3                   # this profile's Home Console database
│       ├── ssh_key / ssh_key.pub        # dedicated key pair for this profile's tunnel
│       └── certbot/                     # created on first certificate issuance
│           ├── config/                  # certbot config and issued certificates
│           ├── work/                    # certbot working directory
│           └── logs/                    # certbot logs
├── providers/config.yaml.example        # template showing all fields for a single profile
├── scripts/
│   └── generate_keys.py                 # standalone: generate an SSH key pair (rarely needed)
└── django/                              # Home Console Django app (one process runs against one active profile)
    ├── homeserver/                      # Django project package
    │   ├── settings.py                  # profile DB path pulled from cloudlink.config at import time
    │   └── home_config.py               # loads home.yaml (machine-wide settings, e.g. allowed_hosts)
    ├── cloudlink/                       # profile config loading (config.py), cloud API client (services.py), dashboard views
    ├── domains/                         # Domain/ProxyEntry models, forms, views, tunnel/certbot services
    │   └── management/commands/
    │       ├── sync_tunnels.py          # re-registers cloud mappings + reopens tunnels
    │       └── deregister.py            # disconnects tunnels, releases the home slot, revokes the API token
    ├── playbooks/                       # scripted multi-step flows (e.g. IssueCertificatePlaybook), listed on the dashboard
    └── tests/                           # pytest suite
```

## Key design points

- **Profiles are fully self-contained**: everything for a given cloud connection
  lives under `providers/<profile>/` — config, SQLite database, dedicated SSH key
  pair, certbot state. Moving a profile to another machine is just copying that
  directory (see [configuration.md](configuration.md#portability--moving-a-profile)).
- **`home.yaml` vs. a profile's `config.yaml`** — `home.yaml` (repo root, optional,
  gitignored) holds settings shared across every profile on this machine (e.g.
  `allowed_hosts`, `default_cloudserver_url`); `providers/<profile>/config.yaml` holds
  one profile's cloud connection. They're read independently: `cah.py` reads
  `home.yaml` directly for its own CLI defaults, and `django/homeserver/home_config.py`
  is the separate reader on the Django side (for settings like `ALLOWED_HOSTS`) —
  deliberately *not* routed through `cloudlink/config.py`, since that module is
  scoped to the cloud connection, not the machine.
- **`CloudServerClient`** (`cloudlink/services.py`) is the sole HTTP client talking
  to the cloud's REST API (auth via `Authorization: Token <auth_token>`). Its
  method signatures for mapping create/delete
  (`create_proxy_mapping(scheme, host=None, public_port=None)`,
  `delete_proxy_mapping(scheme, host=None, public_port=None)`) must stay in sync
  with the cloud's URL structure
  (`/api/homes/<slug>/proxy-mappings/<scheme>/<host>/` for HTTP/HTTPS,
  `/api/homes/<slug>/proxy-mappings/tcp/<port>/` for TCP) — a past bug here
  silently dropped every delete because the client built the wrong URL shape;
  watch for this class of drift since the two repos no longer share a single
  commit history.
- **Tunnels are OS-level SSH processes**: `TunnelService.open_tunnel`/`close_tunnel`
  (`domains/services.py`) manage them via `subprocess`/`os.kill`; PIDs are
  persisted on `ProxyEntry` so they survive a Django restart. `SyncService.sync_entry`
  always tears down and re-establishes both the cloud mapping and the tunnel
  (never trusts a "still running" PID as proof the tunnel is still connected to
  the *current* cloud instance — a local ssh client can outlive the cloud
  restarting under it until `ServerAliveInterval`/`ServerAliveCountMax` time out).
- **`deregister`** (management command) is the counterpart to `cah.py remove`:
  disconnects tunnels, calls the cloud to release the home slot (which itself
  cascades cleanup of base domains/mappings/bandwidth server-side), then revokes
  the API token — in that order, since revoking the token must be last (it
  invalidates the credential every prior call used). Its `--force` flag (surfaced
  as `cah.py remove --force`) turns a failure at either cloud call into a warning
  instead of aborting, so an unreachable/uncooperative cloud server can't strand
  a profile locally — `cah.py remove` still deletes `providers/<profile>/` in that
  case, just with a warning that the cloud may still hold stale records for this
  home.
- **Certbot state** lives under the active profile's `certbot_dir`
  (`get_config().certbot_dir`), never a path derived from the module's own
  location, so concurrent profiles never share certbot's lock files.
- **`features.lan_forwarding`** (per-profile config, off by default) gates whether
  a proxy entry may forward to a home-network host other than `localhost` —
  otherwise `home_host` is forced to `localhost` regardless of what's submitted.
- **HTTP/HTTPS inbound port range** (`cloudlink.http_ports`/`https_ports` in
  `config.yaml`, mirroring `tcp_ports`'s `{base, count}` shape) is cloud-wide
  config, not home-specific — `cah.py`'s `_refresh_inbound_port_ranges` re-fetches
  it from `GET /api/config/inbound-ports/<scheme>/` on every `start` (not just the
  one-time `register`, since this value can change independently of this home's
  registration) and writes it back to `config.yaml` only if it changed.
  `CloudConfig.http_port_base`/`http_port_count`/`https_port_base`/`https_port_count`
  (`cloudlink/config.py`) surface it to `ProxyEntryForm`/`ProxyEntryCreateView`
  (`domains/`) for client-side range validation before calling
  `create_proxy_mapping(scheme, host=..., public_port=...)` — the cloud is still
  authoritative and validates again server-side.
- **A `Domain` can hold one HTTP and one HTTPS `ProxyEntry` at once** (`domain` is
  a `ForeignKey` with `UniqueConstraint(['domain', 'scheme'])`, not the tighter
  `OneToOneField` it briefly was) — needed so `IssueCertificatePlaybook` can
  obtain/renew a certificate via a temporary HTTP entry without ever requiring a
  live HTTPS entry for the same domain to be torn down first. Certificate
  issuance itself only works from an HTTP-scheme entry (ACME HTTP-01 always
  validates over port 80, which only an HTTP-scheme mapping is routed to on the
  cloud side) — `IssueCertificateView` rejects it server-side for an HTTPS entry,
  and the Home Console doesn't show the link there at all.
- **Playbooks** (`playbooks/`) wrap a multi-step flow — register mapping, open
  tunnel, run certbot, clean up — into a single dashboard action with a
  structured, step-by-step result (`PlaybookResult`/`StepResult`), leaving the
  intermediate `ProxyEntry` alive on failure for manual recovery rather than
  rolling back. `IssueCertificatePlaybook` is the only one today; the dashboard's
  playbook list (`cloudlink/views.py`) is built to hold more.

## Talking to the cloud

Full REST API reference lives in the cloud repo's `CLAUDE.md`/`README.md`. From
this side, the relevant surface (all via `CloudServerClient`, all
`TokenAuthentication`) is:

| Method | Endpoint | Used by |
|--------|----------|---------|
| POST | `/api/homes/` | `cah.py register` |
| DELETE | `/api/homes/<slug>/` | `deregister` (via `cah.py remove`) |
| PATCH | `/api/homes/<slug>/` | bandwidth-limit form (`cloudlink/views.py`) |
| GET/POST/DELETE | `/api/homes/<slug>/base-domains/...` | base-domain views (`cloudlink/views.py`) |
| POST/DELETE | `/api/homes/<slug>/proxy-mappings/...` | `SyncService`, `_delete_proxy_entry`, proxy-entry create views (`domains/`) |
| GET | `/api/config/inbound-ports/<scheme>/` | `cah.py start`'s `_refresh_inbound_port_ranges` — called directly via `requests`, not through `CloudServerClient`, since it runs before Django/the profile config are bootstrapped (same reason `cmd_register` also calls the cloud with raw `requests`) |
| DELETE | `/api/auth/token/` | `deregister` |

Authentication is a DRF token generated on the cloud dashboard (`RotateTokenView`
there) and pasted into `cah.py register --token ...`; it's stored in that
profile's `config.yaml`.
