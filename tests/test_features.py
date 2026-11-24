"""Command routing, device names, memory, weather, the local model, and the app's history/timer endpoints."""

import json

import httpx
import pytest
from conftest import AUTH
from helpers import mock_client, wav_bytes

import providers
import timers as TM
import tools as T
from config import Settings
from providers.llm import LLMChain, OpenAICompatLLM


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Tools and timers write next to the bridge; keep them in a temp dir."""
    monkeypatch.setattr(T, "_MEM_PATH", tmp_path / "memory.json")
    monkeypatch.setattr(T, "_CFG_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(TM, "_PATH", tmp_path / "timers.json")
    monkeypatch.delenv("WEATHER_LOCATION", raising=False)
    monkeypatch.delenv("HA_URL", raising=False)
    T._GEO_CACHE.clear()


# ── which tool is forced first ───────────────────────────────────────────────

@pytest.mark.parametrize("text, tool", [
    ("set a timer for 5 minutes", "timer"),
    ("remind me at 6 pm to call mom", "timer"),
    ("play Over My Dead Body", "spotify"),
    ("what's the weather?", "weather"),
    ("what's the weather in Paris tomorrow", "weather"),
    ("will it rain today", "weather"),
    ("do I need an umbrella", "weather"),
    ("what's the price of bitcoin", "crypto_price"),
    ("who is the current prime minister of the UK", "web_search"),
])
def test_forced_first_tool(bridge, text, tool):
    assert bridge._first_tool(text) == tool


@pytest.mark.parametrize("text", [
    "open notepad", "open the weather app", "play youtube videos of cats",
    "what time is it", "where are my keys", "turn on the bedroom light",
])
def test_model_chooses(bridge, text):
    assert bridge._first_tool(text) is None


# ── device names and memory ──────────────────────────────────────────────────

REG = {"bedroom": {}, "living lamp": {}, "porch": {}, "bedroom AC": {}}


@pytest.mark.parametrize("said, key", [
    ("bedroom", "bedroom"), ("the porch light", "porch"), ("living", "living lamp"), ("bedroom ac", "bedroom AC"),
])
def test_device_name_match(said, key):
    assert T._match(said, REG) == key


def test_device_name_never_guesses():
    assert T._match("garage", REG) is None
    assert T._match("living", REG, partial=False) is None


def test_remember_with_fuzzy_key():
    T.remember("set", "car_keys_location", "on the hook by the door")
    assert "hook" in T.remember("get", "car keys")
    assert "nothing stored" in T.remember("get", "passport")


# ── weather (fake Open-Meteo) ────────────────────────────────────────────────

GEO = {"results": [
    {"name": "Springfield", "admin1": "Missouri", "country": "United States", "latitude": 37.2, "longitude": -93.3},
    {"name": "Springfield", "admin1": "Illinois", "country": "United States", "latitude": 39.8, "longitude": -89.6},
]}
FORECAST = {
    "current": {"temperature_2m": 21.4, "apparent_temperature": 20.6, "relative_humidity_2m": 55,
                "weather_code": 2, "wind_speed_10m": 12.2},
    "daily": {"weather_code": [2, 63, 0], "temperature_2m_max": [24.1, 18.0, 25.0],
              "temperature_2m_min": [12.2, 10.4, 11.0], "precipitation_probability_max": [10, 80, 0]},
}


@pytest.fixture
def open_meteo(monkeypatch):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if "geocoding" in request.url.host:
            return httpx.Response(200, json=GEO if request.url.params["name"].lower() == "springfield" else {})
        return httpx.Response(200, json=FORECAST)

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kw: httpx.MockTransport(handler))
    return seen


def test_weather_now(open_meteo):
    out = T.weather("Springfield")
    assert "Springfield, Missouri" in out and "partly cloudy" in out and "21°C" in out and "high 24°C" in out


def test_weather_uses_region_after_comma(open_meteo):
    assert "Illinois" in T.weather("Springfield, Illinois")
    forecast = next(r for r in open_meteo if "forecast" in r.url.path)
    assert forecast.url.params["latitude"] == "39.8"


def test_weather_tomorrow(open_meteo):
    out = T.weather("Springfield", "tomorrow")
    assert "tomorrow: rain" in out and "80% chance of rain" in out


def test_weather_unknown_place(open_meteo):
    assert "couldn't find" in T.weather("Atlantis")


def test_weather_home_location(open_meteo, monkeypatch):
    assert "which city" in T.weather("")
    T.remember("set", "home city", "Springfield")
    assert "Springfield" in T.weather("")
    monkeypatch.setenv("WEATHER_LOCATION", "Springfield, Illinois")
    assert "Illinois" in T.weather("here")


def test_weather_falls_back_to_web_search_when_offline(monkeypatch):
    def down(request):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kw: httpx.MockTransport(down))
    monkeypatch.setattr(T, "web_search", lambda q: f"searched: {q}")
    assert T.weather("Springfield") == "searched: weather in Springfield now"


def test_weather_tool_registered():
    assert "weather" in {t["function"]["name"] for t in T.TOOLS_SPEC} and "weather" in T._DISPATCH


# ── local model ──────────────────────────────────────────────────────────────

def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_local_model_joins_chain_only_when_set():
    assert make(groq_api_key="g").llm_chain() == ["groq"]
    assert make(groq_api_key="g", local_llm_model="qwen3:8b").llm_chain() == ["groq", "local"]
    assert make(llm_provider="local", groq_api_key="g", local_llm_model="qwen3:8b").llm_chain() == ["local", "groq"]
    s = make(llm_provider="local", groq_api_key="g")
    assert s.llm_chain() == ["groq"] and any("LOCAL_LLM_MODEL is empty" in w for w in s.warnings())


def test_local_model_built_with_its_own_timeout():
    p = providers.build(make(local_llm_model="qwen3:8b", local_llm_timeout_s=123), tools_spec=[])
    assert p.llm.names == ["local"]
    local = p.llm.providers[0]
    assert local.model == "qwen3:8b" and local.base_url == "http://localhost:11434/v1"
    assert local.client.timeout.read == 123


def test_ollama_object_arguments_and_no_auth_header():
    body = {"choices": [{"message": {"content": "", "tool_calls": [
        {"function": {"name": "weather", "arguments": {"location": "Chennai"}}}]}}]}
    client, seen = mock_client(lambda r: httpx.Response(200, json=body))
    llm = OpenAICompatLLM("local", "http://localhost:11434/v1", "", "qwen3:8b", 256, client, retries=0)
    out = LLMChain([llm], tools_spec=[]).chat([{"role": "user", "content": "weather?"}])
    call = out["tool_calls"][0]
    assert call["id"] == "local_tool_0" and json.loads(call["arguments"]) == {"location": "Chennai"}
    assert "authorization" not in seen[0].headers


# ── spoken errors the user can act on ────────────────────────────────────────

def test_error_replies_name_the_fix(bridge):
    assert "key was rejected" in bridge._model_error_reply(RuntimeError("groq: HTTP 401: Invalid API Key"))
    assert "retired" in bridge._model_error_reply(
        RuntimeError("nvidia: HTTP 410: the model has reached its end of life"))
    assert "error while thinking" in bridge._model_error_reply(RuntimeError("boom"))


# ── history, reset, timers, on-device audio ──────────────────────────────────

@pytest.fixture
def echo(bridge, monkeypatch):
    def fake_agent(text):
        bridge.CONVO.extend([{"role": "user", "content": text}, {"role": "assistant", "content": f"echo: {text}"}])
        return f"echo: {text}"
    monkeypatch.setattr(bridge, "run_agent", fake_agent)
    return bridge


@pytest.mark.parametrize("method, path", [("get", "/history"), ("post", "/reset"), ("get", "/timers"),
                                          ("delete", "/timers/abc")])
def test_new_endpoints_need_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_history_and_reset(echo, client):
    client.post("/chat", json={"text": "hello"}, headers=AUTH)
    msgs = client.get("/history", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["content"] == "echo: hello"
    assert client.post("/reset", headers=AUTH).json() == {"ok": True}
    assert client.get("/history", headers=AUTH).json()["messages"] == []


def test_chat_can_return_audio(echo, client, monkeypatch):
    monkeypatch.setattr(echo, "render_speech", lambda text: (None, wav_bytes()))
    j = client.post("/chat", json={"text": "hi", "audio": True}, headers=AUTH).json()
    assert j["reply"] == "echo: hi" and j["audio_b64"]
    assert "audio_b64" not in client.post("/chat", json={"text": "hi"}, headers=AUTH).json()


def test_timers_list_and_cancel(client):
    TM.handle("set", "timer", "pasta", minutes=8)
    TM.handle("set", "reminder", "call mom", hours=2)
    items = client.get("/timers", headers=AUTH).json()["timers"]
    assert [i["label"] for i in items] == ["pasta", "call mom"]   # soonest first
    assert "8 minutes" in items[0]["text"]
    assert client.delete(f"/timers/{items[0]['id']}", headers=AUTH).json() == {"ok": True}
    assert client.delete(f"/timers/{items[0]['id']}", headers=AUTH).status_code == 404
    assert [i["label"] for i in client.get("/timers", headers=AUTH).json()["timers"]] == ["call mom"]


def test_reset_phrase_clears_conversation(bridge, monkeypatch):
    monkeypatch.setattr(bridge.P, "llm", LLMChain([object()], tools_spec=[]))   # any configured brain
    bridge.CONVO.extend([{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}])
    assert "cleared" in bridge.run_agent("new conversation")
    assert bridge.CONVO == []


# ── fixes from the CodeRabbit review of #3 ───────────────────────────────────

def test_reset_waits_off_the_event_loop(bridge, client):
    """While a chat holds the agent lock, /reset must not stall unrelated requests like /health."""
    import threading
    import time

    bridge.CONVO.extend([{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}])
    result = {}
    with bridge._AGENT_LOCK:   # a long-running /chat
        reset = threading.Thread(target=lambda: result.update(reset=client.post("/reset", headers=AUTH)))
        reset.start()
        time.sleep(0.2)        # /reset is now waiting for the lock
        # Ask for /health from another thread, so a blocked event loop fails the test instead of hanging it.
        health = threading.Thread(target=lambda: result.update(health=client.get("/health")))
        health.start()
        health.join(2)
        answered_while_locked = "health" in result
        reset_still_waiting = reset.is_alive()
    reset.join(5)
    health.join(5)
    assert answered_while_locked, "/health stalled while /reset waited for the agent lock"
    assert reset_still_waiting
    assert result["reset"].json() == {"ok": True} and bridge.CONVO == []


def test_weather_matches_each_qualifier(monkeypatch):
    geo = {"results": [
        {"name": "London", "admin1": "England", "country": "United Kingdom", "country_code": "GB",
         "latitude": 51.5, "longitude": -0.1},
        {"name": "London", "admin1": "Ontario", "country": "Canada", "country_code": "CA",
         "latitude": 42.98, "longitude": -81.25},
    ]}

    def handler(request):
        if "geocoding" in request.url.host:
            return httpx.Response(200, json=geo)
        return httpx.Response(200, json=FORECAST)
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kw: httpx.MockTransport(handler))
    assert "London, Ontario" in T.weather("London, Ontario, Canada")
    assert "London, Ontario" in T.weather("London, Canada")
    assert "London, England" in T.weather("London, UK")   # no match: keep the geocoder ranking


def test_local_only_drops_cloud_fallbacks():
    keys = {"groq_api_key": "g", "nvidia_api_key": "n", "local_llm_model": "qwen3:8b"}
    assert make(llm_provider="local", **keys).llm_chain() == ["local", "groq", "nvidia"]   # order only
    assert make(llm_provider="local", llm_local_only=True, **keys).llm_chain() == ["local"]
    s = make(llm_local_only=True, groq_api_key="g")
    assert s.llm_chain() == [] and any("LLM_LOCAL_ONLY=1 but LOCAL_LLM_MODEL is empty" in w for w in s.warnings())


class ScriptedLLM:
    name = "local"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, extra):
        self.calls.append((messages, extra))
        return self.replies.pop(0)


def test_forced_tool_skipped_is_asked_again(bridge, monkeypatch):
    """Ollama ignores tool_choice: a text answer to a weather question gets one explicit retry."""
    stale = {"provider": "local", "content": "It's sunny, 30°C.", "tool_calls": []}
    call = {"provider": "local", "content": "", "tool_calls": [
        {"id": "c1", "name": "weather", "arguments": '{"location": "Chennai"}'}]}
    final = {"provider": "local", "content": "Drizzle, 27°C.", "tool_calls": []}
    llm = ScriptedLLM([stale, call, final])
    monkeypatch.setattr(bridge.P, "llm", LLMChain([llm], tools_spec=T.TOOLS_SPEC))
    monkeypatch.setattr(T, "dispatch", lambda name, args: "Right now in Chennai: drizzle, 27°C.")
    assert bridge.run_agent("what's the weather in Chennai?") == "Drizzle, 27°C."
    retry_messages, retry_extra = llm.calls[1]
    assert "Call the weather tool now" in retry_messages[-1]["content"]
    assert retry_extra["tool_choice"]["function"]["name"] == "weather"
