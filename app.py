"""PayPilot AI: Flask backend (pages + JSON API)."""
import calendar
import hashlib
import hmac
import os
import random
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import quote, urlsplit

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import ai
import database
import paypal_api

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ai.load_env(os.path.join(BASE_DIR, ".env"))
APP_ENV = os.environ.get("PAYPILOT_ENV", "development").strip().lower()
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)


def load_secret_key():
    configured = os.environ.get("SECRET_KEY", "").strip()
    placeholders = {
        "dev-only-change-me",
        "change-me-to-a-long-random-string",
        "replace-with-a-random-secret-at-least-32-characters-long",
    }
    if configured and configured not in placeholders and len(configured) >= 32:
        return configured
    if APP_ENV == "production":
        raise RuntimeError("Set a strong SECRET_KEY before starting PayPilot in production.")

    secret_path = os.path.join(INSTANCE_DIR, "secret_key")
    try:
        fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        for _ in range(100):
            with open(secret_path, encoding="ascii") as secret_file:
                configured = secret_file.read().strip()
            if configured:
                return configured
            time.sleep(0.01)
        raise RuntimeError("PayPilot's local secret key could not be initialized.")
    with os.fdopen(fd, "w", encoding="ascii") as secret_file:
        configured = secrets.token_urlsafe(48)
        secret_file.write(configured)
    return configured


paypal_return_base_url = os.environ.get("PAYPAL_RETURN_BASE_URL", "").strip()
if not paypal_return_base_url and APP_ENV == "development":
    paypal_return_base_url = f"http://127.0.0.1:{os.environ.get('PORT', '5000')}"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    PAYPAL_RETURN_BASE_URL=paypal_return_base_url,
    DATABASE=os.environ.get("PAYPILOT_DB", os.path.join(BASE_DIR, "instance", "paypilot.db")),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=1024 * 1024,
)
os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
app.teardown_appcontext(database.close_db)
database.init_db(app)

EXPENSE_CATEGORIES = [
    "Food & Dining", "Groceries", "Transport", "Housing", "Utilities", "Entertainment",
    "Shopping", "Health", "Education", "Travel", "Subscriptions", "Savings", "Other",
]
INCOME_CATEGORIES = ["Salary", "Freelance", "Investments", "Gift", "Other Income"]
CURRENCIES = {"INR": "en-IN", "USD": "en-US", "EUR": "de-DE", "GBP": "en-GB"}
CYCLES = ("weekly", "monthly", "yearly")
FIXED_CATEGORIES = {"Housing", "Utilities", "Subscriptions"}  # paid once a month; not extrapolated


# ---------------------------------------------------------------- helpers -----
class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify(error=err.message), err.status


@app.errorhandler(404)
def handle_404(_err):
    if request.path.startswith("/api/"):
        return jsonify(error="Not found"), 404
    return redirect(url_for("index"))


def db():
    return database.get_db()


def rows(sql, args=()):
    return [dict(r) for r in db().execute(sql, args).fetchall()]


def row(sql, args=()):
    r = db().execute(sql, args).fetchone()
    return dict(r) if r else None


def today():
    return date.today()


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    if "user" not in g:
        g.user = row("SELECT id, name, email, currency FROM users WHERE id = ?", (uid,))
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required"), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    user = current_user()
    currency = user["currency"] if user else "INR"
    return {
        "user": user,
        "pp_config": {
            "currency": currency,
            "locale": CURRENCIES.get(currency, "en-IN"),
            "name": user["name"] if user else "",
            "expenseCategories": EXPENSE_CATEGORIES,
            "incomeCategories": INCOME_CATEGORIES,
            "today": today().isoformat(),
            "currencies": list(CURRENCIES),
        },
    }


def parse_amount(value, field="Amount"):
    try:
        x = round(float(value), 2)
    except (TypeError, ValueError):
        raise ApiError(f"{field} must be a number")
    if not (x > 0) or x > 1e9:
        raise ApiError(f"{field} must be greater than 0")
    return x


def parse_date(value, field="Date", required=True):
    if not value:
        if required:
            raise ApiError(f"{field} is required")
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ApiError(f"{field} must be YYYY-MM-DD")


def clean_text(value, field, max_len=120, required=True):
    text = str(value or "").strip()
    if required and not text:
        raise ApiError(f"{field} is required")
    if len(text) > max_len:
        raise ApiError(f"{field} must be at most {max_len} characters")
    return text


def valid_month(value):
    if value and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        return value
    return today().strftime("%Y-%m")


def month_range(month):
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)


def prev_month(month):
    start, _ = month_range(month)
    return (start - timedelta(days=1)).strftime("%Y-%m")


def month_label(month):
    return month_range(month)[0].strftime("%B %Y")


def add_months(d, n):
    carry, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + carry, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def month_list(count, end_month=None):
    """Last `count` months ending at end_month (oldest first), as YYYY-MM."""
    end = month_range(end_month or today().strftime("%Y-%m"))[0]
    return [add_months(end, -i).strftime("%Y-%m") for i in range(count - 1, -1, -1)]


