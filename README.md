# owaua

Discord bot I made for hanging out in servers.

It talks in DMs and when you ping it. It can sit in a channel and chime in, and it remembers people. Hangout chat stays on GPT-5.6 Luna with no tools. `!debate` is the only path that searches the web or runs code. I'm trying to keep it small on purpose. If something is missing, that's probably why.

If you actually put this in a server, I would like to know. DM me on Discord (`gays._`) or email `ckazros@owaua.com`.

## Commands

`!help` prints these too. Each command has a 25 second cooldown.

- `!active on|off|status` — reply to every 6th message
- `!persona rudeish|nerdish|explicit|host default gpt/deepseek/mistral` — `explicit` only works in age-restricted channels. `host default` uses that model's own voice (`!persona host default` picks GPT).
- `!language <full name>|reset` — one reply language for the server (`hungarian`, not `hu`). If matching pictures are in `pfps/` and `banners/`, only that server’s profile picture and banner change. `reset` restores English and the original picture and banner.
- `!debate <topic>` — lock that topic (confirms it’s on, then waits for a ping or reply); uses GPT-5.6 Terra with web search and code interpreter; skips persona until `!debate off`
- `!music <song or URL>` — plus pause / resume / restart / skip / leave
- `!memory erase` — needs Manage Server

Voices are the text in `personas/`. Edit a file and the next message uses it.

## Run

Python 3.11+, Message Content Intent, then `DISCORD_TOKEN`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, and `MISTRAL_API_KEY` in `.env`.

```sh
pip install -r requirements.txt
cp .env.example .env
./scripts/run-bots.sh
```

Invite with View Channels, Send Messages, Read Message History, Connect, and Speak. Music needs FFmpeg on `PATH`.

MIT.
