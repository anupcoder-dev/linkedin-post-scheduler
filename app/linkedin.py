"""LinkedIn OAuth + Posts API client with error classification.

Error classes drive the duplicate-safe retry logic:
  RetryableError  - request definitely NOT processed (429, 503, connect failure)  -> retry
  AmbiguousError  - request MAY have been processed (timeout, 500/502/504)        -> reconcile, then retry
  PermanentError  - will never succeed (401/403/422/...)                          -> fail
"""
import logging, re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import httpx
from . import config

log = logging.getLogger("linkedin")
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
SCOPES = "openid profile w_member_social"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

class LinkedInError(Exception): pass
class PermanentError(LinkedInError): pass
class RetryableError(LinkedInError):
    def __init__(self, msg, retry_after: float | None = None):
        super().__init__(msg); self.retry_after = retry_after
class AmbiguousError(RetryableError): pass

# ---------- OAuth ----------
def authorize_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": config.CLIENT_ID,
        "redirect_uri": config.REDIRECT_URI, "state": state, "scope": SCOPES})

def exchange_code(code: str) -> dict:
    r = httpx.post(TOKEN_URL, timeout=TIMEOUT, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": config.REDIRECT_URI,
        "client_id": config.CLIENT_ID, "client_secret": config.CLIENT_SECRET})
    if r.status_code != 200:
        raise PermanentError(f"Token exchange failed: {r.status_code} {r.text[:200]}")
    return r.json()

def fetch_profile(access_token: str) -> dict:
    r = httpx.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise PermanentError(f"Userinfo failed: {r.status_code}")
    return r.json()

# ---------- Posts ----------
_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")
def escape_commentary(text: str) -> str:
    """LinkedIn 'little text' format treats these chars as markup."""
    return _RESERVED.sub(r"\\\1", text)

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Linkedin-Version": config.API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0", "Content-Type": "application/json"}

def publish(token: str, author_urn: str, text: str) -> str:
    body = {
        "author": author_urn, "commentary": escape_commentary(text), "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    try:
        r = httpx.post(POSTS_URL, json=body, headers=_headers(token), timeout=TIMEOUT)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise RetryableError(f"connect failure: {e!r}")            # never reached LinkedIn
    except httpx.HTTPError as e:
        raise AmbiguousError(f"network error after send: {e!r}")   # may have been processed

    if r.status_code == 201:
        urn = r.headers.get("x-restli-id")
        if not urn:
            raise AmbiguousError("201 without x-restli-id")
        return urn
    retry_after = float(r.headers["Retry-After"]) if r.headers.get("Retry-After", "").isdigit() else None
    msg = f"HTTP {r.status_code}: {r.text[:300]}"
    if r.status_code in (429, 503):
        raise RetryableError(msg, retry_after)
    if r.status_code in (500, 502, 504):
        raise AmbiguousError(msg, retry_after)
    if r.status_code == 401:
        raise PermanentError("Access token invalid/expired - user must reconnect LinkedIn. " + msg)
    raise PermanentError(msg)  # 400/403/422 (incl. LinkedIn's own duplicate-content rejection)

def find_existing(token: str, author_urn: str, text: str, since: datetime) -> str | None:
    """Reconciliation after an ambiguous failure.
    Returns post URN if found, "" if verified absent, None if it cannot be verified
    (needs r_member_social scope, which LinkedIn restricts)."""
    try:
        r = httpx.get(POSTS_URL, params={"author": author_urn, "q": "author", "count": 10, "sortBy": "LAST_MODIFIED"},
                      headers=_headers(token), timeout=TIMEOUT)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    norm = lambda s: s.replace("\\", "").strip()
    cutoff = (since.replace(tzinfo=timezone.utc) - timedelta(minutes=2)).timestamp() * 1000
    for el in r.json().get("elements", []):
        if norm(el.get("commentary", "")) == norm(text) and el.get("createdAt", 0) >= cutoff:
            return el["id"]
    return ""
