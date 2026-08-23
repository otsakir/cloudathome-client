# Forwards and certificates

How to expose a service — HTTP/HTTPS by domain name, or a raw TCP port — and how
to get it a TLS certificate.

## Base domains

Before creating any HTTP/HTTPS proxy entry, the home must register at least one
base domain with the cloud server. A base domain is a domain the operator
controls in DNS — the cloud server enforces that no two homes can claim the same
domain or overlapping domains (e.g. if Home A owns `example.com`, Home B cannot
register `sub.example.com`).

The cloud validates that the domain is a proper registrable domain (not a bare
TLD like `com` or a public suffix like `co.uk`) using the Public Suffix List.

**From the Home Console dashboard:**
- Click **Register base domain**, enter the domain name, and submit.
- The domain is stored on the cloud server and returned in the home's info response.
- To remove a domain, click **Remove** next to it on the dashboard. This is
  blocked with an error if any active proxy mappings still use that domain or
  its subdomains — disconnect those mappings first.

A home can register multiple base domains. Subdomains do not need to be
registered separately — once `example.com` is registered, the home can freely
create proxy entries for `blog.example.com`, `api.example.com`, etc.

## HTTP/HTTPS forwards

From a domain's detail page, click **Add** to create a proxy entry: choose
scheme **HTTP** or **HTTPS**, and the local port the service listens on. Each
forward registers a mapping directly in HAProxy on the cloud server (no
persistent cloud-side state beyond the mapping itself) and records the
allocated tunnel port locally. HTTP/HTTPS forwards are only accepted if the
hostname falls under one of the home's registered base domains.

A domain can hold **one HTTP entry and one HTTPS entry at the same time** — this
is what makes certificate renewal painless later: you can re-run certificate
issuance against the HTTP entry at any time without ever taking a live HTTPS
forward down.

### Custom inbound ports

By default, an HTTP/HTTPS proxy entry publishes on the cloud's standard port (80
for HTTP, 443 for HTTPS). The cloud server may also advertise a shared alternate
port range — useful if your network blocks outbound access to the standard
ports, or you want more than one independent entry point. This range is **not**
allocated per-home like the TCP range; any home can use any port in it, since
HTTP/HTTPS forwards are routed by hostname, not port alone.

**From the Home Console:** the **Add proxy entry** page shows the currently
advertised HTTP and HTTPS ranges (if the cloud offers one) alongside the
**Public port** field. Leave it blank for the standard port, or enter a port
from the range shown for the scheme you're adding. The cloud validates the port
server-side regardless of what the form shows.

This range is cloud-wide config, not something you set — `python cah.py start`
re-fetches it from the cloud (`GET /api/config/inbound-ports/<scheme>/`) every
time and caches it into that profile's `config.yaml`
(`cloudlink.http_ports`/`https_ports`), so it stays current across restarts
without a network call on every page load. See
[configuration.md](configuration.md#profile-configyaml).

## TCP forwards

For a raw TCP service (no domain, no TLS) use **Add TCP forward** from the
dashboard: pick a **public port** (must fall within your home's allocated TCP
range — see the dashboard for the current range) and the local port the service
listens on. Unlike HTTP/HTTPS, a TCP public port is allocated per-home, so no
two homes can share the same one. Incoming TCP traffic hits HAProxy on that
public port and is routed by destination port through the tunnel — no hostname
or certificate involved.

## Obtaining a TLS certificate

Certificate issuance is tied to a proxy entry, and it must be an **HTTP**-scheme
one — the ACME HTTP-01 challenge always validates over plain port 80, which only
an HTTP proxy entry's tunnel is routed to on the cloud side. An HTTPS entry's
tunnel is only reachable via the SNI-routed port-443 frontend, so the Home
Console doesn't offer "Issue certificate" there at all.

There are two ways to do this — pick whichever fits:

### Option A: the one-click playbook (recommended for a first certificate)

From the dashboard, under **Playbooks**, click **Issue certificate**. Give it
the domain name, your email, and a local port for certbot to listen on. It
handles everything in one step: creates the domain record if needed, creates or
reuses an HTTP proxy entry, opens the tunnel (reusing one that's already open),
runs certbot, and — on success — tears the temporary HTTP entry back down again.

If any step fails, it stops and leaves whatever it already created in place
(visible from the domain/proxy-entry detail pages) so you can investigate or
retry, rather than rolling back silently.

### Option B: the manual, step-by-step flow

Useful if you want an HTTP entry to stick around (e.g. you plan to add the HTTPS
entry right after), or you're troubleshooting a failure from option A.

1. **Add a domain** — go to **Domains → Add domain** and enter the domain name
   (e.g. `mysite.example.com`). DNS must already point to the cloud server.
2. **Add a proxy entry** — from the domain detail page click **Add**, choose
   scheme **HTTP**, and pick the local port certbot will listen on (e.g.
   `8082`). This registers the proxy mapping on the cloud server; the tunnel
   port is allocated server-side.
3. **Open the tunnel** — on the proxy entry detail page click **Open tunnel**.
   This starts an SSH reverse tunnel: `cloud_tunnel_port → home:home_port`.
4. **Issue the certificate** — with the tunnel open, click **Issue
   certificate**. Enter your email on the certificate page and submit. Certbot
   runs in standalone mode, Let's Encrypt validates the HTTP-01 challenge
   through the tunnel, and the certificate is saved to
   `providers/<profile>/certbot/config/live/<domain>/`.
5. **Add an HTTPS proxy entry for real traffic** — add a second proxy entry
   (scheme **HTTPS**) for the same domain, pointing at your actual
   TLS-terminated local service, and open its tunnel too.

The domain record is updated with the certificate path and expiry date on
success either way.

### Auto-deploying the certificate elsewhere

If another service on the home machine needs the certificate files directly
(e.g. nginx), set `certbot.deploy_path` in `config.yaml` (or a per-domain
override from the Home Console) — see
[configuration.md](configuration.md#profile-configyaml). After each successful
issuance, `fullchain.pem`/`privkey.pem`/`chain.pem`/`cert.pem` are copied to
`<deploy_path>/<domain>/`.

### Renewal

Re-run either flow above against the same domain at any time — the HTTP entry
is reused if one already exists, and issuing again simply replaces the
certificate and updates the expiry date. Nothing needs to be torn down first,
including a live HTTPS forward for the same domain.
