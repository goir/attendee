"""Operational admin actions for the Bot model (fork-specific).

Upstream attendee registers its own ``BotAdmin`` (``bots/admin.py``), so the
generic read-only registration in :mod:`admin_extras.registration` leaves Bot
alone. This module *augments* that existing admin with the operational actions
our fork needs to recover bots by hand:

- **Recover stuck POST_PROCESSING** — one click sends ``POST_PROCESSING_COMPLETED``
  to move a bot from ``POST_PROCESSING`` → ``ENDED`` (e.g. after a recorder pod
  crashed mid post-processing and left the bot wedged).
- **Dispatch bot event…** — send any ``BotEventTypes`` event (with optional
  sub-type / JSON metadata) through ``BotEventManager.create_event``, which
  enforces the real state-machine transitions.
- **Start async transcription…** — create an ``AsyncTranscription`` with arbitrary
  provider settings and enqueue processing. Mirrors the validation of the
  ``POST /transcript`` API (``bots/bots_api_views.py``).

Wired from :mod:`admin_extras.admin` after autodiscovery by unregistering the host
``BotAdmin`` and re-registering a subclass that mixes these actions in, so upstream
``bots/admin.py`` stays untouched and rebaseable. See ``FORK_CHANGES.md``.

Unlike the rest of admin_extras this module is intentionally project-specific (it
imports ``bots`` models), so it is kept out of the reusable ``registration.py`` core.
"""

import logging
import os

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.template.response import TemplateResponse
from django.urls import reverse

from bots.models import (
    AsyncTranscription,
    Bot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    Recording,
)

logger = logging.getLogger(__name__)


def _event_type_choices():
    return [(t.value, f"{t.label} ({BotEventTypes.type_to_api_code(t.value)})") for t in BotEventTypes]


def _event_sub_type_choices():
    return [("", "— none —")] + [(t.value, t.label) for t in BotEventSubTypes]


class BotEventDispatchForm(forms.Form):
    event_type = forms.TypedChoiceField(
        label="Event type",
        choices=_event_type_choices,
        coerce=int,
        help_text="Dispatched via BotEventManager.create_event. Only transitions valid for each bot's current state will succeed; others are reported as errors.",
    )
    event_sub_type = forms.TypedChoiceField(
        label="Event sub-type",
        choices=_event_sub_type_choices,
        coerce=int,
        required=False,
        empty_value=None,
    )
    event_metadata = forms.JSONField(
        label="Event metadata",
        required=False,
        help_text='Optional JSON object, e.g. {"reason": "manual admin recovery"}.',
    )


class StartAsyncTranscriptionForm(forms.Form):
    transcription_settings = forms.JSONField(
        label="Transcription settings",
        initial={"custom_async_v2": {"language": "de"}},
        help_text='Provider settings — same schema as the POST /transcript API. '
        'Examples: {"custom_async_v2": {"language": "de"}}, '
        '{"openai": {"model": "whisper-1"}}, {"assembly_ai": {"language_code": "en"}}.',
    )


