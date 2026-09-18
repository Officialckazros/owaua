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

## GitHub Pages

The repository contains the complete static site and deploys `owaua.com/` to
GitHub Pages whenever `main` changes. The checked-in [`CNAME`](CNAME) binds
the Pages site to `owaua.com`.

In the repository settings, enable Pages with **Source: GitHub Actions**. If
the domain should be served by GitHub Pages, point the domain's DNS at the
GitHub Pages endpoints and remove the old hosting route. For an apex domain,
use these records at the DNS provider:

- `A @ 185.199.108.153`
- `A @ 185.199.109.153`
- `A @ 185.199.110.153`
- `A @ 185.199.111.153`
- `CNAME www zeousky.github.io`

If Cloudflare remains authoritative, set these records to **DNS only** while
GitHub validates the certificate. The existing Daki/Cloudflare route currently
serves production, so changing these records is the cutover to GitHub Pages.
