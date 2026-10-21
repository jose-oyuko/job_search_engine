# 1. Django monolith with HTMX instead of a SPA

- Status: Accepted
- Date: 2026-10-21

## Context

JobPilot is built and run by one developer, used mostly on phones over mobile data, and must stay under about 2 GB of RAM. The UI is forms, lists, a kanban board and document downloads.

## Decision

Build one Django application with server-rendered templates. Use HTMX for partial updates and Alpine.js for small client-side state. Tailwind comes from the standalone CLI, so there is no Node build and no SPA framework. A JSON API can be added later because business logic lives in each app's `services.py`.

## Consequences

One deployable, one language, small pages and no JS toolchain to maintain. Highly interactive features (for example the kanban board) need care with HTMX and Sortable. A future mobile app would need an API, which the service layer makes straightforward.
