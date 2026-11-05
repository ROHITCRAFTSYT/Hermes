import io
import json
import wave

import httpx
import pytest
from helpers import mock_client, wav_bytes

import net
from providers.llm import LLMChain, OpenAICompatLLM, is_rate_limit
from providers.stt import GnaniPrismaSTT, STTChain
from providers.tts import GnaniTimbreTTS, canonical_wav


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net, "_sleep", lambda s: None)


# ── Prisma STT ───────────────────────────────────────────────────────────────

def test_prisma_request_shape():
    http, seen = mock_client(lambda r: httpx.Response(200, json={"transcript": " vanakkam ", "request_id": "x"}))
    stt = GnaniPrismaSTT("key-1", "https://api.vachana.ai", "ta-IN", "verbatim", http)
    assert stt.transcribe(wav_bytes(), "speech.wav") == "vanakkam"
    req = seen[0]
    assert req.method == "POST" and str(req.url) == "https://api.vachana.ai/stt/v3"
    assert req.headers["X-API-Key-ID"] == "key-1"
    body = req.content
    assert b'name="audio_file"; filename="speech.wav"' in body
    assert b'name="language_code"\r\n\r\nta-IN' in body
    assert b'name="format"\r\n\r\nverbatim' in body


def test_prisma_retries_5xx_then_succeeds():
    calls = iter([httpx.Response(503), httpx.Response(200, json={"transcript": "hi"})])
    http, seen = mock_client(lambda r: next(calls))
    stt = GnaniPrismaSTT("k", "https://api.vachana.ai", "en-IN", "verbatim", http, retries=2)
    assert stt.transcribe(wav_bytes()) == "hi"
    assert len(seen) == 2


def test_prisma_auth_error_not_retried():
    http, seen = mock_client(lambda r: httpx.Response(401, json={"error": "bad key"}))
    stt = GnaniPrismaSTT("k", "https://api.vachana.ai", "en-IN", "verbatim", http, retries=3)
    with pytest.raises(net.UpstreamError) as e:
        stt.transcribe(wav_bytes())
    assert e.value.status == 401 and len(seen) == 1


class _Fake:
    def __init__(self, name, result=None, exc=None):
        self.name, self.result, self.exc, self.calls = name, result, exc, 0

    def transcribe(self, audio, filename="speech.wav"):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def test_stt_chain_falls_back_on_error():
    gnani = _Fake("gnani", exc=net.UpstreamError("gnani-prisma", "HTTP 500", 500))
    groq = _Fake("groq", result="hello")
    assert STTChain([gnani, groq]).transcribe(b"x") == ("hello", "groq")


def test_stt_chain_empty_transcript_is_an_answer():
    gnani, groq = _Fake("gnani", result=""), _Fake("groq", result="never")
    assert STTChain([gnani, groq]).transcribe(b"x") == ("", "gnani")
    assert groq.calls == 0


def test_stt_chain_all_fail():
    chain = STTChain([_Fake("gnani", exc=RuntimeError("down"))])
    assert chain.transcribe(b"x") == ("", None)


# ── Timbre TTS ───────────────────────────────────────────────────────────────

def _timbre(handler, **kw):
    http, seen = mock_client(handler)
    opts = dict(api_key="k", base_url="https://api.vachana.ai", model="timbre-v2.0", voice="Pranav",
                language="auto", sample_rate=22050, client=http)
    opts.update(kw)
    return GnaniTimbreTTS(**opts), seen


def test_timbre_request_and_wav_normalised():
    audio = wav_bytes(rate=22050, extra_chunk=True)
    tts, seen = _timbre(lambda r: httpx.Response(200, content=audio, headers={"content-type": "audio/wav"}))
    out = tts.synthesize("Hello there")
    req = seen[0]
    assert str(req.url) == "https://api.vachana.ai/api/v1/tts/inference"
    assert req.headers["X-API-Key-ID"] == "k"
    body = json.loads(req.content)
    assert body == {"text": "Hello there", "voice": "Pranav", "model": "timbre-v2.0", "audio_config": {
        "sample_rate": 22050, "encoding": "linear_pcm", "num_channels": 1, "sample_width": 2, "container": "wav"}}
    assert out[:4] == b"RIFF" and out[36:40] == b"data"   # plain 44-byte header for the ESP32
    with wave.open(io.BytesIO(out)) as w:
        assert w.getframerate() == 22050 and w.getnchannels() == 1


