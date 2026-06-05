"""HTTP client for sending a combined MP3 to the custom-async transcription service.

Mirrors the single-chunk request/parse in ``bots.tasks.process_utterance_task`` but
operates on pre-combined MP3 bytes. The request shape and response contract are
intentionally duplicated here (a few lines) so the upstream module stays untouched.

Response contract (``POST /attendee/transcribe``):
    {"status": "done",
     "result": {"transcription": {"full_transcript": str,
                                  "utterances": [{"words": [{"word","start","end"}]}]}}}
Word ``start``/``end`` are floats in SECONDS, relative to the uploaded file.
"""

import json
import logging
import time

import requests

from bots.models import TranscriptionFailureReasons, TranscriptionProviders

from . import config

logger = logging.getLogger(__name__)


def build_request_params(utterances):
    """Return (headers, form-data) for the group's provider, matching the v1/v2 contract.

    All utterances in a group share one AsyncTranscription, so the settings are read
    from the first utterance.
    """
    provider = utterances[0].transcription_provider
    transcription_settings = utterances[0].transcription_settings
    if provider == TranscriptionProviders.CUSTOM_ASYNC_V2:
        headers = transcription_settings.custom_async_v2_headers()
        data = _serialize_form_data(transcription_settings.custom_async_v2_form_data())
    else:
        headers = {}
        data = _serialize_form_data(transcription_settings.custom_async_additional_props())
    return headers, data


def transcribe_combined_mp3(mp3_bytes, *, headers, data, identifier):
    """POST a combined MP3 and return (transcription_result, failure_data).

    ``transcription_result`` is shaped for ``split_transcription_by_utterance``:
    ``{"transcript": str, "words": [{"word","start","end",...}], "language": str|None}``
    where word start/end are seconds relative to THIS uploaded file.
    """
    base_url = config.transcription_url()
    if not base_url:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": f"{config.TRANSCRIPTION_URL_ENV} environment variable not set"}

    files = {"audio": ("audio.mp3", mp3_bytes, "audio/mpeg")}
    timeout = config.request_timeout_seconds()

    try:
        logger.info("transcription_extras: POST %s for %s (%dB, timeout=%ds)", base_url, identifier, len(mp3_bytes), timeout)
        started_at = time.monotonic()
        response = requests.post(base_url, files=files, data=data or None, headers=headers or None, timeout=timeout)
        elapsed = time.monotonic() - started_at
        logger.info("transcription_extras: response %d for %s in %.2fs", response.status_code, identifier, elapsed)

        if response.status_code == 401:
            return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}
        if response.status_code == 429:
            return None, {"reason": TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED, "status_code": response.status_code}
        if response.status_code != 200:
            logger.warning("transcription_extras: non-200 (%d) for %s: %s", response.status_code, identifier, response.text[:500])
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "response_text": response.text}

        result_data = response.json()
    except requests.exceptions.Timeout:
        logger.warning("transcription_extras: request timed out after %ds for %s", timeout, identifier)
        return None, {"reason": TranscriptionFailureReasons.TIMED_OUT, "timeout": timeout}
    except requests.exceptions.RequestException as e:
        logger.warning("transcription_extras: request error for %s: %s", identifier, e)
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": str(e)}
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("transcription_extras: invalid JSON for %s: %s", identifier, e)
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": f"Invalid JSON response: {str(e)}"}

    status = result_data.get("status")
    if status == "done":
        parsed = _parse_done_response(result_data)
        logger.info("transcription_extras: done for %s — %d word(s), transcript len=%d", identifier, len(parsed["words"]), len(parsed["transcript"]))
        return parsed, None
    if status == "error":
        logger.warning("transcription_extras: service reported error for %s: %s", identifier, result_data.get("error_code"))
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "error_code": result_data.get("error_code")}
    logger.warning("transcription_extras: unexpected status %r for %s", status, identifier)
    return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "status": status}


def _parse_done_response(result_data):
    transcription = result_data.get("result", {}).get("transcription", {}) or {}
    words = []
    for utt in transcription.get("utterances", []) or []:
        words.extend(utt.get("words", []) or [])
    return {"transcript": transcription.get("full_transcript", ""), "words": words, "language": transcription.get("language")}


def _serialize_form_data(form_data):
    return {key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in (form_data or {}).items()}
