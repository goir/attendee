"""Tests for custom-async chunk combining.

These are DB-free (SimpleTestCase + lightweight stand-in objects), so they run
without Postgres. They cover the parts that carry the real risk: per-speaker /
size-capped grouping, response parsing, and that each combined file is split on
its own 0-based timeline back onto the correct utterances.
"""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from bots.models import TranscriptionProviders
from transcription_extras import config
from transcription_extras.grouping import estimate_mp3_bytes, group_by_speaker, split_into_size_capped_groups
from transcription_extras.whisperx_group_client import _parse_done_response


def _utt(id, participant_id, timestamp_ms, duration_ms, sample_rate=16000, provider=TranscriptionProviders.CUSTOM_ASYNC):
    settings = SimpleNamespace(
        custom_async_additional_props=lambda: {},
        custom_async_v2_headers=lambda: {},
        custom_async_v2_form_data=lambda: {},
    )
    # Real s16le-mono PCM sized to duration_ms — the splitter derives its windows
    # from the actual encoded length, so the blob must exist and match.
    audio_blob = b"\x00" * int(round(duration_ms / 1000.0 * sample_rate * 2))
    return SimpleNamespace(
        id=id,
        participant_id=participant_id,
        timestamp_ms=timestamp_ms,
        duration_ms=duration_ms,
        get_sample_rate=lambda: sample_rate,
        get_audio_blob=lambda: audio_blob,
        transcription_provider=provider,
        transcription_settings=settings,
    )


class GroupingTests(SimpleTestCase):
    def test_group_by_speaker_partitions_and_preserves_order(self):
        utts = [_utt(1, "A", 0, 1000), _utt(2, "B", 100, 1000), _utt(3, "A", 2000, 1000)]
        grouped = group_by_speaker(utts)
        self.assertEqual(list(grouped.keys()), ["A", "B"])
        self.assertEqual([u.id for u in grouped["A"]], [1, 3])
        self.assertEqual([u.id for u in grouped["B"]], [2])

    def test_size_cap_splits_into_multiple_groups(self):
        # 128 kbps => 16000 bytes/s. Cap at ~3s of audio.
        max_bytes = int(estimate_mp3_bytes(3000, 0, bitrate_kbps=128, silence_seconds=1.5))
        utts = [_utt(i, "A", i * 1000, 1000) for i in range(6)]  # 6 x 1s chunks
        groups = split_into_size_capped_groups(utts, max_bytes=max_bytes, bitrate_kbps=128, silence_seconds=1.5)
        self.assertGreater(len(groups), 1)
        # Every group must fit the cap, and all utterances are accounted for exactly once.
        for group in groups:
            est = estimate_mp3_bytes(sum(u.duration_ms for u in group), len(group) - 1, bitrate_kbps=128, silence_seconds=1.5)
            self.assertLessEqual(est, max_bytes)
        self.assertEqual(sorted(u.id for g in groups for u in g), list(range(6)))

    def test_oversized_single_utterance_is_its_own_group(self):
        utts = [_utt(1, "A", 0, 600_000)]  # 10 minutes, far over any small cap
        groups = split_into_size_capped_groups(utts, max_bytes=1000, bitrate_kbps=128, silence_seconds=1.5)
        self.assertEqual(len(groups), 1)
        self.assertEqual([u.id for u in groups[0]], [1])


class ParseResponseTests(SimpleTestCase):
    def test_parse_flattens_words_across_service_utterances(self):
        result_data = {
            "status": "done",
            "result": {"transcription": {"full_transcript": "hello world", "utterances": [{"words": [{"word": "hello", "start": 0.5, "end": 1.0}]}, {"words": [{"word": "world", "start": 4.0, "end": 4.5}]}]}},
        }
        parsed = _parse_done_response(result_data)
        self.assertEqual(parsed["transcript"], "hello world")
        self.assertEqual([w["word"] for w in parsed["words"]], ["hello", "world"])


