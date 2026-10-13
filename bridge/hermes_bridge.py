"""
Hermes bridge — the brain.

Endpoints:
    GET  /health          status (no auth)
    GET  /app/            the phone/web control app
    POST /voice           ESP32 posts WAV, gets WAV back
    POST /chat            app posts {"text": "..."}, gets {"reply": "..."}
    POST /talk            app posts recorded audio, gets {heard, reply, audio_b64}

All POST endpoints require header  X-Hermes-Token  when HERMES_TOKEN is set.

Run:  .venv\\Scripts\\python hermes_bridge.py
"""

import os
import json
import base64
import io
import html
import socket
import asyncio
import tempfile
import threading
import time
import re
from datetime import datetime
import wave
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

import tools as T
import spotify as SP

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = Path(__file__).parent
_WEBAPP = _HERE / "webapp"

load_dotenv(_HERE / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
NVIDIA_LLM_MODEL = os.getenv("NVIDIA_LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3-turbo")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
PORT = int(os.getenv("PORT", "8787"))
NAME = os.getenv("ASSISTANT_NAME", "Hermes")
TOKEN = os.getenv("HERMES_TOKEN", "").strip()
PLAY_ON_PC = os.getenv("PLAY_ON_PC", "1") == "1"   # speak replies on the PC speakers (no ESP speaker needed)
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))   # includes reasoning tokens on gpt-oss
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low")       # gpt-oss only: low = fast, short thinking
LLM_RATE_LIMIT_RETRIES = int(os.getenv("LLM_RATE_LIMIT_RETRIES", "2"))
AGENT_DEADLINE_S = float(os.getenv("AGENT_DEADLINE_S", "12"))   # then answer with what we have (ESP waits 30 s)

SYSTEM_PROMPT = (
    f"You are {NAME}, a friendly voice assistant that runs the user's smart home, "
    "their PC, and can search and open the web. You control real things via the provided "
    "tools — use them whenever the user asks to do something, don't just describe it. "
    "To open a desktop app use pc_action(open); to open a website, go to a site, browse, "
    "or surf use open_website; for videos use youtube; to answer a question from the web "
    "use web_search. To open an app/website or control ANOTHER computer on the network "
    "(the user will say 'on the other laptop', 'on <name>'s laptop', etc.), use remote_pc "
    "with that computer's name. NEVER say you opened or did something unless you actually called the "
    "tool for it. After the tool runs, confirm in one short sentence. "
    "When the user asks a question, just speak the answer from web_search; do not open websites "
    "or apps unless they asked you to open, show, or play something. If none of your tools can do "
    "what they asked, say so briefly in one sentence instead of replying 'Okay'. "
    "Your replies may be read ALOUD, so keep them short, natural, and conversational — "
    "usually one or two sentences, no markdown, no lists, no emoji. If a tool reports a "
    "device isn't set up, tell the user plainly what to configure. "
    "Use the earlier turns of the conversation as context: resolve follow-ups and words like "
    "'it', 'that', 'the distance', 'how much' from what was just discussed, instead of asking again. "
    "IMPORTANT: Reply in the SAME language the user spoke. If they speak in Tamil, reply "
    "in natural Tamil using Tamil script (not transliteration)."
)

FRESHNESS_RULE = (
    "Your built-in knowledge is out of date and may be years old. For anything that can change "
    "over time — who currently holds a job or office (presidents, prime ministers, chief ministers, "
    "CEOs), news, elections, prices, scores, weather, releases, or anything 'current', 'latest' or "
    "'now' — ALWAYS call web_search first and answer only from its results, even if you think you "
    "know. If the results disagree with your memory, the results are right."
)

# Factual questions get a forced web_search on the first step so the model can't answer from stale memory.
_FACT_QUESTION = re.compile(
    r"^\s*(who|whom|whose|what|what's|whats|when|where|which|"
    r"how (much|many|old|far|long|big|tall|is|are|was|did|does|do))\b"
    r"|\b(latest|current|currently|right now|news|price of|score|weather|election)\b", re.I)
_NO_SEARCH = re.compile(
    r"\b(what time|the time|time is it|the date|what day|your name|who are you|what can you do)\b", re.I)
