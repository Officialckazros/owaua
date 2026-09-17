# owaua.com

Static website for the Owaua Discord bot.

## Layout

- `index.html` — English homepage.
- `french/`, `german/`, `greek/`, `hungarian/`, `italian/`, `polish/`, `romanian/`, and `ukrainian/` — localized homepages.
- `privacy/`, `terms/`, and `partnerships/` — standalone content pages.
- `assets/` — shared CSS, JavaScript, fonts, editorial media, and profile images.
- `assets/archive/` — unused legacy assets retained locally for reference; these are not linked by the site.
- `404.html` — fallback page.

Keep page URLs stable when moving files: the deployment serves each directory's
`index.html` at its directory path.

Deployment instructions are in [`AGENTS.md`](AGENTS.md).
