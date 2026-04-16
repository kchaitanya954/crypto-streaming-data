"""
Auth helpers: password hashing, JWT tokens, TOTP 2FA.

JWT_SECRET must be set in the environment before import.
"""

import os
import time

import bcrypt
import jwt
import pyotp

JWT_SECRET   = os.environ.get("JWT_SECRET", "change-me-in-production-use-64-char-random-hex")
JWT_ALGO     = "HS256"
JWT_EXPIRY_S = 60 * 60 * 24  # 24 hours


# ── Password ─────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── JWT ──────────────────────────────────────────────────────────────────────

def create_jwt(user_id: int, username: str, is_admin: bool = False) -> str:
    payload = {
        "sub":      str(user_id),
        "username": username,
        "is_admin": is_admin,
        "iat":      int(time.time()),
        "exp":      int(time.time()) + JWT_EXPIRY_S,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_jwt(token: str) -> dict:
    """Raises jwt.ExpiredSignatureError / jwt.InvalidTokenError on failure."""
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])


# ── TOTP ─────────────────────────────────────────────────────────────────────

def generate_totp_secret() -> str:
    return pyotp.random_base32()


def get_totp_uri(secret: str, username: str, issuer: str = "CryptoDash") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    """Accepts codes from ±1 window (30-second window × 3 = 90 s tolerance)."""
    return pyotp.TOTP(secret).verify(code, valid_window=1)