_COMMAND = re.compile(
    r"^\s*(open|play|close|launch|start|turn|switch|set|pause|stop|resume|lock|mute|unmute|"
    r"volume|take|show|go to|cast|remember)\b", re.I)


_CRYPTO_PRICE = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|solana|dogecoin|doge|xrp|crypto|cryptocurrency|memecoin|coin|token)s?\b"
    r".*\b(price|worth|cost|value|rate|trading)\b"
    r"|\b(price|worth|cost|value|rate|how much)\b.*"
    r"\b(bitcoin|btc|ethereum|eth|solana|dogecoin|doge|xrp|crypto|cryptocurrency|memecoin|coin|token)s?\b", re.I)


_PERSONAL = re.compile(r"\b(my|mine)\b", re.I)
_MUSIC_QUESTION = re.compile(r"\b(spotify|what'?s playing|what is playing|this song|current song|which song)\b", re.I)
_PLAY_MUSIC = re.compile(r"^\s*(please\s+|can you\s+|hey\s+\w+,?\s+)?play\b(?!.*\b(youtube|video)s?\b)", re.I)


def _first_tool(text: str) -> str | None:
    """Tool the model must call first: fresh data for questions, Spotify for 'play <song>'."""
    if _PLAY_MUSIC.search(text):
        return "spotify"
    if _COMMAND.match(text) or _NO_SEARCH.search(text):
        return None   # open/close/pause Spotify etc.: the model picks the right tool
    if _MUSIC_QUESTION.search(text):
        return "spotify"
    if _PERSONAL.search(text):
        return None   # "where are my keys?" is about the user (memory, devices), not the web
    if _CRYPTO_PRICE.search(text):
        return "crypto_price"
    return "web_search" if _FACT_QUESTION.search(text) else None


def _system_prompt(user_text: str) -> str:
    now = datetime.now()
    return (f"{SYSTEM_PROMPT} Today is {now:%A, %d %B %Y} and the local time is {now:%I:%M %p}. "
            f"{FRESHNESS_RULE} {T.inventory()} {T.memory_facts()} {_language_hint(user_text)}")

try:
    from groq import Groq
    # No silent SDK retries: on a rate limit we'd rather switch to NVIDIA than stall the ESP32.
    _client = Groq(api_key=GROQ_API_KEY, max_retries=0) if GROQ_API_KEY else None
    _client_error = ""
except Exception as e:
    _client = None
    _client_error = str(e)

app = FastAPI(title="Hermes Bridge")
_AGENT_LOCK = threading.Lock()


# ── Auth ─────────────────────────────────────────────────────────────────────

def _auth(request: Request):
    if not TOKEN:
        return  # LAN mode, no token set
    sent = request.headers.get("x-hermes-token") or request.query_params.get("token")
    if sent != TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# ── Speech -> text ───────────────────────────────────────────────────────────

def transcribe(audio: bytes, filename: str = "speech.wav") -> str:
    if not _client:
        return ""
    try:
        result = _client.audio.transcriptions.create(
            file=(filename, audio, "application/octet-stream"),
            model=STT_MODEL,
        )
        text = (result.text or "").strip()
        # Whisper guesses the language per clip and sometimes hears short English as Hindi etc.
        # Hermes speaks English and Tamil, so anything with neither script is re-run as English.
        if text and not re.search(r"[A-Za-z஀-௿]", text):
            print(f"   [STT] heard {text!r} (not English/Tamil) — retrying as English")
            result = _client.audio.transcriptions.create(
                file=(filename, audio, "application/octet-stream"),
                model=STT_MODEL, language="en",
            )
            text = (result.text or "").strip() or text
        return text
    except Exception as e:
        print("   [STT] failed:", _compact_error(e))
        return ""


# ── Short-term conversation memory (remembers the last few turns) ────────────

_CONVO_PATH = _HERE / "conversation.json"
_CONVO_MAX = 12   # keep the last ~6 back-and-forth turns for context
_RESET_PHRASES = ("new conversation", "start over", "forget that", "forget the conversation",
                  "reset conversation", "clear memory", "clear the conversation")


