# 10. LLM client design: one logical call per row, quota groups, fake-provider hints

- Status: Accepted
- Date: 2026-10-22

## Context

Phase 2 builds the client described in spec §11. A few details were left open: how a repair
retry is logged, what counts towards each quota, and how the fake provider can return
schema-valid output without a fixture.

## Decision

- **One `LLMCall` row per logical call.** A repair retry is folded into the same row (tokens and cost
  summed) and logged as a WARNING `llm_repair_retry`. Cache hits get a row with `cache_hit=True` and
  zero cost, so hit rates are visible. Quota and configuration errors raise before any provider
  call and write no row.
- **No content in rows or logs.** `error` holds an exception type or failing field names, never
  values. Prompt and response bodies are stored only when `LLM_LOG_BODIES=True`.
- **Quota groups** (calendar month, UTC): `scores` counts `score_match`, `kits` counts `generate_cv`
  (one per kit), `manual` counts `extract_job` and `extract_page`. Only successful, non-cached
  calls count; the USD cap sums every row, including failures. The other kit documents and
  `parse_cv` are bounded by the USD cap only. Counters are read without locking, so a race can
  overshoot by one call.
- **System work** (`user=None`) is bounded by `LLM_SYSTEM_DAILY_USD_CAP` over the UTC day.
- **Provider interface** gains optional `task` and `schema` keyword hints. Real providers ignore
  them; the fake provider uses `task` to find `tests/fixtures/llm/<task>.json` and `schema` to build
  a schema-valid default.
- **Extra task value** `smoke` for the `llm_smoke` command, and extra settings `LLM_JSON_MODE`,
  `LLM_MAX_TOKENS_PARAM` and `LLM_TIMEOUT_SECONDS` so providers that differ from OpenAI's defaults
  work without code changes.
- **Cache keys belong to the caller.** The cache is keyed by (key, prompt version); anything
  user-specific must be part of the key.

## Consequences

Usage pages and the admin summary are simple aggregate queries. A prompt edit must bump `version`
or old cached results will be reused. Phase 7 may need to revisit how a kit is counted.
