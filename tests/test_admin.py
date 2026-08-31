"""The admin is the authoring UI, so its publish and validate actions are covered here."""

from __future__ import annotations

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.messages import constants
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.base import SessionBase
from django.test import RequestFactory

from vinta_state_machines.admin import (
    StateMachineVersionAdmin,
    StatusTransitionAdmin,
    TransitionInline,
)
from vinta_state_machines.enums import Lifecycle
from vinta_state_machines.models import StateMachineVersion
from vinta_state_machines.services import clone_version

pytestmark = pytest.mark.django_db


@pytest.fixture
def site() -> AdminSite:
    return AdminSite()


@pytest.fixture
def version_admin(site) -> StateMachineVersionAdmin:
    return StateMachineVersionAdmin(StateMachineVersion, site)


@pytest.fixture
def request_with_messages(user):
    """A POST request carrying message storage, which is where admin actions report."""
    request = RequestFactory().post("/admin/")
    request.user = user
    request.session = SessionBase()
    request._messages = FallbackStorage(request)
    return request


def messages_of(request, level=None):
    return [
        message.message for message in request._messages if level is None or message.level == level
    ]


# ---------------------------------------------------------------------- publish


def test_the_publish_action_publishes_a_draft(version_admin, request_with_messages, risk_draft):
    version_admin.publish(request_with_messages, StateMachineVersion.objects.all())

    risk_draft.refresh_from_db()
    assert risk_draft.lifecycle == Lifecycle.PUBLISHED
    assert risk_draft.author_id == request_with_messages.user.pk
    assert risk_draft.state_machine.default_version_id == risk_draft.pk
    assert "1 version published." in messages_of(request_with_messages)


def test_the_publish_action_reports_an_invalid_draft_instead_of_failing(
    version_admin, request_with_messages, risk_draft
):
    risk_draft.states.update(is_initial=False)
    version_admin.publish(request_with_messages, StateMachineVersion.objects.all())

    risk_draft.refresh_from_db()
    assert risk_draft.lifecycle == Lifecycle.DRAFT
    errors = messages_of(request_with_messages, constants.ERROR)
    assert any("declares no initial state" in message for message in errors)


def test_the_publish_action_surfaces_warnings_it_did_not_block_on(
    version_admin, request_with_messages, risk_draft
):
    risk_draft.states.filter(status__key="mitigated").update(is_terminal=False)
    version_admin.publish(request_with_messages, StateMachineVersion.objects.all())

    risk_draft.refresh_from_db()
    assert risk_draft.lifecycle == Lifecycle.PUBLISHED
    warnings = messages_of(request_with_messages, constants.WARNING)
    assert any("not marked terminal" in message for message in warnings)


# ------------------------------------------------------------------------ clone


def test_the_clone_action_copies_a_version_into_a_new_draft(
    version_admin, request_with_messages, risk_version
):
    version_admin.clone(request_with_messages, StateMachineVersion.objects.all())

    draft = StateMachineVersion.objects.get(version="2")
    assert draft.lifecycle == Lifecycle.DRAFT
    assert draft.state_machine_id == risk_version.state_machine_id
    assert draft.states.count() == risk_version.states.count()
    assert draft.transitions.count() == risk_version.transitions.count()
    assert "Cloned from 1." in draft.notes
    assert "1 draft created: risk.status@2." in messages_of(request_with_messages)


def test_the_clone_action_leaves_the_version_it_copied_alone(
    version_admin, request_with_messages, risk_version, risk_machine
):
    version_admin.clone(request_with_messages, StateMachineVersion.objects.all())

    risk_version.refresh_from_db()
    risk_machine.refresh_from_db()
    assert risk_version.lifecycle == Lifecycle.PUBLISHED
    # A draft is not what new records pin, so the default must not have moved.
    assert risk_machine.default_version_id == risk_version.pk


def test_cloning_twice_does_not_collide_on_the_label(
    version_admin, request_with_messages, risk_version
):
    published = StateMachineVersion.objects.filter(pk=risk_version.pk)
    version_admin.clone(request_with_messages, published)
    version_admin.clone(request_with_messages, published)

    assert set(StateMachineVersion.objects.values_list("version", flat=True)) == {"1", "2", "3"}


def test_the_clone_action_records_who_made_the_draft(
    version_admin, request_with_messages, risk_version
):
    version_admin.clone(request_with_messages, StateMachineVersion.objects.all())

    draft = StateMachineVersion.objects.get(version="2")
    assert draft.author is not None
    assert draft.author.user_id == request_with_messages.user.pk


# --------------------------------------------------------------------- validate


def test_the_validate_action_reports_a_clean_version(
    version_admin, request_with_messages, risk_version
):
    version_admin.validate(request_with_messages, StateMachineVersion.objects.all())
    assert messages_of(request_with_messages, constants.SUCCESS) == ["risk.status@1: valid."]


def test_the_validate_action_reports_errors_without_changing_anything(
    version_admin, request_with_messages, risk_draft
):
    risk_draft.states.update(is_initial=False)
    version_admin.validate(request_with_messages, StateMachineVersion.objects.all())

    risk_draft.refresh_from_db()
    assert risk_draft.lifecycle == Lifecycle.DRAFT
    assert messages_of(request_with_messages, constants.ERROR)


# ------------------------------------------------------------------ immutability


def test_a_published_version_is_read_only_in_the_admin(version_admin, risk_version):
    request = RequestFactory().get("/admin/")
    readonly = version_admin.get_readonly_fields(request, risk_version)
    assert "version" in readonly and "lifecycle" in readonly


def test_a_draft_is_still_editable(version_admin, risk_draft):
    request = RequestFactory().get("/admin/")
    assert version_admin.get_readonly_fields(request, risk_draft) == ("published_at",)


def test_history_rows_cannot_be_added_or_changed_through_the_admin(site):
    from vinta_state_machines.models import StatusTransition

    history_admin = StatusTransitionAdmin(StatusTransition, site)
    request = RequestFactory().get("/admin/")
    assert history_admin.has_add_permission(request) is False
    assert history_admin.has_change_permission(request) is False


# ----------------------------------------------------------------------- inlines


def test_the_transition_inline_only_offers_states_of_the_version_being_edited(
    site, risk_version, user
):
    other = clone_version(risk_version, "2")
    inline = TransitionInline(StateMachineVersion, site)

    request = RequestFactory().get(f"/admin/state_machines/statemachineversion/{other.pk}/change/")
    request.user = user
    request.resolver_match = type("Match", (), {"kwargs": {"object_id": str(other.pk)}})()

    for field_name in ("from_state", "to_state"):
        formfield = inline.formfield_for_foreignkey(
            inline.model._meta.get_field(field_name), request
        )
        assert set(formfield.queryset) == set(other.states.all())
        assert not set(formfield.queryset) & set(risk_version.states.all())


def test_without_a_version_in_the_url_no_states_are_offered(site, risk_version, user):
    inline = TransitionInline(StateMachineVersion, site)
    request = RequestFactory().get("/admin/state_machines/statemachineversion/add/")
    request.user = user
    request.resolver_match = None

    formfield = inline.formfield_for_foreignkey(inline.model._meta.get_field("to_state"), request)
    assert list(formfield.queryset) == []


# ------------------------------------------------------------------- annotations


def test_the_version_list_annotates_state_and_transition_counts(version_admin, risk_version):
    request = RequestFactory().get("/admin/")
    row = version_admin.get_queryset(request).get(pk=risk_version.pk)
    assert version_admin.state_count(row) == 4
    assert version_admin.transition_count(row) == 5
