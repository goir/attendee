"""Auto-register read-only admins for project models lacking one.

Django's admin autodiscovery imports this module at startup (after all apps are
loaded), which is exactly when we enumerate the project's models and register a
generic read-only admin for any that the host project hasn't registered itself.

To change which apps are covered, edit PROJECT_APP_LABELS. This is the only
project-specific configuration in the app.
"""

from .registration import register_readonly_admins

# Only the project's own apps. Third-party apps keep their existing admins.
PROJECT_APP_LABELS = ("accounts", "bots")

register_readonly_admins(PROJECT_APP_LABELS)
