"""Combine each speaker's audio chunks into larger files for custom-async transcription.

Public entry point ``get_transcription_for_utterance_group(utterances)`` is called
from the (upstream) async group task for CUSTOM_ASYNC / CUSTOM_ASYNC_V2 providers.

It reuses Attendee's existing MP3 builder and our hardened splitter:
  - get_mp3_for_utterance_group     : concatenate raw PCM + fixed silence -> one MP3
  - .split.split_transcription_by_utterance: re-attribute words to each chunk by
    time window, with drift-free windows (from real encoded length) and
    silence-gap hallucination rejection

Each combined file is INDEPENDENT: the service returns word times starting at 0 for
that file, so every combined file is split on its OWN utterance subset, in
concatenation order, with the same silence gap used to build it. We never share a
window list across files and never treat the times as a global meeting timeline.
"""

import logging

from bots.transcription_utils import get_mp3_for_utterance_group

from . import config
from .grouping import group_by_speaker, split_into_size_capped_groups
from .split import split_transcription_by_utterance
from .whisperx_group_client import build_request_params, transcribe_combined_mp3

logger = logging.getLogger(__name__)


def get_transcription_for_utterance_group(utterances):
    """Return ({utterance_id: {"transcript","words","language","language_confidence","confidence"}}, failure_data).

    On the first sub-group failure, returns (None, failure_data) so the whole group is
    retried/failed as a unit — matching the AssemblyAI group path.
    """
    utterances = list(utterances)
    if not utterances:
        logger.warning("transcription_extras: get_transcription_for_utterance_group called with no utterances")
        return {}, None

    silence_seconds = config.combine_silence_seconds()
    bitrate_kbps = config.mp3_bitrate_kbps()
    max_bytes = config.effective_max_upload_bytes()
    headers, data = build_request_params(utterances)

    by_speaker = group_by_speaker(utterances)
    logger.info(
        "transcription_extras: combining %d utterance(s) across %d speaker(s) (provider=%s, gap=%.2fs, bitrate=%dkbps, max_upload=%dB)",
        len(utterances),
        len(by_speaker),
        utterances[0].transcription_provider,
        silence_seconds,
        bitrate_kbps,
        max_bytes,
    )

    transcriptions = {}
    files_sent = 0
    for participant_id, speaker_utterances in by_speaker.items():
        sub_groups = split_into_size_capped_groups(
            speaker_utterances,
            max_bytes=max_bytes,
            bitrate_kbps=bitrate_kbps,
            silence_seconds=silence_seconds,
        )
        logger.info(
            "transcription_extras: speaker %s -> %d chunk(s) packed into %d combined file(s)",
            participant_id,
            len(speaker_utterances),
            len(sub_groups),
        )

        for sub_group in sub_groups:
            sample_rate = sub_group[0].get_sample_rate()
            utterance_ids = [u.id for u in sub_group]
            audio_ms = sum(u.duration_ms for u in sub_group)
            identifier = f"participant {participant_id} utterances {utterance_ids}"

            mp3_bytes = get_mp3_for_utterance_group(
                sub_group,
                sample_rate=sample_rate,
                silence_seconds=silence_seconds,
                bitrate_kbps=bitrate_kbps,
            )
            logger.info(
                "transcription_extras: built combined MP3 for %s (chunks=%d, audio=%.1fs, sample_rate=%d, size=%dB)",
                identifier,
                len(sub_group),
                audio_ms / 1000.0,
                sample_rate,
                len(mp3_bytes),
            )

            transcription_result, failure_data = transcribe_combined_mp3(mp3_bytes, headers=headers, data=data, identifier=identifier)
            if failure_data:
                logger.warning("transcription_extras: combined transcription failed for %s: %s", identifier, failure_data)
                return None, failure_data

            # Split on THIS file's utterances only — its word times start at 0.
            split = split_transcription_by_utterance(transcription_result, sub_group, silence_seconds=silence_seconds)
            transcriptions.update(split)
            files_sent += 1
            logger.info(
                "transcription_extras: split %s into %d utterance transcript(s); words per utterance: %s",
                identifier,
                len(split),
                {uid: len(split.get(uid, {}).get("words", [])) for uid in utterance_ids},
            )

    logger.info(
        "transcription_extras: completed group — %d utterance(s) transcribed via %d combined request(s)",
        len(transcriptions),
        files_sent,
    )
    return transcriptions, None
