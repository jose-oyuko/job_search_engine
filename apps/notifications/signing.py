"""Callback signatures and the chat hash used in logs (spec §13.1, §21.1).

Telegram callback data is limited to 64 bytes, so it carries a short HMAC rather than a full
`django.core.signing` token. The HMAC covers the action, the match and the owning user, so a
button copied into another chat, or forged, fails the check.
"""

import hashlib
import hmac

from django.conf import settings

ACTIONS = {"k": "kit", "a": "applied", "n": "dismiss"}
_SIG_CHARS = 12


def _mac(action: str, match_id: int, user_id: int) -> str:
    message = f"{action}:{match_id}:{user_id}".encode()
    key = settings.SECRET_KEY.encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:_SIG_CHARS]


def sign_callback(action: str, match_id: int, user_id: int) -> str:
    """`k.123.<sig>`: at most about 30 bytes."""
    if action not in ACTIONS:
        raise ValueError("unknown action")
    return f"{action}.{match_id}.{_mac(action, match_id, user_id)}"


def verify_callback(data: str, user_id: int) -> tuple[str, int] | None:
    """`(action name, match_id)` if the signature is valid for this user, else None."""
    parts = data.split(".")
    if len(parts) != 3 or parts[0] not in ACTIONS or not parts[1].isdigit():
        return None
    action, match_id, signature = parts[0], int(parts[1]), parts[2]
    if not hmac.compare_digest(signature, _mac(action, match_id, user_id)):
        return None
    return ACTIONS[action], match_id


def chat_hash(chat_id: str) -> str:
    """A stable, non-reversible label for a chat, safe to put in logs."""
    digest = hashlib.sha256(f"{settings.SECRET_KEY}:chat:{chat_id}".encode()).hexdigest()
    return digest[:10]
