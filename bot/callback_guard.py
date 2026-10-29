"""Short-window de-duplication for repeated Telegram inline button presses."""

from __future__ import annotations

import time

from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

_WINDOW_SECONDS = 3.0
_MAX_KEYS = 4096
_recent_callbacks: dict[tuple[object, ...], float] = {}


def _callback_key(query) -> tuple[object, ...] | None:
    user = getattr(getattr(query, "from_user", None), "id", None)
    message = getattr(query, "message", None)
    data = getattr(query, "data", None)
    if user is None or data is None:
        return None
    if message is not None:
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "message_id", None)
        if chat_id is not None and message_id is not None:
            return (user, chat_id, message_id, data)
    inline_message_id = getattr(query, "inline_message_id", None)
    if inline_message_id:
        return (user, inline_message_id, data)
    return None


def claim_callback(query, *, now: float | None = None) -> bool:
    """Return False for the same user's repeated action on the same message."""
    key = _callback_key(query)
    if key is None:
        return True
    current = time.monotonic() if now is None else now
    expired = [k for k, stamp in _recent_callbacks.items() if current - stamp >= _WINDOW_SECONDS]
    for old_key in expired:
        _recent_callbacks.pop(old_key, None)
    previous = _recent_callbacks.get(key)
    if previous is not None and current - previous < _WINDOW_SECONDS:
        return False
    _recent_callbacks[key] = current
    if len(_recent_callbacks) > _MAX_KEYS:
        for old_key, _ in sorted(_recent_callbacks.items(), key=lambda item: item[1])[: _MAX_KEYS // 4]:
            _recent_callbacks.pop(old_key, None)
    return True


async def callback_deduplication_guard(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop duplicate callback updates before any conversation/business handler."""
    query = getattr(update, "callback_query", None)
    if query is None or claim_callback(query):
        return
    try:
        await query.answer("تم استلام هذه النقرة بالفعل.", show_alert=False)
    except TelegramError:
        pass
    raise ApplicationHandlerStop
