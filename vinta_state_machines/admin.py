"""Admin for authoring the catalog and the versioned graphs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.db.models import Count, QuerySet
from django.http import Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _, ngettext

from vinta_state_machines.editor import (
    EditorPayloadError,
    action_catalog,
    apply_editor_machine,
    check_guard,
    side_effect_definitions,
    to_editor_machine,
)
from vinta_state_machines.enums import Lifecycle
from vinta_state_machines.exceptions import StateMachineError
from vinta_state_machines.models import (
    ActionType,
    StateMachine,
    StateMachineHook,
    StateMachineIdentity,
    StateMachineScope,
    StateMachineState,
    StateMachineTransition,
    StateMachineVersion,
    StatusDefinition,
    StatusTransition,
)
from vinta_state_machines.services import publish_version, validate_version

if TYPE_CHECKING:
    from django.http import HttpRequest


@admin.register(StatusDefinition)
class StatusDefinitionAdmin(admin.ModelAdmin):
    list_display = ("key", "entity_type", "status_field", "name")
    list_filter = ("entity_type", "status_field")
    search_fields = ("key", "name", "description")
    ordering = ("entity_type", "status_field", "key")


@admin.register(ActionType)
class ActionTypeAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "domain")
    list_filter = ("domain",)
    search_fields = ("key", "name", "description")


class StateInline(admin.TabularInline):
    model = StateMachineState
    extra = 0
    autocomplete_fields = ("status",)
    fields = ("status", "is_initial", "is_terminal", "color", "order", "x", "y")


class TransitionInline(admin.TabularInline):
    model = StateMachineTransition
    extra = 0
    autocomplete_fields = ("action_type",)
    fields = (
        "name",
        "from_state",
        "action_type",
        "to_state",
        "guard",
        "required_permission",
        "requires_approval",
        "order",
    )

    def formfield_for_foreignkey(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        """Only offer states that belong to the version being edited."""
        if db_field.name in ("from_state", "to_state"):
            version_id = (
                request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
            )
            queryset = StateMachineState.objects.select_related("status")
            kwargs["queryset"] = (
                queryset.filter(state_machine_version_id=version_id)
                if version_id
                else queryset.none()
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class HookInline(admin.TabularInline):
    model = StateMachineHook
    extra = 0
    fields = (
        "handler_key",
        "timing",
        "event",
        "transition",
        "state",
        "order",
        "on_commit",
        "is_active",
    )


@admin.register(StateMachineVersion)
class StateMachineVersionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "lifecycle", "state_count", "transition_count", "published_at")
    list_filter = ("lifecycle", "state_machine")
    search_fields = ("version", "state_machine__key", "notes")
    autocomplete_fields = ("state_machine",)
    inlines = (StateInline, TransitionInline, HookInline)
    actions = ("publish", "validate")
    readonly_fields = ("published_at",)
    change_form_template = "admin/state_machines/statemachineversion/change_form.html"

    def get_queryset(self, request: HttpRequest) -> QuerySet[StateMachineVersion]:
        queryset: QuerySet[StateMachineVersion] = (
            super()
            .get_queryset(request)
            .select_related("state_machine")
            .annotate(
                _states=Count("states", distinct=True),
                _transitions=Count("transitions", distinct=True),
            )
        )
        return queryset

    @admin.display(description=_("states"), ordering="_states")
    def state_count(self, obj: StateMachineVersion) -> int:
        return getattr(obj, "_states", 0)

    @admin.display(description=_("transitions"), ordering="_transitions")
    def transition_count(self, obj: StateMachineVersion) -> int:
        return getattr(obj, "_transitions", 0)

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        """A published version is immutable; only a draft can still be edited."""
        if obj is not None and obj.lifecycle != Lifecycle.DRAFT:
            return tuple(field.name for field in self.model._meta.fields)
        return self.readonly_fields

    # -------------------------------------------------------------------- canvas

    def get_urls(self) -> list[Any]:
        """The endpoints the canvas talks to, under this version's own URL."""
        prefix = f"{self.opts.app_label}_{self.opts.model_name}_editor"
        mine = [
            path(
                "<path:object_id>/editor/machine/",
                self.admin_site.admin_view(self.editor_machine_view),
                name=f"{prefix}_machine",
            ),
            path(
                "<path:object_id>/editor/side-effects/",
                self.admin_site.admin_view(self.editor_side_effects_view),
                name=f"{prefix}_side_effects",
            ),
            path(
                "<path:object_id>/editor/actions/",
                self.admin_site.admin_view(self.editor_actions_view),
                name=f"{prefix}_actions",
            ),
            path(
                "<path:object_id>/editor/guard/",
                self.admin_site.admin_view(self.editor_guard_view),
                name=f"{prefix}_guard",
            ),
        ]
        return mine + super().get_urls()

    def _editor_object(self, request: HttpRequest, object_id: str) -> StateMachineVersion:
        version: StateMachineVersion | None = self.get_object(request, unquote(object_id))
        if version is None or not self.has_view_permission(request, version):
            raise Http404
        return version

    def editor_machine_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """GET the version as a canvas document, POST one back to reconcile it."""
        version = self._editor_object(request, object_id)
        if request.method == "GET":
            return JsonResponse(to_editor_machine(version))
        if request.method != "POST":
            return HttpResponseNotAllowed(["GET", "POST"])
        if not self.has_change_permission(request, version):
            raise Http404
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError as exc:
            return JsonResponse({"errors": [f"Malformed JSON: {exc}"]}, status=400)
        try:
            apply_editor_machine(version, payload)
        except EditorPayloadError as exc:
            return JsonResponse({"errors": exc.errors}, status=400)
        # Re-serialized rather than echoed back: the server assigns the real ids, and
        # a state drawn on the canvas only learns its vocabulary key here.
        return JsonResponse(to_editor_machine(version))

    def editor_side_effects_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        self._editor_object(request, object_id)
        return JsonResponse(side_effect_definitions(), safe=False)

    def editor_actions_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        self._editor_object(request, object_id)
        return JsonResponse(action_catalog(), safe=False)

    def editor_guard_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        self._editor_object(request, object_id)
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"ok": False, "errors": ["Malformed JSON."]})
        return JsonResponse(check_guard(str(payload.get("expression") or "")))

    def render_change_form(
        self,
        request: HttpRequest,
        context: dict[str, Any],
        add: bool = False,
        change: bool = False,
        form_url: str = "",
        obj: Any = None,
    ) -> HttpResponse:
        """Hand the template the canvas endpoints, but only for a version that exists."""
        version = obj
        if version is not None and version.pk:
            prefix = f"admin:{self.opts.app_label}_{self.opts.model_name}_editor"

            def endpoint(suffix: str) -> str:
                return reverse(
                    f"{prefix}_{suffix}",
                    args=[version.pk],
                    current_app=self.admin_site.name,
                )

            context["dsm_editor"] = {
                "machine_url": endpoint("machine"),
                "side_effects_url": endpoint("side_effects"),
                "actions_url": endpoint("actions"),
                "guard_url": endpoint("guard"),
                "read_only": not version.is_editable
                or not self.has_change_permission(request, version),
            }
        response: HttpResponse = super().render_change_form(
            request, context, add=add, change=change, form_url=form_url, obj=obj
        )
        return response

    @admin.action(description=_("Publish selected draft versions"))
    def publish(self, request: HttpRequest, queryset: QuerySet[StateMachineVersion]) -> None:
        published = 0
        for version in queryset.select_related("state_machine"):
            try:
                report = publish_version(version, author=request.user)
            except StateMachineError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
                continue
            published += 1
            for warning in report.warnings:
                self.message_user(request, f"{version}: {warning}", level=messages.WARNING)
        if published:
            self.message_user(
                request,
                ngettext("%d version published.", "%d versions published.", published) % published,
                level=messages.SUCCESS,
            )

    @admin.action(description=_("Validate selected versions"))
    def validate(self, request: HttpRequest, queryset: QuerySet[StateMachineVersion]) -> None:
        for version in queryset.select_related("state_machine"):
            report = validate_version(version)
            for error in report.errors:
                self.message_user(request, f"{version}: {error}", level=messages.ERROR)
            for warning in report.warnings:
                self.message_user(request, f"{version}: {warning}", level=messages.WARNING)
            if report.ok and not report.warnings:
                self.message_user(request, f"{version}: valid.", level=messages.SUCCESS)


