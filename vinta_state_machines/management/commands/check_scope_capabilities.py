"""Find versions wired up with keys their scope's policy no longer allows.

Policy is enforced where wiring is *written*, and publication only warns, which leaves
one gap on purpose: a rule written today says nothing about the graphs published under
yesterday's rules.  This is how you find them -- run it after changing a policy, and in
CI if a drifting installation should fail the build.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand, CommandError

from vinta_state_machines.capabilities import policies_for
from vinta_state_machines.enums import CapabilityResource
from vinta_state_machines.guards import NAMED_GUARD_PREFIX
from vinta_state_machines.models import StateMachineVersion

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    help = "Report state machine versions that use keys their scope's policy forbids."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--machine", help="Only check this state machine key.")
        parser.add_argument("--scope", help="Only check machines in this scope key.")
        parser.add_argument(
            "--lifecycle",
            choices=("draft", "published", "archived"),
            help="Only check versions in this lifecycle.",
        )
        parser.add_argument(
            "--fail",
            action="store_true",
            help="Exit non-zero when anything is reported, for CI.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        queryset = StateMachineVersion.objects.select_related(
            "state_machine", "state_machine__scope"
        )
        if options.get("machine"):
            queryset = queryset.for_machine(options["machine"])
        if options.get("scope"):
            queryset = queryset.filter(state_machine__scope__scope_key=options["scope"])
        if options.get("lifecycle"):
            queryset = queryset.filter(lifecycle=options["lifecycle"])

        # One resolution per scope rather than per version: a machine's rules do not
        # change between its versions, and a big installation has far more versions
        # than tenants.
        cache: dict[Any, dict[str, Any]] = {}
        findings = 0

        for version in queryset:
            scope = version.state_machine.scope
            if scope.pk not in cache:
                cache[scope.pk] = policies_for(scope)
            policies = cache[scope.pk]
            for problem in _problems(version, policies):
                findings += 1
                self.stdout.write(self.style.WARNING(f"{version}: {problem}"))

        if not findings:
            self.stdout.write(self.style.SUCCESS("Every version is within its scope's policy."))
            return
        message = f"{findings} binding(s) outside the policy of their scope."
        if options["fail"]:
            raise CommandError(message)
        self.stdout.write(message)


def _problems(version: StateMachineVersion, policies: dict[str, Any]) -> list[str]:
    actions = policies[CapabilityResource.ACTION]
    guards = policies[CapabilityResource.GUARD]
    effects = policies[CapabilityResource.SIDE_EFFECT]
    if actions.unrestricted and guards.unrestricted and effects.unrestricted:
        return []

    found: list[str] = []
    for transition in version.transitions.select_related("action_type"):
        if transition.action_type_id is not None:
            reason = actions.reason(transition.action_type.key)
            if reason is not None:
                found.append(f"transition {transition.name!r} — {reason}")
        guard = (transition.guard or "").strip()
        if guard.startswith(NAMED_GUARD_PREFIX):
            reason = guards.reason(guard[1:].strip())
            if reason is not None:
                found.append(f"transition {transition.name!r} — {reason}")
    for hook in version.hooks.filter(is_active=True):
        reason = effects.reason(hook.handler_key)
        if reason is not None:
            found.append(f"hook {hook.pk} — {reason}")
    return found
