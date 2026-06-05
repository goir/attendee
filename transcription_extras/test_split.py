"""Reproduction + verification for utterance-splitting.

Django-free on purpose: these run with bare ``python -m unittest`` (and under the
repo's ``pytest``) without Postgres, Django settings, or the transcription
service. They feed the split helper a hand-built service response — the same
shape ``whisperx_group_client._parse_done_response`` produces — and assert how
words land on utterances.

``_legacy_split`` is a faithful copy of the current upstream
``bots.transcription_utils.split_transcription_by_utterance`` (the
"first overlapping window, then break" loop with duration_ms windows). It is the
drift witness in WindowDriftTests.

The hardened ``transcription_extras.split`` derives windows from the real encoded
audio length and buckets each word to the window nearest its midpoint, never
dropping real speech. FirstWordPreservationTests guards the real-data regression
where an earlier overlap-fraction drop ate the first word of utterances.
"""

import unittest
from types import SimpleNamespace

from transcription_extras.split import split_transcription_by_utterance

_SAMPLE_RATE = 16000
_BYTES_PER_SECOND = _SAMPLE_RATE * 1 * 2  # s16le mono


def _pcm(seconds):
    """Raw PCM bytes for `seconds` of s16le mono @ 16 kHz."""
    return b"\x00" * int(round(seconds * _BYTES_PER_SECOND))


def _utt(id, duration_ms, *, blob_seconds=None):
    """Stand-in utterance. `blob_seconds` lets the encoded length differ from
    duration_ms to exercise drift; defaults to duration_ms (no drift)."""
    seconds = (duration_ms / 1000.0) if blob_seconds is None else blob_seconds
    blob = _pcm(seconds)
    return SimpleNamespace(
        id=id,
        duration_ms=duration_ms,
        get_audio_blob=lambda blob=blob: blob,
        get_sample_rate=lambda: _SAMPLE_RATE,
    )


def _legacy_split(transcription_result, utterances, *, silence_seconds=3.0):
    """Verbatim copy of upstream split_transcription_by_utterance."""
    utterances = list(utterances)
    if not utterances:
        return {}
    language = transcription_result.get("language")
    words = transcription_result.get("words") or []

    windows = []
    t = 0.0
    for u in utterances:
        dur_s = u.duration_ms / 1000.0
        start = t
        end = start + dur_s
        windows.append((u.id, start, end))
        t = end + silence_seconds

    output = {u.id: {"transcript": "", "words": [], "language": language} for u in utterances}

    word_index = 0
    for window_index, (utterance_id, start, end) in enumerate(windows):
        utterance_words = []
        next_start = windows[window_index + 1][1] if window_index + 1 < len(windows) else None
        while word_index < len(words):
            w = words[word_index]
            if w["start"] >= end:
                break
            if w["end"] > start:
                if next_start is not None and w["end"] > next_start:
                    pass  # upstream logs + skips
                else:
                    wa = dict(w)
                    wa["start"] = wa["start"] - start
                    wa["end"] = wa["end"] - start
                    utterance_words.append(wa)
            word_index += 1
        output[utterance_id]["words"] = utterance_words
        output[utterance_id]["transcript"] = " ".join(w["word"] for w in utterance_words)
    return output


def _words(*triples):
    return [{"word": w, "start": s, "end": e} for (w, s, e) in triples]


def _all_words(result):
    return [w["word"] for utt in result.values() for w in utt["words"]]


class NormalCaseTests(unittest.TestCase):
    def test_clean_split_matches_legacy(self):
        utts = [_utt(1, 2000), _utt(2, 2000)]
        # File: u1 [0,2)  gap [2,3.5)  u2 [3.5,5.5)
        result = {"language": "de", "words": _words(("hallo", 0.5, 1.0), ("welt", 4.0, 4.5))}

        legacy = _legacy_split(result, utts, silence_seconds=1.5)
        new = split_transcription_by_utterance(result, utts, silence_seconds=1.5)

        self.assertEqual(legacy[1]["transcript"], "hallo")
        self.assertEqual(new[1]["transcript"], "hallo")
        self.assertEqual(legacy[2]["transcript"], "welt")
        self.assertEqual(new[2]["transcript"], "welt")
        self.assertAlmostEqual(new[2]["words"][0]["start"], 0.5)  # 4.0 - 3.5

    def test_empty_utterances_returns_empty(self):
        self.assertEqual(split_transcription_by_utterance({"words": []}, []), {})


