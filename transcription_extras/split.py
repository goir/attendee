"""Hardened re-implementation of ``split_transcription_by_utterance``.

This overrides ``bots.transcription_utils.split_transcription_by_utterance`` for
the custom-async combined path. It fixes two defects in the upstream version
that surface as misplaced / orphaned words in the merged transcript:

1. **Drift-free windows.** Each utterance window length is derived from the
   ACTUAL PCM byte length (``len(get_audio_blob()) / bytes_per_second``), not the
   stored ``duration_ms``. ``Utterance.duration_ms`` is a rounded ``IntegerField``
   (``bots.models``) that is not guaranteed to equal the encoded audio length, so
   using it makes the windows drift cumulatively across a multi-chunk file —
   boundary words near the end of a large combined file get pushed onto the wrong
   utterance (or dropped). Deriving from the blob makes the windows match exactly
   what ``get_mp3_for_utterance_group`` fed to ffmpeg.

2. **Gap-artifact rejection.** Each word is assigned to the window of MAXIMUM
   temporal overlap, and any word whose in-window overlap is below
   ``min_overlap_fraction`` of its own duration is DROPPED (and logged) as a
   silence-gap artifact. Whisper hallucinations on the inter-chunk silence land
   in the gap between two windows; upstream's "first window that overlaps, then
   break" leaks such a word onto the *following* utterance's head (often with a
   negative re-based start). Here it is discarded.

Pure module — no Django/model imports. It operates on any duck-typed utterance
exposing ``id``, ``get_audio_blob()`` and ``get_sample_rate()`` — the real
``Utterance`` API. There is no ``duration_ms`` fallback: windows are derived
solely from the actual encoded audio. This keeps it unit-testable without a
database or the transcription service.

The return contract matches upstream exactly:
    { utterance_id: {"transcript": str, "words": [...], "language": str|None} }
with each word's ``start``/``end`` re-based to its own utterance's start.
"""

import logging

logger = logging.getLogger(__name__)

# s16le mono is what get_mp3_for_utterance_group streams into ffmpeg; keep these
# in sync with that builder so window math matches the encoded audio.
_DEFAULT_SAMPLE_WIDTH_BYTES = 2
_DEFAULT_CHANNELS = 1


def _utterance_seconds(utterance, *, sample_width_bytes, channels):
    """Exact audio seconds from the encoded PCM length.

    Uses the utterance's real audio so windows match exactly what ffmpeg encoded
    into the combined MP3. No ``duration_ms`` fallback: it is a rounded field that
    drifts, and every utterance on the combined path carries a blob
    (``get_mp3_for_utterance_group`` already raises if one is missing).
    """
    blob = utterance.get_audio_blob()
    sample_rate = utterance.get_sample_rate()
    if blob is None:
        raise ValueError(f"utterance {getattr(utterance, 'id', '?')} has no audio blob")
    if not sample_rate:
        raise ValueError(f"utterance {getattr(utterance, 'id', '?')} has no sample rate")
    bytes_per_second = int(sample_rate) * int(channels) * int(sample_width_bytes)
    if bytes_per_second <= 0:
        raise ValueError(f"utterance {getattr(utterance, 'id', '?')} has invalid audio params")
    return len(blob) / float(bytes_per_second)


def split_transcription_by_utterance(
    transcription_result,
    utterances,
    *,
    silence_seconds=3.0,
    min_overlap_fraction=0.5,
    sample_width_bytes=_DEFAULT_SAMPLE_WIDTH_BYTES,
    channels=_DEFAULT_CHANNELS,
):
    """Split a combined-file transcription back into per-utterance results.

    Assumes the utterances were concatenated in THIS order with exactly
    ``silence_seconds`` of silence between them (matching
    ``get_mp3_for_utterance_group``).

    ``min_overlap_fraction`` is the share of a word's own duration that must fall
    inside its best-matching utterance window for the word to be kept; words that
    sit mostly in an inter-chunk silence gap (hallucinations) are dropped.
    """
    utterances = list(utterances)
    if not utterances:
        return {}

    language = transcription_result.get("language")
    words = transcription_result.get("words") or []

    # Build utterance time windows in the combined audio, from real encoded length.
    windows = []
    t = 0.0
    for u in utterances:
        dur_s = _utterance_seconds(u, sample_width_bytes=sample_width_bytes, channels=channels)
        start = t
        end = start + dur_s
        windows.append((u.id, start, end))
        t = end + silence_seconds

    output = {u.id: {"transcript": "", "words": [], "language": language} for u in utterances}
    buckets = {u.id: [] for u in utterances}

    dropped = 0
    for w in words:
        w_start = w.get("start")
        w_end = w.get("end")
        if w_start is None or w_end is None:
            dropped += 1
            continue

        # Assign to the window of maximum temporal overlap.
        best_id = None
        best_start = 0.0
        best_overlap = 0.0
        for utterance_id, start, end in windows:
            overlap = min(w_end, end) - max(w_start, start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_id = utterance_id
                best_start = start

        word_duration = max(w_end - w_start, 1e-9)
        if best_id is None or best_overlap < min_overlap_fraction * word_duration:
            # Mostly (or entirely) inside an inter-chunk silence gap: a Whisper
            # hallucination on dead air. Drop rather than leak onto a neighbour.
            dropped += 1
            logger.info(
                "transcription_extras.split: dropping gap-artifact word %r (start=%.3f end=%.3f, best_overlap=%.3f)",
                w.get("word"), w_start, w_end, best_overlap,
            )
            continue

        word_adjusted = dict(w)
        word_adjusted["start"] = w_start - best_start
        word_adjusted["end"] = w_end - best_start
        buckets[best_id].append(word_adjusted)

    for utterance_id, utterance_words in buckets.items():
        output[utterance_id]["words"] = utterance_words
        output[utterance_id]["transcript"] = " ".join(w["word"] for w in utterance_words)

    if dropped:
        logger.info("transcription_extras.split: dropped %d gap/invalid word(s) across %d utterance(s)", dropped, len(utterances))

    return output
