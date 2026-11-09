"""Smoke tests for the PayPilot AI API. Run: python tests/test_api.py"""
import os
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

tmp = tempfile.mkdtemp()
os.environ["PAYPILOT_DB"] = os.path.join(tmp, "test.db")
os.environ["PAYPILOT_ENV"] = "test"
os.environ["GEMINI_API_KEY"] = ""  # force offline mode so tests never hit the network
os.environ["SECRET_KEY"] = "test-secret-key-for-fernet-encryption-which-is-long"
os.environ["PAYPAL_RETURN_BASE_URL"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as paypilot  # noqa: E402

c = paypilot.app.test_client()
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond else ""))
    if not cond:
        fails.append(name)


# auth
r = c.get("/api/transactions")
check("unauthenticated API is 401", r.status_code == 401)
r = c.post("/api/auth/register", json={"name": "Asha", "email": "asha@example.com", "password": "short"})
check("short password rejected", r.status_code == 400)
r = c.post("/api/auth/register", json={"name": "Asha", "email": "asha@example.com", "password": "password123", "currency": "INR"})
check("register", r.status_code == 201)
r = c.post("/api/auth/register", json={"name": "Asha", "email": "asha@example.com", "password": "password123"})
check("duplicate email 409", r.status_code == 409)
c.post("/api/auth/logout")
r = c.post("/api/auth/login", json={"email": "asha@example.com", "password": "wrong"})
check("bad login 401", r.status_code == 401)
r = c.post("/api/auth/login", json={"email": "ASHA@example.com", "password": "password123"})
check("login (case-insensitive email)", r.status_code == 200)

# empty-account behaviour
d = c.get("/api/dashboard").get_json()
check("empty dashboard", d["has_data"] is False and d["summary"]["savings_rate"] is None)
r = c.post("/api/assistant/chat", json={"message": "hello"})
j = r.get_json()
check("chat works offline + intro", r.status_code == 200 and j["mode"] == "offline" and "PayPilot AI" in j["reply"], str(j))

# validation
check("negative amount rejected", c.post("/api/transactions", json={"type": "expense", "amount": -5, "category": "Groceries"}).status_code == 400)
check("nan amount rejected", c.post("/api/transactions", json={"type": "expense", "amount": "nan", "category": "Groceries"}).status_code == 400)
check("bad category rejected", c.post("/api/transactions", json={"type": "expense", "amount": 5, "category": "Salary"}).status_code == 400)
check("bad date rejected", c.post("/api/transactions", json={"type": "expense", "amount": 5, "category": "Groceries", "date": "31/12/2026"}).status_code == 400)

# CRUD
r = c.post("/api/transactions", json={"type": "expense", "amount": 250.5, "category": "Groceries", "description": "Milk"})
tid = r.get_json()["id"]
check("create transaction", r.status_code == 201)
r = c.put(f"/api/transactions/{tid}", json={"type": "expense", "amount": 300, "category": "Groceries", "description": "Milk+eggs"})
check("update transaction", r.status_code == 200)
items = c.get("/api/transactions?q=eggs").get_json()["items"]
check("search finds updated row", len(items) == 1 and items[0]["amount"] == 300)
check("delete transaction", c.delete(f"/api/transactions/{tid}").status_code == 200)
check("delete missing 404", c.delete(f"/api/transactions/{tid}").status_code == 404)

# demo seed + derived endpoints
r = c.post("/api/demo/seed")
check("demo seed", r.status_code == 201, r.get_data(as_text=True))
check("demo seed refuses non-empty", c.post("/api/demo/seed").status_code == 409)
d = c.get("/api/dashboard").get_json()
s = d["summary"]
check("dashboard has data", d["has_data"] and s["txn_count"] > 0)
check("net = income - expenses", abs(s["net"] - (s["income"] - s["expenses"])) < 0.01)
check("category pct sums ~100", abs(sum(x["pct"] for x in s["categories"]) - 100) < 1)
check("daily series covers days so far", len(d["daily"]) == paypilot.today().day)
import calendar  # noqa: E402
elapsed, dim = paypilot.today().day, calendar.monthrange(paypilot.today().year, paypilot.today().month)[1]
if elapsed >= 5:
    naive = s["expenses"] / elapsed * dim
    check("projection >= spent so far", s["projected_expenses"] >= s["expenses"])
    check("projection does not extrapolate rent/bills linearly", s["projected_expenses"] < naive)
