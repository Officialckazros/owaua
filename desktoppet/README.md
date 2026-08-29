# owaua — Desktop Pet

A desktop pet that lives on your screen. It wanders around the bottom of your
desktop, reacts to clicks, talks out loud, answers questions, gets hungry,
wants to play, and generally tries to be a good little creature.

![owaua](desktoppet.jpg)

## Features

- **Lives on your desktop** — frameless, transparent, always-on-top window
- **Wanders around** — walks left and right along the bottom of the screen, flips to face its direction
- **Drag it anywhere** — pick it up and drop it wherever you like
- **Talks** — speech bubbles with a typewriter effect *and* real text-to-speech (macOS `say`, Windows SAPI, Linux espeak — zero extra deps)
- **Answer questions** — ask it anything; it uses an AI brain when a key is configured, with a built-in offline brain otherwise
- **Mood system** — it gets hungry, tired, and happy over time, and tells you about it
- **Interactions** — click (boop!), double-click (headpats), right-click for a full menu
- **Mini-features** — jokes, facts, math, the time, coin flips, dice, rock-paper-scissors, singing, dancing (zoomies), feeding, sleeping
- **System tray** — hide it to the tray, feed it or quit from there
- **Settings** — name, voice pace, toggles for TTS / AI / walking / always-on-top; atomically persisted to `~/.owaua/config.json` with private permissions
- **Custom sprite** — place a bounded PNG/JPEG named `desktoppet.png` or `desktoppet.jpg` in `~/.owaua/sprites/`
- **Cross-platform** — Windows (exe), macOS (app), Linux

## Quick start (from source)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python pet.py
```

## AI brain

owaua uses an OpenAI-compatible API when a key is present (falls back to the
offline brain if not). Configure via environment variables, the operating
system credential store, or `~/.owaua/.env`. It deliberately does not load a
project, working-directory, or app-adjacent `.env` file.

| Variable | Purpose |
| --- | --- |
| `OWAUA_AI_KEY` | API key (also accepts `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `INFERX_API_KEY`) |
| `OWAUA_AI_BASE_URL` | HTTPS OpenAI-compatible base URL for a custom provider |
| `OWAUA_AI_MODEL` | Model name for a custom provider |
| `OWAUA_ALLOW_INSECURE_LOCAL` | Development only: allow an HTTP loopback endpoint when set to `1` |

No key? The offline brain still handles greetings, jokes, facts, math, time,
games, and a chatty personality.

Provider keys are matched to their own endpoint automatically. To force a
specific provider, set all three `OWAUA_AI_KEY`, `OWAUA_AI_BASE_URL`, and
`OWAUA_AI_MODEL` values together.

Store a key in the OS credential store without putting it in shell history:

```bash
python pet.py --store-ai-key GROQ_API_KEY
```

Supported credential names are `OWAUA_AI_KEY`, `GROQ_API_KEY`,
`DEEPSEEK_API_KEY`, and `INFERX_API_KEY`. Custom endpoints must use HTTPS,
cannot contain credentials/query parameters, and cannot point at
private-network addresses. HTTP loopback endpoints are available only when the
explicit development flag is enabled. Questions are sent to the configured
provider only when AI is on.

If you use `~/.owaua/.env` on macOS or Linux, keep it private:

```bash
chmod 600 ~/.owaua/.env
```

owaua also repairs its config directory/file modes to `0700`/`0600` where the
platform supports POSIX permissions.

## Tests

The security and headless helper tests run without showing a window:

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

## Building a standalone app

### macOS (builds `dist/owaua.app` and zips it)

```bash
bash build_mac.sh
```

### Windows (builds `dist/owaua.exe`)

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

> macOS apps are unsigned — on first run, right-click the app and choose **Open** to bypass Gatekeeper.

## GitHub releases

Push a tag and CI builds + attaches the Windows exe and macOS app:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The workflow lives at `.github/workflows/release.yml`.

## Controls

| Input | Action |
| --- | --- |
| Left-click | Boop / random reaction |
| Double-click | Headpat / wake up |
| Right-click | Menu (feed, pet, play, ask, jokes, facts, sleep, settings, quit…) |
| Drag | Move the pet anywhere |
| Tray icon | Show/hide, feed, quit |
