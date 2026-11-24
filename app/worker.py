"""Background worker: polls DB, claims due posts atomically, publishes with duplicate-safe retries."""
import logging, random, signal, time
from datetime import timedelta
from sqlalchemy import select, update
from . import config, linkedin as li
from .db import Session, ScheduledPost, PostEvent, init_db, now
from .security import decrypt

log = logging.getLogger("worker")
_stop = False

def event(s, post, level, msg):
    getattr(log, level.lower())("post=%s %s", post.id, msg)
    s.add(PostEvent(post_id=post.id, level=level, message=msg[:2000]))

def backoff(attempt: int, retry_after: float | None = None) -> timedelta:
    """Exponential: base*2^(n-1), capped, +-20% jitter; honours Retry-After."""
    d = min(config.RETRY_BASE * 2 ** (attempt - 1), config.RETRY_MAX)
    d = max(d * random.uniform(0.8, 1.2), retry_after or 0)
    return timedelta(seconds=d)

def recover_stale():
    """A worker that died mid-flight leaves 'processing' rows; hand them back (send_started_at kept => reconcile)."""
    with Session() as s:
        cutoff = now() - timedelta(seconds=config.LOCK_TIMEOUT)
        for p in s.scalars(select(ScheduledPost).where(ScheduledPost.status == "processing", ScheduledPost.locked_at < cutoff)):
            event(s, p, "WARNING", "stale lock recovered")
            p.status, p.locked_at, p.next_attempt_at = "scheduled", None, now()
        s.commit()

def fail(s, p, msg):
    p.status, p.last_error, p.locked_at = "failed", msg, None
    event(s, p, "ERROR", f"FAILED: {msg}")

def retry_or_fail(s, p, msg, retry_after=None):
    if p.attempts >= config.MAX_ATTEMPTS:
        return fail(s, p, f"max attempts reached; last error: {msg}")
    delay = backoff(p.attempts, retry_after)
    p.status, p.last_error, p.locked_at = "scheduled", msg, None
    p.next_attempt_at = now() + delay
    event(s, p, "WARNING", f"attempt {p.attempts} failed ({msg}); retry in {int(delay.total_seconds())}s")

def process(post_id: int):
    with Session() as s:
        # Atomic claim: only one worker can flip scheduled -> processing.
        claimed = s.execute(update(ScheduledPost)
            .where(ScheduledPost.id == post_id, ScheduledPost.status == "scheduled", ScheduledPost.next_attempt_at <= now())
            .values(status="processing", locked_at=now(), attempts=ScheduledPost.attempts + 1)).rowcount
        s.commit()
        if not claimed:
            return
        p = s.get(ScheduledPost, post_id)
        event(s, p, "INFO", f"claimed (attempt {p.attempts}/{config.MAX_ATTEMPTS})")
        try:
            u = p.user
            if u.token_expires_at <= now():
                raise li.PermanentError("LinkedIn token expired - user must reconnect")
            token = decrypt(u.access_token_enc)

            if p.send_started_at and not p.linkedin_post_urn:
                # Previous attempt may have reached LinkedIn. Verify before sending again.
                found = li.find_existing(token, u.person_urn, p.content, p.send_started_at)
                if found is None:
                    raise li.PermanentError("Previous attempt outcome unknown and cannot be verified; "
                                            "not re-sending to avoid a duplicate. Check LinkedIn manually.")
                if found:
                    p.status, p.linkedin_post_urn, p.published_at, p.locked_at = "published", found, now(), None
                    event(s, p, "INFO", f"reconciled: already published as {found}")
                    return s.commit()

            p.send_started_at = now()
            s.commit()                                 # persisted BEFORE the network call
            urn = li.publish(token, u.person_urn, p.content)
            p.status, p.linkedin_post_urn, p.published_at, p.locked_at, p.last_error = "published", urn, now(), None, None
            event(s, p, "INFO", f"published {urn}")
        except li.AmbiguousError as e:                 # keep send_started_at -> next attempt reconciles first
            retry_or_fail(s, p, str(e), e.retry_after)
        except li.RetryableError as e:                 # definitely not sent
            p.send_started_at = None
            retry_or_fail(s, p, str(e), e.retry_after)
        except (li.PermanentError, ValueError) as e:
            fail(s, p, str(e))
        except Exception as e:                         # unknown => treat as ambiguous (safe)
            log.exception("unexpected error post=%s", post_id)
            retry_or_fail(s, p, f"unexpected: {e!r}")
        finally:
            s.commit()

def tick():
    recover_stale()
    with Session() as s:
        ids = s.scalars(select(ScheduledPost.id).where(
            ScheduledPost.status == "scheduled", ScheduledPost.next_attempt_at <= now()).limit(50)).all()
    for i in ids:
        process(i)

def main():
    global _stop
    init_db()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: globals().__setitem__("_stop", True))
    log.info("worker started (poll=%ss)", config.POLL_SECONDS)
    while not _stop:
        try:
            tick()
        except Exception:
            log.exception("tick failed")
        time.sleep(config.POLL_SECONDS)
    log.info("worker stopped")

if __name__ == "__main__":
    main()
