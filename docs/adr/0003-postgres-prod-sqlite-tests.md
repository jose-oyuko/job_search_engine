# 3. PostgreSQL in dev and prod, SQLite in tests

- Status: Accepted
- Date: 2026-10-21

## Context

Production needs a real database with concurrent writers. Tests need to be fast and need no services.

## Decision

Run PostgreSQL 16 in development and production. Run the test suite on in-memory SQLite. Use the ORM only: no raw SQL and no features that Django does not support on both databases.

## Consequences

Fast, dependency-free tests. The risk is behavioural differences between the two databases; the ORM-only rule and a CI run against SQLite keep it small, and a Postgres CI job can be added if a difference ever bites.
