from django.apps import apps as django_apps
from django.contrib import admin
from django.test import TestCase

from .admin import PROJECT_APP_LABELS
from .registration import ReadOnlyModelAdmin, make_readonly_admin


class AutoRegistrationTests(TestCase):
    def test_every_project_model_is_registered(self):
        for app_label in PROJECT_APP_LABELS:
            for model in django_apps.get_app_config(app_label).get_models():
                self.assertTrue(
                    admin.site.is_registered(model),
                    f"{model._meta.label} has no admin registered",
                )

    def test_generated_admins_are_read_only(self):
        request = None
        for app_label in PROJECT_APP_LABELS:
            for model in django_apps.get_app_config(app_label).get_models():
                model_admin = admin.site._registry[model]
                if not isinstance(model_admin, ReadOnlyModelAdmin):
                    continue  # hand-written admin; not our concern
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))

    def test_sensitive_fields_are_masked_not_shown_raw(self):
        from bots.models import ApiKey

        admin_cls = make_readonly_admin(ApiKey)
        # The raw `key_hash` must never be rendered; only its masked accessor is.
        self.assertNotIn("key_hash", admin_cls.fields)
        self.assertIn("masked_key_hash", admin_cls.fields)
        # Non-secret identifiers stay visible and read-only.
        self.assertIn("object_id", admin_cls.fields)
        self.assertEqual(admin_cls.fields, admin_cls.readonly_fields)
