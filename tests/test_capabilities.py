"""Per-scope allow and deny lists: resolution, precedence, and where they are enforced."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from django.contrib.auth.models import Permission, User
from django.core.management import CommandError, call_command
from django.test import override_settings

from vinta_state_machines.capabilities import (
    assert_permitted,
    denial_reason,
    has_bypass,
    is_permitted,
    permitted_keys,
    policies_for,
    policy_for,
)
from vinta_state_machines.enums import CapabilityResource, RuleEffect, ScopeType
from vinta_state_machines.exceptions import CapabilityDenied
from vinta_state_machines.guards import register_guard
from vinta_state_machines.models import (
    ScopeCapabilityRule,
    StateMachineScope,
)
from vinta_state_machines.scopes import get_default_scope
from vinta_state_machines.services import define_machine, publish_version, validate_version

pytestmark = pytest.mark.django_db

SIDE_EFFECT = CapabilityResource.SIDE_EFFECT
ACTION = CapabilityResource.ACTION
GUARD = CapabilityResource.GUARD


@register_guard("always", replace=True)
def _always(**context: object) -> bool:
    """A named guard to write rules about; what it decides is beside the point."""
    return True


@pytest.fixture
def acme(db) -> StateMachineScope:
    scope = StateMachineScope(label="Acme")
    scope.scope = "org.1"
    scope.save()
    return scope


@pytest.fixture
def other(db) -> StateMachineScope:
    scope = StateMachineScope(label="Other")
    scope.scope = "org.2"
    scope.save()
    return scope


def rule(scope, resource, effect, pattern) -> ScopeCapabilityRule:
    return ScopeCapabilityRule.objects.create(
        scope=scope, resource=resource, effect=effect, pattern=pattern
    )


# --------------------------------------------------------------------- resolution


def test_a_scope_with_no_rules_is_unrestricted(acme):
    policy = policy_for(acme, SIDE_EFFECT)

    assert policy.unrestricted
    assert policy.permits("anything.at.all")


def test_an_allow_list_excludes_everything_it_does_not_name(acme):
    rule(acme, SIDE_EFFECT, RuleEffect.ALLOW, "testapp.record")

    assert is_permitted(acme, SIDE_EFFECT, "testapp.record")
    assert not is_permitted(acme, SIDE_EFFECT, "testapp.boom")


def test_a_deny_list_excludes_only_what_it_names(acme):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    assert is_permitted(acme, SIDE_EFFECT, "testapp.record")
    assert not is_permitted(acme, SIDE_EFFECT, "testapp.boom")


def test_deny_beats_a_wider_allow(acme):
    """The precedence rule, stated as a test: specificity does not enter into it."""
    rule(acme, SIDE_EFFECT, RuleEffect.ALLOW, "testapp.*")
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    assert is_permitted(acme, SIDE_EFFECT, "testapp.record")
    assert not is_permitted(acme, SIDE_EFFECT, "testapp.boom")


def test_deny_beats_an_exact_allow_of_the_same_key(acme):
    rule(acme, SIDE_EFFECT, RuleEffect.ALLOW, "testapp.boom")
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    assert not is_permitted(acme, SIDE_EFFECT, "testapp.boom")


def test_globs_cover_a_namespace(acme):
    rule(acme, ACTION, RuleEffect.ALLOW, "risk.*")

    assert is_permitted(acme, ACTION, "risk.assess")
    assert not is_permitted(acme, ACTION, "billing.charge")


def test_rules_do_not_leak_between_scopes(acme, other):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    assert not is_permitted(acme, SIDE_EFFECT, "testapp.boom")
    assert is_permitted(other, SIDE_EFFECT, "testapp.boom")


def test_rules_do_not_leak_between_resources(acme):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "shared.key")

    assert not is_permitted(acme, SIDE_EFFECT, "shared.key")
    assert is_permitted(acme, ACTION, "shared.key")


def test_matching_is_case_sensitive(acme):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    assert not is_permitted(acme, SIDE_EFFECT, "testapp.boom")
    # Not a licence to use it: nothing allows the upper-cased spelling either, it is
    # simply a different key. What matters is that the deny is not case-folded away.
    assert is_permitted(acme, SIDE_EFFECT, "TESTAPP.BOOM")


# ------------------------------------------------------- global and tenant intersect


def test_a_global_deny_binds_a_tenant_that_allows_it(acme):
    """The whole reason this is an intersection rather than the machine fallback."""
    rule(get_default_scope(), SIDE_EFFECT, RuleEffect.DENY, "internal.*")
    rule(acme, SIDE_EFFECT, RuleEffect.ALLOW, "internal.purge")

    assert not is_permitted(acme, SIDE_EFFECT, "internal.purge")
    reason = denial_reason(acme, SIDE_EFFECT, "internal.purge")
    assert reason is not None
    assert "installation denies" in reason


def test_a_global_allow_list_binds_every_tenant(acme):
    rule(get_default_scope(), ACTION, RuleEffect.ALLOW, "risk.*")

    assert is_permitted(acme, ACTION, "risk.assess")
    assert not is_permitted(acme, ACTION, "billing.charge")


def test_a_tenant_narrows_the_global_allow_list_further(acme):
    rule(get_default_scope(), ACTION, RuleEffect.ALLOW, "risk.*")
    rule(acme, ACTION, RuleEffect.ALLOW, "risk.assess")

    assert is_permitted(acme, ACTION, "risk.assess")
    assert not is_permitted(acme, ACTION, "risk.mitigate")


def test_a_tenant_cannot_widen_the_global_allow_list(acme):
    rule(get_default_scope(), ACTION, RuleEffect.ALLOW, "risk.*")
    rule(acme, ACTION, RuleEffect.ALLOW, "billing.charge")

    assert not is_permitted(acme, ACTION, "billing.charge")


def test_a_record_with_no_tenant_still_obeys_the_global_rules():
    rule(get_default_scope(), SIDE_EFFECT, RuleEffect.DENY, "internal.*")

    assert not is_permitted(None, SIDE_EFFECT, "internal.purge")
    assert is_permitted(None, SIDE_EFFECT, "testapp.record")


def test_policies_for_resolves_every_resource_in_one_query(acme, django_assert_num_queries):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "a")
    rule(acme, ACTION, RuleEffect.DENY, "b")

    with django_assert_num_queries(1):
        policies = policies_for(acme)

    assert not policies[SIDE_EFFECT].permits("a")
    assert not policies[ACTION].permits("b")
    assert policies[GUARD].unrestricted


# ----------------------------------------------------------------------- the bypass


def test_a_user_without_the_permission_does_not_bypass(acme, user):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    assert not has_bypass(user)
    assert not is_permitted(acme, SIDE_EFFECT, "testapp.boom", actor=user)


def test_a_user_with_the_permission_bypasses_every_rule(acme, db):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")
    staff = User.objects.create_user(username="ops", password="x")
    staff.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="state_machines",
            codename="bypass_capability_policy",
        )
    )
    staff = User.objects.get(pk=staff.pk)

    assert has_bypass(staff)
    assert is_permitted(acme, SIDE_EFFECT, "testapp.boom", actor=staff)


def test_a_superuser_bypasses(acme, db):
    root = User.objects.create_superuser(username="root", email="", password="x")
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    assert is_permitted(acme, SIDE_EFFECT, "testapp.boom", actor=root)


def test_the_permission_checker_setting_decides_the_bypass(acme, user):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    with override_settings(
        STATE_MACHINES={
            "CACHE_GRAPHS": False,
            "PERMISSION_CHECKER": "tests.testapp.tenancy.always_allow",
        }
    ):
        assert is_permitted(acme, SIDE_EFFECT, "testapp.boom", actor=user)


def test_enforcement_can_be_switched_off_without_deleting_the_rules(acme):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    with override_settings(
        STATE_MACHINES={"CACHE_GRAPHS": False, "ENFORCE_CAPABILITY_POLICY": False}
    ):
        assert is_permitted(acme, SIDE_EFFECT, "testapp.boom")

    assert ScopeCapabilityRule.objects.count() == 1
    assert not is_permitted(acme, SIDE_EFFECT, "testapp.boom")


# ------------------------------------------------------------------ filtering keys


def test_assert_permitted_raises_with_the_reason_attached(acme):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    with pytest.raises(CapabilityDenied) as caught:
        assert_permitted(acme, SIDE_EFFECT, "testapp.boom")

    assert caught.value.resource == SIDE_EFFECT
    assert caught.value.key == "testapp.boom"
    assert "denies" in str(caught.value)


def test_assert_permitted_is_silent_when_the_key_is_allowed(acme):
    """The deny above is not this key's; the assertion is that nothing is raised."""
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    assert_permitted(acme, SIDE_EFFECT, "testapp.record")


