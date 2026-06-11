from django.apps import apps as django_apps
from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from .admin import PROJECT_APP_LABELS
from .bot_admin import BotActionsMixin
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


class BotAdminActionsTests(TestCase):
    """The host BotAdmin is augmented with operational actions (admin_extras.bot_admin)."""

    def setUp(self):
        from bots.models import Bot, Organization, Project

        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.factory = RequestFactory()
        self.bot_admin = admin.site._registry[Bot]

    def _request(self, data=None):
        request = self.factory.post("/admin/bots/bot/", data or {})
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def _make_bot(self, **kwargs):
        from bots.models import Bot

        defaults = {"project": self.project, "meeting_url": "https://zoom.us/j/123456789", "name": "Test Bot", "settings": {}}
        defaults.update(kwargs)
        return Bot.objects.create(**defaults)

    def test_bot_admin_is_augmented_with_actions(self):
        self.assertIsInstance(self.bot_admin, BotActionsMixin)
        for name in ("recover_post_processing", "dispatch_bot_event", "start_async_transcription"):
            self.assertTrue(callable(getattr(self.bot_admin, name)), f"missing action {name}")

    def test_dispatch_bot_event_applies_valid_transition(self):
        from bots.models import BotEventTypes, BotStates

        bot = self._make_bot()  # defaults to READY
        request = self._request(
            {
                "apply": "Dispatch event",
                "event_type": str(BotEventTypes.JOIN_REQUESTED.value),
                "event_sub_type": "",
                "event_metadata": "",
            }
        )
        result = self.bot_admin.dispatch_bot_event(request, self.bot_admin.model.objects.filter(pk=bot.pk))

        self.assertIsNone(result)  # processed -> redirect back to changelist
        bot.refresh_from_db()
        self.assertEqual(bot.state, BotStates.JOINING)

    def test_invalid_transition_is_reported_and_state_unchanged(self):
        from bots.models import BotStates

        bot = self._make_bot()  # READY; POST_PROCESSING_COMPLETED is not valid from READY
        # Must not raise even though the transition is invalid.
        self.bot_admin.recover_post_processing(self._request(), self.bot_admin.model.objects.filter(pk=bot.pk))

        bot.refresh_from_db()
        self.assertEqual(bot.state, BotStates.READY)

    def test_dispatch_form_with_no_apply_renders_intermediate_page(self):
        bot = self._make_bot()
        response = self.bot_admin.dispatch_bot_event(self._request(), self.bot_admin.model.objects.filter(pk=bot.pk))
        # No "apply" in POST -> the action returns the intermediate confirmation page.
        self.assertEqual(response.template_name, "admin_extras/action_form.html")


class EditableAdminTests(TestCase):
    """User and Organization are overridden to be fully editable with all fields."""

    def setUp(self):
        from accounts.models import User

        self.factory = RequestFactory()
        self.superuser = User.objects.create_superuser(username="root", email="root@example.com", password="pw-12345!")

    def _req(self):
        request = self.factory.get("/admin/")
        request.user = self.superuser
        return request

    def test_user_admin_is_editable_with_permissions_and_all_fields(self):
        from accounts.models import User

        user_admin = admin.site._registry[User]
        self.assertTrue(user_admin.has_add_permission(self._req()))
        self.assertTrue(user_admin.has_change_permission(self._req()))
        self.assertTrue(user_admin.has_delete_permission(self._req()))

        # Flatten the change-view fieldsets (pass an obj so it's not the add form).
        flat = {field for _, opts in user_admin.get_fieldsets(self._req(), self.superuser) for field in opts["fields"]}
        for name in ("is_active", "is_staff", "is_superuser", "groups", "user_permissions", "organization", "invited_by", "role", "object_id", "email", "username", "password"):
            self.assertIn(name, flat, f"User admin is missing field {name}")
        # Permissions M2M must be editable, not read-only.
        self.assertNotIn("groups", user_admin.get_readonly_fields(self._req(), self.superuser))

    def test_organization_admin_is_editable_with_all_fields(self):
        from accounts.models import Organization

        org_admin = admin.site._registry[Organization]
        self.assertTrue(org_admin.has_change_permission(self._req()))

        org = Organization.objects.create(name="Acme")
        fields = org_admin.get_fields(self._req(), org)
        for name in ("name", "centicredits", "is_webhooks_enabled", "is_async_transcription_enabled", "is_app_sessions_enabled", "autopay_enabled", "autopay_stripe_customer_id"):
            self.assertIn(name, fields, f"Organization admin is missing field {name}")
        # Core fields are no longer read-only (host admin had them all read-only).
        readonly = org_admin.get_readonly_fields(self._req(), org)
        self.assertNotIn("centicredits", readonly)
        self.assertNotIn("name", readonly)
        # The credit-transaction convenience button is preserved.
        self.assertIn("add_credit_transaction_button", fields)
