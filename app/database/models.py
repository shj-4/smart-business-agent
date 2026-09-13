from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)

from app.database.db import Base
from app.security import EncryptedNumeric, EncryptedString

# تصنيفات مالية شائعة تُستخرج تلقائيًا من الـ AI (الحقل اختياري)
DEFAULT_CATEGORIES = [
    "إيجار",
    "رواتب",
    "مواد خام",
    "مشتريات",
    "نقل وشحن",
    "كهرباء",
    "ماء",
    "هاتف وانترنت",
    "طعام",
    "صيانة",
    "تسويق وإعلان",
    "ضرائب",
    "أخرى",
]


class Transaction(Base):
    """
    جدول موحّد للمصاريف والإيرادات
    نستخدم عمود type لتمييز النوع (expense / income)
    """

    __tablename__ = "transactions"
    __table_args__ = (
        # message_id فريد لكل محادثة، لذا القيد فريد مركّب (مستخدِم + رسالة)
        UniqueConstraint(
            "telegram_user_id", "telegram_message_id", name="uq_transactions_user_message"
        ),
        Index("ix_transactions_user_created", "telegram_user_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    telegram_message_id = Column(BigInteger, nullable=True)

    type = Column(String(16), nullable=False)  # expense | income
    amount = Column(EncryptedNumeric(), nullable=True)  # مبلغ مالي (مشفر) — يجمع في Python
    currency = Column(String(16), nullable=True)
    person = Column(String(255), nullable=True)  # المورد أو العميل
    category = Column(String(64), nullable=True)  # تصنيف اختياري (إيجار، رواتب...)
    description = Column(EncryptedString(), nullable=True)  # مشفر
    deleted_at = Column(DateTime, nullable=True)  # Soft delete (لميزة /undo)

    # سعر الصرف مثبَّت وقت التسجيل (للدقة التاريخية للتقارير الموحّدة):
    # amount_in_base_currency = المبلغ محوّلًا لعملة الأساس وقت إنشاء العملية، و
    # base_currency_at_creation يوثّق العملة الأساس التي استُخدمت (أو None إن فشل التحويل)
    amount_in_base_currency = Column(EncryptedNumeric(), nullable=True)
    base_currency_at_creation = Column(String(16), nullable=True)

    # حقول الضريبة (VAT): نسبة وكمية اختياريتان تُستخرجان عند التسجيل
    vat_rate = Column(Numeric(6, 3), nullable=True)  # النسبة المؤوية (مثل 17.000)
    vat_amount = Column(Numeric(12, 2), nullable=True)  # قيمة الضريبة بعملة العملية

    raw_message = Column(EncryptedString(), nullable=True)  # نص الرسالة الأصلية (مشفر)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)


class Note(Base):
    """
    جدول الطلبيات والملاحظات (order / note).

    كانت هذه الأنواع تُحلَّل من الـ AI لكن لا تُخزَّن في أي مكان — مما كان
    يوهم المستخدم بأن البيانات حُفظت بينما تضيع بصمت. هذا الجدول يعالج ذلك.
    """

    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", "telegram_message_id", name="uq_notes_user_message"),
    )

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    telegram_message_id = Column(BigInteger, nullable=True)

    note_type = Column(String(16), nullable=False)  # order | note
    description = Column(EncryptedString(), nullable=True)  # نص الطلبية/الملاحظة (مشفر)
    person = Column(String(255), nullable=True)  # المورد أو العميل المرتبط إن وُجد
    category = Column(String(64), nullable=True)  # تصنيف اختياري (مشتريات، مواد خام...)
    status = Column(String(16), nullable=True)  # الطلبيات: open | done (None للملاحظات)
    deleted_at = Column(DateTime, nullable=True)  # Soft delete (لميزة /undo)

    raw_message = Column(EncryptedString(), nullable=True)  # نص الرسالة الأصلية (مشفر)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)


class Task(Base):
    """
    جدول المهام والتذكيرات
    """

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", "telegram_message_id", name="uq_tasks_user_message"),
    )

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    telegram_message_id = Column(BigInteger, nullable=True)

    description = Column(EncryptedString(), nullable=False)  # وصف المهمة (مشفر)
    due_date = Column(DateTime, nullable=True)  # الموعد المحدد
    person = Column(String(255), nullable=True)  # شخص مرتبط بالمهمة إن وجد
    priority = Column(String(8), default="normal", nullable=False)  # high | normal | low
    recurrence_rule = Column(String(16), nullable=True)  # daily | weekly | monthly
    status = Column(String(16), nullable=False, default="pending")  # pending | done | overdue
    deleted_at = Column(DateTime, nullable=True)  # Soft delete (لميزة /undo)

    raw_message = Column(EncryptedString(), nullable=True)  # نص الرسالة الأصلية (مشفر)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)
    reminder_sent = Column(Boolean, default=False, nullable=False)


