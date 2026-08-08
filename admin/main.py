"""autoagents admin webapp — Cloud Run service `autoagents-admin`.

Server-rendered (no client JS framework); sign-in is an email magic link
restricted to ADMIN_EMAIL, with the shared password kept as break-glass.
Lets an operator create/edit tenants, assign email + phone identities, flip
per-tenant run-state (running/paused/stopped) and lifecycle status, and review
recent messages + tasks. Reads/writes the same Firestore the gateway/agent use.
"""
from __future__ import annotations

import hmac
import html
import logging

import requests
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from admin import config, tenancy

app = FastAPI(title="autoagents-admin")
log = logging.getLogger("admin")


# --------------------------------------------------------------------------- #
# Auth — email magic link (primary) + break-glass password.
#
# Session cookies and magic-link tokens are signed with MAGIC_SECRET, decoupled
# from the break-glass password so the latter can be rotated without bumping
# everyone's session. Only ADMIN_EMAIL may request (and redeem) a magic link.
# --------------------------------------------------------------------------- #
def _signer(salt: str) -> URLSafeTimedSerializer:
    # No placeholder fallback. Signing with a known literal like "unset" would let
    # anyone mint a valid session cookie offline and walk into the console — which
    # is now on a public domain. A misconfigured deploy must refuse to serve auth.
    key = config.MAGIC_SECRET or config.ADMIN_PASSWORD
    if not key:
        log.error("neither MAGIC_SECRET nor ADMIN_PASSWORD set — refusing to sign")
        raise HTTPException(500, "server not configured")
    return URLSafeTimedSerializer(key, salt=salt)


def _serializer() -> URLSafeTimedSerializer:
    """Signer for the logged-in session cookie."""
    return _signer("aa-admin")


