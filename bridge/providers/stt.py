"""Speech-to-text providers: Gnani Prisma and Groq Whisper, tried in order."""

import logging
import re

import httpx

import net

log = logging.getLogger("hermes.stt")

_MIME = {"wav": "audio/wav", "webm": "audio/webm", "ogg": "audio/ogg", "m4a": "audio/mp4", "mp3": "audio/mpeg"}


class GnaniPrismaSTT:
    """Prisma REST recognition: POST {base}/stt/v3, multipart, header X-API-Key-ID.

    Field names (audio_file, language_code, format) and the `transcript` response key follow
    Gnani's docs (docs.gnani.ai/api/STT) as used by livekit-plugins-gnani 1.8.10. That client only
    ever sends WAV; webm/ogg from the browser app is unverified and falls back to Whisper if refused.
    """

    name = "gnani"

    def __init__(self, api_key: str, base_url: str, language: str, fmt: str,
                 client: httpx.Client, retries: int = 2):
        self.api_key, self.base_url, self.language, self.fmt = api_key, base_url, language, fmt
        self.client, self.retries = client, retries

    def transcribe(self, audio: bytes, filename: str = "speech.wav") -> str:
        ext = filename.rsplit(".", 1)[-1].lower()
        r = net.request(
            self.client, "POST", f"{self.base_url}/stt/v3", service="gnani-prisma", retries=self.retries,
            headers={"X-API-Key-ID": self.api_key},
            files={"audio_file": (filename, audio, _MIME.get(ext, "application/octet-stream"))},
            data={"language_code": self.language, "format": self.fmt},
        )
        try:
            body = r.json()
        except ValueError as e:
            raise net.UpstreamError("gnani-prisma", f"non-JSON response: {r.text[:200]}") from e
        return (body.get("transcript") or "").strip()


class GroqWhisperSTT:
    name = "groq"

    def __init__(self, client, model: str):
        self.client, self.model = client, model

    def _once(self, audio: bytes, filename: str, **kw) -> str:
        result = self.client.audio.transcriptions.create(
            file=(filename, audio, "application/octet-stream"), model=self.model, **kw)
        return (result.text or "").strip()

    def transcribe(self, audio: bytes, filename: str = "speech.wav") -> str:
        text = self._once(audio, filename)
        # Whisper guesses the language per clip and sometimes hears short English as Hindi etc.
        # Hermes speaks English and Tamil, so anything with neither script is re-run as English.
        if text and not re.search(r"[A-Za-z஀-௿]", text):
            log.info("whisper heard neither English nor Tamil, retrying as English", extra={"heard": text})
            text = self._once(audio, filename, language="en") or text
        return text


class STTChain:
    def __init__(self, providers: list):
        self.providers = providers

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    def transcribe(self, audio: bytes, filename: str = "speech.wav") -> tuple[str, str | None]:
        """Return (text, provider name). An empty transcript is a valid answer (silence), not a failure."""
        for p in self.providers:
            try:
                return p.transcribe(audio, filename), p.name
            except Exception as e:
                log.warning("stt provider failed", extra={"provider": p.name, "error": str(e)[:300]})
        return "", None
