"""
Hermes tools — the actions the assistant is allowed to take.

Each function returns a short plain-english string that gets fed back to the
LLM (and usually spoken to you). Every tool degrades gracefully: if a device
isn't configured yet, it says so instead of crashing, so the whole assistant
works on first boot and you wire up hardware later.
"""

import os
import re
import json
import ctypes
import subprocess
import time
import webbrowser
from urllib.parse import quote
from pathlib import Path

_HERE = Path(__file__).parent
_CFG_PATH = _HERE / "devices.json"
_MEM_PATH = _HERE / "memory.json"


def _cfg() -> dict:
    if _CFG_PATH.exists():
        try:
            return json.loads(_CFG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


# ── Smart bulbs & plugs (Tuya / Smart Life) ──────────────────────────────────

_ON_WORDS = ("on", "turn on", "turn_on", "switch on", "power on", "open", "start", "1", "true", "enable")
_OFF_WORDS = ("off", "turn off", "turn_off", "switch off", "power off", "close", "stop", "0", "false", "disable")
_FILLER = ("the", "my", "light", "lights", "lamp", "plug", "socket", "switch", "device")


def _match(name: str, registry: dict, partial: bool = True) -> str | None:
    """Find a configured name: exact, then without filler ("porch light" -> "porch"), then (if allowed)
    a unique key that STARTS with what was said ("living" -> "living lamp"). Never the reverse:
    "bedroom AC" must not match the "bedroom" light."""
    keys = {k.lower(): k for k in registry}
    n = " ".join((name or "").lower().split())
    if n in keys:
        return keys[n]
    bare = " ".join(w for w in n.split() if w not in _FILLER)
    if bare in keys:
        return keys[bare]
    if not partial or not bare:
        return None
    hits = [k for lk, k in keys.items() if lk.startswith(bare + " ")]
    return hits[0] if len(hits) == 1 else None


def _tuya(entry: dict, bulb: bool = False):
    import tinytuya
    dev = (tinytuya.BulbDevice if bulb else tinytuya.Device)(entry["id"], entry["ip"], entry["key"])
    dev.set_version(float(entry.get("version", "3.4")))
    dev.set_socketTimeout(3)
    return dev


def _tuya_check(resp):
    # tinytuya reports failures by RETURNING {"Error": ..., "Err": ...} rather than raising.
    if isinstance(resp, dict) and resp.get("Error"):
        raise ConnectionError(resp["Error"])
    return resp


def _tuya_switch(entry: dict, label: str, on: bool, brightness: int | None = None, bulb: bool = False) -> str:
    kind = "light" if bulb else "plug"
    try:
        dev = _tuya(entry, bulb)
        if bulb:
            _tuya_check(dev.turn_on() if on else dev.turn_off())
            if on and brightness is not None:
                _tuya_check(dev.set_brightness_percentage(max(1, min(100, int(brightness)))))
        else:
            _tuya_check(dev.set_status(bool(on), 1))
        extra = f" at {brightness}%" if (on and brightness is not None) else ""
        return f"{label} {kind} turned {'on' if on else 'off'}{extra}."
    except Exception as e:
        return f"Couldn't reach the {label} {kind} ({e}). Check it's on the same Wi-Fi and the local key is right."


def _tuya_status(entry: dict, label: str) -> str:
    try:
        dps = _tuya_check(_tuya(entry).status()).get("dps", {})
        state = dps.get("1", dps.get("20"))   # switch DP: 1 on plugs, 20 on most bulbs
        return f"{label} is {'on' if state else 'off'}." if state is not None else f"{label} is reachable."
    except Exception as e:
        return f"Couldn't reach {label} ({e})."


def control_light(name: str, on: bool = True, brightness: int | None = None) -> str:
    lights = _cfg().get("lights", {})
    key = _match(name, lights)
    if not key:
        return _elsewhere(name, "on" if on else "off", brightness, kind="light") or \
            f"No light called '{name}'. Configured lights: {', '.join(lights) or 'none configured'}."
    return _tuya_switch(lights[key], key, on, brightness, bulb=True)


def control_plug(name: str, on: bool = True) -> str:
    plugs = _cfg().get("plugs", {})
    key = _match(name, plugs)
    if not key:
        return _elsewhere(name, "on" if on else "off", kind="plug") or \
            f"No plug called '{name}'. Configured plugs: {', '.join(plugs) or 'none configured'}."
    return _tuya_switch(plugs[key], key, on)


# Which Home Assistant domains a wrong-tool fallback may reach: a "light" request must never
# open a garage door just because the names overlap.
_FALLBACK_DOMAINS = {"light": ("light", "switch"), "plug": ("switch", "fan", "light"),
                     "device": ("light", "switch", "fan", "climate", "media_player", "cover", "lock")}


def _elsewhere(name: str, command: str, brightness: int | None = None, kind: str = "device") -> str | None:
    """The model picked the wrong tool for this device name — look it up in the other registries."""
    cfg = _cfg()
    on = command in _ON_WORDS
    for partial in (False, True):   # an exact name anywhere beats a partial one somewhere else
        if key := _match(name, cfg.get("lights", {}), partial):
            return _tuya_switch(cfg["lights"][key], key, on, brightness, bulb=True)
        if key := _match(name, cfg.get("plugs", {}), partial):
            return _tuya_switch(cfg["plugs"][key], key, on)
        if key := _match(name, cfg.get("devices", {}), partial):
            return _device(key, cfg["devices"][key], command)
        if not partial and (ent := _ha_find(name, _FALLBACK_DOMAINS[kind])):
            return _ha_call(ent, command)
    return None


# ── Custom ESP32 / ESP8266 nodes over MQTT ───────────────────────────────────

def mqtt_command(node: str, command: str) -> str:
    """Publish to hermes/<node>/cmd — your relay_node.ino listens there."""
    try:
        import paho.mqtt.publish as publish
        mq = _cfg().get("mqtt", {"host": "localhost", "port": 1883})
        publish.single(f"hermes/{node}/cmd", command,
                       hostname=mq.get("host", "localhost"),
                       port=int(mq.get("port", 1883)))
        return f"Sent '{command}' to node '{node}'."
    except Exception as e:
        return f"Couldn't publish to MQTT ({e}). Is the Mosquitto broker running?"


# ── Your PC & media ──────────────────────────────────────────────────────────

_VK = {
    "play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
    "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
}


def _tap(vk: int):
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


# Friendly spoken name -> how to launch it. "start …:" are Windows URI apps.
APP_ALIASES = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc",
    "paint": "mspaint", "explorer": "explorer", "files": "explorer",
    "file explorer": "explorer", "settings": "start ms-settings:",
    "task manager": "taskmgr", "cmd": "cmd", "command prompt": "cmd",
    "terminal": "wt", "powershell": "powershell",
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge",
    "microsoft edge": "msedge", "firefox": "firefox",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "spotify": "spotify", "vscode": "code", "vs code": "code", "code": "code",
    "camera": "start microsoft.windows.camera:", "photos": "start ms-photos:",
    "calendar": "start outlookcal:", "store": "start ms-windows-store:",
}