class CorrectionFeedback(Base):
    """سجل "تحليل خاطئ": إلغاء المستخدم (/cancel) أو رفضه للتأكيد.

    يُستخدَم لمراجعة يدوية دورية وتحسين الـ prompt — يتتبّع الرسائل التي
    فشل فيها الفهم أو رفضها المستخدم، دون أن تكون جزءًا من البيانات المالية.
    """

    __tablename__ = "correction_feedback"

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    source = Column(String(16), nullable=False)  # cancel | reject_confirm
    raw_message = Column(EncryptedString(), nullable=True)  # النص الأصلي (مشفر)
    data_type = Column(String(16), nullable=True)  # intent->type المشتبه به إن وُجد
    reviewed = Column(Boolean, default=False, nullable=False)  # رُوجع يدويًا؟
    created_at = Column(DateTime, default=datetime.utcnow)


class WorkspaceMember(Base):
    """عضو في مساحة عمل مشتركة (حساب واحد لعدة معرّفات Telegram).

    تمثيل خفيف دون جداول إضافية مركّبة:
      - workspace_id هو مرتكز الحيازة = معرّف المالك (أوّل من أنشأ المساحة).
      - البيانات تبقى موثقة بمعرّف كاتبها (telegram_user_id) في جداولها؛ لكن
        كل القراءات تُنطّق بمجموعة أعضاء المساحة (IN members) بدل معرّف واحد.
      - عضو واحد لكل معرّف Telegram (مفتاح أساسي) — مغادرة ثم انضمام لتغييرها.
      - status يتحكم في الانضمام:
          * "active"  — العضو وافق صراحةً (فوريًا للمالك؛ وبعد قبول الدعوة لغيره)،
                       وتصبح بيانات الطرفين مرئية داخل المساحة.
          * "pending" — دعوة معلّقة بانتظار موافقة الطرف المدعو؛ لا يمنح أي وصول
                       حتى يُقبِل، فتُساوي "active".
        الصفوف القديمة (قبل الحقل) تُعامَل "active" — الترتيبات القائمة تبقى.
    """

    __tablename__ = "workspace_members"

    telegram_user_id = Column(BigInteger, primary_key=True)
    workspace_id = Column(BigInteger, nullable=False, index=True)
    status = Column(
        String(16), nullable=False, default="active", server_default="active"
    )
    joined_at = Column(DateTime, default=datetime.utcnow)