else:
    check("no projection in first 4 days of month", s["projected_expenses"] is None)
b = c.get("/api/budgets").get_json()
check("budgets computed", len(b["items"]) == 8 and all("status" in x for x in b["items"]))
sg = c.get("/api/budgets/suggestions").get_json()
check("budget suggestions", "plan" in sg and sg["income"] > 0)
a = c.get("/api/analytics?months=6").get_json()
check("analytics 6 months", len(a["monthly"]) == 6 and len(a["weekday"]) == 7)
check("analytics rejects odd period -> 6", len(c.get("/api/analytics?months=99").get_json()["monthly"]) == 6)
subs = c.get("/api/subscriptions").get_json()
check("subscriptions monthly total", subs["summary"]["count"] == 5 and subs["summary"]["monthly_total"] > 0)
check("yearly sub is /12", any(abs(x["monthly_cost"] - x["amount"] / 12) < 0.01 for x in subs["items"] if x["billing_cycle"] == "yearly"))
g = c.get("/api/goals").get_json()["items"]
check("goals computed", len(g) == 3 and all(x["monthly_needed"] for x in g))
check("contribute to goal", c.post(f"/api/goals/{g[0]['id']}/contribute", json={"amount": 1000}).status_code == 200)
check("summary endpoint", c.get("/api/summary?month=2026-10").status_code == 200)
check("invalid month falls back", c.get("/api/summary?month=garbage").get_json()["summary"]["month"] == paypilot.today().strftime("%Y-%m"))

# next_occurrence edge cases
from datetime import date  # noqa: E402
orig_today = paypilot.today
paypilot.today = lambda: date(2026, 11, 9)
check("monthly rolls forward", paypilot.next_occurrence("2026-01-31", "monthly") == date(2026, 11, 30))
check("yearly rolls forward", paypilot.next_occurrence("2024-02-29", "yearly") == date(2027, 2, 28))
check("weekly rolls forward", paypilot.next_occurrence("2026-11-01", "weekly") == date(2026, 11, 15))
check("future date unchanged", paypilot.next_occurrence("2027-01-01", "monthly") == date(2027, 1, 1))
paypilot.today = orig_today

# chat + reports (offline)
for q in ("where did my money go", "which subscriptions can I cut", "how are my goals", "am I over budget", "how can I save more"):
    j = c.post("/api/assistant/chat", json={"message": q}).get_json()
    check(f"chat: {q}", j["mode"] == "offline" and len(j["reply"]) > 30)
check("history stored", len(c.get("/api/assistant/history").get_json()["items"]) == 12)
r = c.post("/api/reports/generate", json={"month": paypilot.today().strftime("%Y-%m")})
check("report generated", r.status_code == 201 and "## Overview" in r.get_json()["content"])
check("empty month report rejected", c.post("/api/reports/generate", json={"month": "2020-01"}).status_code == 400)
rid = r.get_json()["id"]
check("report fetch", c.get(f"/api/reports/{rid}").status_code == 200)

# isolation between users
c2 = paypilot.app.test_client()
c2.post("/api/auth/register", json={"name": "Ravi", "email": "ravi@example.com", "password": "password123"})
check("user 2 sees nothing", c2.get("/api/transactions").get_json()["items"] == [])
check("user 2 cannot read user 1 report", c2.get(f"/api/reports/{rid}").status_code == 404)
check("user 2 cannot delete user 1 goal", c2.delete(f"/api/goals/{g[0]['id']}").status_code == 404)

