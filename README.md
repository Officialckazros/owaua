# SefBot

A privacy-first Discord assistant with scoped memory, human-approved administration tools, and optional moderation, vision, and voice features. Raw message history is off by default and ordinary chat cannot execute Discord actions.

Built on top of [JayyDoesDev/airo](https://github.com/JayyDoesDev/airo), with a self-improvement layer bolted on.

## How it grows

- **Memory** — explicit memories are isolated to an exact guild or private scope. Users control raw-history consent separately with `/privacy`.
- **Lessons** — feedback and deliberate corrections can be distilled into scoped behavior rather than a cross-server global prompt.
- **Commands** — community command requests are stored as bounded prompt data, never executable host code.

Stack enough of all three and its "level" climbs from Newborn to Sage.

## What it actually does

- Model replies use a validated structured shape before they are rendered.
- Guild, DM, and user data use exact scopes so one server or private conversation cannot read another.
- Stored raw history requires both server enablement and the individual user's opt-in, and expires within 30 days.
- Ordinary chat is read-only. `/act` and explicit one-shot `/assistant` or `!assistant` requests may propose one administration action, but the invoking user must confirm an exact preview; permissions and role hierarchy are checked again at confirmation time.
- Confirmed assistant actions keep a scoped, 30-day undo record when an exact inverse exists. Use `!assistant undo` or `/assistant request:undo`; irreversible operations are reported as such and never fake a rollback.
- Can throw together a chart (bar/line/pie/radar) via QuickChart, no API key needed.
- `!vibecheck` gives an unfiltered read on how a channel's doing.
- No emoji, ever, in anything it sends.
- The system prompt is hardened against being told to ignore itself.

## Why it's safe-ish

Community-made commands via `!request` are prompt specs, not code. Discord-accessible host evaluation has been removed. Mutating actions are invoker-bound, single-use, and confirmation-gated; moderation classifiers create private staff review items instead of deleting messages on model output alone.

---

## Getting it running

1. Make a bot at [discord.com/developers/applications](https://discord.com/developers/applications) → New Application → Bot → Reset Token. Under Privileged Gateway Intents turn on **Message Content Intent** (required) and **Server Members Intent** (recommended — makes `/act` moderation and user lookups more reliable; without it the bot falls back to REST fetches). **Voice States Intent** is on by default via `Intents.default()` — no action needed unless you changed the defaults.
2. Invite it: OAuth2 → URL Generator → scope `bot`, permissions Send Messages / Read Message History / Add Reactions, plus Kick/Ban/Manage Roles if you want moderation to work. For voice, also tick **Connect** and **Speak**.
3. Use Python 3.12–3.14, copy `.env.example` to `.env`, set `DISCORD_TOKEN`, `SEFBOT_PRIVACY_CONTACT`, and credentials for the AI providers you actually use. Restrict the file before starting:

```bash
cp .env.example .env
chmod 600 .env
python3 -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements.lock
PYTHONPATH=src python -m sefbot.bot
```

## Commands

`@mention` it or DM it to chat. Use `/privacy` for consent, export, and deletion; `/help` lists the currently registered commands and permissions.

## Web dashboard and community modules

The bot now serves an authenticated control center at `/dashboard` on the same
host and port as its legal and health pages. Generate a private access token,
put it in `SEFBOT_DASHBOARD_TOKEN`, restart the bot, then sign in and select a
connected server. A token shorter than 24 characters is rejected. The token is
exchanged for a signed, HttpOnly, SameSite session; dashboard writes require a
CSRF token and every module change is recorded in the server's dashboard audit
trail.

The dashboard exposes every module under one catalog: community, safety,
automation, support, engagement, feeds, content, utilities and administration.
There are no SefBot paid tiers or artificial item limits. New high-impact
modules are disabled by default so installing an update cannot unexpectedly
moderate members, delete messages, post feeds or change roles; existing safe
features remain enabled for compatibility. Enable and configure only the
modules you want. Structured settings such as rules, forms, role menus and
subscriptions use bounded JSON editors in the module panel. Each module card
also reports whether its core workflow is live, partial, or configuration-only
so the dashboard never implies complete Dyno parity where it does not exist.
The checked-in [`FEATURE_COVERAGE.md`](FEATURE_COVERAGE.md) is the detailed
release truth table and explicitly lists every remaining parity gap.

Implemented live workflows include AFK statuses/notes, event logs, welcome and
announcements, auto-delete, scheduled messages and purge, autoban, deterministic
automod, autoresponders, autoroles/ranks, chat XP, reminders, highlights, tags,
slowmode, starboard, reaction-based role menus, tickets with transcripts,
reaction-entry timed giveaways,
web forms and submission automation, Reddit/YouTube public feeds, voice-text
links and utility/fun commands. Existing confirmed administration actions,
custom commands, moderation review, economy and localization remain integrated.
Third-party networks may still require their own free developer credentials or
impose upstream quotas; SefBot itself does not charge to unlock them.

Reddit and YouTube polling use public feeds. Twitch and Kick use official app
access tokens generated from `SEFBOT_TWITCH_CLIENT_ID` /
`SEFBOT_TWITCH_CLIENT_SECRET` and `SEFBOT_KICK_CLIENT_ID` /
`SEFBOT_KICK_CLIENT_SECRET`. TikTok's official Display API requires the creator
to authorize `video.list`; put that access token in
`SEFBOT_TIKTOK_ACCESS_TOKEN`. Credentials remain environment-only and are never
returned through the dashboard.

## Privacy, legal pages, and service health

- Terms: [kozzyx.org/sefbot/terms](https://kozzyx.org/sefbot/terms)
- Privacy: [kozzyx.org/sefbot/privacy](https://kozzyx.org/sefbot/privacy)
- `/privacy status|opt-in|opt-out|export|delete` remains private and available without accepting the ToS. ToS acceptance is not raw-history consent.
- Moderation, server rules, raw history, and voice transcription are disabled by default. Voice transcription additionally requires participant consent in the exact guild.
- The built-in HTTP service exposes `/healthz` for liveness and `/readyz` for sanitized Discord/database readiness. `SEFBOT_PRIVACY_CONTACT` defaults to `privacy@opsef.bot`; `PORT` defaults to `8080`.
- The authenticated dashboard is at `/dashboard`. Public forms are under
  `/forms/<server-id>/<form-slug>` and are available only when that exact form
  and the Forms module are enabled.
- Changing the legal version invalidates earlier acceptance, so users review material policy changes before resuming normal commands.

## Modular AI features

A second, self-contained model layer (`services/llm_client.py`) that talks to an OpenAI-compatible endpoint over `httpx`. Point `SEFBOT_LLM_BASE_URL` / `SEFBOT_LLM_API_KEY` at your inference provider and set the model ids in `.env` (see `.env.example`).

- **`/ask <question> [mode=reasoning|fast]`** — one-shot Q&A. `reasoning` uses the best model (`SEFBOT_CHAT_MODEL`, default GPT OSS 120B); `fast` uses Llama 3.3 70B on Groq (`SEFBOT_FAST_MODEL`). Cooldown-protected.
- **`/act <natural language>`** — moderators can ask for one typed action such as a timeout or ban. The bot shows an ephemeral, mention-safe preview bound to that invoker; only a confirmation within two minutes can proceed. The executor then re-resolves the target and rechecks the exact permission, bot capability, and role hierarchy. Schemas live in `function_registry.py`.
- **Passive moderation** — disabled until `SEFBOT_SAFETY_ENABLED=1` and an administrator enables it for the guild. Safety GPT is a bounded classifier only: high-confidence flags go to a private staff review with **Delete message** / **Dismiss** controls. The model cannot delete content, warn users, or globally block anyone by itself.
- **Vision** — `/describe [image] [url] [prompt]` and the right-click **Describe image** message context menu. Uses Qwen vision (`SEFBOT_VISION_MODEL`); one call returns both a description and a moderation flag. Remote URLs must resolve to a public HTTP(S) endpoint, redirects are revalidated, and downloads are streamed under `SEFBOT_VISION_MAX_IMAGE_BYTES`. PNG, JPEG, GIF, and WebP are supported. Cooldown-protected.
- **Multilingual** — `!language` / `/language` sets the language the bot replies in (per user, with an optional server default). Non-English messages are also detected with `langdetect` (cheap, never the LLM). In a channel listed in `SEFBOT_MULTILINGUAL_CHANNELS`, and when no language is set, Llama 3.3 70B replies in the message's own language; elsewhere the message is translated for the brain as before.
- **Voice** — `/join`, `/leave`, and `/say <text>` provide playback/TTS. Live `/stt` is off by default and requires `manage_channels`, guild enablement, and consent from every non-bot participant; the session stops when its controller leaves or consent/channel visibility changes. The released `discord-ext-voice-recv` packages still require a vulnerable PyNaCl version, so the base install keeps PyNaCl ≥ 1.6.2 and safely leaves live receive unavailable until a compatible upstream release exists. Do not downgrade PyNaCl to enable it.
- **Server rules (approval-gated)** — the optional preset runs only when `SEFBOT_RULES_ENABLED=1`, `SEFBOT_RULES_GUILD` is explicitly configured, and that guild enables it. Findings go to a private review channel. Approval rechecks the action-specific permission (`ban_members`, `kick_members`, `moderate_members`, or `manage_messages`) and bot hierarchy before doing anything; denial, timeout, or restart takes no action.

## Knowledge base

Separate from the memory system, this is a scoped retrieval store (SQLite FTS5, BM25 ranked). Upload size, row count, chunk length, and prompt retrieval are bounded. Retrieved text is delimited as untrusted reference data and cannot authorize actions.

`PYTHONPATH=src python -m sefbot.fuck_religion --guild-id 123456789` loads the built-in starter corpus into one exact guild scope. Add a folder argument to ingest its bounded `.md`/`.txt` files into that same scope.

In Discord, mods can do `!kb add <topic> | <text>` or attach a file to `!kb add`. Anyone can run `!kb search <query>` or just `!kb` for stats. `SEFBOT_KB_TOPK` controls how many chunks get injected per message (default 6).

## Top.gg / listing notes

Check [TOPGG.md](./TOPGG.md) for the full checklist. Worth knowing up front:

- `!music` / `/music` returns a validated YouTube search/watch link; the bot never downloads or redistributes tracks.
- DMs relayed through the bot name the requester and support `!dmblock` / `!dmunblock`.
- `!privacy` covers in-bot data controls.

## Where things live

All bot code lives in `src/sefbot/` (run with `PYTHONPATH=src python -m sefbot.bot`).

- `bot.py` — Discord glue: chat, embeds, reaction feedback, commands, the reflection loop
- `brain.py` — system prompt construction, memory retrieval, leveling, reflection
- `actions.py` — permission-gated moderation/status actions and chart URLs
- `embeds.py` — embed builders and the emoji stripper
- `customcmds.py` — AI-generated, prompt-defined community commands
- `db.py` — SQLite persistence with in-place migration
- `kb.py` — scoped, bounded knowledge-base ingestion and FTS5/BM25 retrieval
- `fuck_religion.py` — seeds the KB with a starter corpus or a folder of text
- `ai.py` — async Groq wrapper for chat and structured JSON
- `config.py` — env config and the persona
- `services/llm_client.py` — async httpx LLM wrapper (chat, tools, vision, STT, TTS) with retries
- `function_registry.py` — tool schemas + permission-gated executors for `/act`
- `moderation.py` — passive Safety GPT moderation, DM warnings, mod-log
- `multilingual.py` — langdetect routing to Llama 3.3 70B
- `vision.py` — `/describe` + context-menu image description
- `voice.py` — `/join` `/leave` `/say` and Whisper live transcription
- `rules.py` — server ruleset + approval-gated enforcement (Approve/Deny buttons)
- `web.py` — legal pages plus liveness/readiness HTTP endpoints
- `dashboard.py` / `module_catalog.py` — authenticated module dashboard, public forms and shared schemas
- `community.py` — dashboard-driven Discord events, automations, feeds, support and engagement workflows

## Deployment

Run `deploy opsef` (or `./scripts/deploy opsef`) to run pre-deployment checks, sync files, and restart the service on Daki. Startup applies versioned migrations, purges legacy raw history, and enforces the configured retention ceiling before readiness is reported.

## Verification

The regression suite uses only Python's standard-library test runner:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src
cd cloudflare-worker && npm ci --ignore-scripts && npm test && npm run dry-run
```
