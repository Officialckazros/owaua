"""Authenticated, zero-license-cost web dashboard for SefBot modules."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from sefbot import db
from sefbot.module_catalog import MODULES, public_catalog

DASHBOARD_PREFIX: Final = "/dashboard"
SESSION_COOKIE: Final = "sefbot_dashboard_session"
SESSION_SECONDS: Final = 43_200
MAX_LOGIN_ATTEMPTS: Final = 8
LOGIN_WINDOW_SECONDS: Final = 300
AUTH_NONCE_COOKIE: Final = "sefbot_dashboard_auth_nonce"
AUTH_NONCE_SECONDS: Final = 600
DISCORD_API: Final = "https://discord.com/api/v10"
FIREBASE_API: Final = "https://identitytoolkit.googleapis.com/v1"
MANAGE_GUILD_PERMISSIONS: Final = 0x8 | 0x20

GuildProvider = Callable[[], list[dict]]

_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_form_attempts: dict[str, deque[float]] = defaultdict(deque)


@dataclass(frozen=True, slots=True)
class DashboardAuthConfig:
    """Public and private values needed for account and Discord OAuth."""

    public_url: str = ""
    session_secret: str = ""
    firebase_api_key: str = ""
    firebase_auth_domain: str = ""
    firebase_project_id: str = ""
    firebase_app_id: str = ""
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
            and self.firebase_api_key
            and self.firebase_auth_domain
            and self.firebase_project_id
            and self.firebase_app_id
            and self.discord_client_id.isdigit()
            and self.discord_client_secret
        )

    @property
    def base_url(self) -> str:
        return self.public_url.rstrip("/")


def _page(title: str, body: str, *, script: str = "") -> str:
    app_script = (
        f'<script src="/dashboard/assets/{html.escape(script, quote=True)}"'
        + (' type="module"' if script == "auth.js" else " defer")
        + "></script>"
        if script
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>{html.escape(title)} · SefBot Control</title>
  <link rel="stylesheet" href="/dashboard/assets/app.css">
  {app_script}
</head>
<body>{body}</body>
</html>"""


_LOGIN_HTML: Final = """
<main class="login-shell">
  <section class="login-card">
    <a class="login-brand" href="/"><span class="brand-mark" aria-hidden="true">S</span><span>SefBot</span></a>
    <div class="step-label">Step 1 of 2</div>
    <h1>Create your account</h1>
    <p class="muted">Choose how you want to sign in. Next, you’ll connect Discord so SefBot can show only servers you manage.</p>
    <div id="auth-error" class="auth-message error" role="alert" hidden></div>
    <div id="auth-message" class="auth-message" role="status" hidden></div>
    <div class="provider-list">
      <button class="provider-button" type="button" data-provider="google"><span class="provider-icon google">G</span>Continue with Google</button>
      <button class="provider-button" type="button" data-provider="microsoft"><span class="provider-icon microsoft" aria-hidden="true"><i></i><i></i><i></i><i></i></span>Continue with Microsoft</button>
      <button class="provider-button" type="button" data-provider="apple"><span class="provider-icon apple" aria-hidden="true">●</span>Continue with Apple</button>
    </div>
    <div class="divider"><span>or</span></div>
    <form id="email-form" class="email-form">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" autocomplete="email" maxlength="254" required placeholder="you@example.com">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" minlength="8" maxlength="128" required placeholder="At least 8 characters">
      <button class="primary" type="submit">Continue with email</button>
    </form>
    <p class="tiny centered">By continuing, you agree to the <a href="/sefbot/terms">Terms</a> and acknowledge the <a href="/sefbot/privacy">Privacy Notice</a>.</p>
    <details class="owner-access">
      <summary>Owner access</summary>
      <form method="post" action="/dashboard/login" class="owner-form">
        <label for="token">Private dashboard token</label>
        <div><input id="token" name="token" type="password" required autocomplete="current-password" maxlength="512"><button type="submit">Sign in</button></div>
      </form>
    </details>
  </section>
</main>
"""

_DISCORD_HTML: Final = """
<main class="login-shell">
  <section class="login-card connect-card">
    <a class="login-brand" href="/"><span class="brand-mark" aria-hidden="true">S</span><span>SefBot</span></a>
    <div class="step-label">Step 2 of 2</div>
    <h1>Connect Discord</h1>
    <p class="muted">Your account is ready. Authorize Discord to find the servers you own or have permission to manage.</p>
    <div class="completed-step"><span aria-hidden="true">✓</span><div><strong>Account created</strong><small>Your sign-in is secure.</small></div></div>
    <a class="discord-button" href="/dashboard/auth/discord"><span aria-hidden="true">◆</span>Authorize with Discord</a>
    <p class="tiny centered">SefBot requests only your basic Discord identity and server list. It never receives your Discord password.</p>
    <form method="post" action="/dashboard/logout"><button class="link-button" type="submit">Use a different account</button></form>
  </section>
</main>
"""

