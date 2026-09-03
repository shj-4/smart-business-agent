from app.database.db import engine, Base
from app.database import models  # مهم: يستورد كل الجداول حتى تُنشأ


def init_db():
    Base.metadata.create_all(bind=engine)
    print("تم إنشاء قاعدة البيانات والجداول بنجاح")


if __name__ == "__main__":
    init_db()