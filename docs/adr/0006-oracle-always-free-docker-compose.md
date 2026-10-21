# 6. Oracle Always Free ARM VM with Docker Compose

- Status: Accepted
- Date: 2026-10-21

## Context

The only acceptable fixed cost is a domain name. The app must run in under 2 GB of RAM.

## Decision

Deploy to one Oracle Cloud Always Free ARM VM using Docker Compose (web, worker, bot, db, caddy). Images are built for amd64 and arm64. Caddy provides automatic HTTPS.

## Consequences

Free hosting with enough capacity. It is a single point of failure, so daily backups to remote storage are required. Free-tier availability and policies are outside our control.
