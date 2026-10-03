"""Print the table and trigger lists used in docs/DATABASE.md, from a freshly created database.

    python tests/schema_doc.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from app import create_app  # noqa: E402

db = sqlite3.connect(create_app({"TESTING": True}).config["DATABASE"])
for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    cols = ", ".join(f"`{c[1]}`" for c in db.execute(f"PRAGMA table_info({t})"))
    fks = ", ".join(f"{r[3]}→{r[2]}.{r[4]}" for r in db.execute(f"PRAGMA foreign_key_list({t})")) or "—"
    print(f"| `{t}` | {cols} | {fks} |")
print()
for n, t in db.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='trigger' ORDER BY tbl_name, name"):
    print(f"- `{t}`: `{n}`")
