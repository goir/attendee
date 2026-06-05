"""Project date/time formats that always include SECONDS.

Activated via ``FORMAT_MODULE_PATH`` in settings. Django selects this "en" module
for the "en-us" locale (it falls back from ``en_US`` to ``en``), so admin list and
detail views render datetimes as ``YYYY-MM-DD HH:MM:SS`` instead of the default
locale format that drops seconds — which matters for reading runtimes.

Only the datetime/time formats are overridden; date-only and input formats keep
Django's defaults. Note: this is project-wide localization, but Attendee's only
HTML surface is the admin (the API uses DRF's own ISO-8601 formatting, unaffected).
"""

# https://docs.djangoproject.com/en/5.1/ref/templates/builtins/#date
DATETIME_FORMAT = "Y-m-d H:i:s"
SHORT_DATETIME_FORMAT = "Y-m-d H:i:s"
TIME_FORMAT = "H:i:s"
