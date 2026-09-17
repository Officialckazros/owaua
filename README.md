# owaua

Discord bot I made for hanging out in servers.

Ping it and it talks back. It keeps a little conversation history. DMs are off by default.

Normal chat uses Perplexity (GPT-5.6 Luna). There's a full mode that talks to OpenAI instead. Personas are just text files in `personas/` — edit one and the next reply uses it.

If you actually put this in a server, I'd like to know. DM me on Discord (`gays._`) or email `ckazros@owaua.com`.

## Commands

`!help` prints these. 25 second cooldown.

- `!persona rudeish|nerdish|flirty|host default gpt/deepseek/mistral` — your persona, not the server's. Default is `rudeish`.
- `!language hungarian` (or another full language name, not a code) — server-wide replies; needs Manage Server. `reset` goes back to English. Matching pictures in `pfps/` and `banners/` change too.
- `!music <youtube or twitter url>` — plus pause / resume / restart / skip / leave. No name searches, playlists, or long videos.
- `!memory erase` — wipe server memory (Manage Server)
- `!memory erase mine` — wipe just yours

Owner-only: `!security status|pause|resume` and `!shutdown`.

Music needs Linux and a current FFmpeg. Chat works without that.

## Run

Python 3.11+, Message Content Intent, then `DISCORD_TOKEN`, `PERPLEXITY_API_KEY`, and `OPENAI_API_KEY` in `.env`.

```sh
pip install -r requirements.txt
cp .env.example .env
./scripts/run-bots.sh
```

Invite with View Channels, Send Messages, Read Message History, Connect, and Speak.

Limits and other knobs are in `.env.example`.

MIT.

Thanks `https://github.com/Perplexity` for the Agent API
