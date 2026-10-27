"""Password check and login tokens. Standard library only."""
import hashlib
import hmac
import re
import secrets

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
FIRST_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z '\-]{0,49}$")


def valid_first_name(name: str) -> bool:
    return bool(FIRST_NAME_RE.fullmatch(name or ""))


def derive_first_name(username: str) -> str:
    """Best guess at a first name from a username: 'Shreyas_12' -> 'Shreyas'."""
    match = re.search(r"[A-Za-z]+", username or "")
    return match.group(0).capitalize() if match else (username or "")


def valid_username(name: str) -> bool:
    return bool(USERNAME_RE.fullmatch(name or ""))


def verify_password(password: str, stored: str) -> bool:
    """Plain-text comparison (passwords are stored as typed). compare_digest avoids timing leaks."""
    return hmac.compare_digest(password.encode(), (stored or "").encode())


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Only this hash is stored in the database, never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()