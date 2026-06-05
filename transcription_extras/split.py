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

2. **Robust, lossless assignment.** Each word is bucketed to the window nearest
   its midpoint and re-based to that utterance's start. Words are NEVER dropped
   (only entries missing start/end are skipped). This tolerates the small offset
   between WhisperX's decoded-MP3 timeline and the window math (MP3 encoder delay
   + VAD timestamp restoration) that would otherwise clip a word sitting at a
   window's left edge — i.e. the first word of an utterance. Upstream leaks a gap
   word onto the *following* utterance specifically; nearest-midpoint puts it on
   whichever side is actually closer. Suppressing Whisper hallucinations on the
   inter-chunk silence is intentionally NOT done here: a hallucination and a real
   first word are indistinguishable by timing alone, so that belongs in the ASR
   layer (no_speech_prob / VAD), not the splitter.

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

# s16le mono is what get_mp3_for_utterance_group streams into ffmpeg, so the
# combined audio is always 2 bytes/sample, 1 channel.
_BYTES_PER_SAMPLE = 2
_CHANNELS = 1


def _utterance_seconds(utterance):
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
    return len(blob) / float(int(sample_rate) * _CHANNELS * _BYTES_PER_SAMPLE)


def split_transcription_by_utterance(
    transcription_result,
    utterances,
    *,
    silence_seconds=3.0,
):
    """Split a combined-file transcription back into per-utterance results.

    Assumes the utterances were concatenated in THIS order with exactly
    ``silence_seconds`` of silence between them (matching
    ``get_mp3_for_utterance_group``). Each word is bucketed to the window nearest
    its midpoint and re-based to that utterance's start. Words are never dropped
    except entries lacking start/end.
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
        dur_s = _utterance_seconds(u)
        start = t
        end = start + dur_s
        windows.append((u.id, start, end))
        t = end + silence_seconds

    output = {u.id: {"transcript": "", "words": [], "language": language} for u in utterances}
    buckets = {u.id: [] for u in utterances}

    skipped = 0
    for w in words:
        w_start = w.get("start")
        w_end = w.get("end")
        if w_start is None or w_end is None:
            skipped += 1
            continue

        # Bucket by nearest window to the word's midpoint. Robust to the small
        # offset between the decoded-MP3 timeline and the window math: a word that
        # bled into a silence gap still lands on its rightful utterance instead of
        # being clipped. Never drops real speech.
        midpoint = (w_start + w_end) / 2.0
        best_id = None
        best_start = 0.0
        best_dist = None
        for utterance_id, start, end in windows:
            if midpoint < start:
                dist = start - midpoint
            elif midpoint > end:
                dist = midpoint - end
            else:
                dist = 0.0
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_id = utterance_id
                best_start = start

        word_adjusted = dict(w)
        # Re-base to the chosen utterance; clamp so a word that started just
        # inside the preceding gap never yields a negative timestamp.
        rebased_start = w_start - best_start
        rebased_end = w_end - best_start
        word_adjusted["start"] = max(0.0, rebased_start)
        word_adjusted["end"] = max(word_adjusted["start"], rebased_end)
        buckets[best_id].append(word_adjusted)

    for utterance_id, utterance_words in buckets.items():
        output[utterance_id]["words"] = utterance_words
        output[utterance_id]["transcript"] = " ".join(w["word"] for w in utterance_words)

    if skipped:
        logger.info("transcription_extras.split: skipped %d word(s) missing start/end", skipped)

    return output