# Spoken site name -> URL, so "open youtube" opens the real site in the browser.
SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "gmail": "https://mail.google.com", "mail": "https://mail.google.com",
    "maps": "https://maps.google.com", "google maps": "https://maps.google.com",
    "github": "https://github.com", "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai", "whatsapp": "https://web.whatsapp.com",
    "netflix": "https://www.netflix.com", "wikipedia": "https://www.wikipedia.org",
    "instagram": "https://www.instagram.com", "twitter": "https://x.com",
    "x": "https://x.com", "reddit": "https://www.reddit.com",
    "amazon": "https://www.amazon.in", "flipkart": "https://www.flipkart.com",
    "linkedin": "https://www.linkedin.com", "spotify web": "https://open.spotify.com",
}


def _looks_like_url(s: str) -> bool:
    s = s.strip()
    return s.startswith(("http://", "https://")) or bool(re.match(r"^[\w-]+\.[\w.-]+", s))


def open_website(target: str) -> str:
    """Open a website in the default browser — a known site name, a URL, or
    (if it's neither) a Google search for the words. This is 'surf the web'."""
    t = (target or "").strip()
    low = t.lower()
    if low in SITES:
        webbrowser.open(SITES[low])
        return f"Opening {low} in the browser."
    if _looks_like_url(t):
        webbrowser.open(t if t.startswith("http") else "https://" + t)
        return f"Opening {t} in the browser."
    webbrowser.open("https://www.google.com/search?q=" + quote(t))
    return f"Searching the web for '{t}' in the browser."


def youtube(query: str) -> str:
    """Open YouTube search results in the browser."""
    q = (query or "").strip()
    if not q:
        webbrowser.open("https://www.youtube.com")
        return "Opening YouTube."
    webbrowser.open("https://www.youtube.com/results?search_query=" + quote(q))
    return f"Opening YouTube results for '{q}'."


def _launch_app(app: str) -> str:
    key = (app or "").lower().strip()
    # a website name or a URL -> open in browser
    if key in SITES or _looks_like_url(key):
        return open_website(app)
    if key in ("browser", "web browser", "internet", "web"):
        webbrowser.open("https://www.google.com")
        return "Opening the browser."
    cmd = _cfg().get("apps", {}).get(key) or APP_ALIASES.get(key, key)
    try:
        if isinstance(cmd, str) and cmd.startswith("start "):
            subprocess.Popen(cmd, shell=True)
        else:
            os.startfile(cmd)  # type: ignore[attr-defined]
        return f"Opening {app}."
    except Exception:
        try:
            subprocess.Popen(f'cmd /c start "" "{cmd}"', shell=True)
            return f"Opening {app}."
        except Exception:
            # last resort: it's probably not an installed app — search for it
            return open_website(app)


def pc_action(action: str, app: str | None = None, repeat: int = 1) -> str:
    action = (action or "").lower().strip()
    try:
        if action in _VK:
            for _ in range(max(1, min(10, int(repeat)))):
                _tap(_VK[action])
            return f"Done: {action.replace('_', ' ')}."
        if action == "lock":
            ctypes.windll.user32.LockWorkStation()
            return "Locked the PC."
        if action == "open" and app:
            return _launch_app(app)
        return f"I don't know the PC action '{action}'."
    except Exception as e:
        return f"PC action failed ({e})."


# ── The open web ─────────────────────────────────────────────────────────────

SEARCH_TIMEOUT_S = 6


