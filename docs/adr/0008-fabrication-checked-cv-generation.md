# 8. Fabrication-checked CV generation

- Status: Accepted
- Date: 2026-10-21

## Context

A tailored CV that invents experience, numbers or skills could cost users a job and damages trust.

## Decision

The LLM only selects and rephrases. Facts come from the user's stored profile, and generated CV data references profile items by ID. A validator (`kits/validation.py`) rejects unknown IDs, invented figures and banned phrases, and generation retries or fails visibly instead of shipping unchecked output.

## Consequences

Safer documents at the cost of extra validation code and occasionally failed generations. The validator is a core requirement and is tested with deliberately fabricated outputs.
