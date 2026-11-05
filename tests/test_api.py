import base64

from conftest import AUTH
from helpers import wav_bytes

from providers.llm import LLMChain
from providers.stt import STTChain
from ratelimit import RateLimiter


class FakeSTT:
    name = "gnani"

    def __init__(self, text="what time is it", fail=False):
        self.text, self.fail = text, fail

    def transcribe(self, audio, filename="speech.wav"):
        if self.fail:
            raise RuntimeError("prisma down")
        return self.text


class FakeLLM:
    name = "evon"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, extra):
        self.calls.append((messages, extra))
        return self.replies.pop(0)


def _use(bridge, monkeypatch, stt=None, llm=None):
    if stt is not None:
        monkeypatch.setattr(bridge.P, "stt", STTChain(stt))
    if llm is not None:
        monkeypatch.setattr(bridge.P, "llm", LLMChain(llm, tools_spec=[]))
    monkeypatch.setattr(bridge, "render_speech", lambda text: (None, wav_bytes()))


def test_health_is_public_and_hides_secrets(client):
    r = client.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["auth"] is True and "providers" in j
    assert "test-token" not in r.text
    assert r.headers["X-Request-ID"]


def test_post_endpoints_need_token(client):
    for path in ("/chat", "/voice", "/talk"):
        r = client.post(path, content=b"{}")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == 401
    r = client.post("/chat", json={"text": "x"}, headers={"X-Hermes-Token": "wrong"})
    assert r.status_code == 401


def test_rate_limit_applies_before_auth(bridge, client, monkeypatch):
    monkeypatch.setattr(bridge, "_LIMITER", RateLimiter(2))
    codes = [client.post("/chat", json={"text": "x"}).status_code for _ in range(3)]
    assert codes == [401, 401, 429]
    r = client.post("/chat", json={"text": "x"}, headers=AUTH)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_chat_runs_agent_with_mocked_llm(bridge, client, monkeypatch):
    llm = FakeLLM([{"provider": "evon", "content": "Hello from Evon.", "tool_calls": []}])
    _use(bridge, monkeypatch, llm=[llm])
    r = client.post("/chat", json={"text": "say hi"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"reply": "Hello from Evon."}
    assert bridge.CONVO[-1] == {"role": "assistant", "content": "Hello from Evon."}


def test_agent_runs_tools(bridge, client, monkeypatch):
    call = {"id": "c1", "name": "remember", "arguments": '{"operation": "list"}'}
    llm = FakeLLM([{"provider": "evon", "content": "", "tool_calls": [call]},
                   {"provider": "evon", "content": "Done.", "tool_calls": []}])
    _use(bridge, monkeypatch, llm=[llm])
    monkeypatch.setattr(bridge.T, "dispatch", lambda name, args: f"ran {name}")
    r = client.post("/chat", json={"text": "list what you remember about me"}, headers=AUTH)
    assert r.json()["reply"] == "Done."
    second_messages = llm.calls[1][0]
    assert second_messages[-1] == {"role": "tool", "tool_call_id": "c1", "name": "remember", "content": "ran remember"}


def test_chat_validation(client):
    assert client.post("/chat", content=b"not json", headers=AUTH).status_code == 400
    assert client.post("/chat", json=["x"], headers=AUTH).status_code == 400
    assert client.post("/chat", json={"text": "x" * 5000}, headers=AUTH).status_code == 413


def test_chat_without_llm_explains(bridge, client, monkeypatch):
    _use(bridge, monkeypatch, llm=[])
    r = client.post("/chat", json={"text": "hello"}, headers=AUTH)
    assert "isn't connected" in r.json()["reply"]


def test_voice_round_trip(bridge, client, monkeypatch):
    llm = FakeLLM([{"provider": "evon", "content": "It is noon.", "tool_calls": []}])
    _use(bridge, monkeypatch, stt=[FakeSTT()], llm=[llm])
    r = client.post("/voice", content=wav_bytes(), headers={**AUTH, "Content-Type": "audio/wav"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"


def test_talk_when_stt_is_down_says_so(bridge, client, monkeypatch):
    llm = FakeLLM([])
    _use(bridge, monkeypatch, stt=[FakeSTT(fail=True)], llm=[llm])
    r = client.post("/talk", content=wav_bytes(), headers={**AUTH, "Content-Type": "audio/webm"})
    j = r.json()
    assert r.status_code == 200 and j["reply"] == bridge.STT_UNAVAILABLE and j["heard"] == ""
    assert base64.b64decode(j["audio_b64"])[:4] == b"RIFF"
    assert llm.calls == []


def test_audio_limits(bridge, client, monkeypatch):
    monkeypatch.setattr(bridge.S, "max_audio_bytes", 100)
    assert client.post("/voice", content=b"x" * 101, headers=AUTH).status_code == 413
    assert client.post("/voice", content=b"", headers=AUTH).status_code == 400


def test_unhandled_error_is_clean_500(bridge, client, monkeypatch):
    def boom(*a):
        raise ValueError("secret internals")
    monkeypatch.setattr(bridge, "_hear_and_reply", boom)
    r = client.post("/voice", content=wav_bytes(), headers=AUTH)
    assert r.status_code == 500
    assert "secret internals" not in r.text and r.json()["error"]["message"] == "Internal error"


def test_request_id_echoed(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"
    r = client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert r.headers["X-Request-ID"] != "bad id\nwith newline"


def test_render_speech_falls_back_from_timbre(bridge, monkeypatch):
    class Down:
        def synthesize(self, text):
            raise RuntimeError("timbre beta down")
    monkeypatch.setattr(bridge.P, "timbre", Down())
    monkeypatch.setattr(bridge.S, "tts_provider", "gnani")
    monkeypatch.setattr(bridge.S, "gnani_api_key", "k")
    monkeypatch.setattr(bridge, "_edge_mp3", lambda text: None)
    monkeypatch.setattr(bridge, "_gtts_mp3", lambda text: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(bridge, "_sapi_wav", lambda text: b"RIFFsapi")
    assert bridge.render_speech("hi") == (None, b"RIFFsapi")
