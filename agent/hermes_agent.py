"""
Hermes remote agent
====================
Run this on ANY laptop/PC on your Wi-Fi to let Hermes open apps, websites and
control it from across the network. Standard-library only — screenshots and
window control switch on automatically if Pillow / pywin32 happen to be installed.

SETUP on the other laptop:
  1. Install Python 3 (python.org) if it isn't already.
  2. Copy this one file over.
  3. Run:   python hermes_agent.py
  4. It prints a URL like  http://192.168.x.x:8799  — give that to the brain
     (add it under "computers" in the brain's devices.json).

The token must match the brain's HERMES_TOKEN. Edit TOKEN below or set the
HERMES_TOKEN environment variable. Keep it on your own trusted network.
"""

import os
import re
import json
import socket
import ctypes
import subprocess
import webbrowser
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("HERMES_TOKEN", "CHANGE_ME_TO_YOUR_HERMES_TOKEN")
PORT = int(os.environ.get("HERMES_AGENT_PORT", "8799"))

APP_ALIASES = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc", "paint": "mspaint",
    "explorer": "explorer", "files": "explorer", "file explorer": "explorer",
    "settings": "start ms-settings:", "task manager": "taskmgr", "cmd": "cmd",
    "command prompt": "cmd", "terminal": "wt", "powershell": "powershell",
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge",
    "microsoft edge": "msedge", "firefox": "firefox", "word": "winword",
    "excel": "excel", "powerpoint": "powerpnt", "spotify": "spotify",
    "vscode": "code", "vs code": "code", "code": "code",
}
SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "gmail": "https://mail.google.com", "mail": "https://mail.google.com",
    "maps": "https://maps.google.com", "github": "https://github.com",
    "chatgpt": "https://chat.openai.com", "claude": "https://claude.ai",
    "whatsapp": "https://web.whatsapp.com", "netflix": "https://www.netflix.com",
    "wikipedia": "https://www.wikipedia.org", "instagram": "https://www.instagram.com",
    "twitter": "https://x.com", "x": "https://x.com", "reddit": "https://www.reddit.com",
    "amazon": "https://www.amazon.in", "flipkart": "https://www.flipkart.com",
    "linkedin": "https://www.linkedin.com",
}
_VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
       "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}


def _looks_like_url(s):
    s = s.strip()
    return s.startswith(("http://", "https://")) or bool(re.match(r"^[\w-]+\.[\w.-]+", s))


def open_target(target):
    t = (target or "").strip()
    low = t.lower()
    if low in SITES:
        webbrowser.open(SITES[low]); return f"Opening {low} in the browser."
    if _looks_like_url(t):
        webbrowser.open(t if t.startswith("http") else "https://" + t); return f"Opening {t}."
    if low in ("browser", "web browser", "internet", "web"):
        webbrowser.open("https://www.google.com"); return "Opening the browser."
    cmd = APP_ALIASES.get(low, t)
    try:
        if isinstance(cmd, str) and cmd.startswith("start "):
            subprocess.Popen(cmd, shell=True)
        else:
            os.startfile(cmd)  # type: ignore[attr-defined]
        return f"Opening {t}."
    except Exception:
        try:
            subprocess.Popen(f'cmd /c start "" "{cmd}"', shell=True); return f"Opening {t}."
        except Exception:
            webbrowser.open("https://www.google.com/search?q=" + quote(t))
            return f"Searching the web for '{t}'."


def youtube(q):
    q = (q or "").strip()
    webbrowser.open("https://www.youtube.com/results?search_query=" + quote(q) if q
                    else "https://www.youtube.com")
    return f"Opening YouTube results for '{q}'." if q else "Opening YouTube."


def media(action):
    vk = _VK.get(action)
    if not vk:
        return f"Unknown media action '{action}'."
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
    return f"Done: {action.replace('_', ' ')}."


def close_app(name):
    try:
        import win32gui, win32con
        n = (name or "").lower().strip()
        hits = []
        def cb(h, _):
            if win32gui.IsWindowVisible(h):
                t = win32gui.GetWindowText(h)
                if t and n in t.lower() and "hermes" not in t.lower():
                    hits.append((h, t))
        win32gui.EnumWindows(cb, None)
        for h, _t in hits:
            win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
        return f"Closed {hits[0][1]}." if hits else f"No window matches '{name}'."
    except Exception as e:
        return f"Close needs pywin32 on this PC ({e})."


def switch_window(name):
    try:
        import win32gui, win32con
        n = (name or "").lower().strip()
        hits = []
        def cb(h, _):
            if win32gui.IsWindowVisible(h):
                t = win32gui.GetWindowText(h)
                if t and n in t.lower():
                    hits.append((h, t))
        win32gui.EnumWindows(cb, None)
        if not hits:
            return f"No window matches '{name}'."
        h, t = sorted(hits, key=lambda x: len(x[1]))[0]
        win32gui.ShowWindow(h, win32con.SW_RESTORE); win32gui.SetForegroundWindow(h)
        return f"Switched to {t}."
    except Exception as e:
        return f"Switch needs pywin32 on this PC ({e})."


def screenshot():
    try:
        from PIL import ImageGrab
        import datetime
        folder = os.path.join(os.path.expanduser("~"), "Pictures", "Hermes")
        os.makedirs(folder, exist_ok=True)
        fn = os.path.join(folder, "screen_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".png")
        ImageGrab.grab().save(fn)
        return f"Screenshot saved on this PC: {fn}."
    except Exception as e:
        return f"Screenshot needs Pillow on this PC ({e})."


def do(action, target):
    a = (action or "").lower().strip()
    if a in ("open", "website", "go", "browse", "surf", "launch"):
        return open_target(target)
    if a == "youtube":
        return youtube(target)
    if a in ("close", "quit"):
        return close_app(target)
    if a in ("switch", "focus"):
        return switch_window(target)
    if a == "screenshot":
        return screenshot()
    if a == "lock":
        ctypes.windll.user32.LockWorkStation(); return "Locked."
    if a in _VK:
        return media(a)
    return f"Unknown action '{action}'."


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "host": socket.gethostname()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if TOKEN and self.headers.get("X-Hermes-Token") != TOKEN:
            self._send(401, {"error": "bad token"}); return
        if not self.path.startswith("/do"):
            self._send(404, {"error": "not found"}); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            data = {}
        try:
            result = do(data.get("action", ""), data.get("target", ""))
            self._send(200, {"result": result})
        except Exception as e:
            self._send(500, {"result": f"error: {e}"})

    def log_message(self, *a):
        pass  # quiet


def _ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    ip = _ip()
    print("=" * 58)
    print(f"  Hermes agent on {socket.gethostname()}")
    print(f"  Listening: http://{ip}:{PORT}")
    print(f"  Add to the brain's devices.json under \"computers\":")
    print(f'    "this laptop": {{ "url": "http://{ip}:{PORT}", "token": "<your token>" }}')
    print(f"  Token auth: {'ON' if TOKEN else 'OFF'}")
    print("=" * 58)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
