"""Build the STT / LLM / TTS chains from settings."""

import logging

import net
from config import Settings
from providers.llm import GroqLLM, LLMChain, OpenAICompatLLM
from providers.stt import GnaniPrismaSTT, GroqWhisperSTT, STTChain
from providers.tts import GnaniTimbreTTS

log = logging.getLogger("hermes.providers")


class Providers:
    def __init__(self, stt: STTChain, llm: LLMChain, timbre: GnaniTimbreTTS | None):
        self.stt, self.llm, self.timbre = stt, llm, timbre


def _groq(s: Settings, retries: int):
    try:
        from groq import Groq
        return Groq(api_key=s.groq_api_key, max_retries=retries, timeout=s.http_timeout_s)
    except Exception as e:   # bad SDK install shouldn't take the Gnani path down with it
        log.error("groq client unavailable", extra={"error": str(e)})
        return None


def build(s: Settings, tools_spec: list) -> Providers:
    http = net.make_client(s.http_timeout_s)
    stt, llm = [], []

    for name in s.stt_chain():
        if name == "gnani":
            stt.append(GnaniPrismaSTT(s.gnani_api_key, s.gnani_base_url, s.gnani_stt_language,
                                      s.gnani_stt_format, http, s.http_retries))
        elif (c := _groq(s, s.http_retries)) is not None:
            stt.append(GroqWhisperSTT(c, s.stt_model))

    for name in s.llm_chain():
        if name == "groq":
            # no SDK retries: on a rate limit, moving to the next provider beats stalling the ESP32
            if (c := _groq(s, 0)) is not None:
                llm.append(GroqLLM(c, s.llm_model, s.llm_max_tokens, s.reasoning_effort))
        elif name == "nvidia":
            llm.append(OpenAICompatLLM("nvidia", s.nvidia_base_url, s.nvidia_api_key, s.nvidia_llm_model,
                                       s.llm_max_tokens, http, retries=1, reasoning_effort=s.reasoning_effort))
        elif name == "evon":
            llm.append(OpenAICompatLLM("evon", s.evon_base_url, s.evon_api_key, s.evon_model,
                                       s.llm_max_tokens, http, retries=1))

    timbre = None
    if "gnani" in s.tts_chain():
        timbre = GnaniTimbreTTS(s.gnani_api_key, s.gnani_base_url, s.gnani_tts_model, s.gnani_tts_voice,
                                s.gnani_tts_language, s.gnani_tts_sample_rate, http, retries=1)

    return Providers(STTChain(stt), LLMChain(llm, tools_spec, s.llm_rate_limit_retries), timbre)
