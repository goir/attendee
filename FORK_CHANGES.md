# Fork Changes

This is **our fork** of [attendee](https://github.com/attendee-labs/attendee). This file is the
single source of truth for **everything we changed on top of upstream**, so that:

- we can **rebase / merge upstream cleanly** without silently losing our work,
- we can **extract individual changes into upstream PRs** later,
- a new contributor can see at a glance what is "ours" vs "upstream".

> Keep this file updated whenever you add a fork-specific change. It is cheap insurance
> against losing track of divergence.

## Branch model

- `main` — clean mirror of upstream (`origin/main == main`). **Do not put fork work here.**
- `implicible` — our working branch; carries all fork-specific commits on top of `main`.

## How to regenerate the list of fork commits

```bash
# every commit we carry on top of upstream:
git log --oneline main..implicible

# full diff of our divergence:
git diff main...implicible

# files we touched that also exist upstream (rebase-conflict candidates):
git diff --name-only main...implicible
```

If `main` drifts from real upstream, refresh it first:

```bash
git remote add upstream https://github.com/attendee-labs/attendee.git   # one-time
git fetch upstream
git checkout main && git merge --ff-only upstream/main
```

---

## Changes (newest first)

| # | Commit | Summary | Touches upstream files? | PR-ready? |
|---|--------|---------|--------------------------|-----------|
| 5 | _uncommitted_ | **Editable** `User` & `Organization` admins (permissions + every model field), overriding upstream's read-only ones; read-only auto-admins now also list M2M fields | No (overrides host admins at runtime, in `admin_extras`) | Fork-local (operational tooling) |
| 4 | _uncommitted_ | Bot admin **operational actions** (recover stuck POST_PROCESSING, dispatch any bot event, start async transcription) added via `admin_extras` | No (augments the host BotAdmin at runtime) | Fork-local (operational tooling) |
| 3 | _uncommitted_ | `transcription_extras` app: per-speaker, size-capped chunk combining for custom-async transcription | 2 tiny edits (`bots/models.py`, one async task) + `INSTALLED_APPS` line | Fork-local (depends on our WhisperX service) |
| 2 | `35954a4d` (+ follow-ups) | `admin_extras` app: auto read-only admin for every model; admin datetimes with seconds | 2 lines in `attendee/settings/base.py` (`INSTALLED_APPS`, `FORMAT_MODULE_PATH`) | Mergeable as-is, but niche; likely keep fork-local |
| 1 | `d76d92bb` | Record-only mode (`disable_realtime_transcription`) | Yes (`bots/*`) | Yes — good upstream candidate |

---

### 5. Editable User & Organization admin (+ all model fields)

- **Why:** Upstream's `accounts/admin.py` makes `User` and `Organization` **read-only** (every field
  in `readonly_fields`, add/change/delete disabled). We need to actually administer them — edit user
  **permissions** (`is_staff` / `is_superuser` / `groups` / `user_permissions`) and all other
  properties, and edit all Organization settings.
- **What:** `admin_extras` re-registers both models as **fully editable**, exposing **every** model
  field — without editing upstream `accounts/admin.py` (stays rebaseable):
  - `EditableUserAdmin` subclasses Django's auth `UserAdmin` (proper password + permission widgets,
    `filter_horizontal` groups/permissions), with fieldsets extended to the custom fields
    (`organization`, `invited_by`, `role`, `object_id`) and add/change/delete re-enabled.
  - `EditableOrganizationAdmin` subclasses the host `OrganizationAdmin` (so the "Add Credit
    Transaction" button/view is preserved) but flips the read-only gates and builds its field list
    dynamically from `_meta.fields`, so all current **and future** Organization fields show up.
  - Generic read-only auto-admins now also list **many-to-many** fields (read-only), so every model
    surfaces all of its fields.
- **Files (all in the `admin_extras` app — zero upstream edits):**
  - `admin_extras/editable_admins.py` — the two editable admins + `register_editable_admins()`.
  - `admin_extras/admin.py` — one call to `register_editable_admins()` after the read-only pass.
  - `admin_extras/registration.py` — `make_readonly_admin` appends `_meta.many_to_many`.
  - `admin_extras/tests.py` — assert both admins are editable and expose all fields.
- **Caveat:** editing `Organization.centicredits` directly bypasses the `CreditTransaction` audit
  trail — use the preserved "Add Credit Transaction" button when you want a recorded transaction.
- **Verify:** `python manage.py check`; `python manage.py test admin_extras`.

---

### 4. Bot admin operational actions (event dispatch + async transcription)

- **Why:** When a recorder pod crashes mid post-processing, the bot is left stuck in
  `POST_PROCESSING` and there is no UI to move it on. The async-transcription API also refuses
  unless the bot is `ENDED` (`Cannot create async transcription because bot is not in state ended`).
  We need a hands-on way to drive the state machine and (re)start transcription from the admin.
- **What:** Three Django **admin actions** on the Bot changelist:
  - **Recover stuck POST_PROCESSING** — one click sends `POST_PROCESSING_COMPLETED`
    (`POST_PROCESSING → ENDED`) via `BotEventManager.create_event`.
  - **Dispatch bot event…** — intermediate form to send *any* `BotEventTypes` event (optional
    sub-type + JSON metadata). The real state-machine validation applies; invalid transitions are
    reported per-bot, never silently applied.
  - **Start async transcription…** — intermediate form taking provider `transcription_settings`
    (same schema as `POST /transcript`). Mirrors the API's guards (org-enabled, audio chunks
    present, `ENDED`, max-per-recording) then `process_async_transcription.delay(...)`.