def next_occurrence(date_str, cycle):
    orig, t = date.fromisoformat(date_str), today()
    if orig >= t:
        return orig
    if cycle == "weekly":
        return orig + timedelta(weeks=-(-(t - orig).days // 7))
    step = 1 if cycle == "monthly" else 12
    k = ((t.year - orig.year) * 12 + (t.month - orig.month)) // step * step
    d = add_months(orig, k)
    while d < t:
        k += step
        d = add_months(orig, k)
    return d


def monthly_cost(amount, cycle):
    return amount * {"weekly": 52 / 12, "monthly": 1, "yearly": 1 / 12}[cycle]


# ------------------------------------------------------- finance queries ------
def month_totals(uid, month):
    start, end = month_range(month)
    r = row(
        """SELECT COALESCE(SUM(CASE WHEN type='income' THEN amount END), 0) AS income,
                  COALESCE(SUM(CASE WHEN type='expense' THEN amount END), 0) AS expense,
                  COUNT(*) AS n
           FROM transactions WHERE user_id = ? AND tx_date >= ? AND tx_date < ?""",
        (uid, start.isoformat(), end.isoformat()),
    )
    return r


def category_breakdown(uid, start, end):
    items = rows(
        """SELECT category, SUM(amount) AS total, COUNT(*) AS n FROM transactions
           WHERE user_id = ? AND type = 'expense' AND tx_date >= ? AND tx_date < ?
           GROUP BY category ORDER BY total DESC""",
        (uid, start.isoformat(), end.isoformat()),
    )
    grand = sum(i["total"] for i in items) or 1
    for i in items:
        i["total"] = round(i["total"], 2)
        i["pct"] = round(i["total"] / grand * 100, 1)
    return items


def budget_status(uid, month):
    start, end = month_range(month)
    items = rows(
        """SELECT b.id, b.category, b.monthly_limit AS "limit",
                  COALESCE((SELECT SUM(t.amount) FROM transactions t
                            WHERE t.user_id = b.user_id AND t.type = 'expense' AND t.category = b.category
                              AND t.tx_date >= ? AND t.tx_date < ?), 0) AS spent
           FROM budgets b WHERE b.user_id = ? ORDER BY b.category""",
        (start.isoformat(), end.isoformat(), uid),
    )
    for b in items:
        b["spent"] = round(b["spent"], 2)
        b["pct"] = round(b["spent"] / b["limit"] * 100, 1)
        b["remaining"] = round(b["limit"] - b["spent"], 2)
        b["status"] = "over" if b["pct"] > 100 else "warning" if b["pct"] >= 80 else "ok"
    return items


def subscription_list(uid):
    items = rows("SELECT * FROM subscriptions WHERE user_id = ? ORDER BY is_active DESC, name COLLATE NOCASE", (uid,))
    t = today()
    for s in items:
        nxt = next_occurrence(s["next_billing_date"], s["billing_cycle"])
        s["next_date"] = nxt.isoformat()
        s["days_until"] = (nxt - t).days
        s["monthly_cost"] = round(monthly_cost(s["amount"], s["billing_cycle"]), 2)
        s["yearly_cost"] = round(s["monthly_cost"] * 12, 2)
    return items


def subscription_summary(items):
    active = [s for s in items if s["is_active"]]
    monthly = round(sum(s["monthly_cost"] for s in active), 2)
    upcoming = sorted((s for s in active if s["days_until"] <= 14), key=lambda s: s["days_until"])
    return {"count": len(active), "monthly_total": monthly, "yearly_total": round(monthly * 12, 2), "upcoming": upcoming}


def goal_list(uid):
    items = rows("SELECT * FROM goals WHERE user_id = ? ORDER BY id DESC", (uid,))
    t = today()
    for g_ in items:
        g_["pct"] = round(min(g_["saved_amount"] / g_["target_amount"] * 100, 100), 1)
        g_["remaining"] = round(max(g_["target_amount"] - g_["saved_amount"], 0), 2)
        g_["monthly_needed"] = None
        g_["days_left"] = None
        g_["status"] = "done" if g_["remaining"] == 0 else "active"
        if g_["deadline"]:
            days = (date.fromisoformat(g_["deadline"]) - t).days
            g_["days_left"] = days
            if g_["status"] == "active":
                if days < 0:
                    g_["status"] = "overdue"
                else:
                    g_["monthly_needed"] = round(g_["remaining"] / max(days / 30.44, 1), 2)
    return items


def build_summary(uid, month):
    start, end = month_range(month)
    tot = month_totals(uid, month)
    prev = month_totals(uid, prev_month(month))
    income, expense = round(tot["income"], 2), round(tot["expense"], 2)
    in_current = month == today().strftime("%Y-%m")
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    elapsed = today().day if in_current else days_in_month
    change = None
    if prev["expense"] > 0:
        change = round((expense - prev["expense"]) / prev["expense"] * 100, 1)
    top = rows(
        """SELECT description, category, amount, tx_date FROM transactions
           WHERE user_id = ? AND type = 'expense' AND tx_date >= ? AND tx_date < ?
           ORDER BY amount DESC LIMIT 5""",
        (uid, start.isoformat(), end.isoformat()),
    )
    categories = category_breakdown(uid, start, end)
    # Month-end projection: bills paid once a month (rent, utilities, subscriptions) are kept as-is and only
    # variable spending is extrapolated. Skipped in the first days of a month, when there is too little data.
    projected = None
    if in_current and elapsed >= 5 and expense:
        fixed = sum(c["total"] for c in categories if c["category"] in FIXED_CATEGORIES)
        projected = round(expense + (expense - fixed) / elapsed * (days_in_month - elapsed), 2)
    return {
        "month": month,
        "month_label": month_label(month),
        "income": income,
        "expenses": expense,
        "net": round(income - expense, 2),
        "savings_rate": round((income - expense) / income * 100, 1) if income > 0 else None,
        "txn_count": tot["n"],
        "prev_income": round(prev["income"], 2),
        "prev_expenses": round(prev["expense"], 2),
        "expense_change_pct": change,
        "avg_daily_spend": round(expense / max(elapsed, 1), 2),
        "projected_expenses": projected,
        "categories": categories,
        "top_expenses": top,
    }


def smart_alerts(uid, summary, budgets, subs):
    alerts = []
    for b in budgets:
        if b["status"] == "over":
            alerts.append({"level": "danger", "title": f"{b['category']} is over budget",
                           "text": f"Spent {b['pct']:.0f}% of the limit ({b['spent']:,.0f} of {b['limit']:,.0f})."})
        elif b["status"] == "warning":
            alerts.append({"level": "warning", "title": f"{b['category']} is nearing its limit",
                           "text": f"{b['pct']:.0f}% used, {b['remaining']:,.0f} left."})
    chg = summary["expense_change_pct"]
    if chg is not None and chg >= 15 and summary["expenses"] > 0:
        alerts.append({"level": "warning", "title": "Spending is up", "text": f"Expenses are {chg:.0f}% higher than last month."})
    if summary["projected_expenses"] and summary["income"] and summary["projected_expenses"] > summary["income"]:
        alerts.append({"level": "danger", "title": "Projected overspend",
                       "text": f"At this pace you'll spend about {summary['projected_expenses']:,.0f}, above this month's income."})
    for s in subs["upcoming"][:3]:
        when = "today" if s["days_until"] == 0 else f"in {s['days_until']} day{'s' if s['days_until'] != 1 else ''}"
        alerts.append({"level": "info", "title": f"{s['name']} renews {when}", "text": f"{s['amount']:,.0f} ({s['billing_cycle']})."})
    rate = summary["savings_rate"]
    if rate is not None and rate >= 20:
        alerts.append({"level": "success", "title": "Healthy savings rate", "text": f"You're saving {rate:.0f}% of income this month."})
    return alerts[:8]


def build_context(uid, month=None):
    """Compact snapshot sent to Gemini (and used by the offline fallback)."""
    month = month or today().strftime("%Y-%m")
    user = current_user()
    s = build_summary(uid, month)
    budgets = budget_status(uid, month)
    subs = subscription_list(uid)
    sub_sum = subscription_summary(subs)
    trend = []
    for m in month_list(4, month):
        t = month_totals(uid, m)
        trend.append({"month": m, "income": round(t["income"], 2), "expenses": round(t["expense"], 2)})
    return {
        "user_name": user["name"],
        "currency": user["currency"],
        "today": today().isoformat(),
        "month_label": s["month_label"],
        "current": {k: s[k] for k in ("income", "expenses", "net", "savings_rate", "txn_count", "prev_expenses",
                                      "expense_change_pct", "avg_daily_spend", "projected_expenses")},
        "top_categories": s["categories"][:8],
        "largest_expenses": s["top_expenses"],
        "budgets": [{k: b[k] for k in ("category", "limit", "spent", "pct", "status")} for b in budgets],
        "subscriptions": {
            "monthly_total": sub_sum["monthly_total"],
            "items": [{"name": x["name"], "amount": x["amount"], "cycle": x["billing_cycle"], "monthly_cost": x["monthly_cost"],
                       "next_date": x["next_date"]} for x in subs if x["is_active"]],
        },
        "goals": [{k: g_[k] for k in ("title", "target_amount", "saved_amount", "pct", "deadline", "monthly_needed")} for g_ in goal_list(uid)],
        "monthly_trend": trend,
    }


# ------------------------------------------------------------- pages ----------
@app.get("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.get("/register")
def register():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("register.html", currencies=list(CURRENCIES))


def page(name, template):
    @login_required
    def view():
        return render_template(template, page=name)
    view.__name__ = name
    app.add_url_rule(f"/{name}", name, view)


for _name in ("dashboard", "expenses", "budgets", "goals", "subscriptions", "assistant", "reports", "analytics"):
    page(_name, f"{_name}.html")


# -------------------------------------------------------------- auth API ------
_attempts = {}


def _rate_limited(key, limit=10, window=300):
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < window]
    _attempts[key] = hits
    return len(hits) >= limit


@app.post("/api/auth/register")
def api_register():
    d = body()
    name = clean_text(d.get("name"), "Name", 60)
    email = clean_text(d.get("email"), "Email", 120).lower()
    password = str(d.get("password") or "")
    currency = d.get("currency") if d.get("currency") in CURRENCIES else "INR"
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ApiError("Enter a valid email address")
    if len(password) < 8:
        raise ApiError("Password must be at least 8 characters")
    if row("SELECT id FROM users WHERE email = ?", (email,)):
        raise ApiError("An account with this email already exists", 409)
    cur = db().execute(
        "INSERT INTO users (name, email, password_hash, currency) VALUES (?, ?, ?, ?)",
        (name, email, generate_password_hash(password), currency),
    )
    db().commit()
    session.clear()
    session.permanent = True
    session["user_id"] = cur.lastrowid
    return jsonify(ok=True), 201


@app.post("/api/auth/login")
def api_login():
    d = body()
    email = str(d.get("email") or "").strip().lower()
    key = f"{request.remote_addr}:{email}"
    if _rate_limited(key):
        raise ApiError("Too many attempts. Try again in a few minutes.", 429)
    user = row("SELECT id, password_hash FROM users WHERE email = ?", (email,))
    if not user or not check_password_hash(user["password_hash"], str(d.get("password") or "")):
        _attempts.setdefault(key, []).append(time.time())
        raise ApiError("Incorrect email or password", 401)
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    return jsonify(ok=True)


@app.post("/api/auth/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


# -------------------------------------------------------------- PayPal API ----
def paypal_state_matches(order, state):
    if not order or not state:
        return False
    expected = order.get("return_token_hash")
    supplied = hashlib.sha256(state.encode("utf-8")).hexdigest()
    return bool(expected and hmac.compare_digest(expected, supplied))


def paypal_api_error(exc):
    return ApiError(str(exc), 502)


def paypal_callback_url(endpoint, **values):
    base = app.config["PAYPAL_RETURN_BASE_URL"].rstrip("/")
    return f"{base}{url_for(endpoint, _external=False, **values)}"


def paypal_sandbox_link(data, relation):
    if not isinstance(data, dict) or not isinstance(data.get("links", []), list):
        return None
    for link in data.get("links", []):
        if not isinstance(link, dict):
            continue
        try:
            parsed = urlsplit(link.get("href", ""))
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if (
            link.get("rel") == relation
            and parsed.scheme == "https"
            and (host == "sandbox.paypal.com" or host.endswith(".sandbox.paypal.com"))
        ):
            return link["href"]
    return None


@app.get("/api/payments")
@login_required
def api_payments():
    uid = current_user()["id"]
    connection = row(
        """SELECT merchant_id, status, client_id_encrypted, client_secret_encrypted
           FROM paypal_connections WHERE user_id = ?""",
        (uid,),
    )
    if connection:
        connection = {
            "merchant_id": connection["merchant_id"],
            "status": connection["status"],
            "has_credentials": bool(
                connection["client_id_encrypted"] and connection["client_secret_encrypted"]
            ),
        }
    order_items = rows(
        """SELECT id, amount, currency, description, status, capture_id, approval_url, created_at
           FROM paypal_orders WHERE user_id = ? ORDER BY id DESC LIMIT 50""",
        (uid,),
    )
    return jsonify(
        configured=paypal_api.configured(
            str(app.config["SECRET_KEY"]), app.config["PAYPAL_RETURN_BASE_URL"]
        ),
        environment="Sandbox",
        connection=connection,
        orders=order_items,
    )


@app.post("/api/paypal/connect")
@login_required
def api_paypal_connect():
    if not paypal_api.configured(
        str(app.config["SECRET_KEY"]), app.config["PAYPAL_RETURN_BASE_URL"]
    ):
        raise ApiError(
            "Secure PayPal credential storage is unavailable. The app owner must configure a strong SECRET_KEY and PAYPAL_RETURN_BASE_URL.",
            503,
        )
    uid = current_user()["id"]
    data = body()
    client_id = str(data.get("client_id") or "").strip()
    client_secret = str(data.get("client_secret") or "").strip()
    if not client_id or len(client_id) > 256:
        raise ApiError("Enter a valid PayPal Sandbox Client ID.")
    if not client_secret or len(client_secret) > 512:
        raise ApiError("Enter a valid PayPal Sandbox Client Secret.")
    try:
        paypal_api.validate_credentials(client_id, client_secret)
    except paypal_api.PayPalError as exc:
        raise ApiError(str(exc), exc.status) from exc
    encrypted_id, encrypted_secret = paypal_api.encrypt_credentials(
        client_id, client_secret, str(app.config["SECRET_KEY"])
    )
    db().execute(
        """INSERT INTO paypal_connections
           (user_id, tracking_id, merchant_id, status, client_id_encrypted, client_secret_encrypted)
           VALUES (?, ?, NULL, 'pending', ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               tracking_id=excluded.tracking_id, merchant_id=NULL, status='pending',
               client_id_encrypted=excluded.client_id_encrypted,
               client_secret_encrypted=excluded.client_secret_encrypted,
               updated_at=datetime('now')""",
        (uid, secrets.token_urlsafe(24), encrypted_id, encrypted_secret),
    )
    db().commit()
    return jsonify(credentials_saved=True)


@app.post("/api/payments/orders")
@login_required
def api_paypal_create_order():
    if not paypal_api.configured(
        str(app.config["SECRET_KEY"]), app.config["PAYPAL_RETURN_BASE_URL"]
    ):
        raise ApiError("PayPal order creation is unavailable. Ask the app owner to check server configuration.", 503)
    uid = current_user()["id"]
    connection = row(
        """SELECT merchant_id, client_id_encrypted, client_secret_encrypted
           FROM paypal_connections WHERE user_id=?""",
        (uid,),
    )
    if not connection or not connection["client_id_encrypted"] or not connection["client_secret_encrypted"]:
        raise ApiError("Enter and validate your PayPal Sandbox credentials first.", 409)

    data = body()
    try:
        amount = Decimal(str(data.get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ApiError("Amount must be a valid number.")
    if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000000"):
        raise ApiError("Amount must be greater than 0 and no more than 1,000,000,000.")
    currency = str(data.get("currency") or current_user()["currency"]).upper()
    if currency not in CURRENCIES:
        raise ApiError("Choose a supported currency.")
    merchant_id = str(data.get("merchant_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,64}", merchant_id):
        raise ApiError("Enter a valid PayPal merchant ID.")
    description = clean_text(data.get("description"), "Description", 127, required=False) or "PayPilot order"
    client_id, client_secret = paypal_api.decrypt_credentials(
        connection["client_id_encrypted"], connection["client_secret_encrypted"],
        str(app.config["SECRET_KEY"]),
    )
    return_state = secrets.token_urlsafe(32)
    return_hash = hashlib.sha256(return_state.encode("utf-8")).hexdigest()
    request_id, capture_request_id = uuid.uuid4().hex, uuid.uuid4().hex
    cur = db().execute(
        """INSERT INTO paypal_orders
           (user_id, merchant_id, amount, currency, description, status, request_id,
            capture_request_id, return_token_hash)
           VALUES (?, ?, ?, ?, ?, 'creating', ?, ?, ?)""",
        (uid, merchant_id, str(amount), currency, description, request_id,
         capture_request_id, return_hash),
    )
    local_id = cur.lastrowid
    db().commit()

    return_url = paypal_callback_url(
        "paypal_checkout_return", order_id=local_id, state=return_state
    )
    cancel_url = paypal_callback_url(
        "paypal_checkout_cancel", order_id=local_id, state=return_state
    )
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": str(local_id),
            "description": description,
            "amount": {"currency_code": currency, "value": format(amount, ".2f")},
            "payee": {"merchant_id": merchant_id},
        }],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                    "user_action": "PAY_NOW",
                    "shipping_preference": "NO_SHIPPING",
                }
            }
        },
    }
    try:
        order = paypal_api.request(
            "POST",
            "/v2/checkout/orders",
            payload,
            client_id=client_id,
            client_secret=client_secret,
            request_id=request_id,
        )
    except paypal_api.PayPalError as exc:
        db().execute(
            "UPDATE paypal_orders SET status='failed', updated_at=datetime('now') WHERE id=?",
            (local_id,),
        )
        db().commit()
        raise paypal_api_error(exc) from exc
    approval_url = paypal_sandbox_link(order, "approve") or paypal_sandbox_link(order, "payer-action")
    paypal_order_id = order.get("id") if isinstance(order, dict) else None
    if not paypal_order_id or not approval_url:
        db().execute(
            "UPDATE paypal_orders SET status='failed', updated_at=datetime('now') WHERE id=?",
            (local_id,),
        )
        db().commit()
        raise ApiError("PayPal did not return an order approval link.", 502)
    db().execute(
        """UPDATE paypal_orders SET paypal_order_id=?, approval_url=?, status='created',
           updated_at=datetime('now') WHERE id=? AND user_id=?""",
        (paypal_order_id, approval_url, local_id, uid),
    )
    db().execute(
        """UPDATE paypal_connections SET merchant_id=?, status='connected', updated_at=datetime('now')
           WHERE user_id=?""",
        (merchant_id, uid),
    )
    db().commit()
    return jsonify(order_id=local_id, approval_url=approval_url), 201


