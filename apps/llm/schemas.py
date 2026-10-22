"""Pydantic output schemas for LLM tasks. Each later phase adds its own (JobExtraction in
Phase 4, CV data in Phase 7, and so on); Phase 2 only needs the smoke-test schema."""

from pydantic import BaseModel


class SmokeResult(BaseModel):
    ok: bool
    message: str
