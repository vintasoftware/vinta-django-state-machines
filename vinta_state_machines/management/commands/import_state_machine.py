"""Create a draft version from a JSON or YAML-ish definition file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand, CommandError

from vinta_state_machines.exceptions import CapabilityDenied
from vinta_state_machines.services import define_machine, publish_version

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    help = "Import one or more state machine definitions from a JSON file."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("path", type=Path, help="JSON file with one definition or a list.")
        parser.add_argument(
            "--publish",
            action="store_true",
            help="Publish each imported version and make it the machine's default.",
        )
        parser.add_argument(
            "--no-default",
            action="store_true",
            help="With --publish, do not make the new version the default.",
        )
        parser.add_argument(
            "--ignore-policy",
            action="store_true",
            help=(
                "Import even where the target scope's capability rules forbid a key. "
                "For the operator who wrote the rules and is deliberately seeding "
                "around them."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path: Path = options["path"]
        try:
            payload = json.loads(path.read_text())
        except OSError as exc:
            raise CommandError(f"Cannot read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc

        definitions = payload if isinstance(payload, list) else [payload]
        for definition in definitions:
            try:
                version = define_machine(definition, enforce_policy=not options["ignore_policy"])
            except CapabilityDenied as exc:
                raise CommandError(f"{exc} Pass --ignore-policy to import anyway.") from exc
            self.stdout.write(self.style.SUCCESS(f"Created draft {version}"))
            if options["publish"]:
                report = publish_version(version, make_default=not options["no_default"])
                for warning in report.warnings:
                    self.stdout.write(self.style.WARNING(f"  {warning}"))
                self.stdout.write(self.style.SUCCESS(f"Published {version}"))
