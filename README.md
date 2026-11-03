# J.A.R.V.I.S. — your personal AI assistant (Gemini-powered)

A refined British assistant that runs on **your computer** and connects to **your Android phone**.
He reads and edits your Google Calendar, reads, summarises and answers your Gmail, keeps track of your
projects and to-do list, and helps you plan your day. You can talk to him or type.

```
 ┌──────────── your computer ────────────┐          ┌──── Android phone ────┐
 │  JARVIS server (Python)               │  Wi-Fi / │  Installed web app     │
 │   ├─ Gemini brain + tool calling      │◄────────►│  (same dashboard,      │
 │   ├─ Google Calendar + Gmail (OAuth)  │ Tailscale│   voice + text)        │
 │   ├─ Projects / tasks / memory (SQLite)│          └────────────────────────┘
 │   └─ Dashboard (desktop app window)   │
 └───────────────────────────────────────┘
```

## What he can do

| Area | Examples |
|---|---|
| **Calendar** | “What's on tomorrow?” · “Book a meeting with sam@acme.com Thursday afternoon with a Meet link” · “Move my 3pm to 5” · “Cancel the dentist” · “When am I free on Friday?” |
| **Email** | “Summarise my unread emails” · “Anything urgent from Sarah?” · “Reply to the invoice email saying we'll pay Friday” · “Draft a follow-up to the investors” · “Archive the newsletters” |
| **Projects** | “New project: website relaunch, due 1 December, high priority” · “Add a task to write the homepage copy, 2 hours” · “I finished the wireframes” · “How are my projects going?” |
| **Planning** | “Plan my day” — he combines free calendar slots, deadlines, priorities and estimates, proposes a timed plan and puts focus blocks in your calendar once you agree. |
| **Memory** | “Remember that I prefer meetings after 10am” · “Remember Pepper is my business partner” — remembered in every conversation. |
| **Proactive** | Spoken reminder 10 minutes before each meeting, overdue-task awareness, morning briefing button. |

**Safety:** anything outward-facing — sending/replying to email, inviting people, cancelling events — is
**prepared but held for your approval**. JARVIS reads it back and asks “Shall I send it?”; you say yes or tap
**Approve** on the dashboard. (Toggle in Settings.)

---

## 1. Get a Gemini API key (5 min)

1. Go to **https://aistudio.google.com/apikey** and sign in with your Google account.
2. Click **Create API key** and copy it.

> **About your “Gemini subscription”:** the Gemini app subscription (Google AI Pro/Ultra) and the
> **Gemini API** are billed separately — the app subscription does **not** include API usage. JARVIS uses
> the API key. The API has a **free tier** that comfortably covers personal use; if you hit limits, enable
> billing on the key in AI Studio (pay-as-you-go, typically a few dollars a month for one person on Flash).

## 2. Install (Windows)

1. Install **Python 3.10+** from https://www.python.org/downloads/ (tick **“Add python.exe to PATH”**).
2. Download this repository (green **Code** button → *Download ZIP*) and unzip it, e.g. to `Documents\JARVIS`.
3. Double-click **`install.bat`**. It installs everything, creates a **JARVIS icon on your desktop**, and opens
   `.env` in Notepad. Fill in:
   ```ini
   GEMINI_API_KEY=paste-your-key-here
   JARVIS_ACCESS_CODE=a-long-passcode-you-will-type-once-per-device
   TIMEZONE=Europe/London        # your zone, e.g. Asia/Jerusalem, America/New_York
   ```

**macOS / Linux:** run `./install.sh`, edit `.env`, start with `./jarvis.sh`.

## 3. Connect Google Calendar & Gmail (10 min, once)

JARVIS talks to Google with **your own** private OAuth app, so your data never goes through anyone else's server.

1. Open **https://console.cloud.google.com/** → project picker → **New project** → name it `JARVIS` → Create.
2. **APIs & Services → Library**: search and **Enable** both **Google Calendar API** and **Gmail API**.
3. **APIs & Services → OAuth consent screen** (or *Google Auth Platform → Branding*):
   user type **External**, app name `JARVIS`, your email for support/developer contact → Save.
   Under **Audience / Test users**, **add your own Gmail address**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** → Application type **Desktop app**
   → Create → **Download JSON**.
5. Rename the downloaded file to **`credentials.json`** and put it in the **`data`** folder inside JARVIS.
6. Start JARVIS, open **Settings (⚙) → Connect Google**. A Google sign-in page opens on your computer.
   You'll see *“Google hasn't verified this app”* — that's expected for your private app: click
   **Continue**, tick all permissions, done.
   (Alternative: run `.venv\Scripts\python -m jarvis.google_services` from the JARVIS folder.)