def _authed(request: Request) -> bool:
    tok = request.cookies.get(config.COOKIE_NAME)
    if not tok:
        return False
    try:
        _serializer().loads(tok, max_age=config.SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _set_session(resp: Response) -> Response:
    resp.set_cookie(
        config.COOKIE_NAME,
        _serializer().dumps("ok"),
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
    )
    return resp


def _magic_link(email: str) -> str:
    token = _signer("aa-magic").dumps(email)
    return config.ADMIN_PUBLIC_URL.rstrip("/") + f"/auth?token={token}"


def _send_magic_email(email: str, link: str) -> bool:
    """Email a sign-in link via Resend. Returns True on a 2xx send."""
    if not config.RESEND_API_KEY:
        log.error("no RESEND_API_KEY configured; cannot send magic link")
        return False
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            json={
                "from": f"autoagents admin <{config.SENDER_EMAIL}>",
                "to": [email],
                "subject": "Your autoagents admin sign-in link",
                "text": (
                    "Click to sign in to the autoagents admin panel:\n\n"
                    f"{link}\n\n"
                    "This link expires in 15 minutes. If you didn't request it, "
                    "ignore this email."
                ),
            },
            timeout=20,
        )
        if r.status_code >= 300:
            log.error("Resend magic-link send failed: %s %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception:  # noqa: BLE001
        log.exception("Resend magic-link send error")
        return False


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------- #
# HTML helpers
# --------------------------------------------------------------------------- #
def esc(v: object) -> str:
    return html.escape(str(v if v is not None else ""))


CSS = """
<style>
 /* JMKN design tokens — shadcn-flavoured surfaces on the brand palette */
 :root{
   --bg:#F5F5F7; --card:rgba(255,255,255,.8); --well:#FFFFFF; --elev:#FFFFFF;
   --gold:#FFC107; --gold-grad:linear-gradient(135deg,#FFE082 0%,#FFC107 40%,#FF8F00 100%);
   --cyan:#0EA5C4; --cyan-soft:#92E2EC; --ring:rgba(14,165,196,.35);
   --border:#D2D2D7; --border-soft:#E5E5EA;
   --text:#1D1D1F; --text2:#515154; --muted:#86868B;
   --shadow:0 1px 2px rgba(0,0,0,.04),0 10px 30px rgba(0,0,0,.04);
   --shadow-h:0 20px 40px rgba(0,0,0,.08),0 0 20px rgba(146,226,236,.10);
   --radius:16px; --radius-sm:9px;
   --green-bg:#E6F8EC;--green-fg:#137333;--orange-bg:#FFF1DD;--orange-fg:#A85A00;
   --red-bg:#FCE9E8;--red-fg:#C0322C;--grey-bg:#ECECEE;--grey-fg:#6B6B70;
 }
 [data-theme=dark]{
   --bg:#09090C; --card:rgba(18,18,26,.7); --well:#12121A; --elev:#16161F;
   --cyan:#92E2EC; --cyan-soft:#92E2EC; --ring:rgba(146,226,236,.40);
   --border:#21222D; --border-soft:#1B1C25;
   --text:#FFFFFF; --text2:#FFF8E7; --muted:#8E8E93;
   --shadow:none; --shadow-h:0 20px 40px rgba(0,0,0,.45),0 0 25px rgba(146,226,236,.18);
   --green-bg:rgba(46,160,67,.16);--green-fg:#56D364;--orange-bg:rgba(255,170,40,.14);--orange-fg:#F0B429;
   --red-bg:rgba(201,51,46,.18);--red-fg:#FF7B72;--grey-bg:rgba(255,255,255,.07);--grey-fg:#9A9AA0;
 }
 *{box-sizing:border-box}
 html{background:var(--bg)}
 body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;margin:0;
   background:var(--bg);color:var(--text);line-height:1.6;font-weight:400;
   -webkit-font-smoothing:antialiased;font-size:15px;
   transition:background .3s ease,color .3s ease}
 .wrap{max-width:1040px;margin:0 auto;padding:2rem 1.25rem 4rem}
 a{color:var(--cyan);text-decoration:none;transition:opacity .15s}
 a:hover{opacity:.7}
 h1{font-size:1.7rem;font-weight:500;letter-spacing:-.02em;margin:.2rem 0 .6rem}
 h2{font-size:1.05rem;font-weight:500;letter-spacing:-.01em;margin:2rem 0 .6rem;
   padding-bottom:.45rem;border-bottom:1px solid var(--border-soft);color:var(--text)}
 p{margin:.5rem 0}
 code{font-family:'SF Mono','Fira Code',monospace;font-size:.85em;
   background:var(--grey-bg);padding:.1rem .4rem;border-radius:6px}
 /* tables */
 table{border-collapse:separate;border-spacing:0;width:100%;font-size:.875rem;margin:.6rem 0;
   background:var(--well);border:1px solid var(--border-soft);border-radius:var(--radius-sm);
   overflow:hidden}
 th,td{text-align:left;padding:.6rem .8rem;border-bottom:1px solid var(--border-soft);vertical-align:top}
 tr:last-child td{border-bottom:0}
 th{color:var(--muted);font-weight:600;font-size:.72rem;letter-spacing:.04em;text-transform:uppercase;
   background:var(--bg)}
 tbody tr{transition:background .12s}
 tbody tr:hover{background:var(--bg)}
 /* form controls */
 input,select,textarea{padding:.5rem .65rem;border:1px solid var(--border);border-radius:var(--radius-sm);
   font-size:.9rem;font-family:inherit;background:var(--elev);color:var(--text);
   transition:border-color .15s,box-shadow .15s}
 input::placeholder,textarea::placeholder{color:var(--muted)}
 input:focus,select:focus,textarea:focus{outline:none;border-color:var(--cyan);box-shadow:0 0 0 3px var(--ring)}
 button{padding:.45rem .85rem;border:1px solid var(--border);border-radius:var(--radius-sm);
   background:var(--elev);color:var(--text);cursor:pointer;font-size:.85rem;font-weight:500;
   font-family:inherit;transition:background .15s,border-color .15s,transform .05s,box-shadow .15s}
 button:hover{background:var(--bg);border-color:var(--muted)}
 button:active{transform:translateY(.5px)}
 button:disabled{opacity:.55;cursor:default;background:var(--cyan-soft);border-color:transparent;
   color:#0b3b44}
 button.primary{background:var(--gold-grad);border:none;color:#3a2c00;font-weight:600;
   box-shadow:0 2px 8px rgba(255,143,0,.25)}
 button.primary:hover{filter:brightness(1.03);box-shadow:0 4px 14px rgba(255,143,0,.32)}
 form.inline{display:inline}
 /* badges */
 .badge{display:inline-block;padding:.18rem .6rem;border-radius:999px;font-size:.72rem;
   font-weight:600;letter-spacing:.02em;line-height:1.4;border:1px solid transparent}
 .b-green{background:var(--green-bg);color:var(--green-fg)}
 .b-orange{background:var(--orange-bg);color:var(--orange-fg)}
 .b-red{background:var(--red-bg);color:var(--red-fg)}
 .b-grey{background:var(--grey-bg);color:var(--grey-fg)}
 /* layout helpers */
 .bar{display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap}
 .muted{color:var(--muted);font-size:.85rem}
 .card{background:var(--card);border:1px solid var(--border-soft);border-radius:var(--radius);
   padding:1.25rem;margin:.8rem 0;box-shadow:var(--shadow);
   backdrop-filter:saturate(1.1) blur(8px);-webkit-backdrop-filter:saturate(1.1) blur(8px)}
 /* theme toggle */
 .theme-toggle{position:fixed;top:1rem;right:1rem;z-index:50;width:38px;height:38px;padding:0;
   border-radius:999px;font-size:1.05rem;line-height:1;display:grid;place-items:center;
   background:var(--card);border:1px solid var(--border);box-shadow:var(--shadow);
   backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
 @media(max-width:600px){.wrap{padding:1.25rem .9rem 3rem}h1{font-size:1.4rem}
   table{font-size:.8rem}th,td{padding:.45rem .55rem}}
</style>
"""

# Set the saved theme before first paint (no flash); toggle persists in localStorage.
_THEME_HEAD = (
    "<script>(function(){try{var t=localStorage.getItem('jmkn-theme')||'light';"
    "document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>"
)
_THEME_BTN = (
    "<button class=theme-toggle aria-label='Toggle light/dark' "
    "onclick=\"(function(){var d=document.documentElement,"
    "n=d.getAttribute('data-theme')==='dark'?'light':'dark';"
    "d.setAttribute('data-theme',n);try{localStorage.setItem('jmkn-theme',n)}catch(e){}})()\">"
    "&#9681;</button>"
)
_FONT = (
    "<link rel=preconnect href='https://fonts.googleapis.com'>"
    "<link rel=preconnect href='https://fonts.gstatic.com' crossorigin>"
    "<link rel=stylesheet href='https://fonts.googleapis.com/css2?"
    "family=Inter:wght@300;400;500;600&display=swap'>"
)


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang=en data-theme=light><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title>{_FONT}{_THEME_HEAD}{CSS}</head><body>"
        f"{_THEME_BTN}<main class=wrap>{body}</main></body></html>"
    )


