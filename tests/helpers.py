"""Small test helpers."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest


@contextmanager
def raises_detail(
    exc_type: type[Exception], text: str
) -> Iterator[pytest.ExceptionInfo[Exception]]:
    """Like `pytest.raises(..., match=...)`, but matches the exception's first argument.

    LLM exceptions keep `str(exc)` user-safe and put the operator detail in `args[0]`.
    """
    with pytest.raises(exc_type) as info:
        yield info
    assert text in str(info.value.args[0] if info.value.args else "")
