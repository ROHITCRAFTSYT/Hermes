import os
import sys
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"
sys.path.insert(0, str(BRIDGE))

# Must be set before hermes_bridge is imported: never read a developer's real bridge/.env,
# never call a real provider.
os.environ["HERMES_SKIP_DOTENV"] = "1"
for key in ("GROQ_API_KEY", "NVIDIA_API_KEY", "GNANI_API_KEY", "EVON_BASE_URL", "EVON_API_KEY",
            "STT_PROVIDER", "LLM_PROVIDER", "TTS_PROVIDER", "TTS_ENGINE", "SPOTIFY_CLIENT_ID"):
    os.environ.pop(key, None)
os.environ.update({"HERMES_TOKEN": "test-token", "PLAY_ON_PC": "0", "RATE_LIMIT_PER_MIN": "1000",
                   "LOG_LEVEL": "WARNING"})


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    import hermes_bridge as hb
    from ratelimit import RateLimiter

    monkeypatch.setattr(hb, "_CONVO_PATH", tmp_path / "conversation.json")
    monkeypatch.setattr(hb, "CONVO", [])
    monkeypatch.setattr(hb, "_LIMITER", RateLimiter(1000))
    monkeypatch.setattr(hb, "play_on_pc", lambda *a: None)
    monkeypatch.setattr(hb, "speak_on_pc", lambda *a: None)
    return hb


@pytest.fixture
def client(bridge):
    from fastapi.testclient import TestClient
    with TestClient(bridge.app, raise_server_exceptions=False) as c:
        yield c


AUTH = {"X-Hermes-Token": "test-token"}
