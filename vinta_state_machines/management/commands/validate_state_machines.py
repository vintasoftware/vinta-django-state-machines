"""Check every version's graph, and report which side effects are missing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand, CommandError

from vinta_state_machines.models import StateMachineVersion
from vinta_state_machines.services import validate_version
from vinta_state_machines.side_effects import registered_side_effects

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    help = "Validate state machine versions and list registered side effects."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--machine", help="Only check this state machine key.")
        parser.add_argument(
            "--lifecycle",
            choices=("draft", "published", "archived"),
            help="Only check versions in this lifecycle.",
        )
        parser.add_argument(
            "--list-side-effects",
            action="store_true",
            help="Print every registered side effect key and exit.",
        )
        parser.add_argument(
            "--fail-on-warning", action="store_true", help="Treat warnings as failures."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["list_side_effects"]:
            for key in registered_side_effects():
                self.stdout.write(key)
            return

        queryset = StateMachineVersion.objects.select_related("state_machine")
        if options.get("machine"):
            queryset = queryset.for_machine(options["machine"])
        if options.get("lifecycle"):
            queryset = queryset.filter(lifecycle=options["lifecycle"])

        failures = 0
        for version in queryset:
            report = validate_version(version)
            for error in report.errors:
                self.stderr.write(self.style.ERROR(f"{version}: {error}"))
            for warning in report.warnings:
                self.stdout.write(self.style.WARNING(f"{version}: {warning}"))
            if report.errors or (options["fail_on_warning"] and report.warnings):
                failures += 1
            elif not report.warnings:
                self.stdout.write(self.style.SUCCESS(f"{version}: ok"))

        if failures:
            raise CommandError(f"{failures} version(s) failed validation.")
