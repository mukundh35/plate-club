# Plate Club – gym membership website (Python / Flask)

Members sign up and pay online with **Razorpay**; staff manage members from a password-protected **admin page**. Everything is Python: the web pages are built on the server from templates, so there is **no custom JavaScript** in this project. Data is stored in a SQLite database file.

## Files
- `app.py` – the whole backend (pages, payments, admin, database)
- `templates/` – the HTML pages (`index`, `join`, `pay`, `pass`, `checkin`, `attendance`, `login`, `admin`, `confirm`, `message`, `base`)
- `static/styles.css` – the look of the site (dark theme)
- `requirements.txt` – Python packages to install
- `.env.example` – settings template (copy it to `.env`)

## Attendance program
- **Member self check-in:** `/checkin` (the **Check in** link in the menu). A member types their member ID (shown on their pass, like GYM-1A2B3C) and the mobile number they joined with. Put this page on a tablet at the front desk. It shows their day streak, visits this month and total visits.
- **Front desk check-in:** the **Check in** button next to each member on the Admin page.
- **Attendance page:** `/admin/attendance` (Admin > Open attendance). Shows who came on any day, visits per member for any month, and active members who haven't visited in 7 days so staff can follow up. **Download CSV** gives a month's attendance for Excel.
- Rules: only active members can check in, once per day. Self check-ins are rate-limited.

## Admin login
The admin account comes from `ADMIN_EMAIL` and `ADMIN_PASSWORD` in `.env` (password 8+ characters). It is created on the first start, and a new email in `.env` adds another admin on the next start. If you forget the password or the login says "incorrect", stop the site and run `python reset_admin.py`. It sets the admin login to exactly what is in `.env`. Members and payments are not touched.

## Run it on your computer (fake payments, no Razorpay needed)
1. Install Python 3.10 or newer from python.org (tick "Add Python to PATH").
2. Open the folder in VS Code, then Terminal > New Terminal, and run:
   ```
   python -m venv .venv
   .venv\Scripts\activate          (Mac/Linux: source .venv/bin/activate)
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` (Windows PowerShell: `Copy-Item .env.example .env`) and set:
   ```
   ADMIN_EMAIL=your email
   ADMIN_PASSWORD=at least 8 characters
   DEMO_PAYMENTS=true
   ```
4. Run `python app.py` and open http://127.0.0.1:5000. Admin is at http://127.0.0.1:5000/admin.

No secret key is needed: the app makes its own and keeps it in the database.

## Turn on real payments
1. Create a Razorpay account and complete KYC. Use **Test Mode** keys first.
2. In `.env` set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`, and `DEMO_PAYMENTS=false`.
3. In Razorpay Dashboard > Webhooks add `https://YOUR-DOMAIN/api/webhook/razorpay` with events `payment.captured` and `order.paid`, choose a secret, and put it in `RAZORPAY_WEBHOOK_SECRET`.
4. Test with Razorpay's test UPI/cards, then switch to Live keys.

## Put it online
Use a host that runs Python (Render, Railway, PythonAnywhere, a VPS).
1. Push the folder to GitHub (`.env` and the database are git-ignored).
2. Build command `pip install -r requirements.txt`, start command `gunicorn app:app`.
3. Add the variables from `.env.example` in the host's settings and set `APP_ENV=production`.
4. Attach a **persistent disk** and set `DB_PATH` to a path on it (for example `/data/plateclub.db`), or the database is erased on each deploy.
5. Add your domain. HTTPS is required.

## Before you take real members
- Add a privacy policy, terms and a refund policy (Razorpay asks for these).
- Back up the database file regularly.
- Keep `.env` and the `.db` file private.

## Known limits
- Staff record renewals from the Admin page (cash/UPI at the desk). Online renewal isn't built yet.
- No email or SMS confirmations yet.
- Login attempt limits are kept in memory, so they reset on restart and aren't shared between several server processes.
