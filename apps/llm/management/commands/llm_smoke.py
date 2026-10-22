"""Manual check of the configured provider: one tiny structured call. Never run in tests.

uv run python manage.py llm_smoke
"""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.llm.client import call_structured
from apps.llm.exceptions import LLMError
from apps.llm.models import LLMCall
from apps.llm.schemas import SmokeResult


class Command(BaseCommand):
    help = "Run one tiny structured call against the configured LLM provider."

    def handle(self, *args: Any, **options: Any) -> None:
        provider = settings.LLM_PROVIDER
        self.stdout.write(f"Provider: {provider}")
        if provider == "fake":
            self.stdout.write(
                self.style.WARNING("LLM_PROVIDER=fake: this checks the plumbing, not a real model.")
            )
        try:
            result = call_structured(
                task=LLMCall.Task.SMOKE,
                prompt_name="smoke_test",
                variables={"word": "JobPilot"},
                schema=SmokeResult,
                user=None,
                max_output_tokens=100,
            )
        except LLMError as exc:
            raise CommandError(
                f"LLM call failed: {type(exc).__name__}: {exc.args[0] if exc.args else exc}"
            ) from exc

        call = LLMCall.objects.order_by("-id").first()
        self.stdout.write(self.style.SUCCESS(f"ok={result.ok} message={result.message!r}"))
        if call is not None:
            self.stdout.write(
                f"model={call.model} tokens={call.input_tokens}+{call.output_tokens} "
                f"cost=${call.cost_usd} latency={call.latency_ms}ms (LLMCall #{call.pk})"
            )
