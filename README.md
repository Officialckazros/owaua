# SefBot

A privacy-first Discord assistant with scoped memory, human-approved administration tools, and optional moderation, vision, and voice features. Raw message history is off by default and ordinary chat cannot execute Discord actions.

Non-media Discord attachments are scanned locally with ClamAV before any bot
feature reads them. Confirmed malware is removed, privately reported, and the
sender is hard-blocked from bot access. Scanner failures fail closed for the
file without punishing the sender; verified image/audio/video media is excluded.

Built on top of [JayyDoesDev/airo](https://github.com/JayyDoesDev/airo), with a self-improvement layer bolted on.

## How it grows

- **Memory** — explicit memories are isolated to an exact guild or private scope. Users control raw-history consent separately with `/privacy`.
- **Lessons** — feedback and deliberate corrections can be distilled into scoped behavior rather than a cross-server global prompt.
- **Commands** — community command requests are stored as bounded prompt data, never executable host code.

Stack enough of all three and its "level" climbs from Newborn to Sage.

## What it actually does

- Model replies use a validated structured shape before they are rendered.
- Guild, DM, and user data use exact scopes so one server or private conversation cannot read another.
- Ordinary stored raw history requires both server enablement and the individual user's opt-in, and expires within 30 days; the disclosed deployment-level archive guild below is the explicit text-only exception.
- Ordinary chat is read-only. `/act` and explicit one-shot `/assistant` or `!assistant` requests may propose one administration action, but the invoking user must confirm an exact preview; permissions and role hierarchy are checked again at confirmation time.
- Confirmed assistant actions keep a scoped, 30-day undo record when an exact inverse exists. Use `!assistant undo` or `/assistant request:undo`; irreversible operations are reported as such and never fake a rollback.
- Broad staff requests can return a non-executing multi-step plan with a permission explanation for every step. Each meaningful mutation must be requested and confirmed separately; opaque or bundled mutations are rejected.
- Prefix and slash assistant commands both accept a `.txt` attachment as the request, including when no separate prompt text is supplied.
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

First use is locked behind `/tos`: Discord issues a single-use 15-minute link,
the user reads and accepts the current Terms on the public page, then returns to
Discord. The web form discloses its abuse-prevention processing and stores only
a keyed network token for at most 30 days, never the raw client IP. Configure
separate `SEFBOT_TOS_ACCEPTANCE_SECRET` and `SEFBOT_TOS_PROXY_SECRET` values;
the latter must match the secret header set by the trusted Cloudflare Worker.

## Web dashboard and community modules

The bot serves its control center at
[`wearegays.net/dashboard`](https://wearegays.net/dashboard). Discord is the only
sign-in method. Only connected servers that the Discord user owns or can manage
are visible. The bot requests Discord's `identify` and `guilds` scopes; it does
not receive the user's Discord password.

In the Discord Developer Portal, add this OAuth2 redirect URI:

`https://wearegays.net/dashboard/auth/discord/callback`

Set `SEFBOT_DASHBOARD_PUBLIC_URL`, a separate random
`SEFBOT_DASHBOARD_SESSION_SECRET`, and `SEFBOT_DISCORD_CLIENT_ID` /
`SEFBOT_DISCORD_CLIENT_SECRET`; see `.env.example` for the exact names. Discord
OAuth is exchanged for a signed, HttpOnly, SameSite session. Dashboard writes
require a CSRF token and every module change is recorded in the server's
dashboard audit trail.

The **Incident Center** combines searchable moderation cases, private member
notes, HTTPS evidence references, expiry and appeal timelines, malware and rule
reviews, confirmed assistant actions, and unresolved tickets. Queue changes are
OAuth-authorized, CSRF-bound, guild-scoped, assignable, and audit logged.

Server Health reports are opt-in and advisory only. Scheduled staff digests use
an explicit private channel and visibility, and analytics exports contain
aggregate counts rather than message content or personal profiles.

The dashboard exposes every module under one catalog: community, safety,
automation, support, engagement, feeds, content, utilities and administration.
The **Settings** page also exposes every guild-scoped core control used by the
runtime: persona, chat model, language, response tier, command channels, lurk,
history and retention, moderation review, rules review, private staff channels,
and voice transcription. Channel and role fields use the selected server's live
Discord resources. Host credentials, OAuth secrets, database paths, bind ports,
and provider API keys intentionally remain deployment settings rather than being
exposed to server managers.

**Booster Perks** is a fully dashboard-driven module. It persists individual
current/all-time boost history, imports existing boosters, sends randomized
embed/plain/DM greetings with `{user}`, `{username}`, `{userboosts}`, `{level}`,
`{count}` and `{totalcount}`, manages automatic/personal/level/age roles, gifts,
private text/voice channels, mention reactions, emoji role restrictions,
read-only statistic voice channels, manager roles and per-event logs. Member
self-service and safe manager corrections use `!booster help`. Discord does not
reliably expose removal of only one of several boosts, so managers can correct
that specific case with `!booster adjust @member -1`.

The dashboard has a dedicated **Booster Perks** workspace rather than a raw
module JSON editor. Every catalog setting appears once as a typed toggle,
text/URL/number field, live Discord role/channel selector, or add/remove row
builder. The same page shows live current/all-time statistics and individual
records, and lets authorized server managers import/synchronize boosters, send
a test greeting, and apply audited `+N`/`-N` corrections.

There are no SefBot paid tiers or artificial item limits. Every dashboard
module is enabled by default, and server managers can disable any module later.
Modules that need rules, channels, roles, forms, or subscriptions remain inert
until those settings are configured. Structured settings use bounded JSON
editors in the module panel. Each module card
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
custom commands, moderation review, economy, Booster Perks and localization remain integrated.
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
- The built-in HTTP service exposes `/healthz` for liveness and `/readyz` for sanitized Discord/database readiness. `SEFBOT_PRIVACY_CONTACT` defaults to `ckazros@kozzyx.org`; `PORT` defaults to `8080`.
- The authenticated dashboard is at `/dashboard`. Public forms are under
  `/forms/<server-id>/<form-slug>` and are available only when that exact form
  and the Forms module are enabled.
- Changing the legal version invalidates earlier acceptance, so users review material policy changes before resuming normal commands.

## Modular AI features

A second, self-contained model layer (`services/llm_client.py`) that talks to an OpenAI-compatible endpoint over `httpx`. Point `SEFBOT_LLM_BASE_URL` / `SEFBOT_LLM_API_KEY` at your inference provider and set the model ids in `.env` (see `.env.example`).

- **`/ask <question> [mode=reasoning|fast]`** — one-shot Q&A. `reasoning` uses the best model (`SEFBOT_CHAT_MODEL`, default GPT OSS 120B); `fast` uses Llama 3.3 70B on Groq (`SEFBOT_FAST_MODEL`). Cooldown-protected.
- **`/act <natural language>`** — moderators can ask for one typed action such as a timeout or ban. The bot shows an ephemeral, mention-safe preview bound to that invoker; only a confirmation within two minutes can proceed. The executor then re-resolves the target and rechecks the exact permission, bot capability, and role hierarchy. Schemas live in `function_registry.py`.
- **Passive moderation** — disabled until `SEFBOT_SAFETY_ENABLED=1` and an administrator enables it for the guild. Safety GPT is a bounded classifier only: high-confidence flags go to a private staff review with **Delete message** / **Dismiss** controls. The model cannot delete content, warn users, or globally block anyone by itself.
- **Malware scanner** — enabled by default for every non-media attachment and backed by a required local ClamAV installation. Media exclusions require matching MIME, extension, and binary magic. Confirmed signatures delete/report/block immediately; unavailable, oversized, or timed-out scans remove the message without blocking its sender. Files are owner-only temporary data and are never uploaded to an antivirus vendor.
- **Vision** — `/describe [image] [url] [prompt]` and the right-click **Describe image** message context menu. Uses Qwen vision (`SEFBOT_VISION_MODEL`); one call returns both a description and a moderation flag. Remote URLs must resolve to a public HTTP(S) endpoint, redirects are revalidated, and downloads are streamed under `SEFBOT_VISION_MAX_IMAGE_BYTES`. PNG, JPEG, GIF, and WebP are supported. Cooldown-protected.
- **Age-restricted images** — `/nsfw character:<tag> amount:<1-10>` (or `!nsfw <tag> [amount]`) uses the Rule34 API only in server channels Discord marks age-restricted. Set `SEFBOT_RULE34_USER_ID` and `SEFBOT_RULE34_API_KEY` to enable it; credentials stay host-side.
- **Multilingual** — `!language` / `/language` sets the language the bot replies in (per user, with an optional server default). Non-English messages are also detected with `langdetect` (cheap, never the LLM). In a channel listed in `SEFBOT_MULTILINGUAL_CHANNELS`, and when no language is set, Llama 3.3 70B replies in the message's own language; elsewhere the message is translated for the brain as before.
- **Voice** — `/join`, `/leave`, and `/say <text>` provide playback/TTS. Live `/stt` is off by default and requires `manage_channels`, guild enablement, and consent from every non-bot participant; the session stops when its controller leaves or consent/channel visibility changes. The released `discord-ext-voice-recv` packages still require a vulnerable PyNaCl version, so the base install keeps PyNaCl ≥ 1.6.2 and safely leaves live receive unavailable until a compatible upstream release exists. Do not downgrade PyNaCl to enable it.
- **Server rules (approval-gated)** — a server manager enables the preset and selects a private approval channel in the dashboard. Findings go only to that private review channel. Approval rechecks the action-specific permission (`ban_members`, `kick_members`, `moderate_members`, or `manage_messages`) and bot hierarchy before doing anything; denial, timeout, or restart takes no action. Optional LLM confirmation still depends on the host's provider configuration.
- **Swear jar** — disabled by default. Server managers can enable it in Dashboard → Settings or with confirmed `/config swearjar on`. Each message with locally detected profanity gets a reply with that member's server total; only the aggregate number is stored. Use `/swears [user]` or `!swears [@user]` to check it.
- **Dedicated text archive** — guild `1535083112709496903` is the deployment-level archival scope. On startup and every six hours, SefBot resumes a channel/thread backfill from durable cursors, then indexes new and edited messages live. It stores message text and author IDs only: attachments, embeds, stickers, Unicode emoji, custom Discord emoji, and emoji-only messages are omitted. Archived raw text is exempt from the normal 30-day cleanup. Managers can inspect coverage with `/archive-status`; `user` questions retrieve relevant messages from the full indexed history.

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