# PayPal Sandbox credentials and order lifecycle, with external calls mocked.
check("PayPal endpoint reports unavailable secure setup", c.get("/api/payments").get_json()["configured"] is False)
check("example placeholder secret is not accepted for credential encryption", paypilot.paypal_api.configured(
    "replace-with-a-random-secret-at-least-32-characters-long", "http://127.0.0.1:5000"
) is False)
r = c.post("/api/paypal/connect", json={"client_id": "test-client", "client_secret": "test-secret"})
check("credentials are not accepted without secure server setup", r.status_code == 503)
dashboard_page = c.get("/dashboard")
check("dashboard payout dialog asks for credentials and merchant ID", dashboard_page.status_code == 200 and b"Set a payout" in dashboard_page.data and b"payout-client-secret" in dashboard_page.data and b"payout-merchant-id" in dashboard_page.data)
check("dashboard loads cache-busted current script", b"20261109-payout-credentials" in dashboard_page.data)
check("payments page removed from navigation", c.get("/payments").status_code == 302)
check("PayPal link validator rejects non-Sandbox hosts", paypilot.paypal_sandbox_link(
    {"links": [{"rel": "approve", "href": "https://example.com/approve"}]}, "approve"
) is None)
os.environ["PAYPAL_RETURN_BASE_URL"] = "http://127.0.0.1:5000"
paypilot.app.config["PAYPAL_RETURN_BASE_URL"] = os.environ["PAYPAL_RETURN_BASE_URL"]
original_paypal_request = paypilot.paypal_api.request
original_validate_credentials = paypilot.paypal_api.validate_credentials
paypal_order_payload = {}
paypal_capture_calls = []
credential_validations = []


def fake_validate_credentials(client_id, client_secret):
    credential_validations.append((client_id, client_secret))
    if client_secret != "valid-sandbox-secret":
        raise paypilot.paypal_api.PayPalError("PayPal Sandbox credentials were rejected", status=400)


def fake_paypal_request(method, path, payload=None, client_id=None, client_secret=None, request_id=None):
    if path == "/v2/checkout/orders":
        paypal_order_payload["data"] = payload
        paypal_order_payload["credentials"] = (client_id, client_secret)
        return {
            "id": "SANDBOX-ORDER-2",
            "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/approve"}],
        }
    if path == "/v2/checkout/orders/SANDBOX-ORDER-2/capture":
        paypal_capture_calls.append(path)
        punit = paypal_order_payload["data"]["purchase_units"][0]
        return {
            "id": "SANDBOX-ORDER-2",
            "status": "COMPLETED",
            "purchase_units": [{
                "payee": {"merchant_id": "SANDBOX-MERCHANT-2"},
                "payments": {"captures": [{
                    "id": "SANDBOX-CAPTURE-2",
                    "status": "COMPLETED",
                    "amount": punit["amount"],
                }]},
            }],
        }
    raise AssertionError(f"Unexpected PayPal request: {method} {path}")


