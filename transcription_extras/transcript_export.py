"""Assemble an AsyncTranscription's utterances into a readable transcript / JSON.

Pure functions over already-ordered utterances (anything exposing ``timestamp_ms``,
``duration_ms``, ``participant`` and ``transcription``), so they are DB-free testable.
Order the utterances (by ``timestamp_ms``) before passing them in.
"""


def format_timestamp(ms):
    total_seconds = int(ms // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


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
