from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, BigInteger, Boolean
from app.database.db import Base


class Transaction(Base):
    """
    جدول موحّد للمصاريف والإيرادات
    نستخدم عمود type لتمييز النوع (expense / income)
    """
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)

    type = Column(String, nullable=False)        # expense | income
    amount = Column(Float, nullable=True)
    currency = Column(String, nullable=True)
    person = Column(String, nullable=True)        # المورد أو العميل
    description = Column(String, nullable=True)

    raw_message = Column(String, nullable=True)   # نص الرسالة الأصلية (مرجع)
    created_at = Column(DateTime, default=datetime.utcnow)


class Task(Base):
    """
    جدول المهام والتذكيرات
    """
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)

    description = Column(String, nullable=False)  # وصف المهمة
    due_date = Column(DateTime, nullable=True)     # الموعد المحدد
    person = Column(String, nullable=True)         # شخص مرتبط بالمهمة إن وجد
    status = Column(String, nullable=False, default="pending")  # pending | done | overdue

    raw_message = Column(String, nullable=True)   # نص الرسالة الأصلية (مرجع)
    created_at = Column(DateTime, default=datetime.utcnow)