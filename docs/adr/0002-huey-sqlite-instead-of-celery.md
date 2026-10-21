# 2. Huey with SQLite storage instead of Celery and Redis

- Status: Accepted
- Date: 2026-10-21

## Context

We need scheduled and background work (fetching, scoring, kit generation, notifications, email polling) on a single small VM. Celery plus Redis adds two moving parts and memory.

## Decision

Use Huey (`huey.contrib.djhuey`) with `SqliteHuey` storage. The Huey SQLite file lives on a volume shared by the web, worker and bot containers. Every task is wrapped in `@logged_task` and is idempotent.

## Consequences

No broker to run. Throughput and concurrency are modest (thread workers), which is enough at this scale. SQLite file locking limits us to a single host; moving to Redis later is a settings change plus a volume removal.