class EndToEndSplitTests(SimpleTestCase):
    @mock.patch.dict("os.environ", {config.TRANSCRIPTION_URL_ENV: "https://whisperx.example/attendee/transcribe"})
    def test_each_file_is_split_on_its_own_zero_based_timeline(self):
        from transcription_extras import group_transcription

        # Speaker A: u1 (2s) + u2 (3s) combined into one file; Speaker B: u3 alone.
        u1 = _utt(1, "A", 0, 2000)
        u2 = _utt(2, "A", 2000, 3000)
        u3 = _utt(3, "B", 5000, 4000)

        # Service returns word times from 0 for EACH file independently.
        # File A (silence 1.5s): u1 window [0,2), u2 window [3.5,6.5).
        response_a = mock.Mock(status_code=200)
        response_a.json.return_value = {
            "status": "done",
            "result": {"transcription": {"full_transcript": "hi there", "utterances": [{"words": [{"word": "hi", "start": 0.5, "end": 1.0}]}, {"words": [{"word": "there", "start": 4.0, "end": 4.5}]}]}},
        }
        # File B: u3 window [0,4).
        response_b = mock.Mock(status_code=200)
        response_b.json.return_value = {
            "status": "done",
            "result": {"transcription": {"full_transcript": "bye", "utterances": [{"words": [{"word": "bye", "start": 1.0, "end": 1.5}]}]}},
        }

        with mock.patch.object(group_transcription, "get_mp3_for_utterance_group", return_value=b"fake-mp3") as mock_mp3, mock.patch("transcription_extras.whisperx_group_client.requests.post", side_effect=[response_a, response_b]) as mock_post:
            transcriptions, failure = group_transcription.get_transcription_for_utterance_group([u1, u2, u3])

        self.assertIsNone(failure)
        # One combined file per (speaker, size-group): A together, B alone => 2 requests.
        self.assertEqual(mock_mp3.call_count, 2)
        self.assertEqual(mock_post.call_count, 2)
        # Words land on the right utterances, re-based to each utterance's own start.
        self.assertEqual(set(transcriptions.keys()), {1, 2, 3})
        self.assertEqual(transcriptions[1]["transcript"], "hi")
        self.assertEqual(transcriptions[2]["transcript"], "there")
        self.assertEqual(transcriptions[3]["transcript"], "bye")
        self.assertAlmostEqual(transcriptions[1]["words"][0]["start"], 0.5)
        self.assertAlmostEqual(transcriptions[2]["words"][0]["start"], 0.5)  # 4.0 - 3.5 window start

    @mock.patch.dict("os.environ", {config.TRANSCRIPTION_URL_ENV: "https://whisperx.example/attendee/transcribe"})
    def test_combined_path_keeps_first_word_at_window_edge(self):
        from transcription_extras import group_transcription

        # One speaker, two 2s chunks combined: u1 [0,2) gap [2,3.5) u2 [3.5,5.5).
        u1 = _utt(1, "A", 0, 2000)
        u2 = _utt(2, "A", 2000, 2000)

        # "Und" is u2's first word but its timestamp bled into the leading gap
        # (3.3-3.5s, midpoint nearer u2). It must be kept on u2, not clipped —
        # the real-data regression where leading words were dropped.
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "status": "done",
            "result": {"transcription": {"full_transcript": "hi Und there", "utterances": [{"words": [
                {"word": "hi", "start": 0.5, "end": 1.0},
                {"word": "Und", "start": 3.3, "end": 3.5},
                {"word": "there", "start": 4.0, "end": 4.5},
            ]}]}},
        }

        with mock.patch.object(group_transcription, "get_mp3_for_utterance_group", return_value=b"fake-mp3"), mock.patch("transcription_extras.whisperx_group_client.requests.post", side_effect=[response]):
            transcriptions, failure = group_transcription.get_transcription_for_utterance_group([u1, u2])

        self.assertIsNone(failure)
        self.assertEqual(transcriptions[1]["transcript"], "hi")
        self.assertEqual(transcriptions[2]["transcript"], "Und there")
        all_words = [w["word"] for utt in transcriptions.values() for w in utt["words"]]
        self.assertEqual(sorted(all_words), ["Und", "hi", "there"])

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_missing_url_returns_failure(self):
        from transcription_extras import group_transcription

        with mock.patch.object(group_transcription, "get_mp3_for_utterance_group", return_value=b"fake-mp3"):
            transcriptions, failure = group_transcription.get_transcription_for_utterance_group([_utt(1, "A", 0, 1000)])

        self.assertIsNone(transcriptions)
        self.assertIsNotNone(failure)


