"""Assemble an AsyncTranscription's utterances into a readable transcript / JSON.

Functions operate over already-ordered utterances (anything exposing ``timestamp_ms``,
``duration_ms``, ``participant`` and ``transcription``). Order the utterances (by
``timestamp_ms``) before passing them in.
"""

from datetime import datetime
from datetime import timezone as datetime_timezone

from django.utils import timezone


def format_timestamp(epoch_ms):
    """Render a Unix epoch-millisecond timestamp as a localized wall-clock datetime.

    ``Utterance.timestamp_ms`` (inherited from the audio chunk) is a Unix epoch time in
    milliseconds captured during the meeting — NOT an offset from the meeting start. It
    is shown as ``YYYY-MM-DD HH:MM:SS`` in the project timezone, with seconds, which
    matters for correlating runtimes.
    """
    aware_utc = datetime.fromtimestamp(epoch_ms / 1000, tz=datetime_timezone.utc)
    return timezone.localtime(aware_utc).strftime("%Y-%m-%d %H:%M:%S")


def speaker_label(utterance):
    participant = getattr(utterance, "participant", None)
    if participant is None:
        return "Unknown"
    return getattr(participant, "full_name", None) or getattr(participant, "uuid", None) or "Unknown"


def transcript_lines(utterances):
    """Human-readable lines: ``[HH:MM:SS] Speaker: text`` for non-empty utterances."""
    lines = []
    for utterance in utterances:
        transcription = getattr(utterance, "transcription", None) or {}
        text = (transcription.get("transcript") or "").strip()
        if not text:
            continue
        lines.append(f"[{format_timestamp(utterance.timestamp_ms)}] {speaker_label(utterance)}: {text}")
    return lines


def transcript_text(utterances):
    return "\n".join(transcript_lines(utterances))


def transcript_json(utterances):
    """Structured export: one row per utterance, including word-level timings."""
    rows = []
    for utterance in utterances:
        transcription = getattr(utterance, "transcription", None) or {}
        rows.append(
            {
                "utterance_id": getattr(utterance, "id", None),
                "timestamp_ms": utterance.timestamp_ms,
                "duration_ms": utterance.duration_ms,
                "speaker": speaker_label(utterance),
                "transcript": transcription.get("transcript", ""),
                "words": transcription.get("words", []),
                "language": transcription.get("language"),
            }
        )
    return rows
