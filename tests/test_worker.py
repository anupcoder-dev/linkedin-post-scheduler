import os, tempfile
from cryptography.fernet import Fernet
os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/t.db"
os.environ["RETRY_BASE_SECONDS"] = "1"
from datetime import timedelta
import pytest
from app import linkedin as li, worker
from app.db import Session, User, ScheduledPost, init_db, now
from app.security import encrypt

init_db()

def make_post(txt="hello"):
    with Session() as s:
        u = User(linkedin_sub=txt, person_urn="urn:li:person:x", access_token_enc=encrypt("tok"),
                 token_expires_at=now() + timedelta(days=30))
        s.add(u); s.flush()
        p = ScheduledPost(user_id=u.id, content=txt, content_hash=txt, scheduled_at=now(), next_attempt_at=now())
        s.add(p); s.commit(); return p.id

def get(pid):
    with Session() as s: return s.get(ScheduledPost, pid)

def force_due(pid):
    with Session() as s:
        s.get(ScheduledPost, pid).next_attempt_at = now(); s.commit()

def test_success(monkeypatch):
    monkeypatch.setattr(li, "publish", lambda *a: "urn:li:share:1")
    pid = make_post("ok"); worker.process(pid)
    assert get(pid).status == "published" and get(pid).linkedin_post_urn == "urn:li:share:1"

def test_retry_then_success(monkeypatch):
    calls = iter([li.RetryableError("429"), "urn:li:share:2"])
    def fake(*a):
        v = next(calls)
        if isinstance(v, Exception): raise v
        return v
    monkeypatch.setattr(li, "publish", fake)
    pid = make_post("retry"); worker.process(pid)
    p = get(pid); assert p.status == "scheduled" and p.attempts == 1 and p.next_attempt_at > now()
    force_due(pid); worker.process(pid)
    assert get(pid).status == "published"

def test_ambiguous_reconciles_instead_of_duplicate(monkeypatch):
    sent = []
    def fake(*a): sent.append(1); raise li.AmbiguousError("timeout")
    monkeypatch.setattr(li, "publish", fake)
    monkeypatch.setattr(li, "find_existing", lambda *a: "urn:li:share:99")  # it actually went through
    pid = make_post("amb"); worker.process(pid); force_due(pid); worker.process(pid)
    assert len(sent) == 1 and get(pid).status == "published" and get(pid).linkedin_post_urn == "urn:li:share:99"

def test_ambiguous_unverifiable_fails_safe(monkeypatch):
    monkeypatch.setattr(li, "publish", lambda *a: (_ for _ in ()).throw(li.AmbiguousError("timeout")))
    monkeypatch.setattr(li, "find_existing", lambda *a: None)
    pid = make_post("unv"); worker.process(pid); force_due(pid); worker.process(pid)
    assert get(pid).status == "failed"

def test_double_claim_is_noop(monkeypatch):
    n = []; monkeypatch.setattr(li, "publish", lambda *a: n.append(1) or "urn:x")
    pid = make_post("dbl"); worker.process(pid); worker.process(pid)
    assert len(n) == 1

def test_permanent_fails(monkeypatch):
    monkeypatch.setattr(li, "publish", lambda *a: (_ for _ in ()).throw(li.PermanentError("401")))
    pid = make_post("perm"); worker.process(pid); assert get(pid).status == "failed"

def test_backoff_increases():
    assert worker.backoff(4).total_seconds() > worker.backoff(1).total_seconds()

def test_escape():
    assert li.escape_commentary("a (b) #c") == r"a \(b\) \#c"