_APP_HTML: Final = """
<div class="app-shell">
  <aside class="sidebar">
    <a class="logo" href="/dashboard"><span class="brand-mark small">S</span><span>SefBot</span></a>
    <nav aria-label="Dashboard sections">
      <button class="nav-item active" data-view="overview">Overview</button>
      <button class="nav-item" data-view="modules">Modules</button>
      <button class="nav-item" data-view="activity">Activity</button>
    </nav>
    <div class="side-bottom">
      <form method="post" action="/dashboard/logout"><button class="logout" type="submit">Sign out</button></form>
    </div>
  </aside>
  <main class="content">
    <header class="topbar">
      <div><h1 id="page-title">Overview</h1><p class="page-description">Manage SefBot without the clutter.</p></div>
      <label class="server-picker"><span>Server</span><select id="guild-select" aria-label="Select server"><option>Loading…</option></select></label>
    </header>
    <div id="notice" class="notice" role="status" hidden></div>
    <section id="overview-view" class="view">
      <div class="hero-panel">
        <div><h2>Your server at a glance</h2><p>Enable what you need. Every saved change is applied to the running bot and added to the audit log.</p></div>
        <button class="primary" data-go="modules">Manage modules</button>
      </div>
      <div class="stats" id="stats"></div>
      <div class="section-head"><h2>Module status</h2><button class="text-button" data-go="modules">View all</button></div>
      <div class="module-grid compact" id="quick-modules"></div>
    </section>
    <section id="modules-view" class="view" hidden>
      <div class="toolbar"><input id="module-search" type="search" placeholder="Search modules" aria-label="Search modules"><div id="category-filters" class="filters"></div></div>
      <div class="module-grid" id="module-grid"></div>
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

_CSS: Final = r"""
:root{color-scheme:dark;--bg:#090d0d;--panel:#111817;--panel2:#151e1c;--line:#26312f;--text:#f3f7f6;--muted:#91a19d;--lime:#b8ff4f;--teal:#34d6b0;--danger:#ff6b74;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 70% -15%,#16332c 0,transparent 35%),var(--bg);color:var(--text);min-height:100vh}button,input,select,textarea{font:inherit}button{cursor:pointer}.login-shell{min-height:100vh;display:grid;place-items:center;padding:2rem}.login-card{width:min(31rem,100%);padding:3rem;border:1px solid var(--line);border-radius:1.5rem;background:linear-gradient(145deg,#151f1d,#0f1514);box-shadow:0 30px 80px #0008}.brand-mark{width:3.6rem;height:3.6rem;border-radius:1rem;display:grid;place-items:center;background:var(--lime);color:#10200d;font-size:2rem;font-weight:900;box-shadow:0 0 35px #b8ff4f44}.brand-mark.small{width:2rem;height:2rem;border-radius:.55rem;font-size:1.05rem}.eyebrow{font-size:.69rem;letter-spacing:.17em;font-weight:800;color:var(--muted);margin:.8rem 0}.eyebrow.lime{color:var(--lime)}h1,h2,h3,p{margin-top:0}h1{font-size:clamp(2rem,4vw,3.3rem);line-height:1.02;letter-spacing:-.045em}h2{letter-spacing:-.025em}.muted{color:var(--muted);line-height:1.65}.tiny{color:#687672;font-size:.75rem;line-height:1.45}.login-form{display:grid;gap:.7rem;margin-top:2rem}.login-form label,.editor-fields label{font-size:.8rem;font-weight:750;color:#c7d1ce}.login-form input,input[type=search],.editor-fields input,.editor-fields textarea,.editor-fields select,.server-picker select{width:100%;border:1px solid var(--line);background:#0b1110;color:var(--text);border-radius:.7rem;padding:.85rem 1rem;outline:none}.login-form input:focus,input:focus,textarea:focus,select:focus{border-color:var(--teal);box-shadow:0 0 0 3px #34d6b022}button:not(.nav-item,.logout,.icon-button,.text-button){border:0;border-radius:.7rem;padding:.85rem 1.1rem;font-weight:800;background:var(--lime);color:#10200d}.app-shell{display:grid;grid-template-columns:15.5rem 1fr;min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;padding:1.4rem 1rem;border-right:1px solid var(--line);background:#0c1211;display:flex;flex-direction:column}.logo{display:flex;align-items:center;gap:.75rem;color:var(--text);text-decoration:none;font-size:1.15rem;font-weight:900;padding:.3rem .65rem 1.6rem}.sidebar nav{display:grid;gap:.35rem}.nav-item,.logout{border:0;background:transparent;color:var(--muted);padding:.75rem .85rem;border-radius:.65rem;text-align:left;font-weight:700}.nav-item span{display:inline-block;width:1.6rem}.nav-item:hover,.nav-item.active{background:#17211f;color:var(--text)}.nav-item.active span{color:var(--lime)}.side-bottom{margin-top:auto}.free-pill{display:flex;gap:.7rem;padding:.8rem;border:1px solid #34513f;background:#122019;border-radius:.8rem}.free-pill>span{color:var(--lime)}.free-pill strong,.free-pill small{display:block}.free-pill strong{font-size:.77rem}.free-pill small{color:var(--muted);font-size:.68rem;margin-top:.2rem}.logout{margin-top:.75rem;width:100%}.content{padding:2rem clamp(1.2rem,4vw,4rem) 5rem;max-width:105rem;width:100%;margin:auto}.topbar{display:flex;align-items:flex-end;justify-content:space-between;gap:1.5rem;margin-bottom:2rem}.topbar h1{font-size:2rem;margin:0}.server-picker{display:flex;align-items:center;gap:.65rem;color:var(--muted);font-size:.75rem}.server-picker select{min-width:14rem;padding:.7rem}.hero-panel{min-height:16rem;padding:2.3rem;border:1px solid #2d443d;border-radius:1.3rem;background:linear-gradient(120deg,#14251f 0,#111817 62%,#1d2d18);display:flex;justify-content:space-between;align-items:center;overflow:hidden}.hero-panel h2{font-size:clamp(2rem,4vw,3.5rem);max-width:15ch;margin-bottom:.8rem}.hero-panel p:not(.eyebrow){color:#aebbb7;max-width:55ch;line-height:1.6}.orb{width:12rem;height:12rem;display:grid;place-items:center;border-radius:50%;background:radial-gradient(circle at 35% 25%,#d8ff9d,var(--lime) 27%,#2a6d47 67%,#13231b);box-shadow:inset -25px -25px 60px #08281999,0 0 70px #b8ff4f25;transform:rotate(-8deg)}.orb span{font-size:5rem;font-weight:950;color:#10200d}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin:1rem 0 2.8rem}.stat{background:var(--panel);border:1px solid var(--line);border-radius:.9rem;padding:1.1rem}.stat strong{display:block;font-size:1.65rem}.stat small{color:var(--muted)}.section-head{display:flex;justify-content:space-between;align-items:end;margin:1rem 0}.section-head h2{margin:0}.text-button{border:0;background:transparent;color:var(--lime);font-weight:800}.toolbar{display:flex;gap:1rem;align-items:center;justify-content:space-between;margin-bottom:1.2rem}.toolbar input{max-width:24rem}.filters{display:flex;flex-wrap:wrap;gap:.45rem;justify-content:flex-end}.filter{border:1px solid var(--line)!important;background:#101716!important;color:var(--muted)!important;padding:.55rem .7rem!important;font-size:.72rem}.filter.active{border-color:#50733f!important;color:var(--lime)!important}.module-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(17.5rem,1fr));gap:1rem}.module-card{position:relative;border:1px solid var(--line);border-radius:.95rem;background:var(--panel);padding:1.15rem;min-height:12rem;display:flex;flex-direction:column;transition:.18s}.module-card:hover{border-color:#42524e;transform:translateY(-2px)}.module-card .category{color:var(--teal);font-size:.65rem;font-weight:850;letter-spacing:.11em;text-transform:uppercase}.module-card h3{margin:.55rem 0;font-size:1.02rem}.module-card p{font-size:.8rem;line-height:1.5;color:var(--muted)}.module-card footer{margin-top:auto;display:flex;align-items:center;justify-content:space-between}.status{display:flex;align-items:center;gap:.4rem;font-size:.72rem;color:var(--muted)}.status:before{content:"";width:.45rem;height:.45rem;border-radius:50%;background:#5f6b68}.status.on{color:#d8ffae}.status.on:before{background:var(--lime);box-shadow:0 0 9px #b8ff4f}.configure{border:0;background:transparent;color:var(--text);font-size:.75rem;font-weight:800}.compact .module-card:nth-child(n+9){display:none}.notice{position:fixed;right:2rem;top:1.5rem;z-index:20;padding:.8rem 1rem;border-radius:.7rem;background:#20342d;border:1px solid #456554;box-shadow:0 10px 40px #0008}.notice.error{background:#3c1b20;border-color:#754149}dialog{width:min(46rem,calc(100% - 2rem));max-height:90vh;padding:0;border:1px solid #3b4946;border-radius:1rem;background:#101716;color:var(--text);box-shadow:0 30px 100px #000b}dialog::backdrop{background:#020403c9;backdrop-filter:blur(5px)}.dialog-head{display:flex;justify-content:space-between;padding:1.25rem 1.5rem;border-bottom:1px solid var(--line)}.dialog-head h2{margin:0}.icon-button{border:0;background:transparent;color:var(--muted);font-size:2rem}.dialog-body{padding:1.3rem 1.5rem;overflow:auto;max-height:65vh}.switch-row{display:flex;justify-content:space-between;align-items:center;padding:1rem;border:1px solid var(--line);border-radius:.75rem;margin:1.2rem 0}.switch-row span strong,.switch-row span small{display:block}.switch-row small{color:var(--muted);margin-top:.25rem}.switch-row input{position:absolute;opacity:0}.switch-row i{width:2.8rem;height:1.5rem;border-radius:1rem;background:#34403d;position:relative}.switch-row i:after{content:"";position:absolute;width:1.1rem;height:1.1rem;top:.2rem;left:.2rem;border-radius:50%;background:white;transition:.18s}.switch-row input:checked+i{background:var(--lime)}.switch-row input:checked+i:after{left:1.5rem;background:#14210f}.editor-fields{display:grid;grid-template-columns:1fr 1fr;gap:1rem}.field{display:grid;gap:.45rem}.field.full{grid-column:1/-1}.field textarea{min-height:8rem;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.75rem;line-height:1.5}.field small{color:#6f7d79}.dialog-actions{display:flex;justify-content:flex-end;gap:.7rem;padding:1rem 1.5rem;border-top:1px solid var(--line)}.secondary{background:#222c2a!important;color:var(--text)!important}.activity-list{border:1px solid var(--line);border-radius:.9rem;overflow:hidden}.activity-row{display:grid;grid-template-columns:1.2fr 1fr .8fr;gap:1rem;padding:1rem;border-bottom:1px solid var(--line);font-size:.82rem}.activity-row:last-child{border:0}.activity-row small{color:var(--muted)}.empty{padding:2rem;color:var(--muted);text-align:center}@media(max-width:800px){.app-shell{display:block}.sidebar{height:auto;position:static;border-right:0;border-bottom:1px solid var(--line);flex-direction:row;align-items:center;gap:1rem}.sidebar nav{display:flex}.side-bottom,.sidebar .logo span:last-child{display:none}.logo{padding:0}.nav-item{font-size:0}.nav-item span{font-size:1rem;width:auto}.content{padding-top:1.2rem}.topbar{align-items:stretch;flex-direction:column}.server-picker{display:grid}.stats{grid-template-columns:1fr 1fr}.orb{display:none}.toolbar{align-items:stretch;flex-direction:column}.filters{justify-content:flex-start}.editor-fields{grid-template-columns:1fr}.activity-row{grid-template-columns:1fr}}@media(max-width:480px){.stats{grid-template-columns:1fr}.hero-panel{padding:1.4rem}.login-card{padding:2rem 1.4rem}.sidebar{overflow:auto}.content{padding-inline:1rem}}
"""

# A deliberately restrained visual layer over the original component rules.
# Keeping the component names stable avoids coupling the UI refresh to the API.
_SIMPLE_CSS: Final = r"""
:root{--bg:#0d0f12;--panel:#15181d;--panel2:#1a1e24;--line:#2a2f37;--text:#f5f6f7;--muted:#9ca3ad;--lime:#b8ff68;--teal:#b8ff68;--danger:#ff7b82}body{background:var(--bg)}a{color:inherit}.login-shell{padding:1.25rem;background:var(--bg)}.login-card{width:min(27rem,100%);padding:2rem;border-radius:1rem;background:var(--panel);box-shadow:none}.login-brand{display:inline-flex;align-items:center;gap:.65rem;margin-bottom:2rem;text-decoration:none;font-weight:850}.brand-mark{width:2.3rem;height:2.3rem;border-radius:.6rem;font-size:1.25rem;box-shadow:none}.brand-mark.small{width:1.8rem;height:1.8rem}.step-label{color:var(--lime);font-size:.75rem;font-weight:750;margin-bottom:.75rem}.login-card h1{font-size:2rem;letter-spacing:-.035em;margin-bottom:.7rem}.muted{line-height:1.55}.provider-list{display:grid;gap:.65rem;margin:1.5rem 0}.provider-button,.discord-button{display:flex!important;align-items:center;justify-content:center;gap:.8rem;width:100%;min-height:3rem;border:1px solid var(--line)!important;background:#1b1f25!important;color:var(--text)!important;text-decoration:none;border-radius:.65rem!important}.provider-button:hover,.discord-button:hover{border-color:#59616d!important;background:#20252c!important}.provider-icon{width:1.2rem;height:1.2rem;display:grid;place-items:center;font-weight:850}.provider-icon.google{color:#fff}.provider-icon.apple{font-size:.8rem}.provider-icon.microsoft{grid-template-columns:1fr 1fr;gap:1px}.provider-icon.microsoft i{width:.5rem;height:.5rem;background:#f25022}.provider-icon.microsoft i:nth-child(2){background:#7fba00}.provider-icon.microsoft i:nth-child(3){background:#00a4ef}.provider-icon.microsoft i:nth-child(4){background:#ffb900}.divider{display:flex;align-items:center;gap:.8rem;color:var(--muted);font-size:.72rem;margin:1rem 0}.divider:before,.divider:after{content:"";height:1px;background:var(--line);flex:1}.email-form{display:grid;gap:.55rem}.email-form label,.owner-form label,.editor-fields label{font-size:.78rem;font-weight:700;color:#cbd0d7}.email-form input,.owner-form input,input[type=search],.editor-fields input,.editor-fields textarea,.editor-fields select,.server-picker select{width:100%;border:1px solid var(--line);background:#101318;color:var(--text);border-radius:.6rem;padding:.75rem .85rem;outline:none}.email-form input:focus,.owner-form input:focus,input:focus,textarea:focus,select:focus{border-color:#69727f;box-shadow:0 0 0 3px #ffffff0a}.primary{background:var(--lime)!important;color:#14200a!important}.email-form .primary{margin-top:.4rem}.tiny.centered{text-align:center;margin:1rem 0 0}.tiny a{text-decoration:underline}.auth-message{padding:.75rem;border:1px solid #456b51;background:#193321;border-radius:.6rem;font-size:.8rem;margin:1rem 0}.auth-message.error{border-color:#714249;background:#351c20}.owner-access{margin-top:1.2rem;color:var(--muted);font-size:.75rem}.owner-access summary{cursor:pointer;text-align:center}.owner-form{display:grid;gap:.55rem;margin-top:.8rem}.owner-form>div{display:grid;grid-template-columns:1fr auto;gap:.5rem}.owner-form button{padding-inline:.8rem!important}.connect-card{text-align:center}.connect-card .login-brand{display:flex;justify-content:flex-start}.completed-step{display:flex;gap:.75rem;align-items:center;text-align:left;padding:.85rem;margin:1.4rem 0;border:1px solid var(--line);border-radius:.65rem;background:#111419}.completed-step>span{display:grid;place-items:center;width:1.8rem;height:1.8rem;border-radius:50%;background:#223b25;color:var(--lime);font-weight:850}.completed-step strong,.completed-step small{display:block}.completed-step small{color:var(--muted);margin-top:.15rem}.discord-button{background:#5865f2!important;border-color:#5865f2!important}.link-button{border:0!important;background:transparent!important;color:var(--muted)!important;padding:.5rem!important}.app-shell{grid-template-columns:13rem 1fr}.sidebar{padding:1.25rem .8rem;background:#111318}.logo{padding:.2rem .55rem 1.5rem}.nav-item,.logout{padding:.7rem .75rem}.nav-item:hover,.nav-item.active{background:#1c2026}.nav-item.active{color:var(--lime)}.content{padding:2rem clamp(1rem,4vw,3rem) 4rem;max-width:92rem}.topbar{align-items:center;margin-bottom:1.5rem}.topbar h1{font-size:1.65rem}.page-description{margin:.3rem 0 0;color:var(--muted);font-size:.82rem}.server-picker{display:grid;gap:.35rem}.server-picker select{min-width:13rem;padding:.65rem}.hero-panel{min-height:0;padding:1.5rem;border-color:var(--line);border-radius:.85rem;background:var(--panel)}.hero-panel h2{font-size:1.4rem;max-width:none;margin-bottom:.45rem}.hero-panel p{margin:0!important;max-width:55rem!important;font-size:.85rem}.hero-panel .primary{flex:none}.stats{grid-template-columns:repeat(3,1fr);gap:.75rem;margin:.75rem 0 2rem}.stat{padding:.9rem;border-radius:.7rem}.stat strong{font-size:1.3rem}.section-head{margin:1rem 0 .8rem}.section-head h2{font-size:1.05rem}.text-button{font-size:.78rem}.toolbar{margin-bottom:.9rem}.filter{border-radius:999px!important}.module-grid{grid-template-columns:repeat(auto-fill,minmax(16rem,1fr));gap:.75rem}.module-card{min-height:10rem;padding:1rem;border-radius:.75rem;transition:border-color .15s}.module-card:hover{transform:none;border-color:#4a515c}.module-card .category,.dialog-category{color:var(--lime);font-size:.63rem;font-weight:800;letter-spacing:.09em;text-transform:uppercase}.module-card p{margin-bottom:1rem}.configure{color:var(--lime)}.notice{right:1rem;top:1rem}.activity-list,dialog{border-radius:.75rem}.dialog-category{margin:0 0 .4rem}.secondary{background:#252a31!important}@media(max-width:800px){.app-shell{display:block}.sidebar{position:static;height:auto;flex-direction:row}.sidebar nav{display:flex}.side-bottom,.sidebar .logo span:last-child{display:none}.logo{padding:0}.nav-item{font-size:.78rem}.content{padding-top:1.2rem}.topbar{align-items:stretch}.stats{grid-template-columns:repeat(3,1fr)}}@media(max-width:560px){.login-card{padding:1.4rem}.hero-panel{display:block}.hero-panel .primary{margin-top:1rem;width:100%}.stats{grid-template-columns:1fr}.toolbar{align-items:stretch;flex-direction:column}.filters{justify-content:flex-start}.editor-fields{grid-template-columns:1fr}.activity-row{grid-template-columns:1fr}}
"""

_JS: Final = r"""
const state={csrf:"",catalog:[],configs:new Map(),guilds:[],guildId:"",category:"All",editing:null};
const q=s=>document.querySelector(s),qa=s=>[...document.querySelectorAll(s)];
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
async function api(path,options={}){options.headers={"Accept":"application/json",...(options.headers||{})};if(options.method&&options.method!=="GET")options.headers["X-CSRF-Token"]=state.csrf;const r=await fetch(`/dashboard/api${path}`,options);if(r.status===401){location.reload();throw Error("Session expired")};const body=await r.json().catch(()=>({error:"Invalid response"}));if(!r.ok)throw Error(body.error||`Request failed (${r.status})`);return body}
function notice(message,error=false){const el=q("#notice");el.textContent=message;el.classList.toggle("error",error);el.hidden=false;clearTimeout(notice.timer);notice.timer=setTimeout(()=>el.hidden=true,3500)}
function showView(name){qa(".view").forEach(x=>x.hidden=x.id!==`${name}-view`);qa(".nav-item").forEach(x=>x.classList.toggle("active",x.dataset.view===name));q("#page-title").textContent=name[0].toUpperCase()+name.slice(1);if(name==="activity")loadActivity()}
function configFor(id){return state.configs.get(id)||{module:id,enabled:false,settings:{}}}
function moduleCard(m){const c=configFor(m.id),coverage=m.implementation||"core live";return `<article class="module-card" data-id="${esc(m.id)}"><span class="category">${esc(m.category)}</span><h3>${esc(m.title)}</h3><p>${esc(m.description)}</p><footer><span class="status ${c.enabled?"on":""}" title="Implementation coverage">${c.enabled?"Enabled":"Disabled"} · ${esc(coverage)}</span><button class="configure" data-edit="${esc(m.id)}">Configure →</button></footer></article>`}
function render(){const search=(q("#module-search")?.value||"").toLowerCase();const mods=state.catalog.filter(m=>(state.category==="All"||m.category===state.category)&&(`${m.title} ${m.description}`.toLowerCase().includes(search)));q("#module-grid").innerHTML=mods.map(moduleCard).join("")||'<div class="empty">No matching modules.</div>';q("#quick-modules").innerHTML=state.catalog.map(moduleCard).join("");qa("[data-edit]").forEach(b=>b.onclick=()=>openEditor(b.dataset.edit));const enabled=[...state.configs.values()].filter(x=>x.enabled).length;q("#stats").innerHTML=[['Modules',state.catalog.length],['Enabled',enabled],['Categories',new Set(state.catalog.map(x=>x.category)).size]].map(([a,b])=>`<div class="stat"><strong>${esc(b)}</strong><small>${esc(a)}</small></div>`).join("")}
function filters(){const cats=["All",...new Set(state.catalog.map(x=>x.category))];q("#category-filters").innerHTML=cats.map(x=>`<button class="filter ${x===state.category?"active":""}" data-category="${esc(x)}">${esc(x)}</button>`).join("");qa("[data-category]").forEach(b=>b.onclick=()=>{state.category=b.dataset.category;filters();render()})}
function label(key){return key.replaceAll("_"," ").replace(/\b\w/g,c=>c.toUpperCase())}
function openEditor(id){const m=state.catalog.find(x=>x.id===id),c=configFor(id);state.editing=id;q("#editor-category").textContent=m.category;q("#editor-title").textContent=m.title;q("#editor-description").textContent=m.description;q("#editor-enabled").checked=!!c.enabled;const values={...m.settings,...c.settings};q("#editor-fields").innerHTML=Object.entries(values).map(([k,v])=>{const complex=typeof v==="object";let input;if(typeof v==="boolean")input=`<select data-key="${esc(k)}" data-type="bool"><option value="true" ${v?'selected':''}>Yes</option><option value="false" ${!v?'selected':''}>No</option></select>`;else if(typeof v==="number")input=`<input data-key="${esc(k)}" data-type="number" type="number" step="any" value="${esc(v)}">`;else if(complex)input=`<textarea data-key="${esc(k)}" data-type="json" spellcheck="false">${esc(JSON.stringify(v,null,2))}</textarea>`;else input=`<input data-key="${esc(k)}" data-type="string" value="${esc(v)}">`;return `<label class="field ${complex?'full':''}"><span>${esc(label(k))}</span>${input}${complex?'<small>JSON list or object</small>':''}</label>`}).join("");q("#editor-dialog").showModal()}
async function saveEditor(){const settings={};try{qa("#editor-fields [data-key]").forEach(el=>{const t=el.dataset.type;settings[el.dataset.key]=t==="json"?JSON.parse(el.value):t==="number"?Number(el.value):t==="bool"?el.value==="true":el.value})}catch(e){notice("A JSON setting is invalid.",true);return}q("#editor-save").disabled=true;try{const result=await api(`/guild/${encodeURIComponent(state.guildId)}/module/${encodeURIComponent(state.editing)}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:q("#editor-enabled").checked,settings})});state.configs.set(result.module,result);q("#editor-dialog").close();render();notice(`${state.catalog.find(x=>x.id===state.editing).title} saved.`)}catch(e){notice(e.message,true)}finally{q("#editor-save").disabled=false}}
async function loadGuild(){if(!state.guildId)return;const data=await api(`/guild/${encodeURIComponent(state.guildId)}/modules`);state.configs=new Map(data.modules.map(x=>[x.module,x]));render()}
async function loadActivity(){if(!state.guildId)return;try{const data=await api(`/guild/${encodeURIComponent(state.guildId)}/activity`);q("#activity-list").innerHTML=data.items.length?data.items.map(x=>`<div class="activity-row"><div><strong>${esc(x.action)}</strong><br><small>${esc(x.module||"System")}</small></div><div>${esc(x.actor_id)}</div><div><small>${new Date(x.created*1000).toLocaleString()}</small></div></div>`).join(""):'<div class="empty">No dashboard changes yet.</div>'}catch(e){notice(e.message,true)}}
async function boot(){try{const session=await api("/session");state.csrf=session.csrf;const [catalog,guilds]=await Promise.all([api("/catalog"),api("/guilds")]);state.catalog=catalog.modules;state.guilds=guilds.guilds;const select=q("#guild-select");select.innerHTML=state.guilds.length?state.guilds.map(g=>`<option value="${esc(g.id)}">${esc(g.name)}</option>`).join(""):'<option value="">No connected servers</option>';state.guildId=select.value;select.onchange=()=>{state.guildId=select.value;loadGuild().catch(e=>notice(e.message,true))};filters();await loadGuild()}catch(e){notice(e.message,true)}}
qa(".nav-item").forEach(b=>b.onclick=()=>showView(b.dataset.view));qa("[data-go]").forEach(b=>b.onclick=()=>showView(b.dataset.go));q("#module-search").oninput=render;q("#editor-save").onclick=saveEditor;q("#editor-cancel").onclick=()=>q("#editor-dialog").close();boot();
"""

_AUTH_JS: Final = r"""
import { initializeApp } from "https://www.gstatic.com/firebasejs/12.17.1/firebase-app.js";
import { GoogleAuthProvider, OAuthProvider, browserLocalPersistence, createUserWithEmailAndPassword, getAuth, getRedirectResult, sendEmailVerification, setPersistence, signInWithEmailAndPassword, signInWithRedirect, signOut } from "https://www.gstatic.com/firebasejs/12.17.1/firebase-auth.js";

const errorBox=document.querySelector("#auth-error"),messageBox=document.querySelector("#auth-message"),buttons=[...document.querySelectorAll("[data-provider]")],form=document.querySelector("#email-form");
function show(message,error=false){const box=error?errorBox:messageBox,other=error?messageBox:errorBox;other.hidden=true;box.textContent=message;box.hidden=false}
function busy(value){buttons.forEach(button=>button.disabled=value);if(form)form.querySelector("button").disabled=value}
function friendly(error){const code=String(error?.code||"");if(code.includes("wrong-password")||code.includes("invalid-credential"))return "That email or password is not correct.";if(code.includes("too-many-requests"))return "Too many attempts. Please wait and try again.";if(code.includes("popup-blocked"))return "Your browser blocked sign-in. Allow pop-ups and try again.";if(code.includes("network-request-failed"))return "Sign-in could not reach the account service.";return "Sign-in could not be completed. Please try again."}

try{
  const response=await fetch("/dashboard/api/auth/config",{headers:{"Accept":"application/json"}});
  if(!response.ok)throw Error("not-configured");
  const setup=await response.json();
  const auth=getAuth(initializeApp(setup.firebase));
  await setPersistence(auth,browserLocalPersistence);
  if(new URL(location.href).searchParams.has("signed_out")){await signOut(auth);history.replaceState({},"","/dashboard")}
  async function finish(user){
    const passwordOnly=user.providerData.some(item=>item.providerId==="password")&&!user.providerData.some(item=>item.providerId!=="password");
    if(passwordOnly&&!user.emailVerified){await sendEmailVerification(user);await signOut(auth);show("Check your inbox to verify your email, then come back and sign in.");busy(false);return}
    const token=await user.getIdToken(true);
    const result=await fetch("/dashboard/auth/firebase",{method:"POST",headers:{"Accept":"application/json","Content-Type":"application/json","X-Login-CSRF":setup.csrf},body:JSON.stringify({id_token:token})});
    if(!result.ok)throw Error("backend-auth");
    location.assign("/dashboard");
  }
  const redirected=await getRedirectResult(auth);
  if(redirected){busy(true);await finish(redirected.user)}else if(auth.currentUser){busy(true);await finish(auth.currentUser)}
  buttons.forEach(button=>button.addEventListener("click",async()=>{
    try{busy(true);let provider;if(button.dataset.provider==="google")provider=new GoogleAuthProvider();else if(button.dataset.provider==="microsoft")provider=new OAuthProvider("microsoft.com");else{provider=new OAuthProvider("apple.com");provider.addScope("email");provider.addScope("name")}await signInWithRedirect(auth,provider)}catch(error){busy(false);show(friendly(error),true)}
  }));
  form?.addEventListener("submit",async event=>{
    event.preventDefault();busy(true);const data=new FormData(form),email=String(data.get("email")||"").trim(),password=String(data.get("password")||"");
    try{let credential;try{credential=await createUserWithEmailAndPassword(auth,email,password)}catch(error){if(error?.code!=="auth/email-already-in-use")throw error;credential=await signInWithEmailAndPassword(auth,email,password)}await finish(credential.user)}catch(error){busy(false);show(friendly(error),true)}
  });
}catch(error){busy(true);show("Account sign-in is being configured. Owner access is still available below.",true)}
"""


def _client_key(request: web.Request) -> str:
    return request.remote or "unknown"


def _login_limited(key: str) -> bool:
    now = time.monotonic()
    attempts = _login_attempts[key]
    while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    return len(attempts) >= MAX_LOGIN_ATTEMPTS


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


def _signed_payload(secret: bytes, value: dict) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
    encoded = payload.encode("utf-8").hex()
    return f"{encoded}.{_sign(secret, encoded)}"


def _decode_payload(secret: bytes, raw: str) -> dict | None:
    try:
        encoded, signature = raw.rsplit(".", 1)
        if not hmac.compare_digest(signature, _sign(secret, encoded)):
            return None
        value = json.loads(bytes.fromhex(encoded).decode("utf-8"))
        if not isinstance(value, dict) or int(value.get("exp", 0)) <= int(time.time()):
            return None
        return value
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _new_session(
    secret: bytes,
    *,
    actor: str = "dashboard:owner",
    owner: bool = True,
    discord_id: str = "",
    guild_ids: list[str] | None = None,
) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    payload = {
        "actor": actor[:160],
        "csrf": csrf,
        "exp": int(time.time()) + SESSION_SECONDS,
        "owner": bool(owner),
    }
    if discord_id:
        payload["discord_id"] = discord_id[:24]
        payload["guild_ids"] = list(dict.fromkeys(guild_ids or []))[:100]
    return _signed_payload(secret, payload), csrf


def _session(request: web.Request, secret: bytes) -> dict | None:
    value = _decode_payload(secret, request.cookies.get(SESSION_COOKIE, ""))
    if value is None or not str(value.get("actor", "")) or not str(value.get("csrf", "")):
        return None
    return value


async def _read_provider_json(response: object, limit: int = 1_000_000) -> dict | list | None:
    content = getattr(response, "content", None)
    if content is None:
        return None
    body = await content.read(limit + 1)
    if len(body) > limit:
        return None
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, (dict, list)) else None


async def _firebase_user(api_key: str, id_token: str) -> dict | None:
    if not api_key or not 100 <= len(id_token) <= 16_384:
        return None
    timeout = ClientTimeout(total=10)
    url = f"{FIREBASE_API}/accounts:lookup?{urlencode({'key': api_key})}"
    try:
        async with ClientSession(timeout=timeout) as client:
            async with client.post(url, json={"idToken": id_token}) as response:
                payload = await _read_provider_json(response)
                if response.status != 200 or not isinstance(payload, dict):
                    return None
    except Exception:  # noqa: BLE001 - external authentication fails closed
        return None
    users = payload.get("users")
    user = users[0] if isinstance(users, list) and users else None
    if not isinstance(user, dict) or user.get("disabled") is True:
        return None
    uid = str(user.get("localId") or "")
    return user if 1 <= len(uid) <= 128 else None


async def _discord_identity(
    auth: DashboardAuthConfig, code: str
) -> tuple[dict, list[dict]] | None:
    if not auth.ready() or not 10 <= len(code) <= 2048:
        return None
    timeout = ClientTimeout(total=12)
    redirect_uri = auth.base_url + DASHBOARD_PREFIX + "/auth/discord/callback"
    try:
        async with ClientSession(timeout=timeout) as client:
            async with client.post(
                f"{DISCORD_API}/oauth2/token",
                data={
                    "client_id": auth.discord_client_id,
                    "client_secret": auth.discord_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            ) as response:
                token_payload = await _read_provider_json(response)
                if response.status != 200 or not isinstance(token_payload, dict):
                    return None
            access_token = str(token_payload.get("access_token") or "")
            if not access_token:
                return None
            headers = {"Authorization": f"Bearer {access_token}"}
            async with client.get(f"{DISCORD_API}/users/@me", headers=headers) as response:
                user = await _read_provider_json(response)
                if response.status != 200 or not isinstance(user, dict):
                    return None
            async with client.get(
                f"{DISCORD_API}/users/@me/guilds?with_counts=false", headers=headers
            ) as response:
                guilds = await _read_provider_json(response)
                if response.status != 200 or not isinstance(guilds, list):
                    return None
    except Exception:  # noqa: BLE001 - external authentication fails closed
        return None
    if not str(user.get("id") or "").isdigit():
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
            "style-src 'self'; script-src 'self' https://www.gstatic.com; "
            "connect-src 'self' https://identitytoolkit.googleapis.com "
            "https://securetoken.googleapis.com https://www.googleapis.com; "
            "frame-src https://*.firebaseapp.com https://accounts.google.com"
        )


def attach_dashboard_routes(
    app: web.Application,
    *,
    access_token: str | None,
    guild_provider: GuildProvider | None = None,
    auth_config: DashboardAuthConfig | None = None,
) -> None:
    """Attach dashboard UI and JSON API to an aiohttp application."""
    raw_token = str(access_token or "").strip()
    token = raw_token if len(raw_token) >= 24 else ""
    auth = auth_config or DashboardAuthConfig()
    auth_ready = auth.ready()
    secret_material = auth.session_secret if auth_ready else token
    secret = hashlib.sha256(("sefbot-dashboard:" + secret_material).encode("utf-8")).digest()
    provider = guild_provider or (lambda: [])

    def authenticated(request: web.Request) -> dict | None:
        return _session(request, secret) if token or auth_ready else None

    def require_session(request: web.Request) -> dict:
        session = authenticated(request)
        if session is None or not (session.get("owner") or session.get("discord_id")):
            raise web.HTTPUnauthorized(text="authentication required")
        return session

    def require_csrf(request: web.Request, session: dict) -> None:
        supplied = request.headers.get("X-CSRF-Token", "")
        expected = str(session.get("csrf", ""))
        if not expected or not hmac.compare_digest(supplied, expected):
            raise web.HTTPForbidden(text="invalid CSRF token")

    def all_guilds() -> list[dict]:
        try:
            raw = provider()
        except Exception:
            raw = []
        output = []
        for item in raw[:500] if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not str(item.get("id", "")).isdigit():
                continue
            output.append({
                "id": str(item["id"]), "name": str(item.get("name") or item["id"])[:100],
                "icon": str(item.get("icon") or "")[:500],
                "member_count": max(0, int(item.get("member_count") or 0)),
                "channels": item.get("channels") if isinstance(item.get("channels"), list) else [],
                "roles": item.get("roles") if isinstance(item.get("roles"), list) else [],
            })
        return output

    def guilds(session: dict) -> list[dict]:
        available = all_guilds()
        if session.get("owner") is True:
            return available
        allowed = {
            str(value) for value in session.get("guild_ids", [])
            if str(value).isdigit()
        }
        return [item for item in available if item["id"] in allowed]

    def require_guild(session: dict, guild_id: str) -> dict:
        match = next(
            (item for item in guilds(session) if item["id"] == str(guild_id)), None
        )
        if match is None:
            raise web.HTTPNotFound(text="server is not available to this account")
        return match

    def require_connected_guild(guild_id: str) -> dict:
        match = next(
            (item for item in all_guilds() if item["id"] == str(guild_id)), None
        )
        if match is None:
            raise web.HTTPNotFound(text="server is not connected")
        return match

    def secure_request(request: web.Request) -> bool:
        return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def set_session_cookie(
        response: web.StreamResponse, request: web.Request, value: str
    ) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            value,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=secure_request(request),
            samesite="Lax",
            path=DASHBOARD_PREFIX,
        )

    async def index(request: web.Request) -> web.Response:
        session = authenticated(request)
        if session and (session.get("owner") or session.get("discord_id")):
            response = web.Response(
                text=_page("Dashboard", _APP_HTML, script="app.js"),
                content_type="text/html",
            )
        elif session and auth_ready:
            response = web.Response(
                text=_page("Connect Discord", _DISCORD_HTML), content_type="text/html"
            )
        else:
            setup = ""
            if not auth_ready:
                setup = (
                    '<div class="auth-message error">Account sign-in is not configured yet. '
                    "Owner access is available below.</div>"
                )
            response = web.Response(
                text=_page(
                    "Sign in", setup + _LOGIN_HTML, script="auth.js" if auth_ready else ""
                ),
                content_type="text/html",
                status=200 if token or auth_ready else 503,
            )
        _dashboard_headers(response)
        return response

    async def login(request: web.Request) -> web.StreamResponse:
        key = _client_key(request)
        if not token or _login_limited(key):
            raise web.HTTPTooManyRequests(text="too many sign-in attempts")
        data = await request.post()
        supplied = str(data.get("token", ""))
        if not hmac.compare_digest(supplied, token):
            _login_attempts[key].append(time.monotonic())
            raise web.HTTPUnauthorized(text="invalid dashboard token")
        _login_attempts.pop(key, None)
        value, _csrf = _new_session(secret)
        response = web.HTTPSeeOther(location=DASHBOARD_PREFIX)
        set_session_cookie(response, request, value)
        raise response

    async def logout(request: web.Request) -> web.StreamResponse:
        response = web.HTTPSeeOther(location=DASHBOARD_PREFIX + "?signed_out=1")
        response.del_cookie(SESSION_COOKIE, path=DASHBOARD_PREFIX)
        raise response

    async def css(_request: web.Request) -> web.Response:
        response = web.Response(text=_CSS + _SIMPLE_CSS, content_type="text/css")
        _dashboard_headers(response, "text/css")
        return response

    async def js(_request: web.Request) -> web.Response:
        response = web.Response(text=_JS, content_type="application/javascript")
        _dashboard_headers(response)
        return response

    async def auth_js(_request: web.Request) -> web.Response:
        response = web.Response(text=_AUTH_JS, content_type="application/javascript")
        _dashboard_headers(response)
        return response

    async def auth_config_api(request: web.Request) -> web.Response:
        if not auth_ready:
            raise web.HTTPServiceUnavailable(text="account sign-in is not configured")
        csrf = secrets.token_urlsafe(24)
        response = web.json_response(
            {
                "csrf": csrf,
                "firebase": {
                    "apiKey": auth.firebase_api_key,
                    "authDomain": auth.firebase_auth_domain,
                    "projectId": auth.firebase_project_id,
                    "appId": auth.firebase_app_id,
                },
            }
        )
        response.set_cookie(
            AUTH_NONCE_COOKIE,
            csrf,
            max_age=AUTH_NONCE_SECONDS,
            httponly=True,
            secure=secure_request(request),
            samesite="Strict",
            path=DASHBOARD_PREFIX,
        )
        _dashboard_headers(response)
        return response

    async def firebase_login(request: web.Request) -> web.Response:
        key = _client_key(request)
        if not auth_ready or _login_limited(key):
            raise web.HTTPTooManyRequests(text="too many sign-in attempts")
        expected = request.cookies.get(AUTH_NONCE_COOKIE, "")
        supplied = request.headers.get("X-Login-CSRF", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            raise web.HTTPForbidden(text="invalid sign-in CSRF token")
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="invalid JSON") from None
        id_token = str(body.get("id_token") or "") if isinstance(body, dict) else ""
        user = await _firebase_user(auth.firebase_api_key, id_token)
        if user is None:
            _login_attempts[key].append(time.monotonic())
            raise web.HTTPUnauthorized(text="invalid account token")
        provider_info = user.get("providerUserInfo")
        provider_ids = {
            str(item.get("providerId") or "")
            for item in provider_info if isinstance(item, dict)
        } if isinstance(provider_info, list) else set()
        if provider_ids == {"password"} and user.get("emailVerified") is not True:
            raise web.HTTPForbidden(text="verify your email before continuing")
        _login_attempts.pop(key, None)
        actor = "account:" + str(user["localId"])
        value, _csrf = _new_session(secret, actor=actor, owner=False)
        response = web.json_response({"next": DASHBOARD_PREFIX})
        set_session_cookie(response, request, value)
        response.del_cookie(AUTH_NONCE_COOKIE, path=DASHBOARD_PREFIX)
        _dashboard_headers(response)
        return response

    async def discord_start(request: web.Request) -> web.StreamResponse:
        session = authenticated(request)
        if not auth_ready or session is None:
            raise web.HTTPUnauthorized(text="account sign-in required")
        if session.get("owner") or session.get("discord_id"):
            raise web.HTTPSeeOther(location=DASHBOARD_PREFIX)
        state = _signed_payload(
            secret,
            {
                "actor": str(session["actor"]),
                "csrf": str(session["csrf"]),
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
        raise web.HTTPSeeOther(location=f"https://discord.com/oauth2/authorize?{query}")

    async def discord_callback(request: web.Request) -> web.StreamResponse:
        session = authenticated(request)
        if not auth_ready or session is None or session.get("owner"):
            raise web.HTTPUnauthorized(text="account sign-in required")
        if request.query.get("error"):
            raise web.HTTPSeeOther(location=DASHBOARD_PREFIX + "?discord=cancelled")
        state = _decode_payload(secret, str(request.query.get("state") or ""))
        if (
            state is None
            or not hmac.compare_digest(str(state.get("csrf") or ""), str(session["csrf"]))
            or not hmac.compare_digest(str(state.get("actor") or ""), str(session["actor"]))
        ):
            raise web.HTTPForbidden(text="invalid Discord OAuth state")
        identity = await _discord_identity(auth, str(request.query.get("code") or ""))
        if identity is None:
            raise web.HTTPBadGateway(text="Discord authorization failed")
        user, discord_guilds = identity
        connected = {item["id"] for item in all_guilds()}
        manageable = []
        for item in discord_guilds:
            guild_id = str(item.get("id") or "")
            try:
                permissions = int(str(item.get("permissions") or "0"))
            except ValueError:
                permissions = 0
            if guild_id in connected and (
                item.get("owner") is True or permissions & MANAGE_GUILD_PERMISSIONS
            ):
                manageable.append(guild_id)
        value, _csrf = _new_session(
            secret,
            actor=str(session["actor"]),
            owner=False,
            discord_id=str(user["id"]),
            guild_ids=manageable,
        )
        response = web.HTTPSeeOther(location=DASHBOARD_PREFIX)
        set_session_cookie(response, request, value)
        raise response

    async def session_api(request: web.Request) -> web.Response:
        session = require_session(request)
        return web.json_response({"actor": session["actor"], "csrf": session["csrf"]})

    async def catalog_api(request: web.Request) -> web.Response:
        require_session(request)
        return web.json_response({"modules": public_catalog(), "free": True})

    async def guilds_api(request: web.Request) -> web.Response:
        session = require_session(request)
        return web.json_response({"guilds": guilds(session)})

    async def modules_api(request: web.Request) -> web.Response:
        session = require_session(request)
        guild = require_guild(session, request.match_info["guild_id"])
        return web.json_response({"guild": guild, "modules": db.module_configs(guild["id"])})

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
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            raise web.HTTPBadRequest(text="enabled must be a boolean")
        try:
            result = db.module_config_set(
                guild["id"], module, enabled=payload["enabled"],
                settings=payload.get("settings") if isinstance(payload.get("settings"), dict) else {},
                actor_id=str(session["actor"]),
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        return web.json_response(result)

    async def activity_api(request: web.Request) -> web.Response:
        session = require_session(request)
        guild = require_guild(session, request.match_info["guild_id"])
        return web.json_response({"items": db.dashboard_audit_list(guild["id"], 200)})

    def configured_form(guild_id: str, slug: str) -> tuple[dict, dict] | None:
        config = db.module_config(guild_id, "forms")
        if not config["enabled"]:
            return None
        for item in config["settings"].get("forms", [])[:100]:
            if (
                isinstance(item, dict)
                and item.get("enabled", True) is not False
                and str(item.get("slug") or "").lower() == slug.lower()
            ):
                return config, item
        return None

    def form_access(guild_id: str, form: dict, token_value: str) -> dict | None:
        if not form.get("members_only"):
            return None
        if not token_value:
            raise web.HTTPForbidden(text="Open this form from its Discord command.")
        records = db.community_records("form_access", f"guild:{guild_id}", limit=5000)
        access = next(
            (
                item for item in records
                if item.get("record_key") == token_value
                and item["data"].get("form_slug") == form.get("slug")
                and float(item.get("due") or 0) > time.time()
            ),
            None,
        )
        if access is None:
            raise web.HTTPForbidden(text="This form link is invalid or expired.")
        return access

    def form_document(guild_id: str, form: dict, token_value: str) -> str:
        fields = []
        for index, question in enumerate(form.get("questions", [])[:50]):
            if not isinstance(question, dict):
                continue
            qid = re.sub(r"[^a-zA-Z0-9_-]", "", str(question.get("id") or index))[:40]
            label = html.escape(str(question.get("label") or f"Question {index + 1}")[:300])
            required = " required" if question.get("required") else ""
            kind = str(question.get("type") or "short")
            name = f"q_{qid}"
            if kind == "paragraph":
                control = f'<textarea name="{name}" maxlength="4000"{required}></textarea>'
            elif kind in {"multiple_choice", "checkbox"}:
                input_type = "radio" if kind == "multiple_choice" else "checkbox"
                options = "".join(
                    f'<label class="option"><input type="{input_type}" name="{name}" value="{html.escape(str(option)[:300], quote=True)}"{required}> {html.escape(str(option)[:300])}</label>'
                    for option in question.get("options", [])[:30]
                )
                control = f'<div class="option-list">{options}</div>'
            else:
                control = f'<input name="{name}" maxlength="1000"{required}>'
            fields.append(f'<label class="field full"><strong>{label}</strong>{control}</label>')
        safe_title = html.escape(str(form.get("title") or "Form")[:200])
        description = html.escape(str(form.get("description") or "")[:2000])
        return _page(
            safe_title,
            f'''<main class="login-shell"><section class="login-card form-card"><div class="brand-mark">S</div><p class="eyebrow">SEFBOT FORM</p><h1>{safe_title}</h1><p class="muted">{description}</p><form method="post" action="/forms/{html.escape(guild_id, quote=True)}/{html.escape(str(form.get("slug")), quote=True)}" class="editor-fields public-form"><input type="hidden" name="access_token" value="{html.escape(token_value, quote=True)}">{"".join(fields)}<button type="submit">Submit response</button></form></section></main>''',
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
        if _form_limited("form:" + _client_key(request)):
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
        existing = db.community_records(
            "form_submission", f"guild:{guild['id']}", user_id=user_id, status=None, limit=5000
        ) if user_id else []
        same_form = [item for item in existing if item["data"].get("form_slug") == form.get("slug")]
        if form.get("one_submission") and same_form:
            raise web.HTTPConflict(text="You already submitted this form.")
        cooldown = max(0, min(31_536_000, int(form.get("cooldown_seconds") or 0)))
        if same_form and cooldown and time.time() - float(same_form[-1]["created"]) < cooldown:
            raise web.HTTPTooManyRequests(text="This form is still on cooldown.")
        answers = []
        for index, question in enumerate(form.get("questions", [])[:50]):
            if not isinstance(question, dict):
                continue
            qid = re.sub(r"[^a-zA-Z0-9_-]", "", str(question.get("id") or index))[:40]
            name = f"q_{qid}"
            values = [str(value)[:4000] for value in body.getall(name, [])]
            if question.get("required") and not any(value.strip() for value in values):
                raise web.HTTPBadRequest(text=f"Missing required answer: {question.get('label')}")
            answers.append({"id": qid, "label": str(question.get("label") or qid)[:300], "values": values})
        db.community_record_create(
            "form_submission",
            f"guild:{guild['id']}",
            {
                "form_slug": str(form.get("slug")), "form_title": str(form.get("title") or "Form"),
                "answers": answers, "channel_id": str(form.get("channel_id") or ""),
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
    app.router.add_post(DASHBOARD_PREFIX + "/login", login)
    app.router.add_post(DASHBOARD_PREFIX + "/logout", logout)
    app.router.add_get(DASHBOARD_PREFIX + "/assets/app.css", css)
    app.router.add_get(DASHBOARD_PREFIX + "/assets/app.js", js)
    app.router.add_get(DASHBOARD_PREFIX + "/assets/auth.js", auth_js)
    app.router.add_get(DASHBOARD_PREFIX + "/api/auth/config", auth_config_api)
    app.router.add_post(DASHBOARD_PREFIX + "/auth/firebase", firebase_login)
    app.router.add_get(DASHBOARD_PREFIX + "/auth/discord", discord_start)
    app.router.add_get(DASHBOARD_PREFIX + "/auth/discord/callback", discord_callback)
    app.router.add_get(DASHBOARD_PREFIX + "/api/session", session_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/catalog", catalog_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/guilds", guilds_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/guild/{guild_id}/modules", modules_api)
    app.router.add_put(DASHBOARD_PREFIX + "/api/guild/{guild_id}/module/{module}", update_module_api)
    app.router.add_get(DASHBOARD_PREFIX + "/api/guild/{guild_id}/activity", activity_api)
    app.router.add_get("/forms/{guild_id}/{slug}", form_get)
    app.router.add_post("/forms/{guild_id}/{slug}", form_post)
