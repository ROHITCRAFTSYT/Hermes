# Hermes × Gnani demo video (60–90 s)

What the video has to get across: a ₹1,500 ESP32 that you hold and talk to in an Indian language,
running Gnani's Prisma, Evon and Timbre, and actually doing things on the PC and in the house.
Show the real hardware and real responses, and don't speed up the waits. Viewers notice cuts in
the latency, and a real 2-second pause is more convincing than a fake instant reply.

**Format:** vertical 9:16 for Reels/Shorts/X, 1080×1920. Record the screen and camera separately
and cut them together. Burn in captions: most people watch muted.

## Before you record

- [ ] `.env`: `STT_PROVIDER=gnani`, `TTS_PROVIDER=gnani`, `LLM_PROVIDER=evon`, `GNANI_API_KEY` set,
      `EVON_BASE_URL` pointing at your vLLM server (or keep Groq as the LLM and say so on screen)
- [ ] `GNANI_STT_LANGUAGE` matches the language you'll speak in each take (restart between takes
      if you switch, e.g. `ta-IN` for the Tamil shot)
- [ ] For Tamil/Hindi *replies*, use `GNANI_TTS_MODEL=timbre-v2.5` (the model that takes a
      language) with `GNANI_TTS_LANGUAGE=auto`, and pick a voice from Gnani's v2.5 voice list.
      The default `timbre-v2.0` voices (Pranav etc.) are only confirmed for v2.0. Test this first
- [ ] `curl localhost:8787/health` shows gnani/evon first in every chain; keep that terminal handy
- [ ] Run every command 3 times off camera first. Delete `bridge/conversation.json` before the final take
- [ ] Phone on Do Not Disturb, quiet room, ESP32 charged, PC volume up
- [ ] Spotify open and logged in, a smart plug/bulb reachable if you have one

## Shot list

| # | Time | Shot | Audio / on-screen text |
|---|---|---|---|
| 1 | 0:00–0:04 | **Hook.** Close-up: your hand presses the ESP32 button. Cut straight to the reply playing | You (Tamil or Hindi): a short command, e.g. *"லைட்டை ஆன் பண்ணு"*. Text: **"₹1,500 voice assistant. Indian languages. Gnani AI."** |
| 2 | 0:04–0:12 | Wide shot: the bulb/plug actually turns on (or Spotify starts playing) | Timbre's reply audible. Text: *"It understood Tamil and did it."* |
| 3 | 0:12–0:22 | Screen: architecture diagram from the README, zoom on the three Gnani boxes | Voiceover: *"Prisma hears me, Evon decides what to do, Timbre talks back. All Gnani models."* |
| 4 | 0:22–0:35 | Back to hardware. Ask a live question in English: *"What's the price of Bitcoin right now?"* | Real reply. Text: *"Live data via tools, not memory."* |
| 5 | 0:35–0:47 | Code-switch: *"Spotify-la Anirudh song play pannu"* (or Hinglish with `en-IN,hi-IN`) | Song starts. Text: *"Code-mixed speech, like we actually talk"* |
| 6 | 0:47–0:60 | **Resilience shot.** Split screen: stop the Evon server (Ctrl+C), ask again, it still answers. Then show the log line `llm provider failed provider=evon` and `/health` | Text: *"Provider dies → automatic fallback. Never leaves the device hanging."* |
| 7 | 0:60–0:72 | Quick montage, 1 s each: phone web app, timer set by voice, GitHub repo with green CI badge, `pytest` passing | Text: *"Open source · tested · Docker-ready"* |
| 8 | 0:72–0:85 | **CTA.** Face to camera holding the device | *"Built for the Gnani Great Indian AI Internship Challenge. Repo link below. Tell me what you'd make it do."* |

Cut shot 6 or 7 if you're over 90 s. Don't cut 1, 2 or 8.

## Things that will hurt the video

- Opening on a logo, a title card or "Hi guys". The first 2 seconds have to be the device doing something.
- Claiming numbers you didn't measure (latency, accuracy). If you show a latency, show a timer on screen.
- Implying Evon runs on the ESP32. It runs on a server; the ESP32 only records and plays audio.
- Hiding the fallback. Showing it working is the most "production" thing in the video.