def web_search(query: str, max_results: int = 5) -> str:
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS  # older package name
        from concurrent.futures import ThreadPoolExecutor

        def news():
            try:
                with DDGS() as d:
                    return [f"- [news {str(r.get('date',''))[:10]}] {r.get('title','')}: {r.get('body','')}"
                            for r in d.news(query, max_results=3)]
            except Exception:
                return []

        def text():
            with DDGS() as d:
                return [f"- {r.get('title','')}: {r.get('body','')} ({r.get('href','')})"
                        for r in d.text(query, max_results=max_results)]

        # Both lookups run at once, capped so a slow search engine can't stall the ESP32's
        # 20 s reply window. Dated news goes first so the model sees what's current.
        pool = ThreadPoolExecutor(2)
        futures = [pool.submit(news), pool.submit(text)]
        deadline = time.monotonic() + SEARCH_TIMEOUT_S
        out, errors = [], []
        for f in futures:
            try:
                out += f.result(timeout=max(0.1, deadline - time.monotonic()))
            except Exception as e:
                errors.append(e)
        pool.shutdown(wait=False, cancel_futures=True)
        if out:
            # Said right next to the data: small models otherwise fall back on stale memory.
            return (f"Live search results for '{query}'. Answer ONLY from these — if they name a "
                    "person, price or date, use it even if your memory disagrees:\n" + "\n".join(out))
        return f"Web search failed ({errors[-1] or 'timed out'})." if errors else "No results found."
    except Exception as e:
        return f"Web search failed ({e})."


_COIN_CACHE: dict[str, dict] = {}   # spoken name -> CoinGecko coin, saves a call on repeat questions


def crypto_price(coin: str) -> str:
    """Live cryptocurrency price from CoinGecko's free public API (no key needed).
    Falls back to web_search if CoinGecko is rate-limiting or unreachable."""
    def money(v):
        return f"{v:,.2f}" if v >= 1 else f"{v:.6g}"
    try:
        import httpx
        api = "https://api.coingecko.com/api/v3"
        http = httpx.Client(timeout=6, transport=httpx.HTTPTransport(retries=2))   # retries connection resets

        def get(path, **params):
            r = http.get(api + path, params=params)
            r.raise_for_status()   # 429 when the free tier is busy
            return r.json()

        key = coin.lower().strip()
        c = _COIN_CACHE.get(key)
        if not c:
            # "trump coin" finds nothing, "trump" finds OFFICIAL TRUMP — drop filler words first.
            name = re.sub(r"\b(coin|coins|token|crypto|cryptocurrency|price|the|of|current)\b", " ",
                          coin, flags=re.I).strip() or coin
            hits = []
            for q in dict.fromkeys([name, coin]):
                hits = get("/search", query=q).get("coins", [])
                if hits:
                    break
            if not hits:
                return f"No cryptocurrency found matching '{coin}'."
            c = _COIN_CACHE[key] = hits[0]   # CoinGecko ranks search hits by market cap
        p = get("/simple/price", ids=c["id"], vs_currencies="usd,inr",
                include_24hr_change="true").get(c["id"], {})
        if "usd" not in p:
            raise ValueError("no price in response")
        change = p.get("usd_24h_change")
        return (f"{c['name']} ({c['symbol'].upper()}): ${money(p['usd'])} USD"
                + (f" / ₹{money(p['inr'])} INR" if "inr" in p else "")
                + (f", {change:+.1f}% in the last 24 hours" if change is not None else "")
                + ". Live from CoinGecko.")
    except Exception:
        return web_search(f"{coin} crypto price today")


def timer(action: str, kind: str = "", label: str = "", hours: float = 0, minutes: float = 0,
          seconds: float = 0, at: str = "", repeat: str = "", duration: str = "") -> str:
    import timers as tm
    return tm.handle(action, kind, label, hours, minutes, seconds, at, repeat, duration)


def spotify(action: str, query: str = "", level: int | None = None) -> str:
    import spotify as sp
    return sp.control(action, query, level)


