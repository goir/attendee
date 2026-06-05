"""Richer admin for AsyncTranscription: inline transcript preview + download links.

AsyncTranscription has no hand-written admin upstream, so admin_extras auto-registers
a generic read-only admin for it. We swap that out here (unregister + register) for a
view that assembles the per-utterance transcriptions into a readable transcript and
offers .txt / .json downloads. Kept in this app so bots/admin.py stays untouched.
"""

import json
import logging

from django.contrib import admin
from django.http import HttpResponse, HttpResponseNotFound
from django.urls import path, reverse
from django.utils.html import format_html

from bots.models import AsyncTranscription

from .transcript_export import transcript_json, transcript_text

logger = logging.getLogger(__name__)

PREVIEW_CHAR_LIMIT = 5000
DOWNLOAD_URL_NAME = "transcription_extras_asynctranscription_download"


def _ordered_utterances(async_transcription):
    return list(async_transcription.utterances.select_related("participant").order_by("timestamp_ms"))


class AsyncTranscriptionAdmin(admin.ModelAdmin):
    list_display = ("object_id", "recording", "state", "utterance_count", "created_at", "completed_at")
    list_filter = ("state",)
    search_fields = ("object_id", "recording__bot__object_id")
    readonly_fields = (
        "object_id",
        "recording",
        "state",
        "created_at",
        "updated_at",
        "started_at",
        "failed_at",
        "completed_at",
        "settings",
        "failure_data",
        "version",
        "utterance_count",
        "download_links",
        "transcript_preview",
    )
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def utterance_count(self, obj):
        return obj.utterances.count()

    utterance_count.short_description = "Utterances"

    def download_links(self, obj):
        if obj.pk is None:
            return "-"
        txt_url = reverse(f"admin:{DOWNLOAD_URL_NAME}", args=[obj.pk, "txt"])
        json_url = reverse(f"admin:{DOWNLOAD_URL_NAME}", args=[obj.pk, "json"])
        return format_html('<a class="button" href="{}">Download .txt</a>&nbsp;<a class="button" href="{}">Download .json</a>', txt_url, json_url)

    download_links.short_description = "Download"

    def transcript_preview(self, obj):
        if obj.pk is None:
            return "-"
        text = transcript_text(_ordered_utterances(obj))
        if not text:
            return "(no transcription available yet)"
        truncated = text[:PREVIEW_CHAR_LIMIT]
        if len(text) > PREVIEW_CHAR_LIMIT:
            truncated += "\n…(truncated — use the download links above for the full transcript)"
        return format_html('<pre style="white-space:pre-wrap; max-height:500px; overflow:auto; background:#f8f8f8; padding:10px; border:1px solid #ddd;">{}</pre>', truncated)

    transcript_preview.short_description = "Transcript"

    def get_urls(self):
        custom_urls = [
            path(
                "<int:pk>/download/<str:fmt>/",
                self.admin_site.admin_view(self.download_view),
                name=DOWNLOAD_URL_NAME,
            ),
        ]
        return custom_urls + super().get_urls()

    def download_view(self, request, pk, fmt):
        try:
            async_transcription = AsyncTranscription.objects.get(pk=pk)
        except AsyncTranscription.DoesNotExist:
            return HttpResponseNotFound("AsyncTranscription not found")

        utterances = _ordered_utterances(async_transcription)
        object_id = async_transcription.object_id

        if fmt == "json":
            content = json.dumps(transcript_json(utterances), indent=2, ensure_ascii=False)
            response = HttpResponse(content, content_type="application/json; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="{object_id}.json"'
            return response
        if fmt == "txt":
            response = HttpResponse(transcript_text(utterances), content_type="text/plain; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="{object_id}.txt"'
            return response
        return HttpResponseNotFound("Unknown format (use 'txt' or 'json')")


# Replace admin_extras' generic read-only admin (if it registered first) with ours.
if admin.site.is_registered(AsyncTranscription):
    admin.site.unregister(AsyncTranscription)
admin.site.register(AsyncTranscription, AsyncTranscriptionAdmin)
