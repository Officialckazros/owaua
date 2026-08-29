# Top.gg listing checklist for owaua

Use this when submitting at https://top.gg/bot/new  
Official bot rules: https://support.top.gg/hc/en-us/articles/23146912808988-Discord-Bot-Guidelines  
(You must also follow Top.gg’s site ToS/Privacy at https://top.gg/terms and https://top.gg/privacy, plus Discord’s ToS and Developer Policy.)

## Compliance changes already made in this repo

| Risk | Top.gg rule | What we did |
|------|-------------|-------------|
| Music lookup | Avoid redistributing copyrighted media | `!music` / `/music` return validated YouTube search/watch links and metadata only. |
| `dm_user` action | DMs must name the author (or say anonymous) **and** have opt-out | Relayed DMs now attribute the requester; `!dmblock` / `!dmunblock` / `!mydm` |
| Thin privacy page | Honest data disclosure (Discord + good practice) | Expanded ToS/Privacy at `/owaua` (age 13+, third-party AI, retention, controls) |
| Drugs / crime facilitation | No depicting/facilitating sale of controlled substances | Persona hard limits: no sourcing/selling drugs; no real crime help |
| NSFW anywhere | NSFW stays in NSFW channels | Channel NSFW flag injected into prompts; adult content blocked in SFW channels |
| Religious auto-harassment | Must abide by Discord ToS | Removed “insult anyone with religion in profile” rule |

## Before you submit

1. **Bot online** the whole time you’re in the review queue (often ~1 week+).
2. **Public & invitable** in the Discord Developer Portal (public bot toggle on).
3. **Correct prefix:** `!` (slash commands also work — you can put `!` or `! /` on the form).
4. **Working help:** `!help` and `/help` must work during review.
5. **Main features work** without Administrator. Invite with only needed perms, e.g.:
   - Send Messages, Embed Links, Read Message History, Add Reactions  
   - Optional mod features: Kick, Ban, Manage Roles, Manage Messages, Moderate Members, Manage Channels, Manage Nicknames  
   - **Never** require Administrator as a blanket invite permission.
6. **Privacy**: mention `!privacy` for in-bot data controls.
8. **Reviewer notes** (suggested):  
   - Prefix `!` · mention the bot or DM to chat · `!help` for commands  
   - AI needs a working API key env on the host  
   - DM opt-out: `!dmblock`  
   - Music returns a validated YouTube link; it never downloads or attaches audio

## Page content rules (Top.gg bot page)

- No spam/filler short description, no NSFW on the page (avatar/images/text).
- No seizure-inducing media, no malware links, no competitor list ads.
- Don’t block essential UI / ads on the page.
- Buttons must go to relevant content; prefix field must match reality.
- Do not sexualize minors anywhere (bot or page).
- Don’t vote-lock most commands (you currently have none vote-locked — good).
- Don’t ask users for passwords/tokens for other services.
- Don’t mention illegal drugs/pharma sales on the listing page either.

## Short description ideas (&lt;140 chars)

```
Chaotic AI Discord bot that learns from your server — memory, mood, custom commands, mods. Prefix: !
```

## Long description should include

- How to invite / get started (`@owaua` or DM, `!help`)
- Command overview
- Privacy link + `!privacy` / `!dmblock`
- That music returns links and metadata only
- That responses are AI-generated and can be wrong/rude
- Support server invite (if you have one, non-expiring)

## After approval

- Stay online; keep features working.
- Keep owner-only dangerous tools locked (you have no public `eval` — good).
- Join Top.gg Discord for decline/approval pings: https://discord.gg/EYHTgJX

## Residual risks (reviewer judgment)

- **Savage / uncensored tone** is allowed if it still respects Discord + the hard limits above; keep SFW channels clean.

- **Fork heritage:** you ported concepts from Airo with heavy modification — fine if unique; don’t claim to be an unmodified fork.
- Top.gg site ToS/Privacy pages are Cloudflare-protected; the **Discord Bot Guidelines** article is the enforceable checklist for listing.

Not legal advice — this is an engineering compliance pass against published Top.gg bot guidelines.