def _load_convo():
    try:
        return json.loads(_CONVO_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


CONVO = _load_convo()


def _save_convo():
    try:
        _CONVO_PATH.write_text(json.dumps(CONVO[-_CONVO_MAX:], ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass


def _has_tamil(text: str) -> bool:
    return any('஀' <= c <= '௿' for c in text)


def _language_hint(text: str) -> str:
    if _has_tamil(text):
        return "The user's latest message is Tamil. Reply in natural Tamil script."
    if re.search(r"[A-Za-z]", text or ""):
        return "The user's latest message is English or ASCII text. Reply in English."
    return "Reply in the same language as the user's latest message."


def _compact_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:500] if text else exc.__class__.__name__


def _retry_delay_seconds(exc: Exception) -> float | None:
    text = str(exc)
    m = re.search(r"try again in (?:(\d+)m)?\s*([\d.]+)s", text, re.I)
    if not m:
        return None
    minutes = int(m.group(1) or 0)
    seconds = float(m.group(2))
    return minutes * 60 + seconds


def _model_error_reply(exc: Exception) -> str:
    delay = _retry_delay_seconds(exc)
    text = str(exc).lower()
    if "rate_limit" in text or "rate limit" in text or delay is not None:
        if delay and delay >= 60:
            mins = max(1, round(delay / 60))
            return f"My model limit is full right now. Please try again in about {mins} minutes."
        return "My model limit is busy for a moment. Please try again in a few seconds."
    return "My brain hit an error while thinking. Please try again."


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "rate_limit" in text or "rate limit" in text or _retry_delay_seconds(exc) is not None


def _tool_args(force: str | None) -> dict:
    # force=None: model may pick a tool; a tool name: must call it; "none": no tools offered at all
    # (some providers ignore tool_choice="none", so the tools are left out entirely).
    if force == "none":
        return {}
    choice = {"type": "function", "function": {"name": force}} if force else "auto"
    return {"tools": T.TOOLS_SPEC, "tool_choice": choice}


def _reasoning_args(model: str) -> dict:
    # gpt-oss thinks before answering; unchecked, the thinking eats the whole token budget.
    return {"reasoning_effort": REASONING_EFFORT} if "gpt-oss" in model.lower() and REASONING_EFFORT else {}


def _groq_chat(messages: list[dict], force: str | None = None) -> dict:
    if not _client:
        raise RuntimeError(_client_error or "Groq client is not configured")
    resp = _client.chat.completions.create(
        model=LLM_MODEL, messages=messages,
        **_tool_args(force),
        temperature=0.4, max_tokens=LLM_MAX_TOKENS, **_reasoning_args(LLM_MODEL),
    )
    msg = resp.choices[0].message
    return {
        "provider": "groq",
        "content": msg.content or "",
        "tool_calls": [{
            "id": tc.id,
            "name": tc.function.name,
            "arguments": tc.function.arguments or "{}",
        } for tc in (msg.tool_calls or [])],
    }


def _nvidia_chat(messages: list[dict], force: str | None = None) -> dict:
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA API key is not configured")
    import httpx
    payload = {
        "model": NVIDIA_LLM_MODEL,
        "messages": messages,
        **_tool_args(force),
        "temperature": 0.4,
        "max_tokens": LLM_MAX_TOKENS,
        "stream": False,
        **_reasoning_args(NVIDIA_LLM_MODEL),
    }
    # retries=2 re-sends on connection resets (WinError 10054), not on HTTP errors.
    r = httpx.Client(transport=httpx.HTTPTransport(retries=2)).post(
        NVIDIA_BASE_URL + "/chat/completions",
        headers={
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"NVIDIA returned {r.status_code}: {r.text[:500]}")
    data = r.json()
    msg = data.get("choices", [{}])[0].get("message", {}) or {}
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function", {}) or {}
        calls.append({
            "id": tc.get("id") or f"nvidia_tool_{i}",
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments") or "{}",
        })
    return {"provider": "nvidia", "content": msg.get("content") or "", "tool_calls": calls}


def _chat_completion(messages: list[dict], force: str | None = None) -> dict:
    last_error: Exception | None = None
    if _client:
        for attempt in range(LLM_RATE_LIMIT_RETRIES + 1):
            try:
                return _groq_chat(messages, force)
            except Exception as e:
                last_error = e
                if _is_rate_limit(e) and NVIDIA_API_KEY:
                    print("   [LLM] Groq rate-limited; using NVIDIA fallback.")
                    break
                delay = _retry_delay_seconds(e)
                if delay is not None and delay <= 8 and attempt < LLM_RATE_LIMIT_RETRIES:
                    time.sleep(max(0.5, delay + 0.25))
                    continue
                raise

    if NVIDIA_API_KEY:
        try:
            return _nvidia_chat(messages, force)
        except Exception as e:
            if last_error:
                raise RuntimeError(f"Groq failed: {_compact_error(last_error)}; NVIDIA failed: {_compact_error(e)}")
            raise

    if last_error:
        raise last_error
    raise RuntimeError("No LLM provider is configured")


# ── The agent loop ───────────────────────────────────────────────────────────

def run_agent(user_text: str) -> str:
    with _AGENT_LOCK:
        return _run_agent_locked(user_text)


def _run_agent_locked(user_text: str) -> str:
    global CONVO
    if not _client and not NVIDIA_API_KEY:
        detail = f" ({_client_error})" if _client_error else ""
        return "My brain isn't connected yet. Please add a Groq or NVIDIA A P I key to the dot env file." + detail
    if not any(c.isalnum() for c in user_text or ""):   # empty, or Whisper heard only "." on silence
        return "Sorry, I didn't catch that."

    low = user_text.lower().strip()
    if any(p in low for p in _RESET_PHRASES):
        CONVO = []
        _save_convo()
        return "Okay, I've cleared our conversation. Starting fresh."

    # System prompt + recent history + the new turn → this gives it context.
    messages = [{"role": "system", "content": _system_prompt(user_text)}] + CONVO[-_CONVO_MAX:] + \
               [{"role": "user", "content": user_text}]
    reply = "Okay."
    first_tool = _first_tool(user_text)
    started = time.monotonic()
    for step in range(6):
        force = first_tool if step == 0 else None
        if step and time.monotonic() - started > AGENT_DEADLINE_S:
            force = "none"   # out of time: no more tools, answer from what we have
        try:
            try:
                msg = _chat_completion(messages, force)
            except Exception as e:
                if not force or force == "none" or _is_rate_limit(e):
                    raise
                msg = _chat_completion(messages)   # provider refused a forced tool call; let the model choose
            if force == "none" and msg["tool_calls"]:
                # Some models (NVIDIA nemotron) emit tool calls even when none are offered — never run them.
                msg["tool_calls"] = []
                if not (msg["content"] or "").strip():
                    messages.append({"role": "system", "content": "Answer the user now in one or two "
                                     "sentences from the information above. Do not call any tools."})
                    msg = _chat_completion(messages, "none")
                    msg["tool_calls"] = []
                if not (msg["content"] or "").strip():
                    msg["content"] = "Sorry, I couldn't finish looking that up in time. Please ask again."
        except Exception as e:
            print("   [LLM] failed:", _compact_error(e))
            reply = _model_error_reply(e)
            CONVO.append({"role": "user", "content": user_text})
            CONVO.append({"role": "assistant", "content": reply})
            CONVO = CONVO[-_CONVO_MAX:]
            _save_convo()
            return reply
        if not msg["tool_calls"]:
            reply = (msg["content"] or "Okay.").strip()
            break
        messages.append({
            "role": "assistant", "content": msg["content"] or "",
            "tool_calls": [{
                "id": tc["id"], "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            } for tc in msg["tool_calls"]],
        })
        for tc in msg["tool_calls"]:
            try:
                args = json.loads(tc["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            result = T.dispatch(tc["name"], args)
            print(f"   tool  {tc['name']}({args}) -> {result[:120]}")
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "name": tc["name"], "content": result})
    else:
        reply = "That took too many steps, let's try again."

    # Remember the clean exchange (not the tool plumbing) for next time.
    CONVO.append({"role": "user", "content": user_text})
    CONVO.append({"role": "assistant", "content": reply})
    CONVO = CONVO[-_CONVO_MAX:]
    _save_convo()
    reply = reply.replace(" ", " ").replace(" ", " ")   # model emits narrow no-break spaces
    return re.sub(r"(?<=[a-zA-Z0-9])\.(?=[A-Z])", ". ", reply)


# ── Text -> speech ───────────────────────────────────────────────────────────
# Neural voices from Microsoft's Edge "Read aloud" service (free, no key; English + Tamil).
# Falls back to the offline Windows voice (gTTS for Tamil) when it's unreachable or slow.

TTS_ENGINE = os.getenv("TTS_ENGINE", "edge").lower()          # edge | sapi
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-AvaMultilingualNeural")
TTS_VOICE_TA = os.getenv("TTS_VOICE_TA", "ta-IN-PallaviNeural")
TTS_RATE = os.getenv("TTS_RATE", "+0%")                        # e.g. +10% to speak faster
TTS_HEDGE_S = float(os.getenv("TTS_HEDGE_S", "2.5"))     # no audio by then: race a second connection
TTS_TOTAL_S = float(os.getenv("TTS_TOTAL_S", "8"))        # then give up and use the fallback voice
_TTS_CACHE: dict[tuple, bytes] = {}   # repeated phrases ("Sorry, I didn't catch that.") skip the network


def _edge_mp3(text: str) -> bytes | None:
    if TTS_ENGINE != "edge":
        return None
    voice = TTS_VOICE_TA if _has_tamil(text) else TTS_VOICE
    key = (voice, TTS_RATE, text)
    if key in _TTS_CACHE:
        return _TTS_CACHE[key]
    import edge_tts

    async def fetch(got_audio: asyncio.Event):
        stream = edge_tts.Communicate(text, voice, rate=TTS_RATE).stream()
        chunks = []
        try:
            while True:
                c = await asyncio.wait_for(stream.__anext__(), 5.0 if chunks else TTS_TOTAL_S)
                if c["type"] == "audio":
                    chunks.append(c["data"])
                    got_audio.set()
        except StopAsyncIteration:
            return b"".join(chunks)
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass

    async def hedged():
        # Some requests stall with no audio at all while a fresh one is fine (often on mobile
        # hotspots). If the first audio is late, race a second connection; keep whichever finishes.
        tasks, events = [], []
        def launch():
            events.append(asyncio.Event())
            tasks.append(asyncio.create_task(fetch(events[-1])))
        launch()
        waiter = asyncio.create_task(events[0].wait())
        await asyncio.wait({waiter, tasks[0]}, timeout=TTS_HEDGE_S, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if not events[0].is_set():
            launch()
        try:
            for fut in asyncio.as_completed(tasks, timeout=TTS_TOTAL_S):
                try:
                    if mp3 := await fut:
                        return mp3
                except Exception:
                    continue
        finally:
            for t in tasks:
                t.cancel()
        return None

    try:
        mp3 = asyncio.run(hedged())   # always called from a worker thread
    except Exception as e:
        mp3 = None
        print("   [TTS] neural voice error:", _compact_error(e))
    if not mp3:
        print("   [TTS] neural voice unavailable, using fallback")
        return None
    if mp3:
        if len(_TTS_CACHE) > 64:
            _TTS_CACHE.clear()
        _TTS_CACHE[key] = mp3
    return mp3 or None


def _mp3_to_wav(mp3: bytes) -> bytes:
    # The ESP32 and the web app play WAV; miniaudio decodes MP3 without needing ffmpeg.
    import miniaudio
    snd = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1,
                           sample_rate=24000)   # the voices' native rate; miniaudio would upsample to 44.1k
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(snd.sample_rate)
        w.writeframes(snd.samples.tobytes())
    return buf.getvalue()


def _gtts_mp3(text: str) -> bytes:
    from gtts import gTTS
    buf = io.BytesIO()
    gTTS(text=text, lang="ta" if _has_tamil(text) else "en", timeout=6).write_to_fp(buf)
    return buf.getvalue()


def render_speech(text: str) -> tuple[bytes | None, bytes]:
    """Synthesize a reply ONCE: (MP3 for the PC speakers, or None) and a WAV for the ESP32 / app."""
    text = text or "Okay."
    mp3 = _edge_mp3(text)
    if mp3 is None and (TTS_ENGINE == "edge" or _has_tamil(text)):   # smoother than Windows; speaks Tamil
        try:
            mp3 = _gtts_mp3(text)
        except Exception as e:
            print("   [TTS] gTTS failed:", _compact_error(e))
    if mp3:
        try:
            return mp3, _mp3_to_wav(mp3)
        except Exception as e:
            print("   [TTS] couldn't decode MP3:", _compact_error(e))
    try:
        return None, _sapi_wav(text)
    except Exception as e:
        print("   [TTS] failed:", _compact_error(e))
        return None, _silence_wav()


def synthesize(text: str) -> bytes:
    return render_speech(text)[1]


def _warm_up_tts():
    # Importing edge-tts/miniaudio takes ~2 s and the first connection is slow; do both at startup
    # instead of on the first reply. The phrase is cached, so it's also instant when used.
    try:
        render_speech("Sorry, I didn't catch that.")
    except Exception:
        pass


def _sapi_wav(text: str) -> bytes:
    text = text or "Okay."
    try:
        import pythoncom, win32com.client
        pythoncom.CoInitialize()
        tmp = tempfile.mktemp(suffix=".wav")
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(tmp, 3)
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()
        data = Path(tmp).read_bytes()
        os.remove(tmp)
        return data
    except Exception as e:
        print("   [TTS] SAPI failed, trying pyttsx3:", e)
        import pyttsx3
        tmp = tempfile.mktemp(suffix=".wav")
        eng = pyttsx3.init()
        eng.save_to_file(text, tmp)
        eng.runAndWait()
        data = Path(tmp).read_bytes()
        os.remove(tmp)
        return data


_LAST_REPLY_FILE = {"path": None}


def play_on_pc(mp3: bytes | None, wav: bytes):
    """Play an already-synthesized reply on the PC speakers (MP3 if we have it, else the WAV)."""
    if not PLAY_ON_PC:
        return
    try:
        import ctypes
        import uuid
        import winsound
        winmm = ctypes.windll.winmm
        winmm.mciSendStringW("close hsnd", None, 0, 0)    # a new reply interrupts the previous one
        winsound.PlaySound(None, winsound.SND_PURGE)
        old = _LAST_REPLY_FILE["path"]
        if old:
            try:
                os.remove(old)
            except OSError:
                pass
        path = os.path.join(tempfile.gettempdir(), f"hermes_reply_{uuid.uuid4().hex}.{'mp3' if mp3 else 'wav'}")
        with open(path, "wb") as f:
            f.write(mp3 or wav)
        _LAST_REPLY_FILE["path"] = path
        if mp3:
            winmm.mciSendStringW(f'open "{path}" type mpegvideo alias hsnd', None, 0, 0)
            winmm.mciSendStringW("play hsnd", None, 0, 0)
        else:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        print("   [PC audio] could not play:", e)


def speak_on_pc(text: str):
    """Speak a reply aloud on the PC."""
    if PLAY_ON_PC and text:
        play_on_pc(*render_speech(text))


def _silence_wav(seconds: float = 0.35, sample_rate: int = 16000) -> bytes:
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        frames = b"\x00\x00" * int(seconds * sample_rate)
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(frames)
        return Path(tmp).read_bytes()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _ext_for(content_type: str) -> str:
    ct = (content_type or "").lower()
    for key, ext in (("webm", "webm"), ("ogg", "ogg"), ("mp4", "m4a"),
                     ("mpeg", "mp3"), ("wav", "wav"), ("x-m4a", "m4a")):
        if key in ct:
            return ext
    return "wav"


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/voice")
async def voice(request: Request):
    _auth(request)
    audio = await request.body()
    print(f"\n── /voice ── {len(audio)} bytes")
    def work(a):
        heard = transcribe(a)
        reply = run_agent(heard)
        print(f"   heard: {heard!r}")
        print(f"   reply: {reply!r}")
        mp3, wav = render_speech(reply)   # synthesize once: WAV for the ESP32, MP3 for the PC
        play_on_pc(mp3, wav)              # speak it on the PC (no ESP speaker needed)
        return wav
    wav = await asyncio.to_thread(work, audio)
    return Response(content=wav, media_type="audio/wav")


@app.post("/chat")
async def chat(request: Request):
    _auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON body")
    text = (body.get("text") or "").strip()
    print(f"\n── /chat ── {text!r}")
    reply = await asyncio.to_thread(run_agent, text)
    print(f"   reply: {reply!r}")
    # Speak it on the PC in the background: the app shows the text without waiting for the voice.
    threading.Thread(target=speak_on_pc, args=(reply,), daemon=True).start()
    return JSONResponse({"reply": reply})


@app.post("/talk")
async def talk(request: Request):
    _auth(request)
    audio = await request.body()
    ext = _ext_for(request.headers.get("content-type", ""))
    print(f"\n── /talk ── {len(audio)} bytes ({ext})")

    def work(a, e):
        heard = transcribe(a, filename=f"speech.{e}")
        reply = run_agent(heard)
        mp3, wav = render_speech(reply)
        play_on_pc(mp3, wav)              # also speak it aloud on the PC
        return heard, reply, base64.b64encode(wav).decode()

    heard, reply, audio_b64 = await asyncio.to_thread(work, audio, ext)
    print(f"   heard: {heard!r}  reply: {reply!r}")
    return JSONResponse({"heard": heard, "reply": reply, "audio_b64": audio_b64})


def _local_only(request: Request):
    # Spotify login only from this PC: its redirect URI is 127.0.0.1, and LAN devices shouldn't link accounts.
    if (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="Open this page on the PC running Hermes.")


@app.get("/spotify/login")
async def spotify_login(request: Request):
    _local_only(request)
    if not SP.client_id():
        return HTMLResponse("<h2>Add SPOTIFY_CLIENT_ID to bridge/.env and restart Hermes first.</h2>", 400)
    return RedirectResponse(SP.login_url())


@app.get("/spotify/callback")
async def spotify_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    _local_only(request)
    if error or not code:
        return HTMLResponse(f"<h2>Spotify login cancelled ({error or 'no code'}).</h2>", 400)
    try:
        await asyncio.to_thread(SP.finish_login, code, state)
    except Exception as e:
        return HTMLResponse(f"<h2>Spotify login failed</h2><p>{html.escape(str(e))}</p>", 400)
    print("   [Spotify] connected ✅")
    return HTMLResponse("<h2>Spotify connected ✅</h2><p>You can close this tab and ask Hermes to play a song.</p>")


@app.get("/health")
async def health():
    return {"ok": True, "name": NAME, "brain": bool(_client or NVIDIA_API_KEY),
            "auth": bool(TOKEN), "stt": STT_MODEL, "llm": LLM_MODEL,
            "groq": bool(_client), "voice_stt": bool(_client),
            "nvidia": bool(NVIDIA_API_KEY), "nvidia_llm": NVIDIA_LLM_MODEL,
            "max_tokens": LLM_MAX_TOKENS}


@app.get("/")
async def root():
    return RedirectResponse("/app/")


if _WEBAPP.exists():
    app.mount("/app", StaticFiles(directory=str(_WEBAPP), html=True), name="app")


# ── mDNS discovery for the ESP32 ─────────────────────────────────────────────

def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def advertise(ip: str):
    try:
        from zeroconf import Zeroconf, ServiceInfo
        info = ServiceInfo("_hermes._tcp.local.", "Hermes Bridge._hermes._tcp.local.",
                           addresses=[socket.inet_aton(ip)], port=PORT,
                           server="hermes.local.",
                           properties={"path": "/voice"})
        zc = Zeroconf(); zc.register_service(info)
        return zc
    except Exception as e:
        print("   [mDNS] not advertising:", e)
        return None


if __name__ == "__main__":
    ip = _local_ip()
    print("=" * 62)
    print(f"  {NAME} bridge")
    print(f"  Brain (Groq)  : {'connected ✅' if _client else 'MISSING ❌  add key to .env'}")
    print(f"  Auth token    : {'ON 🔒' if TOKEN else 'off (LAN only — set HERMES_TOKEN before exposing!)'}")
    if SP.client_id():
        print(f"  Spotify       : {'connected ✅' if SP.is_connected() else f'not linked — open http://127.0.0.1:{PORT}/spotify/login on this PC'}")
    print(f"  Voice         : {TTS_VOICE if TTS_ENGINE == 'edge' else 'Windows (offline)'}")
    print(f"  On this LAN   : http://{ip}:{PORT}/app/   (open on your phone)")
    print(f"  ESP config.h  : BRIDGE_HOST \"{ip}\"  (or leave USE_MDNS 1)")
    print("=" * 62)
    threading.Thread(target=_warm_up_tts, daemon=True).start()
    advertise(ip)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
