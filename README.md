# Persona test bot

This is a separate, minimal Discord bot for testing an AI persona. It only responds in DMs or when mentioned in a server. It supports text and image attachments, keeps a small conversation context in memory, and never writes conversations to disk.

## Fastest local setup

1. Run `./run.sh` once. It creates `.env` and installs dependencies.
2. Put the Discord bot token and OpenAI key in `.env`.
3. Run `./run.sh` again.
4. Edit [`persona.py`](persona.py) at any time. The next message uses the new full persona; no restart is needed.

Keep the file simple: edit only the text inside `PERSONA = """..."""`. Everything inside that string becomes the AI's complete system persona.

The Discord application needs the Message Content Intent enabled. Invite it with the permissions to View Channels, Send Messages, Embed Links, and Read Message History.

## Docker deployment

Fill in `.env`, then run `./deploy.sh`. The container is named `persona-test-bot` and restarts automatically. After changing `persona.py`, rebuild with `./deploy.sh`; for live editing, use `./run.sh` instead.

The bot token and OpenAI key belong only in `.env` or your hosting provider's secret settings—not in this repository.
