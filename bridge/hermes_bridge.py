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


_PLAY_MUSIC = re.compile(r"^\s*(please\s+|can you\s+|hey\s+\w+,?\s+)?play\b(?!.*\b(youtube|video)s?\b)", re.I)


def _first_tool(text: str) -> str | None:
    """Tool the model must call first: fresh data for questions, Spotify for 'play <song>'."""
    if _PLAY_MUSIC.search(text):
        return "spotify"
    if _COMMAND.match(text) or _NO_SEARCH.search(text):
        return None
    if _CRYPTO_PRICE.search(text):
        return "crypto_price"
    return "web_search" if _FACT_QUESTION.search(text) else None


def _system_prompt(user_text: str) -> str:
    now = datetime.now()
    return (f"{SYSTEM_PROMPT} Today is {now:%A, %d %B %Y} and the local time is {now:%I:%M %p}. "
            f"{FRESHNESS_RULE} {_language_hint(user_text)}")

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
        return (result.text or "").strip()
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
    r = httpx.post(
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
                if not force or _is_rate_limit(e):
                    raise
                msg = _chat_completion(messages)   # provider refused a forced tool call; let the model choose
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


# ── Text -> speech (Windows SAPI, local & free) ──────────────────────────────

def synthesize(text: str) -> bytes:
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


def speak_on_pc(text: str):
    """Play the reply aloud on the PC. English → offline SAPI (fast); Tamil/other → gTTS."""
    if not PLAY_ON_PC or not text:
        return
    try:
        if _has_tamil(text):
            from gtts import gTTS
            import ctypes
            import uuid
            winmm = ctypes.windll.winmm
            winmm.mciSendStringW("close hsnd", None, 0, 0)   # release any file still playing
            path = os.path.join(tempfile.gettempdir(), f"hermes_reply_{uuid.uuid4().hex}.mp3")
            gTTS(text=text, lang="ta").save(path)
            winmm.mciSendStringW(f'open "{path}" type mpegvideo alias hsnd', None, 0, 0)
            winmm.mciSendStringW("play hsnd", None, 0, 0)
        else:
            import winsound
            wav = synthesize(text)
            path = os.path.join(tempfile.gettempdir(), "hermes_reply.wav")
            with open(path, "wb") as f:
                f.write(wav)
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        print("   [PC audio] could not play:", e)


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


def safe_synthesize(text: str) -> bytes:
    try:
        return synthesize(text)
    except Exception as e:
        print("   [TTS] failed:", _compact_error(e))
        return _silence_wav()


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
        wav = safe_synthesize(reply)
        speak_on_pc(reply)          # speak it on the PC (no ESP speaker needed)
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
    await asyncio.to_thread(speak_on_pc, reply)   # speak it aloud on the PC
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
        wav = safe_synthesize(reply)
        return heard, reply, base64.b64encode(wav).decode()

    heard, reply, audio_b64 = await asyncio.to_thread(work, audio, ext)
    print(f"   heard: {heard!r}  reply: {reply!r}")
    await asyncio.to_thread(speak_on_pc, reply)   # also speak it aloud on the PC
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
    print(f"  On this LAN   : http://{ip}:{PORT}/app/   (open on your phone)")
    print(f"  ESP config.h  : BRIDGE_HOST \"{ip}\"  (or leave USE_MDNS 1)")
    print("=" * 62)
    advertise(ip)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
