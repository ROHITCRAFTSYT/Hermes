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

def _tuya(entry: dict):
    import tinytuya
    dev = tinytuya.Device(entry["id"], entry["ip"], entry["key"])
    dev.set_version(float(entry.get("version", "3.4")))
    dev.set_socketTimeout(3)
    return dev


def control_light(name: str, on: bool = True, brightness: int | None = None) -> str:
    lights = _cfg().get("lights", {})
    if name not in lights:
        have = ", ".join(lights) or "none configured"
        return f"No light called '{name}'. Configured lights: {have}."
    try:
        import tinytuya
        entry = lights[name]
        bulb = tinytuya.BulbDevice(entry["id"], entry["ip"], entry["key"])
        bulb.set_version(float(entry.get("version", "3.4")))
        bulb.set_socketTimeout(3)
        if on:
            bulb.turn_on()
            if brightness is not None:
                bulb.set_brightness_percentage(max(1, min(100, int(brightness))))
        else:
            bulb.turn_off()
        state = "on" if on else "off"
        extra = f" at {brightness}%" if (on and brightness is not None) else ""
        return f"{name} light turned {state}{extra}."
    except Exception as e:
        return f"Couldn't reach the {name} light ({e}). Check it's on the same Wi-Fi and the local key is right."


def control_plug(name: str, on: bool = True) -> str:
    plugs = _cfg().get("plugs", {})
    if name not in plugs:
        have = ", ".join(plugs) or "none configured"
        return f"No plug called '{name}'. Configured plugs: {have}."
    try:
        dev = _tuya(plugs[name])
        dev.set_status(bool(on), 1)
        return f"{name} plug turned {'on' if on else 'off'}."
    except Exception as e:
        return f"Couldn't reach the {name} plug ({e})."


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
            return "\n".join(out)
        return f"Web search failed ({errors[-1] or 'timed out'})." if errors else "No results found."
    except Exception as e:
        return f"Web search failed ({e})."


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
        return f"{key}: {data.get(key, 'nothing stored')}."
    if op == "list":
        return "I remember: " + (", ".join(data) if data else "nothing yet") + "."
    return "Memory op must be set, get, or list."


# ── Windows: close, switch, screenshot ───────────────────────────────────────

_PROTECTED = ("hermes", "program manager")


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


def switch_window(name: str) -> str:
    """Bring the window whose title contains `name` to the front."""
    import win32gui, win32con
    n = (name or "").lower().strip()
    matches = [(h, t) for h, t in _windows() if n in t.lower()]
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
    """Gracefully close the window(s) matching `name` (asks to save if needed)."""
    import win32gui, win32con
    aliases = {"browser": "chrome", "vscode": "visual studio code",
               "vs code": "visual studio code", "files": "file explorer",
               "explorer": "file explorer"}
    n = (name or "").lower().strip()
    needle = aliases.get(n, n)
    closed = []
    for hwnd, title in _windows():
        tl = title.lower()
        if any(p in tl for p in _PROTECTED):
            continue
        if needle in tl:
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
    dev = reg.get((name or "").lower().strip())
    if not dev:
        have = ", ".join(reg) if reg else "none configured yet"
        return (f"I don't have a device named '{name}'. Known devices: {have}. "
                f"Say 'scan the network' to find devices, then add it to devices.json.")
    typ = dev.get("type", "")
    cmd = (command or "status").lower().strip()
    on = cmd in ("on", "turn on", "open", "start", "1", "true", "enable")
    try:
        if typ == "wol":
            return _wake_on_lan(dev["mac"], name)
        if typ == "http":
            import httpx
            url = (dev.get("commands", {}) or {}).get(cmd) or dev.get("url", "")
            if not url:
                return f"No URL set for '{cmd}' on {name}."
            httpx.get(url, timeout=5)
            return f"Sent '{cmd}' to {name}."
        if typ == "light":
            return control_light(name, on)
        if typ == "plug":
            return control_plug(name, on)
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


def ha_list() -> str:
    """List the smart devices Home Assistant has discovered."""
    url, tok = _ha()
    if not url or not tok:
        return ("Home Assistant isn't linked yet. Once you install it and set HA_URL and "
                "HA_TOKEN in .env, it auto-discovers your devices and I control them all.")
    try:
        import httpx
        states = httpx.get(url + "/api/states", headers={"Authorization": f"Bearer {tok}"}, timeout=8).json()
        keep = ("light", "switch", "fan", "media_player", "climate", "cover", "lock")
        names = [s["attributes"].get("friendly_name") for s in states
                 if s["entity_id"].split(".")[0] in keep and s["attributes"].get("friendly_name")]
        return "Home Assistant devices: " + (", ".join(names[:40]) if names else "none found.")
    except Exception as e:
        return f"Home Assistant error ({e})."


def ha_control(entity: str, action: str = "toggle") -> str:
    """Turn a Home Assistant device on/off/toggle by its friendly name."""
    url, tok = _ha()
    if not url or not tok:
        return ("Home Assistant isn't linked yet. Install it, then set HA_URL and HA_TOKEN in .env "
                "— it discovers your bulbs, plugs, TVs, etc. and I control them by name.")
    try:
        import httpx
        headers = {"Authorization": f"Bearer {tok}"}
        states = httpx.get(url + "/api/states", headers=headers, timeout=8).json()
        el = (entity or "").lower().strip()
        ent = None
        for s in states:
            fn = s.get("attributes", {}).get("friendly_name", "").lower()
            if el == fn or el == s["entity_id"] or (el and el in fn):
                ent = s["entity_id"]; break
        if not ent:
            return f"No Home Assistant device named '{entity}'."
        domain = ent.split(".")[0]
        svc = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}.get((action or "toggle").lower(), action)
        httpx.post(f"{url}/api/services/{domain}/{svc}", headers=headers,
                   json={"entity_id": ent}, timeout=8)
        return f"{entity} {svc.replace('_', ' ')}."
    except Exception as e:
        return f"Home Assistant error ({e})."


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
        "description": "List the smart-home devices Home Assistant has discovered on the network.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "ha_control",
        "description": "Turn a smart-home device on/off/toggle via Home Assistant, by its name (bulbs, plugs, fans, TVs, locks, etc.).",
        "parameters": {"type": "object", "properties": {
            "entity": {"type": "string", "description": "device name, e.g. living room light"},
            "action": {"type": "string", "description": "on, off, or toggle"}}, "required": ["entity"]}}},
]

_DISPATCH = {
    "control_light": control_light,
    "control_plug": control_plug,
    "mqtt_command": mqtt_command,
    "pc_action": pc_action,
    "open_website": open_website,
    "youtube": youtube,
    "web_search": web_search,
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
