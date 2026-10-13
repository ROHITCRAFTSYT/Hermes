"""
Spotify playback for Hermes, via the Spotify Web API (needs Spotify Premium).

One-time setup (see README):
  1. Create an app at https://developer.spotify.com/dashboard
     - Redirect URI:  http://127.0.0.1:8787/spotify/callback   (match your PORT)
     - API:           Web API
  2. Put its Client ID in bridge/.env as SPOTIFY_CLIENT_ID=...
  3. Start the bridge and open http://127.0.0.1:8787/spotify/login on this PC.

Login uses Authorization Code + PKCE, so no client secret is stored. Tokens are
kept in spotify_token.json (git-ignored) and refreshed automatically.
"""

import os
import json
import time
import base64
import hashlib
import secrets
import socket
import threading
from pathlib import Path
from urllib.parse import urlencode

import httpx

_HERE = Path(__file__).parent
_TOKEN_PATH = _HERE / "spotify_token.json"
_ACCOUNTS = "https://accounts.spotify.com"
_API = "https://api.spotify.com/v1"
_SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing user-read-private"

_lock = threading.Lock()
_pending: dict[str, str] = {}   # OAuth state -> PKCE verifier, for logins in progress


def client_id() -> str:
    return os.getenv("SPOTIFY_CLIENT_ID", "").strip()


def redirect_uri() -> str:
    return f"http://127.0.0.1:{os.getenv('PORT', '8787')}/spotify/callback"


def is_connected() -> bool:
    return _TOKEN_PATH.exists()


# ── Login (PKCE) ─────────────────────────────────────────────────────────────

def login_url() -> str:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _pending[state] = verifier
    return f"{_ACCOUNTS}/authorize?" + urlencode({
        "client_id": client_id(), "response_type": "code", "redirect_uri": redirect_uri(),
        "scope": _SCOPES, "state": state,
        "code_challenge_method": "S256", "code_challenge": challenge,
    })


def finish_login(code: str, state: str) -> None:
    verifier = _pending.pop(state, None)
    if not verifier:
        raise ValueError("Login link expired or was already used. Open /spotify/login again.")
    _save(_token_request({
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "client_id": client_id(), "code_verifier": verifier,
    }))


def _token_request(data: dict) -> dict:
    r = httpx.post(f"{_ACCOUNTS}/api/token", data=data, timeout=10)
    if r.status_code >= 400:
        raise RuntimeError(f"Spotify login failed ({r.status_code}): {r.text[:200]}")
    return r.json()


def _save(tok: dict, old: dict | None = None) -> dict:
    tok = {**(old or {}), **tok, "expires_at": time.time() + tok.get("expires_in", 3600) - 60}
    _TOKEN_PATH.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    return tok


def _access_token() -> str:
    with _lock:
        tok = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
        if time.time() >= tok.get("expires_at", 0):
            # Spotify may rotate the refresh token; _save keeps the old one if it doesn't.
            tok = _save(_token_request({"grant_type": "refresh_token", "client_id": client_id(),
                                        "refresh_token": tok["refresh_token"]}), tok)
        return tok["access_token"]


# ── Web API ──────────────────────────────────────────────────────────────────

class SpotifyError(Exception):
    pass


def _call(method: str, path: str, **kw):
    r = httpx.request(method, _API + path, timeout=10,
                      headers={"Authorization": f"Bearer {_access_token()}"}, **kw)
    if r.status_code == 204 or not r.content:
        return {}
    try:
        data = r.json()
    except ValueError:   # player commands can answer 200 with a plain-text id instead of JSON
        if r.status_code < 400:
            return {}
        raise SpotifyError(f"Spotify said: {r.status_code}.")
    if r.status_code >= 400:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        if err.get("reason") == "PREMIUM_REQUIRED":
            raise SpotifyError("Playing songs needs a Spotify Premium account.")
        if err.get("reason") == "NO_ACTIVE_DEVICE":
            raise SpotifyError("NO_ACTIVE_DEVICE")
        raise SpotifyError(f"Spotify said: {err.get('message') or r.status_code}.")
    return data


def _device_id(wait: bool = True) -> str | None:
    """Pick the device to play on: the active one, else this PC; start the app if needed."""
    for attempt in range(12 if wait else 1):
        devices = _call("GET", "/me/player/devices").get("devices", [])
        active = [d for d in devices if d.get("is_active")]
        this_pc = [d for d in devices if d.get("name", "").lower() == socket.gethostname().lower()]
        apps = [d for d in devices if d.get("type") == "Computer" and "web player" not in d.get("name", "").lower()]
        pick = (active or this_pc or apps or devices or [None])[0]
        if pick:
            return pick["id"]
        if attempt == 0:
            os.startfile("spotify:")   # launch the desktop app; it registers as a device in a few seconds
        time.sleep(1)
    return None


