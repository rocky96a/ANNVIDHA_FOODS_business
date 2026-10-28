from pathlib import Path
import shutil
import sys

path = Path("app.py")
source = path.read_text()

if "class PostgresConnection" in source:
    sys.exit("STOP: PostgreSQL compatibility code already exists. No changes made.")

required = [
    "def db():",
    "def init():",
    "init()",
    "cur.lastrowid",
]

missing = [item for item in required if item not in source]
if missing:
    sys.exit("STOP: Expected code not found: " + ", ".join(missing))

backup = Path.home() / "ANNVIDHA_FOODS_pre_postgres_backup" / "app.py.before_pg_patch2"
shutil.copy2(path, backup)

# Add PostgreSQL driver imports.
source = source.replace(
    "import os, sqlite3, json, uuid, time, io, csv",
    "import os, sqlite3, json, uuid, time, io, csv\nimport psycopg"
)

# Database configuration.
source = source.replace(
    'DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop.db"))',
    'DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop.db"))\n'
    'DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()\n'
    'USE_POSTGRES = bool(DATABASE_URL)'
)

# Hybrid row supports both row[0] and row["column"].
marker = "def db():"
compatibility = '''class HybridRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            key = list(self.keys())[key]
        return super().__getitem__(key)


def postgres_row_factory(cursor):
    # Read column names when each row is fetched, after execute().
    def make_row(values):
        return HybridRow(zip((col.name for col in cursor.description), values))
    return make_row


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=()):
        return self.connection.execute(query.replace("?", "%s"), params)

    def executemany(self, query, params):
        return self.connection.cursor().executemany(query.replace("?", "%s"), params)

    def commit(self):
        return self.connection.commit()

    def close(self):
        return self.connection.close()


'''
source = source.replace(marker, compatibility + marker, 1)

old_db = '''def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db
'''
new_db = '''def db():
    if "db" not in g:
        if USE_POSTGRES:
            connection = psycopg.connect(
                DATABASE_URL,
                row_factory=postgres_row_factory,
                connect_timeout=10,
            )
            g.db = PostgresConnection(connection)
        else:
            g.db = sqlite3.connect(DB)
            g.db.row_factory = sqlite3.Row
    return g.db
'''
if old_db not in source:
    sys.exit("STOP: Could not match db() exactly. No app.py changes made.")

source = source.replace(old_db, new_db, 1)

# Do not write partial changes if expected sections are absent.
if "def postgres_row_factory(cursor):" not in source:
    sys.exit("STOP: Row adapter insertion failed.")

path.write_text(source)
print("Patched database connection layer.")
print("Backup:", backup)
