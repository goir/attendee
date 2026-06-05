"""Runtime configuration for combined custom-async transcription.

Values are read from the environment at call time (not import time) so they can be
overridden per-process and in tests. Nothing here imports Django models, so the
module stays cheap and side-effect free.
"""

import os

# Reused from the existing single-chunk custom-async path, so a deployment that
# already points Attendee at its transcription service needs no new URL.
TRANSCRIPTION_URL_ENV = "CUSTOM_ASYNC_TRANSCRIPTION_URL"

# ~30 MB keeps a single combined upload under Cloud Run's 32 MiB request cap.
DEFAULT_MAX_UPLOAD_BYTES = 30 * 1024 * 1024
DEFAULT_MP3_BITRATE_KBPS = 128
# Silence inserted between concatenated chunks. Must exceed the service VAD's
# min-silence (500 ms) so each chunk is a clean boundary; below the 2 s
# hallucination threshold to avoid provoking phantom phrases on dead air.
DEFAULT_COMBINE_SILENCE_SECONDS = 1.5
DEFAULT_UPLOAD_SAFETY_MARGIN = 0.9
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120


def transcription_url():
    return os.getenv(TRANSCRIPTION_URL_ENV)


def max_upload_bytes():
    return _int_env("CUSTOM_ASYNC_TRANSCRIPTION_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)


def mp3_bitrate_kbps():
    return _int_env("CUSTOM_ASYNC_TRANSCRIPTION_MP3_BITRATE_KBPS", DEFAULT_MP3_BITRATE_KBPS)


def combine_silence_seconds():
    return _float_env("CUSTOM_ASYNC_TRANSCRIPTION_COMBINE_SILENCE_SECONDS", DEFAULT_COMBINE_SILENCE_SECONDS)


def upload_safety_margin():
    return _float_env("CUSTOM_ASYNC_TRANSCRIPTION_UPLOAD_SAFETY_MARGIN", DEFAULT_UPLOAD_SAFETY_MARGIN)


def request_timeout_seconds():
    return _int_env("CUSTOM_ASYNC_TRANSCRIPTION_TIMEOUT", DEFAULT_REQUEST_TIMEOUT_SECONDS)


def effective_max_upload_bytes():
    """The byte budget for one combined file, after applying the safety margin."""
    return int(max_upload_bytes() * upload_safety_margin())


def _int_env(name, default):
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name, default):
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default
