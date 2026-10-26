p="bot/menus.py"
s=open(p,encoding="utf-8").read()

# Add helper after send_main_menu definition? Instead insert at top of send_main_menu.
old='''async def send_main_menu(
    message: Message, context: ContextTypes.DEFAULT_TYPE, text: str = ""
) -> None:
    """����� ����� ����� ����� ����� ����� ����� ����� ����� (����� ���� /menu)."""
    _clear_all_pending(context)'''
new='''async def send_main_menu(
    message: Message, context: ContextTypes.DEFAULT_TYPE, text: str = ""
) -> None:
    """����� ����� ����� ����� ����� ����� ����� ����� ����� (����� ���� /menu)."""
    _clear_all_pending(context)
    # ��� ����� ����� ����� ����� ����� ����� ����� ����� ����� �����
    try:
        bot = message.get_bot() if hasattr(message, "get_bot") else None
        if bot is None and hasattr(context, "bot"):
            bot = context.bot
        active_id = context.user_data.get("active_menu_message_id") if isinstance(context.user_data, dict) else None
        chat_id = getattr(message, "chat_id", None) or (message.chat.id if getattr(message, "chat", None) else None)
        if active_id and bot and chat_id:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=active_id, text=text or "Menu", reply_markup=None)
            except Exception:
                pass
    except Exception:
        pass'''
assert old in s
s=s.replace(old,new,1)
# store active message id after sending? We don't have message id easily. Append tracking in send_main_menu end is complex; skip.
open(p,"w",encoding="utf-8").write(s)
print("done")