> While the app is in “Testing” mode Google expires the sign-in after 7 days. To avoid re-connecting weekly,
> go to the OAuth consent screen / Audience page and click **Publish app** (no review is needed for personal use
> with your own account; you'll just keep seeing the “unverified” notice when signing in).

## 4. Start him

Double-click the **JARVIS** desktop icon. A console starts the server (minimised) and the dashboard opens in its
own app window. Enter your access code once. Click the microphone (or **hold Space**) and talk.

Keep JARVIS running while you want phone access — closing the app window doesn't stop the server; closing the
console does. To start JARVIS with Windows: press `Win+R`, type `shell:startup`, and copy the desktop shortcut there.

## 5. Connect your Android phone

### Recommended: Tailscale (works anywhere, real HTTPS so the microphone works)
1. Install **Tailscale** on your computer (https://tailscale.com/download) and on your phone (Play Store);
   sign in to both with the same account.
2. On the computer, in a terminal: `tailscale serve --bg 8765`
   It prints an address like `https://your-pc.tail1234.ts.net`.
3. On the phone open that address in **Chrome** → enter your access code → menu **⋮ → Add to Home screen /
   Install app**. You now have a JARVIS app icon on your phone, full-screen, with voice — from anywhere,
   privately (only your devices can reach it).

### Same Wi-Fi only (quick test)
Open `http://<your-computer-ip>:8765` on the phone (the console prints it). Typing works; the **microphone needs
HTTPS**, so for voice either use Tailscale (above), or in Chrome open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`,
add `http://<your-computer-ip>:8765`, enable, relaunch.
If Windows asks about the firewall the first time, allow **Private networks**.

## 6. The voice (British accent)

Choose in **Settings → Voice**:

| Engine | Notes |
|---|---|
| **Gemini TTS** (default) | Uses your Gemini key; prompted to speak in a refined RP British accent. Try voices *Charon*, *Iapetus*, *Algenib*, *Sadaltager*. |
| **Microsoft neural en-GB** | `en-GB-RyanNeural` — the most consistently British butler voice, free, fast. |
| **Device voice** | Offline fallback; picks your device's UK English voice. |

If a voice engine fails, JARVIS automatically falls back to the device voice.
**Conversation mode** (the ↻ button next to the input): after he answers, he listens again automatically — a
hands-free back-and-forth.

---

## Configuration (`.env`)

| Key | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | — | Required. |
| `JARVIS_ACCESS_CODE` | — | Passcode for the dashboard on every device. **Set it** — JARVIS can send your email. |
| `GEMINI_MODEL` | `gemini-flash-latest` | The brain. `gemini-pro-latest` is smarter but slower/pricier. If a model is unavailable JARVIS falls back automatically. |
| `GEMINI_FAST_MODEL` | `gemini-flash-lite-latest` | Voice transcription. |
| `GEMINI_TTS_MODEL` | `auto` | Picks the newest Gemini TTS model your key can use. |
| `HOST` / `PORT` | `0.0.0.0` / `8765` | `127.0.0.1` = this computer only. |
| `TIMEZONE` | system | IANA zone name. |
| `SSL_CERTFILE` / `SSL_KEYFILE` | — | Optional HTTPS without Tailscale. |

Settings such as your name, how he addresses you (“sir”, “ma'am”, your name), working hours, background about
you, and voice live in the dashboard's **Settings** panel.

## Privacy & security

- Everything runs on your computer. Your projects, tasks, memories and chat log are in `data/jarvis.db`;
  your Google token is `data/token.json`. Both are git-ignored — never share the `data` folder or `.env`.
- Text you send, and email/calendar content JARVIS reads to answer you, is sent to the Gemini API to process.
- The dashboard requires the access code; brute-force attempts are rate-limited.
- To revoke Google access at any time: Settings → Disconnect, and/or https://myaccount.google.com/permissions.

## Troubleshooting

| Problem | Fix |
|---|---|
| “GEMINI_API_KEY missing” | Put the key in `.env`, restart JARVIS. |
| “Google account is not connected” | Do step 3; check `data/credentials.json` exists. |
| `access_denied` / “app not verified” blocks sign-in | Add your Gmail as a **Test user** on the OAuth consent screen. |
| Microphone button does nothing on the phone | You're on plain `http://` — use Tailscale's `https://` address. |
| No sound on phone | Tap the screen once (browsers block audio until you interact), check media volume. |
| Gemini 429 / quota errors | Free-tier limit reached; wait a minute or enable billing in AI Studio. TTS has the tightest limits — switch voice to *Microsoft neural en-GB* to save quota. |
| Port 8765 in use | Change `PORT` in `.env`. |

## Project layout

```
desktop.py            desktop app launcher (server + app window)
run_jarvis.py         server only (headless)
jarvis/agent.py       Gemini brain, persona, tool-calling loop, conversation memory
jarvis/tools.py       tool registry + approval gate for outward-facing actions
jarvis/google_services.py   Calendar & Gmail tools, OAuth
jarvis/projects.py    projects, tasks, progress log, long-term memory
jarvis/voice.py       Gemini transcription, British TTS (Gemini / Edge / device)
jarvis/server.py      FastAPI API + streaming chat
web/                  dashboard (HTML/CSS/JS, installable PWA)
data/                 your private data (git-ignored)
```

Adding a capability = write a Python function with a clear docstring and add it to `TOOL_FUNCS` in
`jarvis/tools.py`; Gemini picks it up automatically.
