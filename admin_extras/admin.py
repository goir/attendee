"""Auto-register read-only admins for project models lacking one.

Django's admin autodiscovery imports this module at startup (after all apps are
loaded), which is exactly when we enumerate the project's models and register a
generic read-only admin for any that the host project hasn't registered itself.

To change which apps are covered, edit PROJECT_APP_LABELS. This is the only
project-specific configuration in the app.
"""

from .bot_admin import augment_bot_admin
from .editable_admins import register_editable_admins
from .registration import register_readonly_admins

# Only the project's own apps. Third-party apps keep their existing admins.
PROJECT_APP_LABELS = ("accounts", "bots")

register_readonly_admins(PROJECT_APP_LABELS)

# Augment the host project's hand-written BotAdmin with operational actions
# (event dispatch + async transcription). Done after register_readonly_admins so
# the host BotAdmin is already in place; this unregisters and re-registers it.
augment_bot_admin()

# Re-register User and Organization as fully EDITABLE admins (permissions + all
# fields), overriding upstream's read-only versions. Kept here, not in upstream
# accounts/admin.py, so the change survives rebases.
register_editable_admins()
