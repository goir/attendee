# transcription_extras

Per-speaker, size-capped **chunk combining** for custom-async transcription.

Custom-async transcription used to send **one HTTP request per audio chunk**, which means a
Cloud Run GPU **cold-start per chunk**. This app combines each speaker's chunks into larger MP3
files (capped at a configurable upload size) so the bot makes far fewer, larger requests — while
keeping the original Attendee codebase nearly untouched (see [`../FORK_CHANGES.md`](../FORK_CHANGES.md)).

## How it works

For `CUSTOM_ASYNC` / `CUSTOM_ASYNC_V2` providers, the async group task calls
`get_transcription_for_utterance_group(utterances)`, which:

1. **Groups by speaker** (`participant_id`) — speaker is known, so no diarization is needed.
2. **Size-caps** each speaker's chunks into sub-groups whose estimated combined MP3 ≤ the cap.
3. **Combines** each sub-group into one MP3 via Attendee's `get_mp3_for_utterance_group()`
   (raw PCM concatenated with a fixed silence gap between chunks).
4. **Sends** the MP3 to our WhisperX `POST /attendee/transcribe`.
5. **Splits** the response back onto the individual utterances via Attendee's
   `split_transcription_by_utterance()`.

### The one correctness rule

> **one combined MP3  ↔  one ordered utterance sub-group  ↔  one `split_transcription_by_utterance()` call.**

Each combined file is transcribed independently, so the service returns word times **starting at
0 for that file**. We therefore split every file on **its own** utterance subset, in concatenation
order, using the **same silence gap** that built it. We never share a window list across files and
never treat the times as a global meeting timeline. Stored word times stay **utterance-relative**
(0-based per utterance) — exactly like the single-chunk path; the meeting-global position of each
utterance already lives in `Utterance.timestamp_ms`.

## The silence gap

A gap between concatenated chunks gives WhisperX a clean segment boundary and a "dead zone" that
absorbs timestamp drift so a boundary word can't be mis-assigned. It must exceed the service VAD's
min-silence (**500 ms**) to register as a boundary, and stay under the **2 s** hallucination
threshold (long dead air makes Whisper invent phrases). Default **1.5 s**, configurable.

## Files

| File | Purpose |
|------|---------|
| `config.py` | Env-driven settings (read at call time, so tests can override). |
| `grouping.py` | Pure helpers: `group_by_speaker`, `split_into_size_capped_groups`, `estimate_mp3_bytes`. No DB imports. |
| `whisperx_group_client.py` | Build v1/v2 request params; POST a combined MP3; parse the response. |
| `group_transcription.py` | `get_transcription_for_utterance_group()` — orchestration + logging. |
| `transcript_export.py` | Assemble an AsyncTranscription's utterances into readable text / JSON. |
| `admin.py` | Custom AsyncTranscription admin: transcript preview + `.txt`/`.json` downloads. |
| `tests.py` | DB-free `SimpleTestCase`s: grouping, size cap, parsing, per-file split, transcript export. |

## Admin: view / download the transcription result

AsyncTranscription has no hand-written admin upstream, so `admin_extras` auto-registers a generic
read-only admin for it. `admin.py` here **swaps that out** (unregister + register) for a richer one:

- an inline **transcript preview** (`[HH:MM:SS] Speaker: text`, ordered by `timestamp_ms`,
  truncated for very long meetings), and
- **Download .txt / .json** buttons (a custom admin view at
  `<id>/download/<txt|json>/`; JSON includes per-word timings).

The transcript is assembled on the fly from the linked `Utterance.transcription` rows, so it
reflects whatever has been transcribed so far. Kept here so `bots/admin.py` stays untouched.

## Configuration (env vars, all optional)

| Var | Default | Meaning |
|-----|---------|---------|
| `CUSTOM_ASYNC_TRANSCRIPTION_URL` | — | Service endpoint (reused from the single-chunk path). Required. |
| `CUSTOM_ASYNC_TRANSCRIPTION_MAX_UPLOAD_BYTES` | `31457280` (30 MB) | Hard cap per combined upload. |
| `CUSTOM_ASYNC_TRANSCRIPTION_UPLOAD_SAFETY_MARGIN` | `0.9` | Effective cap = max × margin. |
| `CUSTOM_ASYNC_TRANSCRIPTION_MP3_BITRATE_KBPS` | `128` | MP3 bitrate; shared by the encoder and the size estimator so they agree. |
| `CUSTOM_ASYNC_TRANSCRIPTION_COMBINE_SILENCE_SECONDS` | `1.5` | Silence inserted between chunks. |
| `CUSTOM_ASYNC_TRANSCRIPTION_TIMEOUT` | `120` | Per-request timeout (seconds). |

## Integration with Attendee (two tiny upstream hooks)

1. `bots/models.py` — `AsyncTranscription.use_grouped_utterances` returns `True` for the custom
   providers (so they take the grouped path instead of one-chunk-per-request).
2. `bots/tasks/process_utterance_group_for_async_transcription_task.py` — `get_transcription()`
   delegates custom providers to this app via a local import.

Everything else lives here. The single-chunk custom path in `bots/tasks/process_utterance_task.py`
is left intact (just unused for async once grouping is on).

## Logging

All log lines are prefixed `transcription_extras:` and emitted at INFO for the happy path
(speaker→file packing, MP3 size, request status + latency, word counts, per-utterance split
breakdown, completion summary) and WARNING for failures. Grep `transcription_extras` in the bot
worker logs to trace a recording end-to-end.

## Tests

```bash
python manage.py test transcription_extras   # DB-free SimpleTestCases
python manage.py check                        # app loads + hooks import-clean
```
