# owaua

owaua is a self-hosted Discord bot for communities that want useful automation without treating every message as training data. It has chat, moderation helpers, server utilities, a small web dashboard, and optional voice and image features. The bot is designed to keep permissions, message retention, and paid AI calls visible and configurable.

This is the source for the bot. It is not a hosted service and it does not include anyone's Discord token, database, server list, archive allowlist, or deployment credentials.

## What it does

- Replies in DMs, mentions, and configured channels.
- Lets people inspect, export, opt out of, and delete their stored data with `/privacy`.
- Keeps message history off unless a server enables it and a member opts in.
- Offers moderator tools, tickets, roles, reminders, feeds, forms, and community automations.
- Treats model-proposed Discord actions as proposals: an authorized person sees a preview and confirms it before the bot acts.
- Scans non-media attachments locally with ClamAV when scanning is enabled.
- Includes an optional dashboard with Discord OAuth, CSRF protection, and a guild-scoped audit trail.

Some integrations need their own credentials. Voice transcription, AI moderation, web search, and provider-backed AI features stay inactive until you configure them.

## Before you run it

You need Python 3.12–3.14 and a Discord application with a bot token. Enable the Message Content intent in the Discord Developer Portal. Server Members intent is recommended for moderation and member lookups.

Create an invite URL with the `bot` scope. Start with only the permissions the bot needs; add moderation, voice, or role permissions only if you turn on those features.

## Quick start

```bash
git clone https://github.com/Officialckazros/owaua.git
cd owaua
cp .env.example .env
chmod 600 .env

python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock

# Add DISCORD_TOKEN and the required web/legal settings to .env first.
PYTHONPATH=src python -m owaua.bot
```

At minimum, set these in `.env`:

| Setting | Why it is needed |
| --- | --- |
| `DISCORD_TOKEN` | Connects the bot to your Discord application. |
| `OWAUA_PRIVACY_CONTACT` | Contact shown on the bot's legal pages. |
| `OWAUA_TOS_ACCEPTANCE_SECRET` | Signs one-time Terms acceptance links. |
| `OWAUA_TOS_PROXY_SECRET` | Authenticates the trusted reverse proxy to the bot. |

Use distinct random values of at least 32 characters for the two secrets. The rest of the environment file is grouped by feature and can be left blank until you use that feature.

## Privacy and safety defaults

- No archive guild, command-sync guild, static block, public site, or privacy contact is configured in this repository. Set your own values in `.env`.
- Raw history requires both a server setting and an individual opt-in. Its normal retention limit is 30 days.
- The optional permanent text archive is disabled by default. Enabling `OWAUA_ARCHIVE_GUILD_IDS` changes your privacy obligations; disclose it clearly to the affected server before you use it.
- AI providers receive only the request data required for the feature. Check the provider's own terms before enabling it.
- Ordinary chat cannot directly run Discord mutations. Assistant and moderation actions remain permission-checked and confirmation-gated.

The shipped Terms and Privacy pages describe the maintainer's deployment, not universal legal advice. If you run your own public instance, review and adapt them for your operator, location, data flows, and hosting setup.

## Configuration notes

`OWAUA_LLM_BASE_URL` and `OWAUA_LLM_API_KEY` configure the general OpenAI-compatible client. Provider-specific variables are optional. Keep all credentials in `.env` or your host's secret manager, never in guild settings or commits.

The dashboard needs a public HTTPS URL, Discord OAuth client id/secret, and a separate session secret. See `.env.example` for the exact names and redirect URI format.

`COMMANDS.txt` is the current command reference. `FEATURE_COVERAGE.md` records what is implemented and what still needs work. `TOPGG.md` is only relevant if you submit a bot listing.

## Development

Run the same checks used by CI:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src
.venv/bin/ruff check src tests desktoppet/pet.py desktoppet/tests

cd cloudflare-worker
npm ci --ignore-scripts
npm test
npm run dry-run
```

The desktop companion has its own instructions in [`desktoppet/README.md`](desktoppet/README.md). The Cloudflare Worker is optional and lives in `cloudflare-worker/`.

## Releases and deployment

Pushing a `v*` tag runs the GitHub release workflow, which tests the project and builds the desktop companion. `scripts/deploy` is a Daki-specific helper retained for this maintainer's infrastructure; it is not required for self-hosting and should not be run against an account you do not control.

## Contributing and security

Small, focused pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first. Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md), rather than opening a public issue.

The project is licensed under [GPL-3.0-only](LICENSE).
