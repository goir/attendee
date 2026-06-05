# admin_extras

A tiny, **self-contained** Django app that gives **every model an admin entry** — read-only —
without editing the host project's hand-written admins.

It exists so our [fork](../FORK_CHANGES.md) can surface all models in the Django admin while
staying trivially rebaseable against upstream attendee.

## What it does

At admin autodiscovery time (startup), it enumerates the models of the configured apps and
registers a generic **read-only** `ModelAdmin` for any model **not already registered**.

- **Non-collising:** models that already have a hand-written admin are skipped
  (`admin.site.is_registered(model)`), so upstream's admins always win.
- **Future-proof:** a new upstream model automatically gets a read-only admin; a new upstream
  *admin* for a model makes ours automatically step aside. No edits required either way.
- **Read-only:** add / change / delete are all disabled; every field renders read-only.
- **Secret-aware:** field values are masked when the field name contains a sensitive token
  (`password`, `passwd`, `secret`, `token`, `credential`, `private_key`, `signature`,
  `encrypted`, `api_key`, `apikey`, `key`) or the field is a `BinaryField`. Masked values show
  `•••••• (hidden)` and the raw value is never rendered as a widget. Safe identifiers are
  allowlisted (`object_id`, `deduplication_key`, `idempotency_key`).

## Files

| File | Purpose |
|------|---------|
| `apps.py` | `AppConfig` (`AdminExtrasConfig`). |
| `admin.py` | Autodiscovered entry point. Defines `PROJECT_APP_LABELS` and calls `register_readonly_admins(...)`. |
| `registration.py` | Project-agnostic core: `ReadOnlyModelAdmin`, `make_readonly_admin(model)`, `register_readonly_admins(app_labels)`, masking helpers. |
| `tests.py` | Asserts all project models are registered, generated admins are read-only, and secrets are masked. |

## Install / integrate

Two steps (already done in this repo):

1. Add the app to `INSTALLED_APPS` (the **only** host-project edit):
   ```python
   INSTALLED_APPS = [
       # ...
       "admin_extras",
   ]
   ```
2. Choose which apps to cover, in `admin_extras/admin.py`:
   ```python
   PROJECT_APP_LABELS = ("accounts", "bots")
   ```

No models, no migrations.

## Configuration

- **Which apps:** edit `PROJECT_APP_LABELS` in `admin.py`.
- **Sensitive-field detection:** `SENSITIVE_FIELD_TOKENS` / `SENSITIVE_FIELD_ALLOWLIST` in `registration.py`.
- **List columns / filters / search:** `_LIST_DISPLAY_CANDIDATES`, `_FILTER_CANDIDATES`,
  `_SEARCH_CANDIDATES`, `_TIMESTAMP_FIELDS` in `registration.py` (only applied when a model
  actually has those fields).

## Verify

Runs without a database (admin autodiscovery + Django's admin system checks validate every
generated `ModelAdmin`):

```bash
python manage.py check
# -> "admin_extras: registered N read-only admin(s) ..."
# -> "System check identified no issues"
```

Full behavioral tests (need a DB):

```bash
python manage.py test admin_extras
```

## Extracting into a standalone package later

`registration.py` imports **nothing** from the host project, so the path to a reusable
`pip`-installable package is short:

1. Move `admin_extras/` into its own repo / distribution (e.g. `django-readonly-admin-extras`).
2. The only project-specific code is `PROJECT_APP_LABELS` in `admin.py`. Make it a setting:
   ```python
   # admin.py in the packaged version
   from django.conf import settings
   from .registration import register_readonly_admins

   register_readonly_admins(getattr(settings, "ADMIN_EXTRAS_APP_LABELS", ()))
   ```
3. Consumers then just add the app to `INSTALLED_APPS` and set `ADMIN_EXTRAS_APP_LABELS`.

Until then it lives inline; see [`../FORK_CHANGES.md`](../FORK_CHANGES.md) for fork tracking.
