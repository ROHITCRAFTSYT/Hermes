import pytest

from config import Settings, load_settings


def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_defaults_are_valid():
    s = make()
    assert s.stt_provider == "groq" and s.llm_provider == "groq" and s.tts_provider == "edge"


def test_gnani_first_when_selected_and_keyed():
    s = make(stt_provider="gnani", gnani_api_key="k", groq_api_key="g")
    assert s.stt_chain() == ["gnani", "groq"]
    s = make(tts_provider="gnani", gnani_api_key="k")
    assert s.tts_chain() == ["gnani", "edge", "gtts", "sapi"]


def test_gnani_selected_without_key_falls_back_with_warning():
    s = make(stt_provider="gnani", tts_provider="gnani", groq_api_key="g")
    assert s.stt_chain() == ["groq"]
    assert s.tts_chain() == ["edge", "gtts", "sapi"]
    assert any("GNANI_API_KEY is empty" in w for w in s.warnings())


def test_evon_chain_needs_base_url():
    assert make(llm_provider="evon", groq_api_key="g").llm_chain() == ["groq"]
    s = make(llm_provider="evon", evon_base_url="http://gpu:8000/v1/", groq_api_key="g")
    assert s.llm_chain() == ["evon", "groq"]
    assert s.evon_base_url == "http://gpu:8000/v1"


def test_legacy_tts_engine_env(monkeypatch):
    monkeypatch.setenv("TTS_ENGINE", "sapi")
    assert Settings(_env_file=None).tts_provider == "sapi"


def test_empty_env_values_use_defaults(monkeypatch):
    monkeypatch.setenv("PORT", "")
    monkeypatch.setenv("REASONING_EFFORT", "")
    s = Settings(_env_file=None)
    assert s.port == 8787 and s.reasoning_effort == "low"


@pytest.mark.parametrize("field,value", [
    ("port", 70000),
    ("stt_provider", "whisperx"),
    ("gnani_stt_language", "english"),
    ("gnani_base_url", "api.vachana.ai"),
    ("gnani_tts_sample_rate", 12345),
    ("http_timeout_s", 0),
])
def test_bad_values_rejected(field, value):
    with pytest.raises(SystemExit) as e:
        load_settings(_env_file=None, **{field: value})
    assert field.upper() in str(e.value)


def test_language_pair_allowed():
    assert make(gnani_stt_language="en-IN, hi-IN").gnani_stt_language == "en-IN,hi-IN"
