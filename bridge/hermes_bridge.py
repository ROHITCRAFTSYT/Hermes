"""
Hermes bridge — the brain.

Endpoints:
    GET  /health          status (no auth)
    GET  /app/            the phone/web control app
    POST /voice           ESP32 posts WAV, gets WAV back
    POST /chat            app posts {"text": "..."}, gets {"reply": "..."}
    POST /talk            app posts recorded audio, gets {heard, reply, audio_b64}

All POST endpoints require header  X-Hermes-Token  when HERMES_TOKEN is set, and are
rate limited per client (RATE_LIMIT_PER_MIN).

Run:  .venv\\Scripts\\python hermes_bridge.py
"""

import asyncio
import base64
import hmac
import html
import io
import json
import logging
import math
import os
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = Path(__file__).parent
_WEBAPP = _HERE / "webapp"

if os.getenv("HERMES_SKIP_DOTENV") != "1":
    load_dotenv(_HERE / ".env")

import logs  # noqa: E402
import providers  # noqa: E402
import spotify as SP  # noqa: E402
import timers as TM  # noqa: E402
import tools as T  # noqa: E402
from config import load_settings  # noqa: E402
from providers.llm import is_rate_limit, retry_delay_seconds  # noqa: E402
from ratelimit import RateLimiter  # noqa: E402

VERSION = "1.1.0"
S = load_settings()
logs.setup(S.log_level, S.log_format)
log = logging.getLogger("hermes")

NAME = S.assistant_name
TOKEN = S.hermes_token
PORT = S.port
PLAY_ON_PC = S.play_on_pc
AGENT_DEADLINE_S = S.agent_deadline_s
P = providers.build(S, T.TOOLS_SPEC)

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
_TIMER_TALK = re.compile(r"\b(timers?|remind(ers?)?|alarms?|wake me)\b", re.I)


def _first_tool(text: str) -> str | None:
    """Tool the model must call first: fresh data for questions, Spotify for 'play <song>'."""
    if _TIMER_TALK.search(text):
        return "timer"
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


app = FastAPI(title="Hermes Bridge", version=VERSION)
_AGENT_LOCK = threading.Lock()
_LIMITER = RateLimiter(S.rate_limit_per_min)


# ── Errors, request ids, access log ──────────────────────────────────────────

