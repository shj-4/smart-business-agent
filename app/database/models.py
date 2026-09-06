from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, DateTime, BigInteger, Boolean
from app.database.db import Base


class Transaction(Base):
    """
    جدول موحّد للمصاريف والإيرادات
    نستخدم عمود type لتمييز النوع (expense / income)
    """
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    telegram_message_id = Column(BigInteger, index=True, nullable=True, unique=True)

    type = Column(String, nullable=False)        # expense | income
    amount = Column(Numeric(12, 2), nullable=True)  # مبلغ مالي بدقة عالية
    currency = Column(String, nullable=True)
    person = Column(String, nullable=True)        # المورد أو العميل
    description = Column(String, nullable=True)
    deleted_at = Column(DateTime, nullable=True)  # Soft delete (لميزة /undo)

    raw_message = Column(String, nullable=True)   # نص الرسالة الأصلية (مرجع)
    created_at = Column(DateTime, default=datetime.utcnow)


class Note(Base):
    """
    جدول الطلبيات والملاحظات (order / note).

    كانت هذه الأنواع تُحلَّل من الـ AI لكن لا تُخزَّن في أي مكان — مما كان
    يوهم المستخدم بأن البيانات حُفظت بينما تضيع بصمت. هذا الجدول يعالج ذلك.
    """
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    telegram_message_id = Column(BigInteger, index=True, nullable=True, unique=True)

    note_type = Column(String, nullable=False)   # order | note
    description = Column(String, nullable=True)   # نص الطلبية/الملاحظة
    person = Column(String, nullable=True)         # المورد أو العميل المرتبط إن وُجد
    deleted_at = Column(DateTime, nullable=True)   # Soft delete (لميزة /undo)

    raw_message = Column(String, nullable=True)   # نص الرسالة الأصلية (مرجع)
    created_at = Column(DateTime, default=datetime.utcnow)


class Task(Base):
    """
    جدول المهام والتذكيرات
    """
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    telegram_message_id = Column(BigInteger, index=True, nullable=True, unique=True)

    description = Column(String, nullable=False)  # وصف المهمة
    due_date = Column(DateTime, nullable=True)     # الموعد المحدد
    person = Column(String, nullable=True)         # شخص مرتبط بالمهمة إن وجد
    status = Column(String, nullable=False, default="pending")  # pending | done | overdue
    deleted_at = Column(DateTime, nullable=True)   # Soft delete (لميزة /undo)

    raw_message = Column(String, nullable=True)   # نص الرسالة الأصلية (مرجع)
    created_at = Column(DateTime, default=datetime.utcnow)
