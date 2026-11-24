"""Token encryption at rest (Fernet = AES-128-CBC + HMAC-SHA256)."""
from cryptography.fernet import Fernet, InvalidToken
from . import config

def _fernet() -> Fernet:
    if not config.ENCRYPTION_KEY:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(config.ENCRYPTION_KEY.encode())

def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()

def decrypt(blob: str) -> str:
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Cannot decrypt token (wrong TOKEN_ENCRYPTION_KEY?)") from e
