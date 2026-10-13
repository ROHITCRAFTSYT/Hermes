# Hermes — a DIY voice AI agent for your home

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%2B%20ESP32-lightgrey)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![AI](https://img.shields.io/badge/AI-Groq%20%C2%B7%20Whisper%20%C2%B7%20NVIDIA-ff8a41)

Hermes turns a **₹1,500 ESP32** into a hands‑held voice assistant that runs on
your own PC. Hold the button, talk (in **English or Tamil**), and it answers out
loud — and unlike a closed smart speaker, it can **open apps and websites, search
the web, control your PC and media, run smart‑home devices, control other
computers on your network, cast to TVs, and remember your conversation.**

The clever part: the ESP32 is just the *ears and mouth*. All the intelligence
runs in a small Python "brain" on a PC you already own, so the hardware stays
cheap and the AI stays as powerful as you like.

```
   ┌─────────────┐   Wi-Fi    ┌──────────────────────────────────────┐
   │  ESP32 mic  │  ────────► │            The brain (your PC)        │
   │  + button   │   16 kHz   │  Whisper STT → LLM + tools → TTS      │
   │ (powerbank) │  ◄──────── │  ↳ web · PC · smart home · devices    │
   └─────────────┘   reply    └──────────────────────────────────────┘
        speak                         also plays reply on PC speakers
```

> Built and documented as an open maker project. Total build cost **≈ ₹1,500–3,000**;
> the brain runs on a PC you already have.

---

## ✨ Features

- 🎙️ **Push‑to‑talk voice** — press the ESP32's button, speak, release.
- 🌐 **Real web agent** — searches the web and *speaks the answer*; opens
  websites and YouTube in your browser ("surf" mode).
- 🖥️ **PC & media control** — open/close apps, switch windows, play/pause,
  volume, screenshots, lock the PC.
- 🎵 **Spotify** — "play Over My Dead Body" plays that exact song (Premium, one‑time login).
- 📈 **Always current** — knows today's date, searches the web before answering
  anything that changes, and pulls live crypto prices from CoinGecko.
- 🏠 **Smart‑home control** — Tuya bulbs/plugs, custom ESP relay nodes (MQTT),
  Wake‑on‑LAN, HTTP devices, and **Home Assistant** (1000s of devices).
- 📺 **TV / Cast control** — Chromecast / Google Cast / Android TV, no app on the TV.
- 💻 **Control other computers** on your network by name (via a tiny agent).
- 🧠 **Memory** — remembers the last several turns (follow‑ups like "and the
  distance?") and long‑term facts ("remember my router is in the study").
- 🗣️ **Natural, bilingual voice** — auto English **and Tamil**, spoken with
  human‑sounding neural voices (free, no API key).
- 📱 **Phone/web app** — a PWA served by the brain; type or talk from any device.
- 🔒 **Token‑secured**, LAN‑first, with optional **remote access** from anywhere.
- 🔁 **Resilient** — auto‑discovers the brain by name (mDNS), and falls back to a
  second LLM provider (NVIDIA) if Groq's free daily limit is hit.

---

## 🧰 Bill of materials

The **brain runs on your existing PC (₹0)**. You only buy the satellite.

| Component | Notes | Approx ₹ |
|---|---|---|
| **ESP32 DevKit (30‑pin)** | classic ESP‑WROOM‑32, 2.4 GHz | 350–500 |
| **INMP441** I²S MEMS mic | | 150–200 |
| **MAX98357A** I²S amplifier | optional — replies can play on the PC instead | 150–200 |
| **Speaker** 8 Ω 3 W | optional (pairs with the amp) | 80–150 |
| Push button | or just use the onboard **BOOT** button (₹0) | 0–10 |
| Jumper wires + breadboard | | 120–200 |
| USB **data** cable + 5 V source | a power bank makes it fully cordless | 0–150 |

**Optional, to control real devices:** a Tuya smart plug/bulb (~₹400 each), a
spare ESP + relay module for appliance control, a Chromecast/Android TV.

> ⚠️ This is a **classic ESP32** build. It has **no PSRAM** (so no on‑device wake
> word — push‑to‑talk only) and can only join **2.4 GHz, WPA2** networks (not WPA3).

---

## 🔌 Wiring (classic ESP32, 30‑pin)

All six signal wires sit in one run on the bottom edge of the board.
See [`SCHEMATIC_SPEC.md`](SCHEMATIC_SPEC.md) for the full spec.

| INMP441 mic | → ESP32 | | MAX98357A amp | → ESP32 |
|---|---|---|---|---|
| VDD | **3V3** (never 5 V) | | VIN | **VIN** (5 V) |
| GND | GND | | GND | GND |
| L/R | GND | | BCLK | **D25** |
| SCK | **D14** | | LRC | **D33** |
| WS | **D27** | | DIN | **D32** |
| SD | **D26** | | Speaker ± | to the 8 Ω speaker |

- **Button:** the onboard **BOOT** button is the push‑to‑talk (no wiring). An
  external button can go on **D4 → GND** (update `PTT_PIN`).
- **Status LED (optional):** `D2 → 220 Ω → LED → GND`.
- One **GND** is shared by everything; the speaker connects **only** to the amp.

---

## 📁 Repository structure

```
HermesAgent/
├─ README.md                      ← you are here
├─ REMOTE_ACCESS.md               ← reach the brain from anywhere
├─ SCHEMATIC_SPEC.md              ← full wiring spec / BOM
├─ start_bridge.bat               ← double-click to run the brain (Windows)
│
├─ bridge/                        ← the PC "brain" (Python / FastAPI)
│  ├─ hermes_bridge.py            ← STT → agent loop → TTS + web/app server
│  ├─ tools.py                    ← every tool: web, PC, smart home, network…
│  ├─ spotify.py                  ← Spotify login (PKCE) + playback
│  ├─ requirements.txt
│  ├─ .env.example                ← copy to .env, add your Groq key + token
│  ├─ devices.example.json        ← copy to devices.json, add your devices
│  └─ webapp/                     ← the phone/web control app (PWA)
│
├─ firmware/
│  └─ hermes_satellite/           ← flash to the ESP32
│     ├─ hermes_satellite.ino
│     ├─ config.example.h         ← copy to config.h (bridge IP, token, pins)
│     └─ secrets.h.example        ← copy to secrets.h (your Wi-Fi)
│
└─ agent/                         ← run on OTHER PCs so Hermes can control them
   ├─ hermes_agent.py             ← tiny stdlib-only remote agent
   └─ install_agent.bat           ← installs it + auto-start at login
```

---

## 🚀 Setup

### 1 — The brain (your PC)

Requires **Python 3.11+** on Windows (uses Windows TTS + window control).

```bash
cd bridge
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Edit **`.env`**:
- `GROQ_API_KEY` — a free key from https://console.groq.com/keys
- `HERMES_TOKEN` — a secret string; generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(24))"`
- *(optional)* `NVIDIA_API_KEY` — a free https://build.nvidia.com key, used as an
  automatic fallback when Groq's daily limit is reached.

Run it:

```bash
.venv\Scripts\python hermes_bridge.py      # or: double-click start_bridge.bat
```

It prints its LAN IP and starts advertising itself over mDNS. Test in a browser:
`http://localhost:8787/health` → should show `"brain": true`. **Allow the Windows
Firewall prompt** so the ESP32 can reach it.

### 2 — The satellite (ESP32)

Using [`arduino-cli`](https://arduino.github.io/arduino-cli/) (or the Arduino IDE):

```bash
arduino-cli core install esp32:esp32
cd firmware/hermes_satellite
copy config.example.h config.h
copy secrets.h.example secrets.h
```

Edit **`secrets.h`** (your **2.4 GHz, WPA2** Wi‑Fi) and **`config.h`**
(`BRIDGE_TOKEN` must match `HERMES_TOKEN` in `.env`; `BRIDGE_HOST` is just the
fallback IP — mDNS finds the brain automatically). Then flash:

```bash
arduino-cli compile --upload -p COM3 --fqbn esp32:esp32:esp32 .
```

**Hold the BOOT button, speak, release.** The reply plays on the PC (and on the
ESP's speaker if you wired the amp). Power the ESP from any 5 V source — a power
bank makes it cordless; it reconnects and re‑discovers the brain on its own.

### 3 — The phone/web app (optional)

The brain serves a control app — no ESP needed. On any device on your Wi‑Fi open
**`http://<PC-IP>:8787/app/`**, tap ⚙, paste your `HERMES_TOKEN`, then type or
talk. "Add to Home Screen" installs it like a native app. To use it from outside
your home, see [`REMOTE_ACCESS.md`](REMOTE_ACCESS.md).

### 4 — Controlling another computer (optional)

On the *other* PC, run **`agent/install_agent.bat`** once (needs Python). It asks
for your brain's `HERMES_TOKEN` (the agent refuses every command without it),
installs a tiny agent that auto‑starts at login, and prints that PC's IP. Add it
to `bridge/devices.json` under `computers` (with the same token), then say
*"open notepad on the other laptop"*.

### 5 — Spotify playback (optional, needs Premium)

Spotify only allows playing a chosen song through its Web API, which needs a
**Premium** account and a free developer app (one time, ~5 minutes):

1. Go to https://developer.spotify.com/dashboard → **Create app**. Any name and
   description; **Redirect URI** `http://127.0.0.1:8787/spotify/callback` (use
   your `PORT` if you changed it); tick **Web API**; save.
2. Copy the app's **Client ID** into `bridge/.env` as `SPOTIFY_CLIENT_ID=…` and
   restart the brain.
3. On the PC, open **http://127.0.0.1:8787/spotify/login** and click **Agree**.

Login uses PKCE, so there's no client secret; the token is saved in
`bridge/spotify_token.json` (git‑ignored) and refreshes itself. If Spotify isn't
running, Hermes starts the desktop app and plays on it.

---

## 🗣️ Usage — say things like…

| Category | Examples |
|---|---|
| Ask anything | "What's the capital of Japan?" · "Search for today's tech news" |
| Open apps | "open notepad" · "open Spotify" · "open calculator" |
| Web / surf | "open YouTube" · "go to Gmail" · "browse for weather in Chennai" |
| YouTube | "search YouTube for lofi music" |
| PC & media | "pause" · "volume up" · "take a screenshot" · "lock the PC" |
| Spotify | "play Over My Dead Body" · "play artist Anirudh" · "play playlist Today's Top Hits" · "next song" · "what's playing?" |
| Live prices | "what's the price of Bitcoin?" · "how much is Solana?" |
| Windows | "close Chrome" · "switch to Word" |
| Network | "scan the network" · "wake my desktop" · "is the printer online?" |
| Smart home | "turn on the bedroom light" · "turn off the fan plug" |
| Other PCs | "open YouTube on the other laptop" |
| TVs | "pause the TV" · "turn up the living room TV" |
| Memory | "remember my router is in the study" → later "where's my router?" |
| Follow‑ups | "flight from Dubai to India" → "what's the distance?" |
| Tamil | "நோட்பேட் திற" · "பிரான்ஸ் தலைநகரம் எது?" |
| Reset | "new conversation" (clears short‑term memory) |

---

## ⚙️ Configuration reference

- **Voice** — replies use free Microsoft neural voices (the ones behind Edge's
  *Read aloud*): `TTS_VOICE` (default `en-US-AvaMultilingualNeural`) and
  `TTS_VOICE_TA` (default `ta-IN-PallaviNeural`) in `.env`; list all voices with
  `.venv\Scripts\edge-tts --list-voices`. Needs internet — if it's slow or down,
  Hermes falls back to Google's voice, then the offline Windows voice
  (`TTS_ENGINE=sapi` forces offline).
- **`bridge/.env`** — Groq/NVIDIA keys, models, `HERMES_TOKEN`, `PLAY_ON_PC`,
  `SPOTIFY_CLIENT_ID`, `LLM_MAX_TOKENS` / `REASONING_EFFORT`.
- **`firmware/…/config.h`** — `BRIDGE_HOST`/`BRIDGE_TOKEN`, `MAX_RECORD_SECONDS`,
  `MIC_GAIN_SHIFT` (lower = louder), `USE_MDNS`.
- **`bridge/devices.json`** — named devices for control‑by‑voice:
  - `lights` / `plugs` — Tuya (`python -m tinytuya wizard` gets the local keys)
  - `devices` — `wol` (wake a PC), `http` (ESP relay `/on`,`/off`), `ping`
  - `computers` — other PCs running the agent
  - `apps` — custom app launch paths
  - Home Assistant: set `HA_URL` + `HA_TOKEN` (env) → control everything HA finds
    (lights, switches, AC, garage doors, locks, TVs, sensors). Unlocking a lock by
    voice is off unless you also set `HA_ALLOW_UNLOCK=1`.
  - Hermes is told your configured device names on every request, so say them
    as written here (e.g. *"turn on the fan node"*).

---

## 🩺 Troubleshooting

| Symptom | Cause / fix |
|---|---|
| ESP just blinks, no reply | The brain returned an error — usually the LLM daily limit. |
| Spoken *"limit is full"* | Groq's **per‑model** daily token cap. Switch `LLM_MODEL` (e.g. `openai/gpt-oss-20b` → `qwen/qwen3.8-27b`) or set `NVIDIA_API_KEY` for auto‑fallback. |
| Won't join Wi‑Fi (`reason=2/202`) | The network is **WPA3** or 5 GHz. A classic ESP32 needs **WPA2 + 2.4 GHz**. |
| Won't join, "too many devices" (`reason=5`) | The hotspot's **device limit** is full — raise it / free a slot. |
| `"brain": false` on `/health` | `GROQ_API_KEY` missing in `.env`. |
| ESP can't reach bridge | Same Wi‑Fi? Allow the firewall prompt. mDNS blocked on some hotspots → set `BRIDGE_HOST` to the printed IP. |
| Replies come out in the wrong language | Say **"new conversation"** — a prior language instruction can linger in memory. |
| Mic too quiet | Lower `MIC_GAIN_SHIFT` in `config.h` (e.g. 11–13). |
| Words dropped / garbled | Mic is clipping — check the `[Rec] … peak` serial line; raise `MIC_GAIN_SHIFT` (default 16). |
| *"Spotify isn't connected"* | Open `http://127.0.0.1:8787/spotify/login` **on the PC** (it refuses other devices). |
| *"needs a Spotify Premium account"* | Spotify blocks playback control on free accounts — Hermes can still open Spotify and use play/pause media keys. |
| Spotify login says *invalid redirect URI* | The app's Redirect URI must be exactly `http://127.0.0.1:8787/spotify/callback`. |

---

## 🔐 Security notes

- **Secrets never belong in git.** `.env`, `secrets.h`, `config.h`, and
  `devices.json` are `.gitignore`d — commit only the `*.example` templates.
- The brain requires a token (`HERMES_TOKEN`) on every request. **Always set a
  strong token before exposing it beyond your LAN.**
- Controlling another PC requires the agent running there — by design, no device
  can run commands on another without that opt‑in.

---

## 🗺️ Roadmap

- Hands‑free **wake word** ("Hey Hermes") on an ESP32‑**S3** (needs PSRAM).
- **WebSocket streaming** + barge‑in for faster, interruptible replies.
- Wi‑Fi onboarding via a **captive portal** (change networks without reflashing).
- A **local LLM** option (Ollama) for fully offline, no‑limit operation.

---

## 📜 License

MIT — see [`LICENSE`](LICENSE). Built by [ROHITCRAFTSYT](https://github.com/ROHITCRAFTSYT).
Uses Groq (Whisper + LLMs), NVIDIA NIM, Google TTS, and the ESP32 Arduino core.