- **Files (all new, in the `admin_extras` app — zero upstream edits):**
  - `admin_extras/bot_admin.py` — forms, `BotActionsMixin`, and `augment_bot_admin()`, which
    unregisters the host `BotAdmin` and re-registers a subclass mixing the actions in. Keeps
    upstream `bots/admin.py` untouched and rebaseable.
  - `admin_extras/templates/admin_extras/action_form.html` — reusable intermediate-page template.
  - `admin_extras/admin.py` — one call to `augment_bot_admin()` after `register_readonly_admins`.
  - `admin_extras/tests.py` — augmentation + transition (valid/invalid) + intermediate-page tests.
- **Note:** the host `BotAdmin` forces `has_change_permission=False`, so these actions are **not**
  gated on the change permission (that would disable them outright); they're available to anyone
  who can view the Bot changelist (internal/superuser admin).
- **Rebase notes:** Nothing touches upstream. If upstream later renames `BotEventManager`/event
  enums, only `admin_extras/bot_admin.py` needs updating. New files → no merge conflicts.
- **Verify:** `python manage.py check`; `python manage.py test admin_extras`.

---

### 3. `transcription_extras` — per-speaker combined custom-async transcription

- **Why:** Custom-async transcription sent **one HTTP request per audio chunk**, causing a
  Cloud Run GPU cold-start per chunk. We combine each speaker's chunks into larger MP3 files
  (capped at a configurable upload size) so the bot makes far fewer, larger requests.
- **What:** A new self-contained app `transcription_extras/` that owns all the new logic:
  group chunks **per speaker**, split each speaker into **size-capped** sub-groups, combine each
  into one MP3, send to our WhisperX `/attendee/transcribe`, and split the result back onto the
  individual utterances. It **reuses** Attendee's existing, provider-agnostic helpers
  (`get_mp3_for_utterance_group`, `split_transcription_by_utterance`) rather than reimplementing
  them. Verbose `transcription_extras:`-prefixed logging throughout for debugging.
- **Correctness invariant:** one combined MP3 ↔ one ordered utterance sub-group ↔ one
  `split_transcription_by_utterance()` call. Each file's word times start at 0, so files are
  never mixed into a shared/global timeline. Word times stay utterance-relative (0-based);
  meeting position comes from `Utterance.timestamp_ms`, unchanged.
- **Files:**
  - `transcription_extras/` — new app (`config.py`, `grouping.py`, `whisperx_group_client.py`,
    `group_transcription.py`, `transcript_export.py`, `admin.py`, `apps.py`, `tests.py`, `README.md`).
    **New files → zero rebase risk.**
  - `admin.py` also adds a custom **AsyncTranscription admin** (transcript preview + `.txt`/`.json`
    download), replacing admin_extras' generic read-only admin for that model — so `bots/admin.py`
    stays untouched. Records stay non-editable (no add/change) but **may be deleted** (single +
    bulk `delete_selected`); deletion cascades to the transcription's utterances. `transcript_export.py` renders each utterance's `timestamp_ms` (a Unix
    **epoch-millisecond** value, not a meeting offset) as a localized `YYYY-MM-DD HH:MM:SS` datetime
    with seconds — fixing an earlier preview bug that showed overflowed `HH:MM:SS` (e.g. `494628:17:30`).
  - `attendee/settings/base.py` — appended `"transcription_extras"` to `INSTALLED_APPS`.
  - `bots/models.py` — `AsyncTranscription.use_grouped_utterances` now also `True` for
    `CUSTOM_ASYNC` / `CUSTOM_ASYNC_V2` (was AssemblyAI-only). **~4-line edit.**
  - `bots/tasks/process_utterance_group_for_async_transcription_task.py` — `get_transcription()`
    delegates custom providers to `transcription_extras.get_transcription_for_utterance_group`
    via a local import. **~5-line additive edit.**
- **Config (env, all optional):** `CUSTOM_ASYNC_TRANSCRIPTION_MAX_UPLOAD_BYTES` (default ~30 MB),
  `CUSTOM_ASYNC_TRANSCRIPTION_MP3_BITRATE_KBPS` (128), `CUSTOM_ASYNC_TRANSCRIPTION_COMBINE_SILENCE_SECONDS`
  (1.5), `CUSTOM_ASYNC_TRANSCRIPTION_UPLOAD_SAFETY_MARGIN` (0.9), `CUSTOM_ASYNC_TRANSCRIPTION_TIMEOUT` (120).
  Reuses the existing `CUSTOM_ASYNC_TRANSCRIPTION_URL`.