def lifecycle_badge(status: str) -> str:
    cls = {"active": "b-green", "pending": "b-orange", "disabled": "b-grey"}.get(status, "b-grey")
    return f"<span class='badge {cls}'>{esc(status or 'unknown')}</span>"


def run_badge(state: str) -> str:
    cls = {"running": "b-green", "paused": "b-orange", "stopped": "b-red"}.get(state, "b-grey")
    return f"<span class='badge {cls}'>{esc(state)}</span>"


def fmt_n(n: object) -> str:
    return f"{int(n or 0):,}"


def sev_badge(sev: str) -> str:
    cls = {"error": "b-red", "warning": "b-orange"}.get(sev, "b-grey")
    return f"<span class='badge {cls}'>{esc(sev)}</span>"


def render_alerts(alerts: list, back: str) -> str:
    """An alerts table with a dismiss button per row. Empty string if none."""
    if not alerts:
        return ""
    rows = ""
    for a in alerts:
        rows += (
            f"<tr><td class=muted>{esc(a.get('ts','')[:19])}</td>"
            f"<td>{sev_badge(a.get('severity',''))}</td>"
            f"<td><b>{esc(a.get('kind',''))}</b> "
            f"<span class=muted>[{esc(a.get('tenant_id','') or '—')}]</span><br>"
            f"<span class=muted>{esc(a.get('detail',''))}</span></td>"
            f"<td><form class=inline method=post action='/alerts/{esc(a.get('id',''))}/resolve'>"
            f"<input type=hidden name=back value='{esc(back)}'>"
            f"<button type=submit>dismiss</button></form></td></tr>"
        )
    return (
        "<h2>⚠ Alerts</h2><table>"
        "<tr><th>Time<th>Sev<th>Issue<th></tr>" + rows + "</table>"
    )


