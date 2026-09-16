# owaua

Discord bot I made for hanging out in servers.

It talks when you ping it, and keeps a short conversation history. Public DMs are disabled by default. Hangout chat uses DeepSeek. `explicit` uses Mistral Small 4. `!persona host default gpt` selects GPT-5.6 Luna for that server. I'm trying to keep it small on purpose. If something is missing, that's probably why.

The bot uses Discord's gateway reconnects and recreates its client after temporary network or gateway failures (with a 5-second-to-5-minute backoff). A bare `@Owaua` gets a local `yeah?` reply without waiting on an AI provider.

If you actually put this in a server, I would like to know. DM me on Discord (`gays._`) or email `ckazros@owaua.com`.

## Commands

`!help` prints these too. Each command has a 25 second cooldown.

- `!persona rudeish|nerdish|explicit|host default gpt/deepseek/mistral` — server changes need Manage Server; `explicit` only works in age-restricted channels. `host default` uses that model's own voice (`!persona host default` picks DeepSeek).
- `!language <full name>|reset` — requires Manage Server; one reply language for the server (`hungarian`, not `hu`). If matching pictures are in `pfps/` and `banners/`, only that server’s profile picture and banner change. `reset` restores English and the original picture and banner.
- `!music <song or YouTube URL>` — or attach an audio file; plus pause / resume / restart / skip / leave. Playlists, Mix links, live radios, videos over 15 minutes, and non-YouTube URLs are rejected. The bot leaves if the requester drops out or the voice channel is empty.
- `!memory erase` — needs Manage Server
- `!memory erase mine` — deletes your own history, including in DMs when DM chat is disabled
- `!security status|pause|resume` — bot-owner-only API usage and emergency pause
- `!shutdown` — fully stops the bot, including API requests, replies, music, and gateway reconnects (bot owner only)

Music controls require participation in the bot's current voice channel. Downloads are limited to 20 MiB, at most two music sessions run at once, and playback ends after 15 minutes. Supported uploads: MP3, WAV, Ogg/Opus, FLAC, M4A/MP4 and WebM. File names alone do not determine the decoder; playlists and unsupported file contents are rejected.

Every `!music` attempt is written as a JSON object to `MUSIC_AUDIT_LOG` (default: `data/music-audit.jsonl`). Records include UTC timestamps, message/user/server/channel IDs and names, requested action and argument hash, attachment metadata, voice-channel context, outcome, reply, error type, and duration. Signed attachment query strings and media stream URLs are not logged.

Voices are the text in `personas/`. Edit a file and the next message uses it.

## Run

Python 3.11+, Message Content Intent, then `DISCORD_TOKEN`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, and `MISTRAL_API_KEY` in `.env`.

```sh
pip install -r requirements.txt
cp .env.example .env
./scripts/run-bots.sh
```

Invite with View Channels, Send Messages, Read Message History, Connect, and Speak. Music requires **Linux** and an up-to-date FFmpeg on `PATH`; unsupported hosts fail closed. Chat works without native music support.

## Abuse controls

Defaults apply automatically to existing `.env` files; see [.env.example](.env.example) for overrides.

| Limit | Default |
| --- | ---: |
| Global API attempts per rolling minute | 12 |
| Per-user attempts per rolling 24 hours | 30 |
| Per-server attempts per rolling 24 hours | 100 |
| Global attempts per rolling 24 hours | 200 |
| Lifetime API attempts | 1,000 |
| Concurrent AI requests | 3 |

Quotas are reserved before sending and persist in `data/memory.sqlite3`. There are no automatic paid retries or provider fallbacks. Failed/cancelled attempts count, and neither memory erasure nor `!security resume` resets usage. Once the lifetime ceiling is reached, the owner must deliberately raise `API_REQUESTS_LIFETIME` and restart. Keep the database on persistent storage and share it between any replicas using these limits; independent databases do not share a budget. These are **request ceilings, not a dollar cap**; provider prices and other clients using the same keys remain outside this bot's control.

Hangout modes are text-only, with no hosted tools or image analysis, and an 80-token output cap. Once an allowlisted user enables `!full mode` in the trusted channel, the bot removes its own input, attachment, rate, cooldown, admission, quota, pause, output, deadline, and memory-retention limits for that user. Full mode enables OpenAI-hosted web search and code interpreter. Image generation is available only for explicit requests in full mode and is capped at three requests per user per rolling 24 hours. The Discord API and OpenAI account/model policies and limits still apply. Users who are not on that allowlist still hit the API ceilings. `BOT_ALLOWED_GUILD_IDS` can restrict invited servers; `BOT_BLOCKED_USER_IDS` denies selected users. Set `BOT_ALLOW_DMS=1` only if public DM chat is wanted.

Memory keeps at most 20 records per conversation and 10,000 records overall. Expired records are removed on startup, reads, writes and hourly maintenance (seven days plus up to one maintenance interval while idle). Erasure prevents outstanding replies from restoring erased history. Usage counters remain. Existing global persona settings are discarded on startup because ordinary users could previously overwrite them; server administrators should select their server's persona again.

Hangout AI can optionally go through Cloudflare AI Gateway (free plan: logs, 12 requests/minute, cache off). Full mode and music stay on the host so a Cloudflare outage cannot break them. If the gateway is missing or unreachable, the same provider is used directly; that is not a second paid reservation or a different API. `scripts/setup-cloudflare.py` creates the gateway and an append-only audit Worker when `CLOUDFLARE_API_TOKEN` is set. It does not enable Unified Billing or any paid Cloudflare product.

See [security verification and deployment notes](SECURITY-HARDENING.md) for remaining host requirements and verification limits.

MIT.

(Special thanks to [@openai](https://github.com/openai) for developing GPT Astra 6 and helping me maintain this project's security and privacy)