def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def _error(request: Request, status: int, message: str, headers: dict | None = None) -> JSONResponse:
    # "detail" kept for older clients; "error" is the structured shape.
    return JSONResponse({"detail": message, "error": {"code": status, "message": message,
                                                      "request_id": _rid(request)}},
                        status_code=status, headers=headers)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    return _error(request, exc.status_code, str(exc.detail), getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    return _error(request, 422, "Invalid request")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.error("unhandled error", exc_info=exc, extra={"path": request.url.path})
    return _error(request, 500, "Internal error")


_RID_OK = re.compile(r"^[\w.-]{1,64}$")


@app.middleware("http")
async def _access_log(request: Request, call_next):
    rid = request.headers.get("x-request-id", "")
    rid = rid if _RID_OK.match(rid) else uuid.uuid4().hex[:12]
    request.state.request_id = rid
    ctx = logs.request_id.set(rid)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        path = request.url.path
        quiet = path == "/health" or path.startswith("/app")
        log.log(logging.DEBUG if quiet else logging.INFO, "request", extra={
            "method": request.method, "path": path, "status": response.status_code,
            "ms": round((time.perf_counter() - started) * 1000)})
        return response
    finally:
        logs.request_id.reset(ctx)


# ── Auth + rate limit ────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    if S.trust_proxy_headers and (fwd := request.headers.get("x-forwarded-for")):
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _auth(request: Request):
    if not TOKEN:
        return  # LAN mode, no token set
    sent = request.headers.get("x-hermes-token") or request.query_params.get("token") or ""
    if not hmac.compare_digest(sent.encode(), TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def guard(request: Request):
    # Limit before checking the token, so guessing tokens is throttled too.
    wait = _LIMITER.check(_client_ip(request))
    if wait is not None:
        log.warning("rate limited", extra={"client": _client_ip(request), "path": request.url.path})
        raise HTTPException(status_code=429, detail="Too many requests, slow down",
                            headers={"Retry-After": str(max(1, math.ceil(wait)))})
    _auth(request)


async def _read_audio(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > S.max_audio_bytes:
        raise HTTPException(status_code=413, detail="Audio too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > S.max_audio_bytes:
            raise HTTPException(status_code=413, detail="Audio too large")
    if not body:
        raise HTTPException(status_code=400, detail="Empty audio body")
    return bytes(body)


# ── Speech -> text ───────────────────────────────────────────────────────────

STT_UNAVAILABLE = "I couldn't reach the speech service just now. Please try again."


def transcribe(audio: bytes, filename: str = "speech.wav") -> str | None:
    """Transcript ("" for silence), or None when every STT provider failed."""
    if not P.stt.providers:
        return ""
    text, used = P.stt.transcribe(audio, filename)
    if used is None:
        return None
    log.info("transcribed", extra={"stt": used, "chars": len(text)})
    return text


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
    except Exception as e:
        log.warning("couldn't save conversation", extra={"error": str(e)})


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


def _model_error_reply(exc: Exception) -> str:
    if is_rate_limit(exc):
        delay = retry_delay_seconds(exc)
        if delay and delay >= 60:
            mins = max(1, round(delay / 60))
            return f"My model limit is full right now. Please try again in about {mins} minutes."
        return "My model limit is busy for a moment. Please try again in a few seconds."
    return "My brain hit an error while thinking. Please try again."


# ── The agent loop ───────────────────────────────────────────────────────────

def run_agent(user_text: str) -> str:
    with _AGENT_LOCK:
        return _run_agent_locked(user_text)


def _remember(user_text: str, reply: str):
    global CONVO
    CONVO.append({"role": "user", "content": user_text})
    CONVO.append({"role": "assistant", "content": reply})
    CONVO = CONVO[-_CONVO_MAX:]
    _save_convo()


def _run_agent_locked(user_text: str) -> str:
    global CONVO
    if not P.llm:
        return "My brain isn't connected yet. Please add a Groq, NVIDIA or Evon setting to the dot env file."
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
                msg = P.llm.chat(messages, force)
            except Exception as e:
                if not force or force == "none" or is_rate_limit(e):
                    raise
                msg = P.llm.chat(messages)   # provider refused a forced tool call; let the model choose
            if force == "none" and msg["tool_calls"]:
                # Some models (NVIDIA nemotron) emit tool calls even when none are offered — never run them.
                msg["tool_calls"] = []
                if not (msg["content"] or "").strip():
                    messages.append({"role": "system", "content": "Answer the user now in one or two "
                                     "sentences from the information above. Do not call any tools."})
                    msg = P.llm.chat(messages, "none")
                    msg["tool_calls"] = []
                if not (msg["content"] or "").strip():
                    msg["content"] = "Sorry, I couldn't finish looking that up in time. Please ask again."
        except Exception as e:
            log.error("llm failed", extra={"error": _compact_error(e)})
            reply = _model_error_reply(e)
            _remember(user_text, reply)
            return reply
        log.debug("llm step", extra={"step": step, "llm": msg.get("provider"), "tools": len(msg["tool_calls"])})
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
            if not isinstance(args, dict):
                args = {}
            result = T.dispatch(tc["name"], args)
            log.info("tool", extra={"tool": tc["name"], "tool_args": args, "result": result[:120]})
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "name": tc["name"], "content": result})
    else:
        reply = "That took too many steps, let's try again."

    # Remember the clean exchange (not the tool plumbing) for next time.
    _remember(user_text, reply)
    reply = reply.replace(" ", " ").replace(" ", " ")   # model emits narrow no-break spaces
    return re.sub(r"(?<=[a-zA-Z0-9])\.(?=[A-Z])", ". ", reply)


# ── Text -> speech ───────────────────────────────────────────────────────────
# Order comes from TTS_PROVIDER: Gnani Timbre, Microsoft Edge neural voices (free, no key),
# gTTS, then the offline Windows voice. Each one falls through to the next on failure.

TTS_VOICE = S.tts_voice
TTS_VOICE_TA = S.tts_voice_ta
TTS_RATE = S.tts_rate
TTS_HEDGE_S = S.tts_hedge_s     # no audio by then: race a second connection
TTS_TOTAL_S = S.tts_total_s     # then give up and use the fallback voice
_TTS_CACHE: dict[tuple, bytes] = {}   # repeated phrases ("Sorry, I didn't catch that.") skip the network


def _edge_mp3(text: str) -> bytes | None:
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
        log.warning("edge voice error", extra={"error": _compact_error(e)})
    if not mp3:
        return None
    if len(_TTS_CACHE) > 64:
        _TTS_CACHE.clear()
    _TTS_CACHE[key] = mp3
    return mp3


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
    for engine in S.tts_chain():
        try:
            if engine == "gnani" and P.timbre:
                return None, P.timbre.synthesize(text)
            if engine == "edge" and (mp3 := _edge_mp3(text)):
                return mp3, _mp3_to_wav(mp3)
            if engine == "gtts" and (S.tts_provider != "sapi" or _has_tamil(text)):
                mp3 = _gtts_mp3(text)
                return mp3, _mp3_to_wav(mp3)
            if engine == "sapi":
                return None, _sapi_wav(text)
        except Exception as e:
            log.warning("tts engine failed", extra={"tts": engine, "error": _compact_error(e)})
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
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Open(tmp, 3)
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.AudioOutputStream = stream
            voice.Speak(text)
            stream.Close()
        except Exception as e:
            log.info("SAPI unavailable, trying pyttsx3", extra={"error": str(e)[:200]})
            import pyttsx3
            eng = pyttsx3.init()
            eng.save_to_file(text, tmp)
            eng.runAndWait()
        data = Path(tmp).read_bytes()
        if not data:
            raise RuntimeError("offline voice produced no audio")
        return data
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


_LAST_REPLY_FILE = {"path": None}


def play_on_pc(mp3: bytes | None, wav: bytes):
    """Play an already-synthesized reply on the PC speakers (MP3 if we have it, else the WAV)."""
    if not PLAY_ON_PC:
        return
    try:
        import ctypes
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
        log.warning("could not play on PC", extra={"error": str(e)})


def speak_on_pc(text: str):
    """Speak a reply aloud on the PC."""
    if PLAY_ON_PC and text:
        play_on_pc(*render_speech(text))


def announce_timer(text: str, kind: str):
    """A timer/reminder/alarm is due: chime (three times for alarms), then say it."""
    log.info("timer due", extra={"kind": kind, "text": text})
    if not PLAY_ON_PC:
        return
    try:
        import winsound
        mp3, wav = render_speech(text)   # synthesize during the chime's lead-in, not after it
        for _ in range(3 if kind == "alarm" else 1):
            winsound.PlaySound(TM.chime_path(), winsound.SND_FILENAME)
        play_on_pc(mp3, wav)
    except Exception as e:
        log.warning("couldn't announce timer", extra={"error": str(e)})


def _silence_wav(seconds: float = 0.35, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buf.getvalue()


def _ext_for(content_type: str) -> str:
    ct = (content_type or "").lower()
    for key, ext in (("webm", "webm"), ("ogg", "ogg"), ("mp4", "m4a"),
                     ("mpeg", "mp3"), ("wav", "wav"), ("x-m4a", "m4a")):
        if key in ct:
            return ext
    return "wav"


def _hear_and_reply(audio: bytes, filename: str) -> tuple[str, str]:
    heard = transcribe(audio, filename)
    reply = STT_UNAVAILABLE if heard is None else run_agent(heard)
    log.info("turn", extra={"heard": heard, "reply": reply})
    return heard or "", reply


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/voice", dependencies=[Depends(guard)])
async def voice(request: Request):
    audio = await _read_audio(request)

    def work(a):
        _, reply = _hear_and_reply(a, "speech.wav")
        mp3, wav = render_speech(reply)   # synthesize once: WAV for the ESP32, MP3 for the PC
        play_on_pc(mp3, wav)              # speak it on the PC (no ESP speaker needed)
        return wav
    wav = await asyncio.to_thread(work, audio)
    return Response(content=wav, media_type="audio/wav")


@app.post("/chat", dependencies=[Depends(guard)])
async def chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON body") from None
    if not isinstance(body, dict) or not isinstance(body.get("text", ""), str):
        raise HTTPException(status_code=400, detail='Expected {"text": "..."}')
    text = (body.get("text") or "").strip()
    if len(text) > S.max_text_chars:
        raise HTTPException(status_code=413, detail="Text too long")
    reply = await asyncio.to_thread(run_agent, text)
    log.info("turn", extra={"text": text, "reply": reply})
    # Speak it on the PC in the background: the app shows the text without waiting for the voice.
    threading.Thread(target=speak_on_pc, args=(reply,), daemon=True).start()
    return JSONResponse({"reply": reply})


@app.post("/talk", dependencies=[Depends(guard)])
async def talk(request: Request):
    audio = await _read_audio(request)
    ext = _ext_for(request.headers.get("content-type", ""))

    def work(a, e):
        heard, reply = _hear_and_reply(a, f"speech.{e}")
        mp3, wav = render_speech(reply)
        play_on_pc(mp3, wav)              # also speak it aloud on the PC
        return heard, reply, base64.b64encode(wav).decode()

    heard, reply, audio_b64 = await asyncio.to_thread(work, audio, ext)
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
        return HTMLResponse(f"<h2>Spotify login cancelled ({html.escape(error or 'no code')}).</h2>", 400)
    try:
        await asyncio.to_thread(SP.finish_login, code, state)
    except Exception as e:
        return HTMLResponse(f"<h2>Spotify login failed</h2><p>{html.escape(str(e))}</p>", 400)
    log.info("spotify connected")
    return HTMLResponse("<h2>Spotify connected ✅</h2><p>You can close this tab and ask Hermes to play a song.</p>")


def _llm_label() -> str:
    models = {"groq": S.llm_model, "nvidia": S.nvidia_llm_model, "evon": S.evon_model}
    return models[P.llm.names[0]] if P.llm.names else "none"


@app.get("/health")
async def health():
    tts = [e for e in S.tts_chain() if e != "gnani" or P.timbre]
    return {"ok": True, "version": VERSION, "name": NAME, "auth": bool(TOKEN),
            "brain": bool(P.llm), "llm": _llm_label(), "stt": S.stt_model,
            "providers": {"stt": P.stt.names, "llm": P.llm.names, "tts": tts},
            "max_tokens": S.llm_max_tokens}


@app.get("/")
async def root():
    return RedirectResponse("/app/")


if _WEBAPP.exists():
    app.mount("/app", StaticFiles(directory=str(_WEBAPP), html=True), name="app")


# ── mDNS discovery for the ESP32 ─────────────────────────────────────────────

def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def advertise(ip: str):
    try:
        from zeroconf import ServiceInfo, Zeroconf
        info = ServiceInfo("_hermes._tcp.local.", "Hermes Bridge._hermes._tcp.local.",
                           addresses=[socket.inet_aton(ip)], port=PORT,
                           server="hermes.local.",
                           properties={"path": "/voice"})
        zc = Zeroconf()
        zc.register_service(info)
        return zc
    except Exception as e:
        log.warning("mDNS not advertising", extra={"error": str(e)})
        return None


def main():
    import uvicorn
    ip = _local_ip()
    print("=" * 62)
    print(f"  {NAME} bridge {VERSION}")
    print(f"  Speech-to-text: {' -> '.join(P.stt.names) or 'MISSING ❌  add GROQ_API_KEY or GNANI_API_KEY'}")
    print(f"  Brain (LLM)   : {' -> '.join(P.llm.names) or 'MISSING ❌  add a key to .env'}  [{_llm_label()}]")
    print(f"  Voice (TTS)   : {' -> '.join(e for e in S.tts_chain() if e != 'gnani' or P.timbre)}")
    print(f"  Auth token    : {'ON 🔒' if TOKEN else 'off (LAN only — set HERMES_TOKEN before exposing!)'}")
    if SP.client_id():
        link = f"not linked — open http://127.0.0.1:{PORT}/spotify/login on this PC"
        print(f"  Spotify       : {'connected ✅' if SP.is_connected() else link}")
    print(f"  On this LAN   : http://{ip}:{PORT}/app/   (open on your phone)")
    print(f"  ESP config.h  : BRIDGE_HOST \"{ip}\"  (or leave USE_MDNS 1)")
    print("=" * 62)
    for w in S.warnings():
        log.warning(w)
    threading.Thread(target=_warm_up_tts, daemon=True).start()
    TM.start(announce_timer)   # timers/reminders/alarms, including any that came due while off
    advertise(ip)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
