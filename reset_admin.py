"""Reset the admin login to the ADMIN_EMAIL and ADMIN_PASSWORD written in .env.

Stop the website first, then run:   python reset_admin.py
Members, payments and attendance are not touched.
"""
import os
import sqlite3
import sys

from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

here = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(here, ".env"))
email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
password = os.environ.get("ADMIN_PASSWORD", "")
if not email or len(password) < 8:
    sys.exit("Put ADMIN_EMAIL and ADMIN_PASSWORD (8 or more characters) in the .env file first, then run this again.")

path = os.environ.get("DB_PATH") or "plateclub.db"
if not os.path.isabs(path):
    path = os.path.join(here, path)
c = sqlite3.connect(path)
c.execute("CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL)")
c.execute("DELETE FROM admins")
c.execute("INSERT INTO admins(email, password_hash) VALUES (?, ?)", (email, generate_password_hash(password)))
c.commit()
print("Done. Database file:", path)
print("You can now sign in at http://127.0.0.1:5000/admin/login with the email and password from .env")