@app.post("/api/payments/orders/<int:order_id>/capture")
def api_paypal_capture_order(order_id):
    data = body()
    state = str(data.get("state") or "")
    paypal_order_id = str(data.get("paypal_order_id") or "")
    order = row("SELECT * FROM paypal_orders WHERE id=?", (order_id,))
    if not paypal_state_matches(order, state):
        raise ApiError("This payment return link is invalid or expired.", 403)
    if not order["paypal_order_id"] or not hmac.compare_digest(order["paypal_order_id"], paypal_order_id):
        raise ApiError("PayPal order did not match the pending payment.", 400)
    if order["status"] == "completed":
        return jsonify(status="COMPLETED", capture_id=order["capture_id"], amount=order["amount"],
                       currency=order["currency"], description=order["description"])
    if order["status"] != "created":
        raise ApiError("This order is not awaiting payment.", 409)
    created_at = datetime.strptime(order["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created_at > timedelta(hours=3):
        raise ApiError("This PayPal order has expired. Ask the seller to create a new order.", 410)

    connection = row(
        "SELECT client_id_encrypted, client_secret_encrypted FROM paypal_connections WHERE user_id=?",
        (order["user_id"],),
    )
    if not connection or not connection["client_id_encrypted"] or not connection["client_secret_encrypted"]:
        raise ApiError("PayPal credentials are no longer connected. Ask the seller to reconnect.", 409)
    client_id, client_secret = paypal_api.decrypt_credentials(
        connection["client_id_encrypted"], connection["client_secret_encrypted"],
        str(app.config["SECRET_KEY"]),
    )
    try:
        captured = paypal_api.request(
            "POST",
            f"/v2/checkout/orders/{quote(order['paypal_order_id'], safe='')}/capture",
            {},
            client_id=client_id,
            client_secret=client_secret,
            request_id=order["capture_request_id"],
        )
    except paypal_api.PayPalError as exc:
        raise paypal_api_error(exc) from exc

    if not isinstance(captured, dict):
        raise ApiError("PayPal returned an invalid capture response.", 502)
    purchase_units = captured.get("purchase_units") or []
    payments = purchase_units[0].get("payments", {}) if purchase_units else {}
    captures = payments.get("captures", [])
    capture = captures[0] if captures else {}
    captured_amount = capture.get("amount", {})
    payee_id = (purchase_units[0].get("payee") or {}).get("merchant_id") if purchase_units else None
    try:
        amount_matches = Decimal(str(captured_amount.get("value"))) == Decimal(order["amount"])
    except (InvalidOperation, ValueError):
        amount_matches = False
    if (
        captured.get("status") != "COMPLETED"
        or capture.get("status") != "COMPLETED"
        or payee_id != order["merchant_id"]
        or captured_amount.get("currency_code") != order["currency"]
        or not amount_matches
        or not capture.get("id")
    ):
        raise ApiError("PayPal did not confirm the expected completed payment.", 502)
    db().execute(
        """UPDATE paypal_orders SET status='completed', capture_id=?, updated_at=datetime('now')
           WHERE id=? AND status='created'""",
        (capture["id"], order_id),
    )
    db().commit()
    return jsonify(status="COMPLETED", capture_id=capture["id"], amount=order["amount"],
                   currency=order["currency"], description=order["description"])


@app.get("/paypal/checkout/return")
def paypal_checkout_return():
    order_id = request.args.get("order_id", type=int)
    state = request.args.get("state", "")
    paypal_order_id = request.args.get("token", "")
    order = row("SELECT * FROM paypal_orders WHERE id=?", (order_id,)) if order_id else None
    if not paypal_state_matches(order, state) or not paypal_order_id:
        return render_template("paypal_result.html", result="invalid")
    return render_template(
        "paypal_result.html", result="capture", order_id=order_id,
        state=state, paypal_order_id=paypal_order_id,
    )


@app.get("/paypal/checkout/cancel")
def paypal_checkout_cancel():
    order_id = request.args.get("order_id", type=int)
    state = request.args.get("state", "")
    order = row("SELECT * FROM paypal_orders WHERE id=?", (order_id,)) if order_id else None
    if not paypal_state_matches(order, state):
        return render_template("paypal_result.html", result="invalid")
    db().execute(
        """UPDATE paypal_orders SET status='cancelled', updated_at=datetime('now')
           WHERE id=? AND status='created'""",
        (order_id,),
    )
    db().commit()
    return render_template("paypal_result.html", result="cancelled")


# ------------------------------------------------------ transactions API ------
def tx_payload(d):
    tx_type = d.get("type")
    if tx_type not in ("expense", "income"):
        raise ApiError("Type must be expense or income")
    allowed = EXPENSE_CATEGORIES if tx_type == "expense" else INCOME_CATEGORIES
    category = d.get("category")
    if category not in allowed:
        raise ApiError("Choose a valid category")
    return (
        tx_type,
        parse_amount(d.get("amount")),
        category,
        clean_text(d.get("description"), "Description", 200, required=False),
        parse_date(d.get("date") or today().isoformat()),
    )


@app.get("/api/transactions")
@login_required
def api_transactions():
    uid = current_user()["id"]
    where, args = ["user_id = ?"], [uid]
    month = request.args.get("month")
    if month:
        start, end = month_range(valid_month(month))
        where += ["tx_date >= ?", "tx_date < ?"]
        args += [start.isoformat(), end.isoformat()]
    if request.args.get("type") in ("expense", "income"):
        where.append("type = ?")
        args.append(request.args["type"])
    if request.args.get("category"):
        where.append("category = ?")
        args.append(request.args["category"])
    q = request.args.get("q", "").strip()
    if q:
        where.append("(description LIKE ? OR category LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    clause = " AND ".join(where)
    items = rows(
        f"""SELECT id, type, amount, category, description, tx_date AS date FROM transactions
            WHERE {clause} ORDER BY tx_date DESC, id DESC LIMIT 500""", args)
    totals = row(
        f"""SELECT COALESCE(SUM(CASE WHEN type='income' THEN amount END),0) AS income,
                   COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0) AS expense
            FROM transactions WHERE {clause}""", args)
    return jsonify(items=items, totals=totals)


@app.post("/api/transactions")
@login_required
def api_transactions_create():
    uid = current_user()["id"]
    tx_type, amount, category, desc, tx_date = tx_payload(body())
    cur = db().execute(
        "INSERT INTO transactions (user_id, type, amount, category, description, tx_date) VALUES (?,?,?,?,?,?)",
        (uid, tx_type, amount, category, desc, tx_date),
    )
    db().commit()
    return jsonify(id=cur.lastrowid), 201


@app.put("/api/transactions/<int:tid>")
@login_required
def api_transactions_update(tid):
    uid = current_user()["id"]
    tx_type, amount, category, desc, tx_date = tx_payload(body())
    cur = db().execute(
        "UPDATE transactions SET type=?, amount=?, category=?, description=?, tx_date=? WHERE id=? AND user_id=?",
        (tx_type, amount, category, desc, tx_date, tid, uid),
    )
    db().commit()
    if cur.rowcount == 0:
        raise ApiError("Transaction not found", 404)
    return jsonify(ok=True)


@app.delete("/api/transactions/<int:tid>")
@login_required
def api_transactions_delete(tid):
    cur = db().execute("DELETE FROM transactions WHERE id=? AND user_id=?", (tid, current_user()["id"]))
    db().commit()
    if cur.rowcount == 0:
        raise ApiError("Transaction not found", 404)
    return jsonify(ok=True)


# ------------------------------------------------------------ budgets API -----
@app.get("/api/budgets")
@login_required
def api_budgets():
    month = valid_month(request.args.get("month"))
    items = budget_status(current_user()["id"], month)
    total_limit = sum(b["limit"] for b in items)
    total_spent = sum(b["spent"] for b in items)
    return jsonify(items=items, month=month, month_label=month_label(month),
                   total_limit=round(total_limit, 2), total_spent=round(total_spent, 2))


@app.post("/api/budgets")
@login_required
def api_budgets_upsert():
    uid, d = current_user()["id"], body()
    category = d.get("category")
    if category not in EXPENSE_CATEGORIES:
        raise ApiError("Choose a valid category")
    limit = parse_amount(d.get("monthly_limit"), "Monthly limit")
    db().execute(
        """INSERT INTO budgets (user_id, category, monthly_limit) VALUES (?,?,?)
           ON CONFLICT(user_id, category) DO UPDATE SET monthly_limit = excluded.monthly_limit""",
        (uid, category, limit),
    )
    db().commit()
    return jsonify(ok=True), 201


@app.delete("/api/budgets/<int:bid>")
@login_required
def api_budgets_delete(bid):
    cur = db().execute("DELETE FROM budgets WHERE id=? AND user_id=?", (bid, current_user()["id"]))
    db().commit()
    if cur.rowcount == 0:
        raise ApiError("Budget not found", 404)
    return jsonify(ok=True)


@app.get("/api/budgets/suggestions")
@login_required
def api_budget_suggestions():
    """Suggest limits from the last 3 full months of spending, plus a 50/30/20 split."""
    uid = current_user()["id"]
    cur_month = today().strftime("%Y-%m")
    months = month_list(4, cur_month)[:-1]  # three completed months
    start, _ = month_range(months[0])
    _, end = month_range(months[-1])
    spend = rows(
        """SELECT category, SUM(amount) AS total FROM transactions
           WHERE user_id=? AND type='expense' AND tx_date >= ? AND tx_date < ? GROUP BY category""",
        (uid, start.isoformat(), end.isoformat()),
    )
    existing = {b["category"] for b in rows("SELECT category FROM budgets WHERE user_id=?", (uid,))}
    items = []
    for s in spend:
        avg = s["total"] / 3
        if avg < 1 or s["category"] in existing:
            continue
        items.append({"category": s["category"], "avg_spend": round(avg, 2),
                      "suggested_limit": float(max(round(avg * 0.95 / 50) * 50, 50))})
    items.sort(key=lambda i: -i["avg_spend"])
    inc = row(
        """SELECT COALESCE(SUM(amount),0) AS total FROM transactions
           WHERE user_id=? AND type='income' AND tx_date >= ? AND tx_date < ?""",
        (uid, start.isoformat(), end.isoformat()),
    )["total"] / 3
    income = round(inc, 2)
    return jsonify(items=items[:8], income=income,
                   plan={"needs": round(income * .5, 2), "wants": round(income * .3, 2), "savings": round(income * .2, 2)})


# -------------------------------------------------------------- goals API -----
def goal_payload(d):
    return (
        clean_text(d.get("title"), "Goal name", 80),
        parse_amount(d.get("target_amount"), "Target amount"),
        parse_date(d.get("deadline"), "Deadline", required=False),
    )


@app.get("/api/goals")
@login_required
def api_goals():
    return jsonify(items=goal_list(current_user()["id"]))


@app.post("/api/goals")
@login_required
def api_goals_create():
    d = body()
    title, target, deadline = goal_payload(d)
    saved = 0.0
    if d.get("saved_amount") not in (None, "", 0, "0"):
        saved = parse_amount(d.get("saved_amount"), "Saved amount")
    cur = db().execute(
        "INSERT INTO goals (user_id, title, target_amount, saved_amount, deadline) VALUES (?,?,?,?,?)",
        (current_user()["id"], title, target, saved, deadline),
    )
    db().commit()
    return jsonify(id=cur.lastrowid), 201


@app.put("/api/goals/<int:gid>")
@login_required
def api_goals_update(gid):
    title, target, deadline = goal_payload(body())
    cur = db().execute("UPDATE goals SET title=?, target_amount=?, deadline=? WHERE id=? AND user_id=?",
                       (title, target, deadline, gid, current_user()["id"]))
    db().commit()
    if cur.rowcount == 0:
        raise ApiError("Goal not found", 404)
    return jsonify(ok=True)


@app.post("/api/goals/<int:gid>/contribute")
@login_required
def api_goals_contribute(gid):
    amount = parse_amount(body().get("amount"))
    cur = db().execute("UPDATE goals SET saved_amount = saved_amount + ? WHERE id=? AND user_id=?",
                       (amount, gid, current_user()["id"]))
    db().commit()
    if cur.rowcount == 0:
        raise ApiError("Goal not found", 404)
    return jsonify(ok=True)


@app.delete("/api/goals/<int:gid>")
@login_required
def api_goals_delete(gid):
    cur = db().execute("DELETE FROM goals WHERE id=? AND user_id=?", (gid, current_user()["id"]))
    db().commit()
    if cur.rowcount == 0:
        raise ApiError("Goal not found", 404)
    return jsonify(ok=True)


# ---------------------------------------------------- subscriptions API -------
def sub_payload(d):
    cycle = d.get("billing_cycle")
    if cycle not in CYCLES:
        raise ApiError("Choose a billing cycle")
    category = d.get("category") or "Subscriptions"
    if category not in EXPENSE_CATEGORIES:
        raise ApiError("Choose a valid category")
    return (
        clean_text(d.get("name"), "Name", 80),
        parse_amount(d.get("amount")),
        cycle,
        parse_date(d.get("next_billing_date"), "Next billing date"),
        category,
        0 if d.get("is_active") is False else 1,
    )


@app.get("/api/subscriptions")
@login_required
def api_subscriptions():
    items = subscription_list(current_user()["id"])
    return jsonify(items=items, summary=subscription_summary(items))


@app.post("/api/subscriptions")
@login_required
def api_subscriptions_create():
    name, amount, cycle, nxt, cat, active = sub_payload(body())
    cur = db().execute(
        """INSERT INTO subscriptions (user_id, name, amount, billing_cycle, next_billing_date, category, is_active)
           VALUES (?,?,?,?,?,?,?)""",
        (current_user()["id"], name, amount, cycle, nxt, cat, active),
    )
    db().commit()
    return jsonify(id=cur.lastrowid), 201


@app.put("/api/subscriptions/<int:sid>")
@login_required
def api_subscriptions_update(sid):
    name, amount, cycle, nxt, cat, active = sub_payload(body())
    cur = db().execute(
        """UPDATE subscriptions SET name=?, amount=?, billing_cycle=?, next_billing_date=?, category=?, is_active=?
           WHERE id=? AND user_id=?""",
        (name, amount, cycle, nxt, cat, active, sid, current_user()["id"]),
    )
    db().commit()
    if cur.rowcount == 0:
        raise ApiError("Subscription not found", 404)
    return jsonify(ok=True)


@app.delete("/api/subscriptions/<int:sid>")
@login_required
def api_subscriptions_delete(sid):
    cur = db().execute("DELETE FROM subscriptions WHERE id=? AND user_id=?", (sid, current_user()["id"]))
    db().commit()
    if cur.rowcount == 0:
        raise ApiError("Subscription not found", 404)
    return jsonify(ok=True)


# ------------------------------------------------------ dashboard / insights --
@app.get("/api/dashboard")
@login_required
def api_dashboard():
    uid = current_user()["id"]
    month = today().strftime("%Y-%m")
    summary = build_summary(uid, month)
    budgets = budget_status(uid, month)
    subs = subscription_list(uid)
    sub_sum = subscription_summary(subs)
    start, end = month_range(month)
    daily_raw = {r_["tx_date"]: r_["total"] for r_ in rows(
        """SELECT tx_date, SUM(amount) AS total FROM transactions
           WHERE user_id=? AND type='expense' AND tx_date >= ? AND tx_date < ? GROUP BY tx_date""",
        (uid, start.isoformat(), end.isoformat()))}
    days = today().day
    daily = [{"day": d_, "total": round(daily_raw.get((start + timedelta(days=d_ - 1)).isoformat(), 0), 2)}
             for d_ in range(1, days + 1)]
    recent = rows(
        """SELECT id, type, amount, category, description, tx_date AS date FROM transactions
           WHERE user_id=? ORDER BY tx_date DESC, id DESC LIMIT 6""", (uid,))
    has_data = bool(row("SELECT 1 AS x FROM transactions WHERE user_id=? LIMIT 1", (uid,)))
    return jsonify(
        summary=summary, budgets=budgets, goals=goal_list(uid)[:3], subscriptions=sub_sum,
        recent=recent, daily=daily, alerts=smart_alerts(uid, summary, budgets, sub_sum), has_data=has_data,
    )


@app.get("/api/summary")
@login_required
def api_summary():
    uid = current_user()["id"]
    month = valid_month(request.args.get("month"))
    subs = subscription_list(uid)
    return jsonify(summary=build_summary(uid, month), budgets=budget_status(uid, month),
                   subscriptions=subscription_summary(subs))


@app.get("/api/analytics")
@login_required
def api_analytics():
    uid = current_user()["id"]
    try:
        n = int(request.args.get("months", 6))
    except ValueError:
        n = 6
    n = n if n in (3, 6, 12) else 6
    months = month_list(n)
    start, _ = month_range(months[0])
    _, end = month_range(months[-1])
    monthly = []
    for m in months:
        t = month_totals(uid, m)
        monthly.append({"month": m, "label": month_range(m)[0].strftime("%b"), "income": round(t["income"], 2),
                        "expense": round(t["expense"], 2), "savings": round(t["income"] - t["expense"], 2)})
    categories = category_breakdown(uid, start, end)
    by_weekday = [0.0] * 7
    counts = [0] * 7
    for r_ in rows("SELECT tx_date, amount FROM transactions WHERE user_id=? AND type='expense' AND tx_date >= ? AND tx_date < ?",
                   (uid, start.isoformat(), end.isoformat())):
        wd = date.fromisoformat(r_["tx_date"]).weekday()
        by_weekday[wd] += r_["amount"]
        counts[wd] += 1
    weekday = [{"day": d_, "total": round(by_weekday[i], 2), "count": counts[i]}
               for i, d_ in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])]
    top_merchants = rows(
        """SELECT description AS name, SUM(amount) AS total, COUNT(*) AS n FROM transactions
           WHERE user_id=? AND type='expense' AND description != '' AND tx_date >= ? AND tx_date < ?
           GROUP BY LOWER(description) ORDER BY total DESC LIMIT 6""",
        (uid, start.isoformat(), end.isoformat()))
    largest = row(
        """SELECT description, category, amount, tx_date AS date FROM transactions
           WHERE user_id=? AND type='expense' AND tx_date >= ? AND tx_date < ? ORDER BY amount DESC LIMIT 1""",
        (uid, start.isoformat(), end.isoformat()))
    total_in = sum(m["income"] for m in monthly)
    total_out = sum(m["expense"] for m in monthly)
    active = [m for m in monthly if m["expense"] > 0] or monthly
    days = max((min(end, today() + timedelta(days=1)) - start).days, 1)
    return jsonify(
        months=n, monthly=monthly, categories=categories, weekday=weekday, top_merchants=top_merchants,
        stats={
            "avg_monthly_expense": round(sum(m["expense"] for m in active) / len(active), 2),
            "avg_daily_spend": round(total_out / days, 2),
            "savings_rate": round((total_in - total_out) / total_in * 100, 1) if total_in > 0 else None,
            "total_saved": round(total_in - total_out, 2),
            "largest_expense": largest,
        },
    )


# ------------------------------------------------------------ assistant API ---
@app.get("/api/assistant/status")
@login_required
def api_assistant_status():
    return jsonify(gemini=ai.gemini_configured(), model=ai.model_name() if ai.gemini_configured() else None)


@app.get("/api/assistant/history")
@login_required
def api_assistant_history():
    items = rows("SELECT id, role, content, mode, created_at FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 50",
                 (current_user()["id"],))
    return jsonify(items=list(reversed(items)))


@app.post("/api/assistant/chat")
@login_required
def api_assistant_chat():
    uid = current_user()["id"]
    message = clean_text(body().get("message"), "Message", 1500)
    history = list(reversed(rows("SELECT role, content FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 12", (uid,))))
    reply, mode, note = ai.chat(history, message, build_context(uid))
    db().execute("INSERT INTO chat_messages (user_id, role, content) VALUES (?, 'user', ?)", (uid, message))
    db().execute("INSERT INTO chat_messages (user_id, role, content, mode) VALUES (?, 'assistant', ?, ?)", (uid, reply, mode))
    db().commit()
    return jsonify(reply=reply, mode=mode, note=note)


@app.delete("/api/assistant/history")
@login_required
def api_assistant_clear():
    db().execute("DELETE FROM chat_messages WHERE user_id=?", (current_user()["id"],))
    db().commit()
    return jsonify(ok=True)


# --------------------------------------------------------------- reports API --
@app.post("/api/reports/generate")
@login_required
def api_reports_generate():
    uid = current_user()["id"]
    month = valid_month(body().get("month"))
    ctx = build_context(uid, month)
    if not ctx["current"]["txn_count"]:
        raise ApiError("No transactions in that month yet. Add some data first.")
    text, mode, note = ai.generate_report(ctx, month_label(month))
    title = f"Financial report: {month_label(month)}"
    cur = db().execute("INSERT INTO reports (user_id, month, title, content, mode) VALUES (?,?,?,?,?)",
                       (uid, month, title, text, mode))
    db().commit()
    return jsonify(id=cur.lastrowid, title=title, content=text, mode=mode, note=note), 201


@app.get("/api/reports")
@login_required
def api_reports():
    return jsonify(items=rows("SELECT id, month, title, mode, created_at FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 50",
                              (current_user()["id"],)))


@app.get("/api/reports/<int:rid>")
@login_required
def api_report(rid):
    r_ = row("SELECT id, month, title, content, mode, created_at FROM reports WHERE id=? AND user_id=?", (rid, current_user()["id"]))
    if not r_:
        raise ApiError("Report not found", 404)
    return jsonify(r_)


@app.delete("/api/reports/<int:rid>")
@login_required
def api_report_delete(rid):
    cur = db().execute("DELETE FROM reports WHERE id=? AND user_id=?", (rid, current_user()["id"]))
    db().commit()
    if cur.rowcount == 0:
        raise ApiError("Report not found", 404)
    return jsonify(ok=True)


# ------------------------------------------------------------------ demo data -
@app.post("/api/demo/seed")
@login_required
def api_demo_seed():
    uid = current_user()["id"]
    if row("SELECT 1 AS x FROM transactions WHERE user_id=? LIMIT 1", (uid,)):
        raise ApiError("Demo data can only be loaded into an empty account", 409)
    rng = random.Random(uid)
    scale = {"INR": 1, "USD": 0.012, "EUR": 0.011, "GBP": 0.0095}[current_user()["currency"]]

    def amt(x):
        return round(x * scale, 0 if scale == 1 else 2) or 1

    t = today()
    tx = []
    for back in (2, 1, 0):
        first = add_months(t.replace(day=1), -back)
        last_day = calendar.monthrange(first.year, first.month)[1]

        def put(kind, cat, desc, day, value):
            d_ = first.replace(day=min(day, last_day))
            if d_ <= t:
                tx.append((uid, kind, amt(value), cat, desc, d_.isoformat()))

        put("income", "Salary", "Monthly salary", 1, 85000)
        if back != 1:
            put("income", "Freelance", "Design project", rng.randint(10, 20), 12000)
        put("expense", "Housing", "Rent", 3, 22000)
        put("expense", "Utilities", "Electricity bill", 8, rng.randint(1800, 2600))
        put("expense", "Utilities", "Broadband", 10, 999)
        for desc, day, v in (("Netflix", 5, 649), ("Spotify", 12, 119), ("Gym membership", 15, 1500)):
            put("expense", "Subscriptions", desc, day, v)
        for _ in range(4):
            put("expense", "Groceries", rng.choice(["BigBasket", "Local market", "Supermart"]), rng.randint(1, 28), rng.randint(1400, 2800))
        for _ in range(11 if back == 0 else 8):
            put("expense", "Food & Dining", rng.choice(["Swiggy", "Zomato", "Cafe", "Restaurant"]), rng.randint(1, 28), rng.randint(280, 950))
        for _ in range(8):
            put("expense", "Transport", rng.choice(["Uber", "Metro card", "Fuel"]), rng.randint(1, 28), rng.randint(90, 650))
        for _ in range(3):
            put("expense", "Entertainment", rng.choice(["Movie night", "Concert", "Gaming"]), rng.randint(1, 28), rng.randint(300, 1500))
        for _ in range(2):
            put("expense", "Shopping", rng.choice(["Amazon", "Clothing", "Electronics"]), rng.randint(1, 28), rng.randint(1200, 3200))
        put("expense", "Health", "Pharmacy", rng.randint(5, 25), rng.randint(500, 1800))

    db().executemany("INSERT INTO transactions (user_id, type, amount, category, description, tx_date) VALUES (?,?,?,?,?,?)", tx)
    for cat, lim in (("Food & Dining", 6000), ("Groceries", 9000), ("Transport", 3000), ("Entertainment", 3000),
                     ("Shopping", 5000), ("Health", 2500), ("Utilities", 4000), ("Housing", 22000)):
        db().execute("INSERT OR IGNORE INTO budgets (user_id, category, monthly_limit) VALUES (?,?,?)", (uid, cat, amt(lim)))
    for title, target, saved, months_ahead in (("Emergency fund", 150000, 62000, 8), ("Goa trip", 40000, 18500, 3), ("New laptop", 90000, 21000, 6)):
        db().execute("INSERT INTO goals (user_id, title, target_amount, saved_amount, deadline) VALUES (?,?,?,?,?)",
                     (uid, title, amt(target), amt(saved), add_months(t, months_ahead).isoformat()))
    for name, value, cycle, offset in (("Netflix", 649, "monthly", 6), ("Spotify", 119, "monthly", 11), ("Gym membership", 1500, "monthly", 14),
                                       ("Cloud storage", 75, "monthly", 3), ("Prime membership", 1499, "yearly", 40)):
        db().execute("INSERT INTO subscriptions (user_id, name, amount, billing_cycle, next_billing_date, category) VALUES (?,?,?,?,?,?)",
                     (uid, name, amt(value), cycle, (t + timedelta(days=offset)).isoformat(), "Subscriptions"))
    db().commit()
    return jsonify(ok=True, transactions=len(tx)), 201


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
