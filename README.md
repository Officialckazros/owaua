# owaua

Owaua is a small Discord companion bot built for relaxed, ongoing conversations. It answers in DMs and when mentioned in a server, can look at image attachments, and remembers the shape of a conversation without sounding like a support ticket.

The voice lives in plain Python files, so changing Owaua’s personality does not mean digging through the bot itself.

## What it does

- Replies in DMs and on mentions
- Handles image attachments
- Keeps separate per-user, per-channel memory for each AI in SQLite
- Supports three editable voices: everyday, playful, and curious
- Streams longer replies into Discord naturally
- Includes rate limits, input checks, moderation, retries, and stale-request handling
- Lets moderators quietly remove a batch of messages with `!nuke N`

## Run it locally

You need Python 3.11+, a Discord application with the Message Content Intent enabled, and an API key for the provider you plan to use.

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in .env
./scripts/run-bots.sh
```

Invite the Discord application with View Channels, Send Messages, Embed Links, and Read Message History. Keep `.env` private. It is ignored by Git on purpose.

## Changing the voice

Edit the text inside one of these files:

- [`personas/persona.py`](personas/persona.py) — the everyday voice
- [`personas/gpt_persona.py`](personas/gpt_persona.py) — the more playful voice
- [`personas/deepseek_persona.py`](personas/deepseek_persona.py) — the curious, nerdier voice

Upload a changed voice to the running Daki instance with:

```sh
./scripts/update-persona.sh
./scripts/update-persona.sh deepseek mistral gpt
```

The next message uses the new text; a restart is not needed.

Use `!persona` to see the active voice and `!persona explicit`, `!persona nerdish`, or `!persona rudeish` to switch it. The names are kept for compatibility with the existing bot setup.

## Configuration

Copy [`.env.example`](.env.example) to `.env` and add the credentials you need. The most useful settings are:

- `DISCORD_TOKEN` — the Discord bot token
- `OPENAI_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY` — provider credentials
- `MEMORY_DB` — SQLite path, defaulting to `data/memory.sqlite3`
- Each provider has an isolated memory namespace: GPT, DeepSeek, and Mistral cannot read one another's conversation history or summaries
- `MAX_CONTEXT_TURNS` — recent turns kept verbatim
- `MEMORY_RETENTION_DAYS` — raw-message retention; `0` means keep until manually removed
- `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW` — per-user request limits
- `MAX_INPUT_TOKENS`, `MAX_MESSAGE_CHARS`, and `MAX_ATTACHMENTS` — input bounds

The bot sends provider requests with storage disabled where supported. The local SQLite file can contain private conversation text, so protect `data/` and choose a retention period that fits the people using the bot.

## Deploy to Daki

`scripts/deploy.sh` uploads the runtime, excludes credentials and local metadata, then restarts the server:

```sh
./scripts/deploy.sh
```

For a voice-only change, use `scripts/update-persona.sh` instead.

## Tests

```sh
python -m unittest discover -s tests -v
```

## Project layout

```text
bot.py                 Application settings, helpers, and entry point
bot_client.py          Discord client lifecycle and message delivery
bot_service.py         Provider requests, moderation, and memory workflows
memory_store.py        SQLite conversation memory
personas/              Editable voice definitions
scripts/               Local run and Daki deployment commands
tests/                 Unit tests for memory, moderation, and helpers
```

## License

This project is released under the MIT License. See [`LICENSE`](LICENSE).
