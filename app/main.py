import hashlib, logging, secrets
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware
from . import config, linkedin as li
from .db import Session, User, ScheduledPost, PostEvent, init_db, now
from .security import encrypt

log = logging.getLogger("web")
app = FastAPI(title="LinkedIn Post Scheduler")
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET or secrets.token_hex(16),
                   same_site="lax", https_only=config.REDIRECT_URI.startswith("https"))

@app.on_event("startup")
def _startup(): init_db()

def current_user(req: Request, s) -> User:
    uid = req.session.get("uid")
    u = s.get(User, uid) if uid else None
    if not u: raise HTTPException(401, "Connect LinkedIn first")
    return u

# ---- OAuth ----
@app.get("/auth/linkedin/login")
def login(req: Request):
    state = secrets.token_urlsafe(24); req.session["state"] = state
    return RedirectResponse(li.authorize_url(state))

@app.get("/auth/linkedin/callback")
def callback(req: Request, code: str = "", state: str = "", error: str = ""):
    if error: raise HTTPException(400, f"LinkedIn denied access: {error}")
    if not state or state != req.session.pop("state", None): raise HTTPException(400, "Invalid OAuth state (CSRF)")
    try:
        tok = li.exchange_code(code)
        prof = li.fetch_profile(tok["access_token"])
    except li.LinkedInError as e:
        log.error("oauth failed: %s", e); raise HTTPException(502, str(e))
    with Session() as s:
        u = s.scalar(select(User).where(User.linkedin_sub == prof["sub"])) or User(linkedin_sub=prof["sub"])
        u.person_urn, u.name = f"urn:li:person:{prof['sub']}", prof.get("name", "")
        u.access_token_enc = encrypt(tok["access_token"])
        u.refresh_token_enc = encrypt(tok["refresh_token"]) if tok.get("refresh_token") else None
        u.token_expires_at = now() + timedelta(seconds=int(tok.get("expires_in", 5184000)) - 60)
        s.add(u); s.commit(); req.session["uid"] = u.id
    log.info("user %s connected", u.id)
    return RedirectResponse("/")

# ---- API ----
class PostIn(BaseModel):
    content: str = Field(min_length=1, max_length=3000)
    scheduled_at: datetime  # ISO-8601 with offset, e.g. 2026-12-01T09:00:00+05:30

def view(p: ScheduledPost):
    return {"id": p.id, "content": p.content, "status": p.status, "scheduled_at": p.scheduled_at.isoformat() + "Z",
            "attempts": p.attempts, "next_attempt_at": p.next_attempt_at.isoformat() + "Z",
            "linkedin_post_urn": p.linkedin_post_urn, "last_error": p.last_error}

@app.post("/api/posts", status_code=201)
def create_post(body: PostIn, req: Request):
    if body.scheduled_at.tzinfo is None: raise HTTPException(422, "scheduled_at must include a timezone offset")
    when = body.scheduled_at.astimezone(timezone.utc).replace(tzinfo=None)
    if when < now() + timedelta(seconds=10): raise HTTPException(422, "scheduled_at must be in the future")
    with Session() as s:
        u = current_user(req, s)
        if u.token_expires_at <= when: raise HTTPException(422, "LinkedIn token expires before that time; reconnect or pick an earlier time")
        p = ScheduledPost(user_id=u.id, content=body.content, scheduled_at=when, next_attempt_at=when,
                          content_hash=hashlib.sha256(body.content.strip().encode()).hexdigest())
        s.add(p)
        try: s.flush()
        except IntegrityError: raise HTTPException(409, "Duplicate: identical post already scheduled for this time")
        s.add(PostEvent(post_id=p.id, level="INFO", message="scheduled")); s.commit()
        return view(p)

@app.get("/api/posts")
def list_posts(req: Request):
    with Session() as s:
        u = current_user(req, s)
        return [view(p) for p in s.scalars(select(ScheduledPost).where(ScheduledPost.user_id == u.id).order_by(ScheduledPost.id.desc()))]

@app.get("/api/posts/{pid}/events")
def events(pid: int, req: Request):
    with Session() as s:
        u = current_user(req, s); p = s.get(ScheduledPost, pid)
        if not p or p.user_id != u.id: raise HTTPException(404)
        return [{"ts": e.ts.isoformat() + "Z", "level": e.level, "message": e.message}
                for e in s.scalars(select(PostEvent).where(PostEvent.post_id == pid).order_by(PostEvent.id))]

@app.delete("/api/posts/{pid}")
def cancel(pid: int, req: Request):
    with Session() as s:
        u = current_user(req, s); p = s.get(ScheduledPost, pid)
        if not p or p.user_id != u.id: raise HTTPException(404)
        if p.status != "scheduled" or p.send_started_at: raise HTTPException(409, "Only untouched scheduled posts can be cancelled")
        s.delete(p); s.commit(); return {"deleted": pid}

@app.get("/health")
def health(): return {"ok": True}

PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>LinkedIn Scheduler</title>
<style>body{font:16px system-ui;max-width:680px;margin:2rem auto;padding:0 1rem}textarea,input,button{font:inherit;width:100%;margin:.3rem 0;padding:.5rem;box-sizing:border-box}
li{margin:.6rem 0;padding:.6rem;border:1px solid #8884;border-radius:6px;list-style:none}.s{font-weight:600}</style>
<h1>LinkedIn Post Scheduler</h1><p><a href=/auth/linkedin/login>Connect LinkedIn</a></p>
<textarea id=c rows=5 maxlength=3000 placeholder="Post content"></textarea>
<input id=t type=datetime-local><button onclick=go()>Schedule</button><p id=m></p><ul id=l style=padding:0></ul>
<script>
const m=document.getElementById('m');
async function go(){const t=document.getElementById('t').value;
 const r=await fetch('/api/posts',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({content:document.getElementById('c').value,scheduled_at:new Date(t).toISOString()})});
 const j=await r.json();m.textContent=r.ok?'Scheduled #'+j.id:(j.detail||'Error');load()}
async function load(){const r=await fetch('/api/posts');if(!r.ok)return;const l=document.getElementById('l');l.replaceChildren();
 for(const p of await r.json()){const li=document.createElement('li');
 li.textContent=p.content.slice(0,120);const s=document.createElement('div');s.className='s';
 s.textContent=p.status+' - '+new Date(p.scheduled_at).toLocaleString()+' - attempts '+p.attempts+(p.last_error?' - '+p.last_error:'');
 li.append(s);l.append(li)}}
load();setInterval(load,5000)
</script>"""

@app.get("/", response_class=HTMLResponse)
def index(): return PAGE
