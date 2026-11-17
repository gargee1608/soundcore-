"""Engine-neutral transcription errors and retry classification."""

from __future__ import annotations

from typing import Protocol


class TranscriptionError(Exception):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None, kind: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.kind = kind or ("retry" if retryable else "fatal")


class TranscriptionEngine(Protocol):
    name: str

    def transcribe_file(self, path: str, *, language: str = "") -> str: ...

    def transcribe_s3_url(self, s3_url: str, *, language: str = "") -> str: ...


def classify_http(status_code: int | None, *, timed_out: bool = False) -> str:
    """Return ok, retry, fatal, or too_large.

    500s and timeouts retry. 403 and 413 do not: the same key or the same
    oversized body will fail again. 413 is handled once via the S3 URL path.
    """
    if timed_out or status_code is None:
        return "retry"
    if status_code == 200:
        return "ok"
    if status_code == 403:
        return "fatal"
    if status_code == 413:
        return "too_large"
    if status_code == 408 or status_code == 429 or status_code >= 500:
        return "retry"
    return "fatal"


def next_backoff_seconds(attempt: int) -> int:
    """attempt is the failure count, starting at 1. Caps at 15 minutes."""
    if attempt < 1:
        attempt = 1
    return min(30 * (2 ** (attempt - 1)), 900)
