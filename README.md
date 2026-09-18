# owaua

Discord bot I made for hanging out in servers.

Ping it and it talks back. It keeps a little conversation history. DMs are off by default.

Normal chat uses Gemini 3.5 Flash Lite for every persona. Blocked users remain restricted to Groq's `openai/gpt-oss-20b`; explicit host-model commands are unchanged. Full mode can use GPT, Claude, Gemini, DeepSeek, or GLM. Personas are just text files in `personas/` — edit one and the next reply uses it.

On this Mac, local mode uses DeepGrove Maple through its OpenAI-compatible MLX server at `http://127.0.0.1:8080/v1`. `scripts/run-bots.sh` starts that server automatically when `OWAUA_LOCAL_ONLY=1`.

If you actually put this in a server, I'd like to know. DM me on Discord (`gays._`) or email `ckazros@owaua.com`.

## Commands

`!help` prints these. 25 second cooldown.

- `!persona rudeish|nerdish|flirty|chaotic|host default gpt/deepseek/mistral` — your persona, not the server's. Default is `rudeish`.
- `!full mode gpt|claude|gemini|deepseek|glm` — enable full mode with that model. `!full mode on` selects GPT; `!full mode off` disables it. Full mode is opt-in for approved users in the designated channels. Allowlisted users get higher finite per-minute and per-user daily API ceilings, while the shared guild, global daily, lifetime, rate, concurrency, timeout, input, history, and output limits still apply.
- `!reset all` — fully reset this bot in the current server (Manage Server required), including server memory, language/profile, and music state.
- `!language hungarian` (or another full language name, not a code) — server-wide replies; needs Manage Server. `reset` goes back to English. Matching pictures in `pfps/` and `banners/` change too.
- `!music <youtube or twitter url>` — plus pause / resume / restart / skip / leave. No name searches, playlists, or long videos.
- `!memory erase` — wipe server memory (Manage Server)
- `!memory erase mine` — wipe just yours

Owner-only: `!security status|pause|resume` and `!shutdown`.

The bot owner can also use `!pricing` to show the configured models' current
provider list prices per 1M tokens. It is intentionally hidden from normal
`!help` output.

Music needs Linux and a current FFmpeg. Chat works without that.

## Run

Python 3.11+, Message Content Intent, then `DISCORD_TOKEN`, `PERPLEXITY_API_KEY`, and `OPENAI_API_KEY` in `.env`.

The bot runtime lives in `src/owaua/`; use the scripts below rather than
launching an individual module directly.

```sh
pip install -r requirements.txt
cp .env.example .env
./scripts/run-bots.sh
```

In local-only mode (`OWAUA_LOCAL_ONLY=1`), edit a file in `personas/` and run
`./scripts/update-persona.sh chaotic` (or another persona name) to validate it.
The local bot uses the updated file immediately; no Daki server is required.

Deploy profiles separately: `./scripts/deploy-local.sh` verifies the local
`.env` profile, while `./scripts/deploy-daki.sh` uploads `.env.cloud` and
restarts Daki. Run only one profile at a time when both profiles contain the
same Discord token; Discord permits only one active gateway session per token.

Invite with View Channels, Send Messages, Read Message History, Connect, and Speak.

Project structure and Daki deployment details are in
[docs/OPERATIONS.md](docs/OPERATIONS.md). Use
`OWAUA_DAKI_DRY_RUN=1 ./scripts/deploy-daki.sh` to preview the remote upload.

Contributing and security notes are kept in the [docs](docs/) folder

## Releases

The current release version is recorded in [VERSION](VERSION). To build the
same source archives used by GitHub Releases, run:

```sh
./scripts/package-release.sh
```

This creates a `.tar.gz`, `.zip`, and SHA-256 checksum file in `dist/`.
Pushing a tag such as `v0.1.0` runs the release workflow and publishes those
archives automatically. The package contains source code and assets only; it
never includes `.env`, `data/`, virtual environments, or local model files.

The same release tag also publishes a container image to GitHub Packages:

```sh
docker pull ghcr.io/officialckazros/owaua:latest
```

Pass the bot configuration as environment variables when starting the
container and mount `/app/data` if its memory database should persist.

Limits and other knobs are in `.env.example`.

## Please don't burn the API 

Owaua is a hangout bot. Every reply costs real money. You do not get a free
model, a homework mill, a benchmark harness, or a toy for wasting tokens.
Using it for junk that burns API credits instead of hanging out is abuse. That
includes looping it, farming it, pinging it for nothing, dumping huge pastes,
encode-and-decode junk, jailbreak marathons, walls of filler, scripts or extra
accounts, and turning on expensive modes then spamming search, tools, or long
thinking on garbage.

We can ignore you, wipe memory, pull the bot from the server, and stop answering
you without warning. The rate limit is not a free pass to keep doing it. All
blocked users are forced onto the `blocked` persona and can only access Groq's
GPT OSS 20B model.

MIT.

Thanks [@Perplexity](https://github.com/perplexityai) for the Agent API
