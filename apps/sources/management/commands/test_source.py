"""Try a source against the live site, the standard way to verify a new source (spec §6.6).

    uv run python manage.py test_source expertwoka --dry-run --limit 5

With `--dry-run` nothing is saved. Without it the source is run for real (jobs are ingested and a
`SourceRun` is recorded). This is one of the only places that makes real network calls to job
sites besides the scheduled worker; never run it from tests.
"""

import json
from dataclasses import asdict
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.jobs.eligibility import is_eligible, parse_locations
from apps.jobs.pay import parse_pay
from apps.sources import http
from apps.sources.models import Source, SourceRun
from apps.sources.services import collect, run_source


class _Probe:
    """Adapts the parsed location data to what `is_eligible` expects."""

    def __init__(self, locations: list[str]) -> None:
        parsed = parse_locations(locations)
        self.eligible_countries = parsed.countries
        self.eligible_regions = parsed.regions
        self.is_global = parsed.is_global
        self.eligibility_known = parsed.known


class Command(BaseCommand):
    help = "Run one source and print what it finds. Use --dry-run to save nothing."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("name", help="Source name, e.g. expertwoka")
        parser.add_argument("--dry-run", action="store_true", help="Fetch and print only.")
        parser.add_argument("--limit", type=int, default=None, help="Fetch at most N jobs.")
        parser.add_argument("--json", action="store_true", help="Print RawJobs as JSON.")
        parser.add_argument("--country", default="KE", help="Country for the eligibility column.")

    def handle(self, *args: Any, **options: Any) -> None:
        source = Source.objects.filter(name=options["name"]).first()
        if source is None:
            known = ", ".join(Source.objects.values_list("name", flat=True)) or "none yet"
            raise CommandError(
                f"No source named {options['name']!r} (known: {known}). "
                "Run `manage.py seed_sources` to create the built-in ones."
            )
        if not options["dry_run"]:
            run = run_source(source, trigger=SourceRun.Trigger.MANUAL, limit=options["limit"])
            if run is None:
                raise CommandError("Another run of this source is in progress.")
            self.stdout.write(
                f"Run #{run.pk}: {'OK' if run.success else 'FAILED'}; ids seen {run.ids_seen}, "
                f"new {run.new_jobs}, updated {run.updated_jobs}, errors {run.errors}"
            )
            return

        client = http.build_client()
        try:
            out = collect(source, client=client, seen_ids=set(), limit=options["limit"])
        finally:
            client.close()
        if options["json"]:
            self.stdout.write(json.dumps([asdict(r) for r in out.raw_jobs], default=str, indent=2))
        else:
            self._print_table(out.raw_jobs, options["country"])
        self.stdout.write(
            f"\n{len(out.raw_jobs)} job(s) parsed, {len(out.ids_seen)} id(s) seen on the list page."
        )
        problems = ([out.fatal] if out.fatal else []) + out.errors
        if problems:
            self.stdout.write(self.style.WARNING(f"Problems: {problems}"))

    def _print_table(self, jobs: list[Any], country: str) -> None:
        for job in jobs:
            pay = parse_pay(job.pay_text)
            elig = is_eligible(_Probe(job.locations), country)
            verdict = {True: "eligible", False: "not eligible", None: "unclear"}[elig]
            self.stdout.write(f"- {job.title}  [{job.company}]")
            self.stdout.write(f"    url:       {job.url}")
            self.stdout.write(f"    apply:     {job.apply_url or '-'}")
            self.stdout.write(
                f"    locations: {', '.join(job.locations) or '-'}  ({country}: {verdict})"
            )
            self.stdout.write(
                f"    pay:       {job.pay_text or '-'}"
                + (
                    f"  => {pay.minimum}-{pay.maximum} {pay.currency}/{pay.period}"
                    if pay.parsed
                    else ""
                )
            )
            self.stdout.write(
                f"    work type: {job.work_type or '-'}   posted: {job.posted_at or '-'}"
            )
