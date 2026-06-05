from django.apps import AppConfig


class AdminExtrasConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "admin_extras"
    verbose_name = "Admin Extras (read-only)"
