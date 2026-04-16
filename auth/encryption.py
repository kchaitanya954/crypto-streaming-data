"""
Fernet symmetric encryption for API keys stored in SQLite.

FERNET_KEY must be set in the environment — generate once with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
and store in .env.  Changing this key makes existing ciphertext unreadable.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        raw = os.environ.get("FERNET_KEY", "")
        if not raw:
            # Dev fallback: generate a temporary key (data is lost on restart)
            raw = Fernet.generate_key().decode()
        _fernet = Fernet(raw.encode() if isinstance(raw, str) else raw)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string; returns URL-safe base64 ciphertext."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt ciphertext produced by encrypt(). Raises InvalidToken on failure."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def safe_decrypt(ciphertext: str | None) -> str | None:
    """Returns None if ciphertext is None or decryption fails."""
    if not ciphertext:
        return None
    try:
        return decrypt(ciphertext)
    except (InvalidToken, Exception):
        return None