def test_permitted_keys_keeps_the_order_it_was_given(acme):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "b")

    assert permitted_keys(acme, SIDE_EFFECT, ["c", "b", "a"]) == ["c", "a"]


def test_permitted_keys_returns_everything_for_a_bypassing_actor(acme, db):
    rule(acme, SIDE_EFFECT, RuleEffect.ALLOW, "nothing")
    root = User.objects.create_superuser(username="root2", email="", password="x")

    assert permitted_keys(acme, SIDE_EFFECT, ["a", "b"], actor=root) == ["a", "b"]


# ------------------------------------------------------------------ define_machine


def scoped_definition(risk_definition: dict[str, Any], scope_key: str) -> dict[str, Any]:
    definition = copy.deepcopy(risk_definition)
    definition["scope"] = scope_key
    return definition


def test_define_machine_refuses_a_denied_action(acme, risk_definition):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    with pytest.raises(CapabilityDenied) as caught:
        define_machine(scoped_definition(risk_definition, acme.scope_key))

    assert caught.value.key == "risk.discard"
    assert caught.value.resource == ACTION


def test_define_machine_refuses_a_denied_handler(acme, risk_definition):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.*")
    definition = scoped_definition(risk_definition, acme.scope_key)
    definition["hooks"] = [
        {"handler": "testapp.record", "transition": "assess", "timing": "after"}
    ]

    with pytest.raises(CapabilityDenied):
        define_machine(definition)


