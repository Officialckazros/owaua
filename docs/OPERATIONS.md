# Owaua operations

## Project layout

- `src/owaua/` contains the bot runtime package.
- `personas/`, `pfps/`, and `banners/` are runtime content.
- `scripts/` contains local startup, validation, and deployment commands.
- `models/` contains local Ollama model definitions.
- `docs/` contains operator, contributing, and security documentation.
- `tests/` is local/CI-only and is never uploaded to Daki.
- `owaua.com/` is the static website and is separate from the bot runtime.
- `data/` contains persistent SQLite and audit state; it must survive deploys.

## Daki deployment

The Daki server keeps the bot under `persona-test-bot`. Deployments upload only
the runtime manifest: bot modules, pinned dependencies, startup checks, persona
files, profile pictures, and banners. Tests, website files, docs, and setup
utilities stay local.

Preview the exact upload without contacting Daki:

```sh
OWAUA_DAKI_DRY_RUN=1 ./scripts/deploy-daki.sh
```

Deploy the cloud profile:

```sh
OWAUA_DAKI_ENV_FILE=.env.cloud ./scripts/deploy-daki.sh
```

The deploy script stops the bot before replacement, byte-verifies every upload,
keeps `.env` separate from the runtime manifest, and restarts the existing
server command. The remote `data/` directory and its SQLite database are not
replaced.

For local-only work, use `./scripts/deploy-local.sh`; it never contacts Daki.
