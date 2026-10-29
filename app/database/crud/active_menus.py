"""CRUD for persistent Telegram main-menu references."""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import ActiveMenu, ObsoleteMenu
from app.timeutil import now_utc


def get_active_menu(db: Session, chat_id: int, telegram_user_id: int) -> int | None:
    row = (
        db.query(ActiveMenu)
        .filter(
            ActiveMenu.chat_id == chat_id,
            ActiveMenu.telegram_user_id == telegram_user_id,
        )
        .first()
    )
    return row.message_id if row else None


def set_active_menu(db: Session, chat_id: int, telegram_user_id: int, message_id: int) -> None:
    row = (
        db.query(ActiveMenu)
        .filter(
            ActiveMenu.chat_id == chat_id,
            ActiveMenu.telegram_user_id == telegram_user_id,
        )
        .first()
    )
    if row is None:
        row = ActiveMenu(
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            message_id=message_id,
            updated_at=now_utc(),
        )
        db.add(row)
    else:
        row.message_id = message_id
        row.updated_at = now_utc()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = (
            db.query(ActiveMenu)
            .filter(
                ActiveMenu.chat_id == chat_id,
                ActiveMenu.telegram_user_id == telegram_user_id,
            )
            .first()
        )
        if row is not None:
            row.message_id = message_id
            row.updated_at = now_utc()
            db.commit()


def clear_active_menu(db: Session, chat_id: int, telegram_user_id: int, message_id: int) -> None:
    db.query(ActiveMenu).filter(
        ActiveMenu.chat_id == chat_id,
        ActiveMenu.telegram_user_id == telegram_user_id,
        ActiveMenu.message_id == message_id,
    ).delete(synchronize_session=False)
    db.commit()


def mark_obsolete_menu(db: Session, chat_id: int, telegram_user_id: int, message_id: int) -> None:
    exists = db.query(ObsoleteMenu.id).filter(
        ObsoleteMenu.chat_id == chat_id,
        ObsoleteMenu.telegram_user_id == telegram_user_id,
        ObsoleteMenu.message_id == message_id,
    ).first()
    if not exists:
        db.add(ObsoleteMenu(
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            message_id=message_id,
            created_at=now_utc(),
        ))
        db.commit()


def is_obsolete_menu(db: Session, chat_id: int, telegram_user_id: int, message_id: int) -> bool:
    return db.query(ObsoleteMenu.id).filter(
        ObsoleteMenu.chat_id == chat_id,
        ObsoleteMenu.telegram_user_id == telegram_user_id,
        ObsoleteMenu.message_id == message_id,
    ).first() is not None


def list_obsolete_menus(db: Session, chat_id: int, telegram_user_id: int) -> list[int]:
    """Return only this user's previously tracked menu IDs in this chat."""
    return [
        row[0]
        for row in db.query(ObsoleteMenu.message_id)
        .filter(
            ObsoleteMenu.chat_id == chat_id,
            ObsoleteMenu.telegram_user_id == telegram_user_id,
        )
        .order_by(ObsoleteMenu.created_at, ObsoleteMenu.message_id)
        .all()
    ]


def clear_obsolete_menu(db: Session, chat_id: int, telegram_user_id: int, message_id: int) -> None:
    db.query(ObsoleteMenu).filter(
        ObsoleteMenu.chat_id == chat_id,
        ObsoleteMenu.telegram_user_id == telegram_user_id,
        ObsoleteMenu.message_id == message_id,
    ).delete(synchronize_session=False)
    db.commit()
