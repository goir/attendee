"""Editable admin overrides for User and Organization (fork-specific).

By default admin_extras registers *read-only* admins, and upstream
``accounts/admin.py`` ships deliberately read-only admins for ``User`` and
``Organization`` (every field in ``readonly_fields``, add/change/delete disabled).

Our fork wants to actually administer these from Django admin — edit user
permissions (``is_staff`` / ``is_superuser`` / ``groups`` / ``user_permissions``)
and every other property, and edit all Organization settings. This module
re-registers both models as fully editable, exposing **all** model fields,
without touching upstream ``accounts/admin.py`` (so it stays rebaseable).

Wired from :mod:`admin_extras.admin` after autodiscovery (accounts precedes
admin_extras in INSTALLED_APPS, and we force-import ``accounts.admin`` here, so
the host admins always exist before we override them).
"""

import logging

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from accounts.admin import OrganizationAdmin as HostOrganizationAdmin
from accounts.models import Organization, User

logger = logging.getLogger(__name__)

# Never expose these as editable widgets, even though they are concrete fields.
_ALWAYS_READONLY = ("created_at", "updated_at", "version")


class EditableUserAdmin(DjangoUserAdmin):
    """Full Django-auth user admin (permissions + password) plus our custom fields.

    Re-enables add/change/delete (the host admin disabled them) and lists every
    field on the custom ``accounts.User`` model, not just a curated subset.
    """

    list_display = ("email", "username", "first_name", "last_name", "organization", "role", "is_staff", "is_superuser", "is_active")
    list_filter = ("is_staff", "is_superuser", "is_active", "role", "organization", "groups")
    search_fields = ("email", "username", "first_name", "last_name", "object_id")
    ordering = ("email",)
    filter_horizontal = ("groups", "user_permissions")
    # FK selects that could be large (every org / every user) -> id widgets.
    raw_id_fields = ("organization", "invited_by")
    # object_id is editable=False; last_login/date_joined are managed by auth.
    readonly_fields = ("object_id", "last_login", "date_joined")

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email")}),
        ("Organization", {"fields": ("organization", "invited_by", "role", "object_id")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    # Keep the add form to the standard UserCreationForm shape (+ email). A new
    # user's organization is auto-created by accounts.models.create_default_organization;
    # set organization / role / permissions on the change form after creation.
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("username", "email", "password1", "password2")}),)


class EditableOrganizationAdmin(HostOrganizationAdmin):
    """Editable Organization admin that exposes every model field.

    Subclasses the host admin so the "Add Credit Transaction" button + view are
    preserved, but flips the read-only gates and replaces the curated fieldsets
    with a dynamic, all-fields list.
    """

    fieldsets = None  # use get_fields() instead, so new model fields appear automatically
    readonly_fields = (*_ALWAYS_READONLY, "add_credit_transaction_button")

    def get_fields(self, request, obj=None):
        editable = [f.name for f in self.model._meta.fields if f.editable and not f.primary_key and f.name not in _ALWAYS_READONLY]
        fields = editable + list(_ALWAYS_READONLY)
        if obj is not None:
            # The credit button reverses a URL with obj.pk, so only on the change view.
            fields.append("add_credit_transaction_button")
        return fields

    def has_add_permission(self, request):
        return True

    def has_change_permission(self, request, obj=None):
        return True

    def has_delete_permission(self, request, obj=None):
        return True


def register_editable_admins():
    """Re-register User and Organization as fully editable admins."""
    for model, admin_cls in ((User, EditableUserAdmin), (Organization, EditableOrganizationAdmin)):
        if admin.site.is_registered(model):
            admin.site.unregister(model)
        admin.site.register(model, admin_cls)
    logger.info("admin_extras: registered editable admins for User and Organization")
