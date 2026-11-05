# Launch posts (drafts, not posted)

Fill the `[brackets]`, attach the video natively (don't link to YouTube; both platforms push
native video harder), and put the repo link in the first comment on LinkedIn, since posts with
outbound links in the body tend to get less reach. Check the challenge page (gnani.ai/internship)
for the required hashtag/tag and name all three models, which the rules ask for.

---

## LinkedIn

I built a ₹1,500 voice assistant that understands Tamil, and it runs on Gnani.ai's models.

Hold a button on an ESP32, say "லைட்டை ஆன் பண்ணு", and the light turns on. Ask for a song in
Tanglish and Spotify plays it. Ask the Bitcoin price and it reads you the live number.

Under the hood, each stage is a Gnani model:
🎙️ Prisma turns my speech into text, in Indian languages and code-mixed speech
🧠 Evon (gnani-evon-v3.3, open weights) decides what to do and calls tools: web search, PC control, smart home, Spotify, timers
🔊 Timbre speaks the reply back

The ESP32 is only the ears and mouth. The thinking happens on a small FastAPI "brain" on my PC.

The part I'm proudest of is what happens when something breaks. Every stage is a chain of
providers, so if a model times out or an API key expires, the next provider answers and the
device never goes silent. Around that: typed config validation, retries with backoff, rate
limiting, structured logs, a Docker image, and a test suite that mocks every provider in CI.

Built for the #[ChallengeHashtag] by [@Gnani.ai]. 60-second demo below, code in the comments.

What would you make a voice assistant in your language do?

#VoiceAI #IndianLanguages #ESP32 #OpenSource #BuildInPublic

**First comment:** Code (MIT): https://github.com/ROHITCRAFTSYT/Hermes

---

## X / Twitter

**Single post (≤280 chars):**

> Built a ₹1,500 ESP32 voice assistant that takes commands in Tamil 🇮🇳
>
> 🎙️ Gnani Prisma → hears me
> 🧠 Gnani Evon → decides + calls tools
> 🔊 Gnani Timbre → talks back
>
> Lights, Spotify, live prices, timers. Falls back to other providers if one dies.
>
> [@GnaniAI] #[ChallengeHashtag]
> [video]

**Thread version (if the single post gets traction, reply to it):**

1/ The ESP32 only records and plays audio. A FastAPI brain on my PC does the rest: STT → an LLM agent with tool calls → TTS.

2/ Why Gnani: Prisma takes Indian languages and code-mixed speech, which is how people actually talk at home. Whisper kept hearing my short English commands as Hindi.

3/ Evon is open weights (30B MoE, ~3B active). I serve it with vLLM and Hermes talks to it over the OpenAI API, tool calling included.

4/ Every stage is a fallback chain: Prisma → Whisper, Evon → Groq → NVIDIA, Timbre → Edge → offline voice. Pull the plug on one and the device keeps answering. That's in the video.

5/ Open source, tested in CI, Docker-ready: https://github.com/ROHITCRAFTSYT/Hermes

---

## Before you post

- [ ] Watch the video once on a phone with sound off; captions carry the story
- [ ] Every claim in the post appears in the video (Tamil command, Spotify, fallback)
- [ ] If Evon wasn't the live LLM in the recording, change "Evon decides" to match what ran
- [ ] Post around 9–11 am IST on a weekday; reply to every comment in the first hour
- [ ] Deadline: Nov 10, 2026, 11:59 pm IST, per press coverage. Confirm on gnani.ai/internship
