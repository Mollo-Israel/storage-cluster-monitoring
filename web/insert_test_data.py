import sqlite3
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
DB_PATH = os.path.join(ROOT_DIR, "storage.db")

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

clientes = ["LPZ", "CBB", "SCZ", "ORU"]

for c in clientes:
    cursor.execute("""
    INSERT OR REPLACE INTO clients (node_id, status, last_seen)
    VALUES (?, ?, ?)
    """, (c, "ACTIVE", datetime.now().isoformat()))

conn.commit()
conn.close()

print("Clientes de prueba insertados.")