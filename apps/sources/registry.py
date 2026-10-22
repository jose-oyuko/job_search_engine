"""Maps a `Source` to its fetcher class (spec §6.4).

Fetchers register with `@register("<kind>")`, or `@register_custom("<source name>")` for one-off
plugins in `custom/`. Modules in `generic/` and `custom/` are imported on first use (and at app
start-up), so adding a file there is enough to register it.
"""

import importlib
import pkgutil
from collections.abc import Callable

from .base import BaseSource
from .exceptions import UnknownSourceKind
from .models import Source

_REGISTRY: dict[str, type[BaseSource]] = {}
_loaded = False


def register(kind: str) -> Callable[[type[BaseSource]], type[BaseSource]]:
    def decorator(cls: type[BaseSource]) -> type[BaseSource]:
        cls.kind = kind
        _REGISTRY[kind] = cls
        return cls

    return decorator


def register_custom(name: str) -> Callable[[type[BaseSource]], type[BaseSource]]:
    def decorator(cls: type[BaseSource]) -> type[BaseSource]:
        cls.kind = "custom"
        _REGISTRY[f"custom:{name}"] = cls
        return cls

    return decorator


def load_all() -> None:
    """Import every module under `generic/` and `custom/` so their decorators run."""
    global _loaded  # noqa: PLW0603
    if _loaded:
        return
    for package in ("apps.sources.generic", "apps.sources.custom"):
        module = importlib.import_module(package)
        for info in pkgutil.iter_modules(module.__path__):
            importlib.import_module(f"{package}.{info.name}")
    _loaded = True


def fetcher_class(source: Source) -> type[BaseSource]:
    load_all()
    key = f"custom:{source.name}" if source.kind == Source.Kind.CUSTOM else source.kind
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownSourceKind(f"no fetcher registered for {key!r}") from None


def registered_keys() -> list[str]:
    load_all()
    return sorted(_REGISTRY)
