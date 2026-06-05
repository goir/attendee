"""Per-speaker, size-capped batching of utterances.

Pure helpers with no Django/model imports — they operate on any object exposing
``participant_id``, ``timestamp_ms`` and ``duration_ms``, which keeps them
unit-testable without a database.
"""

import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)


def estimate_mp3_bytes(audio_ms, num_gaps, *, bitrate_kbps, silence_seconds):
    """Estimate the combined constant-bitrate MP3 size for a set of chunks.

    CBR MP3 size ~= (bitrate / 8) * encoded_seconds, where the encoded audio is the
    concatenated chunk audio plus the inter-chunk silence we insert.
    """
    total_seconds = (audio_ms / 1000.0) + (num_gaps * silence_seconds)
    return (bitrate_kbps * 1000 / 8.0) * total_seconds


def group_by_speaker(utterances):
    """Group utterances by participant, preserving first-seen speaker order."""
    by_speaker = OrderedDict()
    for utterance in utterances:
        by_speaker.setdefault(utterance.participant_id, []).append(utterance)
    return by_speaker


def split_into_size_capped_groups(utterances, *, max_bytes, bitrate_kbps, silence_seconds):
    """Split time-ordered utterances into sub-groups whose combined MP3 fits max_bytes.

    A single utterance whose estimated size exceeds ``max_bytes`` becomes its own
    group (logged) rather than being dropped.
    """
    ordered = sorted(utterances, key=lambda u: u.timestamp_ms)
    groups = []
    current = []
    for utterance in ordered:
        candidate = current + [utterance]
        estimated_bytes = estimate_mp3_bytes(
            sum(u.duration_ms for u in candidate),
            num_gaps=len(candidate) - 1,
            bitrate_kbps=bitrate_kbps,
            silence_seconds=silence_seconds,
        )
        if current and estimated_bytes > max_bytes:
            groups.append(current)
            current = [utterance]
        else:
            current = candidate

    if current:
        groups.append(current)

    for group in groups:
        if len(group) == 1:
            estimated_bytes = estimate_mp3_bytes(group[0].duration_ms, num_gaps=0, bitrate_kbps=bitrate_kbps, silence_seconds=silence_seconds)
            if estimated_bytes > max_bytes:
                logger.warning(
                    "transcription_extras: utterance %s (~%d bytes) exceeds max upload %d; sending alone",
                    getattr(group[0], "id", "?"),
                    int(estimated_bytes),
                    max_bytes,
                )
    return groups
