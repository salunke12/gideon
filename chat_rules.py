"""Rules for chat names. Plain functions, easy to test."""
import re

MAX_CHATS = 50
MAX_TITLE = 60
DEFAULT_TITLE = "New chat"


def clean_title(raw, default: str | None = DEFAULT_TITLE) -> str | None:
    """Tidy a chat name: single spaces, printable characters only, at most 60 characters.
    A blank name becomes `default` (or None when no default is given)."""
    text = "".join(ch for ch in str(raw or "") if ch.isprintable() or ch.isspace())
    text = re.sub(r"\s+", " ", text).strip()[:MAX_TITLE].strip()
    return text or default
