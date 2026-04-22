# ADR 0001 — No Caddy; reuse existing Traefik for Grafana proxy

Date: 2026-04-22
Status: Accepted
Scope: Phase 0

## Context

Design.md I.2 lists Caddy as [FIRME] reverse proxy choice. VPS already runs
a Traefik instance (`traefik-u6lx-traefik-1`) for an unrelated project
(openclaw) which occupies ports 80 and 443 and holds an active Let's Encrypt
volume. Caddy needs port 80 for HTTP-01 ACME challenge; it cannot coexist
with the running Traefik on that port.

Options considered:

- (a) Remove openclaw Traefik. Rejected — openclaw is in active use.
- (b) Caddy on alternate ports with DNS-01 challenge. Rejected — requires a
  DNS provider API token and a real domain; current hostname is
  `187-124-130-221.nip.io` with no admin API.
- (c) Reuse the existing Traefik to expose `tea-grafana`. Accepted.

## Decision

Remove `tea-caddy` from `docker-compose.yml`. The `tea-grafana` container
joins a shared Docker network (`tea_edge`) that Traefik also reaches, and
Grafana is routed as a new Traefik router behind the existing certificate
resolver. Seven containers in the Phase 0 stack, not eight.

Phase 0 acceptance criterion 1 is updated in `Docs/runbook.md` to reflect
"7 containers Up (Caddy removed — reverse proxy delegated to existing
Traefik)".

## Consequences

- One less moving part in the stack. Grafana HTTPS depends on the
  external Traefik's health.
- If openclaw is retired later, Caddy can be reintroduced without
  re-architecting: restore the service block in compose and remove the
  Traefik router file. The compose network layout already separates
  `tea_edge` from `tea_internal`, so the plug point is clean.
- Divergence from Design.md I.2 is intentional and logged here.

## Revisit

Revisit if: (1) openclaw is decommissioned, (2) we acquire a real domain
with DNS-01-capable provider, or (3) Traefik routing becomes a
troubleshooting bottleneck.
