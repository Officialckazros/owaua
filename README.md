# owaua

Discord bot I made for hanging out in servers.

It talks in DMs and when you ping it. It can sit in a channel and chime in, it remembers people, and it has GIFs, music, and voice if you want those. I'm trying to keep it small on purpose. If something is missing, that's probably why.

If you actually put this in a server, I would like to know. DM me on Discord (`gays._`) or email `ckazros@owaua.com`. `!owner's note` says the same thing.

## Running it

You need Python 3.11+, a Discord app with Message Content Intent turned on, and an API key for whichever model you want to use.

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in .env
./scripts/run-bots.sh
```

Invite it with View Channels, Send Messages, Embed Links, Read Message History, Manage Messages, Connect, and Speak. The bot checks those before it tries to nuke, join voice, play music, or delete an AI image, and it will tell you if something is missing.

Keep `.env` to yourself. Git already ignores it.

## Commands

`!help` prints these in Discord too.

- `!active on|off|status` — join a channel and reply to every 6th message
- `!topic <topic> on|off` — lock replies and GIFs to one subject
- `!language <full name>` — reply language for the whole server. Full name only (`hungarian`, not `hu`). Leave the name off to see the current one
- `!persona rudeish|nerdish|explicit` — show or switch the voice. `explicit` only works in age-restricted channels; anywhere else it falls back to `rudeish`
- `!vc` / `!vc leave` — join your voice channel and say one short line
- `!music help` — `!music <song or URL>`, plus pause / skip / leave and the rest
- `!memory erase` — wipe that server's memory. Needs Manage Server
- `!nuke <1-100>` — delete recent messages. Needs Manage Messages

Every 10th non-command message in a server channel gets a GIF. That's always on. No command for it. Needs `KLIPY_API_KEY` or nothing gets sent.

`!active` and `!topic` survive restarts. `!language` is one setting for the whole server.

Voice and music need FFmpeg on `PATH`.

`!memory erase` checks every channel first, then deletes. Stuff from before the wipe is gone, including anything still being summarized.

## Voices

The personality is just text in these files:

- [`personas/gpt_persona.py`](personas/gpt_persona.py) — `rudeish`
- [`personas/deepseek_persona.py`](personas/deepseek_persona.py) — `nerdish`
- [`personas/persona.py`](personas/persona.py) — `explicit` (age-restricted channels only)

Edit the file, then push it to the running bot:

```sh
update persona
update persona gpt/deepseek/mistral
```

The next message uses the new text. No restart.

`update` lives in `~/.local/bin`. If you move this repo, set `OWAUA_PROJECT_DIR` to the new path.

The names `rudeish` / `nerdish` / `explicit` are leftover from an older setup. I left them because people already type them.

## Config

Copy [`.env.example`](.env.example) to `.env`. The ones you'll actually fill in:

- `DISCORD_TOKEN`
- `OPENAI_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY` — whichever providers you want
- `DEEPSEEK_MODEL` — defaults to `deepseek-flash`
- `OPENAI_SERVICE_TIER` — `fast` by default so GPT uses OpenAI Fast mode. Set `default` if you don't want the extra cost
- `MISTRAL_SERVICE_TIER` — `auto` uses Mistral Priority when the key has it
- `STREAM_RESPONSES` — `true` by default, so Discord shows the reply as it arrives
- `KLIPY_API_KEY` — for the automatic GIFs
- `SIGHTENGINE_API_USER` and `SIGHTENGINE_API_SECRET` — if both are set, the bot deletes messages whose image attachments look AI-generated. Needs Manage Messages in the channel. Threshold defaults to `0.90`; timeout defaults to 15 seconds.

Memory is a SQLite file, default `data/memory.sqlite3`. GPT, DeepSeek, and Mistral cannot read each other's history. That file can have private chats in it, so treat `data/` like `.env`.

Everything else in `.env.example` is caps, rate limits, and retention. The defaults are fine unless you're hitting them. `MEMORY_RETENTION_DAYS=0` means keep messages until someone runs `!memory erase`.

I turn off provider-side storage when the API lets me.

## Deploy

```sh
./scripts/deploy.sh
```

Uploads the bot files and restarts the Daki server. For just a voice change, use `scripts/update-persona.sh`.

## Tests

```sh
python -m unittest discover -s tests -v
```

## License

MIT. See [`LICENSE`](LICENSE).
