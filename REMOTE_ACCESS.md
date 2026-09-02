# Control Hermes from anywhere in the world

Your ESP32 satellite always talks to the brain over your home Wi-Fi — that part
never changes. This guide is about reaching the brain **yourself, from your
phone, when you're away from home**, using the web app at `/app/`.

```
        AT HOME                         ANYWHERE
  ESP32 ── Wi-Fi ── [ Brain (PC) ] ── secure tunnel ── your phone app
                         :8787
```

> ⚠️ **Security first.** The brain can control your PC and your home. The moment
> it's reachable from the internet, the **`HERMES_TOKEN`** in `bridge/.env` is the
> only thing standing between you and anyone who finds the URL. It's already set
> to a strong random value — **keep it secret, never put it in a screenshot, and
> rotate it** (change the value, restart the bridge, update the app + `config.h`)
> if it ever leaks.

Pick one of the two options below.

---

## Option A — Cloudflare Tunnel  (recommended: works for VOICE too)

Gives your brain a public **HTTPS** address with no port-forwarding, works even
behind your ISP's NAT, and — because it's HTTPS — the phone **microphone works**,
so you get full voice from anywhere. Free.

1. **Install cloudflared** (one time):
   ```
   winget install --id Cloudflare.cloudflared
   ```
2. **Start your brain** — double-click `start_bridge.bat`.
3. **Open the tunnel** (new terminal):
   ```
   cloudflared tunnel --url http://localhost:8787
   ```
   It prints a URL like `https://brave-lion-1234.trycloudflare.com`.
4. On your phone, open **`<that-url>/app/`**, tap the gear ⚙, paste your
   `HERMES_TOKEN`, and you're live — text **and** voice, from anywhere.

The quick URL changes each run. For a **permanent** address (`hermes.yourdomain.com`)
set up a *named* tunnel — see Cloudflare's "Tunnel" docs; it's free if you own a
domain.

---

## Option B — Tailscale  (recommended: fully private, no public URL)

Puts your phone and PC on the same private encrypted network, as if they were
always on the same Wi-Fi. Nothing is exposed to the public internet. Free for
personal use.

1. Install **Tailscale** on your PC **and** your phone; sign in with the same
   account on both.
2. On the PC, note its Tailscale IP (looks like `100.x.y.z`).
3. Start your brain (`start_bridge.bat`).
4. On the phone (Tailscale on), open **`http://100.x.y.z:8787/app/`** and paste
   the token.

**Text commands work great.** Phone-mic **voice** needs HTTPS, and a plain
`http://100.x.y.z` address isn't "secure" to the browser — so either use Option A
for voice, or enable HTTPS with `tailscale serve` (see Tailscale docs) and open
the `https://…ts.net` address instead.

---

## Which should I use?
| You want… | Use |
|---|---|
| Voice from anywhere, simplest | **Cloudflare Tunnel** |
| Maximum privacy, nothing public, text mostly | **Tailscale** |
| A permanent custom URL | Cloudflare **named** tunnel |

## Make the brain always available
The brain only answers while the PC is awake. To keep it up:
- Set Windows power plan so the PC doesn't sleep (or "sleep after: never" while plugged in).
- Auto-start on boot: put a shortcut to `start_bridge.bat` in
  `shell:startup` (press Win+R, type `shell:startup`).
- Or run the brain on an always-on device later (old phone via Termux, a Raspberry Pi) — see the blueprint.

## Hardening (optional, recommended once public)
- Trim `pc_action` in `bridge/tools.py` to only the actions you actually want reachable remotely.
- Rotate `HERMES_TOKEN` periodically.
- Prefer Tailscale when you don't specifically need a public link.