def _transcribed_utt(id, participant_name, timestamp_ms, transcript, words=None):
    return SimpleNamespace(
        id=id,
        participant=SimpleNamespace(full_name=participant_name, uuid=f"uuid-{id}"),
        timestamp_ms=timestamp_ms,
        duration_ms=1000,
        transcription={"transcript": transcript, "words": words or [], "language": "en"} if transcript is not None else None,
    )


class AsyncTranscriptionAdminPermissionTests(SimpleTestCase):
    """The admin is view-only for editing but permits deletion."""

    def _admin(self):
        from django.contrib import admin

        from bots.models import AsyncTranscription

        return admin.site._registry[AsyncTranscription]

    def test_registered_admin_is_ours(self):
        from transcription_extras.admin import AsyncTranscriptionAdmin

        self.assertIsInstance(self._admin(), AsyncTranscriptionAdmin)

    def test_records_are_not_editable_but_are_deletable(self):
        model_admin = self._admin()
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertTrue(model_admin.has_delete_permission(None))

    def test_bulk_delete_action_is_available(self):
        # has_delete_permission=True keeps Django's site-wide "delete_selected"
        # bulk action in the changelist (the admin doesn't set actions = None).
        from django.test import RequestFactory

        actions = self._admin().get_actions(RequestFactory().get("/admin/"))
        self.assertIn("delete_selected", actions)


@override_settings(TIME_ZONE="UTC", USE_TZ=True)
class TranscriptExportTests(SimpleTestCase):
    def test_transcript_text_renders_epoch_ms_as_datetime_with_seconds(self):
        from transcription_extras.transcript_export import transcript_text

        # timestamp_ms is a Unix epoch in ms; 0 -> 1970-01-01 00:00:00, 65000 -> +1:05.
        utts = [_transcribed_utt(1, "Alice", 0, "hello"), _transcribed_utt(2, "Bob", 65000, "hi there")]
        text = transcript_text(utts)
        self.assertEqual(text, "[1970-01-01 00:00:00] Alice: hello\n[1970-01-01 00:01:05] Bob: hi there")

    def test_transcript_text_uses_real_meeting_epoch(self):
        from datetime import datetime
        from datetime import timezone as dtz

        from transcription_extras.transcript_export import transcript_text

        # A real meeting-scale epoch (the bug report showed ~1.78e12 ms): must render as
        # a 2020s wall-clock datetime with seconds, not an overflowed HH:MM:SS.
        epoch_ms = 1780669050000
        expected = datetime.fromtimestamp(epoch_ms / 1000, tz=dtz.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(expected.startswith("2026-"), expected)
        utts = [_transcribed_utt(1, "Nicolas", epoch_ms, "Ja")]
        self.assertEqual(transcript_text(utts), f"[{expected}] Nicolas: Ja")

    def test_transcript_text_skips_empty_and_pending(self):
        from transcription_extras.transcript_export import transcript_text

        utts = [_transcribed_utt(1, "Alice", 0, "hello"), _transcribed_utt(2, "Bob", 1000, ""), _transcribed_utt(3, "Cara", 2000, None)]
        self.assertEqual(transcript_text(utts), "[1970-01-01 00:00:00] Alice: hello")

    def test_transcript_json_includes_words_and_speaker(self):
        from transcription_extras.transcript_export import transcript_json

        rows = transcript_json([_transcribed_utt(1, "Alice", 0, "hello", words=[{"word": "hello", "start": 0.1, "end": 0.5}])])
        self.assertEqual(rows[0]["speaker"], "Alice")
        self.assertEqual(rows[0]["transcript"], "hello")
        self.assertEqual(rows[0]["words"][0]["word"], "hello")
