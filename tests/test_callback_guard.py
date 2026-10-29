from types import SimpleNamespace

from bot import callback_guard


def test_same_button_press_is_claimed_once_within_window():
    callback_guard._recent_callbacks.clear()
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=44),
        message=SimpleNamespace(chat_id=500, message_id=10),
        data="confirm:save",
    )

    assert callback_guard.claim_callback(query, now=100.0)
    assert not callback_guard.claim_callback(query, now=101.0)
    assert callback_guard.claim_callback(query, now=104.0)


def test_callback_deduplication_is_scoped_to_user_and_message():
    callback_guard._recent_callbacks.clear()
    base = dict(message=SimpleNamespace(chat_id=500, message_id=10), data="menu:main")
    first = SimpleNamespace(from_user=SimpleNamespace(id=44), **base)
    other_user = SimpleNamespace(from_user=SimpleNamespace(id=45), **base)
    other_message = SimpleNamespace(
        from_user=SimpleNamespace(id=44),
        message=SimpleNamespace(chat_id=500, message_id=11),
        data="menu:main",
    )

    assert callback_guard.claim_callback(first, now=200.0)
    assert callback_guard.claim_callback(other_user, now=200.5)
    assert callback_guard.claim_callback(other_message, now=201.0)