def read_page(url: str, max_chars: int = 2500) -> str:
    try:
        import httpx
        from bs4 import BeautifulSoup
        html = httpx.get(url, follow_redirects=True, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0 HermesAgent"}).text
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ").split())
        return text[:max_chars] if text else "The page had no readable text."
    except Exception as e:
        return f"Couldn't read the page ({e})."


# ── Simple long-term memory ──────────────────────────────────────────────────

def _mem() -> dict:
    if _MEM_PATH.exists():
        try:
            return json.loads(_MEM_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def remember(operation: str, key: str = "", value: str = "") -> str:
    data = _mem()
    op = (operation or "").lower()
    if op == "set":
        data[key] = value
        _MEM_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return f"Noted: {key} = {value}."
    if op == "get":
        # The model rarely repeats the exact key it saved under ("car keys" vs "car_keys_location").
        def norm(s):
            return " ".join(re.sub(r"[_\-]+", " ", (s or "").lower()).split())
        want = norm(key)
        hits = {k: v for k, v in data.items() if want and (want in norm(k) or norm(k) in want)}
        if not hits:
            words = [w for w in want.split() if len(w) > 2]
            hits = {k: v for k, v in data.items() if words and any(w in norm(k) or w in norm(v) for w in words)}
        if not hits:
            return f"{key}: nothing stored."
        return "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in hits.items()) + "."
    if op == "list":
        return "I remember: " + (", ".join(data) if data else "nothing yet") + "."
    return "Memory op must be set, get, or list."


def memory_facts() -> str:
    """Remembered facts for the system prompt, so "where are my keys?" needs no tool call."""
    data = _mem()
    if not data:
        return ""
    facts = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in list(data.items())[-30:])
    return "Facts the user asked you to remember: " + facts[:1500] + "."


# ── Windows: close, switch, screenshot ───────────────────────────────────────

# Never close/switch away these: the bridge's own console (titled after its launch command) and the desktop.
_PROTECTED = ("hermes_bridge", "start_bridge", "hermes bridge", "program manager")


def _windows():
    import win32gui
    out = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if t.strip():
                out.append((hwnd, t))
    win32gui.EnumWindows(cb, None)
    return out


# Spoken app name -> executable name, for apps whose window title doesn't say their name
# (Spotify shows the song, VS Code shows the file, Explorer shows the folder).
_APP_EXE = {"google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge",
            "vscode": "code", "vs code": "code", "visual studio code": "code",
            "files": "explorer", "file explorer": "explorer", "explorer": "explorer",
            "word": "winword", "powerpoint": "powerpnt", "teams": "ms-teams"}
_BROWSERS = ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "comet", "arc", "zen",
             "chromium", "thorium", "librewolf", "waterfox", "yandex", "iexplore")


def _exe_of(hwnd) -> str:
    """Lower-case executable name (e.g. 'spotify') of the process that owns a window."""
    import win32process
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf, size = ctypes.create_unicode_buffer(520), ctypes.c_ulong(520)
            ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return Path(buf.value).stem.lower() if ok else ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def _app_windows(name: str) -> list[tuple[int, str]]:
    """Windows belonging to the app the user named: by program first, then by title.
    Title matches skip browsers — closing one closes every tab, not just the matching one."""
    n = re.sub(r"\b(the|my|app|application|window|windows|program)\b", " ", (name or "").lower()).strip()
    n = " ".join(n.split())
    exe = _APP_EXE.get(n, n.replace(" ", ""))
    wins = [(h, t, _exe_of(h)) for h, t in _windows() if not any(p in t.lower() for p in _PROTECTED)]
    if n in ("browser", "web browser"):   # whichever browser is open
        return [(h, t) for h, t, e in wins if e in _BROWSERS]
    by_program = [(h, t) for h, t, e in wins if e and e == exe]
    if by_program:
        return by_program
    return [(h, t) for h, t, e in wins if n and n in t.lower() and e not in _BROWSERS]


def switch_window(name: str) -> str:
    """Bring the named app's window (or a window whose title contains `name`) to the front."""
    import win32gui, win32con
    matches = _app_windows(name)
    if not matches:
        return f"No open window matches '{name}'."
    hwnd, title = sorted(matches, key=lambda x: len(x[1]))[0]
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return f"Switched to {title}."
    except Exception:
        return f"Brought {title} forward."


def close_app(name: str) -> str:
    """Gracefully close the named app's window(s) (the app may ask to save first)."""
    import win32gui, win32con
    closed = []
    for hwnd, title in _app_windows(name):
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            closed.append(title)
        except Exception:
            pass
    if not closed:
        return f"No open window matches '{name}'."
    return f"Closed {closed[0]}." if len(closed) == 1 else f"Closed {len(closed)} '{name}' windows."


def take_screenshot() -> str:
    """Capture the screen to the Pictures/Hermes folder."""
    try:
        from PIL import ImageGrab
        import datetime
        folder = Path(os.path.expanduser("~")) / "Pictures" / "Hermes"
        folder.mkdir(parents=True, exist_ok=True)
        fn = folder / ("screen_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".png")
        ImageGrab.grab().save(fn)
        return f"Screenshot saved to {fn}."
    except Exception as e:
        return f"Couldn't take a screenshot ({e})."


# ── Network devices: discover & control by name ──────────────────────────────

def scan_network() -> str:
    """List devices currently on the local network (from the ARP table)."""
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10).stdout
        found = []
        for line in out.splitlines():
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s+([\da-fA-F]{2}[-:][\da-fA-F]{2}[-:.\da-fA-F]+)\s+dynamic", line)
            if m:
                found.append((m.group(1), m.group(2)))
        if not found:
            return "No other active devices found on the network."
        named = _cfg().get("devices", {})
        by_ip = {v.get("ip"): k for k, v in named.items() if v.get("ip")}
        lines = [f"{ip}  {mac}" + (f"  ({by_ip[ip]})" if ip in by_ip else "") for ip, mac in found[:25]]
        return f"Found {len(found)} devices on the network:\n" + "\n".join(lines)
    except Exception as e:
        return f"Network scan failed ({e})."


def _wake_on_lan(mac: str, name: str) -> str:
    import socket
    mb = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + mb * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.sendto(pkt, ("255.255.255.255", 9))
    s.close()
    return f"Sent a wake-up signal to {name}."


