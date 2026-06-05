"""Phase 0 reproduction + override verification for utterance-splitting.

Django-free on purpose: these run with bare ``python -m unittest`` (and under the
repo's ``pytest``) without Postgres, Django settings, or the transcription
service. They feed the split helper a hand-built service response — the same
shape ``whisperx_group_client._parse_done_response`` produces — and assert how
words land on utterances.

``_legacy_split`` is a faithful copy of the current upstream
``bots.transcription_utils.split_transcription_by_utterance`` (the
"first overlapping window, then break" loop with duration_ms windows). It is the
regression witness: each test shows the legacy behaviour AND the hardened
``transcription_extras.split`` behaviour side by side.
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
    """Verbatim copy of upstream split_transcription_by_utterance (the buggy one)."""
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


class NormalCaseTests(unittest.TestCase):
    """Clean words inside their windows: legacy and override must agree."""

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
        # Re-based to each utterance's own start.
        self.assertAlmostEqual(new[2]["words"][0]["start"], 0.5)  # 4.0 - 3.5

    def test_empty_utterances_returns_empty(self):
        self.assertEqual(split_transcription_by_utterance({"words": []}, []), {})


class GapHallucinationTests(unittest.TestCase):
    """Defect 1: a phantom word in the inter-chunk silence gap."""

    def setUp(self):
        self.utts = [_utt(1, 2000), _utt(2, 2000)]  # u1 [0,2)  gap [2,3.5)  u2 [3.5,5.5)
        # "danke" sits in the gap and bleeds 0.1s past u2's start (3.5).
        self.result = {
            "language": "de",
            "words": _words(("hallo", 0.5, 1.0), ("danke", 3.3, 3.6), ("welt", 4.0, 4.5)),
        }

    def test_legacy_leaks_phantom_onto_next_utterance(self):
        legacy = _legacy_split(self.result, self.utts, silence_seconds=1.5)
        # Upstream prepends the gap word to u2 — with a NEGATIVE re-based start.
        self.assertEqual(legacy[2]["transcript"], "danke welt")
        self.assertLess(legacy[2]["words"][0]["start"], 0.0)

    def test_override_drops_phantom(self):
        new = split_transcription_by_utterance(self.result, self.utts, silence_seconds=1.5)
        self.assertEqual(new[1]["transcript"], "hallo")
        self.assertEqual(new[2]["transcript"], "welt")
        all_words = [w["word"] for u in new.values() for w in u["words"]]
        self.assertNotIn("danke", all_words)


class WindowDriftTests(unittest.TestCase):
    """Defect 2: duration_ms under-reports the real encoded audio length."""

    def setUp(self):
        # duration_ms says 2.0s, but the PCM blob is actually 2.05s.
        self.utts = [_utt(1, 2000, blob_seconds=2.05), _utt(2, 2000, blob_seconds=2.05)]
        # "ende" is real audio inside u1 (2.02-2.04s < 2.05) but PAST the 2.0s
        # duration_ms window — and far before any duration_ms-based u2 window.
        self.result = {
            "language": "de",
            "words": _words(("hallo", 0.5, 1.0), ("ende", 2.02, 2.04), ("welt", 4.0, 4.5)),
        }

    def test_legacy_drops_boundary_word_due_to_drift(self):
        legacy = _legacy_split(self.result, self.utts, silence_seconds=1.5)
        all_words = [w["word"] for u in legacy.values() for w in u["words"]]
        self.assertNotIn("ende", all_words)  # silently lost

    def test_override_keeps_boundary_word(self):
        new = split_transcription_by_utterance(self.result, self.utts, silence_seconds=1.5)
        self.assertEqual(new[1]["transcript"], "hallo ende")


if __name__ == "__main__":
    unittest.main()