def test_define_machine_refuses_a_denied_named_guard(acme, risk_definition):
    rule(acme, GUARD, RuleEffect.ALLOW, "nothing_at_all")
    definition = scoped_definition(risk_definition, acme.scope_key)
    definition["transitions"][1]["guard"] = "@always"

    with pytest.raises(CapabilityDenied):
        define_machine(definition)


def test_define_machine_leaves_an_inline_guard_expression_alone(acme, risk_definition):
    """Only ``@name`` guards are registered keys; an expression is not one."""
    rule(acme, GUARD, RuleEffect.ALLOW, "nothing_at_all")
    definition = scoped_definition(risk_definition, acme.scope_key)

    assert define_machine(definition) is not None


def test_define_machine_can_be_told_to_ignore_the_policy(acme, risk_definition):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    version = define_machine(
        scoped_definition(risk_definition, acme.scope_key), enforce_policy=False
    )

    assert version.transitions.filter(action_type__key="risk.discard").exists()


def test_define_machine_honours_the_bypass_permission(acme, risk_definition, db):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")
    root = User.objects.create_superuser(username="root3", email="", password="x")

    version = define_machine(scoped_definition(risk_definition, acme.scope_key), actor=root)

    assert version.transitions.filter(action_type__key="risk.discard").exists()


def test_the_global_machine_obeys_the_global_rules(risk_definition):
    rule(get_default_scope(), ACTION, RuleEffect.DENY, "risk.discard")

    with pytest.raises(CapabilityDenied):
        define_machine(risk_definition)


