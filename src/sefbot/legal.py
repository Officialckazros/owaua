"""Canonical OpSef Terms of Service and Privacy Notice.

These pages are generated from the running code's actual behaviour: storage
tables, consent gates, provider calls, and deletion gaps. Keep them honest.
"""

from __future__ import annotations

import html
from typing import Final

LEGAL_VERSION: Final = "3.1"
LEGAL_EFFECTIVE_DATE: Final = "24 August 2026"
PUBLIC_BASE_URL: Final = "https://kozzyx.org/sefbot"
TERMS_URL: Final = f"{PUBLIC_BASE_URL}/terms"
PRIVACY_URL: Final = f"{PUBLIC_BASE_URL}/privacy"


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def terms_inner(contact: str) -> str:
    safe_contact = _esc(contact)
    return f"""
<h1>Terms of Service</h1>
<p><strong>Version {LEGAL_VERSION}</strong> — effective {LEGAL_EFFECTIVE_DATE}</p>
<p>These Terms govern your use of <strong>OpSef</strong> (also called SefBot),
a privately operated Discord bot. The public contact for this instance is
<span>{safe_contact}</span>. The full text lives at
<code>{TERMS_URL}</code>. The Privacy Notice is at
<code>{PRIVACY_URL}</code>.</p>

<div class="card">
<p>By running <code>/tos accept</code> or <code>!tos accept</code> you agree to
this exact version. Until you accept, the bot will not chat or run ordinary
commands. <code>/privacy</code>, <code>/tos</code>, <code>/help</code>,
<code>/about</code>, and DM-block commands still work without acceptance so you
can read this, export or delete data, and refuse the service.</p>
<p>Accepting these Terms is <strong>not</strong> consent to store raw message
history. That is a separate opt-in described in the Privacy Notice.</p>
</div>

<h2>1. Who operates this</h2>
<p>OpSef is not a registered company in this codebase. This instance is run by
a private operator. The bot process and its SQLite database run on Daki Hosting
infrastructure in Germany. The public website and legal pages are reached
through Cloudflare. Discord remains a separate service with its own terms.</p>
<p>The Discord user id configured as the bot operator
(<code>SEFBOT_OWNER_ID</code>) is exempt in code from the acceptance gate, the
automatic ToS strike/block path, and some rate limits, and can use operator
tools such as the DM-relay CLI. That exception is real. It does not place the
operator above Discord's rules, Daki's rules, or the law.</p>

<h2>2. Eligibility</h2>
<p>You must be allowed to use Discord. Discord's current age minimum is 13
(or higher where Discord or local law requires it). OpSef does not collect
dates of birth and does not independently verify age. If you are not allowed
to use Discord, you are not allowed to use OpSef.</p>
<p>You may only use OpSef in servers and channels where you are permitted to
be, and only with a Discord account you are authorized to use.</p>

<h2>3. The service, honestly</h2>
<p>OpSef is a Discord assistant. It can chat, remember facts you or
moderators store, run optional moderation helpers, describe images, speak in
voice channels, look up web results, and propose Discord administration
actions. It is not a lawyer, doctor, journalist, or infallible database.</p>
<ul>
<li>Ordinary chat cannot execute Discord actions. Kick, ban, timeout, role,
channel, and purge actions only run after an authorized human confirms an
exact preview, and the bot re-checks permissions and role hierarchy at
confirmation time. Separately, administrators can deliberately enable
deterministic dashboard rules such as autoban, automod, auto-delete, autoroles,
scheduled purge, slowmode, tickets, forms, and giveaways. Those rules can take
the configured action without a per-message confirmation.</li>
<li>Community commands created with <code>!request</code> are prompt
specifications stored as text. They are not executable host code.</li>
<li>The bot's tone can be rude, sexual (adults), or dark-humored depending on
server settings and optional "freaky" mode. Hard limits still apply: no sexual
content involving minors; no doxxing; no credential theft; no malware
help.</li>
<li>Model output can be wrong, invented, biased, or offensive. You are
responsible for what you do with it.</li>
<li>There is no uptime guarantee. Discord, Daki, Cloudflare, and configured AI
providers can fail. The operator can turn the bot off.</li>
</ul>

<h2>4. Acceptable use</h2>
<p>You must follow Discord's Terms of Service, Discord's Developer Policy as
it applies to bots you interact with, the rules of the server you are in, and
applicable law.</p>
<p>Do not use OpSef to:</p>
<ul>
<li>create, seek, or distribute sexual content involving anyone 17 or under,
including fictional depictions the detectors treat as such;</li>
<li>doxx, swat, or publish another person's private address, phone, SSN, or
similar personal data;</li>
<li>steal, phish, or harvest Discord tokens, passwords, sessions, or cookies,
or run token loggers / raid webhooks;</li>
<li>build or spread malware, RATs, or stealers aimed at people;</li>
<li>harass people, evade a block, or interfere with Discord, Daki, or
configured providers;</li>
<li>attempt to extract the hidden system prompt or internal configuration
("ignore previous instructions", dump the rules, and similar). The code
counts those as leak attempts.</li>
</ul>
<p>Automated detectors look for those categories with regular expressions and
with optional model flags. Detectors miss things and also false-positive.
A miss is not permission. A false positive can still warn or block you until
the operator reviews it.</p>

<h2>5. Enforcement</h2>
<p>Clear detector hits on the categories above warn on the first two strikes
and hard-block on the third (<code>TOS_STRIKE_LIMIT = 3</code>). Prompt-leak
attempts have their own strike counter (block at 3). Repeated model-policy
flags can also block. Rate-limit noise does not add ToS strikes.</p>
<p>A hard block stores your Discord user id, a reason, a category, guild and
channel ids if known, a display tag, and a SHA-256 prefix plus length of the
offending text — not the raw message. The operator can unblock ToS blocks
through an owner CLI. Manual operator bans are separate and are not cleared
by the ToS-review CLI.</p>
<p>The operator may also add ids to a static block list. Blocked users can
still use privacy/ToS/help commands so they can export or delete data.</p>

<h2>6. Servers and administrators</h2>
<p>Adding OpSef to a server is an administrator decision. Administrators can
enable or leave disabled raw history, passive moderation, server-rules review,
voice transcription, and the dashboard community/automation modules. New
high-impact dashboard modules are off by default; existing safe features remain
enabled for compatibility. Configuration changes are attributed to the
dashboard session and retained in an audit trail.</p>
<p>Administrators remain responsible for the actions they approve through
<code>/act</code> and for staff review of moderation or rules findings.
OpSef does not ban, kick, timeout, or delete messages merely because a model
said so. Staff buttons and confirmation previews control model-proposed
actions. Deterministic rules explicitly configured by administrators (for
example an account-age autoban or banned-phrase automod rule) are a separate
automation path.</p>

<h2>7. Third-party services</h2>
<p>Using OpSef means Discord delivers your content to the bot, and the bot
may send the minimum needed prompt, image, or audio to whatever AI, search,
speech, or chart providers this instance has configured. Those providers have
their own terms. The Privacy Notice lists the classes of provider the code
can call.</p>
<p>Music commands only return a YouTube search URL. The bot does not
download or rehost audio. Charts are rendered by constructing a QuickChart
URL. Web search may call Tavily and/or DuckDuckGo.</p>

<h2>8. Changes</h2>
<p>If this document's version string changes, previous acceptances stop
working. You will have to read the new version and accept again before
ordinary use. That is how the code works; it is not a courtesy email.</p>

<h2>9. Termination</h2>
<p>You can stop using the bot, reject the Terms, or run
<code>/privacy delete</code>. The operator can remove the bot, block you, or
shut the instance down. Discord can remove the application. Data handling
after that is described in the Privacy Notice, including what deletion does
not erase.</p>

<h2>10. No warranty</h2>
<p>The service is provided as-is. The operator does not promise fitness for
a particular purpose, uninterrupted availability, or that outputs are
accurate or safe to follow. To the extent the law allows, the operator is
not liable for indirect or consequential loss arising from use of the bot.
This does not exclude liability that cannot legally be excluded, including
for death or personal injury caused by negligence where that rule applies.</p>

<h2>11. Contact</h2>
<p>Questions, privacy requests, and reports: <span>{safe_contact}</span>.</p>
<p>These Terms are meant to describe the running software. If a sentence
here and the code disagree, the code is what the bot will do, and that is a
bug in this page which you should report.</p>
"""


def privacy_inner(contact: str) -> str:
    safe_contact = _esc(contact)
    return f"""
<h1>Privacy Notice</h1>
<p><strong>Version {LEGAL_VERSION}</strong> — effective {LEGAL_EFFECTIVE_DATE}</p>
<p>This notice describes what <strong>this OpSef instance</strong> actually
does with personal data, according to the code that is running. Public copy:
<code>{PRIVACY_URL}</code>. Contact: <span>{safe_contact}</span>.</p>

<div class="card">
<p>The controller of the SQLite database on this instance is the private
operator who runs the bot, reachable at the contact above. Discord, Daki
Hosting (Germany), Cloudflare (public web), and any configured AI or search
providers are separate organisations. OpSef does not sell personal data.
There is no advertising SDK in the bot.</p>
</div>

<h2>1. What this does not cover</h2>
<ul>
<li>Data Discord itself stores (messages, profiles, IPs Discord sees, audit
logs). See Discord's privacy policy.</li>
<li>Logs or training use by Groq, OpenRouter, Google, Anthropic, DeepSeek,
Cerebras, Inception, Inferx, Celeris, Tavily, DuckDuckGo, QuickChart, or
whoever else this instance's environment variables point at. Those are their
policies. This operator cannot delete copies they keep.</li>
<li>Other bots in the same server.</li>
<li>The optional desktop pet, except that if you point it at an AI endpoint
yourself, that endpoint sees what you send.</li>
</ul>

<h2>2. What we receive from Discord on every use</h2>
<p>When you mention the bot, DM it, or run a command, Discord delivers at
least: your user id, username, global/display name, the message or slash
input, timestamps, channel and guild ids and names, and whether you have
relevant permissions. If you are a server member, the bot may also read up
to 25 role names, your top role, join date, and account creation date to
build a speaker profile for the model. Attachments you include can be
downloaded. Voice features read voice-state (who is in which channel).</p>
<p>That profile is assembled in memory for the reply. It is not written to
its own table. Pieces of it can still land in stored memories, conversation
turns, or audit rows if another feature saves them.</p>

<h2>3. What we store in SQLite</h2>
<p>The bot's brain is a local SQLite file (<code>SEFBOT_DB</code>, default
<code>sefbot.db</code>) on the Daki server. Tables and what they hold:</p>
<table>
<thead><tr><th>Store</th><th>What it contains</th><th>When it is written</th></tr></thead>
<tbody>
<tr><td>privacy_consents</td><td>Your user id, a scope id (exact guild or DM scope), opted-in flag, timestamp</td><td>When you <code>/privacy opt-in</code> or <code>opt-out</code></td></tr>
<tr><td>server_messages</td><td>Message id, guild/channel ids and names, user id, username, display name, content, optional bad-word flags, time</td><td>Only if the guild (or DM) history feature is on <em>and</em> you opted in for that exact scope</td></tr>
<tr><td>conversations</td><td>Short user/bot turns, truncated to 1500 characters, kept to about 8 turns each way</td><td>Same dual gate as raw history</td></tr>
<tr><td>memories</td><td>Facts about a subject (your id or "server"), author id, guild/scope, importance, timestamps</td><td>When the bot or a moderator saves a memory; scoped to that guild or DM</td></tr>
<tr><td>lessons</td><td>Short style/behavior notes distilled from feedback, with a scope id</td><td>When enough feedback exists and distillation runs; these are guild-level, not owned by one user</td></tr>
<tr><td>feedback</td><td>Your message, the bot reply, thumbs up/down or a correction note, your id, scope</td><td>When you rate a reply</td></tr>
<tr><td>relationships</td><td>Per-user, per-guild bond score, optional nickname, optional grudge text</td><td>As the bot updates how it treats you in that server</td></tr>
<tr><td>quotes</td><td>Saved lines, who they are about, who saved them, guild id</td><td>When someone saves a quote</td></tr>
<tr><td>commands</td><td>Community command name, prompt spec, author id, guild, use count</td><td>When a command is requested/approved</td></tr>
<tr><td>interactions</td><td>Kind of interaction, author, guild, time (counts, not full text)</td><td>As you use features</td></tr>
<tr><td>guild_settings</td><td>Persona, lurk, swear level, allowed channels, history/moderation/rules/STT flags, retention days, log channel ids, optional reply-language default</td><td>When administrators configure the server</td></tr>
<tr><td>action_audit</td><td>Actor id, scope, action type, target id, parameters (JSON, capped), status, result, times</td><td>On confirmed <code>/act</code> and similar gated actions</td></tr>
<tr><td>assistant_action_history</td><td>Actor/scope/channel ids, confirmed action and result, redacted parameters, and an exact inverse when safely reversible</td><td>After a confirmed assistant action, so <code>assistant undo</code> can propose a rollback</td></tr>
<tr><td>module_settings</td><td>Per-server module enabled flags and bounded JSON settings, updater id and time</td><td>When an authenticated dashboard operator saves a module</td></tr>
<tr><td>dashboard_audit</td><td>Server id, dashboard actor label, module/action, limited detail and time</td><td>For dashboard configuration changes; retained under the same content-retention cleanup</td></tr>
<tr><td>community_records</td><td>Typed records for reminders, highlights, tags, warnings, tickets, forms/access links/submissions, giveaways, feed/starboard state and similar enabled workflows</td><td>Only when the corresponding module is enabled and someone uses it</td></tr>
<tr><td>afk_statuses / afk_notes</td><td>Server/user ids, AFK reason, prior nickname, notification preference, and notes deliberately left by members</td><td>While AFK is enabled; status and delivered notes are removed on return</td></tr>
<tr><td>user_levels / daily_claims</td><td>Per-server XP, level, message count, XP cooldown, daily claim time and streak</td><td>When Levels/Economy is enabled and used</td></tr>
<tr><td>dynamic_blocks</td><td>Blocked user id, source (manual/ToS/other), reason, category, SHA-256 prefix of evidence, guild/channel ids, strike notes, last 10 history events</td><td>On ToS hard-block or operator block</td></tr>
<tr><td>user flags (kv)</td><td>ToS version accepted and when; reject time; strike counters; emergency-block flag; DM-block flag; freaky-mode flag; reply-language preference; per-guild STT consent flags</td><td>As those features are used</td></tr>
<tr><td>economy_accounts / work_cooldowns</td><td>Toy currency balance and work timestamps</td><td>If you use those commands</td></tr>
<tr><td>kb_docs</td><td>Knowledge-base passages for a guild (text chunks, topic, source)</td><td>When mods ingest files or <code>!kb add</code></td></tr>
<tr><td>dm_contacts</td><td>User id, display name, last DM time — used by the operator DM CLI</td><td>When the operator DMs you through the bot account</td></tr>
<tr><td>cli_active_conversations</td><td>Operator CLI session heartbeats</td><td>While an operator chat session is open; dropped after a few minutes idle</td></tr>
</tbody>
</table>

<h2>4. Consent gates (this is the important part)</h2>
<ul>
<li><strong>ToS acceptance</strong> unlocks ordinary commands. It is stored as
the version string you accepted. It is not raw-history consent. The tests
assert that.</li>
<li><strong>Raw history</strong> is off by default. In a server it requires
the administrator to enable <code>history_enabled</code> <em>and</em> you to
<code>/privacy opt-in</code> for that exact guild id. In DMs, opt-in alone is
enough (there is no guild admin). If either gate is off, conversation turns
and server_messages are not stored, and live channel context is not pulled
into the prompt.</li>
<li><strong>Channel context</strong>, when history is enabled, is a live
Discord API read of the last ~10 messages. Authors who have not opted in are
skipped. Messages are truncated to 200 characters and sent to the model for
that reply; they are not extra-stored unless the history writer also
runs.</li>
<li><strong>Passive moderation</strong> is off until the process flag
<code>SEFBOT_SAFETY_ENABLED</code> is on and the guild enables it. The safety
model only classifies. It cannot delete, warn, or globally block by itself.
High-confidence hits go to a private staff review with Delete/Dismiss
buttons. Delete re-checks <code>manage_messages</code> at click time.</li>
<li><strong>Server rules</strong> run only if the process is configured for a
specific guild, rules are enabled, and that guild enables them. Findings go
to an approval channel. Approve/Deny re-checks the matching Discord
permission and hierarchy. Denial, timeout, or restart does nothing.</li>
<li><strong>Voice transcription (<code>/stt</code>)</strong> is off by
default, and live receive is currently unavailable in the released
dependency set (the install refuses to downgrade PyNaCl). If it is working:
the process flag must be on, the guild must enable it, the controller needs
<code>manage_channels</code> (or equivalent control), and every non-bot
participant in the voice channel must have granted STT consent for that
guild. Consent is stored as a user flag. The session stops if the controller
leaves, consent changes, or people who have not consented join. Audio is
sliced and sent to Whisper; transcripts are posted to the text channel where
<code>/stt</code> was used. <code>/privacy delete</code> revokes STT
consent.</li>
<li><strong><code>/say</code> TTS</strong> sends the text you asked the bot
to speak to the configured TTS provider and plays audio. It does not require
STT consent.</li>
<li><strong>Dashboard modules</strong> that can moderate, delete, post feeds,
or change roles are off by default. If an administrator enables them, the bot
can inspect live message/member/voice
events for deterministic filters, send notifications, change configured roles
or channel permissions, and store the bounded workflow records listed above.
Public forms accept the answers a visitor submits; member-only forms use an
expiring link issued to that Discord user. Form submissions are rate-limited.</li>
</ul>
<p>Opt-out for a scope revokes consent and deletes raw <code>server_messages</code>
for you in that scope. Explicit memories are left until you export or
delete.</p>

<h2>5. What we send to models and other vendors</h2>
<p>To generate a reply the bot builds a system prompt that can include: the
persona; server mood; your relationship score/nickname/grudge; swear-level;
guild lessons; your speaker profile; memories about you in that exact scope;
recent stored conversation turns; matching server memories; knowledge-base
chunks (delimited as untrusted reference data); and your current message.
That bundle is sent to whichever chat provider is configured for the
route (examples the code knows: Groq, OpenRouter, Google Gemini, Anthropic,
DeepSeek, Cerebras, Inception Mercury, Inferx, Celeris, plus any
OpenAI-compatible <code>SEFBOT_LLM_BASE_URL</code>).</p>
<p>Other outbound calls, only when you or a staff feature use them:</p>
<ul>
<li><strong>Vision</strong> (<code>/describe</code> or the message context
menu): image bytes or a revalidated public HTTPS URL, size-capped (default
8 MB), PNG/JPEG/GIF/WebP, to the vision model. One call returns a
description and a moderation flag.</li>
<li><strong>Web search</strong>: the search query to Tavily and/or DuckDuckGo
(<code>ddgs</code>).</li>
<li><strong>Charts</strong>: labels and numbers encoded into a QuickChart
URL. No scriptable callbacks.</li>
<li><strong>Music</strong>: no media fetch; a YouTube search URL is built
locally.</li>
<li><strong>Multilingual</strong>: you can set a reply language with
<code>!language</code> / <code>/language</code> (stored as a user flag, plus
an optional server default). Incoming language is also detected on-box with
<code>langdetect</code> (not an LLM). If the channel is in the configured
list and no language is set, a translation/reply model may be called.</li>
<li><strong>Safety / rules models</strong>: classifier prompts, not
open-ended control of the server.</li>
<li><strong>Feeds and utilities</strong>: configured subreddit names and
YouTube channel ids go to public Reddit/YouTube endpoints. Twitch and Kick use
operator-supplied app credentials; TikTok uses a creator-authorized Display API
token. Pokémon, iTunes, GitHub, joke, dog-image, and ISS commands send only the
lookup term needed to their fixed public API endpoint. Integration responses
are size-bounded and redirects are refused.</li>
</ul>
<p>Providers see IP addresses of this server, not your home IP, except that
Cloudflare and Discord see client IPs on the public website and the Discord
client respectively. The website worker may forward
<code>CF-Connecting-IP</code> as <code>X-Forwarded-For</code> to the bot's
HTTP port. Health endpoints <code>/healthz</code> and <code>/readyz</code>
do not include user, guild, or provider payloads.</p>

<h2>6. Retention</h2>
<ul>
<li>Raw history and conversation turns: at most 30 days
(<code>SEFBOT_RETENTION_DAYS</code>, hard-capped at 30). Startup deletes
older rows before the bot reports ready. Legacy raw history from older
schema versions is purged on migrate.</li>
<li>Feedback rows older than that retention window are also deleted on
cleanup.</li>
<li>Assistant action/undo history is exact-user and exact-scope, expires
within the same 30-day retention window, and is included in privacy export
and deletion.</li>
<li>Memories, lessons, quotes, relationships, command specs, economy,
consents, and guild settings stay until deleted or no longer needed to run
the bot.</li>
<li>Action-audit rows stay for abuse investigation. They are not wiped by
<code>/privacy delete</code>.</li>
<li>ToS block records keep hashed evidence, not the original message
body.</li>
<li>Operator CLI contacts persist until deleted with your user data.</li>
</ul>

<h2>7. Your controls</h2>
<p>These work without accepting the Terms, and replies are ephemeral where
the slash command is used:</p>
<ul>
<li><code>/privacy status</code> — consent, history-feature state, ToS
status, links.</li>
<li><code>/privacy opt-in</code> / <code>opt-out</code> — this exact
scope only.</li>
<li><code>/privacy export</code> — a private JSON file of data owned by,
authored by, or explicitly about you: consents, memories, conversations,
relationships, quotes, feedback, interactions, raw messages, DM contact
row, dynamic block metadata. Oversized exports are gzip-compressed. This
is not a complete dump of the database.</li>
<li><code>/privacy delete</code> — after a confirmation click, deletes
your memories, conversations, relationships, quotes, feedback,
interactions, raw messages, consents, authored community commands,
economy, DM contact, CLI sessions, dynamic block row, and user flags
(including ToS acceptance and STT consent). It also revokes in-memory STT
sessions.</li>
<li><code>/tos accept</code> or <code>reject</code>.</li>
<li><code>/dmblock</code> / <code>/dmunblock</code> — stop or allow the
operator DM-relay from messaging you. Relayed DMs name the requester
unless sent as anonymous by that tool's rules.</li>
</ul>
<p><strong>What <code>/privacy delete</code> does not erase</strong> (this
is a real gap, not an oversight we are hiding):</p>
<ul>
<li>action_audit rows that name you as actor or target;</li>
<li>guild knowledge-base passages;</li>
<li>guild lessons distilled from mixed feedback;</li>
<li>guild_settings;</li>
<li>the operator's static environment block list;</li>
<li>quotes or memories that are about you but stored under another
subject id, except those the delete query already matches as
<code>subject</code> or <code>about</code>/<code>author</code>;</li>
<li>anything Discord or a model provider retained;</li>
<li>operator backups of the SQLite file if they make any (the running
code does not upload the DB anywhere by itself).</li>
</ul>
<p>Moderators with <code>view_audit_log</code> can inspect another member's
relationship/intelligence in the current server. They cannot read other
servers or your DMs through the bot. Memory is exact-scope: a guild cannot
see another guild's facts.</p>

<h2>8. Children</h2>
<p>OpSef is not directed at children under Discord's minimum age. We do
not knowingly store extra data to identify children. Sexual content
involving minors is banned and is a ToS-block category. If you believe a
child's data is in this instance, contact <span>{safe_contact}</span>.</p>

<h2>9. Security</h2>
<p>The database is a file on the host. Access is whoever can read that
server (the operator, and in principle Daki staff or anyone with the
panel/SFTP credentials). Legal HTTP pages send
<code>default-src 'none'</code> CSP, no cookies, and no user identifiers.
Exports are sent as an ephemeral Discord attachment to you, not published.
Evidence of ToS violations is hashed. Provider API keys live in the
server's <code>.env</code>.</p>
<p>This is not a certified security programme. A compromised Discord token,
Daki account, or API key would expose data. Report issues to
<span>{safe_contact}</span>.</p>

<h2>10. International processing</h2>
<p>The bot host is in Germany (Daki). Discord, Cloudflare, and most
configured model APIs are outside your country, often in the United
States. If you use OpSef, your prompts and identifiers may leave the EU.
This instance does not implement Binding Corporate Rules or a named SCC
packet in code. If that is not acceptable, do not use the bot and use
<code>/privacy delete</code>.</p>

<h2>11. Legal bases (EU/UK readers)</h2>
<p>We are not a law firm. In plain terms the operator relies on: performing
the service you requested after ToS acceptance; consent for raw history and
STT; legitimate interests in stopping CSAM, doxxing, token theft, and
prompt-leak abuse (hashed evidence and strike counters); and Discord's
position as the platform you already have an account with. You can withdraw
consent, reject the Terms, or delete as described above.</p>

<h2>12. Changes</h2>
<p>Material changes bump <code>LEGAL_VERSION</code>. Your previous
acceptance becomes invalid and ordinary commands stay locked until you
accept the new version. <code>/privacy</code> remains available.</p>

<h2>13. Contact</h2>
<p>Privacy and security: <span>{safe_contact}</span>.</p>
<p>If this page and the running code disagree, the code wins and the page
is wrong. Please report that.</p>
"""
