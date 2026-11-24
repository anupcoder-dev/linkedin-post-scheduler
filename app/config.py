import logging, os, sys
from dotenv import load_dotenv

load_dotenv()

def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing required env var {name}")
    return v

CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("LINKEDIN_REDIRECT_URI", "http://localhost:8000/auth/linkedin/callback")
API_VERSION = os.environ.get("LINKEDIN_API_VERSION", "202504")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", 6))
RETRY_BASE = int(os.environ.get("RETRY_BASE_SECONDS", 30))
RETRY_MAX = int(os.environ.get("RETRY_MAX_SECONDS", 3600))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", 5))
LOCK_TIMEOUT = int(os.environ.get("LOCK_TIMEOUT_SECONDS", 300))
ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")

logging.basicConfig(
    stream=sys.stdout, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
