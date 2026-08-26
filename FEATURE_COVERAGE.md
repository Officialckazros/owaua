# SefBot community feature coverage

This is the release truth table for the dashboard catalog. **Core live** means
the primary workflow runs end to end in Discord. It does not mean exact Dyno
feature parity. **Partial** identifies a usable subset. **Configuration only**
means settings are persisted and audited but no Discord publisher exists yet.

Non-media attachments are intercepted before archive, command, assistant, or
AI handling. Local ClamAV positives delete the message, create a private
moderator incident, and persistently block the human sender from bot use.
Media is excluded only after MIME, extension, and binary-signature agreement;
scanner failures fail closed without blocking the sender.

| Module | Coverage | Notes |
| --- | --- | --- |
| AI Workflow Toolkit | Core live | 21 read-only text/file/replied-message workflows, consent-aware channel intelligence, private message context actions, grounded fact-check sources, advisory-only staff triage, typed dashboard controls, bounded input/output, and model-link defanging. |
| AFK | Core live | Status, nickname marker, mention reason, notes, return delivery, list and moderator clear. |
| Action Log | Core live | Message, member, role, channel, emoji and voice events with per-type channels and ignores. |
| Announcements / Welcome / Onboarding | Core live | Join, leave, ban, channel and DM messages; rules acknowledgement, starter-channel guidance, opt-in role menus, and delayed help follow-up. Generated welcome images are not. |
| Auto Delete / Auto Message / Auto Purge | Core live | Bounded filters and schedules; pins are preserved. |
| Autoban / Automod | Core live | Deterministic rules, deletes, warnings, timeout/ban and logs. Complex Dyno rule chaining remains narrower. |
| Autoresponder / Autoroles | Core live | Exact/wildcard responses, reactions, join roles and self-assignable ranks. |
| Booster Perks | Core live | Durable current/all-time tracking, existing-booster import, randomized greetings, automatic/personal/level/age roles, gifts, private channels, mention reactions, emoji restrictions, statistic voice channels, event logs and manager correction. Discord does not expose reliable partial boost removals, so those require the explicit correction command. |
| Custom Commands | Partial | Existing prompt-defined commands are live; multi-destination response chains and full Dyno variable syntax are not. |
| Economy, Cards and Battles | Partial | Coins, daily/work/pay, packs, collection, fusion, decks and direct PvP are live. Matchmaking, seasons, arenas, gems shop and Battle Pass are not. |
| Forms | Core live | Builder schema, public/member links, validation, submissions and Discord automation are live. The dashboard uses bounded JSON rather than a drag-and-drop builder. |
| Fun | Core live | Coin, dice, RPS, media/info lookups, polls, coordinate distance, and credentialed Rule34 image lookup restricted to Discord age-restricted channels. |
| Giveaways | Partial | Timed reaction entries, winner selection and reroll are live. Button/referral/daily entry modes and the public giveaway site are not. |
| Highlights / Levels / Reminders | Core live | Phrase DMs, XP/rewards/multipliers/leaderboards and timed reminders. |
| Message Embedder | Configuration only | Draft definitions are stored and audited; publish/edit management is not wired to Discord yet. |
| Moderation / Incident Center | Core live | Searchable cases, private notes, HTTPS evidence references, expiry, member appeals, assignment, consistent timelines, and a unified malware/automod/rules/assistant/ticket queue. Discord mutations remain confirmation-gated. |
| Malware Scanner | Core live | Verified media exclusion, bounded local ClamAV scanning, fail-closed deletion, private incident reports and persistent bot-access blocks. |
| Reaction Roles | Core live | Reaction add/remove plus persistent dashboard-configured select menus, safe bot-role hierarchy checks, and optional rules-ack gating. |
| Reddit / YouTube | Core live | Feed polling and channel delivery. |
| Twitch / Kick / TikTok | Core live | Official-API polling when operator/creator credentials are configured. |
| Slowmode / Starboard | Core live | Bot/native rate limiting and reaction starboard. |
| Tags | Core live | Create, read, edit, delete and list with role restrictions. |
| Tickets | Core live | Persistent panels, pre-open modal intake, private channels, routing/category and staff settings, SLA metadata, assignment, resolve/close and transcripts. |
| Server Health / Digests / Analytics | Core live | Advisory-only weekly health reports, explicit private daily/weekly staff digests, per-module retention inventory, and aggregate CSV without message content or personal profiles. |
| Voice Text Linking | Core live | Permission grant/revoke, optional notices and empty-channel purge. |
| Server Management / Bot Controls | Partial | Existing safe actions, module toggles and global role/channel/command gates. Full dashboard CRUD for every Discord object is not. |
| Localization | Core live | Bot reply language and server default are integrated; the dashboard UI itself is English-only. |

There are no paid SefBot gates or artificial free-plan limits. External APIs,
Discord limits, hosting resources and abuse-prevention bounds still apply.