def control_device(name: str, command: str = "status") -> str:
    """Control a named device on the LAN using the 'devices' registry in devices.json.
    Each device has a type: wol (wake a PC), http (call a URL, e.g. an ESP relay),
    light/plug (Tuya), mqtt (a custom ESP node), or ping."""
    reg = _cfg().get("devices", {})
    cmd = (command or "status").lower().strip()
    key = _match(name, reg)
    if not key:
        found = _elsewhere(name, cmd)
        if found:
            return found
        have = ", ".join(reg) if reg else "none configured yet"
        return (f"I don't have a device named '{name}'. Known devices: {have}. "
                f"Say 'scan the network' to find devices, then add it to devices.json.")
    return _device(key, reg[key], cmd)


def _device(name: str, dev: dict, cmd: str) -> str:
    typ = dev.get("type", "")
    on = cmd in _ON_WORDS
    try:
        if typ == "wol":
            return _wake_on_lan(dev["mac"], name)
        if typ == "http":
            import httpx
            cmds = dev.get("commands", {}) or {}
            canon = "on" if cmd in _ON_WORDS else "off" if cmd in _OFF_WORDS else cmd   # "turn on" -> "on"
            url = cmds.get(cmd) or cmds.get(canon) or dev.get("url", "")
            if not url:
                return f"No URL set for '{cmd}' on {name}."
            r = httpx.get(url, timeout=5)
            if r.status_code >= 400:
                return f"{name} answered with an error ({r.status_code})."
            return f"Sent '{cmd}' to {name}."
        if typ in ("light", "plug"):   # Tuya entries in the devices registry carry their own id/ip/key
            if cmd == "status":
                return _tuya_status(dev, name)
            return _tuya_switch(dev, name, on, bulb=(typ == "light"))
        if typ == "mqtt":
            return mqtt_command(dev.get("node", name), cmd)
        if typ == "ping" or cmd == "status":
            ip = dev.get("ip", "")
            if not ip:
                return f"No IP known for {name}."
            r = subprocess.run(["ping", "-n", "1", "-w", "1000", ip], capture_output=True, text=True)
            return f"{name} is {'online' if 'TTL=' in r.stdout else 'offline'}."
        return f"Device '{name}' has an unknown type '{typ}'."
    except Exception as e:
        return f"Couldn't control {name} ({e})."


def remote_pc(computer: str, action: str, target: str = "") -> str:
    """Open an app/website or control ANOTHER computer on the LAN that is running
    hermes_agent.py. Looks the computer up by name in devices.json 'computers'."""
    reg = _cfg().get("computers", {})
    c = reg.get((computer or "").lower().strip())
    if not c:
        have = ", ".join(reg) if reg else "none configured"
        return (f"No computer named '{computer}'. Known computers: {have}. Run hermes_agent.py "
                f"on it and add its URL to devices.json under 'computers'.")
    try:
        import httpx
        r = httpx.post(c["url"].rstrip("/") + "/do",
                       json={"action": action, "target": target},
                       headers={"X-Hermes-Token": c.get("token", "")}, timeout=8)
        if r.status_code == 200:
            return r.json().get("result", "Done.")
        if r.status_code == 401:
            return f"{computer} rejected the access token — make its token match the brain's."
        return f"{computer}'s agent returned status {r.status_code}."
    except Exception as e:
        return f"Couldn't reach {computer} ({e}). Is hermes_agent.py running on it, on this Wi-Fi?"


# ── TVs / media (Chromecast / Google Cast) — NO install on the TV ────────────

def _get_casts(timeout: int = 8):
    import pychromecast
    ccs, browser = pychromecast.get_chromecasts(timeout=timeout)
    pychromecast.discovery.stop_discovery(browser)
    return ccs


def discover_cast() -> str:
    """List Chromecast / Google Cast / Android TV devices on the network."""
    try:
        names = [c.cast_info.friendly_name for c in _get_casts()]
        return "Cast/TV devices found: " + (", ".join(names) if names else "none on this network.")
    except Exception as e:
        return f"Cast discovery failed ({e})."


def cast_control(device: str = "", action: str = "pause", media: str = "") -> str:
    """Control a Cast TV: play, pause, stop, volume_up, volume_down, mute, or cast a media URL."""
    try:
        ccs = _get_casts()
        if not ccs:
            return "No Cast/TV devices found on this network."
        dl = (device or "").lower().strip()
        cc = next((c for c in ccs if dl in c.cast_info.friendly_name.lower()), None) if dl else ccs[0]
        if not cc:
            return f"No TV named '{device}'. Found: " + ", ".join(c.cast_info.friendly_name for c in ccs)
        cc.wait()
        a = (action or "").lower().strip()
        vol = cc.status.volume_level or 0.0
        if a in ("play", "resume"):
            cc.media_controller.play(); return f"Playing on {cc.cast_info.friendly_name}."
        if a == "pause":
            cc.media_controller.pause(); return "Paused."
        if a == "stop":
            cc.media_controller.stop(); return "Stopped."
        if a in ("volume_up", "louder"):
            cc.set_volume(min(1.0, vol + 0.1)); return "Turned it up."
        if a in ("volume_down", "quieter"):
            cc.set_volume(max(0.0, vol - 0.1)); return "Turned it down."
        if a == "mute":
            cc.set_volume_muted(True); return "Muted."
        if a in ("cast", "play_url") and media:
            cc.media_controller.play_media(media, "video/mp4")
            cc.media_controller.block_until_active()
            return f"Casting to {cc.cast_info.friendly_name}."
        return f"Unknown TV action '{action}'."
    except Exception as e:
        return f"TV control failed ({e})."