paypilot.paypal_api.request = fake_paypal_request
paypilot.paypal_api.validate_credentials = fake_validate_credentials
try:
    r = c2.post("/api/paypal/connect", json={"client_id": "sandbox-client-2", "client_secret": "bad-secret"})
    check("reject invalid credentials without saving", r.status_code == 400 and c2.get("/api/payments").get_json()["connection"] is None)
    r = c2.post("/api/paypal/connect", json={"client_id": "sandbox-client-2", "client_secret": "valid-sandbox-secret"})
    check("validate and save merchant PayPal credentials", r.status_code == 200 and r.get_json()["credentials_saved"])
    status = c2.get("/api/payments").get_json()
    check("PayPal credentials are user-specific", status["connection"]["has_credentials"] is True)
    check("API never returns the saved PayPal secret", "client_secret_encrypted" not in str(status) and "valid-sandbox-secret" not in str(status))
    with paypilot.app.app_context():
        saved_credentials = paypilot.row(
            "SELECT client_id_encrypted, client_secret_encrypted FROM paypal_connections WHERE user_id=?",
            (2,),
        )
    check("PayPal credentials are encrypted at rest", saved_credentials["client_id_encrypted"] != "sandbox-client-2" and saved_credentials["client_secret_encrypted"] != "valid-sandbox-secret")
    other_payments = c.get("/api/payments").get_json()
    check("another user sees no PayPal connection or orders", other_payments["connection"] is None, str(other_payments))
    check("merchant ID required before order creation", c2.post("/api/payments/orders", json={"amount": 5}).status_code == 400)
    check("order amount is validated", c2.post("/api/payments/orders", json={"merchant_id": "SANDBOX-MERCHANT-2", "amount": -1}).status_code == 400)
    check("unconnected user cannot create orders", c.post("/api/payments/orders", json={"amount": 5}).status_code == 409)

    r = c2.post("/api/payments/orders", json={
        "merchant_id": "SANDBOX-MERCHANT-2",
        "amount": "5.25", "currency": "INR", "description": "Sandbox test order",
    })
    order = r.get_json()
    check("create PayPal order", r.status_code == 201 and order["approval_url"].endswith("/approve"))
    unit = paypal_order_payload["data"]["purchase_units"][0]
    check("order uses entered amount and merchant ID", unit["amount"] == {"currency_code": "INR", "value": "5.25"} and unit["payee"]["merchant_id"] == "SANDBOX-MERCHANT-2")
    check("PayPal order request uses saved merchant credentials", paypal_order_payload["credentials"] == ("sandbox-client-2", "valid-sandbox-secret"))
    return_url = paypal_order_payload["data"]["payment_source"]["paypal"]["experience_context"]["return_url"]
    return_query = parse_qs(urlparse(return_url).query)
    local_id = order["order_id"]
    capture_url = f"/api/payments/orders/{local_id}/capture"
    r = c2.post(capture_url, json={"state": "not-the-return-state", "paypal_order_id": "SANDBOX-ORDER-2"})
    check("capture endpoint rejects invalid return token", r.status_code == 403)
    r = c2.post(capture_url, json={
        "state": return_query["state"][0],
        "paypal_order_id": "SANDBOX-ORDER-2",
    })
    check("capture verifies successful PayPal payment", r.status_code == 200 and r.get_json()["status"] == "COMPLETED")
    r = c2.post(capture_url, json={
        "state": return_query["state"][0],
        "paypal_order_id": "SANDBOX-ORDER-2",
    })
    check("capture is idempotent after completion", r.status_code == 200 and len(paypal_capture_calls) == 1)
    orders = c2.get("/api/payments").get_json()["orders"]
    check("seller sees completed order history", len(orders) == 1 and orders[0]["status"] == "completed")
    check("merchant ID is saved with payout connection", c2.get("/api/payments").get_json()["connection"]["merchant_id"] == "SANDBOX-MERCHANT-2")
    check("seller can retrieve approval link from order history", orders[0]["approval_url"].endswith("/approve"))
    r = c2.get(
        f"/paypal/checkout/return?order_id={local_id}&state={return_query['state'][0]}&token=SANDBOX-ORDER-2"
    )
    check("buyer return page is available without seller login", r.status_code == 200 and b"Confirming payment" in r.data)
    check("PayPal checkout does not leak into expense transactions", not any("Sandbox test order" in t["description"] for t in c2.get("/api/transactions").get_json()["items"]))
finally:
    paypilot.paypal_api.request = original_paypal_request
    paypilot.paypal_api.validate_credentials = original_validate_credentials
    os.environ["PAYPAL_RETURN_BASE_URL"] = ""
    paypilot.app.config["PAYPAL_RETURN_BASE_URL"] = ""

# prompt integrity
import ai  # noqa: E402
check("system prompt verbatim start", ai.SYSTEM_PROMPT.startswith("You are PayPilot AI, an intelligent financial copilot."))
check("system prompt has Gemini rule", "Do not say you are Gemini unless the user explicitly asks." in ai.SYSTEM_PROMPT)

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