# ------------------------------------------------------------ publication only warns


def test_publishing_warns_rather_than_refusing_when_a_rule_arrives_late(acme, risk_definition):
    """A rule written after a draft was drawn must not strand the draft."""
    version = define_machine(scoped_definition(risk_definition, acme.scope_key))
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    report = validate_version(version)

    assert report.ok
    assert any("risk.discard" in warning for warning in report.warnings)
    publish_version(version)
    version.refresh_from_db()
    assert version.lifecycle == "published"


def test_validation_says_nothing_when_the_scope_is_unrestricted(acme, risk_definition):
    version = define_machine(scoped_definition(risk_definition, acme.scope_key))

    assert validate_version(version).warnings == []


# ---------------------------------------------------------------- the audit command


def test_check_scope_capabilities_reports_a_grandfathered_binding(acme, risk_definition, capsys):
    define_machine(scoped_definition(risk_definition, acme.scope_key))
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    call_command("check_scope_capabilities")

    assert "risk.discard" in capsys.readouterr().out


def test_check_scope_capabilities_is_quiet_when_everything_is_in_policy(
    acme, risk_definition, capsys
):
    define_machine(scoped_definition(risk_definition, acme.scope_key))

    call_command("check_scope_capabilities")

    assert "within its scope's policy" in capsys.readouterr().out


def test_check_scope_capabilities_can_fail_the_build(acme, risk_definition):
    define_machine(scoped_definition(risk_definition, acme.scope_key))
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    with pytest.raises(CommandError):
        call_command("check_scope_capabilities", "--fail")


def test_check_scope_capabilities_reports_a_grandfathered_hook(acme, risk_definition, capsys):
    definition = scoped_definition(risk_definition, acme.scope_key)
    definition["hooks"] = [
        {"handler": "testapp.record", "transition": "assess", "timing": "after"}
    ]
    define_machine(definition)
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.*")

    call_command("check_scope_capabilities")

    assert "testapp.record" in capsys.readouterr().out


def test_check_scope_capabilities_reports_a_grandfathered_guard(acme, risk_definition, capsys):
    definition = scoped_definition(risk_definition, acme.scope_key)
    definition["transitions"][1]["guard"] = "@always"
    define_machine(definition)
    rule(acme, GUARD, RuleEffect.ALLOW, "nothing_at_all")

    call_command("check_scope_capabilities")

    assert "always" in capsys.readouterr().out


def test_check_scope_capabilities_filters_by_machine_and_lifecycle(acme, risk_definition, capsys):
    define_machine(scoped_definition(risk_definition, acme.scope_key))
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    call_command(
        "check_scope_capabilities", "--machine", "risk.status", "--lifecycle", "published"
    )

    assert "within its scope's policy" in capsys.readouterr().out


def test_check_scope_capabilities_filters_by_scope(acme, other, risk_definition, capsys):
    define_machine(scoped_definition(risk_definition, acme.scope_key))
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    call_command("check_scope_capabilities", "--scope", other.scope_key)

    assert "within its scope's policy" in capsys.readouterr().out


# ------------------------------------------------------------------- the rule table


def test_the_same_rule_cannot_be_written_twice(acme):
    from django.db import IntegrityError

    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    with pytest.raises(IntegrityError):
        rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")


def test_a_rule_reads_as_its_own_summary(acme):
    row = rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    assert str(row) == "deny side_effect:testapp.boom"


def test_deleting_a_scope_takes_its_rules_with_it(acme):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.boom")

    StateMachineScope.objects.filter(pk=acme.pk).delete()

    assert not ScopeCapabilityRule.objects.exists()


def test_the_global_scope_row_is_an_ordinary_target(acme):
    """The fallback scope is a row, so the installation's own rules hang off it."""
    global_scope = get_default_scope()
    rule(global_scope, ACTION, RuleEffect.DENY, "risk.discard")

    assert global_scope.scope_type == ScopeType.GLOBAL
    assert global_scope.capability_rules.count() == 1
