import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import respx
from django.core.cache import cache

from apps.llm.prompts import clear_prompt_cache
from apps.llm.providers import _build, get_provider
from apps.llm.providers.fake import FakeProvider
from tests.kit_helpers import pdf_fallback  # noqa: F401  (fixture shared by the kit tests)
from tests.logcapture import ListHandler


@pytest.fixture
def handler() -> Iterator[ListHandler]:
    """Attach a JSON-capturing handler to the root logger for the duration of a test."""
    h = ListHandler()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    yield h
    root.removeHandler(h)
    root.setLevel(old_level)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Rate-limit counters live in the cache; start every test with a clean slate."""
    cache.clear()


@pytest.fixture(autouse=True)
def _reset_llm_caches() -> Iterator[None]:
    """The provider and prompt files are cached; tests that change settings need a fresh start."""
    _build.cache_clear()
    clear_prompt_cache()
    yield
    _build.cache_clear()
    clear_prompt_cache()


@pytest.fixture
def fake_llm() -> FakeProvider:
    """The shared fake provider (test settings). Push to `.script` to control responses."""
    provider = get_provider()
    assert isinstance(provider, FakeProvider)
    return provider


@pytest.fixture
def prompts_dir(tmp_path: Path, settings) -> Path:  # type: ignore[no-untyped-def]
    """A temp prompts directory with two small prompts, wired into settings."""
    score = [
        "---",
        "version: 3",
        "task: score_match",
        "tier: fast",
        "---",
        "@@system",
        "You score jobs.",
        "@@user",
        "Score this: {{ job }}",
        "",
    ]
    letter = [
        "---",
        "version: 1",
        "task: generate_cover_letter",
        "tier: writer",
        "---",
        "Write a letter for {{ job }}.",
        "",
    ]
    (tmp_path / "score_test.md").write_text("\n".join(score), encoding="utf-8")
    (tmp_path / "letter_test.md").write_text("\n".join(letter), encoding="utf-8")
    settings.LLM_PROMPTS_DIR = tmp_path
    clear_prompt_cache()
    return tmp_path


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    """HTTP mocking for source tests: any request that is not mocked fails the test, so a fetcher
    can never reach a real site."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock
