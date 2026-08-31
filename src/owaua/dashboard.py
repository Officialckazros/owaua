"""Authenticated, zero-license-cost web dashboard for owaua modules."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
import typing
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, TypeAlias
from urllib.parse import urlencode, urlsplit

from aiohttp import BasicAuth, ClientSession, ClientTimeout, web

from owaua import ai_control, config, db, multilingual, staffops
from owaua.module_catalog import MODULES, public_catalog, public_server_settings
from owaua.scope import Scope

DASHBOARD_PREFIX: Final = "/dashboard"
SESSION_COOKIE: Final = "owaua_dashboard_session"
SESSION_SECONDS: Final = 43_200
AUTH_NONCE_COOKIE: Final = "owaua_dashboard_auth_nonce"
AUTH_NONCE_SECONDS: Final = 600
DISCORD_API: Final = "https://discord.com/api/v10"
DISCORD_GUILDS_JSON_BYTES: Final = 4 * 1024 * 1024
MANAGE_GUILD_PERMISSIONS: Final = 0x8 | 0x20

GuildProvider: TypeAlias = Callable[[], list[dict[str, Any]]]

_form_attempts: dict[str, deque[float]] = defaultdict(deque)
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DashboardAuthConfig:
    """Public and private values needed for Discord OAuth."""

    public_url: str = ""
    session_secret: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""

    def ready(self) -> bool:
        try:
            public = urlsplit(self.public_url)
        except ValueError:
            return False
        return bool(
            public.scheme == "https"
            and public.hostname
            and not public.username
            and not public.password
            and len(self.session_secret) >= 32
            and self.discord_client_id.isdigit()
            and self.discord_client_secret
        )

    @property
    def base_url(self) -> str:
        return self.public_url.rstrip("/")


def _page(title: str, body: str, *, script: str = "") -> str:
    app_script = (
        f'<script src="/dashboard/assets/{html.escape(script, quote=True)}" defer></script>'
        if script
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>{html.escape(title)} · owaua Control</title>
  <link rel="stylesheet" href="/dashboard/assets/app.css">
  {app_script}
</head>
<body>{body}</body>
</html>"""


_LOGIN_HTML: Final = """
<main class="login-shell">
  <section class="login-card">
    <a class="login-brand" href="/">owaua</a>
    <h1>Dashboard</h1>
    <p class="muted">Sign in with Discord to manage servers where you have permission.</p>
    <!-- auth-status -->
    <a class="discord-button" href="/dashboard/auth/discord">Continue with Discord</a>
    <p class="tiny centered"><a href="/owaua/terms">Terms</a> · <a href="/owaua/privacy">Privacy</a></p>
  </section>
</main>
"""

_APP_HTML: Final = """
<div class="app-shell">
  <aside class="sidebar">
    <a class="logo" href="/dashboard"><span class="brand-mark small">S</span><span>owaua</span></a>
    <nav aria-label="Dashboard sections">
      <button class="nav-item active" data-view="overview"><span aria-hidden="true">01</span>Overview</button>
      <button class="nav-item" data-view="modules"><span aria-hidden="true">02</span>Modules</button>
      <button class="nav-item" data-view="language"><span aria-hidden="true">03</span>Language</button>
      <button class="nav-item" data-view="boosters"><span aria-hidden="true">04</span>Booster Perks</button>
      <button class="nav-item" data-view="operations"><span aria-hidden="true">05</span>Incident Center</button>
      <button class="nav-item" data-view="settings"><span aria-hidden="true">06</span>Settings</button>
      <button class="nav-item" data-view="activity"><span aria-hidden="true">07</span>Activity</button>
    </nav>
    <div class="side-bottom">
      <form method="post" action="/dashboard/logout"><button class="logout" type="submit">Sign out</button></form>
    </div>
  </aside>
  <main class="content">
    <header class="topbar">
      <div><h1 id="page-title">Overview</h1><p class="page-description">Server configuration and activity</p></div>
      <div class="server-tools"><span id="load-status" class="load-status" role="status" aria-live="polite">Loading dashboard…</span><label class="server-picker"><span>Server</span><select id="guild-select" aria-label="Select server" disabled><option>Loading…</option></select></label></div>
    </header>
    <div id="notice" class="notice" role="status" aria-live="polite" hidden></div>
    <section id="overview-view" class="view">
      <div class="hero-panel">
        <div><h2>Configuration</h2><p>Review enabled modules and edit settings for the selected server.</p></div>
        <button class="primary" data-go="modules">Open modules</button>
      </div>
      <div class="stats" id="stats"></div>
      <section class="ops-panel"><div class="section-head"><div><h2>AI health</h2><p class="page-description">Privacy-safe request metadata only. Prompts and response contents are never traced.</p></div><button id="ai-health-refresh" class="secondary" type="button">Refresh</button></div><div class="stats" id="ai-health-stats"></div><div id="ai-provider-health" class="activity-list"></div></section>
      <div class="section-head"><h2>Module status</h2><button class="text-button" data-go="modules">View all</button></div>
      <div class="module-grid compact" id="quick-modules"></div>
    </section>
    <section id="modules-view" class="view" hidden>
      <div class="toolbar"><input id="module-search" type="search" placeholder="Search modules" aria-label="Search modules"><div id="category-filters" class="filters"></div></div>
      <div class="module-grid" id="module-grid"></div>
    </section>
    <section id="language-view" class="view" hidden>
      <div class="hero-panel language-hero">
        <div><h2>Guild language</h2><p>One language controls this selected guild's dashboard, commands, normal replies, errors, buttons, automated modules, and AI output.</p></div>
      </div>
      <section class="ops-panel language-panel">
        <form id="language-form" class="language-form">
          <label class="field"><span>Language name or code</span><input id="guild-language" list="language-catalog" maxlength="80" autocomplete="off" placeholder="Russian, Magyar, العربية, ja…"><small>Type any real language or locale. English (or an empty value) restores the original interface.</small></label>
          <datalist id="language-catalog"></datalist>
          <button id="language-save" class="primary" type="submit">Apply everywhere</button>
        </form>
        <div class="language-contract">
          <h3>Applies to everything</h3>
          <p>Dashboard navigation and settings, prefix and slash-command replies, embeds, errors, confirmation controls, scheduled module messages, and AI-generated text all follow this guild setting.</p>
          <p class="tiny">Slash command invocation names remain stable so existing integrations do not break; command help and every bot response are localized.</p>
        </div>
      </section>
    </section>
    <section id="settings-view" class="view" hidden>
      <div class="section-head"><div><h2>Server settings</h2><p class="page-description">Core AI, privacy, moderation, rules, voice and channel behavior.</p></div></div>
      <div id="server-settings" class="settings-grid"></div>
      <div class="settings-actions"><button id="settings-save" class="primary" type="button">Save server settings</button></div>
    </section>
    <section id="boosters-view" class="view" hidden>
      <div class="section-head"><div><h2>Booster Perks</h2><p class="page-description">Tracking, greetings, roles, rewards, private spaces, reactions, counters and logs.</p></div><button id="booster-refresh" class="secondary" type="button">Refresh records</button></div>
      <div class="stats" id="booster-stats"></div>
      <div class="booster-toolbar">
        <label class="switch-row"><span><strong>Booster Perks enabled</strong><small>Master switch for tracking and all configured perks.</small></span><input id="booster-enabled" type="checkbox"><i></i></label>
        <div class="booster-actions"><button type="button" class="secondary" data-booster-action="sync">Import and sync boosters</button><button type="button" class="secondary" data-booster-action="test">Send test greeting</button></div>
      </div>
      <div id="booster-settings" class="booster-layout"></div>
      <div class="settings-actions"><button id="booster-save" class="primary" type="button">Save every Booster Perks setting</button></div>
      <section class="booster-records">
        <div class="section-head"><div><h2>Booster records</h2><p class="page-description">Current and all-time counts. Use a correction when Discord misses one partial removal.</p></div></div>
        <form id="booster-adjust" class="inline-form"><select id="booster-adjust-user" aria-label="Booster member"></select><input id="booster-adjust-delta" type="number" min="-10000" max="10000" step="1" placeholder="+2 or -1" required><button class="secondary" type="submit">Apply correction</button></form>
        <div class="table-wrap"><table class="record-table"><thead><tr><th>Member</th><th>Current</th><th>All-time</th><th>Status</th><th>First boost</th><th>Updated</th></tr></thead><tbody id="booster-record-list"></tbody></table></div>
      </section>
    </section>
    <section id="operations-view" class="view" hidden>
      <div class="section-head"><div><h2>Staff incident center</h2><p class="page-description">One private queue for cases, malware, automod, rules, assistant actions and unresolved tickets.</p></div><div class="booster-actions"><a id="analytics-export" class="secondary" href="#">Export aggregate CSV</a><button id="operations-refresh" class="secondary" type="button">Refresh</button></div></div>
      <div class="stats" id="health-stats"></div>
      <div class="ops-layout">
        <section class="ops-panel"><h3>New moderation case</h3><form id="case-create" class="ops-form"><label>Member ID<input id="case-subject" inputmode="numeric" pattern="[0-9]{1,24}" required></label><label>Category<input id="case-category" maxlength="80" required></label><label>Severity<select id="case-severity"><option>low</option><option selected>medium</option><option>high</option><option>critical</option></select></label><label>Assign to staff ID<input id="case-assignee" inputmode="numeric" pattern="[0-9]{0,24}"></label><label class="full">Reason<textarea id="case-reason" maxlength="2000" required></textarea></label><label class="full">Evidence links, one HTTPS URL per line<textarea id="case-evidence" maxlength="20000"></textarea></label><button class="primary" type="submit">Create searchable case</button></form></section>
        <section class="ops-panel"><h3>Server health advisor</h3><p class="tiny">Advisory only. Recommendations never change settings.</p><div id="health-recommendations" class="activity-list"></div><h3>Scheduled staff digest</h3><form id="digest-config" class="ops-form"><label>Cadence<select id="digest-cadence"><option>daily</option><option selected>weekly</option></select></label><label>Private delivery channel<select id="digest-channel" required></select></label><label>Visibility<select id="digest-visibility"><option>staff</option><option>admins</option></select></label><label>Enabled<select id="digest-enabled"><option value="true">Yes</option><option value="false">No</option></select></label><button class="primary" type="submit">Save digest schedule</button></form></section>
      </div>
      <div class="toolbar ops-filter"><input id="operations-search" type="search" placeholder="Search case, member, incident or reference" aria-label="Search staff operations"><select id="operations-source" aria-label="Incident source"><option value="">All sources</option><option>malware</option><option>automod</option><option>rules</option><option>assistant</option><option>moderation</option><option>ticket</option><option>feed</option></select></div>
      <div class="ops-layout">
        <section class="ops-panel"><div class="section-head"><h3>Moderation cases</h3><span id="case-count" class="tiny"></span></div><div id="case-list" class="activity-list"></div></section>
        <section class="ops-panel"><div class="section-head"><h3>Unified queue</h3><span id="incident-count" class="tiny"></span></div><div id="incident-list" class="activity-list"></div></section>
      </div>
      <section class="ops-panel retention-panel"><div class="section-head"><h3>Retention transparency</h3><span class="tiny">No message content or personal profiles in aggregate exports</span></div><div id="retention-list" class="table-wrap"></div></section>
    </section>
    <section id="activity-view" class="view" hidden>
      <div class="section-head"><h2>Recent changes</h2></div>
      <div class="activity-list" id="activity-list"></div>
    </section>
  </main>
</div>
<dialog id="editor-dialog">
  <form method="dialog" class="dialog-head"><div><p class="dialog-category" id="editor-category"></p><h2 id="editor-title"></h2></div><button class="icon-button" value="cancel" aria-label="Close">×</button></form>
  <div class="dialog-body"><p class="muted" id="editor-description"></p><label class="switch-row"><span><strong>Module enabled</strong><small>Changes take effect immediately after saving.</small></span><input id="editor-enabled" type="checkbox"><i></i></label><div id="editor-fields" class="editor-fields"></div></div>
  <div class="dialog-actions"><button class="secondary" id="editor-cancel" type="button">Cancel</button><button id="editor-save" type="button">Save changes</button></div>
</dialog>
"""

_MONO_CSS: Final = r"""
:root{color-scheme:dark;font-family:Arial,Helvetica,sans-serif;--black:#000;--white:#fff}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{min-height:100vh;margin:0;background:var(--black);color:var(--white)}
button,input,select,textarea{font:inherit}
button,a{color:inherit}
button{cursor:pointer}
h1,h2,h3,p{margin-top:0}
h1,h2,h3{letter-spacing:0}
.muted,.tiny,.page-description,.field small,.switch-row small,.activity-row small{color:var(--white);opacity:.65}
.tiny{font-size:.75rem;line-height:1.5}
.centered{text-align:center}

.login-shell{min-height:100vh;display:grid;place-items:center;padding:1rem}
.login-card{width:min(26rem,100%);padding:2rem;border:1px solid var(--white);background:var(--black)}
.login-brand{display:inline-block;margin-bottom:3rem;text-decoration:none;font-weight:700}
.login-card h1{font-size:2rem;margin-bottom:.75rem}
.login-card .muted{line-height:1.55;margin-bottom:1.5rem}
.discord-button{display:block;width:100%;padding:.85rem 1rem;border:1px solid var(--white);background:var(--white);color:var(--black);text-align:center;text-decoration:none;font-weight:700}
.discord-button:hover,.discord-button:focus{background:var(--black);color:var(--white)}
.auth-message{padding:.75rem;border:1px solid var(--white);margin-bottom:1rem}
.tiny a{text-decoration:underline}

.app-shell{display:grid;grid-template-columns:13rem minmax(0,1fr);min-height:100vh}
.sidebar{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;padding:1rem;border-right:1px solid var(--white);background:var(--black)}
.logo{display:flex;align-items:center;gap:.65rem;padding:.25rem .5rem 2rem;color:var(--white);text-decoration:none;font-weight:700}
.brand-mark{display:grid;place-items:center;width:2rem;height:2rem;border:1px solid var(--white);background:var(--white);color:var(--black);font-weight:700}
.brand-mark.small{width:1.5rem;height:1.5rem;font-size:.75rem}
.sidebar nav{display:grid}
.nav-item,.logout,.text-button,.configure,.icon-button{border:0;background:transparent;color:var(--white)}
.nav-item,.logout{width:100%;padding:.75rem .5rem;text-align:left}
.nav-item span{display:inline-block;width:2rem;opacity:.6}
.nav-item:hover,.nav-item.active{background:var(--white);color:var(--black)}
.side-bottom{margin-top:auto}
.logout{border-top:1px solid var(--white)}

.content{width:100%;max-width:90rem;padding:2rem clamp(1rem,4vw,3rem) 4rem}
.topbar{display:flex;align-items:end;justify-content:space-between;gap:1rem;padding-bottom:1rem;border-bottom:1px solid var(--white);margin-bottom:1.5rem}
.topbar h1{margin:0;font-size:1.5rem}
.page-description{margin:.25rem 0 0;font-size:.8rem}
.server-picker{display:grid;gap:.35rem;font-size:.75rem}
.server-tools{display:flex;align-items:end;gap:1rem}.load-status{max-width:18rem;font-size:.72rem;opacity:.65;text-align:right}
input[type=search],.editor-fields input,.editor-fields textarea,.editor-fields select,.server-picker select{width:100%;padding:.7rem;border:1px solid var(--white);border-radius:0;outline:0;background:var(--black);color:var(--white)}
input:focus,textarea:focus,select:focus,button:focus-visible,a:focus-visible{outline:2px solid var(--white);outline-offset:2px}
button:disabled,input:disabled,select:disabled,textarea:disabled{cursor:not-allowed;opacity:.45}
.hero-panel{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1.25rem;border:1px solid var(--white)}
.hero-panel h2{margin-bottom:.35rem;font-size:1.25rem}
.hero-panel p{margin:0;line-height:1.5}
.primary,.secondary,#editor-save,.public-form button{padding:.7rem .9rem;border:1px solid var(--white);border-radius:0;background:var(--white);color:var(--black);font-weight:700}
.primary:hover,.secondary:hover,#editor-save:hover,.public-form button:hover{background:var(--black);color:var(--white)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--white);border-top:0;margin:0 0 2rem}
#booster-stats{grid-template-columns:repeat(4,1fr)}
.stat{padding:1rem;border-right:1px solid var(--white)}
.stat:last-child{border-right:0}
.stat strong,.stat small{display:block}
.stat strong{font-size:1.4rem}
.stat small{margin-top:.2rem;font-size:.7rem;text-transform:uppercase}
.section-head{display:flex;align-items:end;justify-content:space-between;margin:1rem 0}
.section-head h2{margin:0;font-size:1rem}
.text-button,.configure{text-decoration:underline}
.toolbar{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding-bottom:1rem;border-bottom:1px solid var(--white);margin-bottom:1rem}
.toolbar input{max-width:22rem}
.filters{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:.4rem}
.filter{padding:.5rem .65rem;border:1px solid var(--white);border-radius:0;background:var(--black);color:var(--white)}
.filter.active,.filter:hover{background:var(--white);color:var(--black)}
.module-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(17rem,1fr));gap:0;border-top:1px solid var(--white);border-left:1px solid var(--white)}
.module-card{min-height:11rem;display:flex;flex-direction:column;padding:1rem;border-right:1px solid var(--white);border-bottom:1px solid var(--white);background:var(--black)}
.module-card .category,.dialog-category{font-size:.65rem;text-transform:uppercase;text-decoration:underline}
.module-card h3{margin:.65rem 0 .4rem}
.module-card p{font-size:.8rem;line-height:1.5;opacity:.7}
.module-card footer{display:flex;align-items:center;justify-content:space-between;gap:.75rem;margin-top:auto;font-size:.75rem}
.status:before{content:"○ ";}.status.on:before{content:"● ";}
.compact .module-card:nth-child(n+9){display:none}
.notice{position:fixed;z-index:20;right:1rem;top:1rem;padding:.75rem;border:1px solid var(--white);background:var(--white);color:var(--black)}
.notice.error{background:var(--black);color:var(--white)}
.activity-list{border:1px solid var(--white)}
.activity-row{display:grid;grid-template-columns:1.2fr 1fr .8fr;gap:1rem;padding:1rem;border-bottom:1px solid var(--white);font-size:.8rem}
.activity-row:last-child{border-bottom:0}
.ops-layout{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem;margin:1rem 0}.ops-panel{min-width:0;padding:1rem;border:1px solid var(--white)}.ops-panel>h3{margin-bottom:.4rem}.ops-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.65rem}.ops-form label{display:grid;gap:.3rem;font-size:.72rem}.ops-form .full{grid-column:1/-1}.ops-form input,.ops-form select,.ops-form textarea,.ops-filter select{width:100%;padding:.65rem;border:1px solid var(--white);background:var(--black);color:var(--white)}.ops-form textarea{min-height:5rem;resize:vertical}.ops-item{padding:.8rem;border-bottom:1px solid rgba(255,255,255,.5);font-size:.8rem}.ops-item:last-child{border-bottom:0}.ops-item header{display:flex;justify-content:space-between;gap:.5rem}.ops-item p{margin:.4rem 0;line-height:1.45}.ops-actions{display:flex;gap:.35rem;flex-wrap:wrap}.ops-actions button{padding:.35rem .5rem;border:1px solid var(--white);background:var(--black);color:var(--white);font-size:.7rem}.retention-panel{margin-top:1rem}.retention-panel table{width:100%;border-collapse:collapse;font-size:.75rem}.retention-panel th,.retention-panel td{padding:.65rem;border-bottom:1px solid rgba(255,255,255,.4);text-align:left;vertical-align:top}
.empty{padding:2rem;text-align:center;opacity:.65}

dialog{width:min(64rem,calc(100% - 2rem));max-height:90vh;padding:0;border:1px solid var(--white);border-radius:0;background:var(--black);color:var(--white)}
dialog::backdrop{background:#000}
.dialog-head{display:flex;align-items:start;justify-content:space-between;padding:1rem;border-bottom:1px solid var(--white)}
.dialog-head h2{margin:0}.icon-button{font-size:1.75rem}
.dialog-body{max-height:65vh;overflow:auto;padding:1rem}
.switch-row{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1rem;border:1px solid var(--white);margin:1rem 0}
.switch-row span strong,.switch-row span small{display:block}
.switch-row input{position:static;width:1.2rem;height:1.2rem;accent-color:var(--white)}
.switch-row i{display:none}
.editor-fields{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.field{display:grid;grid-template-columns:minmax(0,1fr);min-width:0;gap:.4rem}.field.full{grid-column:1/-1}
.field textarea{min-height:8rem;resize:vertical;font-family:monospace}
.field select[multiple]{min-height:9rem}
.structured-field{padding:.75rem;border:1px solid rgba(255,255,255,.55)}
.structured-editor,.structured-node,.structured-children{display:grid;min-width:0;gap:.55rem}
.structured-node{padding:.65rem;border-left:2px solid rgba(255,255,255,.55)}
.structured-head{display:flex;align-items:center;justify-content:space-between;gap:.5rem}
.structured-head>strong{font-size:.72rem;text-transform:uppercase;overflow-wrap:anywhere}
.structured-kind{width:auto!important;min-width:7rem;padding:.4rem!important;font-size:.7rem}
.structured-children{padding-left:.35rem}
.structured-item,.structured-object-field{display:grid;grid-template-columns:minmax(0,1fr) auto;min-width:0;gap:.5rem;align-items:start;padding:.55rem;border:1px solid rgba(255,255,255,.35)}
.structured-object-field{grid-template-columns:minmax(7rem,.45fr) minmax(0,1.55fr) auto}
.structured-object-field>.structured-node,.structured-item>.structured-node{border-left:0;padding:0}
.structured-object-key,.structured-value{width:100%;min-width:0;padding:.6rem;border:1px solid var(--white);background:var(--black);color:var(--white)}
.structured-value{font-family:inherit!important}
.structured-value[multiple]{min-height:7rem}
.structured-actions{display:flex;flex-wrap:wrap;gap:.4rem}
.structured-actions button,.structured-remove{padding:.45rem .6rem;border:1px solid var(--white);background:var(--black);color:var(--white);font-size:.7rem}
.structured-empty{padding:.65rem;border:1px dashed rgba(255,255,255,.4);font-size:.72rem;opacity:.7}
.module-config-group{grid-column:1/-1;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.8rem;min-width:0;margin:0;padding:1rem;border:1px solid rgba(255,255,255,.55)}
.module-config-group legend{padding:0 .4rem;font-size:.72rem;text-transform:uppercase}
.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));border-top:1px solid var(--white);border-left:1px solid var(--white)}
.settings-grid .field{min-height:8rem;padding:1rem;border-right:1px solid var(--white);border-bottom:1px solid var(--white);align-content:start}
.settings-grid .field.full{grid-column:1/-1}
.field small{overflow-wrap:anywhere}.settings-grid .field small{line-height:1.45}
.settings-actions{display:flex;justify-content:flex-end;padding-top:1rem}
.language-hero{margin-bottom:1rem}.language-panel{display:grid;grid-template-columns:minmax(0,1fr) minmax(18rem,.8fr);gap:2rem}.language-form{display:grid;gap:1rem;align-content:start}.language-form .field{display:grid;gap:.45rem}.language-form input{width:100%;padding:.85rem;border:1px solid var(--white);background:var(--black);color:var(--white)}.language-form button{justify-self:start}.language-contract{border-left:1px solid var(--white);padding-left:2rem}.language-contract p{line-height:1.55}
.booster-toolbar{display:flex;align-items:center;justify-content:space-between;gap:1rem;margin-bottom:1rem}.booster-toolbar .switch-row{flex:1;margin:0}.booster-actions{display:flex;gap:.5rem;flex-wrap:wrap}
.booster-layout{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}.booster-group{border:1px solid var(--white);padding:1rem;align-content:start}.booster-group h3{margin-bottom:.25rem}.booster-group>p{font-size:.78rem;opacity:.65;line-height:1.45}.booster-group-fields{display:grid;grid-template-columns:1fr 1fr;gap:.8rem}.booster-group-fields .full{grid-column:1/-1}.collection{display:grid;min-width:0;gap:.5rem}.collection-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(8rem,1fr)) auto;min-width:0;gap:.5rem;align-items:end;padding:.65rem;border:1px solid rgba(255,255,255,.45)}.collection-row>*{min-width:0}.collection-row label{display:grid;gap:.3rem;font-size:.72rem}.collection-row input,.collection-row select,.collection-row textarea,.inline-form input,.inline-form select{width:100%;padding:.65rem;border:1px solid var(--white);background:var(--black);color:var(--white)}.collection-row textarea{resize:vertical}.collection-row select[multiple]{min-height:6rem}.remove-row{padding:.65rem;border:1px solid var(--white);background:var(--black)}.add-row{justify-self:start}.booster-records{margin-top:2rem}.inline-form{display:grid;grid-template-columns:minmax(12rem,2fr) minmax(7rem,1fr) auto;gap:.5rem;margin-bottom:1rem}.table-wrap{overflow:auto;border:1px solid var(--white)}.record-table{width:100%;border-collapse:collapse;font-size:.8rem}.record-table th,.record-table td{padding:.75rem;text-align:left;border-bottom:1px solid rgba(255,255,255,.4);white-space:nowrap}.record-table th{font-size:.68rem;text-transform:uppercase}.record-table tr:last-child td{border-bottom:0}
.dialog-actions{display:flex;justify-content:flex-end;gap:.5rem;padding:1rem;border-top:1px solid var(--white)}
.public-form{margin-top:1.5rem}.option-list{display:grid;gap:.5rem}

@media(max-width:800px){.app-shell{display:block}.sidebar{position:static;height:auto;display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;border-right:0;border-bottom:1px solid var(--white)}.sidebar nav{display:flex;overflow-x:auto}.side-bottom{margin:0}.logout{width:auto;border:0;padding:.75rem}.logo{padding:0}.logo span:last-child,.nav-item span{display:none}.nav-item{width:auto;white-space:nowrap;font-size:.75rem}.content{padding:1rem}.topbar,.toolbar,.booster-toolbar{align-items:stretch;flex-direction:column}.server-tools{align-items:stretch;justify-content:space-between}.load-status{text-align:left}.filters{justify-content:flex-start}.activity-row{grid-template-columns:minmax(0,1fr)}.booster-layout,.ops-layout,.language-panel{grid-template-columns:minmax(0,1fr)}.language-contract{border-left:0;border-top:1px solid var(--white);padding-left:0;padding-top:1.25rem}.booster-group{min-width:0}}
@media(max-width:520px){.login-card{padding:1.25rem}.hero-panel{display:block}.hero-panel .primary{width:100%;margin-top:1rem}.stats,#booster-stats{grid-template-columns:minmax(0,1fr)}.stat{border-right:0;border-bottom:1px solid var(--white)}.stat:last-child{border-right:0;border-bottom:0}.module-grid,.editor-fields,.settings-grid,.booster-group-fields,.collection-row,.ops-form,.structured-object-field,.module-config-group{grid-template-columns:minmax(0,1fr)}.settings-grid .field.full,.booster-group-fields .full,.ops-form .full{grid-column:auto}.inline-form{grid-template-columns:minmax(0,1fr)}.booster-actions{display:grid}.structured-children{padding-left:0}.structured-remove{justify-self:start}}
"""

_JS: Final = r"""
const state={csrf:"",catalog:[],configs:new Map(),guilds:[],guildId:"",guildReady:false,category:"All",editing:null,serverSchema:[],serverSettings:{},languageCatalog:[],translations:new Map(),localizationNodes:new Map(),localizationAttrs:[],localizationId:0,localizing:false,boosterData:null,operations:null,aiHealth:null,loadId:0,boosterLoadId:0,activityLoadId:0,operationsLoadId:0,loading:false};
const q=s=>document.querySelector(s),qa=s=>[...document.querySelectorAll(s)];
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
async function api(path,options={}){options={cache:"no-store",...options};options.headers={"Accept":"application/json",...(options.headers||{})};if(options.method&&options.method!=="GET")options.headers["X-CSRF-Token"]=state.csrf;let r;try{r=await fetch(`/dashboard/api${path}`,options)}catch{throw Error("The dashboard could not reach owaua. Check your connection and try again.")}if(r.status===401){location.reload();throw Error("Your dashboard session expired.")};const raw=await r.text();let body;try{body=JSON.parse(raw)}catch{body={error:raw.slice(0,200)||"owaua returned an invalid response."}}if(!r.ok)throw Error(body.error||`Request failed (${r.status})`);return body}
function resetLocalization(){state.localizationId++;for(const [node,source] of state.localizationNodes){if(node.isConnected)node.nodeValue=source}for(const item of state.localizationAttrs){if(item.element.isConnected)item.element.setAttribute(item.name,item.source)}state.localizationNodes.clear();state.localizationAttrs=[];state.translations.clear();document.documentElement.lang="en";document.documentElement.dir="ltr"}
function localizableElement(element){return element&&!["SCRIPT","STYLE","OPTION"].includes(element.tagName)&&!element.closest("#guild-select,.ops-item,.activity-row,.record-table,[data-localization-skip]")}
function localizationSources(){for(const node of state.localizationNodes.keys())if(!node.isConnected)state.localizationNodes.delete(node);state.localizationAttrs=state.localizationAttrs.filter(item=>item.element.isConnected);const entries=[],seen=new Set,walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let node;while((node=walker.nextNode())){if(!localizableElement(node.parentElement))continue;const source=state.localizationNodes.get(node)||node.nodeValue,trimmed=source.trim();if(!trimmed||!/[A-Za-z\u00C0-\uFFFF]/.test(trimmed))continue;if(!state.localizationNodes.has(node))state.localizationNodes.set(node,source);if(!seen.has(trimmed)){seen.add(trimmed);entries.push(trimmed)}}for(const element of qa("[placeholder],[aria-label],[title]")){if(!localizableElement(element))continue;for(const name of ["placeholder","aria-label","title"]){if(!element.hasAttribute(name))continue;let item=state.localizationAttrs.find(x=>x.element===element&&x.name===name);if(!item){item={element,name,source:element.getAttribute(name)||""};state.localizationAttrs.push(item)}const source=item.source.trim();if(source&&!seen.has(source)){seen.add(source);entries.push(source)}}}return entries}
function renderLocalized(translations){for(const [node,source] of state.localizationNodes){if(!node.isConnected)continue;const trimmed=source.trim(),value=translations.get(trimmed);if(!value)continue;const start=source.slice(0,source.indexOf(trimmed)),end=source.slice(source.indexOf(trimmed)+trimmed.length),next=start+value+end;if(node.nodeValue!==next)node.nodeValue=next}for(const item of state.localizationAttrs){if(!item.element.isConnected)continue;const value=translations.get(item.source.trim());if(value&&item.element.getAttribute(item.name)!==value)item.element.setAttribute(item.name,value)}}
async function applyLocalization(){const raw=String(state.serverSettings.language||"").trim(),low=raw.toLowerCase();if(!state.guildId||!state.guildReady||!raw||["en","eng","english"].includes(low)){document.documentElement.lang="en";document.documentElement.dir="ltr";return}const run=++state.localizationId,sources=localizationSources(),missing=sources.filter(x=>!state.translations.has(x));state.localizing=true;try{for(let i=0;i<missing.length;i+=80){const data=await api(`/guild/${encodeURIComponent(state.guildId)}/localization`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({texts:missing.slice(i,i+80)})});if(run!==state.localizationId)return;for(const [source,value] of Object.entries(data.translations||{}))state.translations.set(source,value)}if(run!==state.localizationId)return;const known=state.languageCatalog.find(item=>item.code.toLowerCase()===low||item.label.toLowerCase()===low||item.label.toLowerCase().startsWith(low+" (")),locale=known?.code||raw.slice(0,35);document.documentElement.lang=locale;document.documentElement.dir=/^(ar|he|fa|ur|yi)(-|$)/i.test(locale)?"rtl":"ltr";renderLocalized(state.translations)}catch(e){if(run===state.localizationId)notice(`Language pack unavailable: ${e.message}`,true)}finally{state.localizing=false}}
function scheduleLocalization(){clearTimeout(scheduleLocalization.timer);if(state.localizing)return;scheduleLocalization.timer=setTimeout(()=>applyLocalization(),120)}
const localizationObserver=new MutationObserver(()=>scheduleLocalization());localizationObserver.observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:["placeholder","aria-label","title"]});
function notice(message,error=false){const el=q("#notice");el.textContent=message;el.classList.toggle("error",error);el.setAttribute("role",error?"alert":"status");el.hidden=false;clearTimeout(notice.timer);notice.timer=setTimeout(()=>el.hidden=true,6000)}
function setLoading(loading,message=""){state.loading=loading;q("#load-status").textContent=message||(loading?"Loading…":"Up to date");q("#guild-select").disabled=loading||!state.guilds.length;qa("#settings-save,#language-save,#booster-save,#booster-refresh,#booster-adjust button,[data-booster-action]").forEach(el=>el.disabled=loading||!state.guildId||!state.guildReady)}
const viewCopy={overview:["Overview","Server configuration and activity"],modules:["Modules","Enable and configure server features"],language:["Language","Localize this guild's entire owaua experience"],boosters:["Booster Perks","Track boosts and manage every booster reward"],operations:["Incident Center","Cases, incidents, tickets, health and retention in one staff view"],settings:["Settings","Core AI, privacy, moderation, rules, voice and channel behavior"],activity:["Activity","Authenticated dashboard changes for this server"]};
function showView(name,{updateHash=true}={}){if(!viewCopy[name])name="overview";qa(".view").forEach(x=>x.hidden=x.id!==`${name}-view`);qa(".nav-item").forEach(x=>{const active=x.dataset.view===name;x.classList.toggle("active",active);if(active)x.setAttribute("aria-current","page");else x.removeAttribute("aria-current")});q("#page-title").textContent=viewCopy[name][0];q(".topbar .page-description").textContent=viewCopy[name][1];if(updateHash&&location.hash!==`#${name}`)history.replaceState(null,"",`#${name}`);if(name==="activity")loadActivity();if(name==="boosters")loadBoosters().catch(e=>notice(e.message,true));if(name==="operations")loadOperations().catch(e=>notice(e.message,true))}
function configFor(id){return state.configs.get(id)||{module:id,enabled:false,settings:{}}}
function moduleCard(m){const c=configFor(m.id),coverage=m.implementation||"core live",disabled=!state.guildId||!state.guildReady||state.loading;return `<article class="module-card" data-id="${esc(m.id)}"><span class="category">${esc(m.category)}</span><h3>${esc(m.title)}</h3><p>${esc(m.description)}</p><footer><span class="status ${c.enabled?"on":""}" title="Implementation coverage">${c.enabled?"Enabled":"Disabled"} · ${esc(coverage)}</span><button class="configure" data-edit="${esc(m.id)}" ${disabled?'disabled':''}>Configure →</button></footer></article>`}
function render(){const search=(q("#module-search")?.value||"").toLowerCase();const mods=state.catalog.filter(m=>(state.category==="All"||m.category===state.category)&&(`${m.title} ${m.description}`.toLowerCase().includes(search)));q("#module-grid").innerHTML=mods.map(moduleCard).join("")||'<div class="empty">No matching modules.</div>';q("#quick-modules").innerHTML=state.catalog.map(moduleCard).join("");qa("[data-edit]").forEach(b=>b.onclick=()=>b.dataset.edit==="boosters"?showView("boosters"):b.dataset.edit==="localization"?showView("language"):openEditor(b.dataset.edit));const enabled=[...state.configs.values()].filter(x=>x.enabled).length;q("#stats").innerHTML=[['Modules',state.catalog.length],['Enabled',enabled],['Categories',new Set(state.catalog.map(x=>x.category)).size]].map(([a,b])=>`<div class="stat"><strong>${esc(b)}</strong><small>${esc(a)}</small></div>`).join("")}
function renderAIHealth(){const data=state.aiHealth||{},usage=data.usage||{},providers=data.providers||[];q("#ai-health-stats").innerHTML=[["Requests / 24h",usage.requests||0],["Success",`${usage.success_rate??100}%`],["Avg latency",`${usage.average_latency_ms||0} ms`],["Fallbacks",usage.fallback_requests||0],["Estimated tokens",(usage.input_tokens||0)+(usage.output_tokens||0)]].map(([a,b])=>`<div class="stat"><strong>${esc(b)}</strong><small>${esc(a)}</small></div>`).join("");q("#ai-provider-health").innerHTML=providers.length?providers.map(item=>`<div class="activity-row"><div><strong>${esc(item.model)}</strong><br><small>${item.circuit_open_seconds?`Circuit open ${esc(item.circuit_open_seconds)}s`:'Available'}</small></div><div>${esc(item.health)}% health</div><div><small>${esc(item.latency_ms)} ms · ${esc(item.successes)} ok / ${esc(item.failures)} failed</small></div></div>`).join(""):'<div class="empty">Provider health appears after AI traffic.</div>'}
async function loadAIHealth(){const guildId=state.guildId;if(!guildId||!state.guildReady)return;state.aiHealth=await api(`/guild/${encodeURIComponent(guildId)}/ai-health`);if(guildId===state.guildId)renderAIHealth()}
function filters(){const cats=["All",...new Set(state.catalog.map(x=>x.category))];q("#category-filters").innerHTML=cats.map(x=>`<button class="filter ${x===state.category?"active":""}" data-category="${esc(x)}">${esc(x)}</button>`).join("");qa("[data-category]").forEach(b=>b.onclick=()=>{state.category=b.dataset.category;filters();render()})}
function label(key){return key.replaceAll("_"," ").replace(/\b\w/g,c=>c.toUpperCase())}
function activeGuild(){return state.guilds.find(g=>g.id===state.guildId)||{channels:[],roles:[]}}
function resourceInput(key,value,kind,multiple=false){const items=kind==="channel"?(activeGuild().channels||[]):(activeGuild().roles||[]),selected=new Set((multiple?(Array.isArray(value)?value:[]):[value]).map(String));const options=items.map(item=>`<option value="${esc(item.id)}" ${selected.has(String(item.id))?'selected':''}>${kind==="channel"?'#':'@'}${esc(item.name||item.id)}</option>`).join("");return `<select data-key="${esc(key)}" data-type="${multiple?'string-list':'string'}" ${multiple?'multiple':''}>${multiple?'':'<option value="">Not set</option>'}${options}</select>`}
function choiceInput(key,value,choices){return `<select data-key="${esc(key)}" data-type="string">${choices.map(item=>{const raw=typeof item==="object"?item.value:item,name=typeof item==="object"?item.label:item;return `<option value="${esc(raw)}" ${String(value)===String(raw)?'selected':''}>${esc(name)}</option>`}).join("")}</select>`}
const structuredHelp={
  greet_messages:'Add one greeting template per row. Variables: {user}, {username}, {userboosts}, {level}, {count}, {totalcount}.',
  greet_images:'Add one HTTPS image or GIF URL per row; one is chosen randomly.',
  personal_role_allowed_colors:'Add six-digit hex colors. Empty allows any color.',
  personal_role_banned_words:'Add one blocked word or phrase per row.',
  boost_level_roles:'Add a boost threshold and choose its reward role. Every reached threshold is applied automatically.',
  boost_age_roles:'Add a boosting duration in seconds and choose its reward role. 2592000 is approximately one month.',
  emoji_restrictions:'Add an emoji ID and select the roles allowed to use it.',
  stat_channels:'Configure each live statistic channel with a metric, destination, name and create/delete behavior.',
  log_events:'Any of boost_add, boost_remove, role and channel.',
  log_routes:'Choose an event and its destination channel for each route.',
  audit_events:'Use Discord’s authoritative audit stream for actor, target, reason and before/after details. owaua needs View Audit Log.',
  reaction_events:'Logs reaction adds/removals and poll vote changes. This can be high-volume.',
  include_message_content:'Include edited/deleted text and bounded bulk-delete samples in private log destinations.',
  bulk_delete_sample_size:'Maximum cached message samples included in one bulk-delete event. Limited to 50 at runtime.',
};
const boosterGroups=[
  {title:"Tracking and managers",description:"Automatic import, durable history and delegated dashboard/command access.",keys:["tracking_enabled","manager_role_ids"]},
  {title:"Boost greetings",description:"Every part of channel, embed, plain-text, DM, image and reaction greetings.",keys:["greetings_enabled","greet_channel_id","greet_messages","greet_images","greet_embed","greet_author","greet_author_icon","greet_title","greet_footer","greet_footer_icon","greet_thumbnail","greet_image","greet_color","greet_addon","greet_dm","greet_include_stats","greet_reaction","react_original","react_custom"]},
  {title:"Automatic boost role",description:"Give a configured role on boost and remove configured roles when boosting ends.",keys:["automatic_role_enabled","automatic_role_id","stop_remove_role_id"]},
  {title:"Personal roles",description:"Eligibility, hierarchy, hoisting, colors, naming rules and cleanup for member-owned roles.",keys:["personal_roles_enabled","personal_role_min_boosts","personal_role_base_role_id","personal_role_allow_hoist","personal_role_allowed_colors","personal_role_prefix","personal_role_suffix","personal_role_banned_words","delete_ineligible_personal_role"]},
  {title:"Alternative eligibility and gifts",description:"Let Patreon, VIP, donor, staff or other roles use personal roles and configure sharing.",keys:["qualifying_role_ids","revoke_role_ids","role_gifts_enabled","role_gift_min_boosts","role_gift_slots"]},
  {title:"Boost and age rewards",description:"Assign roles at exact recorded boost counts or after a boosting duration.",keys:["boost_level_roles","boost_age_roles"]},
  {title:"Private channels",description:"Personal text/voice channels, categories, invitation limits, visibility and manager access.",keys:["private_channels_enabled","private_channel_category_id","private_channel_type","private_channel_min_boosts","private_channel_friend_slots","private_channel_invite_min_boosts","private_channel_allow_role_ids","private_channel_deny_role_ids","private_channel_manager_access"]},
  {title:"Mention reactions and emoji access",description:"Member trademark reactions and normal, animated or per-emoji role restrictions.",keys:["mention_reactions_enabled","mention_reaction_min_boosts","emoji_restrictions_enabled","normal_emoji_role_ids","animated_emoji_role_ids","emoji_restrictions"]},
  {title:"Statistic channels and logs",description:"Live counter voice channels, event selection, colors and per-event channel routing.",keys:["stat_channels","log_channel_id","log_color","log_events","log_routes"]},
];
const collectionSchemas={
  greet_messages:{simple:true,fields:[{key:"value",label:"Message template",type:"textarea",default:""}]},
  greet_images:{simple:true,fields:[{key:"value",label:"Image or GIF URL",type:"url",default:""}]},
  personal_role_allowed_colors:{simple:true,fields:[{key:"value",label:"Allowed hex color",type:"text",default:"8a2be2"}]},
  personal_role_banned_words:{simple:true,fields:[{key:"value",label:"Banned word or phrase",type:"text",default:""}]},
  boost_level_roles:{fields:[{key:"boosts",label:"Recorded boosts",type:"number",default:1},{key:"role_id",label:"Reward role",type:"role",default:""}]},
  boost_age_roles:{fields:[{key:"seconds",label:"Boosting seconds",type:"number",default:2592000},{key:"role_id",label:"Reward role",type:"role",default:""}]},
  emoji_restrictions:{fields:[{key:"emoji_id",label:"Emoji ID",type:"text",default:""},{key:"role_ids",label:"Allowed roles",type:"roles",default:[]}]},
  stat_channels:{fields:[{key:"metric",label:"Metric",type:"choice",options:["current_boosts","all_time_boosts","current_boosters","all_time_boosters"],default:"current_boosts"},{key:"channel_id",label:"Existing voice channel",type:"channel",default:""},{key:"category_id",label:"Category for creation",type:"channel",default:""},{key:"name",label:"Name template",type:"text",default:"Boosts: {value}"},{key:"create",label:"Create channel",type:"bool",default:false},{key:"delete",label:"Delete channel",type:"bool",default:false}]},
  log_events:{simple:true,fields:[{key:"value",label:"Event",type:"choice",options:["boost_add","boost_remove","role","channel"],default:"boost_add"}]},
  log_routes:{dictionary:true,fields:[{key:"event",label:"Event",type:"choice",options:["boost_add","boost_remove","role","channel"],default:"boost_add"},{key:"channel_id",label:"Log channel",type:"channel",default:""}]},
};
const structuredTemplates={
  "auto_delete.rules":{enabled:true,channel_id:"",match:"all",delay_seconds:0,filters:[{kind:"includes_text",value:""}]},
  "auto_delete.rules[].filters":{kind:"includes_text",value:""},
  "auto_message.messages":{enabled:true,channel_id:"",content:"",first_at:0,interval_seconds:3600},
  "auto_purge.rules":{enabled:true,channel_id:"",first_at:0,interval_seconds:3600,maximum:100,match:"all",filters:[{kind:"includes_text",value:""}]},
  "auto_purge.rules[].filters":{kind:"includes_text",value:""},
  "autoresponder.responders":{trigger:"",match:"contains",response:"",reactions:[],allowed_role_ids:[],ignored_role_ids:[],allowed_channel_ids:[],ignored_channel_ids:[]},
  "autoroles.join_roles":{role_id:"",delay_seconds:0,remove_after_seconds:0},
  "autoroles.ranks":{name:"",role_id:""},
  "custom_commands.commands":{name:"",description:"",response:"",cooldown_seconds:0,allowed_role_ids:[],allowed_channel_ids:[]},
  "forms.forms":{enabled:true,slug:"new-form",title:"New form",description:"",members_only:false,submit_channel_id:"",fields:[{id:"question",label:"Question",type:"text",required:true,max_length:500}]},
  "forms.forms[].fields":{id:"question",label:"Question",type:"text",required:true,placeholder:"",max_length:500,options:[]},
  "giveaways.giveaways":{enabled:true,channel_id:"",title:"Giveaway",description:"",ends_at:0,winners:1,required_role_ids:[]},
  "reddit.subscriptions":{subreddit:"",channel_id:"",message:"New post in r/{subreddit}: **{title}**\n{link}",flair:"",include_nsfw:false},
  "youtube.subscriptions":{youtube_channel_id:"",channel_id:"",message:"**{video.title}**\n{video.link}"},
  "twitch.subscriptions":{username:"",channel_id:"",message:"**{streamer}** is live: {title}\n{link}"},
  "kick.subscriptions":{broadcaster_user_id:"",username:"",channel_id:"",message:"**{streamer}** is live: {title}\n{link}"},
  "tiktok.subscriptions":{channel_id:"",message:"{caption}\n{link}"},
  "levels.reward_roles":{level:1,role_id:""},
  "levels.channel_multipliers.*":1,
  "embedder.embeds":{id:"new-embed",title:"",description:"",channel_id:"",color:"5865f2",footer:"",image_url:"",thumbnail_url:""},
  "moderation.autopunish":{trigger:"",action:"timeout",minutes:10,reason:""},
  "moderation.custom_responses.*":"",
  "reaction_roles.menus":{id:"default",title:"Choose roles",description:"",placeholder:"Choose your roles",mode:"toggle",max_values:1,items:[{label:"Role",description:"",emoji:"",role_id:""}]},
  "reaction_roles.menus[].items":{label:"Role",description:"",emoji:"",role_id:""},
  "slowmode.channels":{channel_id:"",seconds:5},
  "tickets.panels":{id:"default",title:"Support",description:"Open a private support ticket.",button_label:"Open ticket",category_id:"",staff_role_ids:[],assigned_to:"",intake_fields:[],routing_rules:[]},
  "tickets.panels[].intake_fields":{id:"subject",label:"How can staff help?",required:true,style:"paragraph",placeholder:"",max_length:500},
  "tickets.panels[].routing_rules":{field_id:"subject",operator:"contains",value:"",category_id:"",staff_role_ids:[],assigned_to:""},
  "tickets.intake_fields":{id:"subject",label:"How can staff help?",required:true,style:"paragraph",placeholder:"",max_length:500},
  "tickets.routing_rules":{field_id:"subject",operator:"contains",value:"",category_id:"",staff_role_ids:[],assigned_to:""},
  "voice_text.bindings":{voice_channel_id:"",text_channel_id:"",join_message:"",leave_message:"",purge_when_empty:false},
  "welcome.embed":{title:"",description:"",color:"5865f2",footer:"",image_url:"",thumbnail_url:""},
  "welcome.role_choices":{label:"Starter role",description:"",role_id:""},
  "welcome.intro_questions":{id:"intro",label:"Tell us about yourself",required:false,paragraph:true,max_length:200},
  "incident_center.sla_hours.*":24
};
const fixedStructuredObjects=new Set(["welcome.embed"]);
const moduleEditorGroups={
  action_log:[
    {title:"Global destination",keys:["channel_id"]},
    {title:"Event coverage",keys:["audit_events","message_events","member_events","moderation_events","voice_events","role_events","channel_events","thread_events","server_events","reaction_events","command_events"]},
    {title:"Log detail",keys:["include_message_content","include_attachments","include_audit_changes","include_reasons","include_ids","include_timestamps","include_bot_events","include_voice_state_changes","bulk_delete_sample_size","show_account_age","show_avatars"]},
    {title:"Exclusions",keys:["ignored_channel_ids","ignored_category_ids","ignored_role_ids","ignored_user_ids"]},
  ],
};
function cloneTemplate(value){if(value===undefined)return "";return JSON.parse(JSON.stringify(value))}
function templateFor(path){return cloneTemplate(structuredTemplates[path])}
function prepareStructured(path,value){const template=structuredTemplates[path];if(fixedStructuredObjects.has(path)&&template&&value&&typeof value==="object"&&!Array.isArray(value))return {...cloneTemplate(template),...value};return value}
function nodeKind(key,value){if(/_channel_ids$/.test(key))return "channels";if(/_role_ids$/.test(key))return "roles";if(/_channel_id$/.test(key))return "channel";if(/_role_id$/.test(key))return "role";if(Array.isArray(value))return "list";if(value!==null&&typeof value==="object")return "group";if(typeof value==="boolean")return "bool";if(typeof value==="number")return "number";return /message|content|description|response|reason|body|topic|footer/.test(key)?"textarea":"text"}
function defaultForKind(kind){return kind==="number"?0:kind==="bool"?false:kind==="list"?[]:kind==="group"?{}:kind==="channels"||kind==="roles"?[]:""}
function kindPicker(kind){const choices={text:"Text",textarea:"Long text",number:"Number",bool:"Yes / no",channel:"Channel",channels:"Channel list",role:"Role",roles:"Role list",list:"List",group:"Field group"};return `<select class="structured-kind" data-change-kind aria-label="Value type">${Object.entries(choices).map(([value,name])=>`<option value="${value}" ${value===kind?'selected':''}>${name}</option>`).join('')}</select>`}
function structuredResource(value,kind,multiple=false){const items=kind==="channel"?(activeGuild().channels||[]):(activeGuild().roles||[]),selected=new Set((multiple?(Array.isArray(value)?value:[]):[value]).map(String));return `<select class="structured-value" data-structured-value ${multiple?'multiple':''}>${multiple?'':'<option value="">Not set</option>'}${items.map(item=>`<option value="${esc(item.id)}" ${selected.has(String(item.id))?'selected':''}>${kind==="channel"?'#':'@'}${esc(item.name||item.id)}</option>`).join('')}</select>`}
function structuredScalar(kind,value){if(kind==="channel"||kind==="role")return structuredResource(value,kind);if(kind==="channels"||kind==="roles")return structuredResource(value,kind.slice(0,-1),true);if(kind==="bool")return `<select class="structured-value" data-structured-value><option value="true" ${value?'selected':''}>Yes</option><option value="false" ${!value?'selected':''}>No</option></select>`;if(kind==="number")return `<input class="structured-value" data-structured-value type="number" step="any" value="${esc(value??0)}">`;if(kind==="textarea")return `<textarea class="structured-value" data-structured-value rows="3">${esc(value??'')}</textarea>`;return `<input class="structured-value" data-structured-value type="text" value="${esc(value??'')}">`}
function structuredObjectField(key,value,path){return `<div class="structured-object-field" data-object-field><input class="structured-object-key" data-object-key aria-label="Field name" value="${esc(key)}">${structuredNode(value,path,key)}<button class="structured-remove" data-remove-structured type="button">Remove field</button></div>`}
function structuredNode(rawValue,path,key="",forcedKind=""){const value=prepareStructured(path,rawValue),kind=forcedKind||nodeKind(key,value);let body;if(kind==="list"){const items=Array.isArray(value)?value:[];body=`<div class="structured-children" data-list-items>${items.map(item=>`<div class="structured-item" data-list-item>${structuredNode(item,`${path}[]`,"item")}<button class="structured-remove" data-remove-structured type="button">Remove item</button></div>`).join('')||'<div class="structured-empty">No items yet.</div>'}</div><div class="structured-actions"><button type="button" data-add-list>Add item</button></div>`}else if(kind==="group"){const entries=value&&typeof value==="object"&&!Array.isArray(value)?Object.entries(value):[];body=`<div class="structured-children" data-object-fields>${entries.map(([name,item])=>structuredObjectField(name,item,`${path}.${name}`)).join('')||'<div class="structured-empty">No fields yet.</div>'}</div><div class="structured-actions"><button type="button" data-add-field>Add field</button></div>`}else body=structuredScalar(kind,value);return `<div class="structured-node" data-structured-node data-kind="${esc(kind)}" data-path="${esc(path)}"><div class="structured-head"><strong>${esc(key?label(key):label(path.split('.').pop()||'value'))}</strong>${kindPicker(kind)}</div><div class="structured-body">${body}</div></div>`}
function structuredEditor(module,key,value){const path=`${module}.${key}`;return `<div class="structured-editor" data-structured-key="${esc(key)}">${structuredNode(value,path,key)}</div>`}
function readStructuredNode(node){const kind=node.dataset.kind,body=node.querySelector(':scope > .structured-body');if(kind==="list")return [...body.querySelectorAll(':scope > [data-list-items] > [data-list-item]')].map(item=>readStructuredNode(item.querySelector(':scope > [data-structured-node]')));if(kind==="group")return Object.fromEntries([...body.querySelectorAll(':scope > [data-object-fields] > [data-object-field]')].map(field=>{const key=field.querySelector(':scope > [data-object-key]').value.trim();if(!key)throw Error('Every structured field needs a name.');return [key,readStructuredNode(field.querySelector(':scope > [data-structured-node]'))]}));const control=body.querySelector(':scope > [data-structured-value]');if(!control)return defaultForKind(kind);if(kind==="number"){const number=Number(control.value);if(!Number.isFinite(number))throw Error('A structured number is invalid.');return number}if(kind==="bool")return control.value==="true";if(kind==="channels"||kind==="roles")return [...control.selectedOptions].map(item=>item.value);return control.value}
function nestedResource(value,kind,multiple=false){const items=kind==="channel"?(activeGuild().channels||[]):(activeGuild().roles||[]),selected=new Set((multiple?(Array.isArray(value)?value:[]):[value]).map(String));return `<select data-subtype="${multiple?'strings':'string'}" ${multiple?'multiple':''}>${multiple?'':'<option value="">Not set</option>'}${items.map(item=>`<option value="${esc(item.id)}" ${selected.has(String(item.id))?'selected':''}>${kind==="channel"?'#':'@'}${esc(item.name||item.id)}</option>`).join('')}</select>`}
function nestedInput(field,value){if(field.type==="role")return nestedResource(value,"role");if(field.type==="roles")return nestedResource(value,"role",true);if(field.type==="channel")return nestedResource(value,"channel");if(field.type==="choice")return `<select data-subtype="string">${field.options.map(item=>`<option value="${esc(item)}" ${String(value)===item?'selected':''}>${esc(label(item))}</option>`).join('')}</select>`;if(field.type==="bool")return `<select data-subtype="bool"><option value="true" ${value?'selected':''}>Yes</option><option value="false" ${!value?'selected':''}>No</option></select>`;if(field.type==="number")return `<input data-subtype="number" type="number" step="1" value="${esc(value??field.default)}">`;if(field.type==="textarea")return `<textarea data-subtype="string" rows="3">${esc(value??field.default)}</textarea>`;return `<input data-subtype="string" type="${field.type==='url'?'url':'text'}" value="${esc(value??field.default)}">`}
function collectionRow(key,item={}){const schema=collectionSchemas[key],simpleValue=item&&typeof item==="object"?(item.value??schema.fields[0].default):item,value=schema.simple?{value:simpleValue}:item;return `<div class="collection-row" data-collection-row>${schema.fields.map(field=>`<label><span>${esc(field.label)}</span><span data-subkey="${esc(field.key)}">${nestedInput(field,value?.[field.key]??field.default)}</span></label>`).join('')}<button class="remove-row" type="button" aria-label="Remove row">Remove</button></div>`}
function collectionEditor(key,value){const schema=collectionSchemas[key];let rows;if(schema.dictionary)rows=Object.entries(value&&typeof value==="object"&&!Array.isArray(value)?value:{}).map(([event,channel_id])=>({event,channel_id}));else rows=Array.isArray(value)?value:[];return `<div class="collection" data-collection="${esc(key)}">${rows.map(item=>collectionRow(key,item)).join('')}<button class="secondary add-row" data-add-row="${esc(key)}" type="button">Add ${esc(label(key).replace(/s$/,''))}</button></div>`}
function boosterField(key,value){const schema=collectionSchemas[key],complex=!!schema;if(complex)return `<label class="field full"><span>${esc(label(key))}</span>${collectionEditor(key,value)}<small>${esc(structuredHelp[key]||'Add, remove and edit each row with the controls below.')}</small></label>`;const full=Array.isArray(value)||key.includes("message")||key.includes("addon");let control;if(key==="private_channel_type")control=choiceInput(key,value,["text","voice","both"]);else if(key==="private_channel_category_id")control=resourceInput(key,value,"channel");else control=inputFor(key,value);return `<label class="field ${full?'full':''}"><span>${esc(label(key))}</span>${control}${structuredHelp[key]?`<small>${esc(structuredHelp[key])}</small>`:''}</label>`}
function renderBoosterSettings(){const config=configFor("boosters"),values=config.settings||{};q("#booster-enabled").checked=!!config.enabled;q("#booster-settings").innerHTML=boosterGroups.map(group=>`<section class="booster-group"><h3>${esc(group.title)}</h3><p>${esc(group.description)}</p><div class="booster-group-fields">${group.keys.map(key=>boosterField(key,values[key])).join('')}</div></section>`).join('')}
function readNested(row,field){const host=row.querySelector(`[data-subkey="${field.key}"]`),el=host?.querySelector('input,select,textarea');if(!el)return field.default;if(el.dataset.subtype==="strings")return [...el.selectedOptions].map(x=>x.value);if(el.dataset.subtype==="bool")return el.value==="true";if(el.dataset.subtype==="number"){const number=Number(el.value);if(!Number.isFinite(number))throw Error(`${field.label} must be a number.`);return number}return el.value}
function readCollection(host,key){const schema=collectionSchemas[key],items=qaFrom(host,"[data-collection-row]").map(row=>Object.fromEntries(schema.fields.map(field=>[field.key,readNested(row,field)])));if(schema.dictionary)return Object.fromEntries(items.filter(item=>item.event&&item.channel_id).map(item=>[item.event,item.channel_id]));if(schema.simple)return items.map(item=>item.value).filter(value=>String(value).trim()!=="");return items}
function qaFrom(root,selector){return [...root.querySelectorAll(selector)]}
function readBoosterSettings(){const values=readFields("#booster-settings");qa("#booster-settings [data-collection]").forEach(host=>{values[host.dataset.collection]=readCollection(host,host.dataset.collection)});return values}
function memberName(id){const member=(state.boosterData?.members||[]).find(item=>item.id===String(id));return member?.name||`User ${id}`}
function renderBoosterData(){const data=state.boosterData||{stats:{},records:[],members:[]},stats=data.stats||{};q("#booster-stats").innerHTML=[["Current boosts",stats.current_boosts||0],["All-time boosts",stats.all_time_boosts||0],["Current boosters",stats.current_boosters||0],["All-time boosters",stats.all_time_boosters||0]].map(([a,b])=>`<div class="stat"><strong>${esc(b)}</strong><small>${esc(a)}</small></div>`).join('');q("#booster-record-list").innerHTML=(data.records||[]).map(row=>`<tr><td>${esc(row.name||memberName(row.user_id))}<br><small>${esc(row.user_id)}</small></td><td>${esc(row.current_boosts)}</td><td>${esc(row.all_time_boosts)}</td><td>${row.active?'Boosting':'Stopped'}</td><td>${row.first_boosted?new Date(row.first_boosted*1000).toLocaleString():'—'}</td><td>${row.updated?new Date(row.updated*1000).toLocaleString():'—'}</td></tr>`).join('')||'<tr><td colspan="6" class="empty">No booster history recorded yet.</td></tr>';const selected=q("#booster-adjust-user").value;q("#booster-adjust-user").innerHTML=(data.members||[]).map(member=>`<option value="${esc(member.id)}" ${member.id===selected?'selected':''}>${esc(member.name)} (${esc(member.id)})</option>`).join('')}
async function loadBoosters(){const guildId=state.guildId;if(!guildId||!state.guildReady)return;const requestId=++state.boosterLoadId;const data=await api(`/guild/${encodeURIComponent(guildId)}/boosters`);if(requestId!==state.boosterLoadId||guildId!==state.guildId)return;state.boosterData=data;renderBoosterSettings();renderBoosterData()}
async function saveBoosters(){const guildId=state.guildId;if(!guildId||!state.guildReady){notice("Choose a connected server and wait for it to finish loading.",true);return}let settings;try{settings=readBoosterSettings()}catch(e){notice(e.message||"A Booster Perks setting is invalid.",true);return}const button=q("#booster-save");button.disabled=true;try{const result=await api(`/guild/${encodeURIComponent(guildId)}/module/boosters`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:q("#booster-enabled").checked,settings})});if(guildId!==state.guildId)return;state.configs.set("boosters",result);renderBoosterSettings();render();notice("Every Booster Perks setting was saved.")}catch(e){notice(e.message,true)}finally{button.disabled=state.loading||!state.guildId||!state.guildReady}}
async function boosterAction(action){const guildId=state.guildId;if(!guildId||!state.guildReady){notice("Choose a connected server and wait for it to finish loading.",true);return}const target=q("#booster-adjust-user").value,button=q(`[data-booster-action="${action}"]`);if(action==="test"&&!target){notice("Choose a server member before sending a test greeting.",true);return}if(button)button.disabled=true;try{const result=await api(`/guild/${encodeURIComponent(guildId)}/boosters/action`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,target_id:target})});notice(result.message||"Booster action queued.");if(guildId===state.guildId)await loadBoosters()}catch(e){notice(e.message,true)}finally{if(button)button.disabled=state.loading||!state.guildId||!state.guildReady}}
async function adjustBooster(event){event.preventDefault();const guildId=state.guildId,target_id=q("#booster-adjust-user").value,delta=Number(q("#booster-adjust-delta").value);if(!guildId||!state.guildReady){notice("Choose a connected server and wait for it to finish loading.",true);return}if(!target_id||!Number.isInteger(delta)||delta===0){notice("Choose a member and enter a non-zero whole-number correction.",true);return}const button=event.submitter;button.disabled=true;try{await api(`/guild/${encodeURIComponent(guildId)}/boosters/action`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"adjust",target_id,delta})});if(guildId!==state.guildId)return;q("#booster-adjust-delta").value="";notice("Booster count corrected.");await loadBoosters()}catch(e){notice(e.message,true)}finally{button.disabled=state.loading||!state.guildId||!state.guildReady}}
function inputFor(key,value,meta={}){const kind=meta.kind||"";if(kind==="channel_id"||(!kind&&/_channel_id$/.test(key)))return resourceInput(key,value,"channel");if(kind==="channel_ids"||(!kind&&/_channel_ids$/.test(key)))return resourceInput(key,value,"channel",true);if(kind==="role_id"||(!kind&&/_role_id$/.test(key)))return resourceInput(key,value,"role");if(kind==="role_ids"||(!kind&&/_role_ids$/.test(key)))return resourceInput(key,value,"role",true);if(kind==="choice"||kind==="model")return choiceInput(key,value,meta.choices||[]);if(kind==="boolean"||typeof value==="boolean")return `<select data-key="${esc(key)}" data-type="bool"><option value="true" ${value?'selected':''}>Yes</option><option value="false" ${!value?'selected':''}>No</option></select>`;if(kind==="integer"||typeof value==="number")return `<input data-key="${esc(key)}" data-type="number" type="number" step="${Number.isInteger(value)?'1':'any'}" ${meta.minimum!==undefined?`min="${esc(meta.minimum)}"`:''} ${meta.maximum!==undefined?`max="${esc(meta.maximum)}"`:''} value="${esc(value)}">`;if(kind==="textarea")return `<textarea data-key="${esc(key)}" data-type="string" maxlength="${esc(meta.max_length||4000)}">${esc(value)}</textarea>`;if(value!==null&&typeof value==="object")return structuredEditor("field",key,value);return `<input data-key="${esc(key)}" data-type="string" ${meta.max_length?`maxlength="${esc(meta.max_length)}"`:''} value="${esc(value)}">`}
function readFields(selector){const values={};qa(`${selector} [data-key]`).forEach(el=>{if(typeof el.checkValidity==="function"&&!el.checkValidity())throw Error(`${label(el.dataset.key)}: ${el.validationMessage}`);const type=el.dataset.type;if(type==="number"){if(el.value===""||!Number.isFinite(Number(el.value)))throw Error(`${label(el.dataset.key)} must be a number.`);values[el.dataset.key]=Number(el.value)}else if(type==="bool")values[el.dataset.key]=el.value==="true";else if(type==="string-list")values[el.dataset.key]=[...el.selectedOptions].map(x=>x.value);else values[el.dataset.key]=el.value});qa(`${selector} [data-structured-key]`).forEach(host=>{const node=host.querySelector(':scope > [data-structured-node]');if(node)values[host.dataset.structuredKey]=readStructuredNode(node)});return values}
function moduleField(module,key,value){const complex=value!==null&&typeof value==="object"&&!/_channel_ids$|_role_ids$/.test(key),help=structuredHelp[key]||(complex?'Use the controls below to add, remove and type every value. No code or JSON is required.':'');if(complex)return `<section class="field full structured-field"><span>${esc(label(key))}</span>${structuredEditor(module,key,value)}<small>${esc(help)}</small></section>`;return `<label class="field"><span>${esc(label(key))}</span>${inputFor(key,value)}${help?`<small>${esc(help)}</small>`:''}</label>`}
function groupedModuleFields(module,values){const groups=moduleEditorGroups[module];if(!groups)return Object.entries(values).map(([key,value])=>moduleField(module,key,value)).join("");const seen=new Set;const sections=groups.map(group=>{const fields=group.keys.filter(key=>key in values).map(key=>{seen.add(key);return moduleField(module,key,values[key])}).join("");return fields?`<fieldset class="module-config-group"><legend>${esc(group.title)}</legend>${fields}</fieldset>`:''}).join("");const extra=Object.entries(values).filter(([key])=>!seen.has(key)).map(([key,value])=>moduleField(module,key,value)).join("");return sections+extra}
function openEditor(id){if(!state.guildId||!state.guildReady||state.loading){notice("Choose a connected server and wait for it to finish loading.",true);return}const m=state.catalog.find(x=>x.id===id),c=configFor(id);if(!m)return;state.editing=id;q("#editor-category").textContent=m.category;q("#editor-title").textContent=m.title;q("#editor-description").textContent=m.description;q("#editor-enabled").checked=!!c.enabled;const values={...m.settings,...c.settings};q("#editor-fields").innerHTML=groupedModuleFields(id,values);q("#editor-dialog").showModal()}
function renderLanguage(){q("#guild-language").value=state.serverSettings.language||"";q("#language-catalog").innerHTML=state.languageCatalog.map(item=>`<option value="${esc(item.label)}">${esc(item.code)}</option>`).join("")}
function renderServerSettings(){q("#server-settings").innerHTML=state.serverSchema.filter(field=>field.key!=="language").map(field=>{const value=state.serverSettings[field.key]??field.default;const full=field.kind==="textarea"||field.kind==="channel_ids";return `<label class="field ${full?'full':''}"><span>${esc(field.label||label(field.key))}</span>${inputFor(field.key,value,field)}<small>${esc(field.description||"")}</small></label>`}).join("")}
async function saveLanguage(event){event.preventDefault();const guildId=state.guildId;if(!guildId||!state.guildReady){notice("Choose a connected server and wait for it to finish loading.",true);return}const language=q("#guild-language").value.trim(),button=q("#language-save");button.disabled=true;try{const result=await api(`/guild/${encodeURIComponent(guildId)}/settings`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({settings:{...state.serverSettings,language}})});if(guildId!==state.guildId)return;resetLocalization();state.serverSettings=result.settings;renderLanguage();renderServerSettings();render();notice(language?`Guild language changed to ${language}. Applying it everywhere…`:"Guild language reset to English.");await applyLocalization()}catch(e){notice(e.message,true)}finally{button.disabled=state.loading||!state.guildId||!state.guildReady}}
async function saveEditor(){const guildId=state.guildId,moduleId=state.editing;if(!guildId||!state.guildReady||!moduleId){notice("Choose a connected server and wait for it to finish loading.",true);return}let settings;try{settings=readFields("#editor-fields")}catch(e){notice(e.message||"A structured setting is invalid.",true);return}q("#editor-save").disabled=true;try{const result=await api(`/guild/${encodeURIComponent(guildId)}/module/${encodeURIComponent(moduleId)}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:q("#editor-enabled").checked,settings})});if(guildId!==state.guildId)return;state.configs.set(result.module,result);q("#editor-dialog").close();render();notice(`${state.catalog.find(x=>x.id===moduleId)?.title||"Module"} saved.`)}catch(e){notice(e.message,true)}finally{q("#editor-save").disabled=state.loading||!state.guildId||!state.guildReady}}
async function saveServerSettings(){const guildId=state.guildId;if(!guildId||!state.guildReady){notice("Choose a connected server and wait for it to finish loading.",true);return}let settings;try{settings={...readFields("#server-settings"),language:state.serverSettings.language||""}}catch(e){notice(e.message,true);return}const button=q("#settings-save");button.disabled=true;try{const result=await api(`/guild/${encodeURIComponent(guildId)}/settings`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({settings})});if(guildId!==state.guildId)return;state.serverSettings=result.settings;renderServerSettings();notice("Server settings saved.")}catch(e){notice(e.message,true)}finally{button.disabled=state.loading||!state.guildId||!state.guildReady}}
function operationTime(value){return value?new Date(Number(value)*1000).toLocaleString():"—"}
function renderOperations(){
  const data=state.operations||{cases:[],incidents:[],health:{score:0,recommendations:[],counts:{}},retention:{modules:[]}};
  const health=data.health||{},counts=health.counts||{};
  q("#health-stats").innerHTML=[["Health score",health.score??0],["Open tickets",counts.open_tickets||0],["Recommendations",(health.recommendations||[]).length]].map(([a,b])=>`<div class="stat"><strong>${esc(b)}</strong><small>${esc(a)}</small></div>`).join("");
  q("#health-recommendations").innerHTML=(health.recommendations||[]).map(item=>`<div class="ops-item"><header><strong>${esc(item.title)}</strong><small>${esc(item.severity)}</small></header><p>${esc(item.explanation)}</p><small>Advisory only · ${esc(item.module||"server")}</small></div>`).join("")||'<div class="empty">No health recommendations right now.</div>';
  q("#case-count").textContent=`${(data.cases||[]).length} shown`;
  q("#case-list").innerHTML=(data.cases||[]).map(item=>`<article class="ops-item"><header><strong>${esc(item.case_number||`CASE-${item.id}`)} · ${esc(item.category)}</strong><small>${esc(item.status)} / ${esc(item.severity)}</small></header><p>${esc(item.reason)}</p><small>Member ${esc(item.subject_id)} · assigned ${esc(item.assigned_to||"unassigned")} · ${esc(operationTime(item.updated||item.created))}</small><div class="ops-actions"><button data-case-status="resolved" data-case-id="${esc(item.id)}">Resolve</button><button data-case-status="monitoring" data-case-id="${esc(item.id)}">Monitor</button><button data-case-assign data-case-id="${esc(item.id)}">Assign</button><button data-case-note data-case-id="${esc(item.id)}">Private note</button></div></article>`).join("")||'<div class="empty">No matching moderation cases.</div>';
  q("#incident-count").textContent=`${(data.incidents||[]).length} shown`;
  q("#incident-list").innerHTML=(data.incidents||[]).map(item=>{const mutable=Number.isInteger(item.id);return `<article class="ops-item"><header><strong>${esc(item.source||item.kind)} · ${esc(item.summary)}</strong><small>${esc(item.status)} / ${esc(item.severity)}</small></header><p>Subject ${esc(item.subject_id||"—")} · assigned ${esc(item.assigned_to||"unassigned")}</p><small>${esc(item.reference||"No external reference")} · ${esc(operationTime(item.updated||item.created))}</small>${mutable?`<div class="ops-actions"><button data-incident-status="acknowledged" data-incident-id="${esc(item.id)}">Acknowledge</button><button data-incident-status="escalated" data-incident-id="${esc(item.id)}">Escalate safely</button><button data-incident-status="resolved" data-incident-id="${esc(item.id)}">Resolve</button><button data-incident-assign data-incident-id="${esc(item.id)}">Assign</button></div>`:""}</article>`}).join("")||'<div class="empty">No matching incidents.</div>';
  const modules=data.retention?.modules||[];
  q("#retention-list").innerHTML=`<table><thead><tr><th>Module</th><th>Stored data</th><th>Retention</th><th>Export / delete</th></tr></thead><tbody>${modules.map(item=>`<tr><td>${esc(item.module)}</td><td>${esc(item.data)}</td><td>${esc(item.retention)}</td><td>${esc(item.export)}<br>${esc(item.delete)}</td></tr>`).join("")}</tbody></table>`;
  const selected=q("#digest-channel").value;q("#digest-channel").innerHTML='<option value="">Choose a private channel</option>'+(activeGuild().channels||[]).filter(item=>String(item.type).includes("text")||String(item.type)==="0").map(item=>`<option value="${esc(item.id)}" ${String(item.id)===selected?'selected':''}>#${esc(item.name||item.id)}</option>`).join("");
}
async function loadOperations(){const guildId=state.guildId;if(!guildId||!state.guildReady)return;const requestId=++state.operationsLoadId;const params=new URLSearchParams();const search=q("#operations-search").value.trim(),source=q("#operations-source").value;if(search)params.set("q",search);if(source)params.set("source",source);const data=await api(`/guild/${encodeURIComponent(guildId)}/operations?${params}`);if(requestId!==state.operationsLoadId||guildId!==state.guildId)return;state.operations=data;q("#analytics-export").href=`/dashboard/api/guild/${encodeURIComponent(guildId)}/analytics.csv`;renderOperations()}
async function createCase(event){event.preventDefault();const guildId=state.guildId;if(!guildId||!state.guildReady)return;const evidence_links=q("#case-evidence").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);const payload={subject_id:q("#case-subject").value.trim(),category:q("#case-category").value.trim(),severity:q("#case-severity").value,assigned_to:q("#case-assignee").value.trim()||null,reason:q("#case-reason").value.trim(),evidence_links};const button=event.submitter;button.disabled=true;try{await api(`/guild/${encodeURIComponent(guildId)}/cases`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});event.currentTarget.reset();notice("Moderation case created with an audit timeline.");await loadOperations()}catch(e){notice(e.message,true)}finally{button.disabled=false}}
async function caseAction(id,payload){await api(`/guild/${encodeURIComponent(state.guildId)}/case/${encodeURIComponent(id)}/action`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});await loadOperations()}
async function incidentAction(id,payload){await api(`/guild/${encodeURIComponent(state.guildId)}/incident/${encodeURIComponent(id)}/action`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});await loadOperations()}
async function saveDigest(event){event.preventDefault();const payload={cadence:q("#digest-cadence").value,channel_id:q("#digest-channel").value,visibility:q("#digest-visibility").value,enabled:q("#digest-enabled").value==="true"};const button=event.submitter;button.disabled=true;try{await api(`/guild/${encodeURIComponent(state.guildId)}/digest`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});notice("Staff digest schedule saved with an explicit private destination.");await loadOperations()}catch(e){notice(e.message,true)}finally{button.disabled=false}}
async function loadGuild(){const guildId=state.guildId,loadId=++state.loadId;resetLocalization();state.guildReady=false;state.boosterLoadId++;state.activityLoadId++;state.operationsLoadId++;state.configs=new Map();state.serverSettings={};state.boosterData=null;state.operations=null;state.aiHealth=null;q("#activity-list").innerHTML='<div class="empty">Choose a server to load activity.</div>';renderOperations();renderAIHealth();if(q("#editor-dialog").open)q("#editor-dialog").close();setLoading(!!guildId,guildId?"Loading selected server…":"No connected server");render();renderLanguage();renderServerSettings();renderBoosterSettings();renderBoosterData();if(!guildId){setLoading(false,"Invite owaua to a server you manage to configure it here.");render();return}try{const guild=encodeURIComponent(guildId),[modules,settings]=await Promise.all([api(`/guild/${guild}/modules`),api(`/guild/${guild}/settings`)]);if(loadId!==state.loadId||guildId!==state.guildId)return;state.configs=new Map(modules.modules.map(x=>[x.module,x]));state.serverSettings=settings.settings;state.guildReady=true;renderLanguage();renderServerSettings();renderBoosterSettings();await loadAIHealth();if(!q("#boosters-view").hidden)await loadBoosters();if(!q("#operations-view").hidden)await loadOperations()}finally{if(loadId===state.loadId&&guildId===state.guildId){setLoading(false,state.guildReady?"Up to date":"Could not load this server");render();await applyLocalization()}}}
async function loadActivity(){const guildId=state.guildId;if(!guildId){q("#activity-list").innerHTML='<div class="empty">Choose a connected server to see activity.</div>';return}const requestId=++state.activityLoadId;q("#activity-list").innerHTML='<div class="empty">Loading activity…</div>';try{const data=await api(`/guild/${encodeURIComponent(guildId)}/activity`);if(requestId!==state.activityLoadId||guildId!==state.guildId)return;q("#activity-list").innerHTML=data.items.length?data.items.map(x=>`<div class="activity-row"><div><strong>${esc(x.action)}</strong><br><small>${esc(x.module||"System")}</small></div><div>${esc(x.actor_id)}</div><div><small>${new Date(x.created*1000).toLocaleString()}</small></div></div>`).join(""):'<div class="empty">No dashboard changes yet.</div>'}catch(e){if(requestId===state.activityLoadId)notice(e.message,true)}}
async function boot(){setLoading(true,"Loading dashboard…");try{const session=await api("/session");state.csrf=session.csrf;const [catalog,guilds]=await Promise.all([api("/catalog"),api("/guilds")]);state.catalog=catalog.modules;state.serverSchema=catalog.server_settings||[];state.languageCatalog=catalog.languages||[];state.guilds=guilds.guilds;const select=q("#guild-select");select.innerHTML=state.guilds.length?state.guilds.map(g=>`<option value="${esc(g.id)}">${esc(g.name)}</option>`).join(""):'<option value="">No connected servers</option>';state.guildId=select.value;select.onchange=()=>{state.guildId=select.value;loadGuild().catch(e=>notice(e.message,true))};filters();renderLanguage();showView(location.hash.slice(1),{updateHash:false});await loadGuild()}catch(e){setLoading(false,"Dashboard unavailable");notice(e.message,true)}}
function removeEmpty(children){children.querySelector(':scope > .structured-empty')?.remove()}
function handleStructuredClick(event){const remove=event.target.closest('[data-remove-structured]');if(remove){const row=remove.closest('[data-list-item],[data-object-field]'),children=row?.parentElement;row?.remove();if(children&&!children.children.length)children.innerHTML='<div class="structured-empty">No items yet.</div>';return}const addList=event.target.closest('[data-add-list]');if(addList){const node=addList.closest('[data-structured-node]'),children=node.querySelector(':scope > .structured-body > [data-list-items]'),path=node.dataset.path,item=templateFor(path);removeEmpty(children);children.insertAdjacentHTML('beforeend',`<div class="structured-item" data-list-item>${structuredNode(item,`${path}[]`,"item")}<button class="structured-remove" data-remove-structured type="button">Remove item</button></div>`);return}const addField=event.target.closest('[data-add-field]');if(addField){const node=addField.closest('[data-structured-node]'),children=node.querySelector(':scope > .structured-body > [data-object-fields]'),path=node.dataset.path,value=templateFor(`${path}.*`);removeEmpty(children);children.insertAdjacentHTML('beforeend',structuredObjectField('',value,`${path}.*`));children.lastElementChild?.querySelector('[data-object-key]')?.focus()}}
function handleStructuredChange(event){const picker=event.target.closest('[data-change-kind]');if(!picker)return;const node=picker.closest('[data-structured-node]'),path=node.dataset.path,key=node.querySelector(':scope > .structured-head > strong')?.textContent||'value',kind=picker.value;node.outerHTML=structuredNode(defaultForKind(kind),path,key,kind)}
qa(".nav-item").forEach(b=>b.onclick=()=>showView(b.dataset.view));qa("[data-go]").forEach(b=>b.onclick=()=>showView(b.dataset.go));window.addEventListener("hashchange",()=>showView(location.hash.slice(1),{updateHash:false}));q("#module-search").oninput=render;q("#editor-save").onclick=saveEditor;q("#editor-cancel").onclick=()=>q("#editor-dialog").close();q("#settings-save").onclick=saveServerSettings;q("#language-form").onsubmit=saveLanguage;q("#booster-save").onclick=saveBoosters;q("#booster-refresh").onclick=()=>loadBoosters().catch(e=>notice(e.message,true));q("#booster-adjust").onsubmit=adjustBooster;q("#boosters-view").onclick=event=>{const add=event.target.closest("[data-add-row]");if(add){add.insertAdjacentHTML("beforebegin",collectionRow(add.dataset.addRow,{}));return}const remove=event.target.closest(".remove-row");if(remove){remove.closest("[data-collection-row]")?.remove();return}const action=event.target.closest("[data-booster-action]");if(action)boosterAction(action.dataset.boosterAction)};
q("#editor-fields").onclick=handleStructuredClick;q("#editor-fields").onchange=handleStructuredChange;q("#ai-health-refresh").onclick=()=>loadAIHealth().catch(e=>notice(e.message,true));
q("#case-create").onsubmit=createCase;q("#digest-config").onsubmit=saveDigest;q("#operations-refresh").onclick=()=>loadOperations().catch(e=>notice(e.message,true));q("#operations-source").onchange=()=>loadOperations().catch(e=>notice(e.message,true));q("#operations-search").oninput=()=>{clearTimeout(q("#operations-search").timer);q("#operations-search").timer=setTimeout(()=>loadOperations().catch(e=>notice(e.message,true)),250)};q("#operations-view").onclick=async event=>{try{const caseStatus=event.target.closest("[data-case-status]");if(caseStatus){await caseAction(caseStatus.dataset.caseId,{action:"update",status:caseStatus.dataset.caseStatus});notice("Case timeline updated.");return}const caseAssign=event.target.closest("[data-case-assign]");if(caseAssign){const assigned_to=window.prompt("Staff Discord ID (blank to unassign):","");if(assigned_to!==null){await caseAction(caseAssign.dataset.caseId,{action:"update",assigned_to:assigned_to.trim()});notice("Case assignment updated.")}return}const caseNote=event.target.closest("[data-case-note]");if(caseNote){const note=window.prompt("Private staff note:","");if(note?.trim()){await caseAction(caseNote.dataset.caseId,{action:"note",note:note.trim()});notice("Private note added.")}return}const incidentStatus=event.target.closest("[data-incident-status]");if(incidentStatus){await incidentAction(incidentStatus.dataset.incidentId,{status:incidentStatus.dataset.incidentStatus});notice("Incident status updated.");return}const incidentAssign=event.target.closest("[data-incident-assign]");if(incidentAssign){const assigned_to=window.prompt("Staff Discord ID (blank to unassign):","");if(assigned_to!==null){await incidentAction(incidentAssign.dataset.incidentId,{assigned_to:assigned_to.trim()});notice("Incident assignment updated.")}}}catch(e){notice(e.message,true)}};boot();
"""


def _form_limited(key: str) -> bool:
    now = time.monotonic()
    attempts = _form_attempts[key]
    while attempts and now - attempts[0] > 600:
        attempts.popleft()
    if len(attempts) >= 20:
        return True
    attempts.append(now)
    return False


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _signed_payload(secret: bytes, value: dict[typing.Any, typing.Any]) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
    encoded = payload.encode("utf-8").hex()
    return f"{encoded}.{_sign(secret, encoded)}"


def _decode_payload(secret: bytes, raw: str) -> dict[typing.Any, typing.Any] | None:
    try:
        encoded, signature = raw.rsplit(".", 1)
        if not hmac.compare_digest(signature, _sign(secret, encoded)):
            return None
        value = json.loads(bytes.fromhex(encoded).decode("utf-8"))
        if not isinstance(value, dict) or int(typing.cast(typing.Any, value).get("exp", 0)) <= int(
            time.time()
        ):
            return None
        return typing.cast(typing.Any, value)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _new_session(
    secret: bytes,
    *,
    discord_id: str,
    guild_ids: list[str] | None = None,
) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    payload = {
        "actor": ("discord:" + discord_id)[:160],
        "csrf": csrf,
        "exp": int(time.time()) + SESSION_SECONDS,
        "discord_id": discord_id[:24],
        "guild_ids": list(dict.fromkeys(guild_ids or []))[:100],
    }
    return _signed_payload(secret, payload), csrf


def _session(request: web.Request, secret: bytes) -> dict[typing.Any, typing.Any] | None:
    value = _decode_payload(secret, request.cookies.get(SESSION_COOKIE, ""))
    if value is None or not str(value.get("actor", "")) or not str(value.get("csrf", "")):
        return None
    return value


async def _read_provider_json(
    response: object, limit: int = 1_000_000
) -> dict[typing.Any, typing.Any] | list[typing.Any] | None:
    content = getattr(response, "content", None)
    if content is None:
        return None
    body = bytearray()
    while len(body) <= limit:
        chunk = await content.read(min(64 * 1024, limit + 1 - len(body)))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            return None
        body.extend(chunk)
    if len(body) > limit:
        return None
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return typing.cast(typing.Any, value if isinstance(value, (dict, list)) else None)


def _provider_error_code(payload: object) -> str:
    """Return a log-safe provider error identifier without response details."""
    if not isinstance(payload, dict):
        return "unknown"
    value = str(
        typing.cast(typing.Any, payload).get("error")
        or typing.cast(typing.Any, payload).get("code")
        or "unknown"
    )
    return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) else "unknown"


async def _discord_identity(
    auth: DashboardAuthConfig, code: str
) -> tuple[dict[typing.Any, typing.Any], list[dict[typing.Any, typing.Any]]] | None:
    if not auth.ready() or not 10 <= len(code) <= 2048:
        log.warning("Discord OAuth rejected invalid local configuration or code shape")
        return None
    timeout = ClientTimeout(total=12)
    redirect_uri = auth.base_url + DASHBOARD_PREFIX + "/auth/discord/callback"
    stage = "token"
    try:
        async with ClientSession(timeout=timeout) as client:
            async with client.post(
                f"{DISCORD_API}/oauth2/token",
                auth=BasicAuth(auth.discord_client_id, auth.discord_client_secret),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            ) as response:
                token_payload = await _read_provider_json(response)
                if response.status != 200 or not isinstance(token_payload, dict):
                    log.warning(
                        "Discord OAuth %s request failed (status=%s error=%s)",
                        stage,
                        response.status,
                        _provider_error_code(token_payload),
                    )
                    return None
            access_token = str(token_payload.get("access_token") or "")
            if not access_token:
                log.warning("Discord OAuth token response did not include an access token")
                return None
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            }
            stage = "user"
            async with client.get(f"{DISCORD_API}/users/@me", headers=headers) as response:
                user = await _read_provider_json(response)
                if response.status != 200 or not isinstance(user, dict):
                    log.warning(
                        "Discord OAuth %s request failed (status=%s error=%s)",
                        stage,
                        response.status,
                        _provider_error_code(user),
                    )
                    return None
            stage = "guilds"
            async with client.get(
                f"{DISCORD_API}/users/@me/guilds?with_counts=false&limit=200",
                headers=headers,
            ) as response:
                guilds = await _read_provider_json(response, limit=DISCORD_GUILDS_JSON_BYTES)
                if response.status != 200 or not isinstance(guilds, list):
                    log.warning(
                        "Discord OAuth %s request failed (status=%s error=%s)",
                        stage,
                        response.status,
                        _provider_error_code(guilds),
                    )
                    return None
    except Exception as error:  # noqa: BLE001
        log.warning(
            "Discord OAuth %s request raised %s",
            stage,
            type(error).__name__,
        )
        return None
    if not str(user.get("id") or "").isdigit():
        log.warning("Discord OAuth user response did not include a valid user id")
        return None
    return user, [item for item in guilds[:200] if isinstance(item, dict)]


def _dashboard_headers(response: web.StreamResponse, content_type: str = "") -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    if content_type != "text/css":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'"
        )


def attach_dashboard_routes(
    app: web.Application,
    *,
    guild_provider: GuildProvider | None = None,
    auth_config: DashboardAuthConfig | None = None,
) -> None:
    """Attach dashboard UI and JSON API to an aiohttp application."""
    auth = auth_config or DashboardAuthConfig()
    auth_ready = auth.ready()
    secret = hashlib.sha256(("owaua-dashboard:" + auth.session_secret).encode("utf-8")).digest()
    provider: GuildProvider = guild_provider or (lambda: [])

    def authenticated(request: web.Request) -> dict[typing.Any, typing.Any] | None:
        return _session(request, secret) if auth_ready else None

    def require_session(request: web.Request) -> dict[typing.Any, typing.Any]:
        session = authenticated(request)
        if session is None or not session.get("discord_id"):
            raise web.HTTPUnauthorized(text="authentication required")
        return session

    def require_csrf(request: web.Request, session: dict[typing.Any, typing.Any]) -> None:
        supplied = request.headers.get("X-CSRF-Token", "")
        expected = str(session.get("csrf", ""))
        if not expected or not hmac.compare_digest(supplied, expected):
            raise web.HTTPForbidden(text="invalid CSRF token")

    def all_guilds() -> list[dict[typing.Any, typing.Any]]:
        try:
            raw: typing.Any = provider()
        except Exception:
            raw: list[typing.Any] = []
        output: list[typing.Any] = []
        for item in raw[:500] if isinstance(raw, list) else []:
            if (
                not isinstance(item, dict)
                or not str(typing.cast(typing.Any, item).get("id", "")).isdigit()
            ):
                continue
            output.append(
                {
                    "id": str(typing.cast(typing.Any, item["id"])),
                    "name": str(
                        typing.cast(
                            typing.Any, typing.cast(typing.Any, item).get("name") or item["id"]
                        )
                    )[:100],
                    "icon": str(typing.cast(typing.Any, item).get("icon") or "")[:500],
                    "member_count": max(
                        0, int(typing.cast(typing.Any, item).get("member_count") or 0)
                    ),
                    "everyone_permissions": max(
                        0, int(typing.cast(typing.Any, item).get("everyone_permissions") or 0)
                    ),
                    "bot_permissions": max(
                        0, int(typing.cast(typing.Any, item).get("bot_permissions") or 0)
                    ),
                    "members": typing.cast(typing.Any, item).get("members")
                    if isinstance(typing.cast(typing.Any, item).get("members"), list)
                    else [],
                    "manager_ids": (
                        [
                            str(value)
                            for value in typing.cast(typing.Any, item).get("manager_ids", [])[
                                :10_000
                            ]
                            if str(value).isdigit()
                        ]
                        if isinstance(typing.cast(typing.Any, item).get("manager_ids"), list)
                        else None
                    ),
                    "channels": typing.cast(typing.Any, item).get("channels")
                    if isinstance(typing.cast(typing.Any, item).get("channels"), list)
                    else [],
                    "roles": typing.cast(typing.Any, item).get("roles")
                    if isinstance(typing.cast(typing.Any, item).get("roles"), list)
                    else [],
                }
            )
        return output

    def guilds(session: dict[typing.Any, typing.Any]) -> list[dict[typing.Any, typing.Any]]:
        available = all_guilds()
        allowed = {str(value) for value in session.get("guild_ids", []) if str(value).isdigit()}
        return [item for item in available if item["id"] in allowed]

    def public_guild(guild: dict[typing.Any, typing.Any]) -> dict[typing.Any, typing.Any]:
        """Return only configuration-picker metadata, never the member roster."""
        return {key: value for key, value in guild.items() if key not in {"members", "manager_ids"}}

    def require_guild(
        session: dict[typing.Any, typing.Any], guild_id: str
    ) -> dict[typing.Any, typing.Any]:
        match = next((item for item in guilds(session) if item["id"] == str(guild_id)), None)
        if match is None:
            raise web.HTTPNotFound(text="server is not available to this account")
        manager_ids = match.get("manager_ids")
        if manager_ids is not None and str(session.get("discord_id")) not in manager_ids:
            raise web.HTTPForbidden(text="Manage Server permission is no longer available")
        return match

    def require_connected_guild(guild_id: str) -> dict[typing.Any, typing.Any]:
        match = next((item for item in all_guilds() if item["id"] == str(guild_id)), None)
        if match is None:
            raise web.HTTPNotFound(text="server is not connected")
        return match

    def set_session_cookie(response: web.StreamResponse, request: web.Request, value: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            value,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=True,
            samesite="Lax",
            path=DASHBOARD_PREFIX,
        )

    async def index(request: web.Request) -> web.Response:
        session = authenticated(request)
        if session and session.get("discord_id"):
            response = web.Response(
                text=_page("Dashboard", _APP_HTML, script="app.js"),
                content_type="text/html",
            )
        else:
            if not auth_ready:
                status = '<p class="auth-message error">Discord sign-in is not configured.</p>'
            elif request.query.get("auth") == "discord_failed":
                status = (
                    '<p class="auth-message error">Discord could not complete sign-in. '
                    "Please try again.</p>"
                )
            elif request.query.get("auth") == "discord_cancelled":
                status = '<p class="auth-message">Discord sign-in was cancelled.</p>'
            else:
                status = ""
            response = web.Response(
                text=_page("Sign in", _LOGIN_HTML.replace("<!-- auth-status -->", status)),
                content_type="text/html",
                status=200 if auth_ready else 503,
            )
        _dashboard_headers(response)
        return response

    async def logout(request: web.Request) -> web.StreamResponse:
        response = web.HTTPSeeOther(location=DASHBOARD_PREFIX)
        response.del_cookie(SESSION_COOKIE, path=DASHBOARD_PREFIX)
        raise response

    async def css(_request: web.Request) -> web.Response:
        response = web.Response(
            text=_MONO_CSS,
            content_type="text/css",
        )
        _dashboard_headers(response, "text/css")
        return response

    async def js(_request: web.Request) -> web.Response:
        response = web.Response(text=_JS, content_type="application/javascript")
        _dashboard_headers(response)
        return response

    async def discord_start(request: web.Request) -> web.StreamResponse:
        session = authenticated(request)
        if not auth_ready:
            raise web.HTTPServiceUnavailable(text="Discord sign-in is not configured")
        if session and session.get("discord_id"):
            raise web.HTTPSeeOther(location=DASHBOARD_PREFIX)
        nonce = secrets.token_urlsafe(32)
        state = _signed_payload(
            secret,
            {
                "nonce": nonce,
                "exp": int(time.time()) + AUTH_NONCE_SECONDS,
            },
        )
        redirect_uri = auth.base_url + DASHBOARD_PREFIX + "/auth/discord/callback"
        query = urlencode(
            {
                "client_id": auth.discord_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "identify guilds",
                "state": state,
                "prompt": "consent",
            }
        )
        response = web.HTTPSeeOther(location=f"https://discord.com/oauth2/authorize?{query}")
        response.set_cookie(
            AUTH_NONCE_COOKIE,
            nonce,
            max_age=AUTH_NONCE_SECONDS,
            httponly=True,
            secure=True,
            samesite="Lax",
            path=DASHBOARD_PREFIX,
        )
        raise response

    async def discord_callback(request: web.Request) -> web.StreamResponse:
        if not auth_ready:
            raise web.HTTPServiceUnavailable(text="Discord sign-in is not configured")
        if request.query.get("error"):
            response = web.HTTPSeeOther(location=DASHBOARD_PREFIX + "?auth=discord_cancelled")
            response.del_cookie(AUTH_NONCE_COOKIE, path=DASHBOARD_PREFIX)
            raise response
        state = _decode_payload(secret, str(request.query.get("state") or ""))
        expected_nonce = request.cookies.get(AUTH_NONCE_COOKIE, "")
        if (
            state is None
            or not expected_nonce
            or not hmac.compare_digest(str(state.get("nonce") or ""), expected_nonce)
        ):
            raise web.HTTPForbidden(text="invalid Discord OAuth state")
        identity = await _discord_identity(auth, str(request.query.get("code") or ""))
        if identity is None:
            response = web.HTTPSeeOther(location=DASHBOARD_PREFIX + "?auth=discord_failed")
            response.del_cookie(AUTH_NONCE_COOKIE, path=DASHBOARD_PREFIX)
            raise response
        user, discord_guilds = identity
        connected = {item["id"] for item in all_guilds()}
        manageable: list[typing.Any] = []
        for item in discord_guilds:
            guild_id = str(item.get("id") or "")
            try:
                permissions = int(str(item.get("permissions") or "0"))
            except ValueError:
                permissions = 0
            if guild_id in connected and (
                item.get("owner") is True or permissions & MANAGE_GUILD_PERMISSIONS
            ):
                if guild_id not in manageable:
                    manageable.append(guild_id)
        value, _csrf = _new_session(
            secret,
            discord_id=str(user["id"]),
            guild_ids=manageable,
        )
        response = web.HTTPSeeOther(location=DASHBOARD_PREFIX)
        set_session_cookie(response, request, value)
        response.del_cookie(AUTH_NONCE_COOKIE, path=DASHBOARD_PREFIX)
        raise response

    async def session_api(request: web.Request) -> web.Response:
        session = require_session(request)
        return web.json_response({"actor": session["actor"], "csrf": session["csrf"]})

    async def catalog_api(request: web.Request) -> web.Response:
        require_session(request)
        server_settings = public_server_settings()
        model_values = [
            config.DEFAULT_MODEL,
            config.MODEL_BIG,
            *(model_id for model_id, _label in config.GROQ_CHAT_MODELS),
        ]
        models = [{"value": "", "label": "Host default"}]
        for model_id in dict.fromkeys(model_values):
            models.append({"value": model_id, "label": config.model_display(model_id)})
        for field in server_settings:
            if field["key"] == "model":
                field["choices"] = models
        return web.json_response(
            {
                "modules": public_catalog(),
                "server_settings": server_settings,
                "languages": [
                    {"code": language.code, "label": language.label}
                    for language in multilingual.LANGUAGES
                ],
                "free": True,
            }
        )

    async def guilds_api(request: web.Request) -> web.Response:
        session = require_session(request)
        return web.json_response({"guilds": [public_guild(item) for item in guilds(session)]})

    async def modules_api(request: web.Request) -> web.Response:
        session = require_session(request)
        guild = require_guild(session, request.match_info["guild_id"])
        return web.json_response(
            {
                "guild": public_guild(guild),
                "modules": db.module_configs(guild["id"]),
            }
        )

    async def settings_api(request: web.Request) -> web.Response:
        session = require_session(request)
        guild = require_guild(session, request.match_info["guild_id"])
        return web.json_response(
            {
                "guild": public_guild(guild),
                "settings": db.guild_settings(guild["id"]),
            }
        )

    async def ai_health_api(request: web.Request) -> web.Response:
        session = require_session(request)
        guild = require_guild(session, request.match_info["guild_id"])
        return web.json_response(ai_control.diagnostics(Scope.guild(guild["id"]).key))

    async def update_settings_api(request: web.Request) -> web.Response:
        session = require_session(request)
        require_csrf(request, session)
        guild = require_guild(session, request.match_info["guild_id"])
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(
            typing.cast(typing.Any, payload).get("settings"), dict
        ):
            raise web.HTTPBadRequest(text="settings must be an object")
        requested_language = str(
            typing.cast(typing.Any, payload["settings"]).get("language") or ""
        ).strip()
        if requested_language and multilingual.coerce(requested_language) is None:
            raise web.HTTPBadRequest(
                text="language must be a real name or locale code (up to six words)"
            )
        allowed_models = {
            "",
            config.DEFAULT_MODEL,
            config.MODEL_BIG,
            *(model_id for model_id, _label in config.GROQ_CHAT_MODELS),
        }
        requested_model: typing.Any = typing.cast(typing.Any, payload["settings"]).get("model", "")
        if requested_model not in allowed_models:
            raise web.HTTPBadRequest(text="model is not available")
        try:
            result = db.dashboard_guild_settings_set(
                guild["id"],
                typing.cast(typing.Any, payload["settings"]),
                actor_id=str(session["actor"]),
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        if result.get("voice_transcription_enabled") is False:
            try:
                from owaua import voice

                voice.stop_guild_stt(int(guild["id"]))
            except (ImportError, TypeError, ValueError):
                log.exception("could not stop voice transcription for guild %s", guild["id"])
        db.cleanup_guild_content(guild["id"], result["retention_days"])
        return web.json_response({"settings": result})

    async def localization_api(request: web.Request) -> web.Response:
        session = require_session(request)
        require_csrf(request, session)
        guild = require_guild(session, request.match_info["guild_id"])
        language = multilingual.guild_language(guild["id"])
        if multilingual.is_english(language):
            return web.json_response({"language": "en", "translations": {}})
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="invalid JSON") from None
        texts: typing.Any = typing.cast(
            typing.Any,
            typing.cast(typing.Any, payload).get("texts") if isinstance(payload, dict) else None,
        )
        if (
            not isinstance(texts, list)
            or len(typing.cast(typing.Any, texts)) > 96
            or any(
                not isinstance(item, str) or len(item) > 2000
                for item in typing.cast(typing.Iterable[typing.Any], texts)
            )
            or sum(
                len(item)
                for item in typing.cast(typing.Iterable[typing.Any], texts)
                if isinstance(item, str)
            )
            > 30_000
        ):
            raise web.HTTPBadRequest(text="texts must be a bounded string list")
        unique: typing.Any = typing.cast(
            typing.Any,
            list(
                dict.fromkeys(
                    item.strip()
                    for item in typing.cast(typing.Iterable[typing.Any], texts)
                    if item.strip()
                )
            ),
        )
        translated = await multilingual.translate_many(
            unique,
            typing.cast(typing.Any, language).label,
            scope_id=Scope.guild(guild["id"]).key,
            user_id=str(session["discord_id"]),
        )
        return web.json_response(
            {
                "language": typing.cast(typing.Any, language).label,
                "translations": dict(zip(unique, translated)),
            }
        )

    async def update_module_api(request: web.Request) -> web.Response:
        session = require_session(request)
        require_csrf(request, session)
        guild = require_guild(session, request.match_info["guild_id"])
        module = request.match_info["module"].lower()
        if module not in MODULES:
            raise web.HTTPNotFound(text="unknown module")
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(
            typing.cast(typing.Any, payload).get("enabled"), bool
        ):
            raise web.HTTPBadRequest(text="enabled must be a boolean")
        try:
            result = db.module_config_set(
                guild["id"],
                module,
                enabled=typing.cast(typing.Any, payload["enabled"]),
                settings=typing.cast(typing.Any, payload).get("settings")
                if isinstance(typing.cast(typing.Any, payload).get("settings"), dict)
                else {},
                actor_id=str(session["actor"]),
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        return web.json_response(result)

    async def activity_api(request: web.Request) -> web.Response:
        session = require_session(request)
        guild = require_guild(session, request.match_info["guild_id"])
        return web.json_response({"items": db.dashboard_audit_list(guild["id"], 200)})

    async def operations_api(request: web.Request) -> web.Response:
        session = require_session(request)
        guild = require_guild(session, request.match_info["guild_id"])
        query = str(request.query.get("q") or "")[:200]
        case_status = str(request.query.get("case_status") or "").lower() or None
        incident_status = str(request.query.get("incident_status") or "").lower() or None
        source = str(request.query.get("source") or "").lower() or None
        assigned_to = str(request.query.get("assigned_to") or "") or None
        try:
            cases = staffops.search_cases(
                guild["id"],
                query=query,
                status=case_status,
                assigned_to=assigned_to,
                limit=250,
            )
            incidents = staffops.incident_center(
                guild["id"],
                query=query,
                status=incident_status,
                source=source,
                assigned_to=assigned_to,
                limit=250,
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        return web.json_response(
            {
                "cases": cases,
                "incidents": incidents,
                "health": staffops.server_health(guild["id"], guild),
                "digest": staffops.digest_preview(guild["id"]),
                "retention": staffops.retention_inventory(guild["id"]),
            }
        )

    async def create_case_api(request: web.Request) -> web.Response:
        session = require_session(request)
        require_csrf(request, session)
        guild = require_guild(session, request.match_info["guild_id"])
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="invalid JSON") from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="case must be an object")
        member_ids = {
            str(typing.cast(typing.Any, item).get("id"))
            for item in guild.get("members", [])
            if isinstance(item, dict)
        }
        if (
            typing.cast(typing.Any, payload).get("assigned_to")
            and str(typing.cast(typing.Any, payload).get("assigned_to")) not in member_ids
        ):
            raise web.HTTPBadRequest(text="assignee must be a current server member")
        try:
            item = staffops.create_case(
                guild["id"],
                actor_id=str(session["actor"]),
                subject_id=typing.cast(typing.Any, payload).get("subject_id"),
                category=typing.cast(typing.Any, payload).get("category"),
                reason=typing.cast(typing.Any, payload).get("reason"),
                severity=typing.cast(typing.Any, payload).get("severity") or "medium",
                evidence_links=typing.cast(typing.Any, payload).get("evidence_links"),
                expires_at=typing.cast(typing.Any, payload).get("expires_at"),
                assigned_to=typing.cast(typing.Any, payload).get("assigned_to"),
                source="dashboard",
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        db.dashboard_audit_record(
            guild["id"],
            actor_id=str(session["actor"]),
            action="case.created",
            module="moderation",
            detail={
                "case_id": item.get("id"),
                "subject_id": str(typing.cast(typing.Any, payload).get("subject_id") or ""),
            },
        )
        return web.json_response({"case": item}, status=201)

    async def case_action_api(request: web.Request) -> web.Response:
        session = require_session(request)
        require_csrf(request, session)
        guild = require_guild(session, request.match_info["guild_id"])
        try:
            case_id = int(request.match_info["case_id"])
            payload = await request.json()
        except (json.JSONDecodeError, TypeError, ValueError):
            raise web.HTTPBadRequest(text="invalid case action") from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="action must be an object")
        action = str(typing.cast(typing.Any, payload).get("action") or "").lower()
        member_ids = {
            str(typing.cast(typing.Any, item).get("id"))
            for item in guild.get("members", [])
            if isinstance(item, dict)
        }
        if (
            typing.cast(typing.Any, payload).get("assigned_to")
            and str(typing.cast(typing.Any, payload).get("assigned_to")) not in member_ids
        ):
            raise web.HTTPBadRequest(text="assignee must be a current server member")
        try:
            if action == "note":
                case = staffops.get_case(guild["id"], case_id, include_timeline=False)
                if case is None:
                    raise ValueError("case not found")
                staffops.add_member_note(
                    guild["id"],
                    actor_id=str(session["actor"]),
                    subject_id=str(case["subject_id"]),
                    note=typing.cast(typing.Any, payload).get("note"),
                    case_id=case_id,
                )
                item = staffops.get_case(guild["id"], case_id)
            elif action == "update":
                item = staffops.update_case(
                    guild["id"],
                    case_id,
                    actor_id=str(session["actor"]),
                    status=typing.cast(typing.Any, payload).get("status"),
                    assigned_to=typing.cast(typing.Any, payload).get("assigned_to"),
                    appeal_status=typing.cast(typing.Any, payload).get("appeal_status"),
                    expires_at=typing.cast(typing.Any, payload).get("expires_at")
                    if "expires_at" in payload
                    else None,
                )
            else:
                raise ValueError("unknown case action")
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        db.dashboard_audit_record(
            guild["id"],
            actor_id=str(session["actor"]),
            action=f"case.{action}",
            module="moderation",
            detail={"case_id": case_id},
        )
        return web.json_response({"case": item})

    async def incident_action_api(request: web.Request) -> web.Response:
        session = require_session(request)
        require_csrf(request, session)
        guild = require_guild(session, request.match_info["guild_id"])
        try:
            incident_id = int(request.match_info["incident_id"])
            payload = await request.json()
        except (json.JSONDecodeError, TypeError, ValueError):
            raise web.HTTPBadRequest(text="invalid incident action") from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="action must be an object")
        member_ids = {
            str(typing.cast(typing.Any, item).get("id"))
            for item in guild.get("members", [])
            if isinstance(item, dict)
        }
        if (
            typing.cast(typing.Any, payload).get("assigned_to")
            and str(typing.cast(typing.Any, payload).get("assigned_to")) not in member_ids
        ):
            raise web.HTTPBadRequest(text="assignee must be a current server member")
        try:
            item = staffops.update_incident(
                guild["id"],
                incident_id,
                actor_id=str(session["actor"]),
                status=typing.cast(typing.Any, payload).get("status"),
                assigned_to=typing.cast(typing.Any, payload).get("assigned_to"),
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        db.dashboard_audit_record(
            guild["id"],
            actor_id=str(session["actor"]),
            action="incident.updated",
            module="incident_center",
            detail={"incident_id": incident_id, "status": item.get("status")},
        )
        return web.json_response({"incident": item})

    async def digest_api(request: web.Request) -> web.Response:
        session = require_session(request)
        require_csrf(request, session)
        guild = require_guild(session, request.match_info["guild_id"])
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(
            typing.cast(typing.Any, payload).get("enabled"), bool
        ):
            raise web.HTTPBadRequest(text="digest configuration is invalid")
        channel_ids = {
            str(typing.cast(typing.Any, item).get("id"))
            for item in guild.get("channels", [])
            if isinstance(item, dict)
        }
        if str(typing.cast(typing.Any, payload).get("channel_id") or "") not in channel_ids:
            raise web.HTTPBadRequest(text="delivery channel must belong to this server")
        try:
            item = staffops.configure_digest(
                guild["id"],
                actor_id=str(session["actor"]),
                cadence=typing.cast(typing.Any, payload).get("cadence"),
                channel_id=typing.cast(typing.Any, payload).get("channel_id"),
                visibility=typing.cast(typing.Any, payload).get("visibility"),
                enabled=typing.cast(typing.Any, payload["enabled"]),
                sections=typing.cast(typing.Any, payload).get("sections")
                if isinstance(typing.cast(typing.Any, payload).get("sections"), list)
                else None,
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        return web.json_response({"digest": item})

    async def analytics_csv_api(request: web.Request) -> web.Response:
        session = require_session(request)
        guild = require_guild(session, request.match_info["guild_id"])
        response = web.Response(
            text=staffops.analytics_csv(guild["id"]),
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="owaua-{guild["id"]}-aggregate.csv"'
            },
        )
        _dashboard_headers(response)
        return response

    async def boosters_api(request: web.Request) -> web.Response:
        session = require_session(request)
        guild = require_guild(session, request.match_info["guild_id"])
        members = [
            {
                "id": str(typing.cast(typing.Any, item).get("id")),
                "name": str(
                    typing.cast(typing.Any, item).get("name")
                    or typing.cast(typing.Any, item).get("id")
                )[:100],
                "boosting": bool(typing.cast(typing.Any, item).get("boosting")),
            }
            for item in guild.get("members", [])[:10_000]
            if isinstance(item, dict) and str(typing.cast(typing.Any, item).get("id", "")).isdigit()
        ]
        names = {item["id"]: item["name"] for item in members}
        records = db.booster_members(guild["id"], limit=10_000)
        for record in records:
            record["name"] = names.get(str(record["user_id"]), "")
        return web.json_response(
            {
                "stats": db.booster_stats(guild["id"]),
                "records": records,
                "members": members,
            }
        )

    async def booster_action_api(request: web.Request) -> web.Response:
        session = require_session(request)
        require_csrf(request, session)
        guild = require_guild(session, request.match_info["guild_id"])
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="invalid JSON") from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="action must be an object")
        action = str(typing.cast(typing.Any, payload).get("action") or "").strip().lower()
        if action not in {"adjust", "sync", "test"}:
            raise web.HTTPBadRequest(text="unknown booster action")
        target_id = str(typing.cast(typing.Any, payload).get("target_id") or "").strip()
        member_ids = {
            str(typing.cast(typing.Any, item).get("id"))
            for item in guild.get("members", [])
            if isinstance(item, dict) and str(typing.cast(typing.Any, item).get("id", "")).isdigit()
        }
        if action in {"adjust", "test"} and (
            not target_id.isdigit() or target_id not in member_ids
        ):
            raise web.HTTPBadRequest(text="choose a current server member")
        actor = str(session["actor"])
        if action == "adjust":
            delta: typing.Any = typing.cast(typing.Any, payload).get("delta")
            if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
                raise web.HTTPBadRequest(text="delta must be a non-zero whole number")
            if not -10_000 <= delta <= 10_000:
                raise web.HTTPBadRequest(text="delta is out of range")
            record = db.booster_adjust(guild["id"], target_id, delta)
            db.community_record_create(
                "booster_dashboard_action",
                f"guild:{guild['id']}",
                {"action": "reconcile", "target_id": target_id, "actor_id": actor},
                user_id=target_id,
                due=db.now(),
            )
            db.dashboard_audit_record(
                guild["id"],
                actor_id=actor,
                action="booster.adjusted",
                module="boosters",
                detail={"target_id": target_id, "delta": delta},
            )
            return web.json_response({"message": "Booster count corrected.", "record": record})
        db.community_record_create(
            "booster_dashboard_action",
            f"guild:{guild['id']}",
            {"action": action, "target_id": target_id, "actor_id": actor},
            user_id=target_id or None,
            due=db.now(),
        )
        db.dashboard_audit_record(
            guild["id"],
            actor_id=actor,
            action=f"booster.{action}.queued",
            module="boosters",
            detail={"target_id": target_id},
        )
        return web.json_response(
            {
                "message": "Booster synchronization queued."
                if action == "sync"
                else "Test greeting queued."
            },
            status=202,
        )

    def configured_form(
        guild_id: str, slug: str
    ) -> tuple[dict[typing.Any, typing.Any], dict[typing.Any, typing.Any]] | None:
        config = db.module_config(guild_id, "forms")
        if not config["enabled"]:
            return None
        for item in config["settings"].get("forms", [])[:100]:
            if (
                isinstance(item, dict)
                and typing.cast(typing.Any, item).get("enabled", True) is not False
                and str(typing.cast(typing.Any, item).get("slug") or "").lower() == slug.lower()
            ):
                return config, typing.cast(dict[typing.Any, typing.Any], item)
        return None

    def form_access(
        guild_id: str, form: dict[typing.Any, typing.Any], token_value: str
    ) -> dict[typing.Any, typing.Any] | None:
        if not form.get("members_only"):
            return None
        if not token_value:
            raise web.HTTPForbidden(text="Open this form from its Discord command.")
        records = db.community_records("form_access", f"guild:{guild_id}", limit=5000)
        access = next(
            (
                item
                for item in records
                if item.get("record_key") == token_value
                and item["data"].get("form_slug") == form.get("slug")
                and float(item.get("due") or 0) > time.time()
            ),
            None,
        )
        if access is None:
            raise web.HTTPForbidden(text="This form link is invalid or expired.")
        return access

    def form_document(guild_id: str, form: dict[typing.Any, typing.Any], token_value: str) -> str:
        fields: list[typing.Any] = []
        for index, question in enumerate(form.get("questions", [])[:50]):
            if not isinstance(question, dict):
                continue
            qid = re.sub(
                r"[^a-zA-Z0-9_-]", "", str(typing.cast(typing.Any, question).get("id") or index)
            )[:40]
            label = html.escape(
                str(typing.cast(typing.Any, question).get("label") or f"Question {index + 1}")[:300]
            )
            required = " required" if typing.cast(typing.Any, question).get("required") else ""
            kind = str(typing.cast(typing.Any, question).get("type") or "short")
            name = f"q_{qid}"
            if kind == "paragraph":
                control = f'<textarea name="{name}" maxlength="4000"{required}></textarea>'
            elif kind in {"multiple_choice", "checkbox"}:
                input_type = "radio" if kind == "multiple_choice" else "checkbox"
                options = "".join(
                    f'<label class="option"><input type="{input_type}" name="{name}" value="{html.escape(str(option)[:300], quote=True)}"{required}> {html.escape(str(option)[:300])}</label>'
                    for option in typing.cast(typing.Any, question).get("options", [])[:30]
                )
                control = f'<div class="option-list">{options}</div>'
            else:
                control = f'<input name="{name}" maxlength="1000"{required}>'
            fields.append(f'<label class="field full"><strong>{label}</strong>{control}</label>')
        safe_title = html.escape(str(form.get("title") or "Form")[:200])
        description = html.escape(str(form.get("description") or "")[:2000])
        return _page(
            safe_title,
            f'''<main class="login-shell"><section class="login-card form-card"><div class="brand-mark">S</div><p class="eyebrow">OWAUA FORM</p><h1>{safe_title}</h1><p class="muted">{description}</p><form method="post" action="/forms/{html.escape(guild_id, quote=True)}/{html.escape(str(form.get("slug")), quote=True)}" class="editor-fields public-form"><input type="hidden" name="access_token" value="{html.escape(token_value, quote=True)}">{"".join(fields)}<button type="submit">Submit response</button></form></section></main>''',
        )

    async def form_get(request: web.Request) -> web.Response:
        guild = require_connected_guild(request.match_info["guild_id"])
        configured = configured_form(guild["id"], request.match_info["slug"])
        if configured is None:
            raise web.HTTPNotFound(text="form not found")
        _config, form = configured
        token_value = str(request.query.get("token") or "")[:200]
        form_access(guild["id"], form, token_value)
        response = web.Response(
            text=form_document(guild["id"], form, token_value), content_type="text/html"
        )
        _dashboard_headers(response)
        return response

    async def form_post(request: web.Request) -> web.Response:
        if _form_limited("form:" + (request.remote or "unknown")):
            raise web.HTTPTooManyRequests(text="too many form submissions")
        guild = require_connected_guild(request.match_info["guild_id"])
        configured = configured_form(guild["id"], request.match_info["slug"])
        if configured is None:
            raise web.HTTPNotFound(text="form not found")
        _config, form = configured
        body = await request.post()
        token_value = str(body.get("access_token") or "")[:200]
        access = form_access(guild["id"], form, token_value)
        user_id = str(access.get("user_id")) if access and access.get("user_id") else None
        existing = (
            db.community_records(
                "form_submission", f"guild:{guild['id']}", user_id=user_id, status=None, limit=5000
            )
            if user_id
            else []
        )
        same_form = [item for item in existing if item["data"].get("form_slug") == form.get("slug")]
        if form.get("one_submission") and same_form:
            raise web.HTTPConflict(text="You already submitted this form.")
        cooldown = max(0, min(31_536_000, int(form.get("cooldown_seconds") or 0)))
        if same_form and cooldown and time.time() - float(same_form[-1]["created"]) < cooldown:
            raise web.HTTPTooManyRequests(text="This form is still on cooldown.")
        answers: list[typing.Any] = []
        for index, question in enumerate(form.get("questions", [])[:50]):
            if not isinstance(question, dict):
                continue
            qid = re.sub(
                r"[^a-zA-Z0-9_-]", "", str(typing.cast(typing.Any, question).get("id") or index)
            )[:40]
            name = f"q_{qid}"
            values = [str(value)[:4000] for value in body.getall(name, [])]
            if typing.cast(typing.Any, question).get("required") and not any(
                value.strip() for value in values
            ):
                raise web.HTTPBadRequest(
                    text=f"Missing required answer: {typing.cast(typing.Any, question).get('label')}"
                )
            answers.append(
                {
                    "id": qid,
                    "label": str(typing.cast(typing.Any, question).get("label") or qid)[:300],
                    "values": values,
                }
            )
        db.community_record_create(
            "form_submission",
            f"guild:{guild['id']}",
            {
                "form_slug": str(form.get("slug")),
                "form_title": str(form.get("title") or "Form"),
                "answers": answers,
                "channel_id": str(form.get("channel_id") or ""),
                "create_thread": bool(form.get("create_thread")),
                "ping_role_ids": form.get("ping_role_ids", [])[:20],
                "add_role_ids": form.get("add_role_ids", [])[:20],
                "remove_role_ids": form.get("remove_role_ids", [])[:20],
                "reactions": form.get("reactions", [])[:10],
                "anonymous": bool(form.get("anonymous")),
            },
            user_id=user_id,
        )
        response = web.Response(
            text=_page(
                "Submitted",
                '<main class="login-shell"><section class="login-card"><div class="brand-mark">S</div><p class="eyebrow">RESPONSE RECEIVED</p><h1>Thank you.</h1><p class="muted">Your submission was saved and queued for the server team.</p></section></main>',
            ),
            content_type="text/html",
        )
        _dashboard_headers(response)
        return response

    app.router.add_get(DASHBOARD_PREFIX, index)
    app.router.add_get(DASHBOARD_PREFIX + "/", index)
    app.router.add_post(DASHBOARD_PREFIX + "/logout", logout)
    app.router.add_get(DASHBOARD_PREFIX + "/assets/app.css", css)
    app.router.add_get(DASHBOARD_PREFIX + "/assets/app.js", js)
    app.router.add_get(DASHBOARD_PREFIX + "/auth/discord", discord_start)
    app.router.add_get(DASHBOARD_PREFIX + "/auth/discord/callback", discord_callback)
    app.router.add_get(DASHBOARD_PREFIX + "/api/session", session_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/catalog", catalog_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/guilds", guilds_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/guild/{guild_id}/modules", modules_api)
    app.router.add_put(
        DASHBOARD_PREFIX + "/api/guild/{guild_id}/module/{module}", update_module_api
    )
    app.router.add_get(DASHBOARD_PREFIX + "/api/guild/{guild_id}/settings", settings_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/guild/{guild_id}/ai-health", ai_health_api)
    app.router.add_put(DASHBOARD_PREFIX + "/api/guild/{guild_id}/settings", update_settings_api)
    app.router.add_post(DASHBOARD_PREFIX + "/api/guild/{guild_id}/localization", localization_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/guild/{guild_id}/activity", activity_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/guild/{guild_id}/operations", operations_api)
    app.router.add_post(DASHBOARD_PREFIX + "/api/guild/{guild_id}/cases", create_case_api)
    app.router.add_post(
        DASHBOARD_PREFIX + "/api/guild/{guild_id}/case/{case_id}/action", case_action_api
    )
    app.router.add_post(
        DASHBOARD_PREFIX + "/api/guild/{guild_id}/incident/{incident_id}/action",
        incident_action_api,
    )
    app.router.add_post(DASHBOARD_PREFIX + "/api/guild/{guild_id}/digest", digest_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/guild/{guild_id}/analytics.csv", analytics_csv_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/guild/{guild_id}/boosters", boosters_api)
    app.router.add_post(
        DASHBOARD_PREFIX + "/api/guild/{guild_id}/boosters/action", booster_action_api
    )
    app.router.add_get("/forms/{guild_id}/{slug}", form_get)
    app.router.add_post("/forms/{guild_id}/{slug}", form_post)
