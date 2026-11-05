"""Typed settings for the bridge, read from the environment (and bridge/.env).

Bad values fail at startup with a readable message instead of surfacing as a
crash halfway through a voice request. Missing API keys are not errors: the
provider is just skipped and the next one in its chain is used.
"""

import re
import sys
from typing import Literal

from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LANG = re.compile(r"^[a-z]{2}-IN(,[a-z]{2}-IN)?$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True, populate_by_name=True)

    # server
    port: int = Field(8787, ge=1, le=65535)
    assistant_name: str = "Hermes"
    hermes_token: str = ""
    play_on_pc: bool = sys.platform == "win32"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"
    rate_limit_per_min: int = Field(30, ge=0, description="per client IP on /voice /chat /talk; 0 = off")
    trust_proxy_headers: bool = False
    max_audio_bytes: int = Field(10 * 1024 * 1024, gt=0)
    max_text_chars: int = Field(2000, gt=0)

    # provider selection: the first one, then the rest of the chain as fallback
    stt_provider: Literal["groq", "gnani"] = "groq"
    llm_provider: Literal["groq", "nvidia", "evon"] = "groq"
    tts_provider: Literal["edge", "gnani", "sapi"] = Field(
        "edge", validation_alias=AliasChoices("tts_provider", "tts_engine"))

    # outbound HTTP
    http_timeout_s: float = Field(20.0, gt=0, le=120)
    http_retries: int = Field(2, ge=0, le=5)

    # Groq (Whisper STT + chat)
    groq_api_key: str = ""
    stt_model: str = "whisper-large-v3-turbo"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_max_tokens: int = Field(1024, ge=64, le=32768)
    reasoning_effort: Literal["low", "medium", "high"] | None = "low"
    llm_rate_limit_retries: int = Field(2, ge=0, le=5)
    agent_deadline_s: float = Field(12.0, gt=0, le=60)

    # NVIDIA NIM (OpenAI-compatible)
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_llm_model: str = "nvidia/nemotron-3-super-120b-a12b"

    # Gnani Vachana platform: Prisma STT + Timbre TTS
    gnani_api_key: str = ""
    gnani_base_url: str = "https://api.vachana.ai"
    gnani_stt_language: str = "en-IN"
    gnani_stt_format: Literal["verbatim", "transcribe"] = "verbatim"
    gnani_tts_model: Literal["timbre-v2.0", "timbre-v2.5"] = "timbre-v2.0"
    gnani_tts_voice: str = "Pranav"
    gnani_tts_language: str = "auto"
    gnani_tts_sample_rate: Literal[8000, 16000, 22050, 44100] = 22050

    # Gnani Evon, served behind any OpenAI-compatible endpoint (vLLM, SGLang, a host)
    evon_base_url: str = ""
    evon_api_key: str = ""
    evon_model: str = "gnani/gnani-evon-v3.3-30B-A3B"

    # Edge neural voices
    tts_voice: str = "en-US-AvaMultilingualNeural"
    tts_voice_ta: str = "ta-IN-PallaviNeural"
    tts_rate: str = "+0%"
    tts_hedge_s: float = Field(2.5, gt=0)
    tts_total_s: float = Field(8.0, gt=0)

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _blank_effort(cls, v):
        return None if v in ("", "none", "off") else v

    @field_validator("nvidia_base_url", "gnani_base_url", "evon_base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("must be an http(s):// URL")
        return v

    @field_validator("gnani_stt_language")
    @classmethod
    def _stt_lang(cls, v: str) -> str:
        v = v.replace(" ", "")
        if not _LANG.match(v):
            raise ValueError("expected a code like en-IN, ta-IN or the pair en-IN,hi-IN")
        return v

    @field_validator("gnani_tts_language")
    @classmethod
    def _tts_lang(cls, v: str) -> str:
        if v != "auto" and not _LANG.match(v):
            raise ValueError("expected 'auto' or a code like hi-IN")
        return v

    @field_validator("hermes_token", "groq_api_key", "nvidia_api_key", "gnani_api_key", "evon_api_key")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    # chains: what actually runs, in order, given which keys are present
    def stt_chain(self) -> list[str]:
        have = {"groq": bool(self.groq_api_key), "gnani": bool(self.gnani_api_key)}
        order = [self.stt_provider] + [p for p in ("gnani", "groq") if p != self.stt_provider]
        return [p for p in order if have[p]]

    def llm_chain(self) -> list[str]:
        have = {"groq": bool(self.groq_api_key), "nvidia": bool(self.nvidia_api_key),
                "evon": bool(self.evon_base_url)}
        order = [self.llm_provider] + [p for p in ("groq", "nvidia", "evon") if p != self.llm_provider]
        return [p for p in order if have[p]]

    def tts_chain(self) -> list[str]:
        if self.tts_provider == "sapi":
            return ["gtts", "sapi"]   # gTTS only for Tamil, which the Windows voice can't speak
        chain = ["edge", "gtts", "sapi"]
        if self.gnani_api_key:
            chain.insert(0 if self.tts_provider == "gnani" else 1, "gnani")
        return chain

    def warnings(self) -> list[str]:
        out = []
        if self.stt_provider == "gnani" and not self.gnani_api_key:
            out.append("STT_PROVIDER=gnani but GNANI_API_KEY is empty; using Groq Whisper")
        if self.tts_provider == "gnani" and not self.gnani_api_key:
            out.append("TTS_PROVIDER=gnani but GNANI_API_KEY is empty; using Edge voices")
        if self.llm_provider == "evon" and not self.evon_base_url:
            out.append("LLM_PROVIDER=evon but EVON_BASE_URL is empty; using Groq/NVIDIA")
        if not self.stt_chain():
            out.append("no speech-to-text key set (GROQ_API_KEY or GNANI_API_KEY); voice input won't work")
        if not self.llm_chain():
            out.append("no LLM configured (GROQ_API_KEY, NVIDIA_API_KEY or EVON_BASE_URL)")
        if not self.hermes_token:
            out.append("HERMES_TOKEN is empty: fine on a home LAN, never expose the bridge like this")
        return out


def load_settings(**overrides) -> Settings:
    try:
        return Settings(**overrides)
    except ValidationError as e:
        lines = [f"  {'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}" for err in e.errors()]
        raise SystemExit("Invalid Hermes configuration (check bridge/.env):\n" + "\n".join(lines)) from None
