# 7. Own thin LLM wrapper over an OpenAI-compatible API, with quotas

- Status: Accepted
- Date: 2026-10-21

## Context

LLM usage is paid and must be metered and capped per user. Providers change, and tests must never hit a real provider.

## Decision

Write a small client in `apps/llm` that speaks the OpenAI-compatible HTTP API (OpenAI, OpenRouter, Groq and others), plus a Fake provider for tests. All calls go through it: it enforces quotas, validates structured output with pydantic, and records cost in `LLMCall`.

## Consequences

One place to meter, cap, log and test. We maintain a little provider code ourselves instead of adopting a large framework.
