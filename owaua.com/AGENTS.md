# owaua.com

Static site for the official owaua Discord bot. Edit here, then deploy. Do not wait to be asked.

## Deploy

1. Copy changed files from this folder into `/Users/ckazro/Downloads/my projects/owaua/owaua.com` (same relative paths).
2. Upload those files to the Daki **websites** server as `sites/owaua/<relative>` using `/Users/ckazro/Downloads/my projects/owaua/scripts/deploy`. Resolve the server with `load_project_config("websites")` — not the bots server in `config.json`. Use `DakiClient.write_file`, and `ensure_directory` for new folders (for example `privacy/`).
3. Credentials are in `~/.config/owaua-deploy/config.json`. Do not print that file. The API key can list both servers; the live site is the one named websites.
4. Confirm live pages on `https://owaua.com/` (and the new path). The origin is behind Cloudflare (`x-owaua-proxy: site-hi`).

A full `scripts/deploy websites` bundle is not the usual path: it expects a sibling `website/` tree that is not this workspace.
