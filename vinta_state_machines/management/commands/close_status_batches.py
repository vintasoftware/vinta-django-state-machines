"""Find stuck fan-out batches and finish or fail them."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from vinta_state_machines.sweeper import sweep


class Command(BaseCommand):
    help = "Repair, claim, re-dispatch and time out fan-out batches. Run about once a minute."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=1000,
            help="Most batches to touch per pass, per stage. Default 1000.",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Say nothing when there was nothing to do, which is the usual case.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        report = sweep(limit=options["limit"])
        if report.total == 0 and options["quiet"]:
            return
        style = self.style.SUCCESS if report.total else self.style.NOTICE
        self.stdout.write(style(str(report)))