# ── Home Assistant hub — the real 'control everything' path ──────────────────

def _ha():
    return os.environ.get("HA_URL", "").rstrip("/"), os.environ.get("HA_TOKEN", "")


_HA_CONTROLLABLE = ("light", "switch", "fan", "media_player", "climate", "cover", "lock", "scene", "script")
_HA_CACHE = {"at": 0.0, "states": None}

# Spoken action -> HA service, per domain. Anything not listed falls back to the generic map.
_HA_SERVICES = {
    "cover": {"on": "open_cover", "open": "open_cover", "off": "close_cover", "close": "close_cover",
              "stop": "stop_cover", "toggle": "toggle"},
    "lock": {"lock": "lock", "on": "lock", "close": "lock", "unlock": "unlock"},
    "media_player": {"play": "media_play", "pause": "media_pause", "next": "media_next_track",
                     "previous": "media_previous_track", "volume_up": "volume_up", "volume_down": "volume_down"},
    "scene": {"on": "turn_on", "activate": "turn_on", "run": "turn_on"},
    "script": {"on": "turn_on", "run": "turn_on", "start": "turn_on"},
}
_HA_GENERIC = {"on": "turn_on", "turn_on": "turn_on", "open": "turn_on", "start": "turn_on", "enable": "turn_on",
               "off": "turn_off", "turn_off": "turn_off", "close": "turn_off", "stop": "turn_off",
               "disable": "turn_off", "toggle": "toggle"}
_HA_DONE = {"turn_on": "turned on", "turn_off": "turned off", "toggle": "toggled", "open_cover": "opening",
            "close_cover": "closing", "stop_cover": "stopped", "lock": "locked", "unlock": "unlocked",
            "media_play": "playing", "media_pause": "paused"}


def _ha_states(max_age: float = 60, timeout: float = 8) -> list | None:
    """Home Assistant's entity list, cached briefly (it's also used to build every prompt)."""
    url, tok = _ha()
    if not url or not tok:
        return None
    if time.time() - _HA_CACHE["at"] < max_age:
        return _HA_CACHE["states"]
    try:
        import httpx
        states = httpx.get(url + "/api/states", headers={"Authorization": f"Bearer {tok}"}, timeout=timeout).json()
    except Exception:
        states = None   # cache the failure too, so a down HA doesn't add a delay to every request
    _HA_CACHE.update(at=time.time(), states=states)
    return states


_HA_SENSITIVE = ("cover", "lock")   # garage doors, gates, door locks: never matched on a partial name


def _ha_find(name: str, domains: tuple | None = None) -> dict | None:
    states = [s for s in (_ha_states() or []) if not domains or s["entity_id"].split(".")[0] in domains]
    n = " ".join((name or "").lower().split())
    bare = " ".join(w for w in n.split() if w not in ("the", "my"))
    def fn(s):
        return (s.get("attributes", {}).get("friendly_name") or "").lower()
    def safe(s):
        return s["entity_id"].split(".")[0] not in _HA_SENSITIVE
    for test in (lambda s: n in (fn(s), s["entity_id"]), lambda s: fn(s) == bare,
                 lambda s: bare and safe(s) and fn(s).startswith(bare),
                 lambda s: bare and safe(s) and bare in fn(s)):
        hits = [s for s in states if test(s)]
        if hits:   # prefer things that can be controlled over sensors with similar names
            return sorted(hits, key=lambda s: s["entity_id"].split(".")[0] not in _HA_CONTROLLABLE)[0]
    return None


def _ha_call(ent: dict, action: str) -> str:
    url, tok = _ha()
    eid = ent["entity_id"]
    domain = eid.split(".")[0]
    label = ent.get("attributes", {}).get("friendly_name") or eid
    a = (action or "toggle").lower().strip().replace(" ", "_")
    if a in ("status", "state"):
        unit = ent.get("attributes", {}).get("unit_of_measurement", "")
        return f"{label} is {ent.get('state')}{(' ' + unit) if unit else ''}."
    svc = _HA_SERVICES.get(domain, {}).get(a) or _HA_GENERIC.get(a) or a
    if svc == "unlock" and os.environ.get("HA_ALLOW_UNLOCK", "") != "1":
        return (f"For safety I don't unlock {label} by voice. "
                "Set HA_ALLOW_UNLOCK=1 in .env if you really want that.")
    try:
        import httpx
        r = httpx.post(f"{url}/api/services/{domain}/{svc}", headers={"Authorization": f"Bearer {tok}"},
                       json={"entity_id": eid}, timeout=8)
        if r.status_code >= 400:
            try:
                why = r.json().get("message", "")
            except Exception:
                why = r.text[:100]
            return f"Home Assistant couldn't {a.replace('_', ' ')} {label} ({why or r.status_code})."
        _HA_CACHE["at"] = 0.0   # state changed; refresh next time
        return f"{label} {_HA_DONE.get(svc, svc.replace('_', ' '))}."
    except Exception as e:
        return f"Home Assistant error ({e})."


