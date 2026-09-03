"""Authentication utilities for FitAcademie Squash Webapp.

Uses bcrypt directly (passlib has compatibility issues with bcrypt >= 5).
Provides JWT token management and Fernet encryption for portal passwords.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import JWTError, jwt
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


# ── Password hashing (bcrypt direct) ──────────────────────────────

def hash_password(password: str) -> str:
    """Hash a password with bcrypt. Returns the hash string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash. Returns True if match."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ── JWT tokens ────────────────────────────────────────────────────

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30
REMEMBER_TOKEN_EXPIRE_DAYS = 365


def create_access_token(user_id: int, email: str, expires_delta: Optional[timedelta] = None) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": datetime.now(timezone.utc),
    }
    if expires_delta:
        payload["exp"] = datetime.now(timezone.utc) + expires_delta
    else:
        payload["exp"] = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Decode JWT. Returns payload dict or None if invalid."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


def get_user_id_from_token(token: str) -> Optional[int]:
    payload = decode_token(token)
    if payload:
        return int(payload["sub"])
    return None


# ── Fernet encryption for FitAcademie passwords ───────────────────

def get_fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_portal_password(password: str) -> str:
    """Encrypt FitAcademie portal password using Fernet."""
    return get_fernet().encrypt(password.encode()).decode()


def decrypt_portal_password(encrypted: str) -> str:
    """Decrypt FitAcademie portal password using Fernet."""
    return get_fernet().decrypt(encrypted.encode()).decode()


# ── Playwright cookie session helpers ─────────────────────────────

def serialize_cookies(cookies: list[dict]) -> str:
    return json.dumps(cookies)


def deserialize_cookies(data: str) -> list[dict]:
    return json.loads(data)


def cookies_are_fresh(cookies_str: Optional[str], max_age_hours: int = 24) -> bool:
    """Check if stored cookies are still likely valid."""
    if not cookies_str:
        return False
    try:
        cookies = json.loads(cookies_str)
        now = datetime.now().timestamp()
        for c in cookies:
            if c.get("name") in ("_session_id", "session", "remember_token"):
                expires = c.get("expires")
                if expires and expires > now:
                    return True
        return False
    except (json.JSONDecodeError, KeyError):
        return False