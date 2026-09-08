# Persona test bot

This is a Discord persona bot for actual conversation. It responds in DMs or when mentioned in a server, supports text and image attachments, and keeps durable per-user/per-channel conversation memory in a local SQLite database.

## Fastest local setup

1. Fill in `.env` with the Discord bot token and OpenAI key.
2. Run `./update-persona.sh` to upload the Mistral persona, or pass model names such as `./update-persona.sh deepseek mistral gpt` to upload several personas.
3. The next message uses the new full persona; no restart is needed.

Keep the file simple: edit only the text inside `PERSONA = """..."""`. Everything inside that string becomes the AI's complete system persona.

Each model uses its own persona file: [`persona.py`](persona.py) for the `explicit` Mistral persona, [`gpt_persona.py`](gpt_persona.py) for the `rudeish` GPT persona, and [`deepseek_persona.py`](deepseek_persona.py) for the `nerdish` DeepSeek persona. The active file changes automatically when `!persona` changes. `GPT_PERSONA_FILE`, `DEEPSEEK_PERSONA_FILE`, and `PERSONA_FILE` can override their paths.

## Using the bot

- Send a DM to the bot, or mention it in a server, to get an AI reply.
- Attach a supported image when you want an image-aware reply.
- Users with Manage Messages can run `!nuke N` in a server to silently purge 1–100 messages. Invalid or unauthorized `!nuke` input is ignored.
- Each user can make 25 AI requests in a rolling 45-second window. The bot replies with the retry time when the limit is reached.
- Guild `1535083112709496903` bypasses the bot's AI quotas, context/input/output caps, and moderation checks. OpenAI/provider limits and Discord message limits still apply.
- If `MISTRAL_API_KEY` is set, the `explicit` Mistral persona is the default. Use `!persona explicit`, `!persona nerdish`, or `!persona rudeish` to switch. Use `!persona` to see the current selection. DeepSeek requires `DEEPSEEK_API_KEY`; Mistral requires `MISTRAL_API_KEY`. On Mistral, consensual adult explicit roleplay is enabled (`safe_prompt` is off, and adult-sexual moderation flags are not treated as rejections).

The Discord application needs the Message Content Intent enabled. Invite it with the permissions to View Channels, Send Messages, Embed Links, and Read Message History.

## Durable memory and reliability

Conversation messages are stored in `data/memory.sqlite3`, with WAL transactions and duplicate-event protection. Recent messages are sent verbatim to the model; older messages are condensed asynchronously into a rolling summary and a small list of stable facts. Restarting the bot does not erase memory.

The bot moderates each current Discord text and image input with `omni-moderation-latest` before it can enter memory or reach the Responses API. A moderation outage or malformed response fails closed, and rejected content is not stored in the conversation database. Repeated moderation rejections temporarily block further AI requests for that user; this is separate from the configurable 25-request/45-second per-user limiter.

The bot also includes per-conversation request serialization, stale-request cancellation, temporary-error retries, an optional fallback model for allowed requests, hidden message classification, output validation logging, and natural Discord message splitting.

OpenAI requests continue to use `store: false`. The SQLite file can contain private conversation content, so keep `data/` private and back it up or delete it according to your own retention policy. Set `MEMORY_RETENTION_DAYS` to a positive number to prune old raw messages at startup; `0` keeps them until the database is manually removed.

The most useful optional `.env` settings are:

- Supported models are fixed to `gpt-5.6-luna`, `deepseek-v4-flash` (DeepSeek V4 Flash 0731), and Mistral Small 4 `mistral-small-2603`; no other model IDs or cross-provider fallbacks are accepted.
- `DEEPSEEK_API_KEY`: credentials for `deepseek-v4-flash`; its endpoint is fixed to `https://api.deepseek.com`
- `MISTRAL_API_KEY`: credentials for `mistral-small-2603`; its endpoint is fixed to `https://api.mistral.ai/v1`
- `NON_GPT_MAX_OUTPUT_TOKENS`, `NON_GPT_MAX_CONTEXT_TURNS`, and `NON_GPT_MAX_INPUT_TOKENS`: tighter defaults for Mistral/DeepSeek so lower per-token pricing is not erased by longer completions or differently-sized prompts
- `MEMORY_MODEL`: model used for background memory compression; defaults to `gpt-5.6-luna` so switching chat providers does not add hidden Mistral/DeepSeek requests
- `MEMORY_SUMMARY_MAX_INPUT_TOKENS` and `MEMORY_SUMMARY_MAX_OUTPUT_TOKENS`: bounds for each background memory request
- `MEMORY_DB`: SQLite path, default `data/memory.sqlite3`
- `MAX_CONTEXT_TURNS`: recent verbatim turns kept in each request
- `MEMORY_RETENTION_DAYS`: raw-message retention, where `0` means unlimited
- `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW`: per-user request limiter
- `MAX_INPUT_TOKENS`, `MAX_MESSAGE_CHARS`, and `MAX_ATTACHMENTS`: context-size limits; older turns are dropped when the approximate input-token budget is reached
- `OPENAI_MODERATION_TIMEOUT`: safety-check timeout; failures reject the request
- `MODERATION_ABUSE_MAX_FLAGGED`, `MODERATION_ABUSE_WINDOW`, and `MODERATION_ABUSE_BLOCK_SECONDS`: temporary repeat-rejection control using a monotonic in-memory window

## Daki deployment

Use `./deploy.sh` to upload the complete bot runtime to Daki and restart the server. It automatically includes new root-level Python modules, persona files, shell scripts, and requirements files while excluding credentials, tests, and local metadata.

For persona-only edits, use `./update-persona.sh`. With no arguments it uploads only [`persona.py`](persona.py); model names can be separated by spaces, commas, or `and`, for example `./update-persona.sh "deepseek, mistral and gpt"`.

The bot token and OpenAI key belong only in `.env` or your hosting provider's secret settings—not in this repository.