def ha_list() -> str:
    """List the smart devices (and sensor readings) Home Assistant has discovered."""
    if not all(_ha()):
        return ("Home Assistant isn't linked yet. Once you install it and set HA_URL and "
                "HA_TOKEN in .env, it auto-discovers your devices and I control them all.")
    states = _ha_states(max_age=0)
    if states is None:
        return "Couldn't reach Home Assistant. Check HA_URL and that it's running."
    names = [f"{s['attributes']['friendly_name']} ({s['state']})" for s in states
             if s["entity_id"].split(".")[0] in _HA_CONTROLLABLE + ("sensor",) and s["attributes"].get("friendly_name")]
    return "Home Assistant devices: " + (", ".join(names[:40]) if names else "none found.")


def ha_control(entity: str, action: str = "toggle") -> str:
    """Control a Home Assistant device by its friendly name: on, off, toggle, open, close, lock,
    unlock, play, pause, status..."""
    if not all(_ha()):
        return ("Home Assistant isn't linked yet. Install it, then set HA_URL and HA_TOKEN in .env "
                "— it discovers your bulbs, plugs, TVs, etc. and I control them by name.")
    ent = _ha_find(entity)
    if not ent:
        if _ha_states() is None:
            return "Couldn't reach Home Assistant. Check HA_URL and that it's running."
        return f"No Home Assistant device named '{entity}'."
    return _ha_call(ent, action)


def inventory() -> str:
    """The user's configured devices, one group per tool, for the system prompt — so the model
    uses real names and picks the right integration instead of guessing."""
    cfg = _cfg()
    kinds = {"wol": "wake-on-LAN PC", "http": "relay", "light": "light", "plug": "plug",
             "mqtt": "ESP node", "ping": "online check only"}
    parts = []
    if cfg.get("lights"):
        parts.append("lights → control_light: " + ", ".join(cfg["lights"]))
    if cfg.get("plugs"):
        parts.append("plugs → control_plug: " + ", ".join(cfg["plugs"]))
    if cfg.get("devices"):
        parts.append("network devices → control_device: " + ", ".join(
            f"{k} ({kinds.get(v.get('type'), v.get('type'))})" for k, v in cfg["devices"].items()))
    if cfg.get("computers"):
        parts.append("other computers → remote_pc: " + ", ".join(cfg["computers"]))
    states = _ha_states(timeout=2) or []
    ha = [f"{s['attributes']['friendly_name']} ({s['entity_id'].split('.')[0]})" for s in states
          if s["entity_id"].split(".")[0] in _HA_CONTROLLABLE + ("sensor",) and s["attributes"].get("friendly_name")]
    if ha:
        parts.append("Home Assistant → ha_control: " + ", ".join(ha[:40]))
    if not parts:
        return ""
    return "The user's devices — use these exact names with the tool shown: " + "; ".join(parts) + "."


# ── Registry: schemas the LLM sees, and the dispatcher ───────────────────────

TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "control_light",
        "description": "Turn a smart light on/off and optionally set brightness (0-100).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "room/label of the light, e.g. bedroom"},
            "on": {"type": "boolean"},
            "brightness": {"type": "integer"}}, "required": ["name", "on"]}}},
    {"type": "function", "function": {
        "name": "control_plug",
        "description": "Turn a smart plug (appliance) on or off.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "on": {"type": "boolean"}}, "required": ["name", "on"]}}},
    {"type": "function", "function": {
        "name": "mqtt_command",
        "description": "Send a command string to one of the user's custom ESP boards (relay/fan/sensor node).",
        "parameters": {"type": "object", "properties": {
            "node": {"type": "string"}, "command": {"type": "string"}}, "required": ["node", "command"]}}},
    {"type": "function", "function": {
        "name": "pc_action",
        "description": "Control this Windows PC. actions: play_pause, next, previous, stop, volume_up, volume_down, mute, lock, and open (launch a desktop app by name via 'app', e.g. notepad, calculator, spotify, chrome). For opening WEBSITES or searching in the browser, prefer open_website instead.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"}, "app": {"type": "string"},
            "repeat": {"type": "integer"}}, "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "open_website",
        "description": "Open a website VISIBLY in the user's browser on their PC — a known site (youtube, gmail, github, google, amazon, etc.), a URL, or a Google search for the words. Use this when the user says 'open', 'go to', 'browse', 'surf', or 'take me to' a site. This is for showing things on screen, NOT for answering questions.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "site name, URL, or search words"}}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "youtube",
        "description": "Open YouTube search results in the browser for a query (e.g. a song, video, or topic).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Silently search the web and READ results to ANSWER a question (facts, prices, news) out loud. Does NOT open a browser window. Use this when the user asks a question, not when they say 'open' or 'browse'. Your own knowledge is out of date: ALWAYS use this for anything that can change (who holds an office or job, news, elections, prices, scores, weather, anything 'current' or 'latest') and trust its results over your memory.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "crypto_price",
        "description": "Get the LIVE price of a cryptocurrency or crypto token (Bitcoin, Ethereum, Solana, Dogecoin, Trump coin / OFFICIAL TRUMP, etc.) in USD and INR, with the 24-hour change. Always use this instead of web_search for crypto prices.",
        "parameters": {"type": "object", "properties": {
            "coin": {"type": "string", "description": "coin name or ticker, e.g. 'solana', 'BTC', 'trump'"}}, "required": ["coin"]}}},
    {"type": "function", "function": {
        "name": "timer",
        "description": "Timers, reminders and alarms: 'set a pasta timer for 8 minutes', 'remind me in 20 minutes to check the oven', 'remind me at 6 pm to call mom', 'wake me up at 7 every day', 'how long is left on my timer', 'what reminders do I have', 'cancel the pasta timer'. For a delay put the numbers the user said in hours/minutes/seconds (don't convert units). For a clock time use 'at', e.g. '18:00', '6:30 pm', 'tomorrow 9:00', '2026-10-20 09:00'.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "description": "set, list, or cancel"},
            "kind": {"type": "string", "description": "timer (counts down), reminder (to do something), or alarm (wake up)"},
            "label": {"type": "string", "description": "what it's for, e.g. 'pasta' or 'call mom'; for cancel/list, which one"},
            "hours": {"type": "number"}, "minutes": {"type": "number"}, "seconds": {"type": "number"},
            "at": {"type": "string", "description": "clock time, e.g. '18:00', '6 pm', 'tomorrow 7:00'"},
            "repeat": {"type": "string", "description": "'daily' for every day, else leave empty"}},
            "required": ["action", "kind"]}}},
    {"type": "function", "function": {
        "name": "spotify",
        "description": "Play music on Spotify and control it. Use this whenever the user asks to play a song, artist, album or playlist (on Spotify or just 'play <song>'). actions: play (with 'query' = song/artist name; prefix with 'album ', 'artist ' or 'playlist ' for those), resume, pause, next, previous, volume (with 'level' 0-100), now_playing.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "description": "play, resume, pause, next, previous, volume, now_playing"},
            "query": {"type": "string", "description": "what to play, e.g. 'Over My Dead Body Drake' or 'artist Anirudh'"},
            "level": {"type": "integer", "description": "volume 0-100, for action volume"}}, "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "read_page",
        "description": "Fetch a URL and return its readable text so you can answer from it.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "remember",
        "description": "Long-term memory. operation: set/get/list, plus key and value.",
        "parameters": {"type": "object", "properties": {
            "operation": {"type": "string"}, "key": {"type": "string"},
            "value": {"type": "string"}}, "required": ["operation"]}}},
    {"type": "function", "function": {
        "name": "close_app",
        "description": "Close/quit an open app or window by name (e.g. 'close chrome', 'close notepad').",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "switch_window",
        "description": "Bring an already-open app/window to the front (e.g. 'switch to chrome', 'go to word').",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "take_screenshot",
        "description": "Take a screenshot of the PC screen and save it.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "scan_network",
        "description": "List the devices currently connected to the same local network (IP and MAC).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "control_device",
        "description": "Control a named device on the local network — wake a PC, toggle an ESP relay, a smart light/plug, or check if it's online. Use the device's spoken name.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "the device's name"},
            "command": {"type": "string", "description": "e.g. on, off, wake, status"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "remote_pc",
        "description": "Open an app or website, or control ANOTHER computer/laptop on the same network by name (e.g. 'open notepad on the other laptop', 'open youtube on Rohit's laptop'). That computer must be running the Hermes agent.",
        "parameters": {"type": "object", "properties": {
            "computer": {"type": "string", "description": "name of the other computer"},
            "action": {"type": "string", "description": "open, close, switch, youtube, screenshot, lock, volume_up, play_pause, etc."},
            "target": {"type": "string", "description": "app/website/query for the action, if any"}}, "required": ["computer", "action"]}}},
    {"type": "function", "function": {
        "name": "discover_cast",
        "description": "Find Chromecast / Google Cast / Android TV devices on the network.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "cast_control",
        "description": "Control a TV / Chromecast on the network (no app needed on the TV): play, pause, stop, volume_up, volume_down, mute, or cast a media URL.",
        "parameters": {"type": "object", "properties": {
            "device": {"type": "string", "description": "TV name (optional; blank = first found)"},
            "action": {"type": "string"}, "media": {"type": "string", "description": "media URL to cast, if action is cast"}}, "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "ha_list",
        "description": "List the smart-home devices Home Assistant has discovered, with their current state and sensor readings.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "ha_control",
        "description": "Control a Home Assistant device by its name (lights, switches, fans, AC, covers/garage doors, locks, TVs, scenes) or read a sensor.",
        "parameters": {"type": "object", "properties": {
            "entity": {"type": "string", "description": "device name, e.g. Kitchen Light, Garage Door"},
            "action": {"type": "string", "description": "on, off, toggle, open, close, lock, unlock, play, pause, or status (read its state/sensor value)"}}, "required": ["entity"]}}},
]

_DISPATCH = {
    "control_light": control_light,
    "control_plug": control_plug,
    "mqtt_command": mqtt_command,
    "pc_action": pc_action,
    "open_website": open_website,
    "youtube": youtube,
    "web_search": web_search,
    "crypto_price": crypto_price,
    "spotify": spotify,
    "timer": timer,
    "read_page": read_page,
    "remember": remember,
    "close_app": close_app,
    "switch_window": switch_window,
    "take_screenshot": take_screenshot,
    "scan_network": scan_network,
    "control_device": control_device,
    "remote_pc": remote_pc,
    "discover_cast": discover_cast,
    "cast_control": cast_control,
    "ha_list": ha_list,
    "ha_control": ha_control,
}


def dispatch(name: str, args: dict) -> str:
    fn = _DISPATCH.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return str(fn(**args))
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except Exception as e:
        return f"{name} error: {e}"