class VersionInline(admin.TabularInline):
    model = StateMachineVersion
    extra = 0
    fields = ("version", "lifecycle", "published_at", "author")
    readonly_fields = ("published_at",)
    show_change_link = True
    fk_name = "state_machine"


# Only when the project has not swapped it out: the replacement is the project's own
# model, and registering it here would collide with its own admin.
if StateMachineScope._meta.swapped is None:

    @admin.register(StateMachineScope)
    class StateMachineScopeAdmin(admin.ModelAdmin):
        list_display = ("scope_key", "scope_type", "label", "machine_count")
        list_filter = ("scope_type",)
        search_fields = ("scope_key", "label")

        def get_queryset(self, request: Any) -> QuerySet[StateMachineScope]:
            queryset: QuerySet[StateMachineScope] = super().get_queryset(request)
            return queryset.annotate(_machines=Count("state_machines"))

        @admin.display(description=_("machines"), ordering="_machines")
        def machine_count(self, obj: StateMachineScope) -> int:
            return int(obj._machines)  # type: ignore[attr-defined]


if StateMachineIdentity._meta.swapped is None:

    @admin.register(StateMachineIdentity)
    class StateMachineIdentityAdmin(admin.ModelAdmin):
        list_display = ("identity_label", "identity_type", "identity_key", "created_at")
        list_filter = ("identity_type", "is_staff", "is_superuser")
        search_fields = ("identity_key", "identity_label")
        date_hierarchy = "created_at"
        list_select_related = ("user",)

        # Snapshots, not records of a principal: editing one would rewrite what somebody
        # was allowed to do at a moment that has already passed.
        def has_add_permission(self, request: HttpRequest) -> bool:
            return False

        def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
            return False


@admin.register(StateMachine)
class StateMachineAdmin(admin.ModelAdmin):
    list_display = ("key", "scope", "entity_type", "status_field", "name", "default_version")
    list_filter = ("entity_type", "scope")
    search_fields = ("key", "name", "description")
    autocomplete_fields = ("default_version",)
    inlines = (VersionInline,)


@admin.register(StatusTransition)
class StatusTransitionAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "target_type",
        "target_id",
        "status_field",
        "from_status",
        "to_status",
        "action_type",
        "transition",
        "actor",
    )
    list_filter = ("target_type", "status_field", "state_machine_version", "actor_type")
    search_fields = ("target_id", "comment", "actor_key", "scope_key")
    date_hierarchy = "created_at"
    list_select_related = (
        "from_status",
        "to_status",
        "action_type",
        "transition",
        "actor",
        "target_type",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
