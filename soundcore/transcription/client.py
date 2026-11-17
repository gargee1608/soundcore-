"""Boondock API v3 client.

File upload: POST {base}/api/v3/transcribe/openai
    multipart part audio_file, header X-Boondock-Key
    success body {"transcription": "..."}

Oversized clips: POST {base}/api/v3/s3/transcribe/openai
    JSON {"s3_url": "https://..."}
"""

from __future__ import annotations

import requests

from soundcore.transcription.base import TranscriptionError, classify_http


class BoondockClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        model: str = "",
        http=None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.model = model or ""
        self.http = http or requests.Session()

    def transcribe_file(self, path: str, *, language: str = "") -> str:
        url = f"{self.base_url}/api/v3/transcribe/openai"
        data = {}
        if language:
            data["language"] = language
        if self.model:
            data["model"] = self.model
        handle = open(path, "rb")
        try:
            filename = path.rsplit("/", 1)[-1] or "audio.wav"
            try:
                response = self.http.post(
                    url,
                    headers={"X-Boondock-Key": self.api_key},
                    files={"audio_file": (filename, handle, "audio/wav")},
                    data=data,
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                raise TranscriptionError("Boondock request timed out", retryable=True, kind="retry") from exc
            except requests.RequestException as exc:
                raise TranscriptionError(str(exc), retryable=True, kind="retry") from exc
        finally:
            handle.close()
        return self._parse(response)

    def transcribe_s3_url(self, s3_url: str, *, language: str = "") -> str:
        url = f"{self.base_url}/api/v3/s3/transcribe/openai"
        payload = {"s3_url": s3_url}
        if language:
            payload["language"] = language
        if self.model:
            payload["model"] = self.model
        try:
            response = self.http.post(
                url,
                headers={"X-Boondock-Key": self.api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise TranscriptionError("Boondock S3 request timed out", retryable=True, kind="retry") from exc
        except requests.RequestException as exc:
            raise TranscriptionError(str(exc), retryable=True, kind="retry") from exc
        return self._parse(response)

    def _parse(self, response) -> str:
        kind = classify_http(response.status_code)
        if kind == "ok":
            try:
                body = response.json()
            except Exception as exc:
                raise TranscriptionError("Boondock returned non-JSON", retryable=True, kind="retry") from exc
            text = body.get("transcription") if isinstance(body, dict) else None
            if not isinstance(text, str):
                raise TranscriptionError("Boondock response missing transcription", retryable=True, kind="retry")
            return text
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("error") or "")
        except Exception:
            detail = (response.text or "")[:300]
        retryable = kind == "retry"
        raise TranscriptionError(
            detail or f"HTTP {response.status_code}",
            retryable=retryable,
            status_code=response.status_code,
            kind=kind,
        )


class BoondockEngine:
    """Adapter so other engines can be registered beside Boondock later."""

    name = "boondock"

    def __init__(self, client: BoondockClient):
        self.client = client

    def transcribe_file(self, path: str, *, language: str = "") -> str:
        return self.client.transcribe_file(path, language=language)

    def transcribe_s3_url(self, s3_url: str, *, language: str = "") -> str:
        return self.client.transcribe_s3_url(s3_url, language=language)


def build_engine(settings) -> BoondockEngine:
    client = BoondockClient(
        settings.boondock_base_url,
        settings.boondock_api_key,
        timeout=settings.boondock_timeout,
        model=settings.boondock_model,
    )
    return BoondockEngine(client)
