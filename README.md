# Persona test bot

This is a Discord persona bot for actual conversation. It responds in DMs or when mentioned in a server, supports text and image attachments, streams replies, and keeps durable per-user/per-channel conversation memory in a local SQLite database.

## Fastest local setup

1. Fill in `.env` with the Discord bot token and OpenAI key.
2. Run `./update-persona.sh` to upload only [`persona.py`](persona.py) to Daki.
3. The next message uses the new full persona; no restart is needed.

Keep the file simple: edit only the text inside `PERSONA = """..."""`. Everything inside that string becomes the AI's complete system persona.

## Using the bot

- Send a DM to the bot, or mention it in a server, to get an AI reply.
- Attach a supported image when you want an image-aware reply.
- Users with Manage Messages can run `!nuke N` in a server to silently purge 1–100 messages. Invalid or unauthorized `!nuke` input is ignored.
- Each user can make 25 AI requests in a rolling 45-second window. The bot replies with the retry time when the limit is reached.

The Discord application needs the Message Content Intent enabled. Invite it with the permissions to View Channels, Send Messages, Embed Links, and Read Message History.

## Durable memory and reliability

Conversation messages are stored in `data/memory.sqlite3`, with WAL transactions and duplicate-event protection. Recent messages are sent verbatim to the model; older messages are condensed asynchronously into a rolling summary and a small list of stable facts. Restarting the bot does not erase memory.

The bot also includes per-conversation request serialization, stale-request cancellation, temporary-error retries, an optional fallback model, hidden message classification, output validation logging, natural Discord message splitting, and a configurable 25-request/45-second per-user limiter.

OpenAI requests continue to use `store: false`. The SQLite file can contain private conversation content, so keep `data/` private and back it up or delete it according to your own retention policy. Set `MEMORY_RETENTION_DAYS` to a positive number to prune old raw messages at startup; `0` keeps them until the database is manually removed.

The most useful optional `.env` settings are:

- `OPENAI_FALLBACK_MODEL`: model used after primary-model retries are exhausted
- `MEMORY_DB`: SQLite path, default `data/memory.sqlite3`
- `MEMORY_MODEL`: model used for background summaries
- `STREAM_RESPONSES`: `1` to progressively update Discord replies
- `MAX_CONTEXT_TURNS`: recent verbatim turns kept in each request
- `MEMORY_RETENTION_DAYS`: raw-message retention, where `0` means unlimited
- `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW`: per-user request limiter

## Docker deployment

Fill in `.env`, then run `./deploy.sh`. The container is named `persona-test-bot`, restarts automatically, and stores SQLite data in the persistent Docker volume `persona-test-bot-data`. For the Daki deployment, `./update-persona.sh` still uploads only `persona.py` and does not deploy these runtime changes.

The bot token and OpenAI key belong only in `.env` or your hosting provider's secret settings—not in this repository.