class FirstWordPreservationTests(unittest.TestCase):
    """Real-data regression: the first word of an utterance, timestamped slightly
    before its window start (it bled into the leading silence gap), must be KEPT,
    not dropped — and re-based to a non-negative time."""

    def test_first_word_in_leading_gap_is_kept(self):
        utts = [_utt(1, 2000), _utt(2, 2000)]  # u1 [0,2) gap [2,3.5) u2 [3.5,5.5)
        # "Das" is u2's first word but landed at 3.3-3.5 (its midpoint 3.4 sits in
        # the gap, nearer u2). Must attach to u2, not vanish.
        result = {"language": "de", "words": _words(("hallo", 0.5, 1.0), ("Das", 3.3, 3.5), ("Fehler", 4.0, 4.6))}
        new = split_transcription_by_utterance(result, utts, silence_seconds=1.5)

        self.assertEqual(new[1]["transcript"], "hallo")
        self.assertEqual(new[2]["transcript"], "Das Fehler")
        self.assertGreaterEqual(new[2]["words"][0]["start"], 0.0)  # clamped, never negative

    def test_no_real_words_are_dropped(self):
        utts = [_utt(1, 2000), _utt(2, 2000)]
        result = {"language": "de", "words": _words(("a", 0.5, 0.8), ("b", 3.3, 3.5), ("c", 4.0, 4.2))}
        new = split_transcription_by_utterance(result, utts, silence_seconds=1.5)
        self.assertEqual(sorted(_all_words(new)), ["a", "b", "c"])

    def test_word_missing_timestamps_is_skipped(self):
        utts = [_utt(1, 2000)]
        result = {"language": "de", "words": [{"word": "a", "start": 0.5, "end": 0.8}, {"word": "x", "start": None, "end": None}]}
        new = split_transcription_by_utterance(result, utts, silence_seconds=1.5)
        self.assertEqual(_all_words(new), ["a"])


class GapWordAssignmentTests(unittest.TestCase):
    """A word in the inter-chunk gap goes to whichever utterance is nearer."""

    def test_gap_word_nearer_first_utterance(self):
        utts = [_utt(1, 2000), _utt(2, 2000)]  # gap [2.0, 3.5)
        result = {"language": "de", "words": _words(("x", 2.1, 2.3))}  # midpoint 2.2 -> u1
        new = split_transcription_by_utterance(result, utts, silence_seconds=1.5)
        self.assertEqual(new[1]["transcript"], "x")
        self.assertEqual(new[2]["transcript"], "")

    def test_gap_word_nearer_second_utterance(self):
        utts = [_utt(1, 2000), _utt(2, 2000)]  # gap [2.0, 3.5)
        result = {"language": "de", "words": _words(("x", 3.2, 3.4))}  # midpoint 3.3 -> u2
        new = split_transcription_by_utterance(result, utts, silence_seconds=1.5)
        self.assertEqual(new[1]["transcript"], "")
        self.assertEqual(new[2]["transcript"], "x")


class WindowDriftTests(unittest.TestCase):
    """duration_ms under-reports the real encoded audio length: legacy (duration_ms
    windows) drops a boundary word; the override (blob-length windows) keeps it."""

    def setUp(self):
        self.utts = [_utt(1, 2000, blob_seconds=2.05), _utt(2, 2000, blob_seconds=2.05)]
        self.result = {
            "language": "de",
            "words": _words(("hallo", 0.5, 1.0), ("ende", 2.02, 2.04), ("welt", 4.0, 4.5)),
        }

    def test_legacy_drops_boundary_word_due_to_drift(self):
        legacy = _legacy_split(self.result, self.utts, silence_seconds=1.5)
        self.assertNotIn("ende", _all_words(legacy))

    def test_override_keeps_boundary_word(self):
        new = split_transcription_by_utterance(self.result, self.utts, silence_seconds=1.5)
        self.assertEqual(new[1]["transcript"], "hallo ende")


if __name__ == "__main__":
    unittest.main()
