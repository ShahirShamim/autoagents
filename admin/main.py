"""autoagents admin webapp — Cloud Run service `autoagents-admin`.

Server-rendered (no client JS framework), gated by a single shared password.
Lets an operator create/edit tenants, assign email + phone identities, flip
per-tenant run-state (running/paused/stopped) and lifecycle status, and review
recent messages + tasks. Reads/writes the same Firestore the gateway/agent use.
"""
from __future__ import annotations

import html

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from admin import config, tenancy

app = FastAPI(title="autoagents-admin")


# --------------------------------------------------------------------------- #
# Auth (signed cookie keyed on the shared password)
# --------------------------------------------------------------------------- #
def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.ADMIN_PASSWORD or "unset", salt="aa-admin")


def _authed(request: Request) -> bool:
    tok = request.cookies.get(config.COOKIE_NAME)
    if not tok or not config.ADMIN_PASSWORD:
        return False
    try:
        _serializer().loads(tok, max_age=config.SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
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
 body{font-family:system-ui,-apple-system,sans-serif;max-width:1000px;margin:1.5rem auto;
   padding:0 1rem;color:#1f2328;line-height:1.5}
 a{color:#0969da;text-decoration:none}a:hover{text-decoration:underline}
 h1{font-size:1.4rem}h2{font-size:1.05rem;margin-top:1.6rem;border-bottom:1px solid #eaecef;padding-bottom:.3rem}
 table{border-collapse:collapse;width:100%;font-size:.9rem;margin:.5rem 0}
 th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #eaecef;vertical-align:top}
 th{color:#57606a;font-weight:600}
 input,select{padding:.35rem .5rem;border:1px solid #d0d7de;border-radius:6px;font-size:.9rem}
 button{padding:.35rem .7rem;border:1px solid #d0d7de;border-radius:6px;background:#f6f8fa;
   cursor:pointer;font-size:.85rem}button:hover{background:#eef1f4}
 form.inline{display:inline}
 .badge{display:inline-block;padding:.1rem .5rem;border-radius:999px;font-size:.75rem;font-weight:600}
 .b-green{background:#dafbe1;color:#1a7f37}.b-orange{background:#fff1e5;color:#bc4c00}
 .b-red{background:#ffebe9;color:#cf222e}.b-grey{background:#eaecef;color:#57606a}
 .bar{display:flex;justify-content:space-between;align-items:center}
 .muted{color:#57606a;font-size:.85rem}.card{border:1px solid #eaecef;border-radius:8px;padding:1rem;margin:.6rem 0}
</style>
"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title>{CSS}</head><body>{body}</body></html>"
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
def login_form(request: Request, err: str = "") -> HTMLResponse:
    if _authed(request):
        return _redirect("/")
    msg = "<p class='b-red badge'>Wrong password</p>" if err else ""
    return page(
        "Login",
        f"<h1>autoagents admin</h1>{msg}"
        "<form method=post action='/login'>"
        "<input type=password name=password placeholder='Admin password' autofocus> "
        "<button type=submit>Sign in</button></form>",
    )


@app.post("/login")
async def login(request: Request, password: str = Form("")) -> Response:
    if config.ADMIN_PASSWORD and password == config.ADMIN_PASSWORD:
        resp = _redirect("/")
        resp.set_cookie(
            config.COOKIE_NAME,
            _serializer().dumps("ok"),
            max_age=config.SESSION_MAX_AGE,
            httponly=True,
            secure=config.COOKIE_SECURE,
            samesite="lax",
        )
        return resp
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
        "<button type=submit>Create (pending)</button>"
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
def tenant_detail(request: Request, tid: str) -> Response:
    if not _authed(request):
        return _redirect("/login")
    t = tenancy.get_tenant(tid)
    if not t:
        return page("Not found", f"<p>No tenant <code>{esc(tid)}</code>.</p><a href='/'>Back</a>")
    run = tenancy.get_run_state(tid)

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


@app.post("/alerts/{aid}/resolve")
async def resolve_alert_route(
    request: Request, aid: str, back: str = Form("/")
) -> Response:
    if not _authed(request):
        return _redirect("/login")
    tenancy.resolve_alert(aid)
    return _redirect(back if back.startswith("/") else "/")
