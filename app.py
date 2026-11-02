"""Plate Club: gym membership website in Python (Flask).

Pages are rendered on the server with templates, so there is no custom JavaScript.
Payments use Razorpay's standard checkout (their own script), or fake payments in DEMO mode.
"""
import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()
E = os.environ
PROD = E.get("APP_ENV") == "production"
DEMO = E.get("DEMO_PAYMENTS", "").lower() == "true"
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = E.get("DB_PATH") or "plateclub.db"
if not os.path.isabs(DB_PATH):  # a relative path is relative to this folder, wherever you run the app from
    DB_PATH = os.path.join(BASE, DB_PATH)
MIN_PW = 8  # shortest allowed admin password

if DEMO and PROD:
    raise SystemExit("DEMO_PAYMENTS cannot be on in production.")
if not DEMO and not (E.get("RAZORPAY_KEY_ID") and E.get("RAZORPAY_KEY_SECRET")):
    raise SystemExit("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env (or DEMO_PAYMENTS=true to test locally).")
rz = None
if not DEMO:
    import razorpay
    rz = razorpay.Client(auth=(E["RAZORPAY_KEY_ID"], E["RAZORPAY_KEY_SECRET"]))

# Prices live on the server only. A browser can never set an amount.
PLANS = [
    {"id": "basic", "name": "Basic", "monthly_paise": 59900},
    {"id": "plus", "name": "Plus", "monthly_paise": 79900},
    {"id": "pro", "name": "Pro", "monthly_paise": 99900},
    {"id": "elite", "name": "Elite", "monthly_paise": 129900},
]
LOOK = {  # display only
    "basic": {"kg": 10, "c": "#2e9e5b", "fg": "#fff", "perks": ["Gym floor, 6am–10pm", "Locker room access", "Free fitness assessment"]},
    "plus": {"kg": 15, "c": "#f2b705", "fg": "#101820", "perks": ["Everything in Basic", "Group classes", "Steam room"]},
    "pro": {"kg": 20, "c": "#2f5bff", "fg": "#fff", "perks": ["Everything in Plus", "2 trainer sessions a month", "Nutrition plan"]},
    "elite": {"kg": 25, "c": "#d93a2f", "fg": "#fff", "perks": ["Everything in Pro", "24/7 access", "Bring a guest each week"]},
}
ANNUAL_MULTIPLIER = 10  # pay for 10 months, get 12


