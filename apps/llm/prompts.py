"""Prompt loader: versioned template files with front-matter (spec §11.1).

File format (`apps/llm/prompts/<name>.md`):

    ---
    version: 1
    task: score_match
    tier: fast
    ---
    @@system
    You are ...
    @@user
    Rate this job: {{ job }}

Without `@@` markers the whole body is the user message. Substitution is a single pass over
`{{ name }}` placeholders, so a value that itself contains `{{ x }}` is never expanded again.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.conf import settings

from .exceptions import PromptError

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_TIERS = {"fast", "writer"}

DEFAULT_SYSTEM = "You are a careful assistant that follows the instructions exactly."


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    task: str
    tier: str
    system: str
    user: str

    def render(self, variables: dict[str, Any]) -> tuple[str, str]:
        """Return `(system, user)` with placeholders filled in. A missing variable is an error."""
        return _substitute(self.system, variables, self.name), _substitute(
            self.user, variables, self.name
        )


def _substitute(template: str, variables: dict[str, Any], name: str) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise PromptError(f"prompt {name!r} needs variable {key!r}")
        return str(variables[key])

    return _PLACEHOLDER_RE.sub(replace, template)


def _parse(name: str, raw: str) -> Prompt:
    raw = raw.replace("\r\n", "\n")
    match = _FRONT_MATTER_RE.match(raw)
    if not match:
        raise PromptError(f"prompt {name!r} has no front-matter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if line.strip():
            key, sep, value = line.partition(":")
            if not sep:
                raise PromptError(f"prompt {name!r} has a bad front-matter line")
            meta[key.strip()] = value.strip()
    for required in ("version", "task", "tier"):
        if not meta.get(required):
            raise PromptError(f"prompt {name!r} is missing front-matter key {required!r}")
    if meta["tier"] not in _TIERS:
        raise PromptError(f"prompt {name!r} has an unknown tier")

    body = match.group(2)
    system, user = DEFAULT_SYSTEM, body
    if "@@user" in body:
        before, _, user = body.partition("@@user")
        if "@@system" in before:
            system = before.partition("@@system")[2]
    elif "@@system" in body:
        raise PromptError(f"prompt {name!r} has @@system but no @@user section")
    return Prompt(
        name=name,
        version=meta["version"],
        task=meta["task"],
        tier=meta["tier"],
        system=system.strip(),
        user=user.strip(),
    )


@lru_cache(maxsize=64)
def _load(directory: str, name: str) -> Prompt:
    path = Path(directory) / f"{name}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptError(f"prompt {name!r} not found") from exc
    return _parse(name, raw)


def load_prompt(name: str) -> Prompt:
    if not _NAME_RE.match(name):
        raise PromptError("invalid prompt name")
    return _load(str(settings.LLM_PROMPTS_DIR), name)


def clear_prompt_cache() -> None:
    _load.cache_clear()
