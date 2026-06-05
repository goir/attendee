"""Generic, read-only admin auto-registration.

This module registers a fully read-only Django admin for every model in a set
of apps that does not already have an admin registered. Models that the host
project (or any third-party app) already registers are left untouched, so this
never collides with hand-written admins.

It is intentionally self-contained and does not import anything from the host
project, so it can later be extracted into a standalone, pip-installable
package without changes.
"""

import logging

from django.contrib import admin
from django.db import models

logger = logging.getLogger(__name__)

# Substrings (case-insensitive) that mark a field as sensitive. Sensitive field
# values are masked in the detail view instead of being shown verbatim.
SENSITIVE_FIELD_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "private_key",
    "signature",
    "encrypted",
    "api_key",
    "apikey",
    "key",
)

# Field names that look like a key but are safe identifiers, so they must NOT be
# masked even though they contain a sensitive-looking token.
SENSITIVE_FIELD_ALLOWLIST = (
    "deduplication_key",
    "idempotency_key",
    "object_id",
)

MASK_PLACEHOLDER = "•••••• (hidden)"

# Curated, low-cardinality field names worth exposing as list filters / columns
# when a model happens to have them.
_FILTER_CANDIDATES = ("state", "platform", "status", "source", "is_active", "is_deleted", "is_enabled")
_LIST_DISPLAY_CANDIDATES = ("object_id", "name", "state", "platform", "status", "is_active")
_SEARCH_CANDIDATES = ("object_id", "name")
_TIMESTAMP_FIELDS = ("created_at", "updated_at")


def _is_sensitive(field):
    """Return True if a field's value should be masked in the admin."""
    name = field.name.lower()
    if name in SENSITIVE_FIELD_ALLOWLIST:
        return False
    if isinstance(field, models.BinaryField):
        return True
    return any(token in name for token in SENSITIVE_FIELD_TOKENS)


def _build_masked_accessor(field_name):
    """Build an admin display callable that hides a field's real value."""

    def accessor(self, obj):
        value = getattr(obj, field_name, None)
        if value in (None, ""):
            return "-"
        return MASK_PLACEHOLDER

    accessor.short_description = field_name.replace("_", " ").title()
    accessor.__name__ = f"masked_{field_name}"
    return accessor


def _build_list_display(field_names):
    display = [name for name in _LIST_DISPLAY_CANDIDATES if name in field_names]
    display += [name for name in _TIMESTAMP_FIELDS if name in field_names]
    # Always include the primary key first so every row is identifiable.
    pk_name = field_names.get("__pk__")
    ordered = [pk_name] + [name for name in display if name != pk_name]
    return tuple(ordered[:6]) or (pk_name,)


def make_readonly_admin(model):
    """Construct a fully read-only ModelAdmin subclass for ``model``.

    Every concrete local field is rendered read-only; sensitive field values are
    masked. Add / change / delete are all disabled.
    """
    local_fields = list(model._meta.fields)  # concrete fields incl. FKs, excl. M2M/reverse
    field_names = {f.name: f for f in local_fields}
    field_names["__pk__"] = model._meta.pk.name

    attrs = {}
    ordered_fields = []
    for field in local_fields:
        if _is_sensitive(field):
            accessor_name = f"masked_{field.name}"
            attrs[accessor_name] = _build_masked_accessor(field.name)
            ordered_fields.append(accessor_name)
        else:
            ordered_fields.append(field.name)

    # Use the same ordered list for `fields` and `readonly_fields`. This keeps
    # every value read-only AND guarantees the raw sensitive field is never
    # rendered as an editable widget (only the masked accessor appears).
    attrs["fields"] = tuple(ordered_fields)
    attrs["readonly_fields"] = tuple(ordered_fields)
    attrs["list_display"] = _build_list_display(field_names)

    search_fields = tuple(name for name in _SEARCH_CANDIDATES if isinstance(field_names.get(name), (models.CharField, models.TextField)))
    if search_fields:
        attrs["search_fields"] = search_fields

    list_filter = tuple(name for name in _FILTER_CANDIDATES if name in field_names and (isinstance(field_names[name], models.BooleanField) or getattr(field_names[name], "choices", None)))
    if list_filter:
        attrs["list_filter"] = list_filter

    if isinstance(field_names.get("created_at"), models.DateTimeField):
        attrs["date_hierarchy"] = "created_at"

    return type(f"{model.__name__}ReadOnlyAdmin", (ReadOnlyModelAdmin,), attrs)


class ReadOnlyModelAdmin(admin.ModelAdmin):
    """Base class for generated admins: view-only, no add/change/delete."""

    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def register_readonly_admins(app_labels):
    """Register a read-only admin for every not-yet-registered model.

    Models already registered by the host project (or third-party apps) are
    skipped, so hand-written admins always win. Any single model that fails to
    register is logged and skipped rather than breaking the whole admin site.
    """
    from django.apps import apps as django_apps

    registered = 0
    for app_label in app_labels:
        try:
            app_config = django_apps.get_app_config(app_label)
        except LookupError:
            logger.warning("admin_extras: app %r is not installed; skipping", app_label)
            continue

        for model in app_config.get_models():
            if admin.site.is_registered(model):
                continue
            try:
                admin.site.register(model, make_readonly_admin(model))
                registered += 1
            except admin.sites.AlreadyRegistered:
                continue
            except Exception:  # never let one bad model break the admin
                logger.exception("admin_extras: failed to register %s", model._meta.label)

    logger.info("admin_extras: registered %d read-only admin(s) for apps %s", registered, list(app_labels))
    return registered