class Budget(Base):
    """
    ميزانية شهرية (سقف مصروف).

    scope يحدد نوع السقف:
      - "currency": سقف على إجمالي مصروفات عملة معيّنة (currency مثل ILS).
      - "person": سقف على إجمالي مصروفات شخص معيّن (تعامل بالدين).
      - "category": سقف على مصروفات تصنيف معيّن (category مثل "مشتريات").
    monthly_limit: الحد الشهري.
    alerted_status: 0=لا تنبيه، 1=تنبيه اقتراب (≥80%)، 2=تنبيه تجاوز (≥100%).
    month_key: "YYYY-MM" للميزانية الجارية — يتغير الشهر عند قبول تنبيه جديد.
    """

    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint(
            "telegram_user_id", "scope", "currency", name="uq_budget_user_scope_currency"
        ),
        UniqueConstraint("telegram_user_id", "scope", "person", name="uq_budget_user_scope_person"),
        UniqueConstraint(
            "telegram_user_id", "scope", "category", name="uq_budget_user_scope_category"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)

    name = Column(String(255), nullable=True)  # وصف/اسم اختياري
    scope = Column(String(16), nullable=False)  # currency | person | category
    currency = Column(String(16), nullable=True)  # عند scope=currency
    person = Column(String(255), nullable=True)  # عند scope=person
    category = Column(String(64), nullable=True)  # عند scope=category
    monthly_limit = Column(Numeric(12, 2), nullable=False)

    alerted_status = Column(Integer, default=0, nullable=False)  # 0|1|2
    month_key = Column(String(7), default="", nullable=False)  # "YYYY-MM"

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)


class CreditLimit(Base):
    """سقف ائتماني لشخص: أقصى دين عليك له (expense - income) يُنبَّه عند اقترابه.

    طبيعة التعامل بالدين خطرة، لذا يوثّق المستخدم حدودًا ائتمانية للأشخاص ويعطي
    البوت تنبيهًا عند الاقتراب من السقف أو تجاوزه (دون حجب العمليات).
    """

    __tablename__ = "credit_limits"
    __table_args__ = (UniqueConstraint("telegram_user_id", "person", name="uq_credit_user_person"),)

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    person = Column(String(255), nullable=False)
    limit_amount = Column(Numeric(12, 2), nullable=False)

    alerted_status = Column(Integer, default=0, nullable=False)  # 0=لا تنبيه | 1=اقتراب | 2=تجاوز
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)


class Invoice(Base):
    """فاتورة آجلة (ذمم): مبلغ مستحق لشخص أو مورّد بتواريخ استحقاق.

    status: pending | paid | overdue. عنصر "paid_at" يوثّق تاريخ السداد.
    تنبيه الفاتورة المتأخرة يُرسل مرة واحدة بفضل عمود alerted.
    """

    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    person = Column(String(255), nullable=True)  # المورّد/الجهة المستحقة
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(16), nullable=True)
    description = Column(EncryptedString(), nullable=True)  # وصف الفاتورة (مشفر)
    due_date = Column(DateTime, nullable=True)  # تاريخ الاستحقاق (UTC)
    status = Column(String(16), nullable=False, default="pending")  # pending|paid|overdue
    paid_at = Column(DateTime, nullable=True)
    alerted = Column(Boolean, default=False, nullable=False)  # أُرسل تنبيه تأخر؟ (مرة واحدة)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)


class ReportPref(Base):
    """
    تفضيلات التقارير الدورية التلقائية.

    frequency: "off" (مُعطَّلة) | "daily" | "weekly" | "monthly".
    deliver_time: وقت الإرسال "HH:MM" بالتوقيت المحلي (افتراضيًا من settings.report_time).
    last_sent_at: آخر مرة أُرسل فيها التقرير (لمنع التكرار).
    """

    __tablename__ = "report_prefs"
    __table_args__ = (UniqueConstraint("telegram_user_id", name="uq_report_pref_user"),)

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    frequency = Column(String(8), nullable=False, default="off")  # off|daily|weekly|monthly
    deliver_time = Column(String(8), nullable=True)  # "HH:MM" محلي
    last_sent_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)


class UserPref(Base):
    """تفضيلات واجهة لكل مستخدم (صف واحد لكل معرّف).

    lang: لغة الواجهة — "ar" (الافتراضي) أو "en".
    """

    __tablename__ = "user_prefs"
    __table_args__ = (UniqueConstraint("telegram_user_id", name="uq_user_pref_user"),)

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, index=True, nullable=False)
    lang = Column(String(2), nullable=False, default="ar")  # ar | en
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)