def usage_cost(agg: dict[str, int]) -> float | None:
    """Estimated USD cost, or None when no rates are configured (tokens-only)."""
    if not (config.LLM_INPUT_COST_PER_1M or config.LLM_OUTPUT_COST_PER_1M):
        return None
    return (agg["prompt"] / 1e6) * config.LLM_INPUT_COST_PER_1M + (
        (agg["output"] + agg["thoughts"]) / 1e6
    ) * config.LLM_OUTPUT_COST_PER_1M


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, err: str = "", sent: str = "", bad: str = "") -> HTMLResponse:
    if _authed(request):
        return _redirect("/")
    flash = ""
    if sent:
        flash = ("<p class='badge b-green'>If that address is authorised, a sign-in "
                 "link is on its way. Check your inbox.</p>")
    elif bad:
        flash = "<p class='badge b-red'>That sign-in link is invalid or has expired.</p>"
    elif err:
        flash = "<p class='badge b-red'>Wrong password.</p>"
    # Break-glass password form only shows if a password is actually configured.
    breakglass = (
        "<details style='margin-top:1.5rem'><summary class=muted "
        "style='cursor:pointer'>Emergency password sign-in</summary>"
        "<form method=post action='/login/password' style='margin-top:.6rem'>"
        "<input type=password name=password placeholder='Break-glass password'> "
        "<button type=submit>Sign in</button></form></details>"
        if config.ADMIN_PASSWORD
        else ""
    )
    return page(
        "Sign in",
        "<h1>autoagents admin</h1>"
        f"{flash}"
        "<div class=card style='max-width:420px'>"
        "<p>Sign in with an email magic link.</p>"
        "<form method=post action='/login'>"
        "<input type=email name=email placeholder='you@example.com' autofocus required "
        "style='min-width:240px'> "
        "<button class=primary type=submit>Email me a link</button></form>"
        f"{breakglass}"
        "</div>",
    )


@app.post("/login")
async def login_magic(request: Request, email: str = Form("")) -> Response:
    """Email a one-time sign-in link, but only to the single authorised address.
    Always responds the same way so the allowlist isn't probeable."""
    if tenancy.normalize_email(email) == config.ADMIN_EMAIL:
        _send_magic_email(config.ADMIN_EMAIL, _magic_link(config.ADMIN_EMAIL))
    return _redirect("/login?sent=1")


