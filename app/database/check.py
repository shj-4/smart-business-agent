from app.database.db import SessionLocal
from app.database.models import Transaction

db = SessionLocal()
rows = db.query(Transaction).all()
for r in rows:
    print(r.id, r.type, r.amount, r.currency, r.person, r.description, r.created_at)
db.close()