def _search(query: str) -> tuple[dict, str]:
    """Return (play request body, spoken description) for the best match."""
    q = query.strip()
    kind = "track"
    for k in ("playlist", "album", "artist"):
        if q.lower().startswith(k + " ") or q.lower().endswith(" " + k):
            kind = k
            q = q[len(k) + 1:] if q.lower().startswith(k + " ") else q[: -len(k) - 1]
            break
    # market=from_token: only results playable in the user's country (needs user-read-private).
    items = _call("GET", "/search", params={"q": q, "type": kind, "limit": 1, "market": "from_token"}) \
        .get(kind + "s", {}).get("items", [])
    items = [i for i in items if i]
    if not items:
        raise SpotifyError(f"I couldn't find '{query}' on Spotify.")
    it = items[0]
    if kind == "track":
        artists = ", ".join(a["name"] for a in it.get("artists", []))
        # Play the track inside its album: the desktop app silently ignores a bare
        # {"uris": [track]}, but starts an album context at an offset reliably.
        album = (it.get("album") or {}).get("uri")
        body = {"context_uri": album, "offset": {"uri": it["uri"]}} if album else {"uris": [it["uri"]]}
        return body, f"{it['name']} by {artists}"
    return {"context_uri": it["uri"]}, f"the {kind} {it['name']}"


def _playing(body: dict | None) -> bool:
    """Is Spotify playing — and, when we asked for something specific, is it that?"""
    cur = _call("GET", "/me/player")
    if not cur.get("is_playing"):
        return False
    if not body:
        return True
    item = cur.get("item") or {}
    want = (body.get("offset") or {}).get("uri") or (body.get("uris") or [None])[0]
    if want:   # a track (Spotify may swap in a regional copy, recorded in linked_from)
        return want in (item.get("uri"), (item.get("linked_from") or {}).get("uri"))
    return (cur.get("context") or {}).get("uri") == body.get("context_uri")


def _start(dev: str, body: dict | None = None) -> bool:
    """Send play, then confirm Spotify actually started; an idle app can drop the first command."""
    for attempt in range(2):
        if attempt:
            _call("PUT", "/me/player", json={"device_ids": [dev], "play": False})   # wake the device
            time.sleep(1)
        _call("PUT", "/me/player/play", params={"device_id": dev}, json=body)
        for _ in range(5):
            time.sleep(0.6)
            if _playing(body):
                return True
    return False


def control(action: str, query: str = "", level: int | None = None) -> str:
    if not client_id():
        return ("Spotify isn't set up yet. Add SPOTIFY_CLIENT_ID to the bridge's .env file, "
                "then open the Spotify login page on the PC. (Tell the user this; don't try other tools.)")
    if not is_connected():
        return ("Spotify isn't connected yet. On this PC, open "
                f"http://127.0.0.1:{os.getenv('PORT', '8787')}/spotify/login and approve access. "
                "(Tell the user this; don't try other tools.)")
    a = (action or "").lower().strip()
    try:
        if a == "play" and query:
            body, desc = _search(query)
            dev = _device_id()
            if not dev:
                return "I couldn't find the Spotify app on any device. Open Spotify and try again."
            if _start(dev, body):
                return f"Playing {desc} on Spotify."
            return (f"I found {desc}, but the Spotify app didn't start playing. "
                    "Press play once in Spotify, then ask me again.")
        if a in ("play", "resume"):
            dev = _device_id()
            if dev and _start(dev):
                return "Resumed Spotify."
            return "Spotify didn't resume. Press play once in the Spotify app."
        if a == "pause":
            _call("PUT", "/me/player/pause")
            return "Paused Spotify."
        if a in ("next", "skip"):
            _call("POST", "/me/player/next")
            return "Skipped to the next track."
        if a in ("previous", "back"):
            _call("POST", "/me/player/previous")
            return "Went back to the previous track."
        if a == "volume" and level is not None:
            _call("PUT", "/me/player/volume", params={"volume_percent": max(0, min(100, int(level)))})
            return f"Spotify volume set to {max(0, min(100, int(level)))} percent."
        if a in ("now_playing", "current", "what"):
            cur = _call("GET", "/me/player/currently-playing")
            item = cur.get("item") if cur else None
            if not item:
                return "Nothing is playing on Spotify right now."
            artists = ", ".join(x["name"] for x in item.get("artists", []))
            return f"Now playing {item['name']} by {artists}."
        return f"I don't know the Spotify action '{action}'."
    except SpotifyError as e:
        if str(e) == "NO_ACTIVE_DEVICE":
            return "Spotify isn't open on any device. Open the Spotify app and try again."
        return str(e)
    except Exception as e:
        return f"Spotify error ({e})."
