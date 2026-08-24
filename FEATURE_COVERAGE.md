# SefBot community feature coverage

This is the release truth table for the dashboard catalog. **Core live** means
the primary workflow runs end to end in Discord. It does not mean exact Dyno
feature parity. **Partial** identifies a usable subset. **Configuration only**
means settings are persisted and audited but no Discord publisher exists yet.

| Module | Coverage | Notes |
| --- | --- | --- |
| AFK | Core live | Status, nickname marker, mention reason, notes, return delivery, list and moderator clear. |
| Action Log | Core live | Message, member, role, channel, emoji and voice events with per-type channels and ignores. |
| Announcements / Welcome | Core live / Partial | Join, leave, ban, channel and DM messages are live; generated welcome images are not. |
| Auto Delete / Auto Message / Auto Purge | Core live | Bounded filters and schedules; pins are preserved. |
| Autoban / Automod | Core live | Deterministic rules, deletes, warnings, timeout/ban and logs. Complex Dyno rule chaining remains narrower. |
| Autoresponder / Autoroles | Core live | Exact/wildcard responses, reactions, join roles and self-assignable ranks. |
| Custom Commands | Partial | Existing prompt-defined commands are live; multi-destination response chains and full Dyno variable syntax are not. |
| Economy, Cards and Battles | Partial | Coins, daily/work/pay, packs, collection, fusion, decks and direct PvP are live. Matchmaking, seasons, arenas, gems shop and Battle Pass are not. |
| Forms | Core live | Builder schema, public/member links, validation, submissions and Discord automation are live. The dashboard uses bounded JSON rather than a drag-and-drop builder. |
| Fun | Core live | Coin, dice, RPS, media/info lookups, polls and coordinate distance. |
| Giveaways | Partial | Timed reaction entries, winner selection and reroll are live. Button/referral/daily entry modes and the public giveaway site are not. |
| Highlights / Levels / Reminders | Core live | Phrase DMs, XP/rewards/multipliers/leaderboards and timed reminders. |
| Message Embedder | Configuration only | Draft definitions are stored and audited; publish/edit management is not wired to Discord yet. |
| Moderation | Partial | Existing confirmation-gated Discord actions, review, logs and purge are live. Full cases/notes/appeals/autopunish parity is not. |
| Reaction Roles | Partial | Reaction add/remove menus are live. Button and dropdown persistent views are not. |
| Reddit / YouTube | Core live | Feed polling and channel delivery. |
| Twitch / Kick / TikTok | Core live | Official-API polling when operator/creator credentials are configured. |
| Slowmode / Starboard | Core live | Bot/native rate limiting and reaction starboard. |
| Tags | Core live | Create, read, edit, delete and list with role restrictions. |
| Tickets | Partial | Private channels, intake, staff access, resolve/close and transcripts. Linked/premium-style panels and custom button builders are not. |
| Voice Text Linking | Core live | Permission grant/revoke, optional notices and empty-channel purge. |
| Server Management / Bot Controls | Partial | Existing safe actions, module toggles and global role/channel/command gates. Full dashboard CRUD for every Discord object is not. |
| Localization | Core live | Bot reply language and server default are integrated; the dashboard UI itself is English-only. |

There are no paid SefBot gates or artificial free-plan limits. External APIs,
Discord limits, hosting resources and abuse-prevention bounds still apply.