- **Rebase notes:** Two small upstream hunks are conflict candidates; both are tiny and additive.
  The single-chunk custom path in `bots/tasks/process_utterance_task.py` is left fully intact
  (it's just no longer used for async once grouping is on). No DB migration (settings are JSON).
- **Deep docs:** see [`transcription_extras/README.md`](transcription_extras/README.md).
- **Verify:** `python manage.py check`; unit tests `python manage.py test transcription_extras`
  (DB-free `SimpleTestCase`s — grouping, parsing, per-file split).

---

### 2. `admin_extras` — auto read-only admin for all models

- **Commit:** `35954a4d feat: introduce admin_extras for read-only model registration`
- **Why:** Upstream only ships hand-written admins for ~9 models. We want a Django admin
  entry for **every** model (read-only is fine) without editing upstream's `admin.py` files,
  so the change survives rebases.
- **What:** A new self-contained app `admin_extras/` that, at admin-autodiscovery time, loops
  over the project's apps and registers a generic **read-only** `ModelAdmin` for any model that
  isn't already registered. Already-registered models (upstream's hand-written admins) are
  skipped, so there is never a collision and upstream's admins always win.
  Sensitive fields (name contains `key`/`token`/`secret`/`password`/`credential`/… or any
  `BinaryField`) are **masked** in the detail view.
- **Files:**
  - `admin_extras/` — new app (`__init__.py`, `apps.py`, `admin.py`, `registration.py`, `tests.py`,
    plus `formats/en/formats.py` for the seconds-everywhere datetime format). **New files → zero rebase risk.**
  - `attendee/settings/base.py` — two appended lines: `"admin_extras"` in `INSTALLED_APPS`, and
    `FORMAT_MODULE_PATH = ["admin_extras.formats"]` (forces admin datetimes to show seconds — the
    default `en-us` locale drops them, and Django 5 always localizes so this is the supported override).
- **Config knob:** `PROJECT_APP_LABELS` in `admin_extras/admin.py` (currently `("accounts", "bots")`).
- **Rebase notes:** The only possible conflict is the `INSTALLED_APPS` tail. If upstream also
  appends an app there, resolve by keeping both lines. Nothing else can conflict.
- **Deep docs / extraction guide:** see [`admin_extras/README.md`](admin_extras/README.md).
- **Upstream PR plan:** This is generic and reusable, but opinionated (read-only everything,
  masking heuristics). If we ever upstream it, it would go as the whole `admin_extras/` app plus
  the one `INSTALLED_APPS` line. More likely we keep it fork-local or publish it as a standalone
  pip package (`django-admin-extras`-style) — the app is written to be lifted out unchanged.

### 1. Record-only mode (`disable_realtime_transcription`)

- **Commit:** `d76d92bb feat: add record-only mode (disable_realtime_transcription)`
- **Why:** Capture per-participant audio during a meeting but **skip realtime transcription**,
  transcribing post-meeting via the async transcription job instead. Avoids per-chunk
  cold-start transcription failures during the meeting and lets the bot reach `ENDED` quickly
  (no in-flight utterances to drain), making the async job the single reliable transcript source.
- **What:** New recording setting `disable_realtime_transcription` (boolean, default `false`).
  When set, `save_utterances_for_individual_audio_chunks()` and `save_utterances_for_closed_captions()`
  return `False` (no realtime utterances enqueued), while `should_capture_audio_chunks()` stays
  true via `record_async_transcription_audio_chunks`. **Use together with
  `record_async_transcription_audio_chunks=true`**, otherwise no audio is retained to transcribe later.
- **Files (all upstream — rebase-conflict candidates):**
  - `bots/models.py` — `Bot.disable_realtime_transcription()` reader (near the other
    `recording_settings` accessors, ~line 1094).
  - `bots/bot_controller/bot_controller.py` — early-return guards in
    `save_utterances_for_individual_audio_chunks()` / `save_utterances_for_closed_captions()`.
  - `bots/serializers.py` — adds the key to `BOT_RECORDING_SETTINGS_DEFAULT_VALUES` and
    `BOT_RECORDING_SETTINGS_SCHEMA`.
- **Rebase notes:** All three files are upstream and actively developed. Watch for conflicts in
  the recording-settings schema/defaults and the bot controller's utterance-gating methods.
- **Upstream PR plan:** Good standalone PR — additive, behind a default-`false` flag, no schema
  migration (it's a JSON setting). Could be proposed to upstream as-is.

---

## Repo-infra tweaks (not features)

- `.gitignore` — added `/.omc` (ignores the local oh-my-claudecode session/state directory).
  Committed in `35954a4d`. Harmless; drop it if upstreaming anything that touches `.gitignore`.