# ---------------------------------------------------------------- database
def connect():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db():
    c = connect()
    c.execute("PRAGMA journal_mode = WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS members (
      id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
      plan TEXT NOT NULL, billing TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','cancelled')),
      start_date TEXT, end_date TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS payments (
      id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
      order_id TEXT UNIQUE NOT NULL, payment_id TEXT, amount_paise INTEGER NOT NULL, method TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created','paid')),
      created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS checkins (
      id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
      at TEXT NOT NULL DEFAULT (datetime('now')), source TEXT NOT NULL DEFAULT 'desk');
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_members_email ON members(email);
    """)
    try:  # databases made before the attendance program need one extra column
        c.execute("ALTER TABLE checkins ADD COLUMN source TEXT NOT NULL DEFAULT 'desk'")
    except sqlite3.OperationalError:
        pass  # column already exists
    email, pw = E.get("ADMIN_EMAIL", "").strip().lower(), E.get("ADMIN_PASSWORD", "")
    if email and len(pw) >= MIN_PW:
        # Adds the admin from .env if that email has no account yet (existing accounts are never overwritten).
        if not c.execute("SELECT 1 FROM admins WHERE email=?", (email,)).fetchone():
            c.execute("INSERT INTO admins(email, password_hash) VALUES (?, ?)", (email, generate_password_hash(pw)))
            print(f"Created admin account for {email}")
    elif not c.execute("SELECT 1 FROM admins").fetchone():
        print(f"No admin account yet. Set ADMIN_EMAIL and ADMIN_PASSWORD ({MIN_PW}+ characters) in .env and restart.")
    c.execute("DELETE FROM members WHERE status='pending' AND created_at < datetime('now','-1 day')")
    # The key that signs login cookies is made automatically and kept in the database.
    row = c.execute("SELECT value FROM settings WHERE key='secret_key'").fetchone()
    if row:
        key = row["value"]
    else:
        key = secrets.token_hex(32)
        c.execute("INSERT INTO settings(key, value) VALUES ('secret_key', ?)", (key,))
    c.commit()
    c.close()
    return key


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.secret_key = E.get("SECRET_KEY") or init_db()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=PROD,
                  PERMANENT_SESSION_LIFETIME=8 * 3600, MAX_CONTENT_LENGTH=20 * 1024)
if E.get("SECRET_KEY"):
    init_db()


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    c = g.pop("db", None)
    if c:
        c.close()


# ---------------------------------------------------------------- helpers
def today():
    return date.today().isoformat()


def add_months(d, n):
    y, m, day = map(int, d.split("-"))
    m0 = m - 1 + n
    y, m = y + m0 // 12, m0 % 12 + 1
    for dd in (day, 30, 29, 28):  # clamp to the end of shorter months
        try:
            return date(y, m, dd).isoformat()
        except ValueError:
            continue


def plan_by_id(pid):
    return next((p for p in PLANS if p["id"] == pid), None)


def amount_for(plan, billing):
    return plan["monthly_paise"] * (ANNUAL_MULTIPLIER if billing == "annual" else 1)


def state_of(m):
    if m["status"] == "cancelled":
        return "Cancelled"
    return "Expired" if m["end_date"] and m["end_date"] < today() else "Active"


def mark_paid(order_id, payment_id):
    """Idempotent: safe to call from the browser redirect and the webhook."""
    c = db()
    pay = c.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
    if not pay:
        return None
    m = c.execute("SELECT * FROM members WHERE id=?", (pay["member_id"],)).fetchone()
    if pay["status"] == "paid":
        return m
    live = m["status"] == "active" and m["end_date"] and m["end_date"] > today()
    start = m["end_date"] if live else today()
    months = 12 if m["billing"] == "annual" else 1
    c.execute("UPDATE payments SET status='paid', payment_id=? WHERE id=?", (payment_id, pay["id"]))
    c.execute("UPDATE members SET status='active', start_date=COALESCE(start_date, ?), end_date=? WHERE id=?",
              (today(), add_months(start, months), m["id"]))
    c.commit()
    return c.execute("SELECT * FROM members WHERE id=?", (m["id"],)).fetchone()


_hits = {}


def limited(key, maximum, window=900):
    now = time.time()
    hits = [t for t in _hits.get(key, []) if now - t < window]
    hits.append(now)
    _hits[key] = hits
    return len(hits) > maximum


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf():
    if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "x")):
        abort(400)


app.jinja_env.globals.update(csrf_token=csrf_token, look=LOOK)
app.jinja_env.filters["inr"] = lambda p: "₹{:,}".format(int(p) // 100)
app.jinja_env.filters["nice_date"] = lambda d: datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y").lstrip("0") if d else "–"


@app.after_request
def headers(r):
    r.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://checkout.razorpay.com; "
        "frame-src https://api.razorpay.com https://checkout.razorpay.com; "
        "connect-src 'self' https://api.razorpay.com https://lumberjack.razorpay.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https://*.razorpay.com; form-action 'self' https://api.razorpay.com; frame-ancestors 'none'")
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "same-origin"
    return r


# ---------------------------------------------------------------- public pages
@app.get("/")
def index():
    billing = "annual" if request.args.get("billing") == "annual" else "monthly"
    prices = {p["id"]: amount_for(p, billing) for p in PLANS}
    return render_template("index.html", plans=PLANS, billing=billing, prices=prices)


@app.route("/join", methods=["GET", "POST"])
def join():
    src = request.form if request.method == "POST" else request.args
    plan = plan_by_id(src.get("plan"))
    billing = "annual" if src.get("billing") == "annual" else "monthly"
    if not plan:
        abort(404)
    form = {"name": "", "email": "", "phone": ""}
    if request.method == "GET":
        return render_template("join.html", plan=plan, billing=billing, price=amount_for(plan, billing), form=form, error=None)

    check_csrf()
    if limited("join:" + (request.remote_addr or ""), 20):
        abort(429)
    form = {k: request.form.get(k, "").strip() for k in form}
    form["email"] = form["email"].lower()
    err = None
    if not 2 <= len(form["name"]) <= 80:
        err = "Enter your full name."
    elif not re.fullmatch(r"\S+@\S+\.\S+", form["email"]) or len(form["email"]) > 120:
        err = "Enter a valid email address, like name@example.com."
    elif not re.fullmatch(r"[6-9]\d{9}", form["phone"]):
        err = "Enter a 10-digit Indian mobile number."
    elif db().execute("SELECT 1 FROM members WHERE email=? AND status='active' AND end_date>=?", (form["email"], today())).fetchone():
        err = "This email already has an active membership. Please ask the front desk to renew it."
    amount = amount_for(plan, billing)
    if not err:
        code = "GYM-" + secrets.token_hex(3).upper()
        try:
            order_id = ("order_demo_" + secrets.token_hex(6)) if DEMO else rz.order.create(
                {"amount": amount, "currency": "INR", "receipt": code, "notes": {"plan": plan["id"], "billing": billing}})["id"]
        except Exception:
            app.logger.exception("Could not create Razorpay order")
            err = "We couldn't start the payment. Please try again in a moment."
    if err:
        return render_template("join.html", plan=plan, billing=billing, price=amount, form=form, error=err), 400
    c = db()
    cur = c.execute("INSERT INTO members(code,name,email,phone,plan,billing,status) VALUES (?,?,?,?,?,?, 'pending')",
                    (code, form["name"], form["email"], form["phone"], plan["id"], billing))
    c.execute("INSERT INTO payments(member_id,order_id,amount_paise,method,status) VALUES (?,?,?,?, 'created')",
              (cur.lastrowid, order_id, amount, "demo" if DEMO else "razorpay"))
    c.commit()
    return redirect(url_for("pay", order_id=order_id))


@app.get("/pay/<order_id>")
def pay(order_id):
    row = db().execute("""SELECT p.order_id, p.amount_paise, p.status, m.name, m.email, m.phone, m.plan, m.billing
                          FROM payments p JOIN members m ON m.id = p.member_id WHERE p.order_id=?""", (order_id,)).fetchone()
    if not row:
        abort(404)
    if row["status"] == "paid":
        return redirect(url_for("member_pass", order_id=order_id))
    return render_template("pay.html", p=row, plan=plan_by_id(row["plan"]), demo=DEMO, key_id=E.get("RAZORPAY_KEY_ID", ""))


@app.post("/pay/<order_id>/demo")
def pay_demo(order_id):
    check_csrf()
    if not DEMO:
        abort(404)
    if not mark_paid(order_id, "pay_demo"):
        abort(404)
    return redirect(url_for("member_pass", order_id=order_id))


@app.post("/razorpay/callback")
def razorpay_callback():
    oid, pid, sig = (request.form.get(k, "") for k in ("razorpay_order_id", "razorpay_payment_id", "razorpay_signature"))
    if DEMO or not (oid and pid and sig):
        abort(400)
    expected = hmac.new(E["RAZORPAY_KEY_SECRET"].encode(), f"{oid}|{pid}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected) or not mark_paid(oid, pid):
        flash(f"Payment could not be verified. If money was taken, contact the front desk with payment ID {pid}.")
        return redirect(url_for("index"))
    return redirect(url_for("member_pass", order_id=oid))


@app.get("/pass/<order_id>")
def member_pass(order_id):
    m = db().execute("""SELECT m.* FROM payments p JOIN members m ON m.id=p.member_id
                        WHERE p.order_id=? AND p.status='paid'""", (order_id,)).fetchone()
    if not m:
        abort(404)
    return render_template("pass.html", m=m, plan=plan_by_id(m["plan"]))


@app.post("/api/webhook/razorpay")
def webhook():
    secret = E.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        abort(404)
    raw = request.get_data()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("X-Razorpay-Signature", ""), expected):
        abort(400)
    ev = request.get_json(force=True, silent=True) or {}
    if ev.get("event") in ("payment.captured", "order.paid"):
        ent = ((ev.get("payload") or {}).get("payment") or {}).get("entity")
        if ent:
            mark_paid(ent["order_id"], ent["id"])
    return "", 200


# ---------------------------------------------------------------- admin
def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        aid = session.get("admin_id")
        if not aid or not db().execute("SELECT 1 FROM admins WHERE id=?", (aid,)).fetchone():
            return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        check_csrf()
        ip = request.remote_addr or ""
        if limited("login:" + ip, 10):
            flash("Too many attempts. Please wait a few minutes.")
            return render_template("login.html"), 429
        a = db().execute("SELECT * FROM admins WHERE email=?", (request.form.get("email", "").strip().lower(),)).fetchone()
        if a and check_password_hash(a["password_hash"], request.form.get("password", "")):
            session.clear()
            session["admin_id"] = a["id"]
            session.permanent = True
            _hits.pop("login:" + ip, None)
            return redirect(url_for("admin"))
        flash("Incorrect email or password.")
    elif not db().execute("SELECT 1 FROM admins").fetchone():
        flash("No admin account exists yet. Put ADMIN_EMAIL and ADMIN_PASSWORD in the .env file and restart the site.")
    return render_template("login.html")


@app.post("/admin/logout")
def admin_logout():
    check_csrf()
    session.clear()
    return redirect(url_for("admin_login"))


@app.get("/admin")
@admin_required
def admin():
    c, t, q = db(), today(), "%" + request.args.get("q", "").strip() + "%"
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    stats = [
        ("Members", one("SELECT COUNT(*) FROM members WHERE status!='pending'")),
        ("Active", one("SELECT COUNT(*) FROM members WHERE status='active' AND end_date>=?", t)),
        ("Revenue", "₹{:,}".format(one("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE status='paid'") // 100)),
        ("Expiring in 7 days", one("SELECT COUNT(*) FROM members WHERE status='active' AND end_date>=? AND end_date<=date(?, '+7 day')", t, t)),
        ("Check-ins today", one("SELECT COUNT(*) FROM checkins WHERE date(at,'localtime')=date('now','localtime')")),
    ]
    rows = c.execute("""SELECT m.*, (SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE member_id=m.id AND status='paid') AS paid_paise
                        FROM members m WHERE m.status!='pending' AND (m.name LIKE ? OR m.email LIKE ? OR m.code LIKE ?)
                        ORDER BY m.id DESC LIMIT 500""", (q, q, q)).fetchall()
    members = [dict(r, state=state_of(r)) for r in rows]
    return render_template("admin.html", stats=stats, members=members, q=request.args.get("q", ""))


@app.route("/admin/members/<int:mid>/<action>", methods=["GET", "POST"])
@admin_required
def admin_action(mid, action):
    if action not in ("checkin", "renew", "cancel", "delete"):
        abort(404)
    c = db()
    m = c.execute("SELECT * FROM members WHERE id=? AND status!='pending'", (mid,)).fetchone()
    if not m:
        abort(404)
    if request.method == "GET":  # confirmation page for actions that can't be undone
        if action == "checkin":
            abort(404)
        return render_template("confirm.html", m=m, action=action)
    check_csrf()
    if action == "checkin":
        if state_of(m) != "Active":
            flash("Only active members can check in.")
        elif c.execute("SELECT 1 FROM checkins WHERE member_id=? AND date(at,'localtime')=date('now','localtime')", (mid,)).fetchone():
            flash(f"{m['name']} is already checked in today.")
        else:
            c.execute("INSERT INTO checkins(member_id) VALUES (?)", (mid,))
            c.commit()
    elif action == "renew":  # offline renewal: cash or UPI at the desk
        oid = "offline_" + secrets.token_hex(8)
        c.execute("INSERT INTO payments(member_id,order_id,amount_paise,method,status) VALUES (?,?,?,'offline','created')",
                  (mid, oid, amount_for(plan_by_id(m["plan"]), m["billing"])))
        c.commit()
        mark_paid(oid, "offline")
    elif action == "cancel":
        c.execute("UPDATE members SET status='cancelled' WHERE id=?", (mid,))
        c.commit()
    else:
        c.execute("DELETE FROM members WHERE id=?", (mid,))
        c.commit()
    return redirect(url_for("admin"))


@app.post("/admin/password")
@admin_required
def admin_password():
    check_csrf()
    a = db().execute("SELECT * FROM admins WHERE id=?", (session["admin_id"],)).fetchone()
    new = request.form.get("new", "")
    if not check_password_hash(a["password_hash"], request.form.get("current", "")):
        flash("Current password is incorrect.")
    elif len(new) < MIN_PW:
        flash(f"New password must be at least {MIN_PW} characters.")
    else:
        db().execute("UPDATE admins SET password_hash=? WHERE id=?", (generate_password_hash(new), a["id"]))
        db().commit()
        flash("Password changed.")
    return redirect(url_for("admin"))


# ---------------------------------------------------------------- attendance
def visit_stats(c, mid):
    rows = c.execute("SELECT DISTINCT date(at,'localtime') AS d FROM checkins WHERE member_id=? ORDER BY d DESC", (mid,)).fetchall()
    days = [date.fromisoformat(r["d"]) for r in rows]
    streak = 0
    if days and days[0] >= date.today() - timedelta(days=1):  # counts if the last visit was today or yesterday
        streak = 1
        for prev, nxt in zip(days, days[1:]):
            if nxt != prev - timedelta(days=1):
                break
            streak += 1
    month = date.today().strftime("%Y-%m")
    return {"streak": streak, "month": sum(1 for d in days if d.strftime("%Y-%m") == month), "total": len(days)}


@app.route("/checkin", methods=["GET", "POST"])
def self_checkin():
    """Self check-in for members (for a tablet at the front desk). Needs member ID + registered mobile number."""
    result = error = None
    if request.method == "POST":
        check_csrf()
        if limited("checkin:" + (request.remote_addr or ""), 30):
            abort(429)
        code = request.form.get("code", "").strip().upper()
        phone = request.form.get("phone", "").strip()
        c = db()
        m = c.execute("SELECT * FROM members WHERE code=? AND phone=? AND status!='pending'", (code, phone)).fetchone()
        if not m:
            error = "We couldn't find that member ID and mobile number. Please check them and try again."
        elif state_of(m) != "Active":
            error = "Your membership isn't active right now. Please see the front desk."
        else:
            already = c.execute("SELECT 1 FROM checkins WHERE member_id=? AND date(at,'localtime')=date('now','localtime')", (m["id"],)).fetchone() is not None
            if not already:
                c.execute("INSERT INTO checkins(member_id, source) VALUES (?, 'self')", (m["id"],))
                c.commit()
            result = {"name": m["name"], "already": already, **visit_stats(c, m["id"])}
    return render_template("checkin.html", result=result, error=error), (400 if error else 200)


def csv_safe(v):  # stops spreadsheet programs from running a name that starts with = + - @
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


@app.get("/admin/attendance")
@admin_required
def admin_attendance():
    c = db()
    try:
        day = date.fromisoformat(request.args.get("date", "")).isoformat()
    except ValueError:
        day = today()
    month = request.args.get("month", "")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        month = day[:7]
    visits = c.execute("""SELECT time(c.at,'localtime') AS t, c.source, m.name, m.code, m.plan FROM checkins c
                          JOIN members m ON m.id=c.member_id WHERE date(c.at,'localtime')=? ORDER BY c.at""", (day,)).fetchall()
    summary = c.execute("""SELECT m.name, m.code, m.plan, COUNT(DISTINCT date(c.at,'localtime')) AS visits
                           FROM members m LEFT JOIN checkins c ON c.member_id=m.id AND strftime('%Y-%m', c.at,'localtime')=?
                           WHERE m.status='active' AND m.end_date>=? GROUP BY m.id ORDER BY visits DESC, m.name""", (month, today())).fetchall()
    away = c.execute("""SELECT * FROM (SELECT m.name, m.code, m.phone, m.plan, m.start_date,
                          (SELECT MAX(date(at,'localtime')) FROM checkins WHERE member_id=m.id) AS last_visit
                        FROM members m WHERE m.status='active' AND m.end_date>=?)
                        WHERE COALESCE(last_visit, start_date) < date('now','localtime','-7 day')
                        ORDER BY COALESCE(last_visit, start_date)""", (today(),)).fetchall()
    return render_template("attendance.html", day=day, month=month, visits=visits, summary=summary, away=away)


@app.get("/admin/attendance.csv")
@admin_required
def attendance_csv():
    month = request.args.get("month", "")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        abort(400)
    rows = db().execute("""SELECT date(c.at,'localtime') AS d, time(c.at,'localtime') AS t, m.code, m.name, m.plan, c.source
                           FROM checkins c JOIN members m ON m.id=c.member_id
                           WHERE strftime('%Y-%m', c.at,'localtime')=? ORDER BY c.at""", (month,)).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Time", "Member ID", "Name", "Plan", "Method"])
    for r in rows:
        w.writerow([r["d"], r["t"], r["code"], csv_safe(r["name"]), r["plan"], "Self check-in" if r["source"] == "self" else "Front desk"])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=attendance-{month}.csv"})


@app.errorhandler(404)
def not_found(_e):
    return render_template("message.html", title="Page not found", text="That page doesn’t exist."), 404


@app.errorhandler(400)
@app.errorhandler(429)
def bad_request(_e):
    return render_template("message.html", title="Something went wrong", text="Please go back, refresh the page and try again."), 400


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(E.get("PORT", 5000)), debug=False)