class BotActionsMixin:
    """Adds operational actions to whatever BotAdmin we mix this into."""

    actions = ["recover_post_processing", "dispatch_bot_event", "start_async_transcription"]

    # --- shared helpers -------------------------------------------------

    def _safe_create_event(self, request, bot, event_type, event_sub_type=None, event_metadata=None):
        try:
            BotEventManager.create_event(bot=bot, event_type=event_type, event_sub_type=event_sub_type, event_metadata=event_metadata or {})
            self.message_user(
                request,
                f"{bot.object_id}: dispatched {BotEventTypes.type_to_api_code(event_type)} → state is now {BotStates.state_to_api_code(bot.state)}.",
                level=messages.SUCCESS,
            )
        except Exception as e:  # ValidationError on invalid transition, etc.
            self.message_user(request, f"{bot.object_id}: {e}", level=messages.ERROR)

    def _render_action_form(self, request, queryset, form, *, action_name, title, description, submit_label):
        opts = self.model._meta
        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "description": description,
            "queryset": queryset,
            "bot_summary": ", ".join(b.object_id for b in queryset) or "(none)",
            "form": form,
            "action_name": action_name,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "submit_label": submit_label,
            "opts": opts,
            "cancel_url": reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist"),
        }
        return TemplateResponse(request, "admin_extras/action_form.html", context)

    # --- actions --------------------------------------------------------

    @admin.action(description="Recover stuck POST_PROCESSING → send POST_PROCESSING_COMPLETED (→ Ended)")
    def recover_post_processing(self, request, queryset):
        for bot in queryset:
            self._safe_create_event(
                request,
                bot,
                BotEventTypes.POST_PROCESSING_COMPLETED,
                event_metadata={"source": "admin_extras.recover_post_processing"},
            )

    @admin.action(description="Dispatch bot event…")
    def dispatch_bot_event(self, request, queryset):
        if "apply" in request.POST:
            form = BotEventDispatchForm(request.POST)
            if form.is_valid():
                event_type = form.cleaned_data["event_type"]
                event_sub_type = form.cleaned_data["event_sub_type"]
                event_metadata = form.cleaned_data["event_metadata"] or {}
                for bot in queryset:
                    self._safe_create_event(request, bot, event_type, event_sub_type, event_metadata)
                return None
        else:
            form = BotEventDispatchForm()
        return self._render_action_form(
            request,
            queryset,
            form,
            action_name="dispatch_bot_event",
            title="Dispatch bot event",
            description="Send a raw state-machine event to the selected bot(s). Invalid transitions for a bot's current state are rejected and reported.",
            submit_label="Dispatch event",
        )

    @admin.action(description="Start async transcription…")
    def start_async_transcription(self, request, queryset):
        if "apply" in request.POST:
            form = StartAsyncTranscriptionForm(request.POST)
            if form.is_valid():
                settings_data = form.cleaned_data["transcription_settings"]
                for bot in queryset:
                    self._start_async_transcription_for_bot(request, bot, settings_data)
                return None
        else:
            form = StartAsyncTranscriptionForm()
        return self._render_action_form(
            request,
            queryset,
            form,
            action_name="start_async_transcription",
            title="Start async transcription",
            description="Creates an AsyncTranscription for each selected bot's default recording and enqueues processing. "
            "The bot must be in the <strong>Ended</strong> state with per-speaker audio chunks still present "
            "(run the POST_PROCESSING recovery action first if a bot is stuck in post processing).",
            submit_label="Start transcription",
        )

    def _start_async_transcription_for_bot(self, request, bot, settings_data):
        from bots.serializers import CreateAsyncTranscriptionSerializer
        from bots.tasks.process_async_transcription_task import process_async_transcription

        def fail(msg):
            self.message_user(request, f"{bot.object_id}: {msg}", level=messages.ERROR)

        if not bot.project.organization.is_async_transcription_enabled:
            return fail("async transcription is not enabled for the organization.")
        if not bot.record_async_transcription_audio_chunks():
            return fail("bot was not created with recording_settings.record_async_transcription_audio_chunks enabled.")
        if bot.state != BotStates.ENDED:
            return fail(f"bot must be in state ended (currently {BotStates.state_to_api_code(bot.state)}). Run the POST_PROCESSING recovery action first.")

        recording = Recording.objects.filter(bot=bot, is_default_recording=True).first()
        if not recording:
            return fail("no default recording found.")
        if not recording.audio_chunks.exclude(audio_blob=b"").exists() and not recording.audio_chunks.exclude(audio_blob_remote_file=None).exists():
            return fail("per-speaker audio data has been deleted or was never created.")

        max_count = int(os.getenv("MAX_ASYNC_TRANSCRIPTIONS_PER_RECORDING", 4))
        if AsyncTranscription.objects.filter(recording=recording).count() >= max_count:
            return fail(f"already at the maximum of {max_count} async transcriptions for this recording.")

        serializer = CreateAsyncTranscriptionSerializer(data={"transcription_settings": settings_data})
        if not serializer.is_valid():
            return fail(f"invalid transcription settings: {serializer.errors}")

        async_transcription = AsyncTranscription.objects.create(recording=recording, settings=serializer.validated_data)
        process_async_transcription.delay(async_transcription.id)
        self.message_user(request, f"{bot.object_id}: started async transcription {async_transcription.object_id}.", level=messages.SUCCESS)


def augment_bot_admin():
    """Re-register Bot's admin as a subclass that includes our operational actions.

    Called from admin_extras.admin after autodiscovery (bots is earlier than
    admin_extras in INSTALLED_APPS, so the host BotAdmin is already registered).
    Falls back to a bare ModelAdmin if Bot somehow isn't registered yet.
    """
    existing = admin.site._registry.get(Bot)
    base_cls = existing.__class__ if existing is not None else admin.ModelAdmin
    if existing is not None:
        admin.site.unregister(Bot)
    new_cls = type("BotAdmin", (BotActionsMixin, base_cls), {})
    admin.site.register(Bot, new_cls)
    logger.info("admin_extras: augmented Bot admin with operational actions (base: %s)", base_cls.__name__)