@app.get("/auth")
def auth(token: str = "") -> Response:
    """Redeem a magic link: valid + authorised email → start a session."""
    try:
        email = _signer("aa-magic").loads(token, max_age=config.MAGIC_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return _redirect("/login?bad=1")
    if tenancy.normalize_email(email) != config.ADMIN_EMAIL:
        return _redirect("/login?bad=1")
    return _set_session(_redirect("/"))


@app.post("/login/password")
async def login_password(request: Request, password: str = Form("")) -> Response:
    # compare_digest: a plain == leaks the password prefix-wise via response timing.
    if config.ADMIN_PASSWORD and hmac.compare_digest(password, config.ADMIN_PASSWORD):
        return _set_session(_redirect("/"))
    log.warning("break-glass password login failed")
    return _redirect("/login?err=1")


@app.get("/logout")
def logout() -> Response:
    resp = _redirect("/login")
    resp.delete_cookie(config.COOKIE_NAME)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    if not _authed(request):
        return _redirect("/login")
    by_usage, grand = tenancy.all_usage()
    rows = ""
    for t in tenancy.list_tenants():
        tid = t.get("id", "")
        emails = ", ".join(t.get("emails", []) or []) or "—"
        phones = ", ".join(t.get("phones", []) or []) or "—"
        u = by_usage.get(tid, {"turns": 0, "total": 0})
        rows += (
            f"<tr><td><a href='/t/{esc(tid)}'>{esc(tid)}</a></td>"
            f"<td>{esc(t.get('name',''))}</td>"
            f"<td>{lifecycle_badge(t.get('status',''))}</td>"
            f"<td>{run_badge(tenancy.get_run_state(tid))}</td>"
            f"<td>{fmt_n(u['total'])}</td>"
            f"<td>{fmt_n(u['turns'])}</td>"
            f"<td class=muted>{esc(emails)}</td>"
            f"<td class=muted>{esc(phones)}</td></tr>"
        )
    alerts = tenancy.open_alerts()
    alerts_block = (
        f"<div class=card>{render_alerts(alerts, '/')}</div>" if alerts else ""
    )
    body = (
        "<div class=bar><h1>Tenants</h1><a href='/logout'>Sign out</a></div>"
        f"{alerts_block}"
        f"<p class=muted>All tenants: <b>{fmt_n(grand['total'])}</b> tokens over "
        f"<b>{fmt_n(grand['turns'])}</b> agent turns.</p>"
        "<table><tr><th>ID<th>Name<th>Status<th>Agent<th>Tokens<th>Turns<th>Emails<th>Phones</tr>"
        f"{rows}</table>"
        "<h2>New tenant</h2>"
        "<form method=post action='/tenants' class=card>"
        "<p><input name=tid placeholder='tenant id (e.g. tenant_1)' required> "
        "<input name=name placeholder='Name'></p>"
        "<p><input name=emails placeholder='emails, comma-separated' size=40></p>"
        "<p><input name=phones placeholder='phones, comma-separated' size=40></p>"
        "<button class=primary type=submit>Create (pending)</button>"
        "<p class=muted>The tenant onboards when it first messages from an assigned "
        "email/phone; it then flips to active automatically.</p>"
        "</form>"
    )
    return page("Tenants", body)


@app.post("/tenants")
async def create_tenant(
    request: Request,
    tid: str = Form(...),
    name: str = Form(""),
    emails: str = Form(""),
    phones: str = Form(""),
) -> Response:
    if not _authed(request):
        return _redirect("/login")
    tid = tid.strip()
    if tid:
        tenancy.create_tenant(tid, name.strip() or tid, status="pending")
        for e in [x.strip() for x in emails.split(",") if x.strip()]:
            tenancy.add_identity(tid, "email", e)
        for p in [x.strip() for x in phones.split(",") if x.strip()]:
            tenancy.add_identity(tid, "whatsapp", p)
    return _redirect(f"/t/{tid}")


@app.get("/t/{tid}", response_class=HTMLResponse)
def tenant_detail(request: Request, tid: str, wa: str = "", ctx: str = "") -> Response:
    if not _authed(request):
        return _redirect("/login")
    t = tenancy.get_tenant(tid)
    if not t:
        return page("Not found", f"<p>No tenant <code>{esc(tid)}</code>.</p><a href='/'>Back</a>")
    run = tenancy.get_run_state(tid)

    wa_linked = bool(t.get("wa_linked")) and bool(t.get("wa_number"))
    wa_status = (
        f"<span class='badge b-green'>linked</span> <span class=muted>{esc(t.get('wa_number',''))}</span>"
        if wa_linked
        else "<span class='badge b-grey'>not linked</span>"
    )
    wa_flash = {
        "sent": "<span class='badge b-green'>link emailed ✓</span>",
        "noemail": "<span class='badge b-orange'>tenant has no email on file</span>",
        "err": "<span class='badge b-red'>send failed</span>",
    }.get(wa, "")
    wa_html = (
        "<h2>WhatsApp</h2>"
        f"<p>Status: {wa_status} &nbsp; {wa_flash}</p>"
        f"<form class=inline method=post action='/t/{esc(tid)}/wa-link'>"
        "<button class=primary type=submit>Send WhatsApp link</button></form>"
        " <span class=muted>— emails the tenant a private QR-pairing link to "
        "link/change their number.</span>"
    )

    ctx_flash = "<span class='badge b-green'>saved ✓</span>" if ctx == "saved" else ""
    ctx_html = (
        "<h2>Agent context</h2>"
        "<p class=muted>Standing instructions prepended to every agent turn for this "
        "tenant — tone, who's who, facts, do/don'ts. Takes effect on the next message.</p>"
        f"<form method=post action='/t/{esc(tid)}/agent-context'>"
        "<textarea name=context rows=6 "
        "style='width:100%;box-sizing:border-box;font:inherit;padding:.5rem;"
        "border:1px solid #d0d7de;border-radius:6px;resize:vertical'>"
        f"{esc(t.get('agent_context', '') or '')}</textarea>"
        f"<p><button class=primary type=submit>Save context</button> &nbsp; {ctx_flash}</p></form>"
    )

    usage = tenancy.tenant_usage(tid)
    counts = tenancy.tenant_counts(tid)
    cost = usage_cost(usage)
    if cost is not None:
        cost_row = (
            f"<tr><th>Est. cost</th><td>${cost:,.4f} "
            f"<span class=muted>(in ${config.LLM_INPUT_COST_PER_1M}/1M, "
            f"out ${config.LLM_OUTPUT_COST_PER_1M}/1M)</span></td></tr>"
        )
    else:
        cost_row = (
            "<tr><th>Est. cost</th><td class=muted>set LLM_INPUT_COST_PER_1M / "
            "LLM_OUTPUT_COST_PER_1M to show $</td></tr>"
        )
    analytics_html = (
        "<h2>Analytics</h2><table>"
        f"<tr><th>Agent turns</th><td>{fmt_n(usage['turns'])}</td></tr>"
        f"<tr><th>Input tokens</th><td>{fmt_n(usage['prompt'])}</td></tr>"
        f"<tr><th>Output tokens</th><td>{fmt_n(usage['output'] + usage['thoughts'])} "
        f"<span class=muted>(incl. {fmt_n(usage['thoughts'])} thinking)</span></td></tr>"
        f"<tr><th>Total tokens</th><td>{fmt_n(usage['total'])}</td></tr>"
        f"{cost_row}"
        f"<tr><th>Messages logged</th><td>{fmt_n(counts['messages'])}</td></tr>"
        f"<tr><th>Tasks</th><td>{fmt_n(counts['tasks'])}</td></tr>"
        "</table>"
    )

    def id_rows(items: list[str], channel: str) -> str:
        out = ""
        for v in items or []:
            out += (
                f"<tr><td>{esc(v)}</td><td>"
                f"<form class=inline method=post action='/t/{esc(tid)}/identity'>"
                f"<input type=hidden name=action value=remove>"
                f"<input type=hidden name=channel value={channel}>"
                f"<input type=hidden name=value value='{esc(v)}'>"
                f"<button type=submit>remove</button></form></td></tr>"
            )
        return out or "<tr><td class=muted colspan=2>none</td></tr>"

    msg_rows = ""
    for m in tenancy.recent_messages(tid):
        msg_rows += (
            f"<tr><td class=muted>{esc(m.get('ts','')[:19])}</td>"
            f"<td>{esc(m.get('channel',''))}</td><td>{esc(m.get('direction',''))}</td>"
            f"<td>{esc(m.get('status',''))}</td>"
            f"<td class=muted>{esc((m.get('from','') or '')[:28])} → {esc((m.get('to','') or '')[:28])}</td></tr>"
        )
    task_rows = ""
    for tk in tenancy.recent_tasks(tid):
        task_rows += (
            f"<tr><td class=muted>{esc(tk.get('due_at','')[:19])}</td>"
            f"<td>{esc(tk.get('status',''))}</td>"
            f"<td>{esc((tk.get('description','') or '')[:80])}</td></tr>"
        )

    def state_btn(s: str) -> str:
        cur = " disabled" if s == run else ""
        return (
            f"<form class=inline method=post action='/t/{esc(tid)}/runstate'>"
            f"<input type=hidden name=status value={s}>"
            f"<button type=submit{cur}>{s}</button></form> "
        )

    def life_btn(s: str) -> str:
        cur = " disabled" if s == t.get("status") else ""
        return (
            f"<form class=inline method=post action='/t/{esc(tid)}/lifecycle'>"
            f"<input type=hidden name=status value={s}>"
            f"<button type=submit{cur}>{s}</button></form> "
        )

    body = (
        f"<div class=bar><h1>{esc(tid)} — {esc(t.get('name',''))}</h1><a href='/'>← all tenants</a></div>"
        f"<p>Lifecycle: {lifecycle_badge(t.get('status',''))} &nbsp; Agent: {run_badge(run)}</p>"
        f"<p class=muted>RAG corpus: {esc(t.get('rag_corpus','') or '— none assigned —')}</p>"
        f"{render_alerts(tenancy.tenant_alerts(tid), '/t/' + tid)}"
        f"{wa_html}"
        f"{ctx_html}"
        f"{analytics_html}"
        f"<h2>Agent run-state</h2><p>{state_btn('running')}{state_btn('paused')}{state_btn('stopped')}</p>"
        f"<h2>Lifecycle status</h2><p>{life_btn('pending')}{life_btn('active')}{life_btn('disabled')}</p>"
        f"<h2>Email identities</h2><table>{id_rows(t.get('emails',[]),'email')}</table>"
        f"<form method=post action='/t/{esc(tid)}/identity'>"
        f"<input type=hidden name=action value=add><input type=hidden name=channel value=email>"
        f"<input name=value placeholder='new email'> <button type=submit>add email</button></form>"
        f"<h2>Phone identities</h2><table>{id_rows(t.get('phones',[]),'whatsapp')}</table>"
        f"<form method=post action='/t/{esc(tid)}/identity'>"
        f"<input type=hidden name=action value=add><input type=hidden name=channel value=whatsapp>"
        f"<input name=value placeholder='new phone (digits)'> <button type=submit>add phone</button></form>"
        f"<h2>Recent messages</h2><table><tr><th>Time<th>Chan<th>Dir<th>Status<th>From → To</tr>{msg_rows or '<tr><td class=muted colspan=5>none</td></tr>'}</table>"
        f"<h2>Tasks</h2><table><tr><th>Due<th>Status<th>Description</tr>{task_rows or '<tr><td class=muted colspan=3>none</td></tr>'}</table>"
    )
    return page(f"{tid} — admin", body)


@app.post("/t/{tid}/identity")
async def edit_identity(
    request: Request,
    tid: str,
    action: str = Form(...),
    channel: str = Form(...),
    value: str = Form(...),
) -> Response:
    if not _authed(request):
        return _redirect("/login")
    if value.strip() and channel in ("email", "whatsapp"):
        if action == "add":
            tenancy.add_identity(tid, channel, value.strip())
        elif action == "remove":
            tenancy.remove_identity(tid, channel, value.strip())
    return _redirect(f"/t/{tid}")


@app.post("/t/{tid}/runstate")
async def edit_runstate(request: Request, tid: str, status: str = Form(...)) -> Response:
    if not _authed(request):
        return _redirect("/login")
    if status in ("running", "paused", "stopped"):
        tenancy.set_run_state(tid, status)
    return _redirect(f"/t/{tid}")


@app.post("/t/{tid}/lifecycle")
async def edit_lifecycle(request: Request, tid: str, status: str = Form(...)) -> Response:
    if not _authed(request):
        return _redirect("/login")
    if status in ("pending", "active", "disabled"):
        tenancy.set_tenant_status(tid, status)
    return _redirect(f"/t/{tid}")


@app.post("/t/{tid}/agent-context")
async def edit_agent_context(
    request: Request, tid: str, context: str = Form("")
) -> Response:
    if not _authed(request):
        return _redirect("/login")
    if tenancy.get_tenant(tid):
        tenancy.set_agent_context(tid, context.strip())
    return _redirect(f"/t/{tid}?ctx=saved")


@app.post("/t/{tid}/wa-link")
async def send_wa_link(request: Request, tid: str) -> Response:
    if not _authed(request):
        return _redirect("/login")
    flash = "err"
    if tenancy.get_tenant(tid):
        try:
            r = requests.post(
                config.GATEWAY_URL.rstrip("/") + f"/internal/wa-link/{tid}",
                headers={"X-Tasks-Token": config.TASKS_TOKEN},
                timeout=20,
            )
            data = r.json() if r.content else {}
            if data.get("emailed"):
                flash = "sent"
            elif not data.get("to"):
                flash = "noemail"
        except Exception:  # noqa: BLE001
            flash = "err"
    return _redirect(f"/t/{tid}?wa={flash}")


@app.post("/alerts/{aid}/resolve")
async def resolve_alert_route(
    request: Request, aid: str, back: str = Form("/")
) -> Response:
    if not _authed(request):
        return _redirect("/login")
    tenancy.resolve_alert(aid)
    return _redirect(back if back.startswith("/") else "/")
