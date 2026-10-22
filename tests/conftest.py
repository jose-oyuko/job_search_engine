import logging
from collections.abc import Iterator

import pytest
from django.core.cache import cache

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