def test_timbre_language_only_on_v25():
    tts, _ = _timbre(lambda r: httpx.Response(200), model="timbre-v2.5", language="ta-IN")
    assert tts.payload("x")["language"] == "ta-IN"
    tts, _ = _timbre(lambda r: httpx.Response(200), language="ta-IN")
    assert "language" not in tts.payload("x")


def test_timbre_json_error_body_raises():
    tts, _ = _timbre(lambda r: httpx.Response(200, json={"status": "error", "message": "voice not found"}))
    with pytest.raises(net.UpstreamError, match="voice not found"):
        tts.synthesize("x")


def test_canonical_wav_wraps_raw_pcm():
    out = canonical_wav(b"\x00\x01" * 100, 16000)
    with wave.open(io.BytesIO(out)) as w:
        assert w.getframerate() == 16000 and w.getnframes() == 100


# ── Evon / OpenAI-compatible LLM ─────────────────────────────────────────────

def _completion(content="", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


def test_evon_payload_and_think_stripped():
    http, seen = mock_client(lambda r: httpx.Response(200, json=_completion("<think>hmm</think>\nNamaste!")))
    evon = OpenAICompatLLM("evon", "http://gpu:8000/v1", "", "gnani/gnani-evon-v3.3-30B-A3B", 512, http)
    out = evon.chat([{"role": "user", "content": "hi"}], {})
    assert out == {"provider": "evon", "content": "Namaste!", "tool_calls": []}
    req = seen[0]
    assert str(req.url) == "http://gpu:8000/v1/chat/completions"
    assert "authorization" not in req.headers
    assert json.loads(req.content)["model"] == "gnani/gnani-evon-v3.3-30B-A3B"


def test_evon_tool_calls_parsed():
    call = {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"x"}'}}
    http, _ = mock_client(lambda r: httpx.Response(200, json=_completion(tool_calls=[call])))
    evon = OpenAICompatLLM("evon", "http://gpu:8000/v1", "secret", "m", 512, http)
    out = evon.chat([], {"tools": [], "tool_choice": "auto"})
    assert out["tool_calls"] == [{"id": "c1", "name": "web_search", "arguments": '{"query":"x"}'}]


def test_openai_compat_429_not_retried_and_flagged():
    http, seen = mock_client(lambda r: httpx.Response(429, text="slow down"))
    llm = OpenAICompatLLM("nvidia", "https://x/v1", "k", "m", 512, http, retries=3)
    with pytest.raises(net.UpstreamError) as e:
        llm.chat([], {})
    assert len(seen) == 1 and is_rate_limit(e.value)


class _FakeLLM:
    def __init__(self, name, reply=None, exc=None):
        self.name, self.reply, self.exc = name, reply, exc

    def chat(self, messages, extra):
        if self.exc:
            raise self.exc
        return {"provider": self.name, "content": self.reply, "tool_calls": []}


def test_llm_chain_falls_back():
    chain = LLMChain([_FakeLLM("evon", exc=net.UpstreamError("evon", "HTTP 502", 502)),
                      _FakeLLM("groq", reply="ok")], tools_spec=[])
    assert chain.chat([])["provider"] == "groq"


def test_llm_chain_reports_every_failure():
    chain = LLMChain([_FakeLLM("evon", exc=RuntimeError("a")), _FakeLLM("groq", exc=RuntimeError("b"))], [])
    with pytest.raises(RuntimeError, match="evon failed: a; groq failed: b"):
        chain.chat([])


def test_llm_chain_forced_tool_args():
    seen = {}

    class Spy(_FakeLLM):
        def chat(self, messages, extra):
            seen.update(extra)
            return super().chat(messages, extra)

    LLMChain([Spy("groq", reply="x")], tools_spec=[{"t": 1}]).chat([], force="web_search")
    assert seen["tool_choice"] == {"type": "function", "function": {"name": "web_search"}}
    seen.clear()
    LLMChain([Spy("groq", reply="x")], tools_spec=[{"t": 1}]).chat([], force="none")
    assert seen == {}


def test_net_gives_up_after_network_errors():
    def boom(r):
        raise httpx.ConnectError("refused")
    http, seen = mock_client(boom)
    with pytest.raises(net.UpstreamError, match="ConnectError"):
        net.request(http, "GET", "https://x", service="x", retries=2)
    assert len(seen) == 3
