"""Gnani Timbre text-to-speech (beta). Edge / gTTS / SAPI stay in hermes_bridge.py."""

import io
import logging
import wave

import httpx

import net

log = logging.getLogger("hermes.tts")


def canonical_wav(data: bytes, sample_rate: int) -> bytes:
    """Rewrite audio as a plain 44-byte-header PCM16 mono WAV.

    The ESP32 firmware reads a fixed 44-byte header, so extra chunks (LIST, fact) would be
    played as noise. Raw PCM (no RIFF header) is wrapped as-is at `sample_rate`.
    """
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != 2:
                raise ValueError(f"expected 16-bit PCM, got {8 * w.getsampwidth()}-bit")
            channels, rate, frames = w.getnchannels(), w.getframerate(), w.readframes(w.getnframes())
        if channels == 2:   # keep the left channel
            frames = b"".join(frames[i:i + 2] for i in range(0, len(frames), 4))
    else:
        rate, frames = sample_rate, data
    if not frames:
        raise ValueError("no audio frames")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


class GnaniTimbreTTS:
    """Timbre REST inference: POST {base}/api/v1/tts/inference, JSON body, raw audio bytes back.

    Body shape (text, voice, model, audio_config{...}, language for timbre-v2.5 only) follows
    Gnani's docs (docs.gnani.ai/api/TTS/tts-inference) as used by livekit-plugins-gnani 1.8.10.
    Voices documented for timbre-v2.0: Pranav, Kaveri, Shubhra, Deepak.
    """

    name = "gnani"

    def __init__(self, api_key: str, base_url: str, model: str, voice: str, language: str,
                 sample_rate: int, client: httpx.Client, retries: int = 1):
        self.api_key, self.base_url, self.model, self.voice = api_key, base_url, model, voice
        self.language, self.sample_rate, self.client, self.retries = language, sample_rate, client, retries

    def payload(self, text: str) -> dict:
        body = {"text": text, "voice": self.voice, "model": self.model, "audio_config": {
            "sample_rate": self.sample_rate, "encoding": "linear_pcm", "num_channels": 1,
            "sample_width": 2, "container": "wav"}}
        if self.model == "timbre-v2.5" and self.language:
            body["language"] = self.language
        return body

    def synthesize(self, text: str) -> bytes:
        r = net.request(self.client, "POST", f"{self.base_url}/api/v1/tts/inference",
                        service="gnani-timbre", retries=self.retries,
                        headers={"X-API-Key-ID": self.api_key, "Content-Type": "application/json"},
                        json=self.payload(text))
        if "json" in r.headers.get("content-type", ""):
            raise net.UpstreamError("gnani-timbre", f"expected audio, got JSON: {r.text[:200]}")
        try:
            return canonical_wav(r.content, self.sample_rate)
        except (ValueError, wave.Error, EOFError) as e:
            raise net.UpstreamError("gnani-timbre", f"unreadable audio: {e}") from